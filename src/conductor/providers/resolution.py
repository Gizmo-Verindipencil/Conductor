"""Provider resolution helpers that do not import provider implementations."""

from __future__ import annotations

from collections.abc import Iterable

from conductor.config.schema import AgentDef, WorkflowConfig


def provider_type_for_agent(agent: AgentDef, default: str) -> str:
    """Resolve an agent override first, then the workflow default provider."""
    return agent.provider or default


def iter_provider_backed_agents(config: WorkflowConfig) -> Iterable[AgentDef]:
    """Yield every top-level and inline for-each provider-backed agent."""
    for step in config.agents:
        if isinstance(step, AgentDef):
            yield step
    for group in config.for_each:
        if isinstance(group.agent, AgentDef):
            yield group.agent


def effective_mcp_consumer_providers(config: WorkflowConfig) -> frozenset[str]:
    """Return the effective providers of agents that consume workflow MCP servers."""
    default = config.workflow.runtime.provider.name
    return frozenset(
        provider_type_for_agent(agent, default) for agent in iter_provider_backed_agents(config)
    )


def format_remote_mcp_stdio_only_error(
    server_name: str,
    transport: str,
    providers: Iterable[str],
) -> str:
    """Format the error for remote MCP transports used by stdio-only providers."""
    unsupported = sorted(providers)
    if len(unsupported) == 1:
        subject = f"provider '{unsupported[0]}'"
        verb = "supports"
    else:
        subject = f"providers {unsupported!r}"
        verb = "support"
    return (
        f"MCP server '{server_name}' uses remote transport '{transport}', but {subject} "
        f"{verb} only stdio MCP."
    )


def format_claude_agent_sdk_remote_env_error(
    server_name: str,
    transport: str,
    secret_ref: str,
) -> str:
    """Format the unsupported Claude Agent SDK remote env-delivery error."""
    return (
        f"MCP server '{server_name}' uses transport '{transport}' with env delivery for secret "
        f"'{secret_ref}', but the claude-agent-sdk provider cannot deliver environment variables "
        "to remote MCP servers (its remote config shape accepts only url and headers)."
    )
