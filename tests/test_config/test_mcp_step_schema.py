"""Tests for ``type: mcp`` step schema validation.

Tests cover:
- Valid mcp step definitions (minimal + full)
- Required server/tool validation (variant-owned custom messages kept)
- The full forbidden-field matrix (every LLM and sibling-step field), now
  surfaced as Pydantic's standard ``extra_forbidden`` error under the
  concrete step-model architecture (issue #517)
- Literal-only server/tool (Jinja templates rejected at load time)
- timeout acceptance (unlike wait/set steps)
- server/tool/arguments rejection on all other step types

Field matrix under test (requirement: every field foreign to ``type: mcp``
must be rejected with ``extra_forbidden``; MCPStepDef itself only declares
name, description, input, routes, output, timeout, server, tool, arguments):
- ALLOWED: name, description, input, output, routes, timeout, server, tool,
  arguments
- FORBIDDEN (extra_forbidden): prompt, system_prompt, provider, model,
  tools, reasoning, context_tier, skills, plugins, validator, dialog,
  sandbox, session_key, max_agent_iterations, max_session_seconds,
  output_mode, retry, timeout_seconds, command, args, env, working_dir,
  settings_dir, options, workflow, input_mapping, max_depth, value, values,
  output_type, stdin, duration, reason, status, output_template
- CUSTOM MESSAGES (variant-owned invariants): missing/empty server/tool
  ("mcp agents require ..."), Jinja in server/tool ("never rendered")
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from conductor.config.schema import (
    AgentDef,
    GateOption,
    MCPStepDef,
    OutputField,
    RouteDef,
    ScriptStepDef,
    WaitStepDef,
    WorkflowConfig,
)


def _mcp_step(**overrides: Any) -> MCPStepDef:
    """Build a valid minimal mcp step, applying overrides."""
    kwargs: dict[str, Any] = {"name": "lookup", "server": "docs", "tool": "search"}
    kwargs.update(overrides)
    return MCPStepDef(**kwargs)


def _mcp_forbidden(field_name: str, value: Any) -> None:
    """Assert a foreign field is rejected on an mcp step with extra_forbidden."""
    with pytest.raises(ValidationError) as exc_info:
        MCPStepDef.model_validate({"name": "x", "server": "s", "tool": "t", field_name: value})
    assert any(
        e["loc"] == (field_name,) and e["type"] == "extra_forbidden"
        for e in exc_info.value.errors()
    )


class TestMcpStepDefValid:
    """Tests for valid mcp step construction."""

    def test_valid_minimal_mcp_step(self) -> None:
        """Requirement: a minimal type: mcp step needs only server and tool."""
        agent = _mcp_step()
        assert agent.type == "mcp"
        assert agent.server == "docs"
        assert agent.tool == "search"
        assert agent.arguments is None
        assert agent.timeout is None

    def test_valid_mcp_step_with_all_allowed_fields(self) -> None:
        """Requirement: output, routes, input, timeout, description, arguments are allowed."""
        agent = _mcp_step(
            description="Look up docs",
            arguments={"query": "{{ workflow.input.q }}"},
            input=["prep.output"],
            output={"hits": OutputField(type="number")},
            routes=[RouteDef(to="$end")],
            timeout=30,
        )
        assert agent.arguments == {"query": "{{ workflow.input.q }}"}
        assert agent.timeout == 30
        assert "hits" in (agent.output or {})

    def test_mcp_step_timeout_accepted(self) -> None:
        """Requirement: timeout is allowed on mcp steps (unlike wait/set which forbid it)."""
        agent = _mcp_step(timeout=30)
        assert agent.timeout == 30

    def test_mcp_step_output_accepted(self) -> None:
        """Requirement: mcp steps may declare an output schema like script steps."""
        agent = _mcp_step(output={"result": OutputField(type="string")})
        assert agent.output is not None

    def test_mcp_arguments_allow_jinja_templates(self) -> None:
        """Requirement: arguments ARE rendered recursively, so Jinja is allowed there."""
        agent = _mcp_step(arguments={"q": "{{ searcher.output.query }}", "n": 5})
        assert agent.arguments == {"q": "{{ searcher.output.query }}", "n": 5}


class TestMcpStepDefRequiredFields:
    """Tests for required server/tool fields (variant-owned custom messages)."""

    def test_mcp_without_server_raises(self) -> None:
        """Requirement: mcp steps require 'server'."""
        with pytest.raises(ValidationError, match="mcp agents require 'server'"):
            MCPStepDef(name="bad", tool="search")

    def test_mcp_with_empty_server_raises(self) -> None:
        """Requirement: an empty server string is rejected as missing."""
        with pytest.raises(ValidationError, match="mcp agents require 'server'"):
            MCPStepDef(name="bad", server="", tool="search")

    def test_mcp_without_tool_raises(self) -> None:
        """Requirement: mcp steps require 'tool'."""
        with pytest.raises(ValidationError, match="mcp agents require 'tool'"):
            MCPStepDef(name="bad", server="docs")

    def test_mcp_with_empty_tool_raises(self) -> None:
        """Requirement: an empty tool string is rejected as missing."""
        with pytest.raises(ValidationError, match="mcp agents require 'tool'"):
            MCPStepDef(name="bad", server="docs", tool="")


# Requirement: each LLM-only or sibling-step field must be rejected on mcp
# steps with Pydantic's standard extra_forbidden error (no custom messages).
# field name -> kwarg value
_FORBIDDEN_FIELDS: list[tuple[str, Any]] = [
    # LLM fields
    ("prompt", "do something"),
    ("system_prompt", "You are..."),
    ("provider", "copilot"),
    ("model", "gpt-4"),
    ("tools", ["web_search"]),
    ("reasoning", {"effort": "high"}),
    ("context_tier", "long_context"),
    ("skills", ["conductor"]),
    ("plugins", ["prs"]),
    ("validator", {"criteria": "must be good"}),
    ("dialog", {"trigger_prompt": "pause if unsure"}),
    ("sandbox", {"identifier_scope": "item"}),
    ("session_key", "my-key"),
    ("max_agent_iterations", 5),
    ("max_session_seconds", 60.0),
    ("output_mode", "raw"),
    ("retry", {"max_attempts": 2}),
    ("timeout_seconds", 30.0),  # mcp uses 'timeout', not the LLM 'timeout_seconds'
    # Sibling-step fields
    ("command", "echo"),
    ("args", ["a"]),
    ("env", {"A": "b"}),
    ("working_dir", "/tmp"),
    ("settings_dir", "/tmp"),
    ("options", [GateOption(label="OK", value="ok", route="$end")]),
    ("workflow", "sub.yaml"),
    ("input_mapping", {"a": "{{ b }}"}),
    ("max_depth", 2),
    ("value", "{{ 1 }}"),
    ("values", {"a": "{{ 1 }}"}),
    ("output_type", "auto"),
]


class TestMcpStepDefForbiddenFields:
    """Parameterized matrix: every foreign field is rejected as extra_forbidden."""

    @pytest.mark.parametrize(("field_name", "value"), _FORBIDDEN_FIELDS)
    def test_mcp_forbidden_field_raises(self, field_name: str, value: Any) -> None:
        """Requirement: mcp steps cannot set LLM-only or sibling-step fields."""
        _mcp_forbidden(field_name, value)

    def test_mcp_forbidden_field_surfaces_through_workflow_config(self) -> None:
        """Requirement: a foreign field on an mcp step is rejected inside a workflow's
        agents list too, with the discriminated-union loc ending at the field."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowConfig.model_validate(
                {
                    "workflow": {"name": "wf", "entry_point": "lookup"},
                    "agents": [
                        {
                            "name": "lookup",
                            "type": "mcp",
                            "server": "docs",
                            "tool": "search",
                            "prompt": "do something",
                        }
                    ],
                }
            )
        assert any(
            e["loc"][-1] == "prompt" and e["type"] == "extra_forbidden"
            for e in exc_info.value.errors()
        )

    def test_mcp_with_stdin_raises(self) -> None:
        """Requirement: stdin belongs to script steps only; it is extra_forbidden on mcp."""
        _mcp_forbidden("stdin", "payload")

    def test_mcp_with_duration_raises(self) -> None:
        """Requirement: duration belongs to wait steps only; it is extra_forbidden on mcp."""
        _mcp_forbidden("duration", 5)

    def test_mcp_with_reason_raises(self) -> None:
        """Requirement: reason belongs to wait/terminate steps only; extra_forbidden on mcp."""
        _mcp_forbidden("reason", "because")

    def test_mcp_with_status_raises(self) -> None:
        """Requirement: status belongs to terminate steps only; it is extra_forbidden on mcp."""
        _mcp_forbidden("status", "success")

    def test_mcp_with_output_template_raises(self) -> None:
        """Requirement: output_template belongs to terminate steps only; extra_forbidden on mcp."""
        _mcp_forbidden("output_template", {"a": "b"})


