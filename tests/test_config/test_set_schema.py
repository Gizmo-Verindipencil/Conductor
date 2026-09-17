"""Tests for 'set' type schema validation.

Covers:
- Valid single-value and multi-values set step definitions
- Mutual exclusion of value/values (both forbidden, neither forbidden)
- output_type only valid on single value (forbidden on values)
- Every field owned by another variant rejected via extra_forbidden
- value/values/output_type rejected on non-set variants
- Cross-validator (set in entry point, parallel groups, for_each)
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from conductor.config.schema import (
    AgentDef,
    ForEachDef,
    GateOption,
    HumanGateStepDef,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    RetryPolicy,
    RouteDef,
    RuntimeConfig,
    ScriptStepDef,
    SetStepDef,
    WorkflowConfig,
    WorkflowDef,
    WorkflowStepDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError

SetOutputType = Literal["auto", "string", "number", "integer", "boolean", "list", "dict"]


def _assert_extra_forbidden(exc_info: pytest.ExceptionInfo[ValidationError], field: str) -> None:
    """Assert a variant-owned-by-sibling field failed with extra_forbidden on that field."""
    assert any(
        e["loc"] == (field,) and e["type"] == "extra_forbidden" for e in exc_info.value.errors()
    )


class TestSetAgentDefValidConfigs:
    """Valid set-type agent definitions."""

    def test_valid_single_value(self) -> None:
        agent = SetStepDef(name="compute", value="{{ workflow.input.org }}")
        assert agent.type == "set"
        assert agent.value == "{{ workflow.input.org }}"
        assert agent.values is None
        assert agent.output_type is None

    def test_valid_multi_values(self) -> None:
        agent = SetStepDef(
            name="derive",
            values={
                "is_breaking": "{{ true }}",
                "target_branch": "main",
            },
        )
        assert agent.values is not None
        assert agent.value is None
        assert len(agent.values) == 2

    def test_valid_with_output_type_on_single(self) -> None:
        output_types: list[SetOutputType] = [
            "auto",
            "string",
            "number",
            "integer",
            "boolean",
            "list",
            "dict",
        ]
        for ot in output_types:
            agent = SetStepDef(name="x", value="42", output_type=ot)
            assert agent.output_type == ot

    def test_valid_with_routes(self) -> None:
        agent = SetStepDef(
            name="flag",
            value="{{ true }}",
            routes=[RouteDef(to="$end")],
        )
        assert len(agent.routes) == 1

    def test_valid_with_input_declarations(self) -> None:
        agent = SetStepDef(
            name="combine",
            value="{{ research.output.summary }}",
            input=["research.output"],
        )
        assert agent.input == ["research.output"]

    def test_valid_with_output_schema(self) -> None:
        agent = SetStepDef(
            name="flags",
            values={"ok": "{{ true }}"},
            output={"ok": OutputField(type="boolean")},
        )
        assert agent.output is not None and "ok" in agent.output


class TestSetAgentDefMutualExclusion:
    """value: / values: mutual exclusion."""

    def test_neither_value_nor_values_rejected(self) -> None:
        # Variant-owned invariant: exactly one of value/values is required.
        with pytest.raises(ValidationError, match="exactly one of 'value' or 'values'"):
            SetStepDef(name="bad")

    def test_both_value_and_values_rejected(self) -> None:
        # Variant-owned invariant: value and values are mutually exclusive.
        with pytest.raises(ValidationError, match="exactly one of 'value' or 'values'"):
            SetStepDef(name="bad", value="1", values={"a": "2"})

    def test_output_type_with_values_rejected(self) -> None:
        # Variant-owned invariant: output_type only applies to a single value.
        with pytest.raises(ValidationError, match="output_type"):
            SetStepDef(
                name="bad",
                values={"a": "1"},
                output_type="string",
            )

    def test_output_type_with_value_accepted(self) -> None:
        agent = SetStepDef(name="ok", value="1", output_type="integer")
        assert agent.output_type == "integer"


class TestSetAgentDefForbiddenFields:
    """Fields owned by other step variants must be rejected on set (extra_forbidden)."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("prompt", "hi"),
            ("provider", "copilot"),
            ("model", "gpt-4"),
            ("tools", ["web_search"]),
            ("system_prompt", "you are"),
            ("options", [GateOption(label="OK", value="ok", route="$end")]),
            ("command", "echo"),
            ("args", ["x"]),
            ("env", {"K": "v"}),
            ("working_dir", "/tmp"),
            ("settings_dir", "/tmp"),
            ("timeout", 5),
            ("workflow", "x.yaml"),
            ("input_mapping", {"a": "1"}),
            ("max_depth", 2),
            ("max_session_seconds", 10.0),
            ("max_agent_iterations", 5),
            ("retry", RetryPolicy(max_attempts=2)),
            ("timeout_seconds", 5.0),
        ],
    )
    def test_forbidden_field_rejected(self, field: str, value: object) -> None:
        # extra="forbid": a field belonging to another variant fails on that field.
        with pytest.raises(ValidationError) as exc_info:
            SetStepDef.model_validate({"name": "bad", "value": "x", field: value})
        _assert_extra_forbidden(exc_info, field)


