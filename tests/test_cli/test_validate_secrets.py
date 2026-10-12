"""``conductor validate`` secret-binding report and three-level cross-checks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()

_SOURCE_VAR = "CONDUCTOR_TEST_TASK10_SOURCE"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    return root


def _write_environment(
    root: Path,
    name: str,
    *,
    secret_names: tuple[str, ...] = ("token",),
    allow: str | None = "[script, mcp]",
) -> Path:
    path = root / ".conductor" / "environments" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["default: local", "profiles:", "  local:", "    backend: local"]
    if secret_names:
        lines.append("secrets:")
        for secret_name in secret_names:
            lines.extend(
                [
                    f"  {secret_name}:",
                    "    source:",
                    f"      env: {_SOURCE_VAR}",
                ]
            )
            if allow is not None:
                lines.append(f"    allow: {allow}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_secret_workflow(root: Path) -> Path:
    path = root / "workflow.yaml"
    path.write_text(
        """\
workflow:
  name: secrets-demo
  entry_point: run
agents:
  - name: run
    type: script
    command: echo
    args: [ok]
    execution:
      secrets:
        - ref: token
          scope: script
          delivery:
            env: INJECTED_TOKEN
    routes:
      - to: $end
output:
  result: "{{ run.output.stdout }}"
""",
        encoding="utf-8",
    )
    return path


def _write_mcp_secret_workflow(root: Path) -> Path:
    path = root / "workflow.yaml"
    path.write_text(
        """\
workflow:
  name: mcp-secrets-demo
  entry_point: run
  runtime:
    mcp_servers:
      api:
        type: http
        url: https://example.test/mcp
        secrets:
          - ref: token
            scope: mcp
            delivery:
              header: Authorization
agents:
  - name: run
    type: script
    command: echo
    args: [ok]
    routes:
      - to: $end
output:
  result: "{{ run.output.stdout }}"
""",
        encoding="utf-8",
    )
    return path


def _write_plain_workflow(root: Path) -> Path:
    path = root / "workflow.yaml"
    path.write_text(
        """\
workflow:
  name: plain
  entry_point: run
agents:
  - name: run
    type: script
    command: echo
    args: [ok]
    routes:
      - to: $end
output:
  result: "{{ run.output.stdout }}"
