"""Tests for ``type: wait`` schema validation.

Covers:
- Valid wait step definitions (literal and templated durations).
- Required ``duration`` field.
- Fields owned by other variants rejected on wait (extra_forbidden).
- Duration bounds (> 0 and <= 24h).
- Boolean duration rejection (pre-coercion).
- Reject wait inside parallel groups and as for-each inline agents.
- Reject ``duration`` and ``reason`` on non-wait variants.
- working_dir allowed on LLM agents and script steps, rejected elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError as PydanticValidationError

from conductor.config.schema import (
    AgentDef,
    ForEachDef,
    GateOption,
    HumanGateStepDef,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    ScriptStepDef,
    SetStepDef,
    StepDef,
    TerminateStepDef,
    WaitStepDef,
    WorkflowConfig,
    WorkflowDef,
    WorkflowStepDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


def _assert_extra_forbidden(
    exc_info: pytest.ExceptionInfo[PydanticValidationError], field: str
) -> None:
    """Assert a variant-owned-by-sibling field failed with extra_forbidden on that field."""
    assert any(
        e["loc"] == (field,) and e["type"] == "extra_forbidden" for e in exc_info.value.errors()
    )


def _make_workflow(
    *agents: StepDef,
    parallel: list[ParallelGroup] | None = None,
    for_each: list[ForEachDef] | None = None,
) -> WorkflowConfig:
    """Build a minimal WorkflowConfig for validator tests."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="wait-test",
            description="test",
            version="1.0.0",
            entry_point=agents[0].name,
            runtime=RuntimeConfig(provider="copilot"),
        ),
        agents=list(agents),
        parallel=parallel or [],
        for_each=for_each or [],
    )


class TestValidWait:
    """Wait agents accept duration as int/float/string or Jinja template."""

    def test_int_seconds(self) -> None:
        a = WaitStepDef(name="w", duration=60)
        assert a.type == "wait"
        assert a.duration == 60

    def test_float_seconds(self) -> None:
        a = WaitStepDef(name="w", duration=1.5)
        assert a.duration == 1.5

    def test_string_seconds(self) -> None:
        a = WaitStepDef(name="w", duration="60s")
        assert a.duration == "60s"

    def test_string_minutes(self) -> None:
        WaitStepDef(name="w", duration="5m")

    def test_string_milliseconds(self) -> None:
        WaitStepDef(name="w", duration="500ms")

    def test_string_hours(self) -> None:
        WaitStepDef(name="w", duration="1h")

    def test_24h_cap_inclusive(self) -> None:
        # Exactly 24h is allowed.
        WaitStepDef(name="w", duration="24h")

    def test_templated_duration_deferred(self) -> None:
        # Templates are not parsed at schema time.
        a = WaitStepDef(name="w", duration="{{ workflow.input.x }}s")
        assert a.duration == "{{ workflow.input.x }}s"

    def test_templated_garbage_deferred(self) -> None:
        # Even nonsense after the template is OK at schema time.
        WaitStepDef(name="w", duration="{{ x }}-not-a-duration")

    def test_optional_reason(self) -> None:
        a = WaitStepDef(name="w", duration="1s", reason="hello")
        assert a.reason == "hello"


class TestWaitRequiresDuration:
    def test_missing_duration(self) -> None:
        # duration is a required field on the wait variant.
        with pytest.raises(PydanticValidationError) as exc_info:
            WaitStepDef(name="w")
        assert any(
            e["loc"] == ("duration",) and e["type"] == "missing" for e in exc_info.value.errors()
        )


class TestWaitDurationBounds:
    def test_zero_rejected(self) -> None:
        # Variant-owned invariant: duration must be positive.
        with pytest.raises(PydanticValidationError, match="must be > 0"):
            WaitStepDef(name="w", duration=0)

    def test_negative_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            WaitStepDef(name="w", duration=-1)

    def test_over_24h_rejected(self) -> None:
        # Variant-owned invariant: duration is capped at 24h.
        with pytest.raises(PydanticValidationError, match="24h cap"):
            WaitStepDef(name="w", duration="25h")

    def test_just_over_24h_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="24h cap"):
            WaitStepDef(name="w", duration=86401)


