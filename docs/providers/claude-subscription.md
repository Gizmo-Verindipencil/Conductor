# Claude Subscription Billing Mode

**Status: experimental.** The `claude-agent-sdk` provider remains experimental (see
[Experimental Providers](experimental.md)). This page is the operator runbook for using it with a
Claude subscription login, and for the maintainer-only live validation harness.

**A prior readiness-plus-inference run observed a first-party subscription login and subscription
billing provenance. That observation is provisional, not official evidence, and nothing is
live-proven until it is revalidated with the corrected harness. Revalidation needs the corrected
harness to be reviewed and CI to be green, then a fresh explicit human approval and a new run. The
observation does not validate fake-key behavior, auto-mode or API-key credential precedence,
fallback or any broader compatibility. Earlier readiness-only and official attempts remain
non-official records: they are never reclassified, authorize nothing and support no claim. No
retry is ever automatic.**
Live-proven cells change only from official evidence.

## What this is, and what it is not

- The provider delegates authentication to the `claude` CLI and your local Claude login.
  Conductor does not store, print or log credentials.
- Dollar figures shown for subscription runs are **API-equivalent estimates** computed from token
  counts at API rates. They are **not** invoices, charges or quota. Conductor does not measure
  subscription quota.
- The live validation harness is **POSIX-only** (macOS or Linux).
- This page describes only behavior that exists in the code. Where a behavior has been exercised
  only against test doubles, or not at all, it is marked in the [status table](#status-table).

## Log in safely

1. Run `claude auth login` yourself, in a terminal, in the same operating-system user session that
   will run Conductor. Neither Conductor nor its tests ever run `claude auth login` or
   `claude auth logout`.
2. Do not put credentials in workflow YAML, and start from a shell that has none of
   `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN` set.
3. The `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` and `CLAUDE_CODE_USE_FOUNDRY` selectors
   must be unset: an explicit `auth_mode` refuses to run while one is inherited.

## Verify readiness

```bash
conductor doctor providers --provider claude-agent-sdk --check
```

This runs the provider's readiness check, which invokes `claude auth status --json` once under the
default `auto` mode (no API key, no inference). It is separate from the live harness and needs its
own approval when run by a maintainer. Raw CLI output is never printed by Conductor.

## Configure explicit subscription mode

`examples/claude-agent-sdk-subscription.yaml` is the shipped minimal workflow:

```yaml
workflow:
  runtime:
    provider:
      name: claude-agent-sdk
      auth_mode: subscription
      native_tools: none
      setting_sources: []
    default_model: claude-haiku-4-5
    max_session_seconds: 60
```

Its agent has `tools: []`, no `retry:` block and one structured output, `answer`. Validate it
without running it:

```bash
conductor validate examples/claude-agent-sdk-subscription.yaml
```

`auth_mode: subscription` refuses to run while a cloud-backend selector is inherited or while
`setting_sources` is non-empty, because a settings file can inject a credential after Conductor has
configured the child environment. `ANTHROPIC_BASE_URL` is **not** neutralized by an explicit
mode; an inherited value forces the billing source to `unknown`. Custom headers and proxy variables
(for example `HTTPS_PROXY`) also pass through to the CLI unchanged, but billing derivation does not
inspect them, so they do not change the billing source.

Under explicit `subscription` mode, Conductor blanks `ANTHROPIC_API_KEY` and the other competing
credential variables in the environment it gives the Claude child process. That is an offline
property of Conductor's own environment construction, covered by the production provider's tests,
not something the live harness observes. Auto-mode credential precedence is unproven and out of
scope.

## Run the minimal example

This is ordinary product use: it makes **one inference-capable workflow run** (one agent-session
attempt) on your subscription and writes run records to the normal `~/.conductor/runs`. The agent
session may make several SDK turns and internal model requests; they are not counted, and this is
not a promise of exactly one model request.

```bash
conductor run examples/claude-agent-sdk-subscription.yaml --input question="What is Conductor?"
```

The automated harness below does **not** exercise this entry point.

## Read the labels

For a priced model on a subscription login, the usage summary shows the total with a label and a
note (format taken from the code, with a placeholder amount; *illustrative, not the output of a
real run*):

```text
Total: $<amount> (API-equivalent estimate)
Estimated at API rates from token counts; not an invoice or an additional charge.
```

- The label is printed only when the total cost is greater than zero. For an unpriced model the
  summary says `Cost data unavailable (unknown model pricing)` instead, and no label appears.
- The label describes the *estimate*. It does not tell you what, if anything, was charged.

## Troubleshooting

Each entry is keyed on a message the code can produce.

| Message | Meaning |
|---|---|
| `Not logged in to Claude Code. Run: claude auth login` | The CLI reported no login. Log in as described above. |
| `Authentication check timed out. Claude CLI may not be accessible or credential store may be blocked.` | The status probe did not finish in 5 seconds. A credential-store (Keychain) prompt may be waiting for a person. |
| `The process running Conductor cannot access the Claude Code login context (keychain/credential store unavailable)…` | Run Conductor in the same user session as your Claude Code login. |
| `Authentication check failed. Claude CLI may not be accessible or the login context is unavailable.` | The probe exited non-zero without a readable status. |
| `Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code` | No usable CLI was resolved. |
| `auth_mode '<mode>' cannot be used while <NAME> is set in the inherited environment…` | A cloud-backend selector is set. Unset it, or use `auth_mode: auto`. |
| `auth_mode '<mode>' cannot be combined with runtime.provider.setting_sources (…)` | Remove `setting_sources`, or use `auth_mode: auto`. |
| `auth_mode=<mode>: blanking inherited <NAMES> for the Claude child process.` | A warning: the named credential variables were set and were blanked for the child. |
| `exceeded maximum session duration` | The session hit `max_session_seconds`. |

## Status table

*Implemented* means the code exists. *Hermetically tested* means the offline test suite covers it
with test doubles and no real CLI, login or network. *Live-proven* means an official live
validation backs it; no cell is live-proven today. *Provisional observation* marks a cell that an
earlier run observed against a real Claude CLI and login but that is not official evidence until it
is revalidated. *Unverified* lists what only a real run can settle.

| Capability | Implemented | Hermetically tested | Live-proven | Unverified |
|---|---|---|---|---|
| `auth_mode` resolution and env blanking | yes | yes | not applicable (offline property, R2b′) | — |
| Readiness (`claude auth status --json`) | yes | yes (fixtures) | provisional observation (revalidation pending) | other authentication states, hosts and CLI versions |
| `billing_mode == subscription` derivation | yes | yes (fixtures) | provisional observation (revalidation pending) | other evidence combinations, hosts and CLI versions |
| `API-equivalent estimate` label | yes | yes | provisional observation (revalidation pending) | other output environments and display paths |
| `auto` + API key ⇒ `metered_api` | yes (by construction) | yes | out of scope | auto-mode credential precedence (unproven, R2′) |
| Cloud-selector / `setting_sources` refusal | yes | yes | not applicable | managed/enterprise settings |
| Hard session timeout / interrupt | yes (#570) | yes | not in this PR | real-CLI timing |
| Bundled CLI reads the user's login | — | no | provisional observation (revalidation pending) | cross-version compatibility |
| Live harness (L0, L1) | yes | yes (all boundaries replaced) | provisional observation (revalidation pending) | other hosts, CLI versions and broader real-CLI behavior |
| `conductor run` entry point on a real login | yes | yes (mocked) | *manual step only* | — |

Live-proven cells change only from official evidence, never from expectation. The provisional
cells keep that wording until a new official run, made after the corrected harness is reviewed and
CI is green and under a fresh explicit approval, replaces them.

A prior readiness-plus-inference run observed a first-party subscription login and subscription
billing provenance. That observation is provisional, not official evidence, and nothing is
live-proven until it is revalidated with the corrected harness. Revalidation needs the corrected
harness to be reviewed and CI to be green, then a fresh explicit human approval and a new run. The
observation does not validate fake-key behavior, auto-mode or API-key credential precedence,
fallback or any broader compatibility. Earlier readiness-only and official attempts remain
non-official records: they are never reclassified, authorize nothing and support no claim. No
retry is ever automatic.

## Live validation (maintainers)

The harness is `tests/test_integration/test_claude_agent_sdk_subscription_real.py`, with its offline
safety tests in `tests/test_config/test_claude_subscription_real_gate.py`. It is experimental,
POSIX-only and never runs in CI, on untrusted pull requests or on shared accounts.

The harness claims exactly four things: (1) readiness reports a usable first-party subscription
login; (2) one inference-capable workflow attempt completes through the subscription path and carries
the expected billing provenance; (3) explicit `subscription` mode neutralizes competing API-key variables in
Conductor's finalized child environment, an offline property proven by the production provider's
tests; and (4) auto-mode credential precedence is unproven and out of scope.

A prior readiness-plus-inference run observed a first-party subscription login and subscription
billing provenance. That observation is provisional, not official evidence, and nothing is
live-proven until it is revalidated with the corrected harness. Revalidation needs the corrected
harness to be reviewed and CI to be green, then a fresh explicit human approval and a new run. The
observation does not validate fake-key behavior, auto-mode or API-key credential precedence,
fallback or any broader compatibility. Earlier readiness-only and official attempts remain
non-official records: they are never reclassified, authorize nothing and support no claim. No
retry is ever automatic.

There are two live operations:

- the **readiness-only check** (`-k readiness_probe_only`) runs case L0 alone. It makes **one**
  authentication probe and no inference. It is **not official end-to-end evidence**. It is
  historical: repeating it is neither required nor authorized, and no official run depends on it.
- the **official live validation** (`-k official_live_evidence`) runs L0 and then L1 in one ordered
  test. It makes **three** authentication probes and at most **one** inference-capable workflow attempt.

The **optional manual example** is a separate, separately approved step: one ordinary
`conductor run` of the shipped example (see [Run the minimal example](#run-the-minimal-example)),
which adds one inference-capable workflow attempt (two in all).

Each official live validation needs a fresh, explicit human approval that names the exact tested commit, with the human present.
The approval covers one readiness-plus-inference session only; the inference step is the only one
that can consume quota, and nothing is retried automatically.

### The two gates

The live tests run only when **both** are supplied to the **same command**:

1. `-m real_api` (the marker is shared with other providers' real-API tests, so it alone never
   spends anything here), and
2. `CONDUCTOR_REAL_CLAUDE_SUBSCRIPTION=1`, the exact value `1`.

**Never `export` the variable.** Use it only as a prefix on one command. Never put it in a shell
profile, `.envrc`, an IDE run configuration, `pyproject.toml`, a workflow file or a CI secret, and
never leave it set between commands. Once both gates are supplied, an unmet prerequisite is a
**failure**, never a skip; a run with any skipped test is failed by the session-wide zero-skip
check. An xdist worker is refused. The harness also refuses to run unless the repository's test
isolation fixtures are active, so run/event/PID directories stay under the test's temporary
directory.

### Before either operation

1. Commit the change under test. The official live validation must run on a
   **clean committed tree**: `git status --porcelain` prints nothing.
   **Untracked files make the tree dirty.** On a dirty tree the run still executes, reports
   `official: false`, and has spent subscription usage for evidence that cannot be used.
2. The tested Git SHA is recorded in the evidence (`git_sha`, with `git_dirty`), so a result maps to
   exactly one commit.
3. Use an interpreter that already has the project dependencies. No command on this page installs
   or synchronizes anything. `uv run` and `make` targets are not used because they may synchronize
   an environment.

### A human must be present

Each authentication probe may read or refresh authentication state, contact Anthropic services or
raise a Keychain prompt, which may surface as `Authentication check timed out`. A person must be at
the machine throughout **both** the readiness-only check and the official live validation.

### Commands

A prior readiness-plus-inference run observed a first-party subscription login and subscription
billing provenance. That observation is provisional, not official evidence, and nothing is
live-proven until it is revalidated with the corrected harness. Revalidation needs the corrected
harness to be reviewed and CI to be green, then a fresh explicit human approval and a new run. The
observation does not validate fake-key behavior, auto-mode or API-key credential precedence,
fallback or any broader compatibility. Earlier readiness-only and official attempts remain
non-official records: they are never reclassified, authorize nothing and support no claim. No
retry is ever automatic.

Run both operations in **Bash or Zsh** (zsh on macOS, bash on Linux), from a **plain terminal
session**, never from inside a Claude Code session shell or another agent shell. Write the steps
below as a script file so that `exit` stops it, and run **each readiness or live operation from its
own script invocation**, with its own fresh `EVIDENCE_FILE` and its own `EXPECTED`. The sequence is
**not** generic POSIX `sh`: under dash `set -o pipefail` is a fatal error, which fails loudly.

`PY` is the interpreter and `EVIDENCE_FILE` a file path **outside the repository** that does not
exist yet. The gate variable is a prefix on one command (an `env` argument) and is never exported.
`EXPECTED` is `readiness_only` for the readiness-only check and `official` for the official live
validation; the sequence refuses to start when it is unset or empty.

The sequence for each operation is: the **pre-gate**, the pytest command through `2>&1 | tee`, the
saved `PIPELINE_STATUS`, the hardened classifier and the status/token matrix.

```bash
PY=<an interpreter that already has the project dependencies>
EVIDENCE_FILE=<a new file path outside this repository>
EXPECTED=readiness_only

# pre-gate: it stops before any live action if any check fails
set -o pipefail                                                    # Bash or Zsh
: "${EXPECTED:?}"                                                  # readiness_only or official
ENV_DUMP=$(env) || exit 1                                          # enumeration must succeed
printf '%s\n' "$ENV_DUMP" | grep -E '^(CLAUDE_|ANTHROPIC_)' >/dev/null 2>&1
ENV_CHECK=$?                                                       # 1 only for a clean no-match
unset ENV_DUMP                                                     # never printed or persisted
[ "$ENV_CHECK" -eq 1 ] || exit 1                                   # plain terminal; any error stops
[ ! -e "$EVIDENCE_FILE" ] && [ ! -L "$EVIDENCE_FILE" ] || exit 1   # no pre-existing path
: > "$EVIDENCE_FILE" || exit 1                                     # the path can be created
# readiness-only check (case L0; not official evidence)
env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u PYTEST_DEBUG -u PYTHONWARNINGS -u PYTHONDEVMODE -u PYTHONVERBOSE -u PYTHONPROFILEIMPORTTIME \
  CONDUCTOR_REAL_CLAUDE_SUBSCRIPTION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src" \
  "$PY" -m pytest -m real_api \
  tests/test_integration/test_claude_agent_sdk_subscription_real.py \
  -k readiness_probe_only -q --color=no --show-capture=no --disable-warnings --tb=no -rN -p no:cacheprovider 2>&1 | tee "$EVIDENCE_FILE"
PIPELINE_STATUS=$?   # the very first command after the pipeline; the pipeline status under pipefail, not necessarily pytest's own

# classification (offline, no gate): the hardened classifier, then the closed matrix
VERDICT=$(
  "$PY" -I -S -B tests/test_integration/test_claude_agent_sdk_subscription_real.py \
    --check-capture "$EVIDENCE_FILE" \
    --pipeline-status "$PIPELINE_STATUS" \
    2>/dev/null
)
CLASSIFY_STATUS=$?
: "${EXPECTED:?}"                                                  # an empty token must never match
case "$CLASSIFY_STATUS:$VERDICT" in
  "0:$EXPECTED")
    echo "classified: $EXPECTED"                                   # a candidate; approves nothing
    exit 0 ;;
  "3:failure_record")
    echo "classified: failure_record (kept; authorizes nothing)"
    exit 3 ;;                                                    # kept, yet never a success
  *)
    printf '%s\n' "$VERDICT" | grep -E '^discard [a-z_]{1,40}$'     # a closed-set code
    if rm -f -- "$EVIDENCE_FILE" && [ ! -e "$EVIDENCE_FILE" ] && [ ! -L "$EVIDENCE_FILE" ]; then
      echo "capture deleted; stop"
    else
      echo "capture NOT deleted: remove it manually, never share it; stop"
    fi
    exit 1 ;;
esac
```

For the official live validation, run a **separate script** with `EXPECTED=official`, a fresh
`EVIDENCE_FILE` and the same pre-gate, classifier and matrix around this command, on the exact
clean commit named in the approval:

```bash
# official live validation (L0, L1)
env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u PYTEST_DEBUG -u PYTHONWARNINGS -u PYTHONDEVMODE -u PYTHONVERBOSE -u PYTHONPROFILEIMPORTTIME \
  CONDUCTOR_REAL_CLAUDE_SUBSCRIPTION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src" \
  "$PY" -m pytest -m real_api \
  tests/test_integration/test_claude_agent_sdk_subscription_real.py \
  -k official_live_evidence -q --color=no --show-capture=no --disable-warnings --tb=no -rN -p no:cacheprovider 2>&1 | tee "$EVIDENCE_FILE"
```

Select one test or the other, never both. Add `-n 0` only if the environment has xdist.

What the command is built from:

- The **seven unset operations** `env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u PYTEST_DEBUG -u
  PYTHONWARNINGS -u PYTHONDEVMODE -u PYTHONVERBOSE -u PYTHONPROFILEIMPORTTIME` remove inherited
  pytest options, plugins, debug output, warning filters, dev mode and import tracing from that one
  process only; your shell is not modified. `PYTEST_DISABLE_PLUGIN_AUTOLOAD` is deliberately **not**
  set: plugin autoload stays on so that the offline proofs and a live run use the same plugin set,
  and an unexpected plugin's output makes the classifier discard the capture.
- `-q` removes pytest's `rootdir:` and `plugins:` header (an absolute path and plugin names).
  `--color=no` removes every ANSI sequence, also when `FORCE_COLOR` or `PY_COLORS` is set.
- `--show-capture=no` hides every captured stdout, stderr and log section, even for a failing test.
  `-rA` and `-s` are deliberately **not** used: they print the capture sections of passing tests
  and turn capture off. Never add either.
- `--disable-warnings` hides the warnings summary, which `--show-capture=no` does not: a warning can
  carry provider text and an absolute path.
- `--tb=no` removes every traceback, source line and exception message, and `-rN` removes the short
  test summary (whose lines carry collection-error messages). No other `-r` option is used.
- `-p no:cacheprovider` keeps a pytest cache out of the repository.
- The evidence is written by a terminal-summary hook, so it appears without `-s`.
- `pipefail` keeps a failing exit status visible through the pipe; without it `tee` reports success
  for a failed or interrupted run.

**The shell gate.** `PIPELINE_STATUS=$?` must be the **very next command** after the pipeline. It is
the **pipeline status under `pipefail`, not necessarily pytest's own status**: an unwritable `tee`
target gives 1 even when pytest returned 0 or 2. The classifier is run with `-I -S -B`: `-I` ignores
every `PYTHON*` variable, the user site and the script directory (a shadowing `json.py` or `re.py`,
or a startup variable, cannot change the verdict), `-S` prevents `site`, `.pth` files and
`sitecustomize`, and `-B` writes no bytecode. The matrix tests **both** the exit status and the exact
stdout token. It prints exactly one of `official` (exit 0), `readiness_only` (exit 0),
`failure_record` (exit 3) or `discard <code>` (exit 1), needs no gate and no environment variable,
echoes nothing and deletes nothing.

- Exit 0 with the **expected** token makes the capture a **candidate**: eligible for review, and it
  approves nothing; the next live step still needs its own explicit approval.
- Exit 3 with `failure_record` is a sanitized failure or interruption record. It is **kept** locally
  at the path you chose outside the repository, for your own reading, and the script exits with
  status 3, so a caller can never mistake it for success. It is **never** shared,
  committed, quoted as successful evidence or used to authorize another step, and it never changes a
  documentation claim.
- **Every other combination** (exit 0 with unexpected stdout, the token of the other run kind, exit
  1 without an exact `discard <code>`, a classifier that failed to start, a `tee` failure, a stale
  path, a malformed status) **deletes the capture and stops**. The script then checks that neither
  a file nor a symlink remains at the path. If it prints `capture NOT deleted`, the capture is still
  on disk: remove it manually and never share, quote or commit it.
- A `tee` failure after a live command has run leaves **no valid capture**: usage may have been
  spent and pytest's own status is lost. Do not reuse the path; the pre-gate refuses an existing
  one.
- Never share, quote, commit or use a capture before the matrix has accepted it as a candidate.
  A capture is never judged by eye, and the evidence section alone is necessary, never sufficient.

**The plain terminal.** A Claude Code session shell carries `CLAUDE_CODE_*`, `CLAUDE_EFFORT`,
`CLAUDE_PID` and similar variables that every child process inherits. Nothing in the harness reads
them and they are **not accepted** as part of the evidence environment: the pre-gate refuses to
start when any `CLAUDE_*` or `ANTHROPIC_*` variable is present, or when the environment cannot be
listed or checked (a failing `env` or `grep` is never read as a clean result: only a clean no-match
proceeds). It prints no name or value and writes no environment dump anywhere: the listing lives in
one shell variable that is unset at once. The
`env -u` prefix does not scrub them (a silent scrub would make the evidence describe an environment
that did not exist). Keep your shell free of `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and
`CLAUDE_CODE_OAUTH_TOKEN`, as [Log in safely](#log-in-safely) says.

**The report sanitizer.** Under both gates a **gate-scoped report sanitizer** renders every failure
and interrupt from fixed text. Exception messages, contexts, causes, tracebacks, source lines,
fixture representations, warnings and absolute paths are **not saved**: a failed test shows only
`unexpected exception: <ClassName>` (or the bare `unexpected exception` for an unsafe class name),
or the fixed harness outcome text; after the evidence section an interrupt shows only the lines
below. Only the rendering changes: the original exception, the exit status and the control flow are
untouched. In a **validated** capture only fixed framework markers and validated `EVIDENCE` records
appear. The registration order inside the session fixture is: both gates, the inert terminal-reporter
lookup, the evidence plugin, then the sanitizer as the very next statement, then every other
prerequisite. If the installed pytest cannot support the sanitizer, the run stops with
`prereq_report_sanitizer_unavailable` before any live operation, and that failure **is** recorded,
because the evidence plugin is registered first.

**What the flags and the fixture cannot guarantee.** The no-path, no-secret guarantee applies to a
capture the classifier prints `official` or `readiness_only` for. It is not a claim about every byte
a failed process may write, and absolute paths can be written transiently by a failed run:

- A `conftest` import failure or a plugin-load failure happens before pytest has a terminal reporter,
  so neither the fixture nor the flags can sanitize it. Such a run is never official: its file has
  no `claude subscription live evidence` section.
- `prereq_terminalreporter_missing` and a failure to register the evidence plugin also leave no
  section. An interruption before a run-level record exists is discarded as `run_record_missing`.
- A failure or interrupt in the registration window between the evidence plugin and the sanitizer
  can leave a section followed by pytest's **default** rendering; the classifier rejects it.
- A terminal width far beyond the grammar's bounds (a `COLUMNS` value far above 500) fails closed.

A saved file **without the evidence section must be deleted**, and so must every capture for which
the classifier prints `discard`. It must **not be shared, committed, quoted as evidence or used to
authorize the next live step**. A failure before any case result (`isolation_fixtures_missing`,
`adapters_not_wired`, `prereq_report_sanitizer_unavailable` and the other prerequisites) **does**
leave a section with a fixed-enum `primary_failure`.

**The interrupt lines.** After the evidence section a `KeyboardInterrupt` shows exactly three lines,
in this order: pytest's separator banner (`!!! KeyboardInterrupt !!!`), `interrupted: details
withheld` and pytest's constant line `(to show a full traceback on KeyboardInterrupt use
--full-trace)`, then a fixed count line. **No other interrupt line is acceptable**, and pytest's
normal banner or location line is never accepted.

**pytest upgrades.** `pytest>=9.0.3` is not upper-pinned, and the interrupt hint and the count
lines are pytest constants. A pytest change to a fixed line makes the classifier discard the
capture, which fails closed. After **any** pytest upgrade, rerun the offline safety tests before
any live validation.

### Sequence

There is exactly one procedure, and each step needs the previous one:

1. Implement and test offline: the full offline safety tests and the focused falsification checks
   pass, and nothing live, no readiness probe, no authentication, inference or network operation is
   run.
2. An independent reviewer accepts the implementation, offline.
3. Commit on the feature branch and confirm the tree is clean, untracked files included. The new
   commit's full SHA is the one the official run must record.
4. Only then may the human consider a fresh, separate, explicit approval of the official live
   validation. It names the exact clean tested commit, is given with the human present, covers one
   session and is not implied by the review, the commit or this page.
5. Run the official live validation once through the shell gate with `EXPECTED=official` and a
   fresh `EVIDENCE_FILE`.
6. Read the verdict through the decision table below. An `official` capture is reviewed by the human
   and then independently reviewed offline against the recorded `git_sha`, before anything is
   pushed to a personal fork or any documentation claim changes.
7. Review the sanitized evidence of an `official` capture **before changing any documentation
   claim**. The status table and the sentence in [Experimental Providers](experimental.md) change
   only in a **later commit** that names the tested commit's full SHA in its commit message, and
   only the five live-proven cells, the history statement and that one sentence may change. Any
   other change after the tested commit invalidates the official result: it then needs a new clean
   commit, an independent review, a fresh approval and a new run.
8. If the evidence contradicts how billing is derived, stop and ask for a separate approval; any
   change to the derivation is a new commit and needs a new official live validation.

The provisional observation described at the top of this page stays provisional until step 5 is
repeated with the corrected harness, after the harness has been reviewed and CI is green. That
needs a fresh explicit approval and a new run; no earlier approval carries over.

After a failure record or a discarded capture, nothing is pushed and no documentation commit is made.

If you check whether the upstream branch moved with `git ls-remote upstream main`, treat the output
as informational only; it is never followed by an automatic update of this branch.

### What the official live validation does

The two cases run in a fixed order and each one's route environment is scrubbed (every
`ANTHROPIC_*` variable, `CLAUDE_CODE_OAUTH_TOKEN` and the three cloud selectors are removed for that
case only; your real `HOME` is kept). The harness sets no credential variable.

| Case | What runs | Passes only if |
|---|---|---|
| L0 | The provider is built as the factory builds it for `auth_mode: subscription`; the real readiness check runs; the real billing derivation runs. | Readiness succeeded; billing is `subscription` with reason `first_party_login`; `subscriptionType` is present and `apiKeySource` is absent; the `apiProvider` constant is `validated` or `unexercised` (a mismatch fails). |
| L1 | The shipped example, loaded with `load_config` and run through `ProviderRegistry` and `WorkflowEngine`, once. | Structured `answer` output; a real `agent_completed` event with `billing_mode == "subscription"`; aggregate billing state `subscription`; requested and effective model present; a non-zero estimated cost; the label appears in the real usage summary. |

L1 starts only after L0 is `ok`, so a login that readiness cannot report never starts an
inference-capable attempt. No other case exists, and no step ever sets an API key.

### Authentication probes and quota

- **Probes.** In the current implementation the production path runs the readiness probe
  (`claude auth status --json`) once for L0 and **twice** for L1 (once when the provider registry
  validates the provider, once when the agent executes). The readiness-only check is therefore
  **one** authentication probe and the official live validation is **three** authentication probes
  (1 + 2). Neither duplicate is avoided, because avoiding it would change normal product behavior.
  This count was observed with test doubles; the real CLI's behavior is unverified. The SDK's own
  transport can also run `<cli> --version`, and the harness runs it once for evidence. Probes are
  not inference.
- **Quota.** At most **one** potentially quota-consuming attempt is allowed (L1); a second is
  refused. An *attempt* is one inference-capable workflow or agent-session run, not one guaranteed
  model request: the agent session may make several SDK turns and internal model requests, and
  those are not counted. `max_agent_iterations` is not set to 1. There is no `retry:` block, so
  retries cannot multiply attempts. The optional manual example adds one attempt, for a total
  ceiling of two.
- **Cost figures** are API-equivalent estimates, never quota, invoices or charges.

### Reading the evidence

Evidence is printed as `EVIDENCE {json}` lines under a `claude subscription live evidence`
section of the terminal summary, in three kinds:

- one **session-facts** record (`case: "session"`) after the official path's preflight succeeded;
  the readiness-only check emits **none**;
- one **per-case** record for each case that ran (L0, then L1), also for a failed, timed-out or
  interrupted case;
- exactly **one run-level** record (no `case` key), last. A failure that escapes before the ordered
  session returns (the opt-in gate, the isolation prerequisite, unwired adapters, an unavailable
  sanitizer) is recorded by `fail_closed` with `primary_failure: session:<enum>:<Class>`; every
  other outcome is recorded after the session returns; never both.

The run-level record carries `primary_failure` (`<prefix>:<enum>:<Class>`: the prefix is `session`
when no case was active and the case otherwise; the class may be `none`), `not_executed`
(`<case>:<value>,...` in case order, only for cases that have no record; a completed case is never
listed), `quota_attempts_total` (0 or 1), `quota_ceiling` (always 1), `pytest_version` and `plugins`
(the sorted distribution plugins, or `unavailable`). `skipped_reports`, `zero_skip_verdict` and
`official` are **not** record fields: they are the three lines written first under the section
heading, and `official: true` requires that the ordered test passed, that no test was skipped and
that the Git working tree was clean. A run that was interrupted before any run-level record exists
is discarded as `run_record_missing`.

Each case record carries:

- `outcome`, the primary outcome, and `adapter_outcome`, what the case itself produced. When a
  safety finding outranks the case, they differ.
- `secondary_findings`, drawn only from `descendant_leak`. The precedence is `descendant_leak`, then
  the case's own outcome; the finding is always recorded.
- `interrupted`: `none`, `keyboard_interrupt`, `cancelled` or `other_base_exception`.
- `exception_class`, present when the case raised an exception: the ASCII class name, or
  `unknown_exception`. This includes interrupted cases but is not limited to them, so a
  non-interrupted L0 or L1 failure may carry it too. Its presence and value follow the sanitized
  evidence schema.
- `cleanup_failed`: a bounded, fixed list drawn only from `descendants` and `evidence`, in that
  order and never repeated; present only when a real cleanup or evidence step failed. It never
  carries free text and never replaces the cancellation or hides a finding.
- `attempted_quota_execution`: whether the case started the one inference-capable workflow attempt (an
  attempt, not consumption and not a model-request count). L0 never does.

SDK and provider log records go to a private discarding sink that counts arrivals and keeps no text;
it never reaches pytest's report handlers, the root logger or Python's last-resort handler.

**Cancellation.** An interrupt (Ctrl-C) or a cancellation is never turned into a test failure and
never continues to a later case. The harness observes descendants, restores its patches and records
the findings as secondary evidence, then re-raises the original exception: a cancellation remains
the primary outcome while its terminal rendering is sanitized. If assembling the normal record
fails during a cancellation, a fallback record is built from the findings already collected (never
empty); if even that cannot be emitted, the only output is the fixed line
`evidence_fallback_failed: <case>`. The guarantee covers the harness's own steps: a production
cleanup exception raised while unwinding can still replace a `CancelledError`, and is then rendered
as `unexpected exception: <ClassName>`.

Only allowlisted fields are ever retained: versions, model names, environment variable **names**,
booleans, fixed outcome codes, token counts and estimated cost. Never retained: account identity,
raw `auth status` output, credential values, the raw subscription type, home paths, Keychain data,
raw stdout/stderr, raw log text, exception arguments or matched text.

### Reading an official run

The verdict of the classifier and the records select exactly one row. `I` means some per-case
record has `interrupted` other than `none`, and `P` is the prefix of the run-level `primary_failure`
(`session`, `L0` or `L1`), or `none` when it is absent.

| Row | Predicate | Reading | Next action |
|---|---|---|---|
| **V1** | `CLASSIFY_STATUS = 0 ∧ VERDICT = official` | a candidate: L0 and L1 both `ok`, one inference-capable workflow attempt, clean committed tree; eligible for the human review of the sequence above; approves nothing by itself | `human_review` |
| **V2** | `CLASSIFY_STATUS = 0 ∧ VERDICT = readiness_only` | readiness reported a usable login; never official evidence | `human_review` |
| **V3** | `CLASSIFY_STATUS = 3 ∧ VERDICT = failure_record ∧ I` | a cancellation or interrupt; the original exception was primary; read `interrupted`, `descendants` and `secondary_findings` of the interrupted case | `operator_review` |
| **V4** | `CLASSIFY_STATUS = 3 ∧ VERDICT = failure_record ∧ ¬I ∧ P = session` | a prerequisite or session preflight failed; no case ran and no inference-capable attempt was started | `prerequisite_investigation` |
| **V5** | `CLASSIFY_STATUS = 3 ∧ VERDICT = failure_record ∧ ¬I ∧ P = L0` | readiness did not report a usable first-party subscription login, or L0 had a safety finding; L1 did not run; no inference-capable attempt | `login_investigation` |
| **V6** | `CLASSIFY_STATUS = 3 ∧ VERDICT = failure_record ∧ ¬I ∧ P = L1` | L1 did not complete an inference-capable workflow attempt with the expected billing provenance (read `outcome`, `exception_class`, `descendants`); one attempt may have been spent | `inference_investigation` |
| **V7** | `CLASSIFY_STATUS = 3 ∧ VERDICT = failure_record ∧ ¬I ∧ P = none` | no interrupt and no case-level or session failure was recorded and the L0/L1 records are otherwise valid, yet the run is not official because of a non-case condition: for example a skip elsewhere in the session (zero-skip), a dirty working tree, or another nonzero pipeline status; read the G4 lines and the session-facts record; no cause is presumed | `not_official_no_case_failure` |
| **V8** | any other combination of `CLASSIFY_STATUS` and `VERDICT` (every `discard <code>`, an unexpected token, a classifier that failed to start) | not a candidate; the matrix has deleted the capture, or told you to delete it manually | `delete_and_stop` |

Every row ends with the same rule: **no automatic retry; any new run needs a fresh, explicit human
approval with the human present.** A `failure_record` (V3 to V7) is kept locally, authorizes nothing
and blocks publication.

**The earlier records.** The two earlier official attempts left retained non-official failure
records, and the earlier readiness-only capture is historical. They are never reclassified, never
passed to the classifier or the shell gate (whose matrix would delete them), never promoted, shared,
committed or quoted as evidence, and no file of this change calls any of them a pass or a partial
pass. They authorize nothing.

**No step is ever retried automatically.** Repeating the readiness-only check before the official
run is neither required nor authorized.

### Descendant processes

After each case the harness looks for child processes of the test process that were not there
before, and reports them (PID and short name only). It **never signals** any discovered PID. A
verdict of `no_descendants_remaining` is deliberately narrow: it is **not** proof that nothing
leaked, because an orphan re-parented to PID 1 or a subreaper is invisible to a descendant walk and
PID reuse can hide a leaked PID.

### Side effects

A live run can update Claude-owned state under `~/.claude` and contact Anthropic services beyond
inference. No verified switch suppresses this, so none is set. The harness never reads, lists or
deletes anything under `~/.claude`.

### What stays unverified

- Whether the bundled CLI reads a login created by a different CLI version, and whether
  `claude auth status` performs a network call or raises a Keychain prompt.
- The real `apiProvider`, `subscriptionType` and `apiKeySource` values beyond the single plan on the
  single host that the official validation covered.
- Which credential the CLI uses under `auto` when an API key and a login are both present:
  auto-mode credential precedence is unproven and out of scope, and nothing here claims that a key
  was rejected or honored.
- Managed or enterprise settings and `apiKeyHelper`.

### Offline safety tests

The offline safety tests need neither gate and start no CLI, network or inference:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src" "$PY" -m pytest -p no:cacheprovider -q \
  tests/test_config/test_claude_subscription_real_gate.py \
  tests/test_integration/test_claude_agent_sdk_subscription_real.py \
  tests/test_integration/test_examples.py
```

The live module reports two skips in this command; those are the two gated live tests.
