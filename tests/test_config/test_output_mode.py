"""Tests for the output_mode field on AgentDef."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import (
    AgentDef,
    HumanGateStepDef,
    OutputField,
    ScriptStepDef,
    SetStepDef,
    TerminateStepDef,
    WaitStepDef,
    WorkflowStepDef,
)


def _assert_extra_forbidden(exc_info: pytest.ExceptionInfo[ValidationError], field: str) -> None:
    """Assert the error is Pydantic's standard extra_forbidden on ``field``.

    The step-model split (issue #517) removed the per-type custom
    "cannot have '<field>'" messages; the contract is now the schema-level
    ``extra="forbid"`` error on the foreign field.
    """
    assert any(
        e["loc"] == (field,) and e["type"] == "extra_forbidden" for e in exc_info.value.errors()
    )


class TestOutputModeValidation:
    """Tests for output_mode validation rules on AgentDef."""

    def test_raw_without_output_is_valid(self) -> None:
        """output_mode='raw' with no output schema is valid."""
        agent = AgentDef(name="a", prompt="p", output_mode="raw")
        assert agent.output_mode == "raw"
        assert agent.output is None

    def test_envelope_with_output_is_valid(self) -> None:
        """output_mode='envelope' with output schema is valid."""
        agent = AgentDef(
            name="a",
            prompt="p",
            output_mode="envelope",
            output={"field": OutputField(type="string")},
        )
        assert agent.output_mode == "envelope"
        assert agent.output is not None

    def test_raw_with_output_raises_validation_error(self) -> None:
        """output_mode='raw' combined with output schema is rejected."""
        with pytest.raises(ValidationError, match="output_mode 'raw' is incompatible"):
            AgentDef(
                name="a",
                prompt="p",
                output_mode="raw",
                output={"field": OutputField(type="string")},
            )

    def test_raw_on_script_raises_validation_error(self) -> None:
        """output_mode on a script step is rejected via extra="forbid"."""
        with pytest.raises(ValidationError) as exc_info:
            ScriptStepDef.model_validate({"name": "a", "command": "echo hi", "output_mode": "raw"})
        _assert_extra_forbidden(exc_info, "output_mode")

    def test_raw_on_human_gate_raises_validation_error(self) -> None:
        """output_mode on a human_gate step is rejected via extra="forbid"."""
        with pytest.raises(ValidationError) as exc_info:
            HumanGateStepDef.model_validate(
                {
                    "name": "a",
                    "prompt": "Choose",
                    "options": [
                        {"value": "yes", "label": "Yes", "route": "next"},
                    ],
                    "output_mode": "raw",
                }
            )
        _assert_extra_forbidden(exc_info, "output_mode")

    def test_raw_on_workflow_raises_validation_error(self) -> None:
        """output_mode on a workflow (sub-workflow) step is rejected via extra="forbid"."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowStepDef.model_validate(
                {"name": "a", "workflow": "sub.yaml", "output_mode": "raw"}
            )
        _assert_extra_forbidden(exc_info, "output_mode")

    def test_raw_on_wait_raises_validation_error(self) -> None:
        """output_mode on a wait step is rejected via extra="forbidden"."""
        with pytest.raises(ValidationError) as exc_info:
            WaitStepDef.model_validate({"name": "a", "duration": 60, "output_mode": "raw"})
        _assert_extra_forbidden(exc_info, "output_mode")

    def test_raw_on_set_raises_validation_error(self) -> None:
        """output_mode on a set step is rejected via extra="forbid"."""
        with pytest.raises(ValidationError) as exc_info:
            SetStepDef.model_validate({"name": "a", "value": "42", "output_mode": "raw"})
        _assert_extra_forbidden(exc_info, "output_mode")

    def test_raw_on_terminate_raises_validation_error(self) -> None:
        """output_mode on a terminate step is rejected via extra="forbid"."""
        with pytest.raises(ValidationError) as exc_info:
            TerminateStepDef.model_validate(
                {"name": "a", "status": "success", "reason": "done", "output_mode": "raw"}
            )
        _assert_extra_forbidden(exc_info, "output_mode")

    def test_none_with_output_is_valid(self) -> None:
        """output_mode=None (default) with output schema is valid — backward compat."""
        agent = AgentDef(
            name="a",
            prompt="p",
            output={"field": OutputField(type="string")},
        )
        assert agent.output_mode is None
        assert agent.output is not None

    def test_none_without_output_is_valid(self) -> None:
        """output_mode=None (default) without output schema is valid — backward compat."""
        agent = AgentDef(name="a", prompt="p")
        assert agent.output_mode is None
        assert agent.output is None

    def test_envelope_without_output_is_valid(self) -> None:
        """output_mode='envelope' without output schema is valid (no-op, wraps as result)."""
        agent = AgentDef(name="a", prompt="p", output_mode="envelope")
        assert agent.output_mode == "envelope"
        assert agent.output is None

    def test_invalid_output_mode_value_rejected(self) -> None:
        """An invalid output_mode string is rejected by the Literal type."""
        with pytest.raises(ValidationError):
            AgentDef(name="a", prompt="p", output_mode="invalid")  # type: ignore[arg-type]