class TestSetFieldsOnOtherTypes:
    """value/values/output_type are set-only — other variants must reject them."""

    def test_value_on_default_agent_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentDef.model_validate({"name": "bad", "value": "x"})
        _assert_extra_forbidden(exc_info, "value")

    def test_values_on_default_agent_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentDef.model_validate({"name": "bad", "values": {"a": "1"}})
        _assert_extra_forbidden(exc_info, "values")

    def test_output_type_on_default_agent_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentDef.model_validate({"name": "bad", "output_type": "string"})
        _assert_extra_forbidden(exc_info, "output_type")

    def test_value_on_script_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ScriptStepDef.model_validate({"name": "bad", "command": "echo", "value": "x"})
        _assert_extra_forbidden(exc_info, "value")

    def test_values_on_script_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ScriptStepDef.model_validate({"name": "bad", "command": "echo", "values": {"a": "1"}})
        _assert_extra_forbidden(exc_info, "values")

    def test_output_type_on_script_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ScriptStepDef.model_validate(
                {"name": "bad", "command": "echo", "output_type": "string"}
            )
        _assert_extra_forbidden(exc_info, "output_type")

    def test_value_on_human_gate_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            HumanGateStepDef.model_validate(
                {
                    "name": "bad",
                    "prompt": "?",
                    "options": [GateOption(label="OK", value="ok", route="$end")],
                    "value": "x",
                }
            )
        _assert_extra_forbidden(exc_info, "value")

    def test_value_on_workflow_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            WorkflowStepDef.model_validate({"name": "bad", "workflow": "x.yaml", "value": "x"})
        _assert_extra_forbidden(exc_info, "value")


class TestSetWorkflowConfig:
    """Cross-validator scenarios."""

    def test_set_at_entry_point_validates(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="compute",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                SetStepDef(
                    name="compute",
                    value="{{ true }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_set_routes_to_agent(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="flag",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                SetStepDef(
                    name="flag",
                    value="{{ true }}",
                    routes=[RouteDef(to="downstream")],
                ),
                AgentDef(name="downstream", prompt="hi", routes=[RouteDef(to="$end")]),
            ],
        )
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_set_in_parallel_group_allowed(self) -> None:
        """Per issue #221, set steps are permitted in parallel groups."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="grp",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(name="real", prompt="hi"),
                SetStepDef(name="bind", value="{{ workflow.input.x }}"),
            ],
            parallel=[
                ParallelGroup(name="grp", agents=["real", "bind"], routes=[RouteDef(to="$end")]),
            ],
        )
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_set_in_for_each_allowed(self) -> None:
        """Per issue #221, set steps may be inline agents in for_each.

        Note: the for_each ``source:`` validator requires a 3-part path, so a
        set step producing a list at ``step.output`` cannot be used directly
        as a source — use ``values: {items: ...}`` instead so ``step.output.items``
        is a valid reference.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="setup",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                SetStepDef(
                    name="setup",
                    values={"items": "{{ [1, 2, 3] }}"},
                    routes=[RouteDef(to="loop")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="loop",
                    type="for_each",
                    source="setup.output.items",
                    **{"as": "item"},
                    agent=SetStepDef(
                        name="binder",
                        value="item-{{ item }}",
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_set_cannot_depend_on_sibling_in_parallel_group(self) -> None:
        """Set templates referencing same-group siblings must be rejected."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="grp",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(name="sibling", prompt="hi"),
                SetStepDef(
                    name="bind",
                    value="{{ sibling.output.summary }}",
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="grp",
                    agents=["sibling", "bind"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="same parallel group"):
            validate_workflow_config(config)


class TestSetBackwardCompatibility:
    """Existing variants still work alongside set steps."""

    def test_default_agent_unchanged(self) -> None:
        # Missing/null type normalizes to a plain LLM agent.
        a = AgentDef(name="x", prompt="hi")
        assert a.type == "agent"
        assert a.prompt == "hi"

    def test_script_unchanged(self) -> None:
        # Script steps still construct independently of the set variant.
        a = ScriptStepDef(name="x", command="echo")
        assert a.type == "script"
        assert a.command == "echo"
