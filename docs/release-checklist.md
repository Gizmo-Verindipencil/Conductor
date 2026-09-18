# Release Checklist

This is the step-by-step process for cutting a Conductor release. Releases are
automated once a `v*` tag is pushed: the
[`release.yml`](../.github/workflows/release.yml) workflow runs the quality
gates (lint, typecheck, tests on Python 3.12 + 3.13), builds the package, and
creates a GitHub Release with build artifacts and curated release notes
extracted from `CHANGELOG.md` (the historical `--generate-notes` flag is removed).

The maintainer's job is therefore to (1) prepare a small release-prep PR that
bumps the version, compiles changelog fragments, and re-locks `uv.lock`, and (2) tag the merge commit.

## TL;DR

```bash
# 1. Pick the next version (default: bump the third/"build" number).
#    e.g. 0.1.37 -> 0.1.38

# 2. Compile changelog fragments into CHANGELOG.md:
make changelog-build VERSION=X.Y.Z

# 3. Edit pyproject.toml  (version = "X.Y.Z")
uv lock                  # 4. Re-lock so uv.lock records the new version
make check && make test  # 5. Quality gates locally

# 6. Open + merge the release-prep PR:  chore(release): cut X.Y.Z
# 7. After merge, tag the merge commit on main and push:
git checkout main && git pull
git tag vX.Y.Z
git push origin vX.Y.Z   # triggers release.yml

# 8. Verify the Release workflow is green and the GitHub Release exists.
```

## Versioning

