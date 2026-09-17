"""Tests for static ``type: mcp`` step validation in ``config/validator.py``.

Covers the early off-network diagnostics that ``conductor validate`` runs:
- The step's ``server`` must be declared in ``workflow.runtime.mcp_servers``.
- The server's ``tools`` filter must allow the step's ``tool``.
- Only stdio servers are supported (http/sse is not implemented yet).
- ``arguments`` templates are collected recursively, so references to
  unknown steps or same-parallel-group members fail at validate-time.
- Explicit context mode must not emit a spurious ``workflow.input`` warning
  for mcp steps (they read rendered arguments, not declared inputs).
- Inline for-each mcp agents (absent from ``config.agents``) are validated
  too, with errors naming the enclosing for-each group.
"""

from __future__ import annotations

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    InputDef,
    MCPServerDef,
    MCPStepDef,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


def _make_workflow(
    *agents: AgentDef,
    entry_point: str | None = None,
    mcp_servers: dict[str, MCPServerDef] | None = None,
    parallel: list[ParallelGroup] | None = None,
    for_each: list[ForEachDef] | None = None,
    context: ContextConfig | None = None,
    inputs: dict[str, InputDef] | None = None,
) -> WorkflowConfig:
    """Build a minimal WorkflowConfig carrying the given mcp server table."""
    names = (
        [a.name for a in agents]
        + [p.name for p in parallel or []]
        + [f.name for f in for_each or []]
    )
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="mcp-step-test",
            entry_point=entry_point or names[0],
            runtime=RuntimeConfig(provider="copilot", mcp_servers=mcp_servers or {}),
            context=context or ContextConfig(),
            input=inputs or {},
        ),
        agents=list(agents),
        parallel=parallel or [],
        for_each=for_each or [],
    )


def _mcp_agent(
    name: str = "m",
    server: str = "srv",
    tool: str = "do_thing",
    arguments: dict[str, object] | None = None,
) -> AgentDef:
    return MCPStepDef(
        name=name,
        server=server,
        tool=tool,
        arguments=arguments,
    )


class TestValidMcpStep:
    def test_stdio_server_with_star_allowlist_passes(self) -> None:
        # Requirement: an mcp step against a stdio server with tools ["*"] validates clean.
        config = _make_workflow(
            _mcp_agent(),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server")},
        )
        assert validate_workflow_config(config) == []

    def test_tool_in_explicit_allowlist_passes(self) -> None:
        # Requirement: exact membership in the server's tools list allows the tool.
        config = _make_workflow(
            _mcp_agent(tool="get_issue"),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server", tools=["get_issue"])},
        )
        assert validate_workflow_config(config) == []

    def test_nested_arguments_pass(self) -> None:
        # Requirement: nested dict/list argument templates are collected without false positives.
        config = _make_workflow(
            _mcp_agent(arguments={"opts": {"labels": ["a", "{{ workflow.input.x }}"]}}),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server")},
            inputs={"x": InputDef(type="string")},
        )
        assert validate_workflow_config(config) == []


class TestUnknownServer:
    def test_unknown_server_errors_and_lists_available(self) -> None:
        # Requirement: a server not declared in runtime.mcp_servers is an error naming both.
        config = _make_workflow(
            _mcp_agent(server="nope"),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server")},
        )
        with pytest.raises(ConfigurationError, match="unknown MCP server 'nope'"):
            validate_workflow_config(config)

    def test_no_servers_declared(self) -> None:
        # Requirement: the error message stays useful when no servers are declared at all.
        config = _make_workflow(_mcp_agent())
        with pytest.raises(ConfigurationError, match="unknown MCP server 'srv'"):
            validate_workflow_config(config)


