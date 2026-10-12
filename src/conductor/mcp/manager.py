"""MCP server manager for spawning and managing MCP server connections.

This module provides the MCPManager class that handles:
- Spawning MCP server processes using stdio transport
- Collecting tools from connected servers
- Executing tool calls and returning results
- Managing server lifecycle (connect, close)

Oversized tool results can be truncated to a per-result character limit. The full
text is optionally spilled to a temporary file so no data is lost. The
resulting marker is generated entirely inside this manager; callers detect
truncation by looking for the ``[output truncated:`` prefix in the trailing
part of the returned string.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import tempfile
import uuid
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any

from conductor import redaction
from conductor.config.schema import ToolOutputConfig

logger = logging.getLogger(__name__)

# Try to import the MCP SDK
try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.types import TextContent

except ImportError:
    ClientSession = None  # type: ignore[misc, assignment]
    StdioServerParameters = None  # type: ignore[misc, assignment]
    stdio_client = None  # type: ignore[misc, assignment]
    TextContent = None  # type: ignore[misc, assignment]

MCP_SDK_AVAILABLE = ClientSession is not None


_MISSING: Any = object()


def _scrub_log_value(obj: Any) -> Any:
    """Scrub a runtime-derived log or public-error value for the active run."""
    active = redaction.current()
    if active is None:
        return obj
    return active.scrub(obj)


def _mcp_field(model: Any, current_name: str, legacy_name: str) -> Any:
    """Read a model field renamed between MCP 1.x and 2.x."""
    value = getattr(model, current_name, _MISSING)
    if value is _MISSING:
        value = getattr(model, legacy_name)
    return value


# Marker constants. The generic hint is embedded by the manager and replaced
# with the fs hint by Claude's agentic loop when filesystem-like tools are
# available. No placeholder mechanism is used; callers replace the exact
# generic-hint constant string. These names are the public contract shared
# with ClaudeProvider's truncation-marker parser, so they are intentionally
# not underscore-prefixed.
TRUNCATION_MARKER_PREFIX = "[output truncated:"
GENERIC_HINT = "The full output was truncated; refine the tool arguments to return less data."
FS_HINT = (
    "The full output was saved to a file; read it with your filesystem tools if you need more."
)
# Window used by ClaudeProvider._parse_truncation_marker to look at the trailing
# portion of a tool result. It must exceed the maximum realistic marker length
# (PATH_MAX ~4096 chars + fixed marker/hint overhead) so that a valid long
# POSIX spill path is never cut off.
TAIL_WINDOW = 8192


def _contains_symlink(path: Path, stop_at: Path | None = None) -> bool:
    current: Path | None = path
    while current is not None:
        if stop_at is not None and current == stop_at:
            break
        if current.is_symlink():
            return True
        if current == current.parent:
            break
        current = current.parent
    return False


def _sanitize_for_filename(value: str) -> str:
    """Replace characters that are unsafe in filenames with an underscore."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