Conductor follows [Semantic Versioning](https://semver.org/): `major.minor.patch`
(the third number is what you may think of as the "build" number).

- **Default (patch bump)** (`0.1.19 -> 0.1.20`): bug fixes and
  backwards-compatible changes. This is the normal case.
- **Minor bump** (`0.1.x -> 0.2.0`): new, backwards-compatible features.
- **Major bump** (`0.x -> 1.0.0`): breaking changes. While the project is `0.x`,
  breaking changes are conventionally signalled by a minor bump.
- **Pre-release** (`0.2.0-beta.1`): any tag with a hyphen after the version is
  marked as a GitHub pre-release automatically. See
  [Pre-releases](#pre-releases) below.

The version lives in exactly one source of truth: the `version` field in
`pyproject.toml`. The CLI reads it at runtime via
`importlib.metadata.version("conductor-cli")` (see `src/conductor/__init__.py`),
so there is **no** separate `__version__` string to edit.

## Step-by-step

### 1. Confirm you're starting clean

- [ ] On an up-to-date `main`: `git checkout main && git pull`.
- [ ] Working tree is clean: `git status`.
- [ ] Decide the next version per [Versioning](#versioning) above.

### 2. Compile `CHANGELOG.md` with towncrier

Pending changes are kept as fragment files in [`changelog.d/`](../changelog.d/README.md) rather than edited directly in `CHANGELOG.md`. To cut release `X.Y.Z`:

- [ ] Run the changelog build target:

  ```bash
  make changelog-build VERSION=X.Y.Z
  ```

  This runs `uvx --from towncrier==25.8.0 towncrier build --version X.Y.Z --yes` under the hood. It compiles all pending fragments from `changelog.d/` into a new `## [X.Y.Z] - YYYY-MM-DD` section directly under the static `## [Unreleased]` marker in `CHANGELOG.md`, then deletes the consumed fragment files.

- [ ] Optionally preview without deleting fragments first:

  ```bash
  make changelog-draft VERSION=X.Y.Z
  ```

- [ ] Review the diff:

  ```bash
  git diff HEAD -- CHANGELOG.md changelog.d/
  ```

  Confirm the newly compiled version section is accurate and that the consumed fragment files under `changelog.d/` are deleted (leaving only `changelog.d/README.md`). Towncrier *stages* its own changes (the compiled `CHANGELOG.md` and the fragment deletions), so a plain `git diff` shows nothing at all right after compilation — `git diff HEAD` shows staged and unstaged changes alike.

### 3. Bump the version in `pyproject.toml`

- [ ] Edit the `version` field under `[project]`:

  ```diff
  -version = "0.1.37"
  +version = "0.1.38"
  ```

### 4. Re-lock `uv.lock`

The lockfile records the project version, so it must be regenerated after the
bump (CI's constraints step runs `uv export --frozen` and will fail on a stale
lock).

- [ ] Run `uv lock` (or `uv sync`) and confirm the only change is the
      `conductor-cli` version: `git diff uv.lock`.

> If this release also changes a dependency floor in `pyproject.toml`, the
> lockfile diff will be larger, which is expected. Re-run the full test suite in
> that case.

### 5. Run the quality gates locally

Mirror what `release.yml` will run so a tag push doesn't fail after the fact.

- [ ] `make check` (ruff lint + format check + `ty` typecheck).
- [ ] `make test` (or `uv run pytest -m "not real_api and not performance"`,
      which matches the CI/release filter).
- [ ] Optionally `make validate-examples` if this release touched schema or
      example workflows.

### 6. Open the release-prep PR

- [ ] Commit on a branch (not `main`). Use the established message convention:

  ```
  chore(release): cut X.Y.Z
  ```

  The commit should contain `CHANGELOG.md`, `pyproject.toml`, `uv.lock`, and the deleted fragment files under `changelog.d/` (plus any deliberate dependency-floor change).

- [ ] Open the PR and let CI (`ci.yml` and `changelog.yml`) go green.
- [ ] Get review/approval and **merge** it. The tag must point at a commit that
      already contains the version bump, so the bump has to land on `main`
      first.

### 7. Tag the merge commit and push

The release workflow extracts the version from the **tag name** (`v` stripped),
and the GitHub Release is built from the tagged commit, so the tag must match
the `pyproject.toml` version exactly and point at the merged release-prep
commit.

- [ ] Sync `main`:

  ```bash
  git checkout main && git pull
  ```

- [ ] Confirm the version on `main` matches the tag you're about to create:

  ```bash
  grep '^version' pyproject.toml      # must read X.Y.Z (no leading v)
  ```

- [ ] Create and push the tag (this is what triggers the release):

  ```bash
  git tag vX.Y.Z
  git push origin vX.Y.Z
  ```

### 8. Verify the release

- [ ] The **Release** workflow run for `vX.Y.Z` is green:
      `gh run list --workflow release.yml` /
      [Actions](https://github.com/microsoft/conductor/actions/workflows/release.yml).
- [ ] The GitHub Release exists with title matching exactly `Conductor X.Y.Z`,
      curated release notes, and attached artifacts (`.whl`, `.tar.gz`,
      `constraints.txt`, `constraints.txt.sha256`): `gh release view vX.Y.Z`.
- [ ] Smoke-test the published install (in a clean shell):

  ```bash
  curl -sSfL https://aka.ms/conductor/install.sh | sh
  conductor --version          # prints Conductor vX.Y.Z
  ```

  The installer resolves the **latest** GitHub Release tag dynamically, so no
  install-script edits are needed per release.

## Pre-releases

To ship a pre-release, use a tag with a hyphen after the version, e.g.
`v0.2.0-beta.1`. The workflow detects the hyphen and marks the GitHub Release as
a **pre-release** automatically (`--prerelease`).

- Set `pyproject.toml` to the matching version (`0.2.0-beta.1`) and re-lock.
- The `conductor update` hint and install script track the latest **stable**
  release semantics; pre-releases are opt-in for testers who pull the tag
  directly.

## Fragment Rules and CI Setup

### Fragment Naming Policy

Fragment filenames in `changelog.d/` follow a free-form convention:
- **Issue known**: `<issue>.<category>.md` (e.g., `392.added.md`). Towncrier appends `(#<issue>)`, which GitHub renders as a clickable link.
- **No issue number**: `+<slug>.<category>.md` (e.g., `+otel-mcp-spans.added.md`). Non-numeric names must start with `+`.
- **Multiple entries**: `<issue_or_slug>.<category>.<seq>.md` (e.g., `450.fixed.1.md`, `450.fixed.2.md`).
- **No renaming needed**: Do not rename fragments after opening a PR. Waiting for or guessing PR numbers is unnecessary; the filename is purely cosmetic once validated.

See [`changelog.d/README.md`](../changelog.d/README.md) for the complete DOs and DON'Ts contract.

### Curated GitHub Release Notes

GitHub Release notes are now curated from `CHANGELOG.md` instead of generated from commit logs. The `.github/scripts/extract-release-notes.sh` script extracts the exact section matching the tag version, and `release.yml` passes it to `gh release create --notes-file`. The historical `--generate-notes` option has been removed.

### Required Status Check and Maintainer Exemption

The `Changelog` workflow ([`.github/workflows/changelog.yml`](../.github/workflows/changelog.yml)) validates pull requests in CI:

- **Advisory Rollout Phase**: Run the workflow in advisory mode first. Merge it into `main` without adding it to required status checks in branch protection settings. Observe its behavior across subsequent pull requests, especially from forks and external contributors. During this advisory period, a failing `Changelog` check on an external PR is a signal for maintainers to help the contributor add a fragment or apply the exemption label, not a blocker. Use the runbook below to convert existing open PRs.
- **Required Check Activation**: After existing open PRs are converted and the workflow is verified stable across forks, add the job name `Changelog` to the repository's required status checks in GitHub branch protection settings. Branch protection rules key off the **JOB** name (`Changelog`), not the workflow file name. If the repository ever enables a merge queue, this check must be excluded from merge-queue required checks (or the workflow extended with a separate `merge_group` job).
- **Maintainer Exemption Label**: For PRs that make no user-facing changes (such as CI tweaks, test fixes, documentation, internal refactoring, or bootstrap changes), maintainers can apply the `changelog-not-required` label. This label provides a full maintainer exemption, waiving both the fragment requirement (zero fragments allowed) and the `CHANGELOG.md` direct-edit prohibition. Fragments present in the PR are still validated: the gate checks every surviving fragment filename in the resulting tree, and additionally runs `towncrier check` whenever the PR adds a fragment (a deletion-only change skips it — towncrier exits "No new newsfragments found" when a branch adds no fragment, which would reintroduce the very requirement the label waives).
- **External PRs**: If an external contributor submits a PR without a fragment, maintainers can either request one, add a `+slug` fragment on the contributor's behalf, or apply the `changelog-not-required` label.
- **Global Kill-Switch**: If the changelog CI check ever needs to be bypassed during an incident, maintainers can temporarily remove `Changelog` from the required status checks list in repository branch protection settings.

## Runbook: Converting Open Pull Requests

Pull requests opened before the fragment workflow migration may still edit `CHANGELOG.md` directly. Convert them using this one-pass runbook:

1. **Inspect the PR**: Check `git diff origin/main...HEAD -- CHANGELOG.md` on the PR branch to find the author's new unreleased entry.
2. **Create the fragment**: Move the entry text into a new fragment file under `changelog.d/` following the naming rules (e.g., `changelog.d/<issue>.added.md` or `changelog.d/+<slug>.fixed.md`). Remove any leading bullet prefix or manual continuation indents.
3. **Revert `CHANGELOG.md`**: Restore `CHANGELOG.md` from `main` so the branch leaves the common changelog file untouched:
   ```bash
   git checkout origin/main -- CHANGELOG.md
   ```
4. **Commit and push**: Commit the new fragment and the restored `CHANGELOG.md`, then push to the PR branch:
   ```bash
   git add changelog.d/ CHANGELOG.md
   git commit -m "chore(changelog): convert changelog edit to towncrier fragment"
   git push
   ```
   The `Changelog` CI check will turn green once `CHANGELOG.md` is unmodified and a valid fragment is present.

## If something goes wrong

- **Release workflow failed during release notes extraction**:
  If the `release` job fails at the extraction step with "section not found or ambiguous", the tag was pushed without a compiled changelog section or the tag version does not match `pyproject.toml`.
  - Cause: `make changelog-build` was omitted during the release-prep PR, or the tag was created with a version mismatch.
  - Remedy: Delete the tag locally and remotely (see below), create a release-prep PR on `main` to run `make changelog-build VERSION=X.Y.Z`, merge it, and re-tag the new merge commit.

- **Release-prep PR blocked by the `Changelog` workflow**:
  If CI rejects the release-prep PR in release mode, check the job's log message:
  - Cause: Leftover fragments remain under `changelog.d/` (for instance, if another PR merged after `make changelog-build` ran), or the version in `uv.lock` does not match `pyproject.toml`.
  - Remedy (lockfile-only mismatch): run `uv lock`, commit `uv.lock`, and push. Nothing else is needed.
  - Remedy (leftover fragments): do **not** just re-run `make changelog-build` — towncrier refuses to compile a version whose `## [X.Y.Z] - <date>` header already exists in `CHANGELOG.md` (same-day re-run fails with "already produced newsfiles for this version"; a later re-run appends a *duplicate* version section, which CI also rejects). Revert the previous compilation, restore every fragment it consumed, pull in whatever landed on the base branch since, and compile the full set in one pass:

    ```bash
    git fetch origin && git rebase origin/main
    git checkout origin/main -- CHANGELOG.md changelog.d/
    make changelog-build VERSION=X.Y.Z
    uv lock
    git add -A && git commit -m "chore(release): recompile changelog for X.Y.Z"
    git push
    ```

    `git checkout origin/main -- CHANGELOG.md changelog.d/` restores the pre-compilation `CHANGELOG.md` and every fragment present on the base branch (including ones merged after your first build), while leaving any fragment that exists only on your branch in place — so the single re-run compiles the complete pending set.

- **Release workflow failed before creating the Release**: fix the cause on
  `main` via a normal PR, then delete and re-push the tag:

  ```bash
  git push origin :refs/tags/vX.Y.Z   # delete remote tag
  git tag -d vX.Y.Z                   # delete local tag
  # ...land the fix on main, pull, then re-tag the new commit...
  ```

- **Release was created but is broken**: do **not** rewrite a published tag.
  Cut a new patch release (`X.Y.Z+1`) following this checklist. Optionally mark
  the bad GitHub Release as a pre-release or add a warning to its notes.

## What the automation does (and doesn't)

| Step | Owner |
|------|-------|
| Bump version, compile changelog fragments (`make changelog-build`), re-lock | **You** (release-prep PR) |
| Lint, typecheck, test (3.12 + 3.13) | `release.yml` |
| Build `.whl` / `.tar.gz`, generate constraints | `release.yml` |
| Extract release notes from `CHANGELOG.md` | `release.yml` (`extract-release-notes.sh`) |
| Create GitHub Release + upload artifacts | `release.yml` (`--notes-file`, `--generate-notes` removed) |
| Publish to PyPI | _Not configured_ (distribution is via GitHub + the install script) |
