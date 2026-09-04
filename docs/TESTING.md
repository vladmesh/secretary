# Testing

Dispatcher-owned exact-SHA GitHub CI is the complete test contract. It validates tests/ci-shards.txt before it starts a
suite, then runs these seven named jobs in parallel:

| Suite | CI job | Scope |
| --- | --- | --- |
| unit | test / unit | Isolated product and protocol behaviour. |
| component | test / component | Individual Secretary components and their direct adapters. |
| runtime-component | test / runtime-component | Runtime and local-PTY component boundaries. |
| integration-recovery | test / integration-recovery | Backup, checkpoint, restore and recovery flows. |
| integration-memory | test / integration-memory | Memory and curator integration flows. |
| integration-board | test / integration-board | Board, dispatcher and Pipeline integration flows. |
| packaging | test / packaging | Bootstrap, installation, provisioning and upgrade flows. |

Each exact-SHA suite execution writes its GitHub step summary and uploads the
`ci-evidence-<suite>-<sha>` artifact. Its artifact root contains `report.json`, `junit.xml` and
`test-output.log`; the log contains all output up to 1,000,000 bytes and carries an explicit
truncation marker if it reaches that boundary. Artifacts, including the JUnit XML, are retained for
14 days. For pull requests, `<sha>` is the branch-head candidate SHA; for other events it is
`github.sha`. The summary names the same candidate SHA, outcome, counts, duration, slowest tests
and concise failure locations. Immediately before and after each selected suite, the runner records
Git's complete `status --porcelain=v1 --untracked-files=all` snapshot for the candidate checkout.
A green suite requires those snapshots to match exactly. Evidence retains snapshot entry counts and
digests, plus at most ten bounded changed-status entries, rather than publishing unbounded checkout
contents.

Each of those same seven executions also writes one raw `coverage.<suite>` datum outside that
three-file evidence root and uploads it as `ci-coverage-<suite>-<sha>`. Coverage is a CI-only
dependency and is configured for line and branch coverage of `src/secretary` and
`src/triggered_agents` only. The required aggregate downloads every raw datum, rejects missing,
malformed, incompatible or uncombinable data as infrastructure failure, and publishes the bounded
`ci-coverage-combined-<sha>` artifact. It contains `combined-coverage.json`, with per-file
executed/missing/excluded lines and executed/missing branches, and `changed-lines.json`.

For pull requests, `changed-lines.json` classifies each changed candidate source line against the
exact GitHub base and candidate SHAs as `covered`, `missed`, `excluded` or `not_executable`. Other
events state that this view is not applicable because they have no pull-request base SHA. A
successful push to `main` also retains the same exact-SHA aggregate artifact as
`ci-coverage-baseline-<sha>` for 90 days. This baseline is evidence and comparison context only:
there is no numeric coverage threshold or local coverage collection.

The test job remains the aggregate required result and succeeds only when every applicable suite
succeeds. Its own summary lists each suite as `success`, `product_failure`,
`infrastructure_failure`, `cancelled` or `not_applicable`. A failing test is a product failure;
missing, malformed or unwritable JSON/JUnit/log evidence is an infrastructure failure. Cancelled
matrix work is never treated as success, while routing that explicitly skips a suite is recorded as
not applicable rather than a test failure.
An unavailable Git status command or any test-generated tracked or untracked product artifact is
also an infrastructure failure. If a product test failed in the same contaminated suite, its concise
failure location remains in the evidence, but the suite is classified as infrastructure failure
because the execution boundary cannot be trusted.
`integration-memory` requires the `secretary[memory]` dependency, and `integration-board` requires
its disposable FakeKanboard fixture. If either required setup is unavailable, its suite is an
infrastructure failure, never a green skip. These fixtures use only temporary state and never
contact a live board; real host, systemd, Orca and credential contours remain outside PR CI. Managed
checkpoint GitHub credential handling is tested hermetically with disposable encrypted stores and
Git's native `credential fill` helper protocol. That establishes helper selection and fail-closed
behavior, not a live GitHub authentication or push; the later live token entry, cutover and recovery
drill remain an operator exercise.
The manifest owns the taxonomy: every top-level tests/test_*.py file must occur once, under one
of those names. Unknown names, missing files, stale entries, duplicate entries and empty suites
make the manifest invalid before a selected suite starts.

On a control host, use only focused local work while changing the runner or its manifest:

    python3 -m unittest -v tests.test_ci_shards
    python3 scripts/ci_test_shards.py --check

## Control-host fast profile

    python3 scripts/ci_test_shards.py --fast

This is the one canonical executable fast profile for worker feedback. It validates its fixed
module list before it starts, then runs only the existing hermetic Kanboard, Orca-discovery and
pipeline-state proofs. It is deliberately not a CI suite and does not read `tests/ci-shards.txt`,
expand to the seven-suite taxonomy, or use repository-wide discovery.

