"""Runtime defensive guards for the step dispatch (issue #517).

The static validator rejects unsupported variants inside parallel and
for-each groups, but a directly-constructed engine never runs it — these
tests pin the engine-side guards so an unsupported variant fails with a
clear ``ExecutionError`` instead of an ``AttributeError`` deep in a
provider call.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    TerminateStepDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ExecutionError
from conductor.providers.base import AgentOutput


def _mock_provider() -> MagicMock:
    provider = MagicMock()
    provider.execute = AsyncMock(
        return_value=AgentOutput(content={"result": "ok"}, raw_response={}, model="gpt-4")
    )
    return provider


def _workflow_config(**overrides) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="defensive",
            entry_point=overrides.pop("entry_point"),
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        **overrides,
    )


class TestParallelGroupGuards:
    """Unsupported variants in a parallel group fail with a named error."""

    @pytest.mark.asyncio
    async def test_terminate_in_parallel_group_rejected(self) -> None:
        # Requirement: a terminate step inside a parallel group raises
        # ExecutionError naming the step, not an AttributeError on routes/provider.
        config = _workflow_config(
            entry_point="group",
            agents=[
                TerminateStepDef(name="stop", status="success", reason="done"),
                AgentDef(
                    name="worker",
                    model="gpt-4",
                    prompt="Work",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[ParallelGroup(name="group", agents=["stop", "worker"])],
        )
        engine = WorkflowEngine(config, _mock_provider())

        with pytest.raises(ExecutionError, match="cannot execute in a parallel group"):
            await engine.run({})


class TestForEachGuards:
    """Unsupported inline for-each variants fail with a named error."""

    @pytest.mark.asyncio
    async def test_terminate_inline_agent_rejected(self) -> None:
        # Requirement: a terminate inline agent raises ExecutionError naming
        # the group, not an AttributeError on model_copy/provider access.
        config = _workflow_config(
            entry_point="seed",
            agents=[
                AgentDef(
                    name="seed",
                    model="gpt-4",
                    prompt="Seed",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="loop")],
                ),
                TerminateStepDef(name="stop", status="success", reason="done"),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "loop",
                        "type": "for_each",
                        "source": "seed.output.items",
                        "as": "item",
                        "agent": {
                            "name": "stop",
                            "type": "terminate",
                            "status": "success",
                            "reason": "done",
                        },
                        "routes": [{"to": "$end"}],
                    }
                )
            ],
        )
        provider = _mock_provider()
        provider.execute = AsyncMock(
            return_value=AgentOutput(content={"items": [1, 2]}, raw_response={}, model="gpt-4")
        )
        engine = WorkflowEngine(config, provider)

        with pytest.raises(ExecutionError, match="cannot execute as the inline agent"):
            await engine.run({})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "inline_agent",
        [
            {"name": "s", "type": "script", "command": "echo hi"},
            {"name": "w", "type": "wait", "duration": "1s"},
            {"name": "t", "type": "terminate", "status": "success", "reason": "done"},
            {"name": "q", "type": "questions", "questions": [{"id": "q1", "text": "Why?"}]},
            {
                "name": "g",
                "type": "human_gate",
                "prompt": "Pick",
                "options": [{"label": "Yes", "value": "yes", "route": "$end"}],
            },
        ],
        ids=["script", "wait", "terminate", "questions", "human_gate"],
    )
    async def test_forbidden_inline_agent_rejected_even_with_empty_source(
        self, inline_agent: dict
    ) -> None:
        # Requirement: the defensive guard fires before the empty-array early
        # return — a forbidden inline variant must not pass silently just
        # because the source resolved to zero items.
        config = _workflow_config(
            entry_point="loop",
            agents=[],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "loop",
                        "type": "for_each",
                        "source": "workflow.input.items",
                        "as": "item",
                        "agent": inline_agent,
                        "routes": [{"to": "$end"}],
                    }
                )
            ],
        )
        engine = WorkflowEngine(config, _mock_provider())

        with pytest.raises(ExecutionError, match="cannot execute as the inline agent"):
            await engine.run({"items": []})
