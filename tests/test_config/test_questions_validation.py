"""Schema and cross-reference validation for ``type: questions`` (issue #376)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import (
    AgentDef,
    HumanGateStepDef,
    OutputField,
    ParallelGroup,
    QuestionDef,
    QuestionsStepDef,
    RouteDef,
    ScriptStepDef,
    StepDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


def _questions(**kwargs) -> QuestionsStepDef:
    """Build a questions node with a single inline question by default."""
    kwargs.setdefault("questions", [QuestionDef(text="Why?")])
    return QuestionsStepDef(name="ask", **kwargs)


def _assert_extra_forbidden(exc_info: pytest.ExceptionInfo, field: str) -> None:
    """The step-model split rejects foreign fields with a plain extra_forbidden."""
    assert any(
        e["loc"] == (field,) and e["type"] == "extra_forbidden" for e in exc_info.value.errors()
    )


class TestQuestionsSchema:
    """Field-level rules on the node itself."""

    def test_requires_questions_or_source(self) -> None:
        """A node with no question source has nothing to ask."""
        with pytest.raises(ValidationError, match="require either 'questions' or 'source'"):
            QuestionsStepDef(name="ask")

    def test_rejects_both_questions_and_source(self) -> None:
        """Two sources of truth would be ambiguous."""
        with pytest.raises(ValidationError, match="cannot set both"):
            QuestionsStepDef(
                name="ask",
                questions=[QuestionDef(text="a")],
                source="x.output.y",
            )

    def test_rejects_gate_options(self) -> None:
        """Per-question choices live on the question, not the node."""
        with pytest.raises(ValidationError) as exc_info:
            QuestionsStepDef.model_validate(
                {"name": "ask", "questions": [{"text": "Why?"}], "options": []}
            )
        _assert_extra_forbidden(exc_info, "options")

    def test_rejects_abort_route_without_allow_abort(self) -> None:
        """An abort route that can never be taken is a silent no-op."""
        with pytest.raises(ValidationError, match="without 'allow_abort"):
            _questions(abort_route="rescue")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("model", "gpt-4"),
            ("provider", "copilot"),
            ("tools", []),
            ("output", {"x": OutputField(type="string")}),
            ("timeout_seconds", 30),
            ("working_dir", "/tmp"),
            ("skills", ["conductor"]),
            ("reasoning", {"effort": "high"}),
        ],
    )
    def test_rejects_provider_only_fields(self, field: str, value: object) -> None:
        """No provider is invoked, so provider-shaped config must not be accepted."""
        with pytest.raises(ValidationError) as exc_info:
            QuestionsStepDef.model_validate(
                {"name": "ask", "questions": [{"text": "Why?"}], field: value}
            )
        _assert_extra_forbidden(exc_info, field)

    def test_source_must_be_a_dotted_path(self) -> None:
        """`source` inherits ForEachDef's format enforcement, not just its name."""
        with pytest.raises(ValidationError, match="Invalid source format"):
            QuestionsStepDef(name="ask", source="architect")

    def test_valid_source_is_accepted(self) -> None:
        """A well-formed dotted path passes."""
        agent = QuestionsStepDef(name="ask", source="architect.output.open_questions")

        assert agent.source == "architect.output.open_questions"


class TestQuestionsFieldsRejectedElsewhere:
    """The questions-only fields must not be silently ignored on other types."""

    def test_source_rejected_on_a_provider_agent(self) -> None:
        """`source` is owned by questions/for-each steps; nothing else reads it."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef.model_validate(
                {"name": "a", "model": "gpt-4", "prompt": "p", "source": "x.output.y"}
            )
        _assert_extra_forbidden(exc_info, "source")

    def test_nav_flag_rejected_on_a_script(self) -> None:
        """Tri-state flags exist so an explicit value is catchable here."""
        with pytest.raises(ValidationError) as exc_info:
            ScriptStepDef.model_validate({"name": "s", "command": "ls", "allow_back": False})
        _assert_extra_forbidden(exc_info, "allow_back")

    def test_questions_list_rejected_on_a_gate(self) -> None:
        """A human_gate has options, not questions."""
        with pytest.raises(ValidationError) as exc_info:
            HumanGateStepDef.model_validate(
                {
                    "name": "g",
                    "prompt": "p",
                    "options": [{"label": "a", "value": "a", "route": "$end"}],
                    "questions": [{"text": "q"}],
                }
            )
        _assert_extra_forbidden(exc_info, "questions")


class TestQuestionDefSchema:
    """Per-question rules."""

    def test_rejects_an_unanswerable_question(self) -> None:
        """No choices and no free text leaves the user nothing to select."""
        with pytest.raises(ValidationError, match="unanswerable"):
            QuestionDef(text="Why?", allow_free_text=False)

    def test_choices_without_free_text_is_answerable(self) -> None:
        """Choices alone are enough."""
        question = QuestionDef(text="Why?", allow_free_text=False, choices=["a", "b"])

        assert question.choices == ["a", "b"]

    def test_free_text_defaults_to_multiline(self) -> None:
        """Long answers are the expected case here."""
        assert QuestionDef(text="Why?").multiline is True


class TestQuestionsCrossReferences:
    """Workflow-level validation."""

    def _config(self, agent: StepDef, **kwargs) -> WorkflowConfig:
        return WorkflowConfig(
            workflow=WorkflowDef(name="w", entry_point="ask"),
            agents=[
                agent,
                AgentDef(
                    name="after",
                    model="gpt-4",
                    prompt="p",
                    output={"r": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            **kwargs,
        )

    def test_unknown_abort_route_is_rejected(self) -> None:
        """The abort edge is only taken after the human has done the work."""
        agent = _questions(allow_abort=True, abort_route="nowhere", routes=[RouteDef(to="after")])

        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(self._config(agent))

        assert "abort_route targets unknown agent" in str(exc.value)

    def test_known_abort_route_is_accepted(self) -> None:
        """A real target passes."""
        agent = _questions(allow_abort=True, abort_route="after", routes=[RouteDef(to="after")])

        validate_workflow_config(self._config(agent))

    def test_end_abort_route_is_accepted(self) -> None:
        """`$end` is always a valid target."""
        agent = _questions(allow_abort=True, abort_route="$end", routes=[RouteDef(to="after")])

        validate_workflow_config(self._config(agent))

    def test_rejected_in_a_parallel_group(self) -> None:
        """Concurrent prompts would compete for one terminal and one gate slot."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="w", entry_point="grp"),
            agents=[
                _questions(),
                AgentDef(name="other", model="gpt-4", prompt="p"),
            ],
            parallel=[
                ParallelGroup(
                    name="grp",
                    agents=["ask", "other"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )

        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(config)

        assert "is a questions step" in str(exc.value)

    def test_question_templates_are_validated(self) -> None:
        """A bad reference must fail at validate time, not in front of a human."""
        agent = _questions(
            questions=[QuestionDef(text="{{ nonexistent.output.x }}")],
            routes=[RouteDef(to="after")],
        )

        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(self._config(agent))

        assert "nonexistent" in str(exc.value)
