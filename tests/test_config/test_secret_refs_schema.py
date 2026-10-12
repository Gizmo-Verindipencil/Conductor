"""Tests for workflow-side secret reference schema (execution.secrets, MCPServerDef.secrets).

Covers:
- SecretDelivery model validation (env vs header, RFC 9110 token charset, extra="forbid").
- StepSecretRef model validation (ref charset, scopes: script/mcp/agent, extra="forbid").
- StepExecutionConfig.secrets defaults and validation.
- MCPServerDef.secrets defaults and validation.
- End-to-end YAML parsing of script steps and MCP servers with secret references.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import (
    MCPServerDef,
    ScriptStepDef,
    SecretDelivery,
    StepExecutionConfig,
    StepSecretRef,
)


class TestSecretDelivery:
    """Tests for SecretDelivery model."""

    def test_env_delivery(self) -> None:
        # Secret delivery via environment variable with valid identifier.
        delivery = SecretDelivery(env="MY_API_KEY")
        assert delivery.env == "MY_API_KEY"
        assert delivery.header is None

    def test_header_delivery(self) -> None:
        # Secret delivery via HTTP header with RFC 9110 compliant token name.
        delivery = SecretDelivery(header="Authorization")
        assert delivery.header == "Authorization"
        assert delivery.env is None

    def test_both_env_and_header_rejected(self) -> None:
        # Exactly one of env or header must be set; both set is rejected.
        with pytest.raises(ValidationError, match="exactly one of 'env' or 'header' must be set"):
            SecretDelivery(env="TOKEN", header="Authorization")

    def test_neither_env_nor_header_rejected(self) -> None:
        # Exactly one of env or header must be set; neither set is rejected.
        with pytest.raises(ValidationError, match="exactly one of 'env' or 'header' must be set"):
            SecretDelivery()

    @pytest.mark.parametrize("invalid_env", ["123VAR", "VAR-NAME", "VAR.NAME", "VAR NAME", ""])
    def test_invalid_charset_env_rejected(self, invalid_env: str) -> None:
        # Environment variable name must match [A-Za-z_][A-Za-z0-9_]*.
        with pytest.raises(ValidationError):
            SecretDelivery(env=invalid_env)

    @pytest.mark.parametrize(
        "invalid_header",
        ["Invalid Header", "Header:Value", "Header@Name", "Header/Name", ""],
    )
    def test_invalid_charset_header_rejected(self, invalid_header: str) -> None:
        # HTTP header name must match RFC 9110 token charset.
        with pytest.raises(ValidationError):
            SecretDelivery(header=invalid_header)

    def test_extra_key_on_delivery_rejected(self) -> None:
        # SecretDelivery rejects extra unrecognised fields (extra="forbid").
        with pytest.raises(ValidationError):
            SecretDelivery(env="TOKEN", extra_field="bad")  # type: ignore[call-arg]


class TestStepSecretRef:
    """Tests for StepSecretRef model."""

    @pytest.mark.parametrize("scope", ["script", "mcp", "agent"])
    def test_all_valid_scopes_parse(self, scope: str) -> None:
        # All three scopes (script, mcp, agent) parse successfully at schema level.
        secret_ref = StepSecretRef(
            ref="my_secret",
            scope=scope,  # type: ignore[arg-type]
            delivery=SecretDelivery(env="MY_SECRET_ENV"),
        )
        assert secret_ref.ref == "my_secret"
        assert secret_ref.scope == scope
        assert secret_ref.delivery.env == "MY_SECRET_ENV"

    def test_scope_agent_parses_successfully(self) -> None:
        # scope: agent MUST parse at schema level (deferred validation happens in validator).
        secret_ref = StepSecretRef(
            ref="agent_token",
            scope="agent",
            delivery=SecretDelivery(header="X-Agent-Auth"),
        )
        assert secret_ref.scope == "agent"
        assert secret_ref.delivery.header == "X-Agent-Auth"

    def test_scope_script_parses_successfully(self) -> None:
        # scope: script parses successfully with env delivery.
        secret_ref = StepSecretRef(
            ref="db_password",
            scope="script",
            delivery=SecretDelivery(env="DB_PASS"),
        )
        assert secret_ref.scope == "script"

    def test_scope_mcp_parses_successfully(self) -> None:
        # scope: mcp parses successfully with header delivery.
        secret_ref = StepSecretRef(
            ref="mcp_token",
            scope="mcp",
            delivery=SecretDelivery(header="Authorization"),
        )
        assert secret_ref.scope == "mcp"

    def test_invalid_scope_rejected(self) -> None:
        # Unrecognised scope is rejected by Literal type.
        with pytest.raises(ValidationError):
            StepSecretRef(
                ref="secret",
                scope="invalid_scope",  # type: ignore[arg-type]
                delivery=SecretDelivery(env="TOKEN"),
            )

    @pytest.mark.parametrize("invalid_ref", ["secret/path", "secret name", "secret@domain", ""])
    def test_invalid_charset_ref_rejected(self, invalid_ref: str) -> None:
        # Secret reference name must match [A-Za-z0-9_.-]+.
        with pytest.raises(ValidationError):
            StepSecretRef(
                ref=invalid_ref,
                scope="script",
                delivery=SecretDelivery(env="TOKEN"),
            )

    def test_extra_key_on_secret_ref_rejected(self) -> None:
        # StepSecretRef rejects extra unrecognised fields (extra="forbid").
        with pytest.raises(ValidationError):
            StepSecretRef(
                ref="my_secret",
                scope="script",
                delivery=SecretDelivery(env="TOKEN"),
                unexpected="extra",  # type: ignore[call-arg]
            )


class TestStepExecutionConfigSecrets:
    """Tests for StepExecutionConfig.secrets field."""

    def test_absent_secrets_defaults_to_empty_list(self) -> None:
        # An omitted secrets field in StepExecutionConfig defaults to an empty list.
        config = StepExecutionConfig()
        assert config.secrets == []
        assert config.profile is None

    def test_full_ref_on_script_step(self) -> None:
        # Full secret reference on a script step parses into StepExecutionConfig.
        step = ScriptStepDef(
            name="run_tests",
            command="pytest",
            execution=StepExecutionConfig(
                profile="docker",
                secrets=[
                    StepSecretRef(
                        ref="api_key",
                        scope="script",
                        delivery=SecretDelivery(env="API_KEY"),
                    )
                ],
            ),
        )
        assert step.execution is not None
        assert len(step.execution.secrets) == 1
        assert step.execution.secrets[0].ref == "api_key"
        assert step.execution.secrets[0].scope == "script"
        assert step.execution.secrets[0].delivery.env == "API_KEY"

    def test_extra_key_on_step_execution_config_rejected(self) -> None:
        # StepExecutionConfig rejects extra unrecognised fields (extra="forbid").
        with pytest.raises(ValidationError):
            StepExecutionConfig(profile="default", extra_field="bad")  # type: ignore[call-arg]


class TestMCPServerDefSecrets:
    """Tests for MCPServerDef.secrets field."""

    def test_absent_secrets_defaults_to_empty_list(self) -> None:
        # An omitted secrets field in MCPServerDef defaults to an empty list.
        server = MCPServerDef(type="stdio", command="mcp-server")
        assert server.secrets == []

    def test_full_ref_on_mcp_server(self) -> None:
        # Full secret reference on an HTTP MCP server with header delivery.
        server = MCPServerDef(
            type="http",
            url="https://mcp.example.com",
            secrets=[
                StepSecretRef(
                    ref="gh_token",
                    scope="mcp",
                    delivery=SecretDelivery(header="Authorization"),
                )
            ],
        )
        assert len(server.secrets) == 1
        assert server.secrets[0].ref == "gh_token"
        assert server.secrets[0].scope == "mcp"
        assert server.secrets[0].delivery.header == "Authorization"

    def test_mcp_server_with_env_delivery(self) -> None:
        # MCP server with stdio command and secret env delivery.
        server = MCPServerDef(
            type="stdio",
            command="gh-mcp",
            secrets=[
                StepSecretRef(
                    ref="github_token",
                    scope="mcp",
                    delivery=SecretDelivery(env="GITHUB_TOKEN"),
                )
            ],
        )
        assert len(server.secrets) == 1
        assert server.secrets[0].delivery.env == "GITHUB_TOKEN"


class TestWorkflowDefaultsSecrets:
    """Tests for the ``workflow.defaults.execution`` position of StepExecutionConfig."""

    def test_defaults_execution_rejects_secret_references(self) -> None:
        # Requirement (review r2): StepExecutionConfig backs both step-level
        # ``execution:`` blocks and ``workflow.defaults.execution``, but
        # secret use-sites resolve per executable step — validation, manifest
        # compilation, and indexing never inspect the defaults location, so a
        # reference there would parse yet produce no delivery and no audit
        # row. It is rejected at the schema until inheritance is designed.
        from conductor.config.loader import load_config_string
        from conductor.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError, match="not supported"):
            load_config_string(
                """\
                workflow:
                  name: defaults-secrets
                  entry_point: run
                  defaults:
                    execution:
                      secrets:
                        - ref: token
                          scope: script
                          delivery:
                            env: TOKEN
                agents:
                  - name: run
                    type: script
                    command: echo ok
                    routes:
                      - to: $end
                """
            )

    def test_defaults_execution_profile_without_secrets_still_valid(self) -> None:
        # Requirement: the defaults block keeps its long-standing profile
        # role — only secret references are rejected there.
        from conductor.config.loader import load_config_string

        config = load_config_string(
            """\
            workflow:
              name: defaults-profile
              entry_point: run
              defaults:
                execution:
                  profile: shell
            agents:
              - name: run
                type: script
                command: echo ok
                routes:
                  - to: $end
            """
        )
        assert config.workflow.defaults.execution is not None
        assert config.workflow.defaults.execution.profile == "shell"
        assert config.workflow.defaults.execution.secrets == []


class TestWorkflowConfigSecretRefsParsing:
    """Tests for full WorkflowConfig parsing of steps and MCP servers with secret references."""

    def test_workflow_with_script_step_secrets(self) -> None:
        # WorkflowConfig parses script steps containing execution.secrets.
        from conductor.config.loader import load_config_string

        yaml_content = """
