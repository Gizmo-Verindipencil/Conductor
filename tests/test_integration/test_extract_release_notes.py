"""Executable tests for the release-notes extractor (PR #535 review).

``.github/scripts/extract-release-notes.sh`` produces the GitHub Release
notes for every tag, so its contract is pinned by running the real script
via subprocess against fixture changelogs with exact-output assertions.

The round-trip test drives a pinned ``towncrier build`` in a temporary git
repository and feeds the result to the extractor, proving the towncrier
output shape and the extractor stay compatible. It also pins two towncrier
behaviors the release checklist's recovery runbook depends on: towncrier
*stages* its changes (so a plain ``git diff`` shows nothing right after a
build), and re-running a build for an already-compiled version fails —
which is why the runbook prescribes restore-then-recompile instead of a
plain re-run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTRACTOR = REPO_ROOT / ".github" / "scripts" / "extract-release-notes.sh"

# Gate on the platform, not just on `which`: GitHub's windows-latest images
# put Git-Bash's bash.exe on PATH, but this harness (POSIX paths, env layout)
# does not work there. Precedent: test_install_script_extras.py.
_BINARIES_OK = (
    sys.platform != "win32" and shutil.which("bash") is not None and shutil.which("git") is not None
)
pytestmark = pytest.mark.skipif(not _BINARIES_OK, reason="requires bash and git on POSIX")

MARKER = "<!-- towncrier release notes start -->"

CHANGELOG = f"""\
# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

{MARKER}

## [0.1.38] - 2026-09-17

### Added

