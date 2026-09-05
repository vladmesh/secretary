# Operations

This is the detailed operator reference. Start with the install and recovery path in
[Recovery](RECOVERY.md); use this document when operating or debugging a running installation. The
state of a particular installation — who owns which units, which components are up, whether the
checkpoint is fresh — comes from `secretary status` and `secretary doctor`, not from this file.

The main areas are:

- [installation and host requirements](#install-and-check-the-code);
- [data, status and checkpoint operation](#data-plane);
- [connecting a project](#connecting-a-project-gate-and-stale-input-recovery);
- [sprints and observer heads](#starting-a-sprint);
- [recovery and the optional cold archive](#recovery);
- [dispatcher operation and watchdogs](#auto-merging-green-cards);
- [background roles and units](#background-role-telemetry);
- [upgrade and runtime health](#upgrade).
- [Codex provider-internal fan-out policy](#codex-provider-internal-fan-out-policy).

## Install and check the code

```bash
python3 -m pip install .
python3 -m pip install '.[memory]'
python3 -m pip install '.[dev]'
python3 -m tests.broad
```

The first form installs the CLI, the second adds the memory runtime, and the third the pinned
linter. `ruff` is pinned to one version in `pyproject.toml`, and any other version refuses to run
rather than report a different set of findings. Run it only on the changed Python paths using the
canonical command in [Testing](TESTING.md#changed-python-lint). Host bootstrap currently
supports Ubuntu 24.04. It installs the pinned board and session-manager runtimes; `secretary install`
or `secretary recover` then applies the instance, as described in [Recovery](RECOVERY.md).

`secretary status --instance <dir>` gives the current summary of an installation. Its `--json` form is a
structured snapshot of services and timers, active attempts, checkpoint, memory and host resources, and
writes no state. `doctor` answers a different question: which invariants are broken. It stays a strict
check and its `--json` form returns a structured list of findings. Changing the host still requires
`reconcile plan` and a separate confirmed apply.

## Runtime secrets

### Board transport

`board-transport.env` beside `instance.yaml` is local, non-secret configuration for Kanboard's
JSON-RPC endpoint, application user and application token. Kanboard still requires Basic Auth, but
this token is not a recoverable credential: a fresh bootstrap or install deterministically creates the
same default. An existing installation without this file must provide the complete legacy runtime tuple;
upgrade and recovery refuse to guess or rotate a live transport. The file is gitignored and its ordinary
contents may appear in board reports.

During upgrade a complete legacy `KANBOARD_URL`, `KANBOARD_API_USER`, `KANBOARD_API_TOKEN` tuple is
copied once into this file, then removed from `runtime.env`. This keeps an existing container working
without recreating it. A disagreement is reported as `board transport mismatch`; inspect the container
and resolve it explicitly, rather than rotating a live token.

For an installation from before this change, first upgrade with its existing container still running.
The upgrade writes the one local transport file only when the legacy tuple is complete and agrees with
any existing file; it never guesses or rotates a token. The old encrypted
`kanboard_url`, `kanboard_api_user` and `kanboard_api_token` entries are deliberately ignored by
recovery and materialisation, but remain in redaction until the owner removes them through the
normal secret-store procedure after verifying the migrated installation. Do not recreate the running
container merely to remove those entries.

Installation secrets live in a recoverable store (`secretary secret init/set/import`, the `secrets/`
directory of the private repository) and are materialised from there into env files. The `runtime.env`
next to `instance.yaml` can be one such target for non-board settings: their canonical values then live
in the store and the file is a materialised copy. Whether a given installation has been moved to materialisation is shown by
`secretary status --json` under `secret_store.materialize`; the product does not do this on its own, the
operator runs `secret import`. Either way the file is `0600`, is gitignored in the private repository,
and is part of no checkpoint or archive payload. `secretary shell` receives the whole file for a trusted
operator session; dispatcher-launched workers and reviewers receive non-secret runtime switches through
the role-environment wrapper and resolve board transport from the installation.

`secret import` rejects retired `KANBOARD_URL`, `KANBOARD_API_USER` and `KANBOARD_API_TOKEN` entries.
Migrate that complete tuple only through the board-transport upgrade path; it is no longer a recoverable
runtime secret.

The store does not promise worker isolation: it has no broker and no grants, and the installation key
opens every secret at once, with the same rights that previously read `runtime.env` (see
[Recovery](RECOVERY.md#secrets)).

`doctor` raises a finding when catalog and values diverge, when the key is missing or unusable while the
catalog is non-empty, and when the key's permissions are wider than `0600`.

To migrate an existing canonical `<instance>/runtime.env` into a new store, use the CLI rather than
copying values through a shell or putting them on an argument list:

```bash
python3 -P -m secretary secret init --instance INSTANCE
python3 -P -m secretary secret import --instance INSTANCE --file INSTANCE/runtime.env \
  --scope installation --purpose runtime --materialize runtime-env
python3 -P -m secretary secret materialize --instance INSTANCE --target runtime-env
```

`secret init` is interactive and shows the recovery phrase only once. `runtime-env` is deliberately a
named target, not a path argument: it resolves to this installation's canonical runtime-env path (including
the supported runtime-env override), so import, materialization and launched roles name the same file.
Do not pass `--materialize-path` with `--materialize runtime-env`; it is valid only for the distinct
`file` target. A plain `KANBOARD_URL` is configuration, not an exact-value secret; a URL containing
userinfo remains sensitive.

Instance config holds no secret materialisation inputs. `reconcile` builds the host plan from bindings
and config and never decrypts the store.

### Checkpoint GitHub access

For a private HTTPS GitHub instance remote, enter the one checkpoint token with `secret checkpoint-github
set --instance INSTANCE --stdin` or a caller-owned, regular mode-0600 `--file`. Use the same command to
rotate it. Output contains only id, byte count, creation/replacement metadata and commit. The checkpoint
pusher disables ambient Git helpers and uses its own native credential helper, so do not treat a manual
`~/.git-credentials` entry as proof of checkpoint readiness. Read `checkpoint.credential` in
`secretary status --json --instance INSTANCE` for managed-ready, locked/unverifiable,
missing/unavailable, or ambient/manual-bypass state.

On a clean recovery, provide `--bootstrap-credential-file` (or `--bootstrap-credential-stdin`) for the
initial clone, and use `--recovery-phrase-file` separately to restore the installation key. Repeating
that command after an interrupted recovery uses the supplied bootstrap input for the existing checkout's
fetch and every missing GitHub project checkout; without it, both use the unlocked managed store credential
and otherwise fail closed before remote contact. Ambient Git helpers are never a recovery or checkpoint
fallback. Under `sudo`, a
mode-0600 bootstrap file may be owned by the sudo caller or root; Secretary creates a temporary mode-0600
copy owned by the installation-user Git child and removes it in the same operation. Local/file remotes
need no GitHub credential, SSH is reported as manual-bypass, and non-GitHub HTTPS remotes are refused.
This hermetically supported path does not perform the later live credential entry, cutover, or recovery
drill: schedule those as an operator change after the candidate is accepted.

Credential readiness and its verification timestamp belong to the installation-user Git consumer. A
root `status`, `recover`, or `upgrade` orchestrates that same child rather than reading a user-owned
installation key itself; a failed readiness attempt preserves the last successful verification time.

## Codex provider-internal fan-out policy

Secretary does not require provider-native child-agent isolation. Codex launches use the validated
best-effort v2 low-fan-out configuration and every worker, reviewer and observer prompt explicitly
forbids spawning or delegating to subagents. Do not describe that as proof that the tool surface is
absent; the historical capability evidence remains in [Codex provider-internal fan-out capability
evidence](evidence/codex-provider-fanout-2026-08-13.md).

The runtime keeps the v1 diagnostic protocol. An attested `HeadRun` may record the exact CLI path and
SHA-256 digest, CLI version, model, role, canonical tool-schema digest and explicit
`no_callable_child_spawn_surface` verdict. `schema_absent`, `schema_unknown`, malformed,
unsupported and historical records remain non-clean diagnostics, but they do not prevent a pane
from opening. Workspace trust failures still refuse launch.

Provider events are durable run data, not screen observations. The collector records only
`collaboration_call`, `child_thread_edge`, `unknown_thread_edge` and
`unparseable_provider_event`, each with available parent/child identities, tool name, raw-event
digest, source sequence/location and capture time. Collaboration calls and child edges are violations;
unknown tools/relations, missing parent identity, malformed input and an event-write failure are
unknown. All such observations are telemetry only: do not stop or replace the head, block work,
refuse delivery, or change continuation liveness because of a collaboration event, child edge,
ambiguous source, or telemetry-write failure.

Where available, the recorder attaches to Codex's structured session-event JSONL at
launch. It reads the journal's `session_meta` and `event_msg` envelopes, not pane text. The v1
HeadRun first records an unbound source root and pre-launch path baseline, then the one new matching
journal's path, provider session id, parent thread id and line/digest cursor before its first
prompt. The retained TUI collaboration item is `event_msg.payload.item.type = CollabAgentToolCall`;
an explicit `thread.started` anchors the parent when present, while Codex 0.147's
`session_meta`-plus-`task_started`/`task_complete` journal uses its selected session id as that root.
its `tool`, `sender_thread_id` and `receiver_thread_ids` are normalized with the documented
`collab_tool_call` form. Any other collaboration-shaped item is unknown, not a clean record. Once
the binding is durable, it records first/root/last anchors and a digest of the complete initially
observed range, then starts the shared scanner at an anchored zero cursor. The scanner classifies the
complete initially observed selected source from its first raw record through the root and all existing
tail records before prompt delivery, then every later line before lifecycle work. Selecting a journal by
valid session/root identity never exempts its pre-root data. Ordinary records may move the cursor only
through a durable write. A malformed, collaboration-shaped, child-edge, unknown-relation or
cursor-write failure is retained as diagnostic state where possible. None gates delivery. This is
not the tolerant workspace rollout-activity scan. Recovery verifies the same source's complete
initial range and cursor before consuming a later line; a missing, unreadable, changed or ambiguous
source is non-fatal unknown telemetry.

Rerun the matrix only when a new approved disposable-auth probe is warranted, such as an installed
Codex binary/model change or a candidate provider control. Use the committed
`scripts/codex_capability_matrix.py` harness with a freshly isolated empty git worktree and
`CODEX_HOME`; never direct it at a production home. The harness accepts an explicitly approved auth
source, does not parse or log it, copies it only into the temporary home and deletes the copy before
the next matrix row. Its JSON result has only raw-stream digests and typed event summaries. A live
canary measures the practical child-edge rate under the configured suppression and prompt rule. It
does not require an allowed schema attestation and never stops a run for an observed edge.

## System requirements

The memory runtime loads a local embedding model and is the installation's dominant memory consumer; an
index rebuild is its peak. No supported minimum is declared. Size the host from the resource figures
`secretary status --json` reports for your own installation rather than from a nominal profile.

The memory model cache lives at `DATA_DIR/memory/fastembed-cache`, never in `/tmp`. The memory unit passes
this path directly to fastembed, so the cache survives temporary-file cleanup. `host.memory_threads` sets
the ONNX Runtime inference limit; the default is `1`, so that a single semantic search does not expand
across every core while the dispatcher still ticks every minute. `secretary doctor --instance INSTANCE`
prints the cache path and warns when `data_dir` places it under a temporary directory.

The session-manager runtime belongs to the host. Secretary neither ships a unit for it nor starts it:
scheduler units only order themselves after it, without a dependency that could restart it, so a
minute-by-minute dispatcher tick cannot bounce the host runtime. `secretary doctor` reports that service
as external and not managed by Secretary, and distinguishes a service that is absent from one that is
merely inactive.

## Data plane

```bash
python3 -P -m secretary data init --instance INSTANCE
python3 -P -m secretary data export --instance INSTANCE [--copy-transcripts]
python3 -P -m secretary data raw-kanboard-dump --instance INSTANCE \
  [--container cp-kanboard] [--source-path /var/www/app/data]
```

`data init` creates the local layout and manifest. The canon for memory facts is
`INSTANCE/state/memory/facts`; the data directory keeps its derived export and index. `data export`
writes normalised board, memory, run and transcript exports; without `--copy-transcripts` only a
transcript inventory is kept. `raw-kanboard-dump` creates a timestamped raw dump by copying out of the
container; it writes nothing to the live container and does not use the board API.

A dump is a copy of the whole Kanboard data directory, so its `data/db.sqlite` holds every project on
the board, including the ones the Pipeline export does not cover. On this installation a dump is
roughly 8 to 11 MB. Only the newest dump is ever read: the active-task counter walks the
`kanboard-raw-*` directories newest name first and stops at the first one with a readable database.
Nothing prunes the others. There is no retention window, no age limit and no size cap, and the
directories stay in `DATA_DIR/board` until an operator deletes them by hand, so on a small disk they
are worth watching. Keep the newest dump, and keep any
older dump that is the only surviving copy of a board outside the Pipeline export; the remaining ones
can be removed. The dumps stay out of the instance repository: the checkpoint writer ignores
`kanboard-raw-*`, so deleting one changes nothing that is committed. Every `backup create` run takes a
fresh dump before it writes its archives, which is how the directory grows without anyone invoking the
command by hand; a `core` archive leaves the dumps out, a `full` archive copies them in.

## Checkpoint writer

At the end of every production tick, under the tick lock, the writer regenerates the board and runs
exports, validates the snapshot and commits `state/board` and `state/runs` into the private instance
repository (the contract is in [Recovery](RECOVERY.md)). Only that pathspec is staged, so manual config
edits are untouched by the commit. The gate is fail-closed: pending task audit, a mismatch between the
counters in `export.json` and the line counts, or a detected secret blocks the commit, the reason goes
into the dispatcher's checkpoint state, and the next tick retries. With no change to `state/` there is no
commit.

The board is regenerated by a single export call: the whole board in one call, with the metadata and
comments of all cards in one batched JSON-RPC request. Board size therefore costs the tick one round trip
rather than one per card, which is what keeps the regeneration inside the tick's 60-second budget.

The memory writer commits `state/memory` independently on `propose`/`commit`/`supersede`. Its pathspec
does not overlap the tick writer's, and the shared instance-repository lock serialises both writers along
with the publishing of reviewed instance-repo changes.

The active shipped Secretary pack at `packaging/memory/product-secretary` is also materialized into this
same canon during install and upgrade. Its authoritative source is the selected product checkout, while
the installed ownership and digest record is `INSTANCE/state/memory/packs/product-secretary.json`. It
publishes facts under `product:secretary`; local overlay facts are allowed at other ids, and an existing
local fact at a shipped id stops the upgrade. A complete new manifest deletes only ids named by its prior
ledger. `secretary upgrade --no-pull` still compares the selected checkout's pack digest to that ledger,
then publishes the normal export and restarts the memory service when reconciliation changed it. The
service reports the actual incremental add/update/delete/reuse result in its own reconciliation path.
The ledger is not `ready` until the export has been published and made readable by that service user;
an interrupted or failed handoff remains `pending` and is retried by the next upgrade.

### Memory access from Claude and Codex

Install and upgrade reconcile an installation-owned `po_memory` stdio MCP entry in the installation
user's `~/.claude.json`, `~/.codex/config.toml`, and Orca's managed Codex home. Existing login state,
preferences, and unrelated MCP entries are preserved. The command is an absolute path to
`PRODUCT_ROOT/.venv/bin/secretary-memory-po-bridge`; its environment names only the selected installation's
grant directory and loopback Memory URL. No bearer is stored in client configuration.

The change applies when the next Claude or Codex process starts. Existing provider sessions retain the MCP
servers with which they were launched and must be restarted to acquire `po_memory`. `secretary shell` and
dispatcher-launched heads do not inherit that broad bridge: their launch command selects the direct HTTP
Memory endpoint, and the launcher supplies the role-bound capability. For a worker or reviewer the server
derives the same scope formula for every product: `project:<card-project> + product:secretary`.

To inspect materialization without exposing credentials:

```bash
secretary upgrade --dry-run --no-pull --instance INSTANCE
rg -n 'po_memory|secretary-memory-po-bridge' \
  ~/.claude.json ~/.codex/config.toml \
  ~/.config/orca/codex-runtime-home/home/config.toml
```

### Read-only checkpoint and quiet-tick check

Use an ordinary, already-authorized semantic board transition as the observation point; do not create a
card change, invoke `production-tick`, change a timer, or force a push for this check. Before and after
the next scheduled tick, read the installation with:

```bash
secretary status --json --instance INSTANCE
secretary dispatcher production-observe --instance INSTANCE
secretary doctor --instance INSTANCE
journalctl --user -u secretary-dispatcher-production.service --since "TIME"
```

These reads do not expose or alter secret values or credentials, head profiles, instance configuration,
scheduling, `runtime.env`, or implementation.

For the transition, the dispatcher service's normal tick result identifies the card action and the
checkpoint result. `status` and `production-observe` then show the resulting checkpoint commit in their
`checkpoint` data, while `doctor` reports any blocked gate, push failure, lag or remote divergence. A
changed normalized board or run export produces one observable local checkpoint commit at the end of that
60-second tick; it does not imply an immediate remote push.

Repeat the same read-only observation across a routine tick with no relevant board event. It has to prove
both halves of quietness: no card transition, and no observer wake or launch activity. For every
`observer-reconcile` result in that tick, accept only the quiescent actions `observer-live`,
`observer-waiting`, or `observer-idle` (or no observer result at all). Treat every other observer action
as a failed quiet-tick observation, including delivery actions (`observer-nudged`,
`observer-wake-pending`, `observer-wake-waiting`, `observer-redelivered`, and
`observer-wake-deferred`) and launch, relaunch, or adoption actions (`observer-launched`,
`observer-relaunched`, `observer-launch-pending`, `observer-launch-deferred`,
`observer-launch-skipped`, and `observer-adopted`). This allow-list also fails closed for a new or
unrecognized lifecycle action. The observer snapshot from `production-observe` (also available through
`status`) should remain at its prior lifecycle state, and checkpoint evidence should show `unchanged`
rather than a new commit when normalized `state/` did not change. This verifies the quiet path without
altering scheduling or runtime behavior.

## Status and doctor

`secretary status --json --instance INSTANCE` is the read-only operational snapshot. It is safe to poll.
The `recovery` object is the recovery-readiness inventory shared with text and JSON doctor. Its
`resources` array contains every resource in the installed head registry, including resources no head
has selected recently. `source` distinguishes a fresh `dispatcher-cache` verdict from a
`live-read-only-probe` and an unavailable observation; `freshness`, `observed_at`, `age_seconds`, and
`observed_state` make a stale cached success visibly different from current readiness. Offline reads
never probe and represent absent evidence as `unknown` and expired evidence as `stale`. Status always
uses that metadata-only resource view so it remains safe to poll; doctor performs the bounded live
read-only probes unless `--offline` is selected. Neither command writes the dispatcher probe cache.

`recovery.credential_consumers` inventories the managed checkpoint GitHub consumer separately from
provider CLI logins. The latter remain intentionally unmanaged. Consumer readiness and verification
time do not borrow the checkpoint pusher's last outcome: an older failed `checkpoint.push_status` can
coexist with a currently `managed-ready` credential. A locked store is `locked/unverifiable`, never a
claim that stored and materialized values match.

The remaining arrays are metadata-only. `paths` compares environment selections only when the
environment is bound to the inspected installation; a matching declared override is supported.
`materializations` reports declared target, presence, kind, mode, and count without reading values.
`catalog_envelope_divergences` names open-metadata mismatches. `bypasses` reports applicable Git URL
rewrites, ambient helpers/files, SSH or manual transport, and retired Kanboard catalog entries. Every
unsupported row carries `supported_next_action`. A bypass finding does not make a managed credential
missing, and legacy Kanboard entries never override `board-transport.env`.

It reports managed services and timers, projects and configured heads, active dispatcher attempts, their
workspace, watchdog pane, progress and respawn state, sprint observer heads, pause state, checkpoint
freshness, memory index state, and host disk, memory and load. A live invocation uses the dispatcher's own
pane probe for watchdog liveness; `--offline` deliberately reports that liveness as unprobed.

Its board reads are a fixed number of round trips: the sprint rows, their metadata in one batched read,
and the Pipeline listing once for all sprints together. Polling it stays cheap as the board grows, and a
board holding hundreds of closed sprints costs the same reads as one holding a single sprint.

`secretary doctor --json --instance INSTANCE` evaluates invariants over the same snapshot and returns
structured findings with a non-zero exit status for a broken or unavailable host. Use `status` to answer
what is running now, and `doctor` to decide what needs repair. The default human-readable `doctor` output
remains available for incident work.

When the production dispatcher component is enabled, the observer-root repository under the data directory
belongs to the installation. It appears lazily on the first observer launch, so a fresh installation gets
no finding for its absence. Once created, `doctor` matches the registration against that path and treats a
matching name at a different path as a foreign registration. `reconcile plan` and `reconcile apply` neither
create it, delete it, nor write it into the managed manifest.

## Record reconciliation and controlled divergences

Before advancing active cards, every production tick reconciles its own records against the real state of
the board. Advancing only looks at cards the board currently calls in-progress or validate, so a record
for a card the PO moved out of the cycle directly would never be seen by that path. Reconciliation closes
that gap: it walks every record whose card is not among the active ones, asks the board for that card's
current state and, if the card really is out of the cycle, drops the record. The tick reports this as a
`record-removed` action with the reference and the card state.

Reconciliation touches bookkeeping only. The workspace and terminal the record was driving are not stopped
or deleted: they belong to the PO exactly as the card did, and dealing with them (or reviving the card)
stays the PO's decision. If the board is temporarily unavailable, the record is left alone until the next
tick: reconciliation does not risk mistaking a backend failure for a card leaving the cycle.

The list of active cards it works from is a snapshot taken at the start of the tick. Between that snapshot
and reconciliation's own board call, the PO may have put the card back into the cycle. So absence from the
snapshot is a reason to look, not grounds to delete: immediately before removing a record (or closing the
divergence attached to it), reconciliation asks the board for that specific card's state again and skips it
if it is active.

A controlled divergence is a recorded signal that the board returned something other than what the
dispatcher expected — a claim mismatch and similar. Its lifecycle:

- **Open.** Created when the mismatch is detected, together with the expected value, the actual value and
  the details needed to investigate.
- **Observed.** While the divergence's card stays in the active cycle the record stays open: `status --json`
  and `doctor --json` list it, and `doctor` raises an unresolved-controlled-divergence finding. The finding
  is visible in `--offline` too, because it is read straight from the state snapshot without contacting the
  host.
- **Closed.** The same reconciliation pass that removes orphaned records closes divergences: as soon as the
  card is no longer in the active cycle, whatever state it ended up in, the divergence gets a closed status,
  a close time and a reason. A divergence attached to a terminal card therefore does not stay open forever.

`status --json` and `doctor --json` give an explicit, non-null picture: `dispatcher.divergences` carries the
open count, the total count and the list of open ones with reference, reason and open time.
`dispatcher.reconciliation` carries the number of tracked records, the time the last tick finished (stamped
by every tick, so it does not prove reconciliation exists in the installed code) and the time of the last
reconciliation pass (stamped only by that pass; `null` until the host has ticked at least once on code that
has it — an honest "unknown" rather than borrowing another field as evidence).

Separately from unit-ownership parity, `host.external_runtime` reports the state of the host session-manager
service that Secretary does not own but the scheduler depends on. Oneshot units started by their own timer
have no install section and are active only around their run: neither enabled nor active is required of
them, but their state is still queried and reported rather than left null.

## Connecting a project: gate and stale-input recovery

The stage contract is in [Protocols](PROTOCOLS.md#connecting-a-project). What follows is the operator's
order of work: how to tell a stale input from an invalid one, how to refresh a disabled draft, and how to
verify the result.

### Identity and mutable binding fields

A project's identity is exactly four fields: `id`, `repo`, `adapter`, `default_branch`. `project add` sets
them, and the draft, the provision task and the gate run's `result.json` repeat them verbatim. The schemas
of all three require all four fields and forbid a fifth, so identity cannot drift between stages.

The provision result is deliberately the exception: its schema allows only `id` and `adapter` inside
`identity`, and forbids `repo` and `default_branch`. Provisioning compares exactly that pair against the
draft and rejects a mismatch as a foreign result. The provision agent does not need the path and branch in
its answer, so full identity is evidenced by the draft, the task and the gate result, while the provision
result confirms only `id` and `adapter`.

Routing is not part of identity. `plane` and `policy.code_concurrency` are mutable binding fields: the
provision task reads them as constraints, but neither the gate nor the contract pins them. A repeat
`project add` carries them from the existing binding into the rewritten one, so refreshing a draft does not
reset routing.

### Stale input or an invalid schema

Both reasons stop the same commands, but they are not checked at the same time: validity first, freshness
second.

A schema-invalid input wins first and never mentions HEAD. `project add` validates an existing draft before
it re-reads and rewrites the scanner HEAD recorded in it, so a draft broken against its schema answers
`draft.invalid` whether or not the repository moved on. `provision-*` and `gate` answer `draft_invalid` on
such an input and publish nothing. The errors name a schema path, not a pair of revisions.

The object with findings that a failed `project add` prints is diagnostics, not a disk write: every error
return happens before publication, so instance artifacts are untouched. Fix the source named in the errors;
do not expect a repeat call to pick up a recorded finding.

Stale input is checked only after the draft and binding have validated. It means a commit appeared in the
repository on the default branch after the draft was written. `provision-start` and `provision-apply` answer
with a stale status and print the expected and actual scanner heads. The gate publishes a result with a
stale status and a `stale.input` finding.

The gate reports a separate conflict status for other input desyncs: provisioning not in a drafted state, a
canonical adapter that is unreadable or invalid, or an enabled binding with no matching passed result.

Once a schema error is ruled out, one comparison separates the revisions: read the scanner head recorded in
`adapter-drafts/<id>.yaml` and compare it against the tip of the project's default branch. If the revisions
differ, the input is stale and the recovery below fixes it. If they match and the command still refuses, the
artifact named in the error is at fault; recovery will not help, and loosening the guard, the schema or the
policy to get past it is not an option.

A run whose five checks are all `not-run` is not a universal sign of staleness. That happens only when the
gate on a fresh disabled draft saw HEAD move before it built its worktree. A stale result published after
the run had started keeps whatever checks completed. Both of those reach disk. A stale result on an enabled
binding, by contrast, exists only in the command output and rewrites no result file.

### Refreshing a disabled draft

Staleness is an expected status, not a breakage. Instance files are not edited by hand: each stage rewrites
its own artifacts.

```bash
python3 -P -m secretary project add PROJECT_PATH --instance "$INSTANCE"
python3 -P -m secretary project provision-start PROJECT_ID --instance "$INSTANCE"
# the provision agent writes result.yaml next to task.yaml, taking run_id and scanner head from the task
python3 -P -m secretary project provision-apply PROJECT_ID --instance "$INSTANCE"
python3 -P -m secretary project gate PROJECT_ID --instance "$INSTANCE"
```

Expected statuses for a clean run: `project add` prints a contract artifact with an ok scanner status and a
pending provision status; `provision-start` answers `task_ready` with the path to `task.yaml`;
`provision-apply` answers `drafted` with the binding still disabled; `gate` answers `passed`. Exit code 0
belongs to a successful stage only; any refusal exits 1.

What each stage does:

- `project add` rescans the repository. If HEAD changed, provisioning and gate state in the draft reset to
  pending and the stale canonical adapter is deleted in the same atomic transition, so an old run id and an
  old adapter cannot ride along on a new input. Uncommitted changes in the project reach nothing: the
  scanner reads only the recorded revision and notes the tree's cleanliness, and the gate works on its own
  temporary worktree.
- `provision-start` is idempotent: `task.yaml` for the same run id is not rewritten a second time. The run
  id is a digest of identity and the scanner head, so a new head yields a new run.
- `provision-apply` reads `--result PATH` or the default result path for the run, publishes the canonical
  adapter and keeps the binding disabled. A result carrying a foreign run id or a foreign scanner head is
  rejected.
- `project gate` builds a temporary worktree at the recorded head, runs setup, smoke and validation, and is
  the only stage that sets the binding to enabled.

`project add` on an enabled binding refuses with an "existing binding is enabled" error. That is not a
reason to edit YAML: run `project gate` on the live binding first. It will clear the enable itself if the
input is stale and return the project to the disabled state the recovery works from.

### Re-onboarding an enabled legacy project

A project connected before drafts existed carries `enabled: true` and a canonical adapter but no draft,
provision run or gate result. The gate has no passed result to compare against, so it refuses with
"enabled binding has no matching passed gate result" and leaves the enable in place — `project add` alone
cannot get past it either. `--re-onboard` is the supported way out, and the only one; editing the binding
or the adapter by hand is not:

```bash
python3 -P -m secretary project add PROJECT_PATH --re-onboard --instance "$INSTANCE"
python3 -P -m secretary project provision-start PROJECT_ID --instance "$INSTANCE"
python3 -P -m secretary project provision-apply PROJECT_ID --instance "$INSTANCE"
python3 -P -m secretary project gate PROJECT_ID --instance "$INSTANCE"
```

The flag only lifts the refusal on an enabled binding; nothing else about the stage changes, and the gate
stays the only owner of the enable. In one transition `project add --re-onboard` republishes the binding as
disabled, publishes a fresh draft on the current scanner head with provision and gate back to pending, and
deletes the canonical adapter so it cannot be executed before a new gate. Identity (`id`, `repo`, `adapter`,
`default_branch`) must still match and the binding must still satisfy its schema: a mismatch, a schema
error, or a repository the scanner cannot read fails closed, writing nothing and leaving the enable exactly
as it was. `plane`, `policy`, `remote` and `orca_binding` are carried over.

A takedown also opens a new onboarding cycle, recorded as `onboarding_cycle` in the draft and mixed into
the provision run id. Without it a re-onboarding on an unchanged HEAD would land on the previous cycle's
run: `provision-apply` would republish the very adapter the takedown deleted without any new provisioning,
and the old passed dispatcher-owned exact-SHA gate receipt would then make `project gate` refuse the new run as superseded. A new
cycle gives `provision-start` a fresh run directory instead. The old run directories stay on disk as
history; nothing reads them again. Drafts published before this existed carry no cycle and keep their run
ids.

On an already disabled binding the flag does nothing at all: the run is an ordinary `project add`, so
repeating the command after `provision-apply` republishes the same bytes instead of discarding the run and
without burning a cycle. To re-onboard on a moved HEAD, just run it: the new draft records the current
head, which is what `provision-start` derives its run id from.

What a failure leaves behind depends on how the command died. A refusal or an I/O error restores every file
the transition touched, so nothing needs cleaning up. A host crash or a kill mid-transition has no such
rollback, and the transition is ordered for that case: the draft is written first, the binding second, the
adapter deleted last. The binding is what the next run reads to decide whether a takedown is still owed, so
writing it last keeps an unfinished re-onboarding legible instead of leaving leftovers that look like a
finished one.

Two interruption windows exist, and re-running the command clears both:

- Killed after the draft, before the binding: the project still carries the enable it started from, on the
  adapter it already had. Nothing new is trusted. Re-run `project add --re-onboard` and the takedown
  completes.
- Killed after the binding, before the adapter is deleted: the project is disabled with the new draft, and
  only the stale adapter is left behind. A plain `project add` deletes it, the binding being disabled
  already; `--re-onboard` is not needed and would do nothing.

### Verifying the result

Read the three artifacts:

```bash
cat "$INSTANCE/gate-runs/<project>/<run-id>/result.json"
cat "$INSTANCE/projects/<project>.yaml"
cat "$INSTANCE/adapter-drafts/<project>.yaml"
```

A passed result carries an empty findings list, the four identity fields, the input revisions (scanner head
and provision run id), the adapter digest, and five passed checks: `clean_worktree`, `setup`, `smoke`,
`validation`, `artifact_policy`. The binding holds the same identity, `enabled: true` and the mutable fields
that survived the refresh. The draft holds a gate block with a passed status and the same five checks.

Verify by reading those three files. `project gate` is not a read-only check: on an enabled binding two of
its three outcomes change state.

- The live HEAD matches the recorded one and the canonical adapter still digests the same: the gate finds
  the published passed result for that pair and returns it with exit code 0, changing nothing.
- The live HEAD moved on: the gate clears the enable and prints a stale result with exit code 1.
- HEAD is the same but the adapter was rewritten and its digest no longer matches: the gate looks up the
  previous passed result by scanner head and provision run id and, finding it, clears the enable the same
  way.

Both clearings leave the gate-run result file alone. The atomic publish rewrites only the binding (to
disabled) and the draft, whose gate block becomes failed with a `stale.input` finding and five `not-run`
checks. The stale object that is printed is assembled in memory, so on a rewritten adapter the printed
copy still shows five passed checks.

Hence the discrepancy to keep in mind while investigating: an older passed result sits on disk while the
command just answered stale. The durable traces of the clearing are the disabled binding and the failed
gate block in the draft. A result file describes its own run, not the current state of the project, and its
checks are not evidence of freshness. When state must not be touched, restrict yourself to reading the
result, the binding and the draft.

### What this lifecycle does not prove

The onboarding gate does not check a project's forge configuration. Declaring GitHub CI in the adapter only
means the gate runs no local validation command: without an explicit validation command it runs a default
`git diff --check HEAD` on the temporary worktree. A passed validation says the diff is clean, not that
branch protection is configured.

The set of required checks is declared by the adapter's `validation.required_checks` field, which is the
source of truth for the mechanical gate rather than forge branch protection. The mechanical gate reads it
like this:

- the list is set: only those names colour the card. A name is matched against the name of an Actions
  check-run or the context of a legacy commit status. A required check that has not appeared on the SHA, or
  has not finished, leaves the card pending, where the pending watchdog picks it up; a failed required check
  makes it red and names the check; all required checks successful make it green.
- the list is not set: every check on the SHA goes into the rollup, and any failure makes it red.

Anything outside the list is optional to the gate: a failed or hanging unrelated check-run on the same SHA
does not change the result.

The gate proves nothing about a dispatcher run either, since it publishes no files for one.

## Starting a sprint

A person starts a sprint through an interactive secretary session; the sprint itself is born as an entity on
the sprints board, not as a document. The preparation is defined by the secretary role skill `open-sprint`,
which is delivered to shells by the ordinary `secretary role-skills sync`. It sits in both the Claude and
the Codex target of the secretary role, so behaviour does not depend on which secretary was opened.

The skill walks the secretary through preparation: live context (open and closed sprints, deferred items
from their resume entries and comments, roadmap, the Issues backlog of the affected repositories), a check that no other
open sprint is holding the repositories needed, an interview on unresolved product forks, and a Definition of
Done phrased as checkable items. Choosing the goal stays with the person and is not delegated. A sprint also
needs the Product it belongs to, at least one of its open Issues and at least one reserved registered
project; an installation holds one open sprint at a time unless the [two-sprint
pilot](#the-two-sprint-pilot) is deliberately enabled, and a project another open sprint reserves is
refused as a resource conflict.

The entity is created by the product command, as the `po` role:

```bash
python3 -P -m secretary sprint create --role po --actor <actor> \
  --goal "<one sentence>" --dod-file DOD.md \
  --product <product-id> --issue issue:<ID> --project <project-id> \
  --observer <head-profile|none> \
  --repository <repo> [--repository <repo>]
python3 -P -m secretary sprint show --ref sprint:<ID>
python3 -P -m secretary sprint status --ref sprint:<ID>
```

After that the sprint is not driven by hand: the production tick launches the observer head (see below),
communication with a running sprint goes through entries on the entity (`secretary sprint comment`), and
status is read from data (`secretary sprint status`, `secretary task list --sprint`).

The sprint entity goes into the checkpoint as its own set and is restored along with the cards: after a
recovery the sprint comes back with every field and entry, and does not need to be recreated. The contract is
in [Recovery](RECOVERY.md#what-the-checkpoint-contains).

Storage split: the goal, Definition of Done text, repositories, status, budget, current card and resume are
fields of the entity; a knowledge document holds only the "why" (the context of the moment, the choice of
goal, the alternatives rejected) plus a pointer to the sprint reference. The document does not duplicate the
entity's fields.

## The two-sprint pilot

The shipped default is one open sprint per installation. A second one is a pilot behind an instance
setting, it is off unless somebody turns it on, and turning it on is a deliberate act with consequences
listed under [what stays installation-wide](#what-the-pilot-does-not-isolate) below. Read those first: they
are the part an operator meets during an incident, not during setup.

What admission checks, in what order and at which limit is stated once, in
[Protocols](PROTOCOLS.md#the-open-sprint-limit). Read it before enabling the setting: it is what decides
whether a second sprint you have in mind can be opened at all. In operator terms the second sprint has to
be work that touches nothing the first one touches. Each sprint declares its own observer independently:
both may run a head, since a write of role `observer` is bound to the sprint the launcher bound its head
to.

That binding covers writes made under role `observer`, and the role is what the caller declares. The
sprint entity's `close`, `reopen` and `record_budget` take role `po`, do not check the binding, and take
the sprint reference as an argument, so a head that declares `--role po` reaches any open sprint. An
observer head closing its own sprint is the documented path and uses exactly that route. With two open
sprints this means an observer of one can close the other; nothing in the product prevents it, and the
audit records it as `role=po` with the observer's actor id.

### Enabling it

Add the setting to `instance.yaml` in the instance repository and commit it the way any other config
change lands:

```yaml
open_sprint_limit: 2
```

The only accepted values are the integers 1 and 2. Anything else fails closed: the installation keeps the
limit of one and `secretary doctor` reports the value as an `open_sprint_limit` finding. Nothing restarts;
the limit is read from the config each time an admission asks for it.

### Verifying it took effect

`secretary doctor --instance <instance>` proves the value is not one the installation refused, but a
clean doctor run does not distinguish `2` from an absent setting. Read the effective limit back directly;
this only reads config:

```bash
python3 -c 'import sys; from pathlib import Path; from secretary.sprints import instance_open_sprint_limit; print(instance_open_sprint_limit(Path(sys.argv[1])))' <instance>
```

`1` after writing `2` means the file the command read is not the file that was edited, or the value was
refused; check `secretary doctor` and the `--instance` path. The other observable difference is the
wording of the count refusal: at limit one it reads `installation already has an open sprint`, at limit
two `installation already holds its limit of 2 open sprints`. That is the refusal a candidate gets when
nothing more specific collided, so it is a confirmation when it appears, not a check you can force.

### Reading a refusal

Every refusal happens before any board row, metadata or audit event is written, so a refused `sprint
create` leaves nothing behind and is repeated by fixing the argument. Which of these a given candidate
meets, and which are checked at which limit, follows the rule in
[Protocols](PROTOCOLS.md#the-open-sprint-limit). This table is for reading the message that came back.

| refusal | what it says | what to do |
| --- | --- | --- |
| `resource_conflict` | `project(s) already reserved by an open sprint: <project> held by sprint:ID` | the two sprints want the same project. Give the new sprint different projects, or close the holder. |
| `resource_conflict` | `product <id> is already the product of open sprint sprint:ID; a second open sprint needs a different product` | one Product may have one open sprint. Sequence the two, or open the second sprint on another Product. |
| `resource_conflict` | `... declares no product, so it cannot be proven disjoint ...` | one of the rows predates sprint ownership and carries no Product. Such a sprint cannot be paired; close it, and open a new sprint that declares its Product. |
| `resource_conflict` | `repository root <a> overlaps <b>, held by open sprint sprint:ID` | the two sprints would write in one working tree, including one nested in the other. Narrow the roots, or sequence the sprints. |
| `resource_conflict` | `declares repository root '<value>', which is not an absolute path` | a row stores a relative root, which names a different tree to every process that reads it. New sprints canonicalize their roots at declaration, so this is an old or hand-written row: close it, or correct its `sprint_repositories` metadata before pairing. |
| `sprint_conflict` | `installation already holds its limit of 2 open sprints: ...; close one before opening another` | the installation is full and the candidate collided with nothing specific. Close one of the named sprints. |

### What the pilot does not isolate

Three behaviours stay installation-wide by decision. None of them is a defect to be worked around; they are
the price of the pilot, and they change what an operator running two sprints should expect.

- **`pause drain` and `pause freeze` stop both sprints.** There is no per-sprint pause. A drain called to
  slow one sprint down stops new claims for the other one as well; cards already in flight in both keep
  riding their cycle. A freeze stops the heads of both.
- **One production tick writes for both sprints.** The tick is a singleton per installation and both
  sprints advance inside it. A tick that ends badly, or a dispatcher stopped for repair, is an outage of
  both sprints at once, and the per-tick health line and unit exit code do not say which sprint caused it.
- **A tick that cannot read the sprint board fences the sprint-held work of both sprints.** This is the
  one most likely to show up during an incident. The sprint board and the Pipeline board are separate
  Kanboard projects that fail separately, so the tick can read a sprint's cards perfectly well while it
  cannot read the declaration saying who is watching them. It fences rather than guesses: no declaration
  could be checked, so nothing it can identify as a sprint's work moves, in either sprint. It identifies
  that work two ways, because the board that would answer it is the one that is down: every project the
  last pass that *could* read the sprint board recorded as reserved, kept as a snapshot in the production
  state, plus every card whose own Pipeline metadata names a sprint. The tick reports
  `sprint_board_unavailable` as a critical outcome naming the fenced sprints and projects, and it clears
  by itself as soon as the board answers. The repair is the Kanboard outage, not the sprints. Cards
  belonging to no sprint keep running.

  The gap in that, which is worth knowing before an incident rather than during one: a sprint admitted
  after the last successful pass is not in the snapshot, so its reservations are not either. Its own
  linked cards are still fenced, by their metadata, but a card that was already sitting in a project it
  newly reserved and is not itself linked to it is fenced by neither source, and can be advanced or
  claimed while the board is down. The window is from the sprint's admission to the next pass that reads
  the sprint board, so it is one tick wide in normal running and only opens if the outage starts inside
  it. Opening a sprint and immediately losing the sprint board is the shape to watch for; if that
  happens, `pause freeze` covers it: a frozen tick advances nothing and claims nothing, whatever the
  fence could work out from a stale snapshot. A `drain` covers only the claim half, since cards already
  in flight keep riding their cycle under it.

What *is* per sprint: the declared observer, whose calls are bound to its own sprint and whose head
runs beside the other sprint's; the observer fence when the board is readable (a dead or corrupt
observer stops only its own sprint's projects and cards); the budget counter and its hard stop; and
the claim suppression a blocked card causes, where a card blocked in one sprint closes its own
sprint and its own project to new claims that cycle and nothing beyond them.

A sprint opened with `--observer none` is the other choice, and it is not a degraded observer but no
observer at all: nobody writes resume entries for it, nobody parks its cards for a decision, and its
cards are bounded instead by the [no-observer ceiling](PROTOCOLS.md#the-no-observer-ceiling), where
the third red review moves a card to Blocked. Plan such a sprint as work a person checks on.

### Rolling back to one open sprint

The limit is checked when a sprint is admitted, not continuously, so lowering it does not close anything.
An installation that already holds two open sprints and then sets the limit back to one keeps both open,
keeps ticking both, and refuses every new `create` and `reopen` while it is over its limit. Which refusal
the caller gets follows the ordinary rule in [Protocols](PROTOCOLS.md#the-open-sprint-limit), so do not
expect it to always be the count: a candidate that wants a project one of the two open sprints holds is
told which sprint holds it. The one place this bites is
recovery: a checkpoint taken while two sprints were open cannot be restored onto an installation whose
limit is one, because restore judges the exported open set against the target's limit and refuses the
whole restore with `restored open sprints are not admissible on this installation`.

So the procedure is:

1. close the second sprint first: `python3 -P -m secretary sprint close --role po --ref sprint:ID
   --decisions-file DECISIONS.yaml`, whose decisions cover every issue that sprint declared and every
   card of it outside Done. Its terminal Done cards are archived, each disposed card is taken into the
   end its disposition names, and its reservations are released.
2. confirm with `python3 -P -m secretary sprint list --status open` that exactly one sprint is open.
3. set `open_sprint_limit: 1` in `instance.yaml`, or delete the key (absent means one), and commit it.
4. verify with the read-back command above that the effective limit is `1`.
5. let one production tick run and check that the checkpoint is written and pushed, so the next archive
   is one a limit-of-one installation can restore.

If the limit has to go down before a sprint can be closed (an incident, a bad canary), lower it first and
close the second sprint afterwards: the installation is then over its limit for that window, which
refuses new admissions and, until the second sprint closes, refuses a restore of that window's archive.
Do not leave it in that state longer than the incident.

### What is not proven yet

The repository-root rule reads the roots back out of Kanboard task metadata (`sprint_repositories`). The
exact string round trip through the live backend's `saveTaskMetadata` and `getTaskMetadata` is covered by
in-memory fixtures only, because verifying it for real would mean mutating live sprint rows. If a real
backend altered those strings on the way through by trimming, re-encoding or changing a path's spelling, the
overlap check would be comparing something other than what was declared. Nothing observed says it does;
it is untested against the real thing, and that is the state of the evidence.

## Sprint observer heads

The same production tick, in the same reconciliation pass, keeps one observer head per open sprint on the
sprints board. An observer takes no part in claiming cards: it occupies no project slot, appears in no card
record and does not affect the Ready queue.

While a sprint is open, the projects it reserves belong to that head as the only product writer: the observer creates
only cards linked to it and drives them through board changes. The dispatcher keeps the normal cycle of cards
that are already linked. If an operator needs to intervene, the PO passes `--sprint-override` and a non-empty
`--sprint-override-reason-file` to `secretary task create`, `move` or `edit`; the reason stays in the durable
audit. A `sprint_write_forbidden` refusal names the sprint and suggests recording the change on its entity.
`sprint_guard_unavailable` means the live sprints board could not be checked, so the write was deliberately
refused. `observer_sprint_mismatch` means the observer that wrote belongs to another sprint, and
`observer_identity_unbound` means the head carries no sprint binding at all.

The head's binding is rendered into its command line at launch, so a head that is already running when the
binding is deployed cannot acquire one, and no probe of that process can tell it from a bound head. Its record
answers instead: `bound` is false for a record written before the binding existed, `status --json` shows it per
observer beside `alive`, and the first production tick after the deploy stops such a head with
`observer head predates the sprint binding` in the durable stop event. The tick after that brings the sprint's
head back up bound. No operator step: an installation upgraded while its observer runs performs the changeover
on its own, one stop and one launch, and pays for it with the head's delivery cursor, which the new head
baselines from the current board like any first launch.

The same no-operator adoption rule applies to the Codex provider source. A live pre-contract observer
whose persisted source is `unbound` but lacks the current run descriptor and pre-pane baseline is not
retroactively matched to a journal in its workspace. On a pending significant event the dispatcher persists
typed unavailable wake-liveness evidence, identity-fences the old pid/leaf through the ordinary confirmed-stop
path, and launches the installed observer profile with the same delivery id and event high-water mark. A
foreign heartbeat or an unconfirmed stop remains a fence: no cleanup or replacement is performed beside it.

Before launching, the production tick checks the budget audit of the linked cards. At the signal threshold the
observer's prompt carries a note that the threshold was reached, and the role skill tells it to reconsider the
plan and record that in a resume entry. At the hard threshold the sprint becomes `stopped`: the head is stopped
normally, newly linked Ready cards are skipped, and active cards stay in the ordinary cycle. The operator
checks this through `secretary status --json`, where `installation.sprints.items` shows each sprint's status,
the reason for a hard stop, its budget breakdown, resume freshness and observer state. An unreachable board
shows up in `installation.sprints.error`. Details of one sprint are available through
`secretary sprint status --ref sprint:ID`. Only `secretary sprint reopen --role po` can continue a stopped
sprint.

The tick's decision per sprint is visible in its actions under an `observer-reconcile` step:

- `observer-launched` — an open sprint with no record got a head;
- `observer-live` — the head is alive, the tick did nothing;
- `observer-waiting` — the observer is working and no durable event needs a new turn;
- `observer-idle` — the live head is ready for input with no unacknowledged linked-card event;
- `observer-nudged` — a committed linked-card event woke one idle observer turn;
- `observer-wake-pending` — a delivery batch was already sent and awaits its own acknowledgement;
- `observer-wake-waiting` — an event arrived while the observer was working; the next tick after its
  pane is ready again delivers one nudge, unless exact provider progress says the same run is still advancing.
  The row carries the wait's `admission`: what the provider source answered for it, so a head held by
  telemetry which was never admitted — an unbound, foreign or unreadable source — is not read back as an
  ordinary busy observer. Such an observation is typed durable evidence, not busy or screen liveness: it
  survives a dispatcher reload and can never rebaseline the episode, but it no longer ends the tick either.
  A head nothing can prove is working is bounded by the unproven turn ceiling below;
- `observer-wake-progressing` — an admitted opaque cursor from this record's exact HeadRun advanced. The
  observer, event batch and causal acknowledgement marker remain unchanged; no nudge, stop, replacement,
  cleanup or block occurs;
- `observer-wake-no-progress` — the exact admitted provider cursor is unchanged while the pane remains busy.
  The durable three-observation ladder advances without sending raw input;
- `observer-redelivered` — a batch already on the head was sent again, with the reason on the row: the
  observer was seen ready for input without having acknowledged it, or its acknowledgement deadline
  (`SECRETARY_OBSERVER_ACK_DEADLINE_SECONDS`, 30 minutes by default) ran out. The redelivery keeps the
  original batch, so the resume that follows acknowledges exactly what was owed;
- `observer-wake-deferred` — the event wake failed, including a prompt the pane never took after its
  retries; the observer row carries its reason and bounded retry. After
  `SECRETARY_OBSERVER_WAKE_MAX_ATTEMPTS` (3 by default) such failures the batch is delivered by
  replacing the head instead, which reads as `observer-relaunched` with the failure as its reason;
- `observer-relaunched` — the head was replaced: it had a dead pid, either with unacknowledged work
  owed to it or with a quiet queue, since an open sprint's head is brought up on the evidence that
  the previous one died rather than on the queue. A replacement over a quiet queue leaves a cooldown
  on the record (`launch_attempts`/`launch_next_at`, the ordinary launch backoff), so a head that
  dies again reads as `observer-launch-deferred` until that window passes; a head seen alive on a
  later tick clears it;
- `observer-stopped` — the sprint is closed or gone from the board, the head was stopped, the record dropped;
- `observer-stop-failed` — the host rejected the stop, so the head counts as alive: the record stays in
  `stop-pending` with its handle, no stop event is written, and the next tick retries. This also covers the
  case where the stop has to go by workspace and the session manager did not return a terminal list: an
  unreadable inventory is not an empty one, otherwise a live head would be left with no record;
- `observer-launch-deferred` — the launch was deferred (the head's resource is not ready, the role skill is
  not delivered to that head's shell, bring-up failed, or an old terminal could not be closed first); the
  sprint stays in the record with the reason and the next tick tries again. If bring-up failed after the
  terminal was created and the terminal could not be closed, the record keeps the handle flagged as
  abandoned: the tick does not treat such a head as alive, retries closing the terminal first and only then
  launches a replacement;
- `observer-adopted` — a launch intent that outlived its tick was found on disk and the pid it names is
  alive: that head is accepted as this sprint's head and no second one is launched. Its terminal handle died
  with that tick, so the record reports no known handle and the stop goes by the observer's workspace;
- `observer-launch-pending` — the launch intent is still inside its pid-wait window: the head may simply not
  have written its pid file yet, so the tick leaves it alone and resolves it on the next pass;
- `observer-launch-skipped` — a drain is in progress and no new heads are launched. A record is created anyway
  (deferred, with the reason and the head profile) so the open sprint is visible from outside; neither the
  readiness gate nor the host is called, and after a `resume` the next tick launches the head from that same
  record;
- `sprint-board-unavailable` — the sprints board could not be read, and no live head is stopped.

The acknowledgement deadline (`SECRETARY_OBSERVER_ACK_DEADLINE_SECONDS`, 30 minutes) remains separate
from provider-progress liveness. It is armed when the batch is sent and says how long that one delivery may
stay unacknowledged before it is sent again. It is never compared against the age of the card event: an event
that sat on the board for a day, delivered a minute ago, is a delivery a minute old.

For a current observer source, `wake_liveness` is the authority while a pane is non-idle. Codex stores its
descriptor in `provider_source`; Claude stores its descriptor in `provider_progress_source`. Both are prepared
before the pane opens, bind only their exact run's one post-launch session, and use the same first admitted
cursor as a baseline. A later cursor outranks `tui-idle` and resets only the no-progress ladder; an unchanged
cursor advances the persisted three-observation ladder to an identity-fenced replacement and relaunch.
`payload-left-in-composer` is bounded evidence of a completed/quiescent turn only when it accompanies unchanged
admitted progress. It never authorizes Ctrl-C, Escape, a generic key chord or a raw terminal input.
A head is judged on the clock exactly when it cannot be judged on provider progress. The old
`SECRETARY_OBSERVER_TURN_CEILING_SECONDS` (3 hours) applies to observer records with no attested
provider-progress source, retained for compatibility with historical records. A record
which does carry a source but never got it admitted — it stayed unbound, was rejected as foreign, or
could not be read — is held to `SECRETARY_OBSERVER_UNPROVEN_TURN_CEILING_SECONDS` (15 minutes)
instead: the compatibility ceiling was written for heads nobody can watch, and applying it to
telemetry that is never coming parked a live sprint for hours (sprint:1407, 2026-08-26). Past that
ceiling the delivery fails onto the ordinary bounded wake retries and then replacement, and the batch
is carried into the replacement's own launch delivery. A head whose exact cursor is admitted has no
ceiling at all: its no-progress ladder decides.

A Codex source which was still unbound when its pane was launched is not written off. Every lifecycle
poll retries the binding under exactly the launch-time rules — the immutable run descriptor, the
pre-pane baseline, the workspace the session names, and exactly one remaining candidate — so a journal
the pane wrote a moment after launch is picked up by the next tick. Nothing about a retry is looser
than the first attempt: an ambiguous or foreign journal leaves the source unbound and the head on the
unproven ceiling.
Replacement retains the terminal old-run episode as audit-only state and durably opens a new
episode for the replacement HeadRun before its first provider probe; neither source baseline nor
recovery rung crosses that identity boundary.

Idleness on this path is the pane-readiness signal from the session manager, plus a last-output
timestamp that can be read at all. That is the whole test: the tick that sees a ready pane holding an
unacknowledged batch sends it again on that tick, with no quiet interval required over the last output
or over the delivery's own send. A pane whose activity cannot be read says nothing about whether a turn
ended, so it is not idle here and waits for the deadline instead, and so does a head that is still busy.
A delivery whose send never completed, left in `delivery-intent` by a dispatcher that died mid-send, also
waits for the deadline: a ready pane there may be one the prompt never reached, and reading it as a
finished turn would prompt the head twice. A card in Ready, In progress or Validate is an ordinary wait
throughout and never by itself an idle head.

Prompt delivery does not poll the audit log for the observer's resume. It establishes terminal
acceptance only; the following production reconciliation reads the durable resume and advances the
delivery cursor. This keeps a ready or slow pane from multiplying full audit scans inside one tick.

A refused `terminal wait --for tui-idle` has its own durable evidence state. Orca's failed-command
body `error.code: timeout`, and a body whose `wait.satisfied` is false, mean `busy`: Orca observed
the owned pane working before any prompt was sent. The dispatcher leaves that exact HeadRun, pane
handle and leaf, workspace and pending delivery/acknowledgement marker in place. It does not signal,
close, stop, clean up, release, reattribute or replace that head. An observer wake returns to its
persisted exact-source liveness episode: fresh provider progress keeps the run, while unchanged admitted
progress advances only its bounded ladder. A retained worker continuation records a bounded durable backoff
and remains pending until one later delivery reaches the ordinary confirmation boundary. A reviewer launch
whose document nudge sees busy keeps the exact run, pane
binding and pending delivery in its launch intent; recovery retries that same nudge on its capped
durable schedule before it may freeze the worker, record reviewer routing, set reviewer lifecycle
state or clear the intent. Busy is neither a failed wake nor an acknowledgement. The same retry now
carries every other state in which the pointer was not accepted — the pane held in a dialog, a head
still starting, or the pointer found sitting in the composer — because they are one fact for this
purpose: the reviewer has not received the document.

`unavailable` and `stale_handle` are different evidence states. An unreadable or malformed wait,
or a real transport refusal, is unavailable; `error.code: terminal_handle_stale` is stale-handle
evidence. Neither historical evidence that lacks this typed field nor either of those current states
is treated as busy. They retain their existing conservative recovery paths, including the normal
liveness, launch-intent and confirmed-stop fences.

### Provider-progress liveness canary

Before the final live Terra canary, verify that the candidate has the version-1
`worker_continuation_liveness` record and that it is bound to the worker's current `HeadRun`. The
record must show the first busy time, last provider cursor observation, last fresh progress (when
one occurred), opaque cursor, source fingerprint, baseline status, busy attempts, recovery rung
and terminal outcome. It never contains terminal, composer, prompt or provider text. A missing,
malformed, unsupported or mismatched record is typed `unknown`, not a clean or busy result. The
only unbound shape is that explicit unknown record; a legacy busy count is audit data and cannot
start a v1 ladder.

The observer side has the same zero-operator prerequisite. Its `wake_liveness` record must name the
exact observer HeadRun, source baseline/cursor or a typed unavailable/identity-mismatch state, first
observation, last admitted progress, no-progress rung and terminal outcome. Exercise both branches:
a fresh admitted cursor must outrank `tui-idle` without a nudge or replacement; unchanged cursor plus
residual-composer evidence must reach the bounded identity-fenced relaunch without raw terminal input.
An installed-revision observer with a pre-contract unbound source must be replaced automatically, carrying
the same delivery id/high-water marker until the replacement's matching resume acknowledges it. A missing,
foreign or incomplete source is a canary failure, not evidence that a workspace journal is reusable.

The canary's retained post-`report:done` worker must exercise both precedence branches without an
operator action: advancing evidence from the one launch-bound Codex journal or Claude transcript
keeps the exact run while `tui-idle` is busy; unchanged evidence after that persisted v1 baseline
reaches the three-observation bounded ladder. Workspace-wide newest-file mtimes are not evidence.
Fan-out observations, source-enumeration ambiguity and recorder failures do not enter this liveness
decision. A foreign or incomplete source presented to the exact retained-liveness reader is not
admitted as progress: it keeps the retained head and its existing unavailable or identity-mismatch
fence, rather than spending a recovery rung. The only recovery extension point is a
provider/terminal-safe capability whose receipt names the retained run. Do not use `Ctrl-C`, Escape,
a generic chord, or screen inspection to clear a composer. If the source was admitted and that
capability is unavailable, the recorded terminal path is the existing identity-fenced confirmed
stop followed by exactly one replacement. An unconfirmed stop or identity mismatch is a fence, not
permission to target another pane or launch beside the old run.

A source rejection seals the persisted episode rather than clearing its baseline or no-progress
ladder. A later provider reply cannot re-admit that episode. The shared worker and reviewer status
reads also verify the response's run id and HeadRun fingerprint before its opaque timestamp may
renew the watchdog clock.

For Codex and Claude, inspect the bound source after a real preflight-to-bind handoff. It must retain the
preflight run descriptor exactly: run id, HeadRun fingerprint, resolved workspace, role and task reference.
Journal or transcript selection may add only its verified identity and opaque cursor facts, never replace those
facts. The worker and reviewer provider reads must both reject a source whose descriptor is incomplete or
foreign. The same check applies to an observer launch and its recovered watchdog record. Before the final Terra
canary, verify that the post-delivery HeadRun returned by the
worker, reviewer and observer launch paths is the one in the durable intent/record, with the same
bound source and cursor. A stale local launch copy, conflicting source or mismatched run is a canary
failure: do not nudge, stop, replace, clean up or attribute that head.

The head profile comes from the sprint's own `sprint_observer` field: one concrete profile, or `none` for
a sprint that runs without an observer (see [Protocols](PROTOCOLS.md#the-declared-observer)). It is never
read from `role_defaults.observer` — a sprint that declares a profile the registry does not have is fenced,
not silently launched on a default, and there is no exception for a row that carries no field.
The same resource-readiness gate that runs before claiming a card runs first, with the
same verdicts (see [Head readiness](#head-readiness)). The head is launched through the role-environment
wrapper in its own workspace with its own terminal; the prompt is rendered from the live sprint entity at
launch and references the role skill by path.

An observer's workspace is a registered worktree, not just a directory: without registration the session
manager refuses to create a terminal and the launch becomes `observer-launch-deferred`. It is cut not from a
project repository but from a separate empty repository under the data directory, which the dispatcher creates
itself on first launch and reuses for every sprint. Nothing has to be configured by hand and it must not be
deleted. A directory left at the workspace path that the session manager does not know about is removed and
the workspace recreated; it holds only the rendered prompt, which the next launch rewrites anyway.

Stopping a head kills the workspace's terminals and removes the worktree registration, so after a sprint closes
neither the observer's terminal nor its worktree is left behind. If the worktree is already unregistered, the
stop counts as done: that is what makes retrying an unfinished stop terminate.

A bring-up that failed after the worktree was created leaves a registration with no head. The record remembers
that separately from process liveness, so closing the sprint still removes the worktree instead of abandoning
it. A refusal at that step is an ordinary failed stop: the record stays in `stop-pending` and the next tick
returns to it.

### The observer role skill

What an observer does inside its session is defined by the `observer` role's `observe-sprint` skill. The canon
lives in the product, reaches shells through the ordinary `secretary role-skills sync` (the `role-skills` step
of `secretary upgrade`) and is checked by `secretary role-skills audit --check`.

Before launching, the tick checks that the skill is present in the shell of the head being launched. If it is
not, the head is not launched: the tick reports `observer-launch-deferred` with a reason of the form

```
observer role skill is not available to this head: observer/observe-sprint is not in the codex
skill directory (<root>/observe-sprint/SKILL.md); run `secretary role-skills sync`
```

The reason is stored in the observer record's deferred reason, so it is visible in `secretary status --json`,
`secretary sprint status --ref sprint:ID` and `secretary dispatcher production-observe`. The fix is to deliver
the skill:

```bash
secretary role-skills audit --check
secretary role-skills sync
```

The next tick launches the head from the same record. The same reason is printed when the head's shell has no
`observer` target in `skills/manifest.toml` at all, and when the manifest itself is unreadable.

Both commands read the product manifest plus the optional `<instance>/skills/manifest.toml` of the installation
named by `--instance` (default `SECRETARY_INSTANCE`). An installation may add its own skills without touching
the product tree; an installation with no overlay is a supported one. A skill from either layer may ship one
executable `<skill>.sh`, which sync links into the operator's bin directory as `<skill>`. See
`skills/README.md` for that contract.

Liveness is the same versioned launch-identity heartbeat as for worker and reviewer. A freshly launched head
has not written it yet, so a missing or unreadable file counts as alive for the duration of the initial-output
window and dead afterwards. A live file whose run, role, sprint binding, pane leaf, boot id or process start
ticks do not match the observer record is a distinct `heartbeat-identity-mismatch`: it is neither adopted nor
stopped, and no replacement is launched beside it. A Codex head with an admitted but unchanged provider cursor
does have automatic bounded repair through `wake_liveness`; an untrusted source or foreign heartbeat remains
fenced for an operator rather than targeting a possibly unrelated process.

Lifecycle events go to the shared durable audit log keyed by the sprint reference; a repeat with the same
request id creates no second event. The request id is built from the reference, the record generation and the
launch counter, so a sprint that returns to the board after its record was dropped writes its events afresh
instead of dissolving into the deduplication of the first cycle.

An event is staged before the host call and committed after it. Failures are visible as follows:

- `observer-launch-deferred` with a staging reason, or `observer-stop-failed` mentioning staging — storage
  failed before the action, no head was launched and no terminal closed; the next tick retries;
- any outcome with a pending audit field (a degraded status) — the action happened and was recorded in
  production state, but the event stayed pending. Repair appends it:

```bash
secretary task verify-audit --instance INSTANCE     # .pending
secretary task reconcile-audit --instance INSTANCE  # repaired/unresolved
```

The observer record is persisted in the same order and for the same reason. The launch intent (sprint,
generation, head profile, attempt number, workspace and the future head's pid file) is written to production
state before the host call, not at the end of the tick. That gives two observable cases:

- `observer-launch-deferred` with an intent-not-persisted reason — state is not writable and no head was
  launched; fix the disk or the permissions on the production state file, and the next tick retries;
- a record in a launching state with a non-empty pending launch — the tick died before recording the launch
  outcome. The next tick resolves this from the pid file in the same record: a live pid gives
  `observer-adopted`; no pid file yet inside the initial-output window gives `observer-launch-pending` and the
  intent is left alone; after the window, or with a dead pid, the tick closes the workspace's terminals. The
  attempt counts as spent if its event is already in the log (giving `observer-relaunched` with its own audit
  line), and simply repeats under the same number if the event stayed pending, since the host never answered.
  There is nothing to do by hand.

A successful launch whose state write failed returns a degraded outcome with a pending state field: the head is
up, the intent is on disk, and the next tick adopts it.

### Worker and reviewer launch intent

Card heads use the same loop, on every launch path: first claim, rework after a red review or a red gate,
rework after a repeat `done` on a rejected SHA, a watchdog respawn, and a relaunch on resume. The intent (role,
action, head profile, attempt and round, workspace and the future head's pid file) is a field of the card record
and is written before the host call. The round in the intent is the round of the head being launched: rework
reserves the next round before the host call, so a head accepted after a failure continues the rework rather
than the round a red review or gate already closed. Two observable cases again:

- a worker or review launch-intent-unwritable outcome, status degraded — state is not writable, no head was
  launched and the card is unchanged; fix the disk or permissions and the next tick retries;
- a non-empty launch intent in the record — the tick died before recording the outcome. The next tick resolves
  it first, from the heartbeat in the intent itself: a live matching identity gives a launch-adopted outcome
  (the head is accepted, there will be no second one), a missing file inside the initial-output window gives a
  launch-pending outcome (the head is still coming up and nobody touches it), and a dead heartbeat or an expired
  window drops the intent, closes what is left in the workspace, and lets the ordinary path launch a head again
  into the round the intent reserved. A live identity mismatch is degraded and leaves the intent in place: it is
  not signalled, adopted or replaced.

A third case is a launch that failed after the terminal was created: prompt delivery failed but the pane did
not close, or the reviewer came up but the worker head could not be stopped. The host returns that as a
distinct aborted outcome and the tick reports a launch-aborted action, status degraded. The card does not go to
Blocked and the record is not deleted: a live head would be left with no pointer to it. The intent stays on disk
together with the handle from the error, and the next tick resolves it like any other.

What the delivery boundary saw travels on that intent too, and it is what the next tick reads before anything else.
A launch whose pointer the composer never accepted is **not adopted as a claim**, however alive its pid is: no
routing event, no `claimed`, no `review_starting`, no `reviewing`, no `waiting-review-verdict`, no worker freeze,
and the intent is not cleared. The tick reports `worker-launch-undelivered` or `review-launch-undelivered`,
degraded, with the state that is holding the pointer (`busy`, `blocked`, a pre-delivery state such as
`update-modal`, `starting` or `unknown-dialog`, or `refused` when the pointer was found sitting in the composer)
and which attempt it was. A reviewer re-delivers the same document pointer over its exact retained run on a capped
schedule. Past `SECRETARY_LAUNCH_DELIVERY_MAX_ATTEMPTS` (five) the head is stopped through its own intent first and
the ordinary path launches again: `worker-launch-undeliverable` / `review-launch-undeliverable`. Nothing is ever
opened beside a head that has not been stopped, and a stop the host will not confirm reports
`*-stop-unconfirmed` and keeps the intent.

Two field incidents are the reason. On `issue:6afc6644` a reviewer delivery correctly returned blocked, unconfirmed
and zero bytes written, and the next tick adopted the retained launch as `reviewing` because the pid was alive; the
system reported `waiting-review-verdict` for over an hour against a reviewer that had never received the document.
On `issue:2fdac531` Orca reported `tui-idle/ready` for a Codex head that was still starting its MCP servers, the
TASK pointer stayed in the composer through three Enters, and recovery adopted the live head as a successful claim —
80 minutes to a manual Enter. A live pid, a writable pane and Orca's own `accepted` / `bytesWritten` are not a
provider taking a prompt, and none of them authorises a claim.

The codex update prompt (`Update available! … 1. Update now  2. Skip  3. Skip until next version`) is normally
prevented rather than answered: preflight sets `dismissed_version` in the runtime `CODEX_HOME`'s `version.json`
before the pane exists, which is what codex itself writes when a person picks "Skip until next version". If one
appears anyway the delivery answers that one documented choice, a bounded number of times, and proves readiness
again before writing the pointer. **No delivery ever upgrades codex to get past a dialog**; an upgrade is a
separate, explicit action. A dialog the code does not recognise gets no keystrokes at all — it is a typed refusal
that takes the bounded bring-up deferral above and then the operator-visible infrastructure Blocked.

All of that is decided from the pane's **live screen**, not from everything `orca terminal read` returns. Orca
retains raw output and a TUI redraws in place, so a started, idle Codex pane still carries `Starting MCP servers`
in its tail and a settled update modal still carries its own six lines: the live screen is what follows the last
prompt marker, or the end of the tail when nothing is painting a composer. A keystroke is authorised only while
the dialog is that live screen: a delivery that recognises the update modal's words in history refuses with
`modal-not-on-screen`, having typed nothing, rather than submitting a bare `3` to the provider ahead of the card's
own pointer.

What an operator should **not** expect is a pre-write refusal for a head that is merely still starting. On this
backend nothing before the write says a composer is live and idle: across a real Codex startup window held open
on purpose, Orca answered `tui-idle` satisfied with no `blockedReason` every time, the pane's output cursor never
advanced, and the startup status arrived as a redraw whose fragments spell no phrase. So the delivery writes,
records `sendability=unestablished` on its evidence, and is caught by the receipt instead: the pointer is found
still in the composer, the failure is `payload-left-in-composer` with `pre-delivery-starting` recorded as the
state observed *after* the write, and adoption refuses the claim without spending the launch intent. A report of
`pre-delivery-starting` therefore comes with bytes written, not with zero, and that is the working path rather
than a defect.

Reviewer launch prefers a split from the worker pane. Orca can return
`terminal_split_source_not_found` before or after it attempts to create the child, so the dispatcher
compares the worktree's pane inventory from before and after that refusal. It opens a standalone
reviewer terminal only when no pane appeared; otherwise it remains fail-closed rather than risking a
second reviewer in the same checkout. The successful tick reports
`reviewer_fallback_reason=terminal_split_source_not_found`. Do not treat another split error this
way: its outcome is ambiguous and remains an ordinary fail-closed infrastructure failure.

Everything the tick does with an already-launched head — reading its pane id, writing the routing event, saving
the record — happens while the intent is live, so a failure at those steps does not mean "there is no head" and
is reported the same aborted way. By then the intent holds the launch configuration, so an adopted head reaches
the routing journal with its own profile rather than whatever the registry holds now. A journal that fails at
that write gives an adopt-deferred outcome (degraded): the head stays adopted, the intent stays on disk, and the
next tick appends the journal entry.

The launch result is the authoritative post-delivery `HeadRun`, not the pre-pane/pre-send value. Pane creation
may add its verified handle and leaf, and the delivery boundary may bind the Codex or Claude source. Intent
confirmation, routing and role records merge those facts only after their identities agree. A later write cannot
turn a bound source back into `unbound`, rewind its cursor or substitute its session/range; it may add only its
own verified pane or forward lifecycle evidence. A mismatch leaves the intent and prior run in place and permits no adoption,
signal, stop, resume or replacement. This ordering applies equally to worker, reviewer and observer launch and
recovery, while the generic non-Codex launch path keeps its existing behavior.

A head adopted that way usually has no handle, because the tick that launched it did not survive to record one.
Its liveness is read from the launch-identity heartbeat and reported as such in terminal status. It is also
stopped that way, before review starts, on respawn, on a red review and on freeze, which is why the role's pid
file and `HeadRun` are kept in the record.

A stop the host did not confirm is not a stop: the tick reports a stop-unconfirmed outcome (degraded) and does
not launch a replacement until the previous head is confirmed dead. The same holds during reconciliation: a card
leaving the active cycle with an unresolved intent first has that head stopped and only then loses its record. A
claim mismatch is handled the same way: if someone else claimed the card, the tick first stops the unresolved
intent's head and only after a confirmed stop moves the card to Blocked and deletes the record. A freeze follows
the same order, so a card reaches the stopped-worker or stopped-reviewer list only after a confirmed stop;
otherwise the intent stays on disk and `resume` launches nothing next to a live head. When this happens, look at
the session manager: the head is alive and either the stop is refused or the head's process does not exit on
signal.

State from outside, without reading a transcript:

```bash
secretary status --json --instance INSTANCE                    # .dispatcher.observers
secretary dispatcher production-observe --instance INSTANCE    # .observers
secretary pause-status --instance INSTANCE                     # .observers, .stopped_observer
```

An observer row carries the sprint, the head profile, the state (`running`, `waiting`, `idle-grace`, `wake-deferred`,
`launching`, `deferred`, `stop-pending`, `pause-stop-pending`, `stopped-by-pause`, `pending`), pid liveness,
the launch count, the workspace, the handle-known and abandoned-handle flags, the time and kind of the last
action, the reason for a deferred launch, and a delivery object with its stage, fixed event high-water mark,
causal acknowledgement, deadline, retry state and external-failure reason. It also carries `wake_liveness`,
the versioned exact-HeadRun provider-progress episode, without terminal or composer text.

### An infrastructure bring-up outcome

A card that goes to Blocked because a head never came up is a different event from a card blocked
over its own work, and the pipeline says which one it is rather than leaving it to be read out of
the prose. The vocabulary is in
[Bring-up outcomes](PROTOCOLS.md#bring-up-outcomes); this is where to read it and what to do.

Where the class and the evidence are:

- on the card — the Blocked reason ends in a clause of the form `[bring-up outcome:
  class=infrastructure, cause=pane_never_ready, stage=claim, head=worker, attempt=ATTEMPT_ID]`
  followed by the sentence that the head never came up, so this is not a verdict about the card. The
  causes are `pane_never_ready`, `launch_aborted` and `host_unavailable` for the infrastructure
  class, and `workspace_contract` and `base_branch_contract` for the task class;
- in the tick — the outcome carries `failure_class`, `failure_cause`, a `failure_reason` that is the
  same string as the card's, and a `bring_up` object with the stage, the head, the attempt id and
  the host's own detail. A card refused by the broad-check contract preflight carries its refusal
  shape beside them under `contract_refusal`;
- in the audit — the transition's request id ends in `-infrastructure-blocked`. That token is where
  the class is durable, and it is what everything downstream reads;
- in the sprint — `secretary sprint show --ref sprint:ID` and `secretary sprint status --ref
  sprint:ID` carry `budget.uncharged.infrastructure_blocked`, and a newly launched observer's prompt
  says how many infrastructure bring-up outcomes are recorded and that they are charged to no
  threshold.

How it differs from a Blocked card that is the task's fault: nothing about the card was judged, and
often nothing was even built — a card refused by the contract preflight has no workspace and no head
at all. It spends none of the sprint's restart budget, so it moves neither the signal nor the hard
threshold and a bad night on the host cannot stop a sprint by itself. The task-class bring-up
outcome is the opposite case and the one to look for in the clause: `cause=workspace_contract` means
the checkout the card was requeued onto is gone or is not the worktree on the branch its claim
recorded, and `cause=base_branch_contract` means the card names an integration base the project
cannot integrate into (a predecessor's `pipeline/*` branch, most often) or a seed the remote does not
carry — neither of which a relaunch repairs, and both of which want a person.

What to do: read the cause and the detail, repair what they name — the pane, the head's resource,
the project's adapter, the checkout — and then move the card out of Blocked with a reason, the
ordinary way. Nothing does that for you. After an infrastructure outcome the dispatcher opens no new
attempt and schedules no return: the decision to retry or to block the sprint belongs to the sprint's
observer, and the card is only tried again once it is moved back, at which point it is claimed under
a fresh attempt id. A card standing in Blocked with an infrastructure clause is waiting for that
decision and not for a timer.

Before concluding that a head is missing at all, ask
[head-status](#head-status-in-a-live-workspace): a workspace with no visible pane is not a workspace
with no head.

## Checkpoint push

The push runs on the same tick but in its own window: every 30 minutes, fast-forward only, never a force push.
Before pushing, a remote listing compares the remote tip against the local HEAD: if it already equals HEAD, no
push is needed; if it is an ancestor of HEAD, the push runs. Git calls are non-interactive and time-limited so
an unreachable remote or a password prompt cannot hold the tick.

A push failure is fail-closed on the checkpoint but not on the work: the dispatcher keeps moving cards, local
commits continue, the reason and the growing lag are visible, and the next window retries.

`remote diverged` means the remote holds commits that are not local. The push stops, the alarm stays in `status`
and `doctor`, and no automation rewrites anything. If the cause was interleaving between a green publish and the
checkpoint, the next dispatcher tick reconciles the local instance checkout on its own and the pusher re-checks
the diverged state and clears the alarm fast-forward only. Manual work is needed when the remote holds history
that is in neither the reviewed branch nor the local checkpoint checkout:

```bash
git -C INSTANCE fetch origin
git -C INSTANCE merge --no-edit FETCH_HEAD   # or rebase, as appropriate
```

Once the remote is an ancestor of the local HEAD, the next tick pushes on its own and the alarm clears.

Freshness is visible in `dispatcher production-observe` under `checkpoint` and in `doctor` under checkpoint
freshness: last commit, last successful push, lag in commits and minutes, the reason the gate is blocked, and
the diverged state. The lag in minutes is the age of the oldest unpushed commit, that is, the real size of the
loss if the machine dies. `doctor` raises a finding on divergence, on a blocked gate, and on a lag above 60
minutes (two missed windows).

### A checkpoint blocked by a Product/Issue transaction

The checkpoint gate and the board export both refuse to run while a Product or Issue write is staged and
unfinished, naming the number of pending records. `transaction list` includes both released transaction
documents and typed Product/Issue pending events, so its request id, kind and ref are the supported way to
find a repair; no file under `board/product-issue-transactions/` or `board/pending-audit/` is ever moved by hand:

```bash
secretary product transaction list --data-dir DATA_DIR
secretary product transaction retry --request-id REQUEST_ID --data-dir DATA_DIR
secretary product transaction discard --request-id REQUEST_ID --data-dir DATA_DIR
```

`retry` is the first move: it resumes the operation where it stopped and commits its audit event. `discard`
is for a released transaction the backend never accepted; it reads the board first and refuses with
`live_write` when the row or the board comment of that request already exists. It is read-only for typed
pending events and likewise refuses them as `live_write`; retry the listed request id instead. A document
that is already outside the released journal comes back with `secretary product transaction adopt --path FILE`,
which files it under its own request id and removes the copy, after which `retry` and `discard` see it again.

### A checkpoint blocked by duplicate card references

`board export is not restorable: ... duplicate references` means publication stopped before replacing the
prior good normalized pair or touching the checkpoint Git index. Use the supported preview and exact-ID apply
commands in [Recovery](RECOVERY.md#repairing-historical-duplicate-card-references). Do not use `task show` to
choose a row: its compatibility rule intentionally selects one live row when an archived duplicate exists.
Do not edit normalized files or Kanboard storage. After apply, retry the normal managed checkpoint, verify its
remote SHA, and only then repeat the isolated recovery drill.

## Board column schema

Install creates the Pipeline columns and then refuses to reshape a board that already holds cards:
renaming a column in place would change what its cards mean, and removing one moves every card it
holds to the trash. A live board that predates a column therefore needs one explicit repair:

```bash
python3 -P -m secretary board migrate-assessment --instance /path/to/instance
```

It adds the `Assessment` column at position 5 of a board that carries the earlier six-column layout,
without moving, reordering or trashing a card and without renaming an existing column. It reads the
board transport from that instance. Every outcome is retryable: a finished board reports `unchanged`,
a run whose `addColumn` committed but whose answer
was lost leaves the six columns plus a trailing `Assessment` and the next run finishes that column
(`resumed`) instead of adding a second one, and any layout that is none of those three is refused
with all of them named. Every run proves that each card's column and position are unchanged before
it reports success. After it runs, install accepts the board unchanged.

The retired `triggered_agents pipeline setup` command is not a migration. Use the canonical
`secretary board migrate-assessment` command above for the explicit, audited repair path.

## An export whose sprint rows carry no observer

Every sprint row carries an observer value, closed rows included, and restore validates the whole
exported set before its first backend write. A row without the field is named and refused, and the
refusal does not guess why: an export can lack it because it is damaged or because it was taken
before the field existed, and nothing in the archive tells the two apart. Either way nothing of the
export reaches the backend.

The repair is the same for both. Open the export's `state/board/sprints.json`, add the value to each
named row, and restore again:

```json
"observer": {"kind": "head", "profile": "<profile>"}
"observer": {"kind": "none"}
```

Use `none` for a row that ran without an observer. A closed row whose head you cannot establish takes
`{"kind": "historical", "profile": null, "source": "migration_unknown"}`, which records that there
was nothing to recover; it is provenance, never a head to run, so an open row may not carry it. An
open row that names a head the installation's registry no longer has is refused the same way and
repaired the same way, by declaring a profile the registry does have. The forms are defined in
[Protocols](PROTOCOLS.md#the-declared-observer).

## Recovery

The Git-backed checkpoint and the full recovery sequence are documented in
[Recovery](RECOVERY.md#fresh-install-and-recovery). On a clean replacement host, bootstrap the pinned
runtimes and use `recover` rather than `install`:

```bash
sudo secretary bootstrap --instance-remote REMOTE --instance-dir INSTANCE --installation-user INSTALL_USER
sudo secretary recover --instance-remote REMOTE --instance-dir INSTANCE --installation-user INSTALL_USER \
  --recovery-phrase-file PHRASE_FILE
```

On an absent or empty target, these stock commands create a private sibling stage and request a
depth-1, single-branch, no-tags clone of the remote's current default branch. They validate origin,
branch/upstream and exact tip before atomic adoption. Timeout and interruption terminate the isolated
Git process group and discard both stage and operation-scoped credential capability. Do not retry into
a non-empty partial checkout left by an older release: inspect it, then remove that failed target or
choose a fresh `--instance-dir`. Existing repositories, including dirty or mismatched ones, are never
replaced or reset.

The isolated PO recovery drill should record the candidate SHA and supported command, elapsed clone
time, remote default branch and tip, local `HEAD` and `@{u}`, `--is-shallow-repository`, commit/object or
transfer counts, and absence of clone descendants and staging after an injected timeout. After advancing
the private remote, repeat the supported recovery and record the fast-forwarded tip, retained shallow
boundary and bounded added objects; repeat once unchanged for idempotence. Then continue through board,
memory, project, host-finalization and checkpoint push validation. Credentials, recovery phrases, helper
arguments and private remote details do not belong in the evidence.

Root recovery hands the restored instance and data roots to `--installation-user` at the named recovery
ownership barrier, before a restored mode-`0600` installation key reaches that user's Git or remote child.
Record only root/child numeric identities, ownership and file type/mode, never key material. The real
materializer must execute `head-registry` followed by `head-registry-checkpoint`. Prove its successful branch
against an isolated disposable Git destination; never enable the protected drill copy's production push URL.
The same barrier runs after restored pipeline state is written and on every partial or successful recovery
exit, covering the instance Git lock, recovery progress and dispatcher run-state roots. If final ownership
cleanup itself fails, report it separately while retaining the earlier actionable failure.

If checkpoint publication is disabled, unavailable or divergent, recovery reports
`checkpoint-publication` as degraded, exits non-zero, and retains the named local commit while completing safe
host, pipeline-state, reconciliation and ownership work. This is not permission to call the push or recovery
healthy. Repair only the destination or credential, then rerun the same recovery command. A compatible remote
fast-forwards to the retained commit; an independently advanced remote remains divergent, with both histories
preserved. Do not reset, rebase, force-push, delete progress, create an empty replacement commit or use an
ambient credential helper. `secretary upgrade`, dispatcher checkpointing and explicit checkpoint operations
remain publication-mandatory and fail closed.

The low-level `bootstrap --empty`, `restore-board`, `memory reindex`, `reconcile apply` and `restore-reconcile`
commands remain diagnostic primitives, not the main runbook. `restore-reconcile` intentionally exits non-zero
with `status: degraded` while a configured project checkout is unavailable and does not mark reconcile complete;
repair the checkout through `recover`, then rerun the diagnostic if it is needed.

`restore-board` holds the restore lock while it stages and batches normalized card obligations. If it reports an
uncertain card batch, rerun the same supported command without deleting backend rows, pending audit, restore state
or the request namespace. The rerun reconciles the current Task/Product/Issue inventory and metadata, skips every
proved row, and retries only absent or incomplete work. A duplicate reference or conflicting existing content is
not a cleanup instruction: preserve that evidence and investigate the named reference. Comments follow card
initialization; archived closure follows comment proof; post-close active-order reconciliation and a fresh final
parity snapshot remain the completion gates.

An error naming an oversized `create`, `metadata/state` or `closure` payload is a pre-write validation refusal,
not an uncertain batch. Reduce or repair the named normalized record before retrying; the refusal has staged no
new card obligation. Likewise, a named backend rejection is definite for the member fresh evidence still shows
absent or incomplete. Preserve any proved sibling rows and their audit records. Only the explicit `uncertain`
result calls for the ordinary ambiguity-safe rerun above.

Recovery prints a structured row for every configured project. `failed` rows make the aggregate status
`degraded` and the command exits non-zero, but board, memory, run-state, safe host finalization and ownership
handoff still complete. The host contract preserves an unavailable project's existing managed registration
but defers checkout-dependent creation. Dispatch may inspect the binding, then refuses it before any worker,
reviewer process or project worktree for that binding starts. Unknown ids and registered inventory-only projects
are separate configuration outcomes. A sprint observer is not project-dependent: it consumes the dedicated
observer repository and is unaffected by the sprint's canonical repository roots and project-id reservations.
Global automations do not schedule project work. Fix the reported external cause, then rerun the same
`secretary recover` command: matching completed board and
memory phases are not imported again, existing repositories remain untouched, and only missing projects and
dependent host state are retried. Persisted project rows are diagnostic only; filesystem checkout truth
drives retry. Do not edit `recovery-progress.json`, project registry files or Git
credential files as a recovery procedure.

## Optional cold archive

`backup create` and `backup verify` are a manual tool for dumping raw material, not a recovery contract. There is
no timer, no offsite transfer and no `doctor` gate for them.

```bash
python3 -P -m secretary backup create --instance INSTANCE --kind both
python3 -P -m secretary backup verify ARCHIVE.tar [--strict]
```

`create` writes a plain tar into `backups/` (`core`, `full` or `both`), unencrypted. `verify` returns `0` on
success, `1` for findings or strict warnings, and `2` for an unreadable archive. Restoring from such an archive is
still available through `secretary restore ARCHIVE.tar` for compatibility. The archive is not a recovery contract
and does not affect `doctor` or readiness.

## Auto-merging green cards

A green verdict on a card whose sprint declares a concrete observer does not merge on its own. It
parks the card in Assessment (see [Tasks](PROTOCOLS.md#tasks)) once the mechanical gate is green, and
the merge below runs on the tick that performs a recorded `release` decision. A red or pending gate
still resolves in Validate, so a card only reaches Assessment with nothing mechanical left to decide.
A card with no observer to release it merges on the verdict's own tick, as below.

### Dispatcher-owned exact-SHA gate receipt

An executed local or GitHub mechanical gate can leave a valid dispatcher-owned exact-SHA gate
receipt only when it names the exact commit SHA being judged, its base SHA, completed terminal
checks, completion time and check-set digest.
Its ownership, attestation and travel are defined in [Receipt names](PROTOCOLS.md#receipt-names). A
reviewer or observer may suppress a
routine repeat of the already-attested broad validation on that unchanged SHA, but must still inspect
the diff and acceptance criteria; focused reproduction, mandatory CI and the fresh pre-merge gate remain
independent decisions.

Do not carry a dispatcher-owned exact-SHA gate receipt to a new commit, a later lifecycle stage, or
a different check set. Missing evidence, `gate_mode: none`, and noop execution are explicit absence
of a broad-suite attestation, even when they preserve dispatcher control flow. In those cases,
obtain the focused or broad validation the decision needs instead of describing a suite as already
passed.

A release the dispatcher cannot carry out takes the card to Blocked with the failure on it, the
same as any merge that could not land before Assessment existed. It never sends the card back for
rework. Recovering a release that failed part-way through, so that the card can be decided again,
is a separate card.

On the release, the production dispatcher takes the card to done without a manual merge:

1. Push the worker branch to the default branch, fast-forward only. If the default branch diverged, the push is
   rejected; the dispatcher neither forces nor resolves the conflict itself.
2. Fast-forward the project's local checkout onto the new default-branch tip. For the product's own repository
   this is a self-deploy: the dispatcher merges and immediately pulls the change into the checkout it runs from.
   A card whose base branch is another card's branch lands on that base, not on the default branch, and the
   checkout is still only ever refreshed from the default branch. There the refresh is a courtesy for the next
   worktree: it cannot fast-forward when the default branch has moved on since the base was cut, and a card whose
   branch already merged is not sent back for rework over it.
3. Tear down the workspace: stop the worktree's terminals (worker, reviewer and their child processes) and remove
   the worktree.

For the private instance repository, publishing happens under the same writer lock as the checkpoint. The
dispatcher publishes only the reviewed branch and locally known checkpoint history: the remote tip must be an
ancestor of the worker branch or of the local instance checkout. Foreign remote history stays a manual runbook
case, with no auto-merge into a green card. After a successful publish the dispatcher merges the remote default
branch into the local instance checkout, so a checkpoint commit that appeared between preflight and publish is
preserved by an ordinary merge commit alongside the feature commit. If the tick died after the remote publish but
before the local merge, the next tick repeats the done path idempotently.

Teardown happens only on this done path. While a card is parked, and on the rework decision after a
red review, the workspace and its branch are left untouched so the worker can continue in the same
worktree.

Kill switch: `SECRETARY_DISPATCHER_AUTOMERGE=off` disables the push and fast-forward steps entirely. The card
still reaches done, but the branch stays unmerged and needs a manual merge. The default is on.

## Pausing the pipeline

An emergency stop is one product CLI command with two modes:

```bash
python3 -P -m secretary pause drain  --instance INSTANCE --reason "why"
python3 -P -m secretary pause freeze --instance INSTANCE --reason "why"
python3 -P -m secretary resume       --instance INSTANCE
python3 -P -m secretary pause-status --instance INSTANCE
```

`drain` stops the tick from claiming Ready cards, but cards already in flight finish their cycle: the worker
finishes writing, the reviewer judges, a green branch merges. Use it to stop the inflow without cutting work off.

`freeze` does that and also stops live worker, reviewer and sprint-observer heads, after which the tick advances
nothing. Workspaces, worktrees and uncommitted work are untouched: only terminals are stopped. Use it when the
host has to be freed right now (a backup, a reboot, an incident).

`resume` lifts the pause, brings the frozen worker and reviewer heads back up in the same workspaces, and gives
the waiting watchdogs a fresh window so a long pause is not later read as a silent head. A card whose head managed
to report during the freeze gets no head: the next tick moves it on the report already recorded. `resume` does not
launch observers itself; it clears their pause mark and the next tick brings them up through ordinary
reconciliation. Under `drain` nobody touches a live observer and no new ones are launched; a sprint opened during a
drain gets a deferred record with its reason and is visible in every summary.

If the host refused to stop an observer during a `freeze`, the `pause` response carries that as a separate warning
listing the sprints, and the record stays in `pause-stop-pending` with its handle. Reconciliation does not run
under a freeze, so the frozen tick itself retries the stop and reports the result per sprint. If the host refuses
again, the tick goes degraded — a non-zero unit exit and a red health line — because the freeze is sitting on a
head it could not stop. A head that survived until `resume` is not launched again; the next tick simply sees it
alive.

Switching mode in flight is forbidden: `resume` first, then pause in the other mode. A repeat pause in the same
mode is a no-op.

The flag lives in the live data plane, next to the state of the dispatcher that is actually running:
`<data_dir>/dispatcher/pause.json`. Every product tick reads it from there. Background roles still read a legacy
flag in the pipeline workspace, so the pause additionally writes a mirror there and `resume` removes it — but only
if the pause put it there. A foreign legacy flag is neither overwritten nor deleted.

### Pause or breakage

`pause-status` shows the product dispatcher's state: the mode, who set it and when, the path to the flag file, and
a line per card describing its heads:

- `running` — the head is alive;
- `stopped-by-pause` — the pause stopped the head, the workspace is intact, `resume` will bring it back;
- `not-running` — there is no head and the pause did not stop it: either a card nothing has reached yet, or a real
  break to be investigated as one (see [Waiting watchdogs](#waiting-watchdogs)).

A tick during a freeze answers `skipped` with the reason and a snapshot of the pause, rather than staying silent,
so "nothing is moving" in the log is always distinguishable from a stalled dispatcher. The health probe answers ok
with the same snapshot during a freeze.

A frozen tick still writes and pushes the checkpoint: a freeze stops cards from advancing, not durability.
Otherwise a long pause would be a hole in the snapshot history and a growing push lag exactly where a recovery
would be needed. Such a tick's response carries its checkpoint and push fields even though it has no actions at
all.

### A freeze that lifts itself

A freeze set by an automation has a TTL. If the pause actor is on the configured allowlist, then after the
configured number of seconds (45 minutes by default) the next tick calls `resume` itself, the same way an operator
would, bringing heads back up with fresh watchdog windows. Without that, a `secretary backup create` killed before
its cleanup would leave the dispatcher frozen forever. A freeze from a person (any other actor) never expires: the
maintenance window is lifted by whoever opened it. Setting the TTL to zero disables auto-resume entirely.

`pause-status` answers in its `auto_resume` field whether the pause will lift itself: `fresh` (it will, the TTL has
not expired), `manual-or-unknown-actor` (it will not, a person is holding it), `disabled` (auto-resume is off). The
response of a tick that lifted a pause by TTL carries the pause's age and the lists of heads it brought back, so a
TTL lift is confused with neither a manual resume nor a break.

## Waiting watchdogs

The dispatcher waits for a head at two points: waiting for the worker report (card in progress) and waiting for the
review verdict (card in validate). On every waiting tick it compares the stored handle and pane id of the active
attempt against the session manager's terminal list. The session manager may hand a new handle to the same pane, so
the pane id is the stable token for both worker and reviewer. A missing or disconnected pane immediately triggers
the same path as a stall: one respawn in the same workspace, then Blocked with a signal to the operator. An answer
that the runtime is unavailable does not count as a dead head: the tick reports that and leaves the card alone
rather than acting on an inventory error. Such a tick proves no progress either, so the ordinary waiting ceiling
keeps running as a fallback and starts the usual respawn/Blocked path when it expires.

A present pane is not sufficient on its own. The inventory carries a last-output timestamp, and the dispatcher
tracks it for the stored pane specifically, not for the whole workspace. If the output does not move before the
ceiling, the same watchdog fires. That catches a login screen and any other live but idle head, while output from an
unrelated shell in the same worktree does not mask the problem. The terminal title and a running status are
deliberately not used: a head rewrites its own title, and a running status can stick after a silent exit.

A third case: the pane stays connected, but the session manager keeps its own interactive workspace shell in it even
after the head's process has exited, since that shell types the head command line by line and does not close with
it. Returning to the shell prompt updates the last-output timestamp once, so by the first two signals such a head
reads as "there was output, then silence" and would wait out the ordinary long ceiling. What separates these cases
without reading session text is the launch identity the shell writes before exec: the launch command records its own
pid and then `exec`s the head, replacing the process image without a fork, so the recorded pid stays the head's pid
for its whole life. The atomically replaced JSON record carries format version 1, pid, Linux boot id, process start
ticks, HeadRun id, role, card or sprint binding and, once the pane is known, its leaf. On each waiting tick the
dispatcher compares all of those facts before it probes or signals the process. Terminal create can return before the
shell reaches its writer, so the returned pane leaf is first handed off atomically beside the heartbeat; the writer
uses that handoff in either ordering before readers require the leaf. A matching live record confirms liveness; a
dead record takes the missing-pane path; missing and unreadable records retain the grace and output fallback; a live
mismatch is degraded and never authorizes a pane close, workspace stop, signal, adoption or replacement. The file
lives outside the workspace, like report and verdict bodies, under `SECRETARY_DISPATCHER_BODY_DIR` (default `/tmp`);
respawn deletes it and its leaf handoff before a new launch so a dead predecessor's record is not read before the new
head overwrites it.

If the identity probe confirms the head's process is alive, that is a positive liveness signal rather than merely an
absence of proof of death, and silence from it proves nothing. The short first-output window never applies to such a
head: printing nothing right after launch is exactly what the heartbeat answers. Whether the long ceiling applies
depends on the work state below. While the file does not exist yet — a fresh launch has not run its write, or the
runner does not provide this signal at all — that is read as neither death nor confirmed life, and the tick keeps
using the ordinary last-output checks. The only runner without the signal is the raw command override, which
substitutes a command bypassing the head registry and therefore gets no heartbeat wrapper. Its PID is never
promoted into a synthetic identity; it keeps the grace and output fallback precisely because there is no way to
confirm liveness independently of pane output.

Every fresh progress signal starts a new waiting window. So the ceiling measures how long a head has been silent,
not how long a task has run: a head producing output is not respawned merely because its card is old. If the
last-output timestamp is known but has not moved past the head's launch time, a separate short window of 180 seconds
applies, catching a login screen and any other head that printed nothing after launch. After the first output only
the long ceiling applies, because a head is entitled to think for a long time. A TUI head on an alternate screen may
not update the last-output timestamp, so for those profiles the signal is supplemented by the modification time of
the session rollout file, which is tied to the worktree rather than to a specific pane. That is a deliberate
compromise for the alternate screen: it inspects file metadata only and never reads session text. The first breach
is one respawn of the same head in the same workspace; the second moves the card to Blocked with a signal to the
operator.

### A card in an active column with no worker at all

Before any of the wait handling below, the tick asks whether there is a head to wait for. A card that
arrived in an active column with nothing running — most often a raw board move out of Blocked back
into In progress, but also a bring-up whose tick died before it bound anything — is settled in that
same tick instead of waiting for a report nobody will file. What you see in the outcome:

- `headless-worker-replacement-launched` (ok) — a replacement was launched on the retained checkout.
  The outcome names the workspace, the branch, the candidate SHA and whether the tree was dirty, and
  the same line goes on the card. Nothing was recreated from base, re-seeded or reset.
- `headless-worker-recovery-refused` (blocked) — the card went back to Blocked with a
  `recovery_error` on it: `workspace_missing`, `workspace_unbindable`, `workspace_unreadable`,
  `candidate_unknown` or `round_already_answered`. The first four say the retained checkout could not
  be bound to this card, and the repair is on disk, not in the dispatcher. `round_already_answered`
  says the checkout's round already has an accepted report, so there is no worker work to hand out:
  returning that card to an active column was the wrong move, and validating the unchanged candidate
  is a different path.
- `orphan-worker-heartbeat-unbound` (degraded) — a live heartbeat sits at this card's worker pid path
  and cannot be bound to it. Nothing is launched beside it and nothing is signalled. Find out whose
  process it is before touching the card.

Returning the same card a second time gets a second answer: the refusal is keyed on the episode, not
only on the card, so it moves the board and comments every time it is needed.

While such a card is unresolved, `secretary status` marks its attempt row `degraded` and fills in
`headless` (record state, missing handle and heartbeat, how long it has been waiting, the retained
workspace, branch, dirty flag and candidate SHA); the sprint summary repeats the refs under
`degraded_cards` — `secretary sprint status --ref <sprint>` reports the same map, from the same
production state. A card sitting in In progress is not on its own evidence that anything is running.

A confirmed pid says the process is running; it does not say the head is doing anything. A head that finished its
turn and went back to its prompt holds the same live pid as one that is thinking, which is how a card could sit in
`waiting-worker-report` forever with the work already done: the report call was never made, or it was made with the
command of a round that is over, which the task protocol answers as that round's retry and which therefore leaves
nothing on the card and no error the dispatcher can see. So on a pid-confirmed head the dispatcher also asks the
session manager whether the pane is ready for input, the same readiness the prompt delivery waits on. A pane that is
working is never ready, so this never touches a head that is thinking. Readiness that holds for the idle window
(5 minutes by default) while nothing has landed for the round being waited on takes the ordinary path: one respawn,
then Blocked. A pane held in a dialog counts the same way: nothing in the pipeline answers a dialog, so that head has
stopped as surely as one at its prompt, and the comment says which of the two it was.

Before any of that happens, a worker head that is still a live conversation is asked for the report once. The
commonest reason a head is idle with nothing on the card is that the work is finished and only the report call was
missed, and replacing that head throws the work away, so at the first confirmed-idle boundary of a report round the
dispatcher types one reminder into the worker's own pane: run the report command in the TASK.md you already have,
for this generation. It changes nothing else. The task document, the report bodies, the generation and the ownership
of the checkout are exactly what they were, and a report that follows takes the ordinary path — result verification,
the mechanical gate, then review — because a commit, a push or a green test run has never been a report and is not
one here either. The tick is `worker-report-prompted` and degraded, and the reminder is written on the board.

That reminder is bounded per report round and is durable before it is sent. A second confirmed-idle episode in the
same round finds it spent and takes the respawn-then-Blocked path above, so the ladder is one prompt, one
replacement, then the operator. A head nothing can be typed into — for example, one adopted without a pane identity
or one whose interactive session disappeared — is never prompted and takes that path immediately. Legacy Codex exec
records are normalized to TUI before launch or rejected by registry validation; they cannot create a one-shot worker
on this path. A send that is refused, or that cannot be
confirmed to have landed, is not retried and not trusted: the round continues on the same path, through the confirmed
stop that protects the checkout, and if the host will not confirm that stop the tick ends with nothing opened beside
the head. A dispatcher that dies between the intent and its confirmation leaves a round that reads as already
prompted, because typing a second prompt into a live conversation is the one thing the bound exists to prevent.

That leaves the heads nothing can be read about: one adopted from a launch intent whose pane identity was never
persisted, and one whose pane binding the session manager has lost, where the inventory still lists the pane but the
readiness probe is refused. Neither is a working head and neither is a stopped one, so neither answer is invented for
them. They fall back to the long ceiling, the same fallback a runtime with no signals at all gets: silence for the
whole ceiling is one respawn and then Blocked. The respawn gives that card a head with a fresh pane and a fresh
heartbeat, which is also how the identity comes back.

For a worker that means the round does not move. The same TASK.md is written back into the checkout with the same
report commands and the same generation, and the head is pointed at them again; the report the dispatcher is waiting
for is still the one the operator will see land. The respawn comment names the generation, and the Blocked reason
names it too along with the fact that a respawn was already tried. A card blocked this way has the worker's work in
its workspace: the operator's question is why the report never arrived, not what the head was doing.

That bounce is a degraded tick, unlike a stall the timing ceilings catch. A head that is alive, idle and has
delivered nothing is the pipeline failing to move a card rather than the watchdog doing its job, so the tick reports
`degraded` with the trigger as its reason, the production telemetry records it as unhealthy with the card and the
round in the diagnostic, and `triggered_agents health` shows the pipeline line red until a healthy tick follows. The
operator therefore sees this one round before the card blocks, rather than finding a Blocked card hours later under
an unbroken run of green ticks. The Blocked that follows is not itself degraded, on the same rule every other
blocked card follows: the board carries the reason and the steward reports it as a `new_blocked` signal.

A respawn writes a comment on the board, so the operator can tell a first stall from a card whose head has already
been restarted, without waiting for the final Blocked.

- `SECRETARY_INITIAL_OUTPUT_STALL_SECONDS` — the short first-output window, default 180 seconds.
- `SECRETARY_REVIEW_VERDICT_STALL_SECONDS` — the ceiling for a verdict after first output, default 5400 seconds.
- `SECRETARY_WORKER_REPORT_STALL_SECONDS` — the ceiling for a report after first output, default 21600 seconds.
- `SECRETARY_HEAD_IDLE_STALL_SECONDS` — how long a head ready for input with nothing delivered is left alone,
  default 300 seconds.
- `SECRETARY_BRINGUP_DEFER_ATTEMPTS` — how many bring-ups of one role's head are deferred over a pane that is not
  ready for its launch prompt before the card is blocked over that pane, default 5 attempts.
- `SECRETARY_LAUNCH_DELIVERY_MAX_ATTEMPTS` — how many ticks a live head may hold its card while its pointer is
  still unaccepted before it is stopped and launched again, default 5 attempts.
- `SECRETARY_TUI_PRE_DELIVERY_TIMEOUT_S` — how long a pane held in a dialog is given to leave it inside one
  delivery, default 45 seconds.
- `SECRETARY_TUI_MODAL_ANSWER_ATTEMPTS` — how many times the one known update modal is answered on screen before
  the delivery is refused, default 2.

All five are read at check time; garbage or a zero value falls back to the default, so a typo in a unit file does
not stop the dispatcher from starting.

A bring-up can also fail before the head has said anything at all: the pane it was launched into is working, is
held in a dialog the head cannot leave on its own, or is still starting up. The launch
prompt then goes nowhere, and the pane is closed behind it. That is not a failed round. The card keeps its claim and
its record, the tick reports `worker-launch-deferred` or `review-launch-deferred` with the pane's state and which
attempt it was, and the next tick makes the same bring-up again. Once the attempts above are spent the card does go
to Blocked, and the reason names the pane and the state it stayed in rather than saying the bring-up failed. A probe
Orca does not answer is deliberately not deferred: a pane nothing can ask about is not a busy pane, and it takes the
ordinary failure path immediately. That Blocked card carries the infrastructure class of the bring-up
vocabulary: the spent ceiling ends the waiting and says nothing about the card's work. See
[An infrastructure bring-up outcome](#an-infrastructure-bring-up-outcome).

A head writes report and verdict bodies to a file outside the workspace
(`/tmp/secretary-report-<ref>-<round>.md`, `/tmp/secretary-verdict-<ref>-<round>.md`, with the directory overridden
by `SECRETARY_DISPATCHER_BODY_DIR`) rather than assembling them inline in a shell: some agent runtimes reject
commands containing `rm`, and quotes or backticks in the body break the call. The file is left in place and the head
does not clean it up, which is why the round number is in the name: otherwise a second reviewer would pick up the
first one's body.

The round number is also part of the verdict request id. The attempt id lives for the whole attempt and does not
change across review-red, rework and report-done, so without the round a second red inside one attempt would look
like a replay of the first to the task writer: no comment written, the CLI still answering "verdict recorded", the
reviewer exiting, and the card standing in validate until the watchdog.

Which report ends a worker round is decided by that id, not by the comment. The board comment reads `[report:done]`
whoever filed it and for whichever round, so the dispatcher matches the report against the round it is holding
through the request id the audit recorded with it. A report filed under some other id is written to the card and
answered normally by `secretary task report`, and it moves nothing: the round it was meant for is still open, the
head is bounced once and the card blocks if the round stays unreported. Reporting on behalf of a worker by hand
therefore means copying the command out of that worker's `TASK.md`, ids included, not writing one of your own. The
id names the attempt as well as the round, so a card retried through Ready starts with a clean slate: the reports of
the attempt that was blocked stay in the audit and cannot end a round of the new one.

Which ids belong to the open round comes from a hidden `<!-- report-round generation=N ids=... -->` line the
dispatcher writes at the end of every worker `TASK.md`, not from the report commands printed above it. The card
description is copied into that document unchanged, so a `--request-id` token that happens to appear in a spec, an
example or an operator note is prose and names no round: a report committed under it is written to the card and
moves nothing, exactly like any other id the round did not issue. Neither the hidden line nor the round number in
it is edited by hand; a checkout with no readable document falls back to the ids the dispatcher would issue itself,
which bounces the head once onto the current command.

A report the audit could not record is not a report yet. `secretary task report` answers `audit_pending` when the
comment reached the card and the audit write did not, and the card stays where it is until that is repaired: run the
same command again, ids and body unchanged, and it commits the pending event and answers `replayed`. `secretary task
reconcile-audit` repairs it too. An unrepaired one shows up as an ordinary unreported round, so the head is bounced
and the card eventually blocks with the report visible on the card, which reads as the audit failure it is.

## Background-role telemetry

```bash
python3 -P -m triggered_agents health
```

One line per role: whether its timer is active and how fresh its last healthy tick is. Expected state comes from the
`host.components` section of the `instance.yaml` bound to the process, using the same component names and
`dispatcher-production` mapping as host reconciliation. An omitted component is enabled; an explicit `enabled: false`
prints `DISABLED` and is neutral, so it neither requires a timer nor makes the command non-zero. An unreadable or
invalid installation configuration prints an explicit error rather than falling back to the checkout that imported the
command. A non-zero exit means at least one enabled role is red or the installation configuration is unavailable.

The sources are the live data plane, not a checkout:

- `scripts/secretary-agent-gate.sh` preserves one role-local environment and one exit-code protocol
  for every role, while routing steward and retro through `secretary.dispatch.standing_agent`.
  Curator remains on `triggered_agents`. The steward deep-sweep variant stays ungated and uses the
  same Secretary root; cleanup-only and terminal finalizers retain their existing zero-side-effect
  and lifecycle paths.

- curator, steward and retro write a run log through their shared agent state, that is, under `$TA_STATE/<agent>/`
  or, when that variable is unset (as it is in the packaged units), under the data directory. Healthy means the last
  event that answered, that is, one whose result is neither `error` nor `board-unreachable` (the record of a tick the
  gate deferred because the board never accepted a connection): the precheck writes one of those every tick until the
  board and environment come up, so by the raw last event a dead role would look forever fresh.

Curator harvest is bounded before either Markdown or JSON output. Its environment controls maximum signal
turns, input bytes and sources (TA_CURATOR_MAX_TURNS, TA_CURATOR_MAX_INPUT_BYTES,
TA_CURATOR_MAX_SOURCES), with record/row and personal-memory caps for source reads. Discovery decorates every
transcript and personal-memory source with a route from the selected instance's canonical project registry:
every valid `id` plus absolute `repo` binding routes its resolved checkout to that `id`; a safe optional
`orca_binding` additionally routes its `<workspaces root>/<orca_binding>/` tree, including recorded descendants
whose ephemeral worktree has since been removed. Optional absolute `curator_roots` name ad-hoc historical checkout
trees for input routing only; they neither register an Orca workspace nor authorize execution. A missing binding
name never invents a workspace route.
Directory boundaries are exact after path normalization, so a prefix, relative alias, unreadable binding, missing checkout,
ambiguous match or unregistered cwd is `unknown`, never guessed. Dispatcher tokens a reference such as
`sprint:1412` as `sprint-1412`, so observer workspaces under `workspaces/observers/sprint-<token>` restore the
canonical `sprint:` prefix. A readable sprint with one registered reservation routes to that project; two or more
distinct registered reservations route to the explicit `review:po` triage selector. Malformed, duplicate, empty or
unregistered reservations remain `unknown`. Installation-wide sources are explicitly `global`.
`curator harvest --project <canonical-id|review:po>` filters those routes
before budget selection; no selector is the explicit all-backlog mode. Its pending record signs that selector, so
neither replay nor advance can cross from a selected project to another project or all-backlog invocation.
`curator backlog [--project <canonical-id|review:po>] [--json]` is read-only metadata: deterministic route/head groups with
source counts and timestamp bounds, never transcript or personal-memory text. Harvest, precheck and advance share
one cursor-settlement transaction: a curator-local advisory flock serializes watermark.json and pending.json without
taking the agent lifecycle lock. A selected fact-bearing batch is stored as versioned, identity-bound pending.json
and replayed exactly until advance checks its identity, selector and starting cursors. A scan with only complete
non-emitting rows advances its cursors immediately under that lock and writes no pending.json, so a zero-input
prompt has nothing to advance. A legacy line-based watermark.json from the disabled installation remains a
supported read input and converts a source to its byte cursor only after that source is selected and advanced.
An unversioned, stale, foreign, corrupt, or cursor-only pending file is deliberately refused and left untouched.
Discovery excludes only the current curator workspace/session, not the Secretary checkout or sibling worker,
reviewer and observer workspaces. Complete non-emitting or oversized rows are included in a scan cursor. An
incomplete trailing JSONL row is source-local: only its uncommitted tail remains unwatermarked, while complete
prefixes and independent sources settle normally. Precheck reports a source-local partial tail rather than a
role-wide clean skip, and retries that source from its last complete cursor after the writer completes the row.
Precheck tries this transaction without waiting: exit 102 is a successful deferred tick, and the gate performs
neither dispatch nor cleanup. A process death releases the flock, so its lock file never needs stale-PID repair.
- the `pipeline` line is built from the production dispatcher's tick telemetry in its production state file. The
  dispatcher writes it at the end of every terminal tick: time, healthy or degraded, and diagnostics (step, reason,
  error codes). A tick that ended degraded colours the line by itself; the previous healthy tick does not vouch for
  it. Degraded is not only a caught exception: if a tick's action reported degraded or failed, the tick is terminally
  degraded too, and its diagnostics are recorded and reach the health line. A card moving to Blocked does not hurt
  tick health: that is the dispatcher working normally, the reason is on the board, and the steward sees it as a
  new-blocked signal. Freshness of the last healthy tick is checked separately, for the case where ticks stopped
  being written at all. A pause freeze is a deliberate stop and is recorded by a healthy tick; the exception is a
  frozen tick that again failed to stop an observer head, which is an unperformed operation and therefore terminally
  degraded. A tick that died with an exception (an unreachable board fails the very first task read) writes a failed
  record with the error code, otherwise the line would stay green on the previous tick until freshness expired. A
  tick that never got as far as checking its right to the state (the singleton lock was taken, or the mutation guard
  refused another owner's state) writes nothing: it is visible by healthy ticks no longer appearing.

Readers resolve the path to dispatcher state exactly as the dispatcher does: an explicit `--data-dir`, defaulting to
`SECRETARY_DATA_DIR`, else `data_dir` from the instance, which is the only thing the packaged unit passes with
`--instance`. One rule for everyone: an installation or drop-in that sets `SECRETARY_DATA_DIR` in `runtime.env`
moves both the dispatcher's writes and the health reader onto the same data plane, so a reader cannot look at a file
nobody writes. The dispatcher's unit takes `runtime.env` wholesale through `EnvironmentFile`, and the variable
reaches role processes through the role-environment allowlist, since it is the address of the data plane rather than
a secret. A configured `data_dir` may be absolute or relative; the latter is resolved from the containing
`instance.yaml`, never from the process working directory. `~` is expanded before either form is used.

A continuous run of unhealthy ticks is one **incident**. An unreachable board fails every tick for as long as it
lasts, and that is one breakage with one cause and one moment of ending. The dispatcher keeps it in its tick
telemetry: the first unhealthy tick opens a record (id, open time, and the tick reason in full with its errors and
degradations) and moves an incident counter, each following unhealthy tick only extends it, and the first healthy
tick closes it into a recovery record and moves a recovery counter.

The steward's pipeline-ticks signal reads the same telemetry, and its unit is the incident, not the tick: one
unhealthy event per incident (with the opening reason and the number of failed ticks) and one recovered event for its
recovery. Deduplication uses the monotonic incident and recovery counters, so an ordinary tick between two steward
runs does not swallow an event the steward has not seen yet, and a repeat precheck or scan before the advance, along
with new failures inside the same incident, does not open a second external incident. A first run with no counters in
its watermark takes the current values as a baseline rather than replaying what is already in state. The baseline is
saved by that same run, so a quiet hour that never reaches the advance does not leave the counters empty and the next
failed tick is not read as a first scan and silenced.

An unhealthy hit also carries `retained_window`: `degradations` is a deterministic list grouped by `step` and
`action`, and `errors` is one grouped by `code`. Each item has its retained diagnostic occurrence `count` and sorted,
unique affected `refs`. This complements, rather than replaces, the newest incident's opening tick, `cause`, and
`incidents` count. The telemetry keeps only a bounded diagnostic list per tick, so the summary groups every readable
item in the retained unhealthy ring but never invents a class or ref for a diagnostic that its original tick count says
was not retained.

Counters only mean something inside one telemetry history, so the dispatcher keeps a `generation` next to them: an
identifier issued once and never changed afterwards. The steward's watermark stores it with the counters, and as soon
as the generation differs (a restore from a backup, a rebuilt installation, a manual edit) the steward gets a
telemetry-reset hit and re-reports what the new history holds. Counters alone would not be enough, because a new
history can land on exactly the numbers the watermark has already seen and its events would silently deduplicate. A
counter that moved backwards still counts as a reset on its own, which is the only signal available on an
installation whose dispatcher does not yet write a generation.

The same scan's resource-flip signal reads the production dispatcher's cache of readiness verdicts, the same file the
head-readiness check writes before launching a head, resolved by the same path contract as the tick telemetry. The
steward runs no probes of its own: they cost tokens and would describe a check the dispatcher never saw. An unreadable
or missing cache leaves the previous baseline in place rather than clearing it, otherwise a flip would be lost on the
first successful read.

## Units

The current templates and what they are for are documented in
[packaging/systemd/README.md](../packaging/systemd/README.md). Units are rolled out by
`secretary reconcile apply`; manual installation is neither needed nor a source of ownership.

The production dispatcher timer runs a one-shot tick. Memory, curator, steward and retro must each have exactly one
scheduler owner.

## Upgrade

`secretary upgrade --instance <dir>` pulls a new product version and re-materialises the installation onto it. It is
idempotent: a repeat run on an up-to-date host does nothing.

```bash
secretary upgrade --instance INSTANCE --dry-run   # decide everything, write nothing
secretary upgrade --instance INSTANCE
```

The steps, in order; each prints `changed`, `unchanged`, `skipped` or `failed`, and the first failure stops the run:

| step | what it does |
| --- | --- |
| `pull` | `git fetch` plus `merge --ff-only` of the product checkout. A dirty checkout is refused. |
| `registries` | read the selected checkout's skill manifest, this installation's optional overlay and the head canon, and decide the whole skill delivery; a registry that cannot be read or cannot be delivered stops the run here, before the first write |
| `dependencies` | reinstall into the virtualenv if the pull moved the dependency manifest |
| `head-registry` | generate `heads/heads.yaml` from this installation's canon plus `heads/source.yaml`, naming that canon, its owner, the checkout and revision it came from, and the snapshot digest |
| `head-registry-checkpoint` | commit only the generated pair under the shared instance-repository writer lock and fast-forward publish it; an unavailable or diverged remote stops the upgrade with the retained local checkpoint named |
| `role-skills` | `role_skills sync` into the shells' skill directories |
| `role-worktrees` | fast-forward the role worktrees onto the base branch |
| `host` | `reconcile apply`: units from `packaging/systemd` plus session-manager registrations |
| `automations` | create or repoint session-manager automations from `automation.toml` |
| `memory` | restart the memory service if its code, dependencies, unit or shipped pack changed, then complete a bounded launch-authenticated MCP `memory_list` read |
| `verify` | a repeat dry run: the second rollout must be a no-op |

Flags: `--no-pull` (re-materialise only), `--base-branch`, `--product-root`, `--runtime-user`,
`--json`.

### Upgrading from another checkout

`--product-root` names the checkout to install. Every step then reads that checkout and nothing
else: its `skills/manifest.toml` and `skills/roles/` tree, its `packaging/systemd` templates, its
`src/triggered_agents/agents/*/automation.toml` specs, the role worktrees it declares, and its head
canon when the installation owns none. The checkout that happens to be running the `secretary`
module has no say, which is what lets one installation be moved onto a candidate version, and what
lets a second checkout install a host at all.

`secretary role-skills audit|sync --product-root <checkout>` takes the same argument on its own, for
delivering skills without running a whole upgrade.

Without `--product-root`, an install or upgrade materializes the configured checkout —
`TA_SECRETARY_REPO`, else `$HOME/secretary` — and not the checkout the command was typed in. A
candidate checkout is the normal place to run the upgrade from, so the running module deciding
would make the working directory pick the version a host ends up on. `secretary install` and
`secretary recover` select the same way and check the result before they use it: a path with no
product in it is refused by name, rather than surfacing later as a missing file inside a directory
nobody meant to install from. A first install out of a checkout that is not `~/secretary` therefore
names it with `--product-root`.

The checkout an upgrade selects is written into the dispatcher unit as `TA_SECRETARY_REPO`, and the
dispatcher renders it into every head it launches. Orca creates a head's terminal, so it inherits
nothing from the unit and a runtime.env line cannot take the name back: after an upgrade from a
candidate checkout, a worker, reviewer or observer imports the product the installation was moved
onto rather than whatever `$HOME/secretary` still points at.

The `registries` step reads both registries before the first materializing write, which is why it
runs directly after the pull and ahead of `dependencies`: a `pip install -e` into the checkout's
`.venv` is already a write into the version being installed. A product manifest or instance overlay
that is malformed, unreadable, a directory or a dangling link, and a `heads/heads.toml` in any of
the same states, stops the run there and names the file. So does a manifest that parses and still
cannot be delivered: a declared skill with no `SKILL.md` beside its manifest, two skills claiming
one skill directory, overlapping target roots, or a command entry point whose path in `bin` is
occupied by something this registry does not own. No dependency install, head snapshot, pin, role
worktree, skill copy, command link or host resource is written on that path, so a bad hand edit
leaves the installation exactly as it was.

### Path precedence

The product ships no absolute path of its own. Each of these resolves in order, first hit wins:

| what | order |
| --- | --- |
| the installation | `--instance` / `SECRETARY_INSTANCE`, else `~/secretary-instance` |
| the product checkout a head imports | `TA_SECRETARY_REPO`, else `$HOME/secretary` |
| the checkout an install or upgrade materializes | `--product-root`, else `TA_SECRETARY_REPO`, else `$HOME/secretary` |
| the product skill manifest | `--product-root`, else `SECRETARY_ROLE_SKILLS_MANIFEST`, else the configured checkout's |
| the checkout a launcher starts a role out of | `TA_RUNTIME_PYTHONPATH`, else `TA_SECRETARY_REPO`, else `$HOME/secretary` |
| the packaged units a plan or a doctor run compares against | the checkout named by the command, else the one `heads/source.yaml` recorded, else `TA_SECRETARY_REPO`, else `$HOME/secretary` |
| the account an upgrade materializes for | `--runtime-user`, else the owner of the instance directory |
| a skill's shell root | the manifest's `root`, expanded against the installation owner's home |
| a skill's command link | `SECRETARY_BIN_DIR`, else `<owner home>/bin` |
| a role worktree and an automation workspace | `TA_WORKSPACES_ROOT`, else `<owner home>/orca/workspaces` |
| the role runtime env file | `SECRETARY_RUNTIME_ENV_FILE`, else `TA_RUNTIME_ENV_FILE`, else `<instance>/runtime.env` |
| the head registry a tick reads | `TA_HEADS_REGISTRY`, else `<instance>/heads/heads.yaml`, else the running checkout's default |

`~` in a shipped manifest and `$HOME` in a shipped entry point are the *installation owner's* home.
An upgrade resolves that account once, from the owner of the instance directory or from
`--runtime-user`, and materializes skills, command links, role worktrees and automation workspaces
under it. It is the same account the units are rendered for, so a repair run as root writes the
paths those units then name instead of filling `/root`. A skill source is the opposite case: it
always resolves beside the manifest that declared it, because a source is a file in a checkout
rather than something the operator owns.

`secretary role-skills sync` run by hand has no installation owner to resolve and uses the calling
user's home, which is that operator's own installation.

None of these fall back to the checkout the running `secretary` module was imported from. That
applies to the launchers as well: `scripts/secretary-start.sh`, `scripts/secretary-agent-gate.sh`
and the role-env wrapper the dispatcher builds all read `TA_RUNTIME_PYTHONPATH` first and the
configured checkout second, and the packaged services carry both names bound to the checkout the
upgrade installed. An offline `secretary doctor` compares the host against the units of the
checkout recorded in `heads/source.yaml`, so it reports the installation rather than the copy of
the code the operator happened to run it from.

### The installation's head registry

A live tick reads the head registry only from the installation's own `heads/heads.yaml` and matching
`heads/source.yaml`, never from a product checkout. The pin carries the canon, checkout path,
revision and snapshot digest, so a stale or incomplete pair fails before routing any role. The only
operation that moves and immediately checkpoint-publishes that pair is `secretary upgrade`. Editing
the product's head canon in a working tree (a branch, an uncommitted change, a half-finished
refactor) therefore has no effect at all on a running installation.

Which heads exist is installation configuration. An installation owns its registry by keeping `heads/heads.toml` in its
instance directory; that file is then the canon `upgrade` materialises from. An installation without one materialises
from the product's shipped default, which is deliberately small: two resources (a Claude and an OpenAI subscription), a
handful of profiles whose fallback chains cross between them, and a role default per role — enough to bring a clean host
up on either subscription, and no account policy or model routing belonging to any one installation. A `heads/heads.toml`
that is present but unusable — malformed, unreadable, a directory, a dangling symlink — fails the upgrade by name instead
of silently reverting the host to product heads.

The source is visible from outside: `secretary status --json` returns `installation.head_registry` with `snapshot`,
`canonical`, `canonical_owner` (`instance` or `product`), `product_root`, `revision` and `error`, and the text `status`
prints the same line. `error` is filled when the pin has not been written yet (the installation never ran `upgrade` on
this version) or when the snapshot itself is broken.

`[role_defaults]` in that one snapshot routes the dispatcher's worker and reviewer heads and the head the
curator, retro and steward launch on. It no longer routes the observer: a sprint declares its own observer
head, and `role_defaults.observer` is read only to label an observer record filled in with no sprint to
read. Each background role's `automation.toml` still carries a `head`, but only
as a last resort for a registry that routes that role nowhere. The packaged unit of every one of those roles exports
`SECRETARY_INSTANCE` and the path of its own `runtime.env`, so each process resolves the same installation's snapshot
rather than the host's default one. A head the dispatcher launches starts in a terminal Orca creates and inherits none
of that, so the launcher writes both names into the head's own command line. `SECRETARY_INSTANCE` from a `runtime.env`
never overrides either: which installation a role belongs to is decided by whoever started it, and `runtime.env` is a
file inside an installation.

### Manual curator routing in an instance canon

This is a deferred, manual operator procedure for an installation whose private
`INSTANCE/heads/heads.toml` already declares `profiles.codex-curator`. It changes only that instance canon. Do not
add `codex-curator`, its model, or its account policy to
`src/triggered_agents/agents/pipeline/heads.toml`: the product file remains the portable fallback for an installation
with no canon of its own.

Before changing the role default, record the current `role_defaults.curator` as `PREVIOUS_PROFILE`. Inspect the
existing `profiles.codex-curator` without editing it: it must remain a Codex profile with
`model = "gpt-5.6-terra"` and `effort = "extra"`, and its declared `fallback` sequence must name existing profiles.
The fallback is instance policy. Record its current order and do not invent, delete, or reorder it as part of this
routing change. If the profile is missing, malformed, has a different model or effort, or has an invalid fallback,
stop. That is a separate canon-policy decision, not a reason to edit the portable registry or make a replacement
profile here.

Change the existing instance table only as follows:

```toml
[role_defaults]
curator = "codex-curator"
```

`secretary-curator.timer` is the sole scheduler owner when the curator component is enabled. The Orca curator
automation must remain disabled, and this installation's curator component must remain disabled for this deferred
route change. Verify the latter read-only against the selected installation:

```bash
SECRETARY_INSTANCE=INSTANCE python3 -P -m triggered_agents health
```

The output must retain the `DISABLED curator` line. Do not change `host.components.curator`, enable the Orca
automation, run `systemctl`, start or stop a service or timer, invoke the curator, run a production
baseline/backfill, write or delete a fact, reindex, or run a canary. Routing a role authorizes none of those actions.

After a separately approved instance-canon edit, materialize it manually with the normal instance rollout, for
example `secretary upgrade --no-pull --instance INSTANCE --product-root PRODUCT_ROOT`. Confirm with
`secretary status --json --instance INSTANCE` that the head-registry canonical owner is `instance` and that the new
snapshot was written. The routing assignment has no automatic rollout, shim, migration, or dependency step. The
routing change takes effect only for a later eligible scheduled run; it does not justify a
manual invocation. To roll back, restore `role_defaults.curator = "PREVIOUS_PROFILE"` in the same private canon,
leave `profiles.codex-curator` and its fallback untouched, repeat that same manual materialization, and confirm the
resulting instance snapshot. Do not delete the profile or alter scheduler ownership during rollback.

The role route does not widen the curator protocol. Selection is still bounded before content is read: normalized
descendants of exactly one registered canonical project `repo`, or the matching safe Orca workspace binding, route to
that project; multi-reservation observers route only to the reserved `review:po` selector; ambiguous, malformed,
unreadable, unregistered and prefix-only paths are `unknown`, while installation-wide sources are `global`. A
fact-bearing pending batch remains bound to its curator workspace, run and
session identity, its selected-project or all-backlog selector, and its starting cursors. Replay or advance with a
different identity or selector fails closed.

Likewise, a later intentional baseline is a separate manual operation. It requires one registered canonical project
or the reserved `review:po` selector,
an explicit actor, a non-empty reason, and exactly one current opaque cutoff or pending-batch identity. It cannot
bypass a pending record or use all-backlog mode. The baseline audit records the project, actor, redacted reason,
evidence identity, outcome, and hashed cursor identities/count only. It contains no transcript, personal-memory,
fact, raw-source or credential payload. Legacy line watermarks remain readable only through the released conversion
path; unversioned, stale, foreign, corrupt or cursor-only pending state, a changed source, an incomplete tail, or a
failed write is refused and left for manual resolution rather than guessed forward. The detailed protocol is in
[Memory](PROTOCOLS.md#memory) and [Project baseline settlement](PROTOCOLS.md#project-baseline-settlement).

A broken snapshot still stops the tick and names the reason: a missing table, an entry of the wrong shape, an unknown
resource or adapter on a profile, or a role in `role_defaults` pointing at a head that does not exist. A process handed
`SECRETARY_INSTANCE` whose snapshot is missing, unreadable, a directory or a dangling link fails by that snapshot path
too — the shipped registry is the fallback for a checkout with no installation selected, not for a selected installation
that has none of its own. The dispatcher answers `invalid_heads` with the text of the check; the fix is
`secretary upgrade`.

### Ownership and fail-closed behaviour

`reconcile apply` writes only what the managed manifest confirms. A name under the configured unit prefix that is in
neither the plan nor the manifest is a conflict, and any conflict aborts the whole run before the first write. There
are two ways to resolve it:

- the unit really is ours and matches the packaged file byte for byte:
  `secretary reconcile adopt --instance <dir> --logical-id systemd:unit:<name> --yes`;
- the name belongs to something else: list it in `host.foreign_units` in `instance.yaml`.

A unit that differs from the packaged file will not be adopted: either remove it by hand and let `apply` install the
canonical one, or work out why the host diverged from the product.

A component this installation deliberately does not run is switched off in config, not by the absence of a unit on the
host:

```yaml
host:
  components:
    curator:
      enabled: false
      reason: "load shedding"
```

A disabled component whose unit is installed and owned by us will be stopped and removed.

### Health suite

A deterministic set, usable as a gate before and after an upgrade:

```bash
secretary doctor --instance <dir>
secretary role-skills audit --check
secretary dispatcher production-tick --instance <dir> --probe
python3 -m tests.broad
```

The three `secretary` commands check the installation; `python3 -m tests.broad` checks the code
that installation is running, and is the `unit` plus `component` suites — about 1440 tests in ~77
seconds — not repository-wide discovery, which is all seven CI suites in one 402-second process.
This set is an operator gate around an upgrade, not the test contract: that is the dispatcher-owned
exact-SHA GitHub CI run described in [Testing](TESTING.md), and a green health suite never stands
in for it. When an upgrade touches a contour the profile does not cover — packaging, recovery,
memory, the local-PTY runtime or the board seam — run that named suite directly, for example
`python3 scripts/ci_test_shards.py packaging`.

### Worker-local broad receipt

A broad run is expensive enough that its result should survive the terminal it printed to. The
documented form wraps the suite:

```bash
python3 -m secretary check broad --module tests.broad
python3 -m secretary check show --module tests.broad
```

Where the registered project's adapter declares its own `broad_check` module, both commands run
that suite with no flag at all (`secretary check broad --reuse`, `secretary check show`); an
explicit `--module` still overrides it, and a project that declares none and is given none is
refused as `no_broad_check_module` rather than falling back to repository-wide discovery.

`check broad` streams the check's combined output to stderr while it runs, exits with the check's
own status (a signal-killed check becomes the usual `128+N`), and writes one worker-local broad
receipt under `state/checks/` in the workspace — an ignored path, never committed. The
worker-local broad receipt holds the check and its check-set digest, the working directory, the
import provenance described below, start, end and duration, the exit code, the parsed verdict and
counts where the runner prints them, and a bounded tail of the output. The verdict is scanned off the
stream as it goes past, so a runner that prints `OK (skipped=8)` and then megabytes of cleanup output
still has its counts recorded, without
the receipt growing to hold the logs.

Two check shapes are accepted, and they differ in one promise:

- `--module unittest` (add `--module-arg` for arguments) is the standard shape. The wrapper builds
  the argv, so the suite runs in a process that records its own working directory, interpreter and
  project package import. A registered project's adapter may set `broad_check.interpreter` and
  `broad_check.import_package`; the interpreter is relative to the candidate workspace unless
  absolute, and the package is the one that process imports for provenance. For example,
  `codegen-orchestrator` uses `.venv/bin/python` and `codegen_orchestrator`. This is an explicit
  adapter contract, not a package-name or tree-layout heuristic, and declaring it is **mandatory**
  for every registered project that gets cards: an adapter that declares no `broad_check` is
  refused as `broad_check_not_declared`, both here and at the dispatcher's preflight. Silence no
  longer means "the same broad check as Secretary" — an adapter that said nothing used to inherit
  the `sys.executable`/`secretary` default, which was a true contract for the Secretary project
  alone.
  A checkout that matches **no registered project at all** is a different case and is unaffected:
  running `secretary check broad --module ...` by hand in a plain clone keeps the CLI's own
  `sys.executable`/`secretary` default, and the JSON response names that fallback as
  `module_contract.source: cli_default` with a `module_contract.reason` of `no_project_binding` or
  `project_binding_disabled`. Adding `broad_check` changes the adapter bytes and therefore its
  digest, so run `project gate` again after adding it: until that gate re-enables the binding, the
  binding is disabled and the workspace falls into that unregistered case.
  If the configured interpreter cannot start, `check broad` returns the structured
  `interpreter_start_failed` error with exit status 2; this is distinct from a completed red suite
  and writes no receipt because no check process ran.
- `--command '<shell>'` accepts anything a project needs. A shell can `cd` elsewhere or reach a
  different interpreter or import path before any check starts, so this receipt records
  `origin: unobservable`, claims no import, and is never reused in place of a run. It remains a
  summary to read.

The dispatcher asks the same question of the same registry before it gives a card to a worker at
all, so a card is not issued on a contract its worker would then refuse. That preflight reads the
binding and the adapter and nothing else: an adapter that is unavailable or invalid, an adapter
that declares no `broad_check` at all (`broad_check_not_declared`), a `broad_check` that is
incomplete, and an absolute interpreter that cannot be started are refusals, and they
block the card before a workspace or a head exists, with the infrastructure class of
[Bring-up outcomes](PROTOCOLS.md#bring-up-outcomes). A relative interpreter is not refused there:
the schema resolves it from the candidate workspace, which does not exist yet, so the question is
left to the side that holds the tree and the card goes to work. That is a named decision rather
than a silence, and it is why the recommended spelling stays relative. The preflight judges the
declared contract as declared, exactly as promised above; what a run actually imported is caught
afterwards by the provenance below.

Observed provenance is necessary and not sufficient. A receipt may replace a run only when the
check process imported the adapter's configured project package *from this workspace*: a missing or
unreadable record, an empty path, an unresolvable one, and a path outside the candidate are all
refusals. That matters in an ordinary Python setup, where `PYTHONPATH` can put another checkout of
the project ahead of this one
— the receipt records that other path truthfully and is still refused for reuse, because the run it
describes was a run of different code. An import from the configured interpreter's own environment
(such as `.venv/.../site-packages`) is also refused even when that environment sits under the
workspace: it attests an installed copy, not the candidate tree. A shell-form check records no
import provenance and is never offered for reuse.

A check is identified by its structured check set — shape, module and the exact argument vector, or
the shell string — not by how it renders. `--module-arg 'one two'` and
`--module-arg one --module-arg two` read the same and are different checks, with different digests,
different receipt files, and no ability to answer for one another. The receipt stores that check set
so a reader recomputes the digest instead of trusting the file it was found in.

`check show` runs nothing. It answers whether the receipt still describes the checkout, comparing
the recorded git tree object id — the tree this worktree, tracked edits and untracked files included,
would commit to — with the current one, and exits non-zero when it does not. Its answer fails closed: a truncated or edited
receipt, a run that was killed or timed out, a checkout with no resolvable identity, an import from
outside the candidate, and a shape that attests no import are all "not usable" rather than a
summary. Reading goes through one boundary, `load_receipt`, which also refuses a result no run
could have written, even when the artifact's own digest was recomputed over the damage. Every
recorded result field — `signal`, `status`, `verdict`, the stored reason and the status the command
returns — is derived from one model of the raw process result, and the boundary rebuilds that model
and requires the stored fields to match it exactly. So a "complete" receipt that records a signal, an
exit code and signal that disagree, a complete run whose reason is a single space, or a status
outside the 0..255 a POSIX process can return are all refused, and reuse cannot hand back a masked
or invented status. Corruption outranks both status preservation and reuse. `check broad --reuse`
skips the run while the receipt is usable — through the same single predicate `check show` reports,
so the two can never disagree — and a report can quote the evidence instead of rebuilding it.

Its ownership, attestation and travel are defined in [Receipt names](PROTOCOLS.md#receipt-names).

`--probe` is a real dry tick: it takes the same singleton lock, passes the same mutation guards,
scans the same card states and runs the same decision logic, but the first write turns into an abort
and lands in the report as "what the next tick would do". A green probe with a broken tick is
impossible, because a broken tick fails here too.

### Head readiness

Before a new worker, reviewer or observer launch, the dispatcher reads the profile's resource from `heads/heads.yaml`
and runs its probe. The verdicts are cached in the data directory and can be inspected without running a card:

```bash
secretary dispatcher resource-health --instance <dir>
```

The check is cached for 300 seconds. That limits probe spend to one cheap call per resource per window, even though the
production tick may run more often. `ready` allows a launch. `unauthenticated`, `unavailable` and `exhausted` (a spent
quota, which reads differently to an operator: the account is not flaky, it is out until it resets) do not. For a card
already taken, a repeat worker launch blocks it with the reason, preserving the attempt's context. For an observer head
those verdicts mean a deferred launch: the sprint stays open, the reason is visible in the observer record, and the next
tick tries again.

`unknown` means the resource answered something nobody could classify, or did not answer in time. It is visible in the
snapshot but does not forbid a launch: a failure to observe does not prove the resource is down and must not stop the
queue forever.

`probe_broken` is the separate case where the probe never ran at all — the command does not exist, the interpreter does
not exist, or the interpreter cannot import the package the probe names. That is a defect of this installation rather
than a fact about the account, and unlike `unknown` it forbids a launch: a resource nobody can probe is a resource
nobody has gated, so the claim walks the fallback chain instead. The probe is run with the dispatcher's own interpreter
directory first on `PATH`, so the host-agnostic `python3 -m triggered_agents ...` in the registry resolves to the
interpreter the dispatcher itself runs under, whatever `PATH` the unit pins.

`secretary doctor` reports the probe of every resource in the installed registry and names the ones that cannot run as
their own findings, apart from a red resource: while a probe is broken, every claim on that resource was allowed
without the health gate having an opinion. It reuses a verdict the dispatcher wrote inside the 300-second window rather
than re-probing, and `--offline` reports only what is recorded.

For a card still in Ready, a verdict that forbids a launch sends the claim down the fallback chain the registry writes
for that head, and the card is claimed on the first head whose own resource allows one — normally the other family's
counterpart. The transfer is not silent: the tick names both heads, the card gets a comment saying which head replaced
which and on what verdict, and the reviewer's document says who wrote the branch. Two cases end in no claim at all, and
both leave the card in Ready with the reason on the tick rather than in Blocked: no launchable head anywhere in the
chain, and a transfer that would give the worker and the reviewer the same head, which is a review by the author and is
refused. Neither occupies a project slot, so a temporary provider problem never becomes an operator's Blocked card, and
neither ends the tick's Ready pass: every claim-skip is about the card in front of the scan, which records it under
`skipped_ready` and goes on to consider the next card. A card that cannot be claimed never costs the cards behind it
their tick.

If a resource shows `unauthenticated`, re-authenticate that runtime's CLI in the runtime home the profile names, then
wait out the TTL or check the next tick. On `unavailable` do not restart cards: check the provider's status, wait for
the next TTL and re-read the readiness snapshot. On `exhausted` the wait is until the quota resets or is topped up;
cards that have somewhere to go are already going there, and the ones that stayed in Ready are the ones with nowhere.
Which chains exist is a canon decision — see the head registry section — and a chain to a head of a lower class buys
attempts that never reach a report, which is why the shipped chains cross families at comparable class.

On `probe_broken` nothing about the account is wrong and waiting fixes nothing: run the probe string from the registry
by hand under the dispatcher's own environment and repair what it names. The usual cause is a probe command whose
interpreter cannot import the product; `secretary doctor` prints the failing line next to the resource.

### Head status in a live workspace

An operator standing in front of a workspace that looks empty asks one question — is there a head
here? — and the window is not what answers it. This is the read-only answer:

```bash
secretary head-status --instance <dir> --workspace <path>
```

It prints JSON: one row per head the dispatcher holds in that workspace, worker and reviewer apart,
each with a `summary` sentence written to be acted on without interpreting anything else. The exit
status is 0 for an answer and 3 for a degraded one — no workspace path, or a host in `noop` mode,
which observes no live workspace and would otherwise answer "live" to every question by
construction. A workspace the dispatcher holds no head in answers with no rows rather than with a
guess.

Every row answers two questions and never lets the second qualify the first:

- `head` — `alive`, `absent` or `unproven`, from the vitality snapshot and from nothing else.
  `alive` is a heartbeat whose process is running or suspended, or a provider cursor bound to this
  run that advanced. `absent` comes from the heartbeat alone, because it is the only source that
  observes the process and therefore the only one that may say a head is gone. Everything else is
  `unproven`, which is a statement about the observation and not about the head: the channels that
  could not answer are listed in `unavailable_sources` and each one's own reading is in `evidence`.
  The answer is bound to the head's `run_id`, and a role the dispatcher holds a head identity for
  but no durable `HeadRun` is `unproven` with that as its reason, because binding another run's
  evidence to it is the lie that binding exists to prevent.
- `runtime_pane` — `visible`, `no-runtime-pane`, `no-pane`, `unknown` or `unavailable`, read from
  the renderer's own tree of drawn panes rather than from the list of ptys, because a pty can be
  listed and connected while nothing draws it. That is `no-runtime-pane`, and it is the case this
  command exists for: a pty Orca listed as connected, drawn by no runtime pane, with a live head
  working behind it (2026-08-24). `no-pane` is a pty no inventory answers for, `unknown` is a
  renderer channel that could not decide — unsupported by this build, silent about this workspace,
  or naming no identity the pty can be compared by — and `unavailable` is a pane inventory that
  refused.

- `episode` — the persisted vitality conclusion for that run, and since secretary-1543 what an
  operator needs when a head goes quiet behind it: `quiet_seconds` (how long since the last
  advancement, or since the episode began), `dark_progress_sources` (each progress source that
  answered and stopped, with `dark_since`, `dark_seconds` and the instant its freeze expires),
  `missing_progress_sources` (a progress channel this episode never heard from at all),
  `last_progress` (the episode's own advancement plus the card's pane-output and waiting-since
  stamps beside it), and `next_recovery_deadline` — the verdict the next reduction will reach and
  when, or `null` with a `deadline_note` where the ladder has no further rung to climb (a confirmed
  stall belongs to the recovery path, a suspended or retained process has its clocks frozen). The
  `summary` sentence carries the dark source and the deadline too, so the one line an operator
  reads names both.

The invariant is printed on the answer and beside every row: pane readings are advisory. A pane with
no runtime pane, a disconnected pane, a pane no inventory names and an unreadable pane channel are
all facts about the window, and none of them is evidence that a head is absent. So an empty-looking
workspace is never on its own a reason to drop the claim, kill the workspace or restart the card —
read the row first, because that intervention on a live head destroys the round it is in.

The command only reads. It starts nothing, stops nothing and repairs nothing, and it writes neither
the dispatcher's state nor the head's: its transport carries no lifecycle call, the provider cursor
comes from the run already persisted rather than being rebound, and a head whose channel cannot
answer is reported `unproven` instead of being probed harder.
