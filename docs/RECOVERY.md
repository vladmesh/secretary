# Git-centric recovery

The private instance Git repository is the recovery contract. It holds configuration and portable
state. Moving to a new machine needs the product, access to that repository, and the credentials it
does not hold. No bundle or object-store transport is part of the main path.

## Topology

```text
product repository    public template: product, CLI, runtime, schemas, generic skills
instance repository   one private repository per owner: config + state/
host runtime          local runtime, rebuilt from the checkpoint; not canonical
```

The private repository is the only Git canon for the data plane: one remote, one HEAD, one RPO.

## Source of truth

The selected board backend (PostgreSQL in production) is the operational store. The remote Git HEAD
is the last confirmed recovery checkpoint. Between commits, live state runs ahead of the checkpoint by
the RPO; that gap is expected.

## What the checkpoint contains

The canon is the normalised minimum needed to resume work:

- instance config: `instance.yaml`, `persona/`, `projects/`, `adapters/`, `heads/`, `policies/`.
  `heads/` holds this installation's `heads.toml` canon when it has one, the generated `heads.yaml`
  snapshot, and the `source.yaml` pin (installed heads canon, checkout, exact revision). After a
  restore the pin identifies the heads configuration and product ref that were running; moving the
  snapshot to a new checkout is `secretary upgrade`'s job;
