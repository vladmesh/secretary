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

## Recovery finalization boundaries

`tests.test_secret_recover`, `tests.test_installation`, `tests.test_upgrade`,
`tests.test_github_credential` and `tests.test_checkpoint` jointly cover secret recovery, the root-to-runtime
ownership handoff, real child identity, materializer order and checkpoint publication. The root fixture is
platform-gated because it needs `runuser` and a usable non-root account; when available it loads the installation
key and runs instance Git as the selected child while reporting only numeric uid/gid and mode evidence. The
fixture copies the required product packages beneath its child-traversable temporary root, so it does not depend
on access to the worker checkout under a private home. A separate partial-finalization regression proves that
the same named barrier covers secret, Git lock, progress and dispatcher run-state paths after a later failure.

Head-registry tests use real local repositories. They prove local commit before a disabled push, continuation
through a genuine later step only under the recovery publication policy, isolated successful publication,
fast-forward retry of the retained commit, unchanged retry without an empty commit, and divergence without
reset/rebase/force-push. Ordinary materializer and checkpoint tests retain mandatory stop-on-publication-failure
semantics. Combined recovery/project tests require safe host and pipeline-state finalization to execute while
project rows and checkpoint durability remain truthfully degraded.

Installation recovery tests also use a genuine depth-1 checkout and installation-user Git child. They build the
witnessed one-local/many-remote graph, prove the retained local SHA and fetched upstream are the merge parents,
observe both trees' non-conflicting state, verify no checkout-reuse push, repeat unchanged, advance upstream and
repeat again. Negative fixtures cover overlap conflicts, interruption cleanup, non-head paths, wrong identity and
message, arbitrary merges and ordinary non-recovery refusal. Cleanup evidence is the unchanged HEAD and index
tree, a clean worktree, no `MERGE_HEAD`, and retained fetched remote ref; no automatic side selection is used.

## Normalized board bulk recovery

`tests.test_bulk_card_restore` drives the restore-specific card planner through the real
`KanboardClient.call_batch` encoder/decoder and an in-process JSON-RPC peer. It covers Task, Product and Issue
records, durable staging before mutation, lost create prefixes, lost and malformed initialization replies,
duplicate/conflicting rows, definite backend rejection, oversized-call preflight, audit append failure and
mutation-free replay. Its committed fixture is a sanitized field projection of the real 1,440-row recovery shape:
894 Tasks, 538 Issues, 8 Products, 341 active and 1,099 archived rows, retaining the source columns, 19 swimlane
spellings and sparse `actual_position` values. It contains only shape fields, not source references or content.

The routine benchmark executes a faithful driver for the released per-card create/restore call shape and the
current full `import_normalized_board` path against separate instances of the same hermetic JSON-RPC peer. Both
legs perform creation, metadata/state, archived closure, at least four post-close group repairs and final content
and order parity. Every reported RPC, post and phase duration comes from the executed wire log and phase clock;
pre-write inventory and post-write proof are separate. A 2026-09-05 run with simulated 0.05 ms post latency
measured 33,743 logical RPCs / 25,190 posts before and 17,912 RPCs / 1,087 posts after. The after phase receipt was:
inventory 25/7/0.111s, create 1,440/8/0.106s, metadata/state 4,320/23/0.935s, proof 4,331/48/0.454s,
audit 0/0/0.021s, closure 3,984/40/1.042s, order 927/927/0.277s and final parity 2,885/34/0.102s, where each
triple is logical RPCs/posts/wall time. The test asserts a greater than tenfold post reduction, fewer logical
RPCs, archived closure, multi-group repair, full parity and a second import with zero mutations. Timing is
explicitly `durability=excluded`; a separate 40-card sample runs real `TaskAudit` pending files, locking and
fsync (about 2.6 ms/card on that run). These hermetic numbers are structural evidence, not a live recovery SLO or
durable product wall time.

The general batch policy is at most 200 calls and 1 MiB per JSON document. Comment reads and writes retain their
separate 50-call caps. Tests assert that the clean card path makes no interactive `TaskWriter.create`, generic
`restore_card`, full `TaskReader.show/show_id`, comment read or per-card HTTP post.

`tests.test_bulk_comment_restore` exercises the restore-specific Card/Sprint comment boundary against the real
`KanboardClient.call_batch` encoder and response validator with an in-process wire peer. Deterministic cases cover
pre-existing prefixes, identical bodies, repeat import, first/middle/last lost replies, mixed per-call rejection
and audit append failure, including pending Sprint comments through the public task reconciliation path. The
routine production-shape transport microbenchmark constructs 1,429 cards with 14,174 comments and 93 sprints
with 1,987 comments, counts logical RPCs and actual transport posts, and labels its time `durability=excluded`:
its in-memory audit exists only to isolate transport scaling and is not product wall time. A smaller routine
sample uses real `TaskAudit` pending files, locks, append and fsync and reports per-occurrence durable cost.

The full real-audit benchmark is opt-in because it performs 16,161 durable occurrences:

```console
SECRETARY_FULL_BULK_BENCHMARK=1 PYTHONPATH=src python3 -m unittest -v \
  tests.test_bulk_comment_restore.DurableAuditBenchmark.test_full_production_shape_real_audit
```

On 2026-09-05 it measured 14,174 Card comments in 144.787s over 576 posts and 28,348 logical RPCs,
then 1,987 Sprint comments in 20.578s over 86 posts and 3,974 logical RPCs. The same routine test's
transport-only phases measured 1.211s and 0.157s respectively; those numbers explicitly exclude durability.
Interpret all times as hermetic receipts, not a live SLO. The structural assertion is that posts follow bounded
50-comment entity waves/chunks while logical creates remain one per exported occurrence. The comparison baseline
is the measured legacy Card path at 13–15 logical RPCs and HTTP posts per comment. The supported backend ordering,
result shape and disposable timeout canary live in
[the Kanboard comment contract evidence](evidence/kanboard-comment-contract-v1.2.46.md).

`tests.test_restore` also models Kanboard's post-close position behavior. Its focused order cases cover the
minimal `A active position 1 / B archived historical position 1 / C active position 2` regression, mixed Task,
Product and Issue rows, retry of an already populated parity-failed target, and the full 151-, 156-, 9- and
12-row active sequences from all four sanitized production mismatch groups. The fixture covers near-total
reversal, a correct prefix with a long disordered tail, localized disorder and a near-reversal with a displaced
pair; it requires exactly
131 moves before two placement-free passes. Lost reads, malformed move results and interruption after the group
effect but before audit append are covered separately. The cases assert exact active relative order, retained
archived comments and duplicate occurrences, no duplicate references and no unrelated retry writes:

```console
PYTHONPATH=src python3 -m unittest -v \
  tests.test_restore.RestoreTests.test_post_close_reconciliation_preserves_task_product_and_issue_order \
  tests.test_restore.RestoreTests.test_failed_populated_restore_retries_only_order_and_third_run_moves_nothing \
  tests.test_restore.RestoreTests.test_interrupted_and_malformed_order_moves_are_proven_before_commit \
  tests.test_restore.RestoreTests.test_interruption_after_group_effect_resumes_without_another_move \
  tests.test_restore.RestoreTests.test_sanitized_four_group_failed_state_is_reconciled_once
```