- Shiny new thing (#100).
- Another thing.

### Fixed

- A stubborn bug (#99).

## [0.1.37](https://github.com/microsoft/conductor/compare/v0.1.36...v0.1.37) - 2026-09-09

### Added

- Historical entry with a compare link (#90).
"""

EXPECTED_0138 = """\
## [0.1.38] - 2026-09-17

### Added

- Shiny new thing (#100).
- Another thing.

### Fixed

- A stubborn bug (#99).

"""


def run_extractor(version: str, changelog: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(EXTRACTOR), version, str(changelog), str(out)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def write_changelog(tmp_path: Path, text: str, *, crlf: bool = False) -> Path:
    path = tmp_path / "CHANGELOG.md"
    if crlf:
        text = text.replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))
    return path


def assert_failed(
    proc: subprocess.CompletedProcess[str], *needles: str, out: Path | None = None
) -> None:
    assert proc.returncode != 0, f"extractor unexpectedly passed:\n{proc.stdout}\n{proc.stderr}"
    for needle in needles:
        assert needle in proc.stderr, f"missing {needle!r} in stderr:\n{proc.stderr}"


def test_extracts_section_with_exact_output(tmp_path: Path) -> None:
    # Requirement: extraction returns the target section verbatim, from its
    # header through the line before the next level-two heading.
    changelog = write_changelog(tmp_path, CHANGELOG)
    out = tmp_path / "notes.md"
    proc = run_extractor("0.1.38", changelog, out)
    assert proc.returncode == 0, f"extractor failed:\n{proc.stderr}"
    assert out.read_text(encoding="utf-8") == EXPECTED_0138


def test_crlf_input_produces_identical_output(tmp_path: Path) -> None:
    # Requirement: CRLF changelogs are normalized once up front, so matching
    # and extraction see the same bytes as for LF input.
    changelog = write_changelog(tmp_path, CHANGELOG, crlf=True)
    out = tmp_path / "notes.md"
    proc = run_extractor("0.1.38", changelog, out)
    assert proc.returncode == 0, f"extractor failed:\n{proc.stderr}"
    assert out.read_text(encoding="utf-8") == EXPECTED_0138


def test_missing_version_section_fails(tmp_path: Path) -> None:
    # Requirement: a tag without a compiled section fails loudly.
    changelog = write_changelog(tmp_path, CHANGELOG)
    proc = run_extractor("9.9.9", changelog, tmp_path / "notes.md")
    assert_failed(proc, "section not found", "9.9.9")


def test_duplicate_version_sections_fail(tmp_path: Path) -> None:
    # Requirement: two sections for the same version are ambiguous and rejected.
    changelog = write_changelog(tmp_path, CHANGELOG + "\n## [0.1.38] - 2026-09-18\n\n- Dup.\n")
    proc = run_extractor("0.1.38", changelog, tmp_path / "notes.md")
    assert_failed(proc, "ambiguous")


def test_missing_marker_fails(tmp_path: Path) -> None:
    # Requirement: the towncrier insertion marker must exist exactly once.
    changelog = write_changelog(tmp_path, CHANGELOG.replace(f"{MARKER}\n", ""))
    proc = run_extractor("0.1.38", changelog, tmp_path / "notes.md")
    assert_failed(proc, "expected exactly one", "found 0")


def test_duplicate_marker_fails(tmp_path: Path) -> None:
    # Requirement: a duplicated marker is rejected rather than silently used.
    changelog = write_changelog(tmp_path, CHANGELOG + f"\n{MARKER}\n")
    proc = run_extractor("0.1.38", changelog, tmp_path / "notes.md")
    assert_failed(proc, "expected exactly one", "found 2")


def test_section_before_marker_fails(tmp_path: Path) -> None:
    # Requirement: the release section must sit below the marker — a section
    # above it means the file was hand-edited outside the towncrier flow.
    text = f"""\
# Changelog

## [Unreleased]

## [0.1.38] - 2026-09-17

### Added

- Shiny new thing (#100).

{MARKER}

## [0.1.37] - 2026-09-09

### Added

- Historical entry.
"""
    changelog = write_changelog(tmp_path, text)
    proc = run_extractor("0.1.38", changelog, tmp_path / "notes.md")
    assert_failed(proc, "must appear after the towncrier marker")


def test_heading_only_section_fails(tmp_path: Path) -> None:
    # Requirement: a section with headings but no content lines is rejected —
    # it would publish empty release notes.
    text = CHANGELOG.replace(
        "- Shiny new thing (#100).\n- Another thing.\n\n### Fixed\n\n- A stubborn bug (#99).\n",
        "",
    )
    changelog = write_changelog(tmp_path, text)
    proc = run_extractor("0.1.38", changelog, tmp_path / "notes.md")
    assert_failed(proc, "no non-empty content")


def test_legacy_compare_link_header_not_matched(tmp_path: Path) -> None:
    # Requirement: legacy `## [X.Y.Z](compare-link) - date` headers are
    # intentionally not matched, so tagging without a towncrier-compiled
    # section fails loudly.
    changelog = write_changelog(tmp_path, CHANGELOG)
    proc = run_extractor("0.1.37", changelog, tmp_path / "notes.md")
    assert_failed(proc, "section not found")


def test_version_metacharacters_matched_literally(tmp_path: Path) -> None:
    # Requirement: regex metacharacters in the version are escaped, so a
    # decoy header like `## [0X2X0-betaX1]` cannot collide with 0.2.0-beta.1.
    text = CHANGELOG.replace(
        "## [0.1.38] - 2026-09-17",
        "## [0X2X0-betaX1] - 2026-01-01\n\n### Added\n\n- Decoy.\n\n## [0.2.0-beta.1] - 2026-09-17",
    )
    changelog = write_changelog(tmp_path, text)
    out = tmp_path / "notes.md"
    proc = run_extractor("0.2.0-beta.1", changelog, out)
    assert proc.returncode == 0, f"extractor failed:\n{proc.stderr}"
    assert out.read_text(encoding="utf-8").startswith("## [0.2.0-beta.1] - 2026-09-17\n")
    assert "Decoy" not in out.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def towncrier() -> None:
    """Skip the round trip when uv cannot provide the pinned towncrier
    (first run needs network; afterwards it is cached)."""
    if shutil.which("uvx") is None:
        pytest.skip("uvx not available")
    try:
        subprocess.run(
            ["uvx", "--from", "towncrier==25.8.0", "towncrier", "--version"],
            capture_output=True,
            timeout=300,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"pinned towncrier unavailable: {exc}")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return proc.stdout


def test_towncrier_build_then_extract_round_trip(tmp_path: Path, towncrier: None) -> None:
    # Requirement: a real pinned towncrier build produces a section the
    # extractor accepts; towncrier stages its changes (documented in the
    # release checklist), and a repeated same-version build fails — the
    # recovery runbook's restore-then-recompile remedy must actually work.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "rt@test.invalid")
    _git(repo, "config", "user.name", "Round Trip")
    _git(repo, "config", "commit.gpgsign", "false")

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    towncrier_config = pyproject[pyproject.index("[tool.towncrier]") :].rstrip() + "\n"
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "conductor-cli"\nversion = "0.1.37"\n\n' + towncrier_config,
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n" + MARKER + "\n",
        encoding="utf-8",
    )
    fragments = repo / "changelog.d"
    fragments.mkdir()
    (fragments / "README.md").write_text("# Contract\n", encoding="utf-8")
    (fragments / "100.added.md").write_text("Round-trip addition (#100).\n", encoding="utf-8")
    (fragments / "+slug.fixed.md").write_text("Round-trip fix.\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    build = [
        "uvx",
        "--from",
        "towncrier==25.8.0",
        "towncrier",
        "build",
        "--version",
        "0.1.38",
        "--yes",
    ]
    subprocess.run(build, cwd=repo, check=True, capture_output=True, text=True)

    built = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert built.count("## [0.1.38] - ") == 1
    assert "Round-trip addition (#100)." in built
    assert "Round-trip fix." in built
    assert not (fragments / "100.added.md").exists()
    assert not (fragments / "+slug.fixed.md").exists()

    # Towncrier stages the compiled CHANGELOG.md and the fragment deletions,
    # so a plain `git diff` is empty right after a build (release checklist).
    assert _git(repo, "status", "--porcelain").strip() != ""
    assert _git(repo, "diff", "--name-only").strip() == ""
    _git(repo, "commit", "-qm", "compile 0.1.38")

    out = repo / "notes.md"
    proc = run_extractor("0.1.38", repo / "CHANGELOG.md", out)
    assert proc.returncode == 0, f"extractor failed on towncrier output:\n{proc.stderr}"
    notes = out.read_text(encoding="utf-8")
    assert notes.startswith("## [0.1.38] - ")
    assert "Round-trip addition (#100)." in notes
    assert "Round-trip fix." in notes

    # A second build for the same version must fail (same-day duplicate
    # header), which is why the runbook restores before recompiling.
    (fragments / "555.added.md").write_text("Late addition (#555).\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "late fragment")
    rerun = subprocess.run(build, cwd=repo, capture_output=True, text=True)
    assert rerun.returncode != 0
    assert "already produced newsfiles" in rerun.stdout + rerun.stderr

    # The documented remedy: restore CHANGELOG.md and the consumed fragments
    # from the pre-compile commit, keep the late fragment, compile once.
    _git(repo, "checkout", "HEAD~2", "--", "CHANGELOG.md", "changelog.d/")
    subprocess.run(build, cwd=repo, check=True, capture_output=True, text=True)
    rebuilt = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert rebuilt.count("## [0.1.38] - ") == 1
    assert "Late addition (#555)." in rebuilt
    assert "Round-trip addition (#100)." in rebuilt
    proc = run_extractor("0.1.38", repo / "CHANGELOG.md", repo / "notes2.md")
    assert proc.returncode == 0, f"extractor failed after remedy rebuild:\n{proc.stderr}"