""",
        encoding="utf-8",
    )
    return path


def _invoke_validate(path: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["validate", str(path), *args])
    return result.exit_code, result.output


def _flatten(output: str) -> str:
    """Collapse panel borders and whitespace so Rich wrapping cannot break matches."""
    return " ".join(output.replace("│", " ").split())


class TestSecretBindingsReport:
    """The Secret Bindings section under an explicit ``--environment``."""

    def test_report_renders_bindings_and_consumers_without_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: --environment renders the bindings (Name/Source/Allow) and
        # consumers (Consumer/Ref/Scope/Delivery) tables, disclosing neither
        # secret values nor source variable names.
        monkeypatch.setenv(_SOURCE_VAR, "x")
        root = _repo(tmp_path)
        _write_environment(root, "demo")
        workflow_path = _write_secret_workflow(root)

        exit_code, output = _invoke_validate(workflow_path, "--environment", "demo")

        assert exit_code == 0
        flattened = _flatten(output)
        assert "Secret Bindings" in output
        assert "token" in flattened
        assert "script, mcp" in flattened
        assert "env: INJECTED_TOKEN" in flattened
        assert _SOURCE_VAR not in output

    def test_report_marks_unset_source_without_naming_the_variable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: an unset source variable produces a validator warning and
        # an ``(unset)`` marker in the bindings table, never the variable name.
        monkeypatch.delenv(_SOURCE_VAR, raising=False)
        root = _repo(tmp_path)
        _write_environment(root, "demo")
        workflow_path = _write_secret_workflow(root)

        exit_code, output = _invoke_validate(workflow_path, "--environment", "demo")

        assert exit_code == 0
        flattened = _flatten(output)
        assert "unset" in flattened
        assert "env (unset)" in flattened
        assert _SOURCE_VAR not in output

    def test_report_includes_mcp_server_consumers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: MCP server consumers render with their ``mcp:<name>``
        # label, mcp scope, and header delivery.
        monkeypatch.setenv(_SOURCE_VAR, "x")
        root = _repo(tmp_path)
        _write_environment(root, "demo")
        workflow_path = _write_mcp_secret_workflow(root)

        exit_code, output = _invoke_validate(workflow_path, "--environment", "demo")

        assert exit_code == 0
        flattened = _flatten(output)
        assert "Secret Bindings" in output
        assert "mcp:api" in flattened
        assert "header: Authorization" in flattened
        assert _SOURCE_VAR not in output

    def test_secret_free_workflow_skips_the_report(self, tmp_path: Path) -> None:
        # Requirement: the lazy gate skips the report entirely for a workflow
        # with no secret references, even under --environment.
        root = _repo(tmp_path)
        _write_environment(root, "demo")
        workflow_path = _write_plain_workflow(root)

        with patch("conductor.cli.validate._report_secret_bindings") as report:
            exit_code, output = _invoke_validate(workflow_path, "--environment", "demo")

        assert exit_code == 0
        report.assert_not_called()
        assert "Secret Bindings" not in output


class TestAllowAndCompileErrors:
    """Explicit-mode failures: validator allow check and manifest compile."""

    def test_allow_violation_fails_naming_the_allow_list(self, tmp_path: Path) -> None:
        # Requirement: a consumer outside the binding's allow list fails
        # validation, naming the allow list.
        root = _repo(tmp_path)
        _write_environment(root, "strict", allow="[mcp]")
        workflow_path = _write_secret_workflow(root)

        exit_code, output = _invoke_validate(workflow_path, "--environment", "strict")

        assert exit_code == 1
        assert "binding allow list is [mcp]" in _flatten(output)

    def test_unknown_ref_fails_through_manifest_compile(self, tmp_path: Path) -> None:
        # Requirement: under --environment an unknown ref is reported by manifest
        # compilation, which runs before semantic validation.
        root = _repo(tmp_path)
        _write_environment(root, "demo", secret_names=("other",))
        workflow_path = _write_secret_workflow(root)

        exit_code, output = _invoke_validate(workflow_path, "--environment", "demo")

        assert exit_code == 1
        flattened = _flatten(output)
        assert "is not defined in environment" in flattened
        assert "token" in flattened


class TestBareValidate:
    """Bare ``conductor validate``: advisory ambient checks, zero new noise."""

    def test_bare_validate_with_refs_warns_and_prints_no_report(self, tmp_path: Path) -> None:
        # Requirement: with references but no authored environments, bare
        # validate emits only the warning pointing at --environment.
        root = _repo(tmp_path)
        workflow_path = _write_secret_workflow(root)

        exit_code, output = _invoke_validate(workflow_path)

        assert exit_code == 0
        flattened = _flatten(output)
        assert "run conductor validate --environment" in flattened
        assert "Secret Bindings" not in output

    def test_bare_validate_secret_free_is_byte_identical_with_no_io(self, tmp_path: Path) -> None:
        # Requirement (lazy gate): bare validate of a secret-free workflow is
        # byte-identical whether or not environments with bindings exist, and
        # performs no secret-related report or discovery I/O.
        root = _repo(tmp_path)
        workflow_path = _write_plain_workflow(root)
        before_code, before = _invoke_validate(workflow_path)
        _write_environment(root, "demo")

        with (
            patch("conductor.cli.validate._report_secret_bindings") as report,
            patch("conductor.config.environment.discover_all_environments") as discover,
        ):
            after_code, after = _invoke_validate(workflow_path)

        assert before_code == after_code == 0
        report.assert_not_called()
        discover.assert_not_called()
        assert after == before

    def test_ambient_ref_known_everywhere_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: a ref present in every discovered environment is silent,
        # prints no report section, and never checks source presence (unset
        # warnings are an --environment-only behavior).
        monkeypatch.delenv(_SOURCE_VAR, raising=False)
        root = _repo(tmp_path)
        _write_environment(root, "one")
        _write_environment(root, "two")
        workflow_path = _write_secret_workflow(root)

        exit_code, output = _invoke_validate(workflow_path)

        assert exit_code == 0
        flattened = _flatten(output)
        assert "absent from environment" not in flattened
        assert "unset" not in flattened
        assert "Secret Bindings" not in output

    def test_ambient_ref_missing_in_some_warns(self, tmp_path: Path) -> None:
        # Requirement: a ref missing from only some discovered environments warns,
        # naming just those environments.
        root = _repo(tmp_path)
        _write_environment(root, "has-token")
        _write_environment(root, "missing-token", secret_names=("other",))
        workflow_path = _write_secret_workflow(root)

        exit_code, output = _invoke_validate(workflow_path)

        assert exit_code == 0
        flattened = _flatten(output)
        assert "absent from environment(s)" in flattened
        assert "missing-token" in flattened
        assert "has-token" not in flattened

    def test_ambient_ref_unknown_everywhere_fails(self, tmp_path: Path) -> None:
        # Requirement: a ref unknown to every discovered authored environment
        # fails bare validation.
        root = _repo(tmp_path)
        _write_environment(root, "one", secret_names=("other",))
        _write_environment(root, "two", secret_names=("another",))
        workflow_path = _write_secret_workflow(root)

        exit_code, output = _invoke_validate(workflow_path)

        assert exit_code == 1
        flattened = _flatten(output)
        assert "not defined in any discovered authored environment" in flattened
        assert "one" in flattened and "two" in flattened