- board export: the logical files `cards.ndjson`, `sprints.ndjson`, `events.ndjson`, `audit.ndjson`,
  `export.json` and the analytics seal `analytics-manifest.json`, stored in `state/board` in the
  split layout (see [Layout](#layout));
- run and audit state: `state/runs/runs.ndjson`, `claims.json`, `watermarks.json`, `export.json`;
- memory facts: `state/memory/facts/**`;
- knowledge documents: `state/knowledge/**` (free-form markdown, see
  [Architecture](ARCHITECTURE.md#knowledge-planes));
- the secret store under `secrets/` (see [Secrets](#secrets)).

Board records are NDJSON for line-wise diffs. Derived JSON card and sprint duplicates are not
checkpointed.

`runs.ndjson` is a portable journal: each entry records its source path and line number. Recovery
materialises the entries back into the pipeline role worktree's live `state/pipeline/` sources, per
source, preserving line-number gaps, before dispatcher units are installed or started. A live journal
that is a valid append-only extension of the checkpoint is kept; a divergent or truncated prefix is
never overwritten. A checkpoint likewise refuses to publish a truncated or rewritten live export over
a non-empty canonical journal.

Cards (including Product and Issue records) and sprints are separate sets; the writer reads sprints
in their own pass. A sprint record carries its reference, goal, Definition of Done, repositories,
owning product, issues, reserved projects, status, budget by event type, current card, resume entry,
all entries and the source's audit metadata. A record without a product, issues or reservations
omits those keys rather than storing empty values. Derived values (budget totals, installation
thresholds, resume freshness) are recomputed, not stored.

Outside the canon, rebuilt or kept in an optional cold archive:

- raw board dumps;
- the vector index and derived memory exports;
- transcripts, artifacts, backups;
- terminals, worktrees and generated host state (systemd units from `packaging/systemd/`). The
  product is canonical for these: units are compiled from packaging templates, and those timers are
  the background roles' only schedule. `secretary reconcile apply` and `secretary upgrade`
  re-materialise them idempotently, so unit names stay stable. Orca automations are not recovered:
  Secretary no longer manages them.

## Layout

```text
<private repository>/
  instance.yaml, persona/, projects/, adapters/, heads/, policies/   config, committed by the operator
  state/                                                             state, committed by the auto-writer
    board/   layout.json, export.json, analytics-manifest.json,
             cards/NNNN/NNNNNNNN.json, sprints/NNNN/NNNNNNNN.json      one record per file
             audit/NNNN/NNNNNNNN.ndjson, events/NNNN/NNNNNNNN.ndjson   immutable segments
    runs/    runs.ndjson, claims.json, watermarks.json, export.json
    memory/facts/**
    knowledge/**   brainstorms, decision logs, incident write-ups
  secrets/                                                           secret store
    catalog.yaml, installation-key.json, values/<id>.enc.json
```

`secrets/installation.key` is the raw installation key, mode `0600`, outside Git and the checkpoint.

### Board checkpoint layout

The local export in the data directory stays flat. Only the copy committed into `state/board` is
split, so a checkpoint's Git cost follows what changed instead of the size of the board
(secretary-1656):

- `layout.json` marks the split layout (`secretary.board.checkpoint-layout`, version 2). A directory
  without it is the flat layout every earlier checkpoint used: one file per logical file.
- `cards.ndjson` and `sprints.ndjson` are stored one line per file, `<dir>/<index // 1000>/<index>`,
  in line order. Changing one card rewrites one small blob and the two trees above it; a checkpoint
  over an unchanged board writes no file and so creates no Git object.
- `audit.ndjson` and `events.ndjson` only grow, so they are stored as immutable segments. A
  checkpoint whose log extends the committed one adds one segment holding the appended bytes. A log
  that does not extend the committed one (the first split checkpoint after a flat one, or rewritten
  history) is replaced by a single segment.
- A logical file's bytes are the concatenation of its parts in index order; a gap in the sequence or
  an unexpected entry is a broken checkpoint, not a shorter one.

Every consumer of a committed checkpoint reads it through `board.checkpoint_layout.open_checkpoint_board`,
which understands both layouts and returns the same logical bytes for the same board. The first
checkpoint after the upgrade converts a flat checkpoint in place: it writes the split parts and
removes the flat files in the same commit. Earlier commits keep their flat files; history is never
rewritten.

Memory facts are stored flat in this repository; the memory writer commits `propose`/`commit`/
`supersede` into it. `state/memory/facts` is the only canon for every derived form of memory, so the
instance directory is a required argument on the export and index-rebuild paths: a missing argument
fails instead of pointing the export at another installation's memory.

## Cadence and RPO

- The dispatcher ticks every 60 s. The periodic board/run checkpoint prepares at most once per
  five-minute cadence window; if the hash of normalised `state/` did not change, it makes no commit.
- A remote push is attempted in its own 30-minute window, fast-forward only. A due push window forces
  one fresh, verified preparation in that tick before pushing.
- Durable RPO on machine loss is 30 minutes. Local commits give fine-grained history and local
  rollback but do not survive the machine.

The pusher publishes only when the remote tip is an ancestor of local `HEAD`; otherwise it records the
failure or divergence for the next window or operator action.

## Writers

Six writers touch the repository, each with its own pathspec:

- tick writer: `state/board`, `state/runs`, at the cadence above, under the tick lock;
- memory writer: `state/memory`, on `propose`/`commit`/`supersede`;
- knowledge writer: `state/knowledge`, on `secretary knowledge write`;
- secret writer: `secrets/`, on `secret init/set/import/remove` (`list` and `materialize` do not
  commit);
- head-registry writer: `heads/heads.yaml`, `heads/source.yaml`, on `secretary upgrade`; it commits and
  immediately pushes the pair;
- local-configuration writer: `.gitignore`, when local configuration such as `board-store.env`
  needs a durable exclusion.

Pathspecs do not overlap, and nobody uses `git add -A`, so uncommitted manual config edits are left
alone. Every writer holds the shared repository lock while staging and committing. All writers except
the tick writer commit synchronously; the next push carries their commits out. Explicit checkpoint
users (install, recover) are also synchronous and bypass the periodic cadence.

## Checkpoint readers and freshness

`state/board` and `state/runs` are recovery and offline-analytics artifacts, not a live read model.
Their in-product readers are `installation.materialize_checkpoint` and the recovery identity during
recovery, `bootstrap` (checkpoint swimlanes) and `board.analytics.project_analytics_checkpoint`, which
first verifies the sealed manifest. All of them read the board through the one checkpoint reader
(see [Board checkpoint layout](#board-checkpoint-layout)); `restore.py` reads the materialised local
export, not the checkpoint. `status`
and `doctor` read Git and the dispatcher's production-state telemetry for freshness only.

Live card, sprint, audit and command reads use the selected backend through `TaskReader`, `TaskAudit`
and the web read layer. Dispatcher lifecycle state comes from `dispatcher/production-state.json` and
live run journals. Backend audit ownership is in [Board store](BOARD_STORE.md) §7.3.

## Local Git packing controls

Install, recover and upgrade idempotently set, with `git -C INSTANCE config --local --replace-all`,
only these settings in the private instance repository (never global config, never a project repo):

```
pack.threads=1
pack.windowMemory=128m
pack.deltaCacheSize=64m
gc.auto=0
maintenance.auto=false
```

`doctor` names missing, drifted or duplicate values with the exact remediation. To roll back, run
`git -C INSTANCE config --local --unset-all` for each key. These settings constrain packing; they are
not a hard memory limit.

`gc.auto=0` and `maintenance.auto=false` stop every `git commit` from starting Git's implicit
`gc --auto`, which would otherwise pack the repository inside whichever checkpoint tick first crosses
the loose-object threshold. Packing runs from `secretary-instance-maintenance.timer` instead (daily,
`Persistent=true`): its service runs `secretary instance-maintenance`, which is `git gc --auto` with
Git's stock thresholds (6,700 loose objects, 50 packs) restated on the command line, so a quiet day
costs one object count. That packing step takes no state-repo lock: it touches objects only, and the
two ref-writing parts of `gc` (`pack-refs`, reflog expiry) are switched off for it. Reflogs are then
expired as a separate step under the state-repo lock, the only moment a checkpoint can wait on
maintenance, bounded at 60 seconds. `secretary status` lists the timer under `host.schedules` with
`last_trigger`, and the service under `host.units` reads `failed` after a failed run; the run's
before/after object counts are in its journal.

## Validation gate

Before each tick commit the snapshot passes a fail-closed check. If any item fails, the tick skips the
checkpoint, records the reason in status and retries next tick:

- task audit is settled with no pending board mutation. The audit is the one the card client names
  (`tasks.task_audit_for`): staged `requests` rows. If the card client cannot be established the
  checkpoint is blocked by name, never checked against a file journal instead;
- `cards.ndjson` and `sprints.ndjson` are regenerated from the live backend, both counters in
  `export.json` match the line counts, the generated `cards.json`/`cards.ndjson` pair is identical and
  card references are unique, all before local export or canonical files are replaced;
- memory staging is empty;
- the secret scan of `state/` is clean. The memory and knowledge writers run the same scan over their
  own text before committing.

### Analytics checkpoint seal v2

`analytics-manifest.json` is `secretary.board.analytics-checkpoint` version 2, the boundary for
offline analytics projection. Its object has exactly `schema`, `version`, `checkpoint_id` and `files`.
`files` has exactly one entry each for the logical `events.ndjson`, `cards.ndjson`, `sprints.ndjson`,
`audit.ndjson` and `export.json`, each with path, lowercase SHA-256 and byte count of the logical
bytes, whichever layout stores them; NDJSON entries
also record their non-blank line count. `checkpoint_id` is the SHA-256 of the canonical entries.
Version 1 seals (without `audit.ndjson`) stay readable; new exports are sealed as version 2.
`export.json` is a count summary, never proof of the cut.

The writer validates all five files flat in staging (synthesising an empty `events.ndjson` when
there are no events), validates the staged manifest, removes the prior manifest, writes the changed
split parts and renames the new manifest last. A copy taken mid-write has either no manifest or a
complete matching cut.

`verify_analytics_checkpoint(directory)` is read-only and reads only that directory. It rejects
unknown schemas, missing or extra files, duplicate entries, malformed metadata, digest or count
mismatches and stale summaries. A projection must call it before parsing rows. Unsealed checkpoints
remain valid recovery input but not analytics input; seals are not backfilled.

## Failure and divergence

A push failure (network, forge or auth) is fail-closed on the checkpoint, not on work: local commits
continue, the dispatcher keeps running, lag is visible in `status` and `doctor`, and the next window
retries.

On remote divergence (remote commits not present locally) the push stops and `status` raises
`remote diverged`. Force-push and history rewriting are forbidden; the operator resolves it
([Operations](OPERATIONS.md#checkpoint-push)).

## Secrets

The host `runtime.env` is mode `0600`, gitignored, outside the checkpoint, and may hold materialised
installation secrets. `board-store.env` is local connection material that bootstrap generates,
not restored from the secret store. Forge access and interactive head logins stay in the operator's password manager; the product
never copies them to the host.

The secret store (`secretary/secret_store.py`, `secrets/`) is a recoverable canon in the same
repository: a metadata catalog and versioned encrypted envelopes, tracked in Git and pushed with the
checkpoint. The repository never contains the raw installation key (`secrets/installation.key`,
gitignored, `0600`) or the recovery phrase, which `secret init` shows once and the product stores
nowhere. With the phrase the key is rebuilt and values return byte for byte; without it `recover`
prints a locked/missing report and writes nothing. Losing the phrase means reissuing secrets, not
losing the installation. Command contracts are in [Protocols](PROTOCOLS.md#secrets).

Security boundary: a trusted single-user host. Board and memory endpoints listen on loopback. External
tokens are protected by host access control, `.gitignore` and the `state/` secret scan, not by
at-rest encryption on the host. The installation key belongs to the installation user; any process
that can read `runtime.env` can read the key and open every secret. There is no broker, grant or
per-worker isolation.

The checkpoint scan distinguishes configuration from credentials. It reads values whose runtime
variable names identify credentials (`*_TOKEN`, `*_PAT`, `*_IDENTITY`, `*_KEY`, `*_SECRET`, passwords,
credentials, auth, webhooks) and URLs with embedded userinfo. With the installation key present it also
reads catalog values under sensitive names or values matching a credential shape. `SECRETARY_DATA_DIR`,
`TA_SECRETARY_REPO` or a board URL without userinfo are not secrets. A locked or incomplete store is a
`doctor` finding but does not halt checkpointing; runtime-file and pattern scans still run. Protocol
text is redacted by the same credential-specific redactor before it reaches the board or audit; known
token and webhook formats are a second fail-closed scan layer.

### GitHub checkpoint credential

The HTTPS `github.com` checkpoint remote and the dispatcher's registered-project GitHub HTTPS
operations use one managed credential, the encrypted `github.checkpoint-token` set by
`secret checkpoint-github set --stdin` (or `--file`). Setup, rotation, doctor rows and card preflight
are in [Operations](OPERATIONS.md#checkpoint-and-project-github-access). For recovery:

- A clean host cannot read the encrypted store before cloning it. Supply one external bootstrap
  credential with `--bootstrap-credential-file TOKEN_FILE` (mode `0600`, owned by the `sudo` caller or
  the effective user) or `--bootstrap-credential-stdin` (cannot share stdin with the recovery phrase).
- Secretary copies it into a mode-`0600` operation-scoped capability owned by the installation-user
  Git child and removes it on success or failure. It is not retained and is not an ongoing checkpoint
  source.
- A rerun fetches the existing checkout with the supplied bootstrap credential, or else the unlocked
  managed credential. With neither, recovery stops before contacting the remote. Ambient Git helpers
  are never used for `github.com` HTTPS; other HTTPS hosts are refused; local/file remotes are plain
  Git; SSH is explicit manual bypass.
- Under `sudo`, credential readiness is evaluated by the installation-user Git child, not by root.

For a pre-recovery inventory read `status.recovery` or text `doctor`. Rows come from the installed
registry and carry probe provenance and age; online doctor reuses a fresh dispatcher verdict or runs a
read-only probe, offline doctor reports `stale` or `unknown`. `probe_broken` (the probe itself failed)
is distinct from provider states `unauthenticated`, `exhausted`, `unavailable`. Credential consumers,
last checkpoint operation, path configuration, secret materialisation and manual Git bypasses are
separate rows; remediate each with its `supported_next_action`. An old push result proves nothing
about current credential health.

## Observability

`status` and `doctor` show checkpoint freshness: time and hash of the last commit, last successful
preparation, a not-yet-due skip, retry state, last failed preparation and reason, last successful
push, last operation attempt and its age, lag in minutes and commits, and `remote diverged`. A skip is
never reported as a fresh preparation. The attempt timestamp does not replace the last successful
push timestamp.

A blocked checkpoint degrades the production tick and its telemetry; the dispatcher retries on the
next tick. If the push window was due, the push is recorded as withheld instead of sending an older
snapshot. Unit health and the steward must not read such a tick as healthy.

Past the 30-minute RPO, `doctor` and the dashboard lamp report a red `checkpoint.rpo_exceeded`
finding that names what stopped publication. If preparation is failing, that is the gate's reason
and the time it began failing (`failing_since_at`); otherwise it is the push failure. A blocked gate
commits nothing, so the unpushed lag stays at zero while it is blocked. For that case the exposure
is counted from the last successful preparation. On PostgreSQL the gate first settles audit rows left
staged by a dead writer (`docs/BOARD_STORE.md` §3.9). A stale row therefore blocks for the grace
period plus one tick, not until an operator repairs it.

`status --json` has a `secret_store` section: initialised or not, secret count, last catalog change,
whether a usable installation key exists, and a materialisation-target summary, with no value, key or
phrase. `doctor` raises a finding when catalog and values diverge, when the key is missing or unusable
while the catalog is non-empty, or when key permissions are wider than `0600`. A healthy store and an
absent store both produce no findings.

## Backend-aware cold archives

The Git checkpoint is the only recovery contract. `backup create`/`backup verify` are a manual cold
archive for raw material, with no timer, offsite transfer or `doctor` gate. Commands are in
[Operations](OPERATIONS.md#optional-cold-archive).

Every archive carries the normalized Product, Issue, Task and Sprint views, comments, request/audit
history and inert run/claim state. A `core` archive holds only that engine-independent set. A `full`
archive is version 2 and adds
`engine/postgres.dump`, a custom-format data-only dump made and listed by the pinned `postgres:16`
client; its manifest records source Alembic head, server/client version, table counts and purpose. The
dump is taken after the pipeline pause, and its table counts are taken inside the same exported
snapshot the dump reads, so rows the pause itself writes are in both or neither. `backup verify`
checks the manifest's counts are well formed but does not decode the dump; `restore-postgres` is the
check that the restored rows match them. No
archive carries a database password, role secret, `board-store.env` or the memory model cache
`memory/fastembed-cache` (the index rebuild downloads the model again).

`secretary restore-postgres ARCHIVE --instance TARGET` restores only
into a distinct disposable target whose `board-store.env`, container, database and owner/app/read roles
are managed by the board-store lifecycle. It verifies archive identity and checksums, refuses the
source endpoint, migrates the target to the recorded head, verifies roles, requires every application
table empty, restores in one `pg_restore` transaction as owner, and compares table counts and
normalized cards/sprints. A same-archive retry verifies the marker and parity and restores nothing. It
never starts a dispatcher, worker, reviewer or observer. An error leaves the archive and source
database untouched; repair or recreate only the target before retrying.

## Fresh install and recovery

Install the product with the memory extra. On Ubuntu 24.04, `secretary bootstrap` installs the pinned
Docker and session-manager runtimes and provisions, migrates and role-verifies the PostgreSQL board
store, with no recovery phrase or manual board credentials. `secretary install` installs neither
runtime and checks both before changing live state.

```bash
python3 -m pip install '.[memory]'
sudo secretary bootstrap \
  --instance-remote git@github.com:OWNER/secretary-instance.git \
  --instance-dir INSTANCE \
  --installation-user INSTALL_USER

sudo secretary install \
  --instance-remote git@github.com:OWNER/secretary-instance.git \
  --instance-dir INSTANCE \
  --installation-user INSTALL_USER
```

Fresh install refuses an existing installation user or checkout and names the choice: `--recover`
for the same installation, or the separate adopt workflow for a live host. Recover does not overwrite
a dirty checkout, a different remote, an arbitrary non-empty data target or an unowned host resource.

On a clean host, recovery bootstraps and then runs `recover` instead of `install`:

```bash
sudo secretary recover --instance-remote REMOTE --instance-dir INSTANCE --installation-user USER \
  --bootstrap-credential-file TOKEN_FILE --recovery-phrase-file PHRASE_FILE
```

### Checkout

The clone takes only the current default-branch checkpoint: depth 1, one branch, no tags. Git clones
into a private sibling staging directory; recovery verifies origin, branch, upstream, exact tip and
shallow boundary, then atomically adopts the requested path. A timeout or interruption kills the
clone's whole process group, removes the credential capability and staging directory, and leaves an
absent or empty target as it was.

Later recovery of that checkout fetches the tracked branch without tags and merges `@{u}` with
`--ff-only`; the checkout stays shallow. An unchanged tip is a no-op. Recovery never shallows, resets
or replaces an existing checkout and never unshallows one.

A non-empty target that is not a valid instance repository is refused, not overwritten. Inspect and
preserve it, then remove it outside Secretary or choose a fresh `--instance-dir`. A dirty checkout,
different origin, invalid repository or unsupported non-fast-forward is also left untouched and
refused. A clean tree alone never proves product ownership.

`runtime.env` and `board-store.env` stay gitignored and are never committed.

### Sequence

`recover` runs one sequence:

1. Opens the secret store, if present, before reading `runtime.env`. With `--recovery-phrase-file`,
   `--recovery-phrase-stdin`, or a TTY prompt when the key is not on disk, it rebuilds the installation
   key and materialises values into the files the catalog names. Without the phrase it writes nothing,
   reports locked/missing, and `runtime.env` stays as it is.
2. Crosses the recovery ownership barrier: the instance checkout, secrets, locks and declared data root
   are handed to `--installation-user` before that user's Git or remote child can consume a restored
   key. A present key must be a regular non-symlink mode-`0600` file owned by that user.
3. Checks the remote and checkout, materialised credentials, board reachability and the installed
   session manager.
4. Materialises `state/board` and `state/runs` into a new local data plane, builds derived JSON from
   the NDJSON and verifies counters before any live write.
5. Idempotently imports the board and rebuilds the memory export and index from `state/memory/facts`
   (see [Board import](#board-import)).
6. Attempts every missing project checkout from the registry through the same remote-execution
   boundary as the instance checkout, and creates the non-secret managed runtime-home files for agent
   CLIs. Provider authentication stays manual.
7. Runs the pre-host materialiser: regenerates the installed head snapshot and source pin, commits the
   pair locally, attempts managed fast-forward-only publication, then synchronises role skills and
   recreates role worktrees (owned by `--installation-user` under `sudo`). A publication failure is a
   degraded result that does not stop later safe steps; ordinary `secretary upgrade` stops at it.
8. Rebuilds the pipeline worktree's live run journal from the checkpoint, before any dispatcher unit is
   installed or started.
9. Applies host units, performs any required memory recovery and
   verifies restore status. Orca repo registrations are not part of this step; they stay Orca's own
   state. Dispatch refuses an unavailable binding before starting
   its worker, reviewer or project worktree. Observers use the dedicated observer repository and are
   unaffected by unavailable reserved projects. Heads are connected afterwards as a separate step.
10. Re-enters the ownership barrier on every partial or successful exit, handing root-created instance
    Git locks, recovery progress and restored dispatcher run-state to the installation user. A cleanup
    error is reported separately and does not replace an earlier failure.

`heads/source.yaml` is provenance for the installed heads snapshot and supports the read-only
host-packaging lookup. The product root to materialise comes from `--product-root` or the
configured/default root, not from the pin.

`secretary recover --dry-run` checks checkout, credentials, runtime prerequisites and checkpoint
integrity and prints steps as `would-change`. It writes no data plane, does not touch the board and
runs neither the memory reindex nor the host materialiser.

### Board import

Card and sprint parity are checked separately and both fail closed: a mismatch leaves recovery
unfinished and visible in `doctor`. A live backend holding a sprint entity absent from the export
stops the restore.

The import restores sprint entities whole (goal, Definition of Done, repositories, product, issues,
reservations, status, budget, current card, resume, entries, source audit metadata). A restored entity
is a new board row: its own dates describe the restore; source dates are in its audit metadata.
Recovery does not apply sprint-opening validation. Parity compares whether `product`, `issues` and
reservations are present, not only their values; gaining an empty field the export lacked fails parity.
A checkpoint without a sprints file restores as an installation without sprints.

Restore-only bulk boundaries, all idempotent on rerun:

- **Cards.** Task, Product and Issue creation validates the full plan and stages a deterministic
  per-card audit obligation before the first backend mutation. Every create, metadata/state and closure
  batch is reconciled against a fresh board inventory, and only individually proved rows commit. A
  retry writes only absent or incomplete obligations. Duplicate references, conflicting
  title/description, or a committed card that no longer matches fail closed.
- **Comments.** Card and sprint history is read in bounded batches and written in ordered waves with at
  most one next occurrence per entity per batch. Each occurrence is staged under a stable restore
  request id; a fresh history read proves applied occurrences before audit append, and only unproved
  ones are retried. Identical bodies keep multiplicity and order (body digest plus occurrence ordinal).
  Pending evidence carries the body only while the outcome is ambiguous.
- **Order.** Archived rows are closed in bounded batches only after their comments are proven. Then,
  from an authoritative snapshot, the relative order of active rows in each `(column, swimlane)` group
  is reconciled to normalized `(position, reference)` order. Only mismatched groups move; each owns one
  deterministic `restored_order` request and pending audit record, resumes at its first mismatch, and
  commits only after a final read proves the order. This repairs a preserved `board_parity=failed`
  target without clearing progress or repeating unrelated writes. Absolute positions of archived rows
  are not compared.

Restore is complete only after fresh card and sprint snapshots prove full content and order parity.
Released per-card create/restore events remain valid resume evidence. Recovery never clears
audit/progress or rotates a namespace still bound to the target.

### Degraded outcomes

Project failures are isolated after board and memory recovery. Output has one row per binding: project
id, target state, transport, outcome (`cloned`, `unchanged`, `failed`), sanitised reason and
retryability. If any row fails, recovery still completes safe host finalisation and the ownership
handoff, then exits non-zero with `status: degraded`. Invalid global configuration, board/sprint
parity, memory corruption, unsafe host materialisation and operator interruption stay fatal.

Checkpoint publication is a second degraded boundary. The head-registry pair is committed before the
push. A disabled or unavailable destination, credential refusal or divergence leaves the command
non-zero and reports the retained local commit, while later safe steps complete. With an unchanged
compatible remote, a rerun publishes that same commit fast-forward without an empty commit. Recovery
never resets, rebases, deletes or force-pushes.

If upstream advanced after recovery retained a local head-registry checkpoint, checkout reuse first
tries a fast-forward, then locally merges the fetched tip (without publishing) only for a recognised
product lineage:

- the checkout is clean, tracks its matching `origin` branch, and has exactly one trustworthy merge
  base in the shallow history;
- all local-only history lies on one first-parent chain;
- each one-parent commit has the configured product Git identity, the exact head-registry checkpoint
  message, and touches only the two installed-head files;
- each earlier reconciliation merge has that identity, the exact reconciliation message, and two
  parents: prior local lineage first, an ancestor of the fetched upstream second.

Anything else (missing shallow ancestry, octopus or arbitrary merge, manual/config/state/secret commit,
wrong identity/message/path, untracked content, conflict) is refused with both tips preserved. Conflict
or interruption aborts the merge and verifies original HEAD, index tree, clean worktree and no merge
head.

### Retry

Rerunning the same `secretary recover` command is the only supported retry. The checkout is
fast-forward only; completed board import and memory indexing are skipped while the board, run,
memory-fact and binding identity still matches; successful checkouts are untouched; only
missing/failed checkouts and their dependent host resources are retried. The identity length-delimits
every canonical path, entry type and content value before hashing.

`recovery-progress.json` in the data directory is non-secret derived state: identity, completed core
phases and sanitised project outcomes. Project rows are diagnostic only; filesystem checkout state is
the retry authority. Do not edit it, registry bindings, or managed-state files to force a retry.

A restored instance stays recoverable: its own restore audit goes into the next checkpoint, and a later
recovery into another empty backend writes events under a new namespace kept in the restore state
file, so retrying one recovery stays idempotent.

The low-level `restore-reconcile` diagnostic returns non-zero `degraded` while any configured checkout
is unavailable and does not mark reconcile complete.

Terminals, worktrees, the vector index, generated units and host caches are not copied from the
remote. No object store or separate backup host is required.

## Repairing historical duplicate card references

Cards created by releases before commit `d9e872b` may reuse an archived row's reference. The repair
keeps the older owner and reassigns the later row only when its backend ID equals the duplicated
numeric suffix, its exact `created` audit event binds that ID, both records are tasks with complete
metadata, and no active or ambiguous dependent state exists. A numeric coincidence without that
evidence is not authority.

Preview is read-only. It lists active and archived Pipeline rows with backend IDs, record type, state,
a bounded title, retention evidence, proposed collision-free references, refusals and a plan hash:

```bash
secretary task repair-references-preview --instance INSTANCE --data-dir DATA_DIR
```

Apply names that plan and every proposed row by backend ID; put the non-secret reason in a file:

```bash
secretary task repair-references-apply --role po --instance INSTANCE --data-dir DATA_DIR \
  --plan-id PLAN_ID --task-id BACKEND_ID --request-id REQUEST_ID --reason-file REASON_FILE
```

Repeat `--task-id` for every proposed row. Apply takes the allocation lock, compares the whole preview
before writing, stages all audit intents, then updates each row with `reference_repair` metadata and
an append-only `reference_repaired` event. A retry with the same request ID, plan, IDs and reason
resumes or proves the same effects. It never deletes, merges, reopens or moves a card; titles,
descriptions, comments, metadata, closed state and position are unchanged. A target taken by another
row, mixed record types, missing producer evidence, active work, reference-bearing dependents or a
concurrent board revision fail closed.

After a committed backend change, pending audit blocks export until the identical retry or
`task reconcile-audit` completes it. If another card claims a still-untouched row's target after an
interruption, either command keeps that card and reallocates under the allocation lock, recording each
superseded allocation; a row already changed is never reallocated. Rollback is a reviewed follow-up
after audit reconciliation, never a hand edit of checkpoint files, pending audit or board storage.
Never edit `cards.ndjson` as the source of truth.

## Not covered

- Moving configuration into a control-plane database.
- Automating provider credentials and head authorisation.
- A mandatory object-store transport, a full archive of transcripts and artifacts, a public plugin API.