class MCPManager:
    """Manages MCP server connections and tool execution.

    This class handles the lifecycle of MCP server processes, including:
    - Connecting to servers via stdio transport
    - Collecting available tools from servers
    - Routing tool calls to the appropriate server
    - Cleaning up connections on close
    - Optionally truncating oversized tool results and spilling the full text to disk

    Tool names are prefixed with the server name to avoid collisions:
    `{server_name}__{tool_name}` (e.g., "web-search__search")

    Example:
        >>> manager = MCPManager()
        >>> await manager.connect_server(
        ...     name="web-search",
        ...     command="npx",
        ...     args=["-y", "open-websearch@latest"],
        ...     env={"MODE": "stdio"}
        ... )
        >>> tools = manager.get_all_tools()
        >>> result = await manager.call_tool("web-search__search", {"query": "python"})
        >>> await manager.close()
    """

    def __init__(self, tool_output: ToolOutputConfig | None = None) -> None:
        """Initialize the MCP manager.

        Args:
            tool_output: MCP tool result output-size configuration. When None,
                the default configuration is used.

        Raises:
            ImportError: If MCP SDK is not installed.
        """
        if not MCP_SDK_AVAILABLE:
            raise ImportError("MCP SDK not installed. Install with: uv add 'mcp>=1.0.0'")

        self.sessions: dict[str, Any] = {}
        self.tools: dict[str, list[dict[str, Any]]] = {}  # server -> tools
        self.tool_to_server: dict[str, str] = {}  # prefixed_name -> server
        self._connection_tasks: dict[str, asyncio.Task[None]] = {}
        self._connection_stops: dict[str, asyncio.Event] = {}
        self._tool_output = tool_output or ToolOutputConfig()

    async def connect_server(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        cwd: str | None = None,
        redact_errors: bool = False,
    ) -> list[dict[str, Any]]:
        """Connect to an MCP server and return its tools.

        Spawns the server process using stdio transport, initializes the
        session, and fetches the available tools.

        Args:
            name: Unique name for this server (used as tool prefix).
            command: Command to execute (e.g., "npx", "node").
            args: Command arguments.
            env: Environment variables for the server process.
            timeout: Connection timeout in seconds (not currently used).
            cwd: Working directory for the spawned server process. When None,
                the server inherits the conductor process's current working
                directory (pre-pool legacy behavior).
            redact_errors: When True, a connection failure is logged with
                safe metadata only (server name; no exception text or
                traceback — the exception a stdio failure carries can embed
                server-supplied stderr, which may contain values the caller's
                redaction policy excludes). The raised ``RuntimeError`` still
                chains the original exception for the caller's own diagnostic
                sink. Deterministic ``type: mcp`` steps use this; the default
                preserves the existing provider-facing logging behavior.

        Returns:
            List of tool definitions from this server. Each tool dict contains:
            - name: Prefixed tool name ({server}__{tool})
            - description: Tool description
            - input_schema: JSON schema for tool input
            - server: Server name
            - original_name: Original tool name without prefix

        Raises:
            RuntimeError: If connection fails.
        """
        if not MCP_SDK_AVAILABLE:
            raise RuntimeError("MCP SDK not available")
        assert StdioServerParameters is not None
        assert stdio_client is not None
        assert ClientSession is not None
        server_parameters_type = StdioServerParameters
        open_stdio = stdio_client
        session_type = ClientSession
        if name in self._connection_tasks:
            raise RuntimeError(f"MCP server '{name}' is already connected or connecting")

        logger.info(
            "Connecting to MCP server '%s': %s %s",
            name,
            _scrub_log_value(command),
            _scrub_log_value(args or []),
        )

        # Build server parameters
        server_params = server_parameters_type(
            command=command,
            args=args or [],
            env=env,
            cwd=cwd,
        )

        ready: asyncio.Future[list[dict[str, Any]]] = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def own_connection_lifecycle() -> None:
            try:
                async with AsyncExitStack() as stack:
                    transport = await stack.enter_async_context(open_stdio(server_params))
                    read_stream, write_stream = transport
                    session = await stack.enter_async_context(
                        session_type(read_stream, write_stream)
                    )
                    await session.initialize()

                    response = await session.list_tools()
                    tools: list[dict[str, Any]] = []
                    for tool in response.tools:
                        prefixed_name = f"{name}__{tool.name}"
                        tools.append(
                            {
                                "name": prefixed_name,
                                "description": tool.description or "",
                                # model_dump(by_alias=True) works for Tool, but the
                                # helper keeps both rename sites in one idiom.
                                "input_schema": _mcp_field(tool, "input_schema", "inputSchema"),
                                "server": name,
                                "original_name": tool.name,
                            }
                        )
                        self.tool_to_server[prefixed_name] = name

                    self.sessions[name] = session
                    self.tools[name] = tools
                    ready.set_result(tools)
                    await stop.wait()
            except asyncio.CancelledError:
                if not ready.done():
                    ready.cancel()
                raise
            except Exception as exc:
                if not ready.done():
                    ready.set_exception(exc)
                raise

        task = asyncio.create_task(
            own_connection_lifecycle(),
            name=f"mcp-lifecycle-{name}",
        )
        self._connection_tasks[name] = task
        self._connection_stops[name] = stop

        try:
            tools = await asyncio.shield(ready)
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            task.cancel()
            cleanup = asyncio.gather(task, return_exceptions=True)
            # Await the owner task's teardown under a re-shielding loop: a
            # repeated cancellation of connect_server() lands on the shield
            # instead of the gather, so cleanup still completes and the
            # bookkeeping below always runs.
            results: list[BaseException | None] | None = None
            while results is None:
                try:
                    results = list(await asyncio.shield(cleanup))
                except asyncio.CancelledError:
                    if cleanup.done():
                        results = list(cleanup.result())
            for result in results:
                if isinstance(result, Exception):
                    logger.warning(
                        "Error cancelling MCP connection '%s': %s",
                        name,
                        _scrub_log_value(str(result)),
                    )
            self._connection_tasks.pop(name, None)
            self._connection_stops.pop(name, None)
            self._discard_server_state(name)
            raise
        except Exception as exc:
            await asyncio.gather(task, return_exceptions=True)
            self._connection_tasks.pop(name, None)
            self._connection_stops.pop(name, None)
            self._discard_server_state(name)
            if redact_errors:
                logger.error(
                    "Failed to connect to MCP server '%s' "
                    "(details redacted; see the MCP step diagnostic file)",
                    name,
                )
            else:
                scrubbed_exc = _scrub_log_value(str(exc))
                if redaction.current() is None:
                    logger.error(
                        "Failed to connect to MCP server '%s': %s", name, scrubbed_exc, exc_info=exc
                    )
                else:
                    logger.error("Failed to connect to MCP server '%s': %s", name, scrubbed_exc)
            raise RuntimeError(
                f"Failed to connect to MCP server '{name}': {_scrub_log_value(str(exc))}"
            ) from exc

        logger.info(
            f"Connected to MCP server '{name}' with {len(tools)} tools: "
            f"{[tool['original_name'] for tool in tools]}"
        )
        return tools

    async def call_tool(
        self,
        prefixed_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Call a tool by its prefixed name.

        Routes the tool call to the appropriate MCP server and returns
        the result as a string.

        Args:
            prefixed_name: Full tool name with server prefix (e.g., "web-search__search").
            arguments: Tool input arguments matching the tool's input schema.

        Returns:
            Tool result as a string. Text content is returned directly;
            other content types are stringified.

        Raises:
            ValueError: If the tool is not found.
            RuntimeError: If tool execution fails.
        """
        server_name = self.tool_to_server.get(prefixed_name)
        if not server_name:
            raise ValueError(f"Unknown tool: {prefixed_name}")

        session = self.sessions.get(server_name)
        if not session:
            raise RuntimeError(f"No session for server: {server_name}")

        # Extract original tool name (remove server prefix)
        original_name = prefixed_name.split("__", 1)[1]

        logger.debug(
            "Calling MCP tool '%s' on server '%s' with arguments: %s",
            original_name,
            server_name,
            _scrub_log_value(arguments),
        )

        try:
            result = await session.call_tool(original_name, arguments=arguments)

            # Extract text content from result
            # The result.content is a list of content items
            text_parts: list[str] = []
            for content in result.content:
                if TextContent is not None and isinstance(content, TextContent):
                    text_parts.append(content.text)
                elif hasattr(content, "text"):
                    # Fallback for other text-like content
                    text_parts.append(str(content.text))
                else:
                    # For non-text content, stringify it
                    text_parts.append(str(content))

            response_text = "\n".join(text_parts) if text_parts else ""

            # If no text content, try structured content
            structured = _mcp_field(result, "structured_content", "structuredContent")
            if not response_text and structured:
                response_text = str(structured)

            # Scrub the complete response before tool-result truncation can
            # split a registered value and make exact-match redaction miss it.
            # This copy is logging-only; the returned result stays raw and
            # follows the configured truncation/spill contract below.
            scrubbed_full = _scrub_log_value(response_text)

            try:
                response_text = self._maybe_truncate_response(
                    response_text,
                    server_name=server_name,
                    original_name=original_name,
                )
            except Exception as truncation_err:
                # Truncation is best-effort: a bug here must not fail an
                # otherwise successful tool call or masquerade as a tool
                # error, so it is logged separately and the untruncated
                # result is returned.
                logger.warning(
                    "Failed to apply output truncation for %s; returning untruncated result: %s",
                    prefixed_name,
                    _scrub_log_value(str(truncation_err)),
                )

            # Scrub the COMPLETE response before slicing the preview: a secret
            # starting inside the 200-char window would otherwise leave its
            # leading characters in the log (exact-match redaction cannot see
            # a value the slice truncated). A marker the boundary splits is
            # completed rather than left as a broken token.
            preview = scrubbed_full[:200]
            # Complete a marker the boundary split — but only when a redactor
            # actually replaced values: with no active redactor an ordinary
            # response containing a literal ``***redacted***`` must log
            # byte-identically to the pre-fix preview.
            active = redaction.current()
            if active is not None and active.active:
                marker = redaction.REDACTED_MARKER
                for width in range(len(marker) - 1, 0, -1):
                    if preview.endswith(marker[:width]) and scrubbed_full[
                        len(preview) :
                    ].startswith(marker[width:]):
                        preview = scrubbed_full[: len(preview) + (len(marker) - width)]
                        break
            logger.debug(
                "MCP tool '%s' returned: %s...",
                original_name,
                preview,
            )
            return response_text

        except Exception as e:
            scrubbed_error = _scrub_log_value(str(e))
            logger.error("MCP tool call failed: %s: %s", prefixed_name, scrubbed_error)
            raise RuntimeError(f"MCP tool call failed: {prefixed_name}: {scrubbed_error}") from e

    async def call_tool_structured(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call a tool on a named server and return a JSON-safe structured envelope.

        Unlike :meth:`call_tool`, this keeps the result as structured data
        instead of flattening it to a string, for ``type: mcp`` workflow steps.

        The returned envelope has the shape::

            {"content": [...], "structured": dict | None, "is_error": bool}

        where each entry of ``content`` is a JSON-safe dict produced by
        ``block.model_dump(mode="json")`` (with a ``{"type", "text"}`` fallback
        for blocks that are not pydantic models). ``structured`` is read from
        the ``structuredContent``/``structured_content`` field and is strictly
        ``dict | None`` — any other shape is a malformed MCP response.
        ``is_error`` mirrors the result's error flag.

        The per-result text budget (``runtime.tool_output``) applies to TEXT
        blocks only: when the combined text length exceeds ``max_chars``,
        blocks are walked in order and each keeps ``min(len(text), remaining)``
        characters, where ``remaining`` starts at ``max_chars``. Every
        truncated block gets ``"truncated": true`` and, when spilling is
        enabled, a ``"spill_path"`` written by :meth:`_spill_full_output`
        holding that block's FULL original text. ``structured`` is structural
        data and is NEVER truncated.

        Logging contract: this method emits no log records at all, so argument
        values, result values, and exception text can never leak into logs.

        Args:
            server_name: Name of the connected MCP server.
            tool_name: Tool name as exposed by the server (no server prefix).
            arguments: Tool input arguments matching the tool's input schema.

        Returns:
            The envelope described above.

        Raises:
            ValueError: If the server is unknown.
            RuntimeError: If the server has no live session, the call fails,
                or the response carries malformed structured content.
        """
        if server_name not in self.sessions:
            raise ValueError(f"Unknown server: {server_name}")
        session = self.sessions[server_name]
        if not session:
            raise RuntimeError(f"No session for server: {server_name}")

        try:
            result = await session.call_tool(tool_name, arguments=arguments)
        except Exception as e:
            raise RuntimeError(
                f"MCP tool call failed: {tool_name}: {_scrub_log_value(str(e))}"
            ) from e

        content: list[dict[str, Any]] = []
        for block in result.content or []:
            try:
                dumped = block.model_dump(mode="json")
            except Exception:
                dumped = {"type": getattr(block, "type", "unknown"), "text": str(block)}
            # ``truncated`` / ``spill_path`` are Conductor-local metadata
            # generated by the truncation pass below — a server must not set
            # them. A forged ``spill_path`` would otherwise be forwarded into
            # ``mcp_completed`` as trusted metadata (leaking result data the
            # no-values policy excludes, and breaking the frontend's ``str``
            # type for that field); strip any server-supplied values here so
            # live events and checkpoint/replay reads stay trusted.
            dumped.pop("truncated", None)
            dumped.pop("spill_path", None)
            content.append(dumped)

        structured = _mcp_field(result, "structured_content", "structuredContent")
        if structured is not None and not isinstance(structured, dict):
            raise RuntimeError(
                f"MCP tool '{tool_name}' on server '{server_name}' returned malformed "
                "structured content (expected a dict or null)"
            )

        if self._tool_output.enabled:
            max_chars = self._tool_output.max_chars
            text_total = sum(
                len(block["text"])
                for block in content
                if block.get("type") == "text" and isinstance(block.get("text"), str)
            )
            if text_total > max_chars:
                remaining = max_chars
                for block in content:
                    if block.get("type") != "text" or not isinstance(block.get("text"), str):
                        continue
                    text = block["text"]
                    kept = min(len(text), max(remaining, 0))
                    if kept < len(text):
                        block["text"] = text[:kept]
                        block["truncated"] = True
                        if self._tool_output.spill_to_file:
                            spill_path = self._spill_full_output(
                                full_text=text,
                                server_name=server_name,
                                original_name=tool_name,
                            )
                            if spill_path:
                                block["spill_path"] = spill_path
                    remaining -= kept

        return {
            "content": content,
            "structured": structured,
            "is_error": bool(_mcp_field(result, "is_error", "isError")),
        }

    def _maybe_truncate_response(
        self,
        response_text: str,
        server_name: str,
        original_name: str,
    ) -> str:
        """Cap oversized tool results and optionally spill the full text to disk.

        The marker is generated entirely here. Callers detect truncation by
        looking for ``[output truncated:`` in the trailing part of the returned
        string and may replace the embedded generic hint with a filesystem hint
        when the agent has filesystem-like tools available.

        Args:
            response_text: The full assembled tool result text.
            server_name: Name of the MCP server that handled the tool.
            original_name: Original tool name without the server prefix.

        Returns:
            The (possibly truncated) result string with a plain-text marker at the end.
        """
        if not self._tool_output.enabled:
            return response_text

        max_chars = self._tool_output.max_chars
        if len(response_text) <= max_chars:
            return response_text

        original = len(response_text)
        kept = max_chars
        truncated = response_text[:kept]

        spill_path: str | None = None
        if self._tool_output.spill_to_file:
            spill_path = self._spill_full_output(
                full_text=response_text,
                server_name=server_name,
                original_name=original_name,
            )

        if spill_path:
            marker = (
                f"\n\n[{TRUNCATION_MARKER_PREFIX[1:]} "
                f"{original} chars -> {kept} kept; "
                f"full output saved to: {spill_path}. {GENERIC_HINT}]"
            )
        else:
            marker = (
                f"\n\n[{TRUNCATION_MARKER_PREFIX[1:]} "
                f"{original} chars -> {kept} kept. {GENERIC_HINT}]"
            )

        return f"{truncated}{marker}"

    def _spill_full_output(
        self,
        full_text: str,
        server_name: str,
        original_name: str,
    ) -> str | None:
        """Write the full tool result to a process-private temporary file.

        Files are created with mode 0o600 on POSIX (Windows does not honour
        permission bits — see the note in the body) and may contain raw tool
        output (possibly including secrets). The caller is responsible for
        lifecycle.

        This method is best-effort and must never raise. Any failure (invalid
        path, symlink, permission, I/O, encoding) is logged as a warning and
        returns None so the caller can fall back to a marker without a path.

        Args:
            full_text: The full tool result text to persist.
            server_name: Name of the MCP server that produced the result.
            original_name: Original tool name without the server prefix.

        Returns:
            The absolute path of the spill file, or None if writing failed.
        """
        try:
            spill_dir_str = self._tool_output.spill_dir
            if spill_dir_str:
                # Symlink policy for an explicit spill dir: a symlink is only
                # allowed when it resolves to a location inside the system temp
                # root. That keeps platform layout working (on macOS /tmp and
                # /var/tmp are themselves symlinks into /private/...) while a
                # symlink pointing elsewhere is rejected, so a world-writable
                # sticky directory cannot redirect spilled output into an
                # attacker-chosen location outside the temp root.
                spill_dir = Path(spill_dir_str).resolve()
                temp_root = Path(tempfile.gettempdir()).resolve()
                if not spill_dir.is_relative_to(temp_root) and _contains_symlink(
                    Path(spill_dir_str)
                ):
                    logger.warning(
                        "Spill dir %s contains a symlink; refusing to write tool output spill.",
                        _scrub_log_value(str(spill_dir)),
                    )
                    return None
            else:
                temp_parent = Path(tempfile.gettempdir()).resolve()
                spill_dir = temp_parent / "conductor" / "tool-output"
                if _contains_symlink(spill_dir, stop_at=temp_parent):
                    logger.warning(
                        "Default spill dir %s contains a symlink; "
                        "refusing to write tool output spill.",
                        _scrub_log_value(str(spill_dir)),
                    )
                    return None

            spill_dir = spill_dir.resolve()
            spill_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Harden a pre-existing leaf directory before writing potentially
            # sensitive tool output into it. On POSIX this chmod is the only
            # protection ever applied to a directory the caller (not
            # Conductor) may have already created, since spill_dir can be a
            # user-configured path outside the system temp root.
            #
            # On Windows, POSIX permission bits are not implemented: stat()
            # reports 0o777 for a directory (0o555 when the read-only
            # attribute is set), so the low bits are always non-zero and this
            # branch always fires. The subsequent os.chmod() only maps the
            # owner-write bit onto FILE_ATTRIBUTE_READONLY, which Windows
            # ignores as an access control for directories, so it succeeds
            # without restricting access — the "harden or refuse" invariant
            # below is POSIX-only. The default spill dir inherits the
            # per-user temp directory's ACL; a configured spill_dir gets only
            # whatever ACL that path carries, and Conductor cannot tighten it
            # on Windows.
            #
            # This is accepted deliberately rather than guarded with a
            # sys.platform check: guarding it would also skip
            # test_manager_truncation.py::test_spill_dir_chmod_failure_is_rejected
            # on Windows, giving up real coverage of the refusal path below
            # (issue #425). The failure path still refuses the write; a
            # chmod failure on Windows is a genuine anomaly, not expected
            # noise.
            current_mode = stat.S_IMODE(spill_dir.stat().st_mode)
            if current_mode & 0o077:
                try:
                    os.chmod(spill_dir, 0o700)
                except OSError as chmod_err:
                    logger.warning(
                        "Spill dir %s has permissions %04o and chmod failed: %s; "
                        "refusing to write tool output spill.",
                        _scrub_log_value(str(spill_dir)),
                        current_mode,
                        _scrub_log_value(str(chmod_err)),
                    )
                    return None
            safe_server = _sanitize_for_filename(server_name)
            safe_tool = _sanitize_for_filename(original_name)
            unique = uuid.uuid4().hex[:8]
            filename = f"mcp-{safe_server}-{safe_tool}-{unique}.txt"
            path = spill_dir / filename

            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                f = os.fdopen(fd, "w", encoding="utf-8")
            except OSError:
                os.close(fd)
                with suppress(OSError):
                    os.remove(path)
                raise
            try:
                with f:
                    f.write(full_text)
            except Exception:
                # Any write failure (including UnicodeEncodeError) leaves a partial
                # file; clean it up and degrade gracefully.
                with suppress(OSError):
                    os.remove(path)
                logger.warning(
                    "Failed to write full MCP tool output spill to disk; continuing without it.",
                )
                return None
            return str(path)
        except (OSError, ValueError) as e:
            logger.warning(
                "Failed to spill full MCP tool output to disk: %s",
                _scrub_log_value(str(e)),
            )
            return None

    def get_all_tools(self) -> list[dict[str, Any]]:
        """Get all tools from all connected servers.

        Returns:
            List of all tool definitions across all servers.
        """
        all_tools: list[dict[str, Any]] = []
        for tools in self.tools.values():
            all_tools.extend(tools)
        return all_tools

    def get_server_tools(self, server_name: str) -> list[dict[str, Any]]:
        """Get tools from a specific server.

        Args:
            server_name: Name of the server.

        Returns:
            List of tool definitions from the specified server,
            or empty list if server not found.
        """
        return self.tools.get(server_name, [])

    def has_servers(self) -> bool:
        """Check if any servers are connected.

        Returns:
            True if at least one server is connected.
        """
        return len(self.sessions) > 0

    def _discard_server_state(self, server_name: str) -> None:
        self.sessions.pop(server_name, None)
        self.tools.pop(server_name, None)
        stale_tools = [
            tool_name
            for tool_name, mapped_server in self.tool_to_server.items()
            if mapped_server == server_name
        ]
        for tool_name in stale_tools:
            self.tool_to_server.pop(tool_name, None)

    async def close(self) -> None:
        """Close all server connections and clean up resources.

        This method should be called when the manager is no longer needed.
        It properly closes all stdio connections and cleans up internal state.
        Cancellation is deliberately absorbed until owner-task teardown
        completes, so task-affine MCP contexts are never orphaned.
        """
        if not self._connection_tasks:
            return

        logger.debug(f"Closing {len(self._connection_tasks)} MCP server connection(s)")

        for stop in self._connection_stops.values():
            stop.set()

        cleanup = asyncio.gather(*self._connection_tasks.values(), return_exceptions=True)
        # Await teardown under a re-shielding loop: a repeated cancellation of
        # close() lands on the shield instead of the gather, so cleanup still
        # completes before bookkeeping is cleared.
        results: list[BaseException | None] | None = None
        while results is None:
            try:
                results = list(await asyncio.shield(cleanup))
            except asyncio.CancelledError:
                if cleanup.done():
                    results = list(cleanup.result())

        self.sessions.clear()
        self.tools.clear()
        self.tool_to_server.clear()
        self._connection_tasks.clear()
        self._connection_stops.clear()
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Error closing MCP connections: %s", _scrub_log_value(str(result)))

        logger.debug("MCP manager closed")