class TestWaitDurationBool:
    def test_true_rejected(self) -> None:
        # Booleans must be rejected pre-coercion. Pydantic v2 would
        # otherwise accept True as int 1.
        with pytest.raises(PydanticValidationError, match="boolean"):
            WaitStepDef(name="w", duration=True)

    def test_false_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="boolean"):
            WaitStepDef(name="w", duration=False)


class TestWaitForbiddenFields:
    """Fields owned by other step variants must be rejected on wait (extra_forbidden)."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("prompt", "x"),
            ("provider", "copilot"),
            ("model", "claude-haiku-4.5"),
            ("system_prompt", "x"),
            ("command", "ls"),
            ("working_dir", "/tmp"),
            ("timeout", 5),
            ("workflow", "./sub.yaml"),
            ("max_session_seconds", 30.0),
            ("max_agent_iterations", 5),
            ("timeout_seconds", 10.0),
        ],
    )
    def test_forbidden(self, field: str, value: object) -> None:
        # extra="forbid": a field belonging to another variant fails on that field.
        with pytest.raises(PydanticValidationError) as exc_info:
            WaitStepDef.model_validate({"name": "w", "duration": "1s", field: value})
        _assert_extra_forbidden(exc_info, field)

    def test_tools_list_rejected(self) -> None:
        with pytest.raises(PydanticValidationError) as exc_info:
            WaitStepDef.model_validate({"name": "w", "duration": "1s", "tools": ["foo"]})
        _assert_extra_forbidden(exc_info, "tools")

    def test_options_rejected(self) -> None:
        with pytest.raises(PydanticValidationError) as exc_info:
            WaitStepDef.model_validate(
                {
                    "name": "w",
                    "duration": "1s",
                    "options": [GateOption(label="x", value="x", route="$end")],
                }
            )
        _assert_extra_forbidden(exc_info, "options")

    def test_args_rejected(self) -> None:
        with pytest.raises(PydanticValidationError) as exc_info:
            WaitStepDef.model_validate({"name": "w", "duration": "1s", "args": ["x"]})
        _assert_extra_forbidden(exc_info, "args")

    def test_env_rejected(self) -> None:
        with pytest.raises(PydanticValidationError) as exc_info:
            WaitStepDef.model_validate({"name": "w", "duration": "1s", "env": {"FOO": "bar"}})
        _assert_extra_forbidden(exc_info, "env")

    def test_input_mapping_rejected(self) -> None:
        with pytest.raises(PydanticValidationError) as exc_info:
            WaitStepDef.model_validate({"name": "w", "duration": "1s", "input_mapping": {"x": "y"}})
        _assert_extra_forbidden(exc_info, "input_mapping")

    def test_max_depth_rejected(self) -> None:
        with pytest.raises(PydanticValidationError) as exc_info:
            WaitStepDef.model_validate({"name": "w", "duration": "1s", "max_depth": 2})
        _assert_extra_forbidden(exc_info, "max_depth")

    def test_output_rejected(self) -> None:
        with pytest.raises(PydanticValidationError) as exc_info:
            WaitStepDef.model_validate(
                {"name": "w", "duration": "1s", "output": {"x": {"type": "string"}}}
            )
        _assert_extra_forbidden(exc_info, "output")


class TestWaitFieldsOnOtherTypes:
    """duration/reason are wait-only — other variants must reject them."""

    def test_duration_on_plain_agent(self) -> None:
        with pytest.raises(PydanticValidationError) as exc_info:
            AgentDef.model_validate({"name": "a", "duration": "1s", "prompt": "hi"})
        _assert_extra_forbidden(exc_info, "duration")

    def test_reason_on_plain_agent(self) -> None:
        with pytest.raises(PydanticValidationError) as exc_info:
            AgentDef.model_validate({"name": "a", "reason": "x", "prompt": "hi"})
        _assert_extra_forbidden(exc_info, "reason")

    def test_duration_on_script(self) -> None:
        with pytest.raises(PydanticValidationError) as exc_info:
            ScriptStepDef.model_validate({"name": "s", "command": "ls", "duration": "1s"})
        _assert_extra_forbidden(exc_info, "duration")


class TestWaitInParallelOrForEach:
    """Wait steps cannot be used in parallel groups or for-each groups."""

    def test_reject_wait_in_parallel(self) -> None:
        wait = WaitStepDef(name="w", duration="1s", routes=[RouteDef(to="$end")])
        other = WaitStepDef(name="o", duration="1s", routes=[RouteDef(to="$end")])
        config = _make_workflow(
            wait,
            other,
            parallel=[ParallelGroup(name="pg", agents=["w", "o"], routes=[RouteDef(to="$end")])],
        )
        with pytest.raises(ConfigurationError, match="Wait steps cannot be used in parallel"):
            validate_workflow_config(config)

    def test_reject_wait_in_for_each(self) -> None:
        wait = WaitStepDef(name="w", duration="1s", routes=[RouteDef(to="$end")])
        # An entry-point agent + a producer agent (so the for-each
        # source resolves to a real agent reference).
        entry = AgentDef(
            name="entry",
            prompt="x",
            model="m",
            output={"items": OutputField(type="array", items={"type": "string"})},
            routes=[RouteDef(to="fe")],
        )
        for_each = ForEachDef(
            name="fe",
            type="for_each",
            source="entry.output.items",
            **{"as": "item"},
            agent=wait,
            routes=[RouteDef(to="$end")],
        )
        config = _make_workflow(entry, for_each=[for_each])
        with pytest.raises(ConfigurationError, match="Wait steps cannot be used in for_each"):
            validate_workflow_config(config)


class TestWaitValidationViaWorkflow:
    """Smoke test: a workflow containing only a wait step validates."""

    def test_minimal_wait_workflow(self) -> None:
        wait = WaitStepDef(name="w", duration="100ms", routes=[RouteDef(to="$end")])
        config = _make_workflow(wait)
        # Should not raise.
        validate_workflow_config(config)


class TestWorkingDirTypeMatrix:
    """Requirement: ``working_dir`` is allowed on provider-backed LLM agents and
    script steps, and rejected on wait/set/terminate/human_gate/workflow types."""

    def test_working_dir_allowed_on_llm_agent(self) -> None:
        agent = AgentDef(name="llm", prompt="hi", working_dir="/repo")
        assert agent.working_dir == "/repo"

    def test_working_dir_allowed_on_script_step(self) -> None:
        agent = ScriptStepDef(name="script", command="ls", working_dir="/repo")
        assert agent.working_dir == "/repo"

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(
                lambda: WaitStepDef.model_validate(
                    {"name": "w", "duration": "1s", "working_dir": "/repo"}
                ),
                id="wait",
            ),
            pytest.param(
                lambda: SetStepDef.model_validate(
                    {"name": "s", "value": "1", "working_dir": "/repo"}
                ),
                id="set",
            ),
            pytest.param(
                lambda: TerminateStepDef.model_validate(
                    {"name": "t", "status": "success", "reason": "done", "working_dir": "/repo"}
                ),
                id="terminate",
            ),
            pytest.param(
                lambda: HumanGateStepDef.model_validate(
                    {
                        "name": "g",
                        "prompt": "Pick",
                        "options": [GateOption(label="Yes", value="yes", route="$end")],
                        "working_dir": "/repo",
                    }
                ),
                id="human_gate",
            ),
            pytest.param(
                lambda: WorkflowStepDef.model_validate(
                    {"name": "wf", "workflow": "./sub.yaml", "working_dir": "/repo"}
                ),
                id="workflow",
            ),
        ],
    )
    def test_working_dir_rejected(self, build: Callable[[], object]) -> None:
        # extra="forbid": working_dir is not a field of these variants.
        with pytest.raises(PydanticValidationError) as exc_info:
            build()
        assert any(
            e["loc"][-1] == "working_dir" and e["type"] == "extra_forbidden"
            for e in exc_info.value.errors()
        )