class TestMcpFieldsLiteralOnly:
    """Tests for the literal-only server/tool contract (variant-owned custom messages)."""

    @pytest.mark.parametrize(
        "template", ["{{ workflow.input.server }}", "{% if x %}docs{% endif %}"]
    )
    def test_jinja_in_server_rejected(self, template: str) -> None:
        """Requirement: server is never rendered — Jinja templates are rejected at load time."""
        with pytest.raises(ValidationError, match="never rendered"):
            MCPStepDef(name="bad", server=template, tool="search")

    @pytest.mark.parametrize(
        "template", ["{{ workflow.input.tool }}", "{% if x %}search{% endif %}"]
    )
    def test_jinja_in_tool_rejected(self, template: str) -> None:
        """Requirement: tool is never rendered — Jinja templates are rejected at load time."""
        with pytest.raises(ValidationError, match="never rendered"):
            MCPStepDef(name="bad", server="docs", tool=template)


class TestMcpFieldsForbiddenOnOtherTypes:
    """server/tool/arguments are exclusive to type: mcp — extra_forbidden elsewhere."""

    @pytest.mark.parametrize("field_name", ["server", "tool", "arguments"])
    def test_mcp_fields_rejected_on_script(self, field_name: str) -> None:
        """Requirement: server/tool/arguments on a script step raise extra_forbidden."""
        value: Any = {"server": "docs", "tool": "search"}.get(field_name, {"q": "x"})
        with pytest.raises(ValidationError) as exc_info:
            ScriptStepDef.model_validate({"name": "bad", "command": "echo", field_name: value})
        assert any(
            e["loc"] == (field_name,) and e["type"] == "extra_forbidden"
            for e in exc_info.value.errors()
        )

    @pytest.mark.parametrize("field_name", ["server", "tool", "arguments"])
    def test_mcp_fields_rejected_on_regular_agent(self, field_name: str) -> None:
        """Requirement: server/tool/arguments on an LLM agent raise extra_forbidden."""
        value: Any = {"server": "docs", "tool": "search"}.get(field_name, {"q": "x"})
        with pytest.raises(ValidationError) as exc_info:
            AgentDef.model_validate({"name": "bad", "prompt": "hello", field_name: value})
        assert any(
            e["loc"] == (field_name,) and e["type"] == "extra_forbidden"
            for e in exc_info.value.errors()
        )

    def test_mcp_fields_rejected_on_wait(self) -> None:
        """Requirement: server on a wait step is extra_forbidden."""
        with pytest.raises(ValidationError) as exc_info:
            WaitStepDef.model_validate({"name": "bad", "duration": 5, "server": "docs"})
        assert any(
            e["loc"] == ("server",) and e["type"] == "extra_forbidden"
            for e in exc_info.value.errors()
        )
