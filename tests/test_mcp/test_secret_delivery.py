"""Secret delivery and runtime-value redaction at the MCP boundary."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor import redaction
from conductor.cli.run import _build_mcp_servers
from conductor.config.environment import (
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
    SecretBinding,
    SecretBindingSource,
)
from conductor.config.schema import (
    MCPServerDef,
    RouteDef,
    RuntimeConfig,
    SecretDelivery,
    SetStepDef,
    StepSecretRef,
    ToolOutputConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.secrets import SecretUseIndex, SecretValueCache, index_config
from conductor.exceptions import ConfigurationError
from conductor.mcp_auth import resolve_mcp_server_config
from conductor.redaction import REDACTED_MARKER, RunRedactor

_SECRET = "task-eight-secret-value"
_SECOND_SECRET = "task-eight-second-value"
_SOURCE = "CONDUCTOR_TEST_TASK8_SECRET"
_SECOND_SOURCE = "CONDUCTOR_TEST_TASK8_SECOND_SECRET"


def _secret(ref: str, *, env: str | None = None, header: str | None = None) -> StepSecretRef:
    return StepSecretRef(
        ref=ref,
        scope="mcp",
        delivery=SecretDelivery(env=env, header=header),
    )


def _config(servers: dict[str, MCPServerDef]) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="mcp-secret-delivery",
            entry_point="done",
            runtime=RuntimeConfig.model_validate({"provider": "copilot", "mcp_servers": servers}),
        ),
        agents=[SetStepDef(name="done", value="'ok'", routes=[RouteDef(to="$end")])],
    )


def _secret_index(
    monkeypatch: pytest.MonkeyPatch,
    servers: dict[str, MCPServerDef],
) -> SecretUseIndex:
    monkeypatch.setenv(_SOURCE, _SECRET)
    monkeypatch.setenv(_SECOND_SOURCE, _SECOND_SECRET)
    document = EnvironmentDocument(
        default="local",
        profiles={"local": ProfileDefinition(backend="local")},
        secrets={
            "token": SecretBinding(source=SecretBindingSource(env=_SOURCE), allow=["mcp"]),
            "second": SecretBinding(source=SecretBindingSource(env=_SECOND_SOURCE), allow=["mcp"]),
        },
    )
    environment = ResolvedEnvironment(
        document=document,
        name="task-eight",
        source="path",
        path=None,
        digest="sha256:task-eight",
    )
    cache = SecretValueCache(environment, RunRedactor())
    return index_config(_config(servers), cache)


@pytest.mark.asyncio
async def test_stdio_env_delivery_preserves_legacy_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: legacy ${VAR} expansion runs first, then a binding value is
    # delivered into the same stdio env mapping without changing missing→"".
    monkeypatch.delenv("CONDUCTOR_TEST_TASK8_MISSING", raising=False)
    server = MCPServerDef(
        command="server",
        env={"AMBIENT": "${CONDUCTOR_TEST_TASK8_MISSING}"},
        secrets=[_secret("token", env="BOUND_TOKEN")],
    )
    uses = _secret_index(monkeypatch, {"stdio": server})

    resolved = await resolve_mcp_server_config(
        "stdio",
        {"type": "stdio", "command": "server", "env": server.env},
        secret_uses=uses,
    )

    assert resolved["env"] == {"AMBIENT": "", "BOUND_TOKEN": _SECRET}


@pytest.mark.asyncio
async def test_http_header_delivery_suppresses_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: a binding-backed Authorization header is installed before
    # OAuth resolution and suppresses discovery exactly like a literal header.
    server = MCPServerDef(
        type="http",
        url="https://example.test/mcp",
        secrets=[_secret("token", header="Authorization")],
    )
    uses = _secret_index(monkeypatch, {"remote": server})

    with patch("conductor.mcp_auth.get_mcp_oauth_headers", new_callable=AsyncMock) as oauth:
        resolved = await resolve_mcp_server_config(
            "remote",
            {"type": "http", "url": server.url},
            secret_uses=uses,
        )

    assert resolved["headers"] == {"Authorization": _SECRET}
    oauth.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_mcp_servers_threads_deliveries_to_both_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: the CLI builder passes one prepared index to stdio and HTTP
    # resolution so both transport-specific delivery maps receive plaintext.
    servers = {
        "stdio": MCPServerDef(command="server", secrets=[_secret("token", env="BOUND_TOKEN")]),
        "remote": MCPServerDef(
            type="http",
            url="https://example.test/mcp",
            secrets=[_secret("second", header="X-Api-Key")],
        ),
    }
    uses = _secret_index(monkeypatch, servers)

    with patch("conductor.mcp_auth.get_mcp_oauth_headers", new=AsyncMock(return_value={})):
        resolved = await _build_mcp_servers(_config(servers), secrets=uses)

    assert resolved is not None
    assert resolved["stdio"]["env"]["BOUND_TOKEN"] == _SECRET
    assert resolved["remote"]["headers"]["X-Api-Key"] == _SECOND_SECRET


@pytest.mark.asyncio
async def test_missing_index_rejects_declared_refs() -> None:
    # Requirement: declared MCP secret refs cannot silently disappear when a
    # caller omitted the prepared SecretUseIndex.
    config = _config(
        {"stdio": MCPServerDef(command="server", secrets=[_secret("token", env="BOUND_TOKEN")])}
    )

    with pytest.raises(ConfigurationError, match="no resolved secret-use index"):
        await _build_mcp_servers(config)


@pytest.mark.asyncio
async def test_literal_env_collision_is_posix_case_sensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: POSIX env names collide case-sensitively, so differently
    # cased literal and binding names remain distinct.
    server = MCPServerDef(
        command="server",
        env={"token": "literal"},
        secrets=[_secret("token", env="TOKEN")],
    )
    uses = _secret_index(monkeypatch, {"stdio": server})
    monkeypatch.setattr("conductor.mcp_auth.sys.platform", "linux")

    resolved = await resolve_mcp_server_config(
        "stdio",
        {"type": "stdio", "command": "server", "env": server.env},
        secret_uses=uses,
    )

    assert resolved["env"] == {"token": "literal", "TOKEN": _SECRET}


@pytest.mark.asyncio
async def test_literal_env_collision_on_windows_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: Windows env names collide case-insensitively and the
    # backstop error names both the secret binding and literal delivery sides.
    server = MCPServerDef(
        command="server",
        env={"token": "literal"},
        secrets=[_secret("token", env="TOKEN")],
    )
    uses = _secret_index(monkeypatch, {"stdio": server})
    monkeypatch.setattr("conductor.mcp_auth.sys.platform", "win32")

    with pytest.raises(ConfigurationError, match=r"binding 'token'.*literal environment"):
        await resolve_mcp_server_config(
            "stdio",
            {"type": "stdio", "command": "server", "env": server.env},
            secret_uses=uses,
        )


@pytest.mark.asyncio
async def test_literal_header_collision_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: HTTP header collisions are case-insensitive on every
    # platform, including authorization vs Authorization.
    server = MCPServerDef(
        type="http",
        url="https://example.test/mcp",
        headers={"authorization": "literal"},
        secrets=[_secret("token", header="Authorization")],
    )
    uses = _secret_index(monkeypatch, {"remote": server})

    with pytest.raises(ConfigurationError, match=r"binding 'token'.*literal HTTP header"):
        await resolve_mcp_server_config(
            "remote",
            {"type": "http", "url": server.url, "headers": server.headers},
            secret_uses=uses,
        )


@pytest.mark.asyncio
async def test_binding_duplicate_delivery_is_backstopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: resolver delivery remains a backstop for two binding uses
    # targeting the same name even if prepare-time validation was bypassed.
    server = MCPServerDef(
        command="server",
        secrets=[
            _secret("token", env="TOKEN"),
            _secret("second", env="TOKEN"),
        ],
    )
    uses = _secret_index(monkeypatch, {"stdio": server})

    with pytest.raises(ConfigurationError, match=r"binding 'second'.*binding 'token'"):
        await resolve_mcp_server_config(
            "stdio", {"type": "stdio", "command": "server"}, secret_uses=uses
        )


def _manager(tool_output: ToolOutputConfig | None = None) -> Any:
    with patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True):
        from conductor.mcp.manager import MCPManager

        return MCPManager(tool_output=tool_output)


def _tool_result(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    result = MagicMock()
    result.content = [block]
    result.structuredContent = None
    return result


async def _log_tool_arguments(manager: Any) -> None:
    session = AsyncMock()
    session.call_tool.return_value = _tool_result("safe response")
    manager.tool_to_server["server__tool"] = "server"
    manager.sessions["server"] = session
    text_type = type(session.call_tool.return_value.content[0])
    with patch("conductor.mcp.manager.TextContent", text_type):
        await manager.call_tool("server__tool", {"token": _SECRET})


async def _log_response_preview(manager: Any) -> None:
    session = AsyncMock()
    session.call_tool.return_value = _tool_result(f"response {_SECRET}")
    manager.tool_to_server["server__tool"] = "server"
    manager.sessions["server"] = session
    text_type = type(session.call_tool.return_value.content[0])
    with patch("conductor.mcp.manager.TextContent", text_type):
        await manager.call_tool("server__tool", {})


def _make_preview_boundary_exercise(marker_width: int) -> Callable[[Any], Awaitable[None]]:
    """A response whose secret starts ``marker_width`` chars before the preview cut."""

    async def _exercise(manager: Any) -> None:
        # Requirement (review r5): the debug preview is built from the
        # COMPLETE scrubbed response and only then truncated to 200 chars —
        # a secret starting inside the preview window must not leave its
        # leading characters in the log (exact-match redaction cannot see a
        # value the slice truncated), at every marker-split width.
        session = AsyncMock()
        session.call_tool.return_value = _tool_result(f"{'x' * (200 - marker_width)}{_SECRET}")
        manager.tool_to_server["server__tool"] = "server"
        manager.sessions["server"] = session
        text_type = type(session.call_tool.return_value.content[0])
        with patch("conductor.mcp.manager.TextContent", text_type):
            await manager.call_tool("server__tool", {})

    return _exercise


async def _log_tool_exception(manager: Any) -> None:
    session = AsyncMock()
    session.call_tool.side_effect = RuntimeError(f"tool stderr {_SECRET}")
    manager.tool_to_server["server__tool"] = "server"
    manager.sessions["server"] = session
    with pytest.raises(RuntimeError) as raised:
        await manager.call_tool("server__tool", {})
    assert _SECRET not in str(raised.value)


async def _log_truncation_exception(manager: Any) -> None:
    session = AsyncMock()
    session.call_tool.return_value = _tool_result("safe response")
    manager.tool_to_server["server__tool"] = "server"
    manager.sessions["server"] = session
    with (
        patch("conductor.mcp.manager.TextContent", type(session.call_tool.return_value.content[0])),
        patch.object(
            manager,
            "_maybe_truncate_response",
            side_effect=RuntimeError(f"truncation {_SECRET}"),
        ),
    ):
        await manager.call_tool("server__tool", {})


@asynccontextmanager
async def _raising_stdio(_params: Any) -> AsyncIterator[tuple[Any, Any]]:
    raise RuntimeError(f"connect stderr {_SECRET}")
    yield MagicMock(), MagicMock()


async def _log_connect_exception(manager: Any) -> None:
    with (
        patch("conductor.mcp.manager.StdioServerParameters", return_value=MagicMock()),
        patch("conductor.mcp.manager.stdio_client", side_effect=_raising_stdio),
        patch("conductor.mcp.manager.ClientSession", MagicMock()),
        pytest.raises(RuntimeError) as raised,
    ):
        await manager.connect_server(name="server", command="server")
    assert _SECRET not in str(raised.value)


class _CancellationFailure:
    @asynccontextmanager
    async def stdio(self, _params: Any) -> AsyncIterator[tuple[Any, Any]]:
        try:
            yield MagicMock(), MagicMock()
        finally:
            raise RuntimeError(f"cancel cleanup {_SECRET}")

    @asynccontextmanager
    async def session(self, _read: Any, _write: Any) -> AsyncIterator[AsyncMock]:
        session = AsyncMock()
        session.initialize = AsyncMock(side_effect=self._initialize)
        yield session

    async def _initialize(self) -> None:
        await asyncio.Event().wait()


async def _log_cancellation_exception(manager: Any) -> None:
    failure = _CancellationFailure()
    with (
        patch("conductor.mcp.manager.StdioServerParameters", return_value=MagicMock()),
        patch("conductor.mcp.manager.stdio_client", side_effect=failure.stdio),
        patch("conductor.mcp.manager.ClientSession", side_effect=failure.session),
    ):
        connecting = asyncio.create_task(manager.connect_server(name="server", command="server"))
        await asyncio.sleep(0)
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exercise",
    [
        pytest.param(_log_tool_arguments, id="tool-arguments"),
        pytest.param(_log_response_preview, id="response-preview"),
        pytest.param(_make_preview_boundary_exercise(1), id="response-preview-boundary-width-1"),
        pytest.param(_make_preview_boundary_exercise(7), id="response-preview-boundary-width-7"),
        pytest.param(_make_preview_boundary_exercise(13), id="response-preview-boundary-width-13"),
        pytest.param(_log_tool_exception, id="tool-exception"),
        pytest.param(_log_truncation_exception, id="truncation-exception"),
        pytest.param(_log_connect_exception, id="connect-exception"),
        pytest.param(_log_cancellation_exception, id="cancellation-exception"),
    ],
)
async def test_manager_dynamic_log_sinks_scrub_active_secret(
    exercise: Callable[[Any], Awaitable[None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement: every manager logger sink carrying runtime-derived data,
    # and every public RuntimeError it builds, uses the active run redactor.
    active = RunRedactor()
    active.register([_SECRET])
    token = redaction.set_current(active)
    caplog.set_level(logging.DEBUG, logger="conductor.mcp.manager")
    try:
        await exercise(_manager())
    finally:
        redaction.reset_current(token)

    assert _SECRET not in caplog.text
    assert REDACTED_MARKER in caplog.text


@pytest.mark.asyncio
async def test_manager_preview_is_byte_stable_without_active_redactor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement (review r5, zero-noise round): with NO active redactor an
    # ordinary response containing a literal ``***redacted***`` crossing the
    # 200-char preview boundary logs exactly the pre-fix preview — the
    # marker completion must not fire when no replacement happened.
    manager = _manager()
    session = AsyncMock()
    session.call_tool.return_value = _tool_result(f"{'y' * 190}{REDACTED_MARKER}tail")
    manager.tool_to_server["server__tool"] = "server"
    manager.sessions["server"] = session
    text_type = type(session.call_tool.return_value.content[0])
    caplog.set_level(logging.DEBUG, logger="conductor.mcp.manager")
    with patch("conductor.mcp.manager.TextContent", text_type):
        await manager.call_tool("server__tool", {})

    message = next(r.getMessage() for r in caplog.records if "returned:" in r.getMessage())
    # The preview is cut mid-token at exactly 200 chars, as before the fix.
    assert f"{'y' * 190}{REDACTED_MARKER[:10]}..." in message
    assert f"{'y' * 190}{REDACTED_MARKER}" not in message


@pytest.mark.asyncio
async def test_manager_scrubs_full_response_before_tool_result_truncation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement (review fix C): logging redacts the complete tool response
    # before result truncation can split a registered secret at max_chars.
    secret = "boundary-secret-" + "s" * 1199
    full_response = "x" * 190 + secret
    manager = _manager(ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=False))
    session = AsyncMock()
    session.call_tool.return_value = _tool_result(full_response)
    manager.tool_to_server["server__tool"] = "server"
    manager.sessions["server"] = session
    text_type = type(session.call_tool.return_value.content[0])
    active = RunRedactor()
    active.register([secret])
    token = redaction.set_current(active)
    caplog.set_level(logging.DEBUG, logger="conductor.mcp.manager")
    try:
        with patch("conductor.mcp.manager.TextContent", text_type):
            result = await manager.call_tool("server__tool", {})
    finally:
        redaction.reset_current(token)

    assert secret not in caplog.text
    assert secret[:10] not in caplog.text
    assert REDACTED_MARKER in caplog.text
    assert result.startswith(full_response[:1000])
    assert "[output truncated: 1405 chars -> 1000 kept." in result
    assert REDACTED_MARKER not in result
