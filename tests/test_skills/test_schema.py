"""Tests for the ``skills`` field on :class:`AgentDef` and :class:`RuntimeConfig`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import (
    AgentDef,
    HumanGateStepDef,
    RuntimeConfig,
    ScriptStepDef,
    SetStepDef,
    SkillDiscoveryConfig,
    TerminateStepDef,
    WaitStepDef,
    WorkflowStepDef,
)


def _assert_extra_forbidden(exc_info: pytest.ExceptionInfo[ValidationError], field: str) -> None:
    """Since issue #517 a sibling-variant field is rejected by the concrete
    step model's ``extra="forbid"``, not by a custom message — so assert the
    standard ``extra_forbidden`` error structurally."""
    assert any(
        e["loc"] == (field,) and e["type"] == "extra_forbidden" for e in exc_info.value.errors()
    )


class TestAgentDefSkills:
    def test_defaults_to_none(self) -> None:
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello")
        assert agent.skills is None

    def test_empty_list_means_opt_out(self) -> None:
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello", skills=[])
        assert agent.skills == []

    def test_explicit_list(self) -> None:
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello", skills=["conductor"])
        assert agent.skills == ["conductor"]

    def test_unknown_skill_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unknown skill"):
            AgentDef(name="a", model="gpt-4", prompt="Hello", skills=["does-not-exist"])

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty strings"):
            AgentDef(name="a", model="gpt-4", prompt="Hello", skills=[""])

    def test_forbidden_on_script_agent(self) -> None:
        # Issue #517: sibling-variant fields are rejected by extra="forbid",
        # not by a custom "<type> agents cannot have 'skills'" message.
        with pytest.raises(ValidationError) as exc_info:
            ScriptStepDef.model_validate(
                {"name": "s", "command": "echo hi", "skills": ["conductor"]}
            )
        _assert_extra_forbidden(exc_info, "skills")

    def test_forbidden_on_workflow_agent(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            WorkflowStepDef.model_validate(
                {"name": "w", "workflow": "sub.yaml", "skills": ["conductor"]}
            )
        _assert_extra_forbidden(exc_info, "skills")

    def test_forbidden_on_human_gate(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            HumanGateStepDef.model_validate(
                {
                    "name": "g",
                    "prompt": "Choose:",
                    "options": [{"label": "Yes", "value": "y", "route": "next"}],
                    "skills": ["conductor"],
                }
            )
        _assert_extra_forbidden(exc_info, "skills")

    def test_forbidden_on_wait_agent(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            WaitStepDef.model_validate({"name": "w", "duration": "1s", "skills": ["conductor"]})
        _assert_extra_forbidden(exc_info, "skills")

    def test_forbidden_on_set_agent(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            SetStepDef.model_validate({"name": "s", "value": "hello", "skills": ["conductor"]})
        _assert_extra_forbidden(exc_info, "skills")

    def test_forbidden_on_terminate_agent(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TerminateStepDef.model_validate(
                {
                    "name": "t",
                    "status": "success",
                    "reason": "done",
                    "skills": ["conductor"],
                }
            )
        _assert_extra_forbidden(exc_info, "skills")

    def test_allowed_on_default_type_agent(self) -> None:
        agent = AgentDef(name="r", model="gpt-4", prompt="p", skills=["conductor"])
        assert agent.skills == ["conductor"]
        assert agent.type == "agent"

    def test_allowed_on_explicit_agent_type(self) -> None:
        agent = AgentDef(name="r", type="agent", model="gpt-4", prompt="p", skills=["conductor"])
        assert agent.skills == ["conductor"]


class TestRuntimeConfigSkills:
    def test_defaults_to_empty_list(self) -> None:
        config = RuntimeConfig()
        assert config.skills == []

    def test_can_be_set(self) -> None:
        config = RuntimeConfig(skills=["conductor"])
        assert config.skills == ["conductor"]

    def test_unknown_skill_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unknown skill"):
            RuntimeConfig(skills=["does-not-exist"])

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty strings"):
            RuntimeConfig(skills=[""])


class TestPathEntriesAtSchemaLevel:
    """Path entries (issue #350) need the workflow file's directory to
    resolve, which the schema does not have — so they are shape-checked
    here and resolved in ``conductor validate`` / ``AgentExecutor``.

    Bare *names* keep their eager check, so an unknown built-in still fails
    at load time exactly as it did before paths existed.
    """

    @pytest.mark.parametrize(
        "entry",
        ["./team-skills/acme", "../shared/acme", "~/scratch/skills", "/abs/acme", "team/acme"],
    )
    def test_path_entries_are_accepted_unresolved(self, entry: str) -> None:
        assert AgentDef(name="r", prompt="p", skills=[entry]).skills == [entry]
        assert RuntimeConfig(skills=[entry]).skills == [entry]

    def test_unknown_bare_name_still_fails_at_load_time(self) -> None:
        """The pre-existing error timing must not regress: a typo'd built-in
        name needs no base directory to detect."""
        with pytest.raises(ValidationError, match="Unknown skill"):
            AgentDef(name="r", prompt="p", skills=["conductorr"])

    def test_unknown_name_error_mentions_the_path_form(self) -> None:
        with pytest.raises(ValidationError, match=r"\./team-skills/my-skill"):
            AgentDef(name="r", prompt="p", skills=["nope"])

    def test_names_and_paths_can_be_mixed(self) -> None:
        entries = ["conductor", "./team-skills/acme"]
        assert AgentDef(name="r", prompt="p", skills=entries).skills == entries

    def test_whitespace_only_entry_still_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty strings"):
            AgentDef(name="r", prompt="p", skills=["   "])

    def test_path_entries_still_forbidden_on_non_provider_steps(self) -> None:
        # A path entry is still a skills entry: it cannot reach a script step
        # either, and #517 routes that through extra="forbid".
        with pytest.raises(ValidationError) as exc_info:
            ScriptStepDef.model_validate({"name": "s", "command": "echo hi", "skills": ["./a/b"]})
        _assert_extra_forbidden(exc_info, "skills")


class TestSkillDiscoveryConfig:
    """``runtime.skill_discovery`` — off unless asked for."""

    def test_defaults_to_disabled(self) -> None:
        config = RuntimeConfig()
        assert config.skill_discovery.sources == ()
        assert config.skill_discovery.is_enabled is False

    def test_enabled_when_a_source_is_set(self) -> None:
        config = RuntimeConfig(skill_discovery={"sources": ["personal"]})
        assert config.skill_discovery.is_enabled is True

    @pytest.mark.parametrize("source", ["personal", "project"])
    def test_known_sources_accepted(self, source: str) -> None:
        assert SkillDiscoveryConfig(sources=[source]).sources == (source,)

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SkillDiscoveryConfig(sources=["everywhere"])

    def test_plugins_source_rejected(self) -> None:
        # Removed with issue #378: it took a plugin's skills/ and dropped
        # its subagents and MCP servers. `runtime.plugins` replaces it.
        with pytest.raises(ValidationError):
            SkillDiscoveryConfig(sources=["plugins"])

    def test_duplicate_source_rejected(self) -> None:
        # Listing one twice has no effect, so it always means the author
        # believed it would.
        with pytest.raises(ValidationError, match="duplicate entries"):
            SkillDiscoveryConfig(sources=["personal", "personal"])

    def test_blank_exclude_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty skill names"):
            SkillDiscoveryConfig(exclude=["  "])

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SkillDiscoveryConfig(source=["personal"])

    def test_frozen(self) -> None:
        # Same reason ``SkillInjectionConfig`` is frozen: the field
        # validators do not re-fire on per-attribute assignment.
        config = SkillDiscoveryConfig(sources=["personal"])
        with pytest.raises(ValidationError):
            config.sources = ["plugins"]

    def test_sources_are_immutable_and_hashable(self) -> None:
        # Tuples, not lists: ``frozen=True`` would otherwise still allow
        # ``config.sources.append(...)``, and Pydantic generates a
        # ``__hash__`` that a list field makes raise.
        config = SkillDiscoveryConfig(sources=["personal"], exclude=["x"])
        assert isinstance(config.sources, tuple)
        assert isinstance(config.exclude, tuple)
        assert hash(config) == hash(SkillDiscoveryConfig(sources=["personal"], exclude=["x"]))

    def test_exclude_does_not_enable_discovery(self) -> None:
        # An exclude list on its own is inert, not an implicit opt-in.
        assert SkillDiscoveryConfig(exclude=["a"]).is_enabled is False