class TestToolAllowlist:
    def test_tool_outside_allowlist_errors(self) -> None:
        # Requirement: a tool not in the server's tools filter (and no "*") is an error.
        config = _make_workflow(
            _mcp_agent(tool="delete_everything"),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server", tools=["get_issue"])},
        )
        with pytest.raises(ConfigurationError, match="not allowed by server 'srv'"):
            validate_workflow_config(config)

    def test_unknown_server_skips_allowlist_check(self) -> None:
        # Requirement: an unknown server reports one error, not a cascade.
        config = _make_workflow(
            _mcp_agent(server="nope", tool="delete_everything"),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server", tools=["get_issue"])},
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "not allowed by server" not in str(exc_info.value)

    def test_singleton_wildcard_allows_any_tool(self) -> None:
        # Requirement: ["*"] means all tools (schema docstring contract).
        config = _make_workflow(
            _mcp_agent(tool="anything_at_all"),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server", tools=["*"])},
        )
        assert validate_workflow_config(config) == []

    def test_mixed_wildcard_list_allows_any_tool(self) -> None:
        # Requirement: wildcard MEMBERSHIP is the rule at both boundaries —
        # ["*", "health"] is accepted here exactly when the runtime check in
        # engine/workflow.py::_run_mcp_step accepts it (shared rule).
        config = _make_workflow(
            _mcp_agent(tool="anything_at_all"),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server", tools=["*", "health"])},
        )
        assert validate_workflow_config(config) == []

    def test_empty_tools_list_allows_nothing(self) -> None:
        # Requirement: an explicitly empty allowlist permits no tool.
        config = _make_workflow(
            _mcp_agent(tool="anything_at_all"),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server", tools=[])},
        )
        with pytest.raises(ConfigurationError, match="not allowed by server 'srv'"):
            validate_workflow_config(config)


class TestArgumentTemplateSyntax:
    def test_malformed_template_in_arguments_errors(self) -> None:
        # Requirement: a syntax-broken argument template fails at validate
        # time with the step and nested path named — reference analysis
        # (_extract_template_refs) deliberately swallows TemplateSyntaxError,
        # so without the explicit parse this only failed at execution.
        config = _make_workflow(
            _mcp_agent(arguments={"path": "{{ workflow.input.foo"}),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server")},
            inputs={"foo": InputDef(type="string")},
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "invalid Jinja2 template syntax" in str(exc_info.value)
        assert "arguments.path" in str(exc_info.value)

    def test_malformed_template_nested_in_list_errors(self) -> None:
        # Requirement: the syntax check walks lists too, not just mappings.
        config = _make_workflow(
            _mcp_agent(arguments={"opts": {"labels": ["ok", "{% if x"]}}),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server")},
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "invalid Jinja2 template syntax" in str(exc_info.value)
        assert "arguments.opts.labels[1]" in str(exc_info.value)

    def test_valid_nested_templates_still_pass(self) -> None:
        # Requirement: well-formed nested templates produce no syntax error.
        config = _make_workflow(
            _mcp_agent(arguments={"opts": {"labels": ["a", "{{ workflow.input.x }}"], "n": 3}}),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server")},
            inputs={"x": InputDef(type="string")},
        )
        assert validate_workflow_config(config) == []


class TestTransport:
    def test_http_server_errors(self) -> None:
        # Requirement: type: mcp supports stdio servers only; http is rejected with a clear message.
        config = _make_workflow(
            _mcp_agent(),
            mcp_servers={"srv": MCPServerDef(type="http", url="http://localhost:8080/mcp")},
        )
        with pytest.raises(
            ConfigurationError,
            match=r"stdio servers only \(got 'http'\); http/sse support is not implemented yet",
        ):
            validate_workflow_config(config)

    def test_sse_server_errors(self) -> None:
        # Requirement: sse transport is rejected the same way as http.
        config = _make_workflow(
            _mcp_agent(),
            mcp_servers={"srv": MCPServerDef(type="sse", url="http://localhost:8080/sse")},
        )
        with pytest.raises(ConfigurationError, match=r"got 'sse'"):
            validate_workflow_config(config)


class TestArgumentsTemplateReferences:
    def test_unknown_step_reference_in_arguments_errors(self) -> None:
        # Requirement: arguments templates are validated, so a reference to an
        # unknown step fails at validate-time with the arguments label.
        config = _make_workflow(
            _mcp_agent(arguments={"query": "{{ ghost.output.x }}"}),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server")},
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        message = str(exc_info.value)
        assert "arguments.query" in message
        assert "unknown agent 'ghost'" in message

    def test_same_parallel_group_reference_in_arguments_errors(self) -> None:
        # Requirement: an mcp step in a parallel group cannot reference another
        # member of the same group via arguments (pre-group snapshot semantics).
        config = _make_workflow(
            _mcp_agent(name="m", arguments={"q": "{{ m2.output.x }}"}),
            _mcp_agent(name="m2"),
            entry_point="pg",
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server")},
            parallel=[
                ParallelGroup(
                    name="pg",
                    agents=["m", "m2"],
                )
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        message = str(exc_info.value)
        assert "arguments.q" in message
        assert "same parallel group 'pg'" in message

    def test_explicit_mode_workflow_input_reference_does_not_warn(self) -> None:
        # Requirement: mcp steps read rendered arguments (not declared inputs),
        # so a workflow.input reference must not trigger the explicit-mode warning.
        config = _make_workflow(
            _mcp_agent(arguments={"q": "{{ workflow.input.topic }}"}),
            mcp_servers={"srv": MCPServerDef(command="my-mcp-server")},
            context=ContextConfig(mode="explicit"),
            inputs={"topic": InputDef(type="string")},
        )
        assert validate_workflow_config(config) == []


class TestInlineForEachAgent:
    def _for_each_workflow(
        self, agent: AgentDef, servers: dict[str, MCPServerDef]
    ) -> WorkflowConfig:
        return _make_workflow(
            entry_point="fans",
            mcp_servers=servers,
            for_each=[
                ForEachDef(
                    name="fans",
                    type="for_each",
                    source="workflow.input.items",
                    **{"as": "item"},
                    agent=agent,
                    routes=[RouteDef(to="$end")],
                )
            ],
            inputs={"items": InputDef(type="array")},
        )

    def test_unknown_server_errors_naming_the_group(self) -> None:
        # Requirement: inline for-each mcp agents (absent from config.agents) are
        # validated, and the error names the enclosing for-each group.
        config = self._for_each_workflow(
            _mcp_agent(server="nope"),
            {"srv": MCPServerDef(command="my-mcp-server")},
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        message = str(exc_info.value)
        assert "unknown MCP server 'nope'" in message
        assert "for-each group 'fans'" in message

    def test_http_transport_errors_naming_the_group(self) -> None:
        # Requirement: transport restrictions apply to inline for-each agents too.
        config = self._for_each_workflow(
            _mcp_agent(),
            {"srv": MCPServerDef(type="http", url="http://localhost:8080/mcp")},
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        message = str(exc_info.value)
        assert "stdio servers only" in message
        assert "for-each group 'fans'" in message

    def test_tool_outside_allowlist_errors_naming_the_group(self) -> None:
        # Requirement: the tools-filter restriction applies to inline for-each agents too.
        config = self._for_each_workflow(
            _mcp_agent(tool="delete_everything"),
            {"srv": MCPServerDef(command="my-mcp-server", tools=["get_issue"])},
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        message = str(exc_info.value)
        assert "not allowed by server 'srv'" in message
        assert "for-each group 'fans'" in message