workflow:
  name: test-secrets-workflow
  entry_point: run_script

agents:
  - name: run_script
    type: script
    command: pytest
    execution:
      profile: isolated
      secrets:
        - ref: db_credential
          scope: script
          delivery:
            env: DB_PASSWORD
    routes:
      - to: $end
"""
        config = load_config_string(yaml_content)
        assert len(config.agents) == 1
        script_step = config.agents[0]
        assert isinstance(script_step, ScriptStepDef)
        assert script_step.execution is not None
        assert script_step.execution.profile == "isolated"
        assert len(script_step.execution.secrets) == 1
        assert script_step.execution.secrets[0].ref == "db_credential"
        assert script_step.execution.secrets[0].delivery.env == "DB_PASSWORD"

    def test_workflow_with_mcp_server_secrets(self) -> None:
        # WorkflowConfig parses runtime.mcp_servers containing secrets.
        from conductor.config.loader import load_config_string

        yaml_content = """
workflow:
  name: test-mcp-secrets-workflow
  entry_point: answerer
  runtime:
    mcp_servers:
      remote_service:
        type: http
        url: https://mcp.internal.net
        secrets:
          - ref: service_bearer
            scope: mcp
            delivery:
              header: Authorization

agents:
  - name: answerer
    prompt: "Hello"
    routes:
      - to: $end
"""
        config = load_config_string(yaml_content)
        mcp_servers = config.workflow.runtime.mcp_servers
        assert "remote_service" in mcp_servers
        server = mcp_servers["remote_service"]
        assert len(server.secrets) == 1
        assert server.secrets[0].ref == "service_bearer"
        assert server.secrets[0].delivery.header == "Authorization"
