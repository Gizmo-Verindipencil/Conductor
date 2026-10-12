"""MCP OAuth authentication helpers.

This module provides functions to discover OAuth requirements for HTTP MCP servers
and fetch Azure AD tokens automatically, similar to how VS Code handles authentication.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import urllib.request
from typing import TYPE_CHECKING, Any
from urllib.error import URLError

from conductor.exceptions import ConfigurationError

if TYPE_CHECKING:
    from conductor.engine.secrets import SecretUseIndex


async def discover_oauth_requirements(url: str) -> dict[str, Any] | None:
    """Discover OAuth requirements for an HTTP MCP server.

    Checks for a .well-known/oauth-protected-resource endpoint to determine
    if the server requires OAuth authentication.

    Args:
        url: The base URL of the MCP server.

    Returns:
        OAuth metadata dict if the server requires OAuth, None otherwise.
        The dict contains 'resource', 'authorization_servers', and 'scopes_supported'.
    """
    # Ensure URL ends with /
    base_url = url.rstrip("/") + "/"
    well_known_url = f"{base_url}.well-known/oauth-protected-resource/"

    def fetch_metadata() -> dict[str, Any] | None:
        try:
            req = urllib.request.Request(well_known_url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError, json.JSONDecodeError):
            pass
        return None

    return await asyncio.to_thread(fetch_metadata)


def get_azure_token(scope: str) -> str | None:
    """Get an Azure AD token using the Azure CLI.

    Args:
        scope: The OAuth scope to request (e.g., 'api://xxx/mcp-user').

    Returns:
        The access token string, or None if token acquisition fails.
    """
    try:
        # Set PYTHONUTF8=1 so child Python processes use UTF-8 encoding
        # instead of the system default (cp1252 on Windows).
        env = {**os.environ, "PYTHONUTF8": "1"}
        result = subprocess.run(
            [
                "az",
                "account",
                "get-access-token",
                "--scope",
                scope,
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


async def get_mcp_oauth_headers(url: str, name: str) -> dict[str, str]:
    """Get OAuth headers for an HTTP MCP server if required.

    Discovers OAuth requirements and fetches an Azure AD token if needed.

    Args:
        url: The base URL of the MCP server.
        name: The name of the MCP server (for logging).

    Returns:
        Dict with Authorization header if OAuth is required and token
        acquisition succeeds, empty dict otherwise.
    """
    # Import here to avoid circular dependency
    from conductor.cli.run import verbose_log

    # Discover OAuth requirements
    oauth_metadata = await discover_oauth_requirements(url)
    if not oauth_metadata:
        return {}

    # Extract the scope from metadata
    scopes = oauth_metadata.get("scopes_supported", [])
    if not scopes:
        verbose_log(f"MCP server '{name}' requires OAuth but no scopes defined", style="yellow")
        return {}

    # Use the first scope (typically the main access scope)
    scope = scopes[0]
    verbose_log(f"MCP server '{name}' requires OAuth, fetching token for scope: {scope}")

    # Get Azure AD token
    token = await asyncio.to_thread(get_azure_token, scope)
    if not token:
        verbose_log(
            f"Failed to get Azure AD token for '{name}'. Run 'az login' first.", style="yellow"
        )
        return {}

    verbose_log(f"Successfully acquired OAuth token for '{name}'")
    return {"Authorization": f"Bearer {token}"}


async def resolve_mcp_server_auth(
    name: str,
    server_config: dict[str, Any],
) -> dict[str, Any]:
    """Resolve authentication for an HTTP/SSE MCP server.

    If the server requires OAuth and no Authorization header is provided,
    attempts to discover OAuth requirements and fetch a token.

    Args:
        name: The name of the MCP server.
        server_config: The server configuration dict.

    Returns:
        Updated server configuration with headers added if needed.
    """
    # Only process http/sse servers
    server_type = server_config.get("type", "stdio")
    if server_type not in ("http", "sse"):
        return server_config

    # Skip if Authorization header already provided
    headers = server_config.get("headers", {})
    if any(header.casefold() == "authorization" for header in headers):
        return server_config

    url = server_config.get("url")
    if not url:
        return server_config

    # Try to get OAuth headers
    oauth_headers = await get_mcp_oauth_headers(url, name)
    if oauth_headers:
        # Merge OAuth headers with existing headers
        updated_config = server_config.copy()
        updated_config["headers"] = {**headers, **oauth_headers}
        return updated_config

    return server_config


# Pattern for resolving ${VAR} and ${VAR:-default} in MCP env values. Kept in
# step with conductor.config.loader.ENV_VAR_PATTERN: "${{ expr }}" is not a var.
_ENV_VAR_PATTERN = re.compile(r"\$\{([^{}:]+)(?::-([^}]*))?\}")


def resolve_mcp_env_vars(env: dict[str, str]) -> dict[str, str]:
    """Resolve ${VAR} and ${VAR:-default} patterns in env values.

    Unlike the config loader which resolves at load time, this resolves
    at runtime from the current process environment. This allows users
    to reference environment variables (like API keys) in MCP server
    configuration without hardcoding them in the YAML.

    Syntax:
        - ${VAR} - Replace with value of VAR, or empty string if not set
        - ${VAR:-default} - Replace with value of VAR, or 'default' if not set

    Args:
        env: Dictionary of environment variable names to values,
             where values may contain ${VAR} patterns.

    Returns:
        New dictionary with all ${VAR} patterns resolved.

    Example:
        >>> import os
        >>> os.environ['MY_KEY'] = 'secret123'
        >>> resolve_mcp_env_vars({'API_KEY': '${MY_KEY}', 'DEBUG': '${DEBUG:-false}'})
        {'API_KEY': 'secret123', 'DEBUG': 'false'}
    """

    def replace_match(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_value = match.group(2)
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        elif default_value is not None:
            return default_value
        else:
            return ""

    resolved: dict[str, str] = {}
    for key, value in env.items():
        resolved[key] = _ENV_VAR_PATTERN.sub(replace_match, value)
    return resolved


def _delivery_collision(
    *,
    server_name: str,
    delivery_kind: str,
    delivery_name: str,
    secret_ref: str,
    existing_side: str,
) -> ConfigurationError:
    namespace = "environment variable" if delivery_kind == "env" else "HTTP header"
    return ConfigurationError(
        f"Secret binding '{secret_ref}' delivery {namespace} '{delivery_name}' for MCP server "
        f"'{server_name}' collides with {existing_side}.",
        suggestion=f"Use a unique {namespace} name for each literal and secret binding delivery.",
    )


async def resolve_mcp_server_config(
    name: str,
    server_config: dict[str, Any],
    *,
    secret_uses: SecretUseIndex | None = None,
) -> dict[str, Any]:
    """Apply Conductor's full resolution pipeline to one MCP server config.

    The single place both server sources agree on. A workflow-declared
    server (``runtime.mcp_servers``) and a plugin-declared one
    (``.mcp.json``) reach the SDK by different routes, and before this
    existed only the first was resolved — so a plugin's stdio server was
    handed a literal ``${TOKEN}`` and its http server attached with no
    ``Authorization`` header. The server loaded and did not work, which is
    the silent-divergence failure plugins exist to remove.

    Args:
        name: Server name, used for OAuth token cache keying and messages.
        server_config: Resolved-shape config dict (``type`` plus the
            transport's own fields).
        secret_uses: Optional root-config secret-use index. Declared values
            are read from its cache-backed API only at delivery time; the
            index itself never stores plaintext.

    Returns:
        A new dict with ``env`` placeholders expanded and, for http/sse,
        any discovered OAuth ``Authorization`` header merged in. The input
        is never mutated — plugin configs are shared across agents.
    """
    resolved = dict(server_config)
    env = resolved.get("env")
    if isinstance(env, dict) and env:
        resolved["env"] = resolve_mcp_env_vars(env)

    if secret_uses is not None:
        literal_env = resolved.get("env")
        env_values: dict[str, str] = dict(literal_env) if isinstance(literal_env, dict) else {}
        literal_env_keys = {
            (key.casefold() if sys.platform == "win32" else key): key for key in env_values
        }
        binding_env: dict[str, str] = {}

        literal_headers = resolved.get("headers")
        header_values: dict[str, str] = (
            dict(literal_headers) if isinstance(literal_headers, dict) else {}
        )
        literal_header_keys = {key.casefold(): key for key in header_values}
        binding_headers: dict[str, str] = {}

        for use in secret_uses.deliveries_for_server(name):
            delivery_name = use.delivery_name
            if use.delivery_kind == "env":
                key = delivery_name.casefold() if sys.platform == "win32" else delivery_name
                if key in literal_env_keys:
                    raise _delivery_collision(
                        server_name=name,
                        delivery_kind="env",
                        delivery_name=delivery_name,
                        secret_ref=use.ref,
                        existing_side=(f"literal environment variable '{literal_env_keys[key]}'"),
                    )
                if key in binding_env:
                    raise _delivery_collision(
                        server_name=name,
                        delivery_kind="env",
                        delivery_name=delivery_name,
                        secret_ref=use.ref,
                        existing_side=(
                            f"secret binding '{binding_env[key]}' delivery environment variable "
                            f"'{delivery_name}'"
                        ),
                    )
                binding_env[key] = use.ref
                env_values[delivery_name] = secret_uses.value_for(use.ref)
                resolved["env"] = env_values
                continue

            key = delivery_name.casefold()
            if key in literal_header_keys:
                raise _delivery_collision(
                    server_name=name,
                    delivery_kind="header",
                    delivery_name=delivery_name,
                    secret_ref=use.ref,
                    existing_side=f"literal HTTP header '{literal_header_keys[key]}'",
                )
            if key in binding_headers:
                raise _delivery_collision(
                    server_name=name,
                    delivery_kind="header",
                    delivery_name=delivery_name,
                    secret_ref=use.ref,
                    existing_side=(
                        f"secret binding '{binding_headers[key]}' delivery HTTP header "
                        f"'{delivery_name}'"
                    ),
                )
            binding_headers[key] = use.ref
            header_values[delivery_name] = secret_uses.value_for(use.ref)
            resolved["headers"] = header_values

    return await resolve_mcp_server_auth(name, resolved)


async def resolve_mcp_servers(servers: dict[str, Any]) -> dict[str, Any]:
    """Resolve a whole name-keyed mapping of MCP server configs.

    Args:
        servers: Mapping of server name to config dict.

    Returns:
        A new mapping with every entry resolved by
        :func:`resolve_mcp_server_config`.
    """
    return {name: await resolve_mcp_server_config(name, config) for name, config in servers.items()}
