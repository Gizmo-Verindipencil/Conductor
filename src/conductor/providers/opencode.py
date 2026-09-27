"""OpenCode ACP provider for Conductor.

This provider drives `opencode acp` (the Agent Client Protocol server shipped
with OpenCode) as an *agent runtime* — the same role Claude Code / GitHub
Copilot CLI play in Conductor. It is NOT a "model backend": OpenCode owns the
agentic loop, the file-editing tools, and the model connection (OpenRouter,
OrcaRouter, local, ...). Conductor only orchestrates: it spawns the ACP
subprocess, performs the JSON-RPC handshake, opens a session rooted at the
agent's working directory, streams prompts in, and normalizes the streamed
`session/update` events into Conductor's event / AgentOutput contract.

Why ACP rather than `opencode run`?
- `run` is one-shot and owns the TTY; ACP exposes a long-lived session we can
  prompt repeatedly, stream events from, and (with a forked session) resume —
  the shape Conductor's orchestration loop expects.
- ACP is a stable stdio JSON-RPC surface the OpenCode project commits to, so
  this provider tracks the upstream protocol rather than scraping CLI flags.

Protocol notes observed against the installed opencode binary:
- `initialize` wants `protocolVersion: 1` (int, not the ACP draft string).
- `session/new` requires `cwd` (string) and `mcpServers` (array) — both are
  mandatory, not optional.
- `session/prompt` takes `prompt` as an array of parts
  (`[{"type": "text", "text": "..."}]`), never a bare string.
- Streamed `session/update` carries `agent_message_chunk` (text deltas),
  `tool_call` / `tool_call_update` (tool activity), and `usage_update`
  (token accounting). The terminal `session/prompt` *result* carries
  `stopReason` and `usage`.

This is an experimental provider; see capabilities.py for the declared
contract and AGENTS.md "Experimental Providers" for permitted carve-outs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from typing import TYPE_CHECKING, Any

from conductor.exceptions import ProviderError
from conductor.providers.base import AgentOutput, AgentProvider
from conductor.providers.capabilities import ProviderCapabilities

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef
    from conductor.providers.base import EventCallback

logger = logging.getLogger(__name__)

_DEFAULT_PROTOCOL_VERSION = 1
_ACP_METHODS = (
    "initialize",
    "session/new",
    "session/prompt",
    "session/fork",
    "session/delete",
)


class _AcpChannel:
    """Minimal JSON-RPC-over-stdio client for the OpenCode ACP server.

    One channel == one spawned `opencode acp` subprocess. The handshake
    (initialize + notifications/initialized) is performed lazily on first use.
    Request ids are monotonic ints; notifications carry no id.
    """

    def __init__(self, binary: str, cwd: str, env: dict[str, str], timeout: float) -> None:
        self._binary = binary
        self._cwd = cwd
        self._env = env
        self._timeout = timeout
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._handshake_done = False
        self._reader_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def is_alive(self) -> bool:
        """True while the underlying opencode subprocess is running."""
        return self._proc is not None and self._proc.poll() is None

    async def _ensure_started(self) -> None:
        if self._proc is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._proc = await asyncio.to_thread(
            subprocess.Popen,
            [self._binary, "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
            text=True,
            bufsize=1,
        )
        self._reader_task = asyncio.ensure_future(
            asyncio.to_thread(self._read_loop_sync)
        )

    def _read_loop_sync(self) -> None:
        """Blocking stdout reader run in a worker thread (to_thread).

        Must not touch the event loop or any coroutine primitives — it only
        pushes parsed messages onto ``_queue`` (a thread-safe asyncio.Queue)
        and resolves futures via ``call_soon_threadsafe``.
        """
        assert self._proc is not None and self._proc.stdout is not None
        loop = self._loop
        if loop is None:  # Defensive: should be set by _ensure_started.
            loop = asyncio.get_event_loop()
        reader = self._proc.stdout
        try:
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("opencode acp: non-JSON stdout: %r", line[:200])
                    continue
                rid = msg.get("id")
                if rid is not None and rid in self._pending:
                    fut = self._pending.pop(rid)
                    if not fut.done():
                        if "error" in msg:
                            loop.call_soon_threadsafe(
                                fut.set_exception,
                                ProviderError(
                                    f"opencode acp error: {msg['error']}",
                                    suggestion="Check `opencode acp` output and the model/auth config.",
                                ),
                            )
                        else:
                            loop.call_soon_threadsafe(fut.set_result, msg)
                else:
                    loop.call_soon_threadsafe(self._queue.put_nowait, msg)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("opencode acp reader loop died: %s", exc)
        finally:
            # Process EOF reached (opencode exited) without resolving pending
            # requests. Fail those fast instead of blocking until _timeout, so a
            # dead opencode subprocess surfaces as a clear ProviderError rather
            # than a silent 30-minute hang.
            for rid, fut in list(self._pending.items()):
                self._pending.pop(rid, None)
                if not fut.done():
                    loop.call_soon_threadsafe(
                        fut.set_exception,
                        ProviderError(
                            "opencode acp process exited before responding",
                            suggestion="Check that `opencode acp` stays alive for the whole session and that the model backend is reachable.",
                        ),
                    )
            # Unblock any drain_loop waiting on the queue.
            loop.call_soon_threadsafe(self._queue.put_nowait, None)

    async def _handshake(self) -> None:
        if self._handshake_done:
            return
        result = await self._request(
            "initialize",
            {
                "protocolVersion": _DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "conductor-opencode-provider", "version": "0.1.0"},
            },
        )
        if "result" not in result:
            raise ProviderError(
                "opencode acp initialize handshake failed",
                suggestion="Verify `opencode acp` starts and speaks ACP on stdin/stdout.",
            )
        # opencode acp does not require (and rejects) notifications/initialized;
        # mark the handshake complete and proceed to session/new.
        self._handshake_done = True

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_started()
        if method not in ("initialize",):
            await self._handshake()
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params,
        }
        await self._write(payload)
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            raise ProviderError(
                f"opencode acp request {method!r} timed out after {self._timeout}s",
                suggestion="Increase runtime.timeout / max_session_seconds, or check the model backend.",
            ) from exc

    async def _write(self, obj: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        line = json.dumps(obj) + "\n"
        await asyncio.to_thread(self._proc.stdin.write, line)
        await asyncio.to_thread(self._proc.stdin.flush)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a request, performing the handshake automatically."""
        return await self._request(method, params)

    async def drain_notifications(self) -> list[dict[str, Any]]:
        """Collect queued unsolicited messages (session/update, etc.)."""
        items: list[dict[str, Any]] = []
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not None:  # None is the EOF sentinel, not a real message.
                items.append(item)
        return items

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._proc is not None:
            try:
                if self._proc.stdin is not None:
                    await asyncio.to_thread(self._proc.stdin.close)
            except Exception:
                pass
            try:
                self._proc.terminate()
                await asyncio.to_thread(self._proc.wait, timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None


class OpenCodeProvider(AgentProvider):
    """Agent-runtime provider that drives `opencode acp`.

    OpenCode owns file editing and the model connection; Conductor orchestrates
    the multi-agent workflow around it. See module docstring for protocol
    details and rationale.
    """

    CAPABILITIES = ProviderCapabilities(
        tier="experimental",
        mcp_tools=True,
        workflow_tools_passthrough=True,
        streaming_events=True,
        agent_reasoning_events=False,
        reasoning_effort=("low", "medium", "high", "xhigh", "max"),
        structured_output="prompt_injection",
        interrupt=True,
        max_session_seconds=True,
        checkpoint_resume=False,
        usage_tracking=True,
        concurrent_safe=True,
        skills=False,
        plugins=False,
    )

    def __init__(
        self,
        *,
        provider_settings: Any = None,
        binary: str | None = None,
        cwd: str | None = None,
        default_model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_agent_iterations: int | None = None,
        max_session_seconds: float | None = None,
        timeout: float | None = None,
        mcp_servers: dict[str, Any] | None = None,
        api_key: str | None = None,
        tool_output: str | None = None,
    ) -> None:
        del provider_settings  # OpenCode authenticates via its own config/env.
        del api_key  # OpenCode reads OPENROUTER_API_KEY etc. from the environment.
        resolved = binary or os.environ.get("OPENCODE_BIN") or shutil.which("opencode")
        if not resolved:
            raise ProviderError(
                "opencode binary not found on PATH",
                suggestion="Install OpenCode (https://opencode.ai) or set OPENCODE_BIN.",
            )
        self._binary = resolved
        self._cwd = cwd or os.getcwd()
        self._default_model = default_model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._max_agent_iterations = max_agent_iterations
        self._max_session_seconds = max_session_seconds or 1800.0
        self._timeout = timeout or self._max_session_seconds
        self._mcp_servers = mcp_servers or {}
        self._tool_output = tool_output
        self._channel: _AcpChannel | None = None
        self._session_id: str | None = None

    @property
    def supports_native_skills(self) -> bool:
        return False

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # OpenCode reads its own auth (OPENROUTER_API_KEY etc.) from the
        # environment; we intentionally inherit so the user's login applies.
        if self._default_model:
            # Best-effort default; the agent can override via opencode config.
            env.setdefault("OPENCODE_MODEL", self._default_model)
        return env

    async def _get_channel(self) -> _AcpChannel:
        if self._channel is None:
            self._channel = _AcpChannel(
                binary=self._binary,
                cwd=self._cwd,
                env=self._build_env(),
                timeout=self._timeout,
            )
            await self._channel._ensure_started()
            await self._channel._handshake()
        return self._channel

    async def validate_connection(self) -> bool:
        """Verify `opencode acp` can start and handshake."""
        try:
            channel = await self._get_channel()
            # Probe a session creation to confirm the runtime is usable.
            # (opencode acp has no session/delete; sessions close with the process.)
            result = await channel.request(
                "session/new",
                {"cwd": self._cwd, "mcpServers": []},
            )
            sid = result.get("result", {}).get("sessionId")
            if sid:
                # Intentionally do NOT call session/delete (unsupported);
                # leave the probe session to be reclaimed on process exit.
                self._session_id = None
            return sid is not None
        except Exception as exc:  # pragma: no cover - diagnostic path
            logger.warning("opencode validate_connection failed: %s", exc)
            return False

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        *,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
        skill_directories: list[str] | None = None,
        custom_agents: list[dict[str, Any]] | None = None,
        extra_mcp_servers: dict[str, Any] | None = None,
        continuation_state: object | None = None,
    ) -> AgentOutput:
        channel = await self._get_channel()
        await self._handshake_and_session(channel, extra_mcp_servers)

        # Stream the prompt. Use the agent's working_dir as session cwd when
        # resolved; OpenCode edits files relative to the session root.
        prompt_parts = [{"type": "text", "text": rendered_prompt}]
        try:
            result = await self._prompt_with_streaming(
                channel,
                prompt_parts,
                event_callback,
                interrupt_signal,
            )
        except ProviderError:
            await self._cleanup_session(channel)
            raise

        usage = result.get("usage", {}) or {}
        content_text = result.get("text", "")
        stop_reason = result.get("stopReason", "end_turn")

        # Surface a `summary` field so YAML `output.summary` constraints pass
        # when the workflow asks for one. Use the leading non-empty lines of the
        # agent's response; OpenCode does not emit a structured summary itself.
        summary_lines = [ln.strip() for ln in content_text.splitlines() if ln.strip()]
        summary = " ".join(summary_lines[:3]) if summary_lines else content_text

        # Surface a `verdict` field when a reviewer-style agent ends its
        # response with "VERDICT: <value>". OpenCode does not emit structured
        # output, so parse the trailing VERDICT line. Everything else is `notes`.
        verdict = None
        notes = content_text
        for ln in reversed(content_text.splitlines()):
            s = ln.strip()
            if s.upper().startswith("VERDICT:"):
                verdict = s.split(":", 1)[1].strip().lower()
                notes = content_text.rsplit(s, 1)[0].strip()
                break

        # Build the full set of fields OpenCode-derived agents can surface, then
        # restrict to ONLY the fields declared in this agent's `output` schema.
        # Conductor warns on every undeclared content key, so returning a fixed
        # superset (summary/plan/verdict/notes) for every agent triggers
        # "undeclared fields" noise on agents that declare a subset. When the
        # agent declares no output schema, keep the full set for compatibility.
        all_fields: dict[str, Any] = {
            "summary": summary,
            "plan": content_text,
            "verdict": verdict,
            "notes": notes,
        }
        declared = set((agent.output or {}).keys())
        if declared:
            content = {k: v for k, v in all_fields.items() if k in declared}
        else:
            content = all_fields

        return AgentOutput(
            content=content,
            raw_response=result,
            tokens_used=(usage.get("totalTokens") if "totalTokens" in usage else None),
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
            model=self._default_model,
            continuation_state=self._session_id,
        )

    async def _handshake_and_session(
        self, channel: _AcpChannel, mcp_servers: dict[str, Any] | None
    ) -> None:
        if self._session_id is not None:
            return
        server_list = []
        if mcp_servers:
            # Conductor's mcp_servers map → ACP mcpServers list shape.
            for name, cfg in mcp_servers.items():
                server_list.append({"name": name, **cfg})
        new_result = await channel.request(
            "session/new",
            {"cwd": self._cwd, "mcpServers": server_list},
        )
        sid = new_result.get("result", {}).get("sessionId")
        if not sid:
            raise ProviderError(
                "opencode acp session/new returned no sessionId",
                suggestion="Check `opencode acp` startup and model auth.",
            )
        self._session_id = sid

    async def _prompt_with_streaming(
        self,
        channel: _AcpChannel,
        prompt_parts: list[dict[str, Any]],
        event_callback: EventCallback | None,
        interrupt_signal: asyncio.Event | None,
    ) -> dict[str, Any]:
        """Send session/prompt and collect streamed updates until the result."""
        # The result future resolves when session/prompt returns.
        result_fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        # Monkeypatch the channel's pending handling: session/prompt's result
        # is solicited but arrives via the normal request channel, so we send
        # it through request() and separately drain session/update notifications.
        prompt_task = asyncio.ensure_future(
            channel.request(
                "session/prompt",
                {"sessionId": self._session_id, "prompt": prompt_parts},
            )
        )

        # Buffer for streamed text and tool activity.
        text_parts: list[str] = []
        usage_acc: dict[str, Any] = {}

        async def drain_loop() -> None:
            try:
                while not prompt_task.done():
                    msg = await self._next_notification(channel, timeout=1.0)
                    if msg is None:
                        # None means either a 1s queue timeout (keep draining) or
                        # the EOF sentinel that _read_loop_sync pushes when the
                        # opencode process exits. If the process is dead, stop
                        # draining — prompt_task will fail on its own _timeout
                        # (or earlier, via the pending-future error path).
                        if not channel.is_alive():
                            break
                        if interrupt_signal is not None and interrupt_signal.is_set():
                            # Best-effort interrupt: ask opencode to stop.
                            await channel.request(
                                "session/interrupt",
                                {"sessionId": self._session_id},
                            )
                        continue
                    self._dispatch_update(msg, event_callback, text_parts, usage_acc)
            except asyncio.CancelledError:
                return

        drain = asyncio.ensure_future(drain_loop())
        try:
            result = await prompt_task
        finally:
            drain.cancel()
            try:
                await drain
            except asyncio.CancelledError:
                pass

        usage_acc.update(result.get("result", {}).get("usage", {}) or {})
        return {
            "text": "".join(text_parts),
            "usage": usage_acc,
            "stopReason": result.get("result", {}).get("stopReason", "end_turn"),
        }

    async def _next_notification(
        self, channel: _AcpChannel, timeout: float
    ) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(channel._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def _dispatch_update(
        self,
        msg: dict[str, Any],
        event_callback: EventCallback | None,
        text_parts: list[str],
        usage_acc: dict[str, Any],
    ) -> None:
        if msg.get("method") != "session/update":
            return
        params = msg.get("params", {})
        update = params.get("update", {})
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            chunk = update.get("content", {}).get("text", "")
            if chunk:
                text_parts.append(chunk)
                if event_callback is not None:
                    event_callback("agent_message", {"text": chunk})
        elif kind == "tool_call" or kind == "tool_call_update":
            if event_callback is not None:
                event_callback("agent_tool_call", update)
        elif kind == "usage_update":
            used = update.get("used")
            if used is not None:
                usage_acc["totalTokens"] = used
            if "inputTokens" in update:
                usage_acc["inputTokens"] = update["inputTokens"]
            if "outputTokens" in update:
                usage_acc["outputTokens"] = update["outputTokens"]
            if event_callback is not None:
                event_callback("usage_update", update)

    async def _cleanup_session(self, channel: _AcpChannel) -> None:
        # opencode acp has no session/delete; sessions are reclaimed when the
        # underlying process exits. We simply drop our reference.
        self._session_id = None

    async def close(self) -> None:
        if self._channel is not None:
            try:
                await self._cleanup_session(self._channel)
            except Exception:
                pass
            await self._channel.close()
            self._channel = None
