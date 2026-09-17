"""Which optional ``AgentDef`` fields each step type accepts.

Fields that only mean something for provider-backed LLM agents are rejected
on the other step types at load time, so a typo surfaces in ``conductor
validate`` rather than being silently ignored at runtime.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from conductor.config.schema import (
    AgentDef,
    GateOption,
    HumanGateStepDef,
    QuestionsStepDef,
    ScriptStepDef,
    SetStepDef,
    StepBase,
    TerminateStepDef,
    WaitStepDef,
    WorkflowConfig,
    WorkflowStepDef,
)


class TestStaticStepUnion:
    """Workflow parsing produces concrete variants and publishes their discriminator."""

    def test_workflow_config_stores_concrete_step_models(self) -> None:
        # Requirement: parsed workflow steps retain their named runtime variant types.
        config = WorkflowConfig.model_validate(
            {
                "workflow": {"name": "typed", "entry_point": "write"},
                "agents": [
                    {"name": "write", "prompt": "Write", "routes": [{"to": "pause"}]},
                    {"name": "pause", "type": "wait", "duration": "1s"},
                ],
            }
        )

        assert type(config.agents[0]) is AgentDef
        assert type(config.agents[1]) is WaitStepDef

    def test_null_llm_type_is_canonicalized(self) -> None:
        # Requirement: an explicit YAML null discriminator remains compatible with LLM steps.
        config = WorkflowConfig.model_validate(
            {
                "workflow": {"name": "typed", "entry_point": "write"},
                "agents": [{"name": "write", "type": None, "prompt": "Write"}],
            }
        )

        assert type(config.agents[0]) is AgentDef
        assert config.agents[0].type == "agent"

    def test_workflow_json_schema_exposes_static_discriminator(self) -> None:
        # Requirement: tooling can select a step schema through JSON Schema oneOf metadata.
        agents_schema = WorkflowConfig.model_json_schema()["properties"]["agents"]["items"]

        assert agents_schema["discriminator"]["propertyName"] == "type"
        assert set(agents_schema["discriminator"]["mapping"]) == {
            "agent",
            "human_gate",
            "mcp",
            "questions",
            "script",
            "set",
            "terminate",
            "wait",
            "workflow",
        }
        assert len(agents_schema["oneOf"]) == 9

    def test_variant_rejects_fields_owned_by_another_step(self) -> None:
        # Requirement: each concrete model forbids fields owned by sibling variants
        # via extra="forbid" (standard extra_forbidden error).
        with pytest.raises(PydanticValidationError) as exc_info:
            ScriptStepDef.model_validate(
                {"name": "run", "command": "echo", "prompt": "not allowed"}
            )
        assert any(
            e["loc"] == ("prompt",) and e["type"] == "extra_forbidden"
            for e in exc_info.value.errors()
        )


class TestSessionKeyTypeMatrix:
    """Requirement: ``session_key`` is allowed only on provider-backed LLM
    agents — every other step type has no provider session to continue."""

    def test_session_key_allowed_on_llm_agent(self) -> None:
        agent = AgentDef(name="llm", prompt="hi", session_key="investigation")
        assert agent.session_key == "investigation"

    def test_session_key_defaults_to_none(self) -> None:
        assert AgentDef(name="llm", prompt="hi").session_key is None

    def test_empty_session_key_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="session_key"):
            AgentDef(name="llm", prompt="hi", session_key="")

    @pytest.mark.parametrize(
        "step_class,valid_kwargs",
        [
            (ScriptStepDef, {"name": "sc", "command": "ls"}),
            (
                QuestionsStepDef,
                {"name": "q", "questions": [{"id": "q1", "text": "Why?"}]},
            ),
            (WaitStepDef, {"name": "w", "duration": "1s"}),
            (SetStepDef, {"name": "s", "value": "1"}),
            (TerminateStepDef, {"name": "t", "status": "success", "reason": "done"}),
            (
                HumanGateStepDef,
                {
                    "name": "g",
                    "prompt": "Pick",
                    "options": [GateOption(label="Yes", value="yes", route="$end")],
                },
            ),
            (WorkflowStepDef, {"name": "wf", "workflow": "./sub.yaml"}),
        ],
        ids=["script", "questions", "wait", "set", "terminate", "human_gate", "workflow"],
    )
    def test_session_key_rejected(self, step_class: type[StepBase], valid_kwargs: dict) -> None:
        # Requirement: session_key is an LLM-agent-only field; every sibling variant
        # forbids it via extra="forbid" (standard extra_forbidden error, not a
        # per-type custom message).
        with pytest.raises(PydanticValidationError) as exc_info:
            step_class.model_validate({**valid_kwargs, "session_key": "investigation"})
        assert any(
            e["loc"] == ("session_key",) and e["type"] == "extra_forbidden"
            for e in exc_info.value.errors()
        )


class TestSessionKeyLiteral:
    """``session_key`` is never rendered, so a template must not pass silently."""

    @pytest.mark.parametrize(
        "value",
        ["item-{{ _key }}", "{{ workflow.input.id }}", "{% if x %}a{% endif %}"],
        ids=["expression", "input-ref", "statement"],
    )
    def test_template_rejected(self, value: str) -> None:
        with pytest.raises(PydanticValidationError, match="never rendered"):
            AgentDef(name="a", prompt="hi", session_key=value)

    def test_static_label_accepted(self) -> None:
        assert AgentDef(name="a", prompt="hi", session_key="investigation").session_key == (
            "investigation"
        )
