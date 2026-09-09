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

The PostgreSQL delivery proof is deliberately split across those boundaries. Recovery coverage
uses isolated disposable source and target stores with dynamically published loopback ports; it
exercises the pinned container's `pg_dump` and `pg_restore`, backend-specific archive verification,
source-endpoint refusal, parity and same-archive idempotency. Missing Docker or SQL dependencies is
a red setup failure, not a skip. Packaging tests
exercise atomic credential materialization, fail-closed config and Compose drift, upgrade ordering
and pulled-code handoff. `tests.test_board_store_schema` in `integration-board` uses real Docker
Compose and `postgres:16`: it creates a disposable named volume on a dynamically selected loopback
port, provisions
all nine private values, migrates scratch to `0007_card_transport_key`, verifies owner/app/read
logins and privileges, proves default privileges with a later owner-created table, reruns unchanged
and removes the disposable project and volume. Missing Docker, Compose, psycopg, SQLAlchemy or
Alembic is a red setup failure, never a skip. No integration test reads or writes the live
installation, its Compose project or its volume.

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

The task checkout's dispatcher-owned `.secretary-task-env/venv` installs a candidate's `.[dev]`
contract when its adapter declares `broad_check` but omits `broad_check.interpreter`, and therefore
supplies its tools to worker and reviewer `PATH`. With no `broad_check`, it intentionally stays bare.
The outer receipt wrapper and all protocol/report/verdict commands still use the absolute production
interpreter; only a fit contract's inner broad suite uses this candidate default.
An adapter-owned `.venv` is separate and is used only when the adapter or its broad-check contract
names it. The production virtualenv is not a lint or test tool boundary.
Never lint the repository as a whole. Against the task base, build the non-deleted
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

## The published web path

Four files cover the path an owner walks from a browser — the front, the transport, the read
operations and the product runtime beneath them. Each is a plain unit test in the `unit` suite;
none of them contacts a live board, a live Orca or the network.

| file | what it holds this path to |
| --- | --- |
| `tests/test_web_front.py` | that no published route is answered without the password — the predicate is run over the shipped renderer *and* over hand-written counter-examples that must be reported, so a green result means it can fail and did not — plus the rendered configuration's own refusals (a non-https site, a non-bcrypt hash, an upstream off the host) and the two units being one service in two halves |
| `tests/test_web_transport.py` | the route table being the whole surface and one route being one operation; the code-to-status table; the cursor a client keeps, which is what makes a reload and a reconnection resume rather than restart; a repeated POST raising no second head; the loopback refusal, name resolution included; and what a card page draws — an unavailable source as a marked block rather than an empty list, an open run apart from a settled one, a reviewer's verdict, and a failed run as a failure carrying its exit status rather than as a run with nothing to show |
| `tests/test_web_read_protocol.py` | the three read operations, their honest sources and their protocol codes |
| `tests/test_web_run_protocol.py` | the product runtime: the admission gate and the order it decides in, the run lifecycle, the five outcomes told apart from the two facts (`value` and `ended`), event visibility through the card's own history, and the absence of Orca on every path |

Two suites in that last file raise **real** processes on real terminals under the real
`LocalPtyHeadRuntime`, because two claims cannot be stood in for by a double:

* `RealHeadOwnershipTests` — a head is raised through the product's own start path, the handle is
  thrown away, and a `HeadRun` rebuilt out of the *write-ahead* record stops it; the ending is
  confirmed from the launch identity, the supervisor's journal and the process table;
* `RealBackendContractTests` — that the real backend honours `run_start` and `run_review`: a head
  that publishes a result is `finished` and is ended by the product that owns it, a head that exits
  non-zero is `process_failed` carrying that status and is on the card's history once, and a review
  is raised by a real worker's result in the worker's own workspace and its verdict is read off its
  own result file. Which binary each head is, is the one substitution; everything else is real.

```console
PYTHONPATH=src python3 -m unittest -v \
  tests.test_web_front tests.test_web_transport \
  tests.test_web_read_protocol tests.test_web_run_protocol
```

What no automated suite can answer is whether the *installed* service works, because that is a fact
about this host rather than about the code: a real card, a real Codex worker, a real Claude reviewer
and a real refusal, over HTTPS through the front and with the owner's password. That walk-through is
in [Operations](OPERATIONS.md#running-a-card-through-the-installed-service), and the unauthorised
half of it — 401 and no body on every published route — is the `curl` loop under
[*Auditing what is exposed*](OPERATIONS.md#auditing-what-is-exposed).

## Import and audit migration evidence

`tests.test_board_import_mapping` covers strict streaming journal validation, duplicate request and
event identity refusal, closed typed-event validation, generic retention, centralized budget claim
ownership, and source movement at the stream and complete-observation boundaries.
`tests.test_board_import_integration` executes the resulting request and event rows against
`postgres:16`, including foreign keys, exact intent retention and occupied-target refusal. Docker,
Compose, psycopg, SQLAlchemy and Alembic are required evidence; absence is red rather than a skip.

The release rehearsal is additionally a read-only observation of the current Kanboard and journal
applied to an isolated dynamic-port Compose target. Its report records current source and
destination counts, fence identities, Alembic head, duration, request replay/ownership and public
history lookup probes. It is migration evidence only. The dispatcher-owned exact-SHA gate remains
the authoritative broad test result, and neither the rehearsal nor a worker-local broad receipt is
live cutover acceptance.
# PostgreSQL cutover rehearsal

`tests.test_cutover` is the controller failure matrix. It interrupts each durable phase, retries the
same identity, and asserts earlier phases execute once. It also covers atomic selector preservation,
runtime-readable/symlink-safe state, backend/phase disagreement, source movement, both recovery
branches, actual service/import/parity/backup/checkpoint/first-write seams, controller identity
across process uids, terminal release during a later freeze, and selector propagation to every
launched role without inference from an existing `board-store.env`. The successor-publication matrix
interrupts both early recovery branches after their immutable archive is published, retries recovery,
proves the old identity and evidence survive, refuses its old token, and applies a distinct plan.
The eligibility matrix then contrasts those two pre-import branches with completed import before
activation, completed import after activation but before an application write, PostgreSQL-only
recovery and `resume-ready`; only the pre-import pair can release the canonical slot.

`tests.test_postgres_recovery.PostgresRecoveryIntegrationTests.test_real_cutover_phases_share_one_disposable_postgres_16_boundary`
runs the real provision, migration verification, quiescence, import, parity, recovery backup,
activation, public acceptance and post-switch checkpoint methods against one isolated PostgreSQL 16
boundary. Only systemd, installed-head probes and dispatcher host launch are substituted. Missing
Docker, psycopg, SQLAlchemy, Alembic or PostgreSQL is a red prerequisite failure, never a skip.

The PostgreSQL contract also seeds `butler-1` and `codegen-product-kit-1` with public task number 1
in different projects. It proves distinct transport keys, active and archived reads, isolated
metadata/comments/moves/audit, idempotent replay, `TaskReader.export`, and lossless upgrade of a
populated `0006` database before a fresh create advances the allocator. Neither public ref is
renumbered.

An external rehearsal and live operation remain conditional evidence boundaries. Record source and
target identities, migration head, per-table import/parity counts, protocol acceptance counts,
archive/checkpoint identities, failure-injection results and exact installed provenance. Never point
a test rehearsal at `/home/dev/secretary-data`, `/home/dev/secretary-instance`, production systemd or
a live observer.