The runner gives the complete child process group a 120-second ceiling. On timeout it reports a
failure, terminates the group, and waits for the test child to stop. The child inherits only a
fixture-owned temporary home, XDG directories, Codex home, pipeline-state directory, temporary
directory and a restricted standard tool path, together with the candidate checkout source path. It does not
inherit board, API, cloud or other ambient credentials. A process-startup guard rejects network
connections and subprocesses other than Python and the read-only temporary-instance Git queries
used by the board seam. Live board/API use and Docker, VM, Ansible or provisioning commands
therefore fail loudly. Mutable state is limited to those temporary fixtures and the candidate
workspace.

The profile is intentionally narrow: it proves the existing isolation seams rather than testing
real host, systemd, Orca, credentials, Docker, VM, Ansible or provisioning behaviour. Those runtime
contours remain in their named CI suites or explicit operator checks. Start with focused checks and
`--fast`; when a task or repository contract requires the canonical local broad suite, run the
control-host broad profile below once through the reusable receipt wrapper. Complete validation
remains dispatcher-owned exact-SHA GitHub CI.

## Control-host broad profile

    python3 -m tests.broad

This is the Secretary project's local broad suite: the manifest's `unit` and `component` modules and
nothing else — about 1440 tests in roughly 77 seconds (unit ~58s, component ~19s). It replaces bare
`python3 -m unittest` as the local answer to "run the broad suite". That form is repository-wide
discovery: 3782 tests, about 402 seconds, all seven suites in one process — too expensive to run
between edits, so in practice it was either skipped or paid for once and stretched far past the
point where it still described the code.

The other five suites — `runtime-component` (~128s), `integration-board` (~122s), `packaging`
(~41s), `integration-recovery` (~26s) and `integration-memory` (~13s) — are not part of this
profile. They run in dispatcher-owned exact-SHA GitHub CI, which is still the complete gate and is
not weakened by anything here. A green local broad receipt is a worker's evidence about its own
round, never a substitute for that gate.

The module list is read from `tests/ci-shards.txt` at run time, through the same parser
`scripts/ci_test_shards.py` uses, so the profile cannot drift from the taxonomy: a new top-level
test file assigned to `unit` or `component` joins it with no second list to update, and an invalid
or unreadable manifest fails loudly instead of running a smaller set. `tests/broad.py` lives inside
the `tests` package, so this invocation imports `tests/__init__.py` — and every hermetic default
above — before any test module, which is the secretary-748 invariant
`tests/test_health_suite_command.py` pins.

A registered project names its own broad suite in its adapter's `broad_check` block
(`module`, optional `args`, `import_package`, optional `interpreter`), so the receipt wrapper can
run it with no flag at all:

    python3 -m secretary check broad --reuse
    python3 -m secretary check show

An explicit `--module` still overrides the declared suite. A project whose adapter declares no
module and is given none is refused by name (`no_broad_check_module`) rather than falling back to
repository-wide discovery.

## Runtime deadline boundary

`runtime-component` owns the real local-PTY, process-group, socket and lifecycle tests. It is not
part of `--fast`, and the fixed-fast-profile regression rejects any expansion into those modules.
Expiry, retry, termination and recovery tests in that suite inject short bounds where the production
semantic is unchanged, so they do not wait for a shipped production deadline merely to prove its
ordering or cleanup.

`tests.test_runtime_deadline_contract.ShippedRuntimeDeadlineContractTests` is the deliberately
small exception: it starts the production local-PTY substrate and runtime without deadline
overrides, reads back the admitted shipped delivery deadline, and checks the runtime's shipped
grace and stop-confirmation wiring. It belongs only to `runtime-component`. Do not move it, or any
real PTY/process lifecycle test, into `--fast`. A local broad receipt does not replace the
dispatcher-owned exact-SHA GitHub gate, which remains the complete required suite.

## Changed Python lint

The product checkout's `.venv` supplies the same pinned Ruff that worker and reviewer role commands
receive on `PATH`. Never lint the repository as a whole. Against the task base, build the non-deleted
changed and untracked Python path set, then pass only that set explicitly to both checks:

```bash
base=$(git merge-base main HEAD)
{
  git diff --name-only -z --diff-filter=d "$base" -- '*.py'
  git ls-files --others --exclude-standard -z -- '*.py'
} | sort -zu | xargs -0r ruff check

base=$(git merge-base main HEAD)
{
  git diff --name-only -z --diff-filter=d "$base" -- '*.py'
  git ls-files --others --exclude-standard -z -- '*.py'
} | sort -zu | xargs -0r ruff format --check
```

Use both commands whenever the set contains Python files. The `xargs -r` guard leaves an empty set
as a no-op, rather than making Ruff choose a repository-wide default.
