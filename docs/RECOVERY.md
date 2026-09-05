# Git-centric recovery

This is the single recovery contract. The private installation Git repository is the durable
checkpoint of configuration and portable state. Moving to a new machine requires installing the
product, access to that repository, and re-entering the credentials it deliberately does not hold. A
separate bundle or object-store transport is not part of the main path.

## Topology

Kanboard transport is local non-secret configuration in `board-transport.env`, not secret-store
content. A clean-host recovery recreates the deterministic default without a recovery phrase. It does
not restore a legacy random container token: first run the upgrade migration on an installation that
still uses one. Legacy encrypted `kanboard_*` entries remain inert until the operator removes them.

```text
product repository    public template: product, CLI, runtime, schemas, generic skills
instance repository   one private repository per owner: config + state/
host runtime          local runtime, rebuilt from the checkpoint; not canonical
```

The private repository absorbs the normalised data plane. There is no second Git canon for the local
data directory. One remote, one HEAD, one RPO.

## Source of truth

The live board backend stays the operational store. The remote Git HEAD is the last confirmed
recovery checkpoint. Between commits, live state runs ahead of the checkpoint by the RPO. That is the
expected gap, not a desync.

## What the checkpoint contains

The canon, the normalised minimum needed to resume work:

- instance config: `instance.yaml`, `persona/`, `projects/`, `adapters/`, `heads/`, `policies/`.
  `heads/` includes this installation's own `heads.toml` canon when it has one, the generated
  `heads.yaml` snapshot, and the `source.yaml` pin recording the installed heads canon, its
  checkout and its exact revision. After a restore, this pin identifies the heads configuration
  and product ref the installation was running; bringing the snapshot up to a new checkout is
  `secretary upgrade`'s job;
- board export: `state/board/cards.ndjson`, `state/board/sprints.ndjson`,
  `state/board/events.ndjson`, `state/board/export.json`, and the analytics seal
  `state/board/analytics-manifest.json`;
- run and audit state: `state/runs/runs.ndjson`, `claims.json`, `watermarks.json`, `export.json`;
- memory facts: `state/memory/facts/**`;
- knowledge documents: `state/knowledge/**` (free-form markdown, see
  [Architecture](ARCHITECTURE.md#knowledge-planes)).

`runs.ndjson` is a normalized, portable journal: each entry records its source
path and line number. During recovery, after the pipeline role worktree exists,
Secretary materializes those entries back into that worktree's live
`state/pipeline/` source before the dispatcher units are installed or started.
The materializer preserves each source's history and its original line-number
gaps; it does not compact independent sources into one synthetic stream. A live
journal may already be a valid append-only extension of the checkpoint; that
extension is retained. A divergent or truncated prefix is never overwritten.
Likewise, a checkpoint refuses to publish a truncated or rewritten live export
over a non-empty canonical journal.

Outside the canon, because it is derived or bulky raw material, rebuilt or kept in an optional cold
archive:

- raw board dumps;
- the vector index and derived memory exports;
- transcripts, artifacts, backups;
- terminals, worktrees and generated host state (systemd units from `packaging/systemd/` and the
  session-manager automations of the background roles). The product, not the checkpoint, is canonical
  for these: units are compiled from the packaging templates for the installation user and layout, and
  automation schedules come from each role's `automation.toml`. `secretary reconcile apply` and
  `secretary upgrade` re-materialise them idempotently during provisioning and recovery, matching
  automations by name and editing in place so ids and unit names stay stable.

Board records are kept as NDJSON for line-wise diffs. The checkpoint also carries the small
`export.json` summary and, for newly published cuts, the analytics seal; derived JSON card and sprint
duplicates are not part of the checkpoint.

Pipeline cards, including Product and Issue records, and sprint entities go into the checkpoint as separate sets: sprints live on their own
board and are not part of the card export, so the writer reads them in its own pass instead of
deriving them from cards that reference a sprint. A sprint record carries its reference, goal,
Definition of Done, repositories, owning product, issues, reserved projects, status, budget by event
type, current card, resume entry, all entries
on the entity and the source's audit metadata. A sprint closed before a sprint owned a product has none
of those three fields on its row, so its record omits them instead of storing an empty value, and a
checkpoint written before they existed omits them the same way. Derived values (budget totals,
installation thresholds, resume freshness) are not stored: they are recomputed from the record and
configuration.

## Layout

```text
<private repository>/
  instance.yaml, persona/, projects/, adapters/, heads/, policies/   config, committed by the operator
  state/                                                             state, committed by the auto-writer
    board/   cards.ndjson, sprints.ndjson, events.ndjson, export.json, analytics-manifest.json
    runs/    runs.ndjson, claims.json, watermarks.json, export.json
    memory/facts/**
    knowledge/**   brainstorms, decision logs, incident write-ups
  secrets/                                                           secret store, see below
    catalog.yaml, installation-key.json, values/<id>.enc.json
```

`secrets/installation.key` is the raw installation key, mode `0600`, outside Git and outside the
checkpoint. The store is described under [Secrets](#secrets) and in
[Protocols](PROTOCOLS.md#secrets).

Memory facts are stored flat in the single repository; the memory writer commits
`propose`/`commit`/`supersede` into it. One history, one HEAD. `state/memory/facts` is the only canon
for every derived form of memory, so the instance directory is a required argument on the export and
index-rebuild paths: a forgotten argument fails rather than pointing the export at someone else's
memory.

## Cadence and RPO

- commit once per dispatcher tick (60s), on change: if the hash of the normalised `state/` did not
  move, the end-of-tick checkpoint skips the commit;
- attempt a remote push in its own 30-minute window, fast-forward only;
- durable RPO on machine loss is 30 minutes. Local commits give fine-grained history and fast local
  rollback, but they do not survive the machine.

The two operations are deliberately separate. A changed-state tick can make one local checkpoint
commit; an unchanged tick makes none. The scheduled pusher is not forced: it only publishes when
the remote tip is an ancestor of local `HEAD`, and it otherwise records the failure or divergence
for the next window or operator action.

## GitHub checkpoint credential

The HTTPS `github.com` checkpoint remote has one product-managed credential, stored as the encrypted,
versioned `github.checkpoint-token` envelope. Set or rotate it without putting its value on an argument
list:

```bash
python3 -P -m secretary secret checkpoint-github set --instance INSTANCE --stdin
# or: --file TOKEN_FILE     # regular, owned-by-caller, mode 0600
```

`set` and `import` are aliases for this one-value operation. Repeating an unchanged input is a no-op;
replacement preserves the single catalog identity. One product-owned remote-execution boundary classifies
the transport, resolves the actual Git child identity, selects the permitted source, and gives that child
an operation-scoped capability. For `https://github.com`, it clears all ambient credential helpers and
explicitly selects Secretary's native Git credential helper. The helper emits the token only to Git's
credential protocol, never to command arguments, repository config, logs, or status. A locked, missing,
malformed, or rejected value stops the HTTPS checkpoint push.

A clean host cannot fetch an inaccessible private repository from the encrypted store inside it. Supply
one external bootstrap credential for the initial clone. The same recover command is safe after an
interruption: its existing-checkout fetch uses that supplied bootstrap credential when present, or the
unlocked encrypted checkpoint credential when it is not. If neither source is available, recovery stops
before contacting the remote with an actionable credential-input reason; it never falls through to an
ambient Git helper.

```bash
sudo secretary recover --instance-remote REMOTE --instance-dir INSTANCE --installation-user USER \
  --bootstrap-credential-file TOKEN_FILE --recovery-phrase-file PHRASE_FILE
```

`--bootstrap-credential-stdin` is also supported, but cannot share stdin with a recovery phrase. A
mode-0600 bootstrap file may be owned by the account invoking `sudo` or by the effective command user;
any other owner is refused. Before Git starts, Secretary copies either input into a mode-0600,
operation-scoped capability owned by the actual installation-user Git child, then removes it on success
or failure. The bootstrap input is neither tracked nor retained after the Git operation, and is not an
ongoing checkpoint source. Local/file remotes remain ordinary hermetic Git; SSH remotes are explicit
manual-bypass transport and use no HTTPS helper. Any HTTPS host other than `github.com` is refused,
rather than falling through to an ambient helper. `status --json` reports
`checkpoint.credential` readiness, source, and verification age only; it never compares or prints
values while the store is locked. When an operator invokes status, recovery, or an upgrade through
`sudo`, readiness is evaluated by the resolved installation-user Git consumer, not by the root
orchestrator reading a user-owned installation key.

For a complete pre-recovery inventory, inspect `status.recovery` or run text doctor. Resource rows are
derived only from the installed registry and retain probe provenance and age. Online doctor reuses a
fresh dispatcher verdict; it performs a read-only probe for stale or absent evidence and never writes
the dispatcher's cache. Offline doctor performs no probe and says `stale` or `unknown`. Provider-red
states (`unauthenticated`, `exhausted`, or `unavailable`) are distinct from `probe_broken`, which means
the probe executable or import itself failed.

Credential consumers, the last checkpoint operation, path configuration, secret materialization, and
manual Git bypasses are independent rows and findings. Remediate with each row's
`supported_next_action`; do not infer credential health from a successful old push, infer current
credential failure from an old failed push, or treat an ambient helper as supported recovery access.

## Writers

Six writers touch the repository, each with its own pathspec:

- tick writer: `state/board`, `state/runs`, at the end of a dispatcher tick under the tick lock;
- memory writer: `state/memory`, on `propose`/`commit`/`supersede`;
- knowledge writer: `state/knowledge`, on `secretary knowledge write`;
- secret writer: `secrets/`, on `secret init/set/import/remove`.
  `secret list` and `secret materialize` are not part of this writer.
- head-registry writer: `heads/heads.yaml`, `heads/source.yaml`, on `secretary upgrade`; it commits
  and immediately pushes the matching pair rather than waiting for the tick's 30-minute window.
- local-configuration writer: `.gitignore`, when local configuration such as
  `board-transport.env` needs a durable exclusion.

The pathspecs do not overlap, so none of them picks up another's half-written tree. Nobody uses
`git add -A`: uncommitted manual config edits are left alone. The Git index is not built for
concurrent writes, so every writer takes a shared repository lock for the duration of staging and
commit. The memory, knowledge and secret writers commit immediately rather than waiting for a tick;
the 30-minute push carries the commit out with the rest of the checkpoint.

## Validation gate

Before each commit the snapshot passes a fail-closed check. If any item fails, the tick skips the
checkpoint, records the reason in status and retries on the next tick. A torn snapshot never reaches
history.

- task audit is settled, with no pending board mutation;
- the writer regenerates `cards.ndjson` and `sprints.ndjson` from the live boards, and both counters
  in `export.json` match the line counts. The generated `cards.json`/`cards.ndjson` pair must be
  identical and card references must be unique before either the local export or canonical checkpoint
  files are replaced;
- memory staging is empty;
- the secret scan of `state/` is clean. `state/` goes to the remote and is the one place a secret could
  leak, for example a token pasted into a card or a log. The memory and knowledge writers run the same
  scan over their own text before committing, since their path does not go through the tick gate.

### Analytics checkpoint seal v1

`analytics-manifest.json` is `secretary.board.analytics-checkpoint` version 1. It is the boundary for
a later offline analytics projection, not a replacement for recovery validation. Its object has exactly
`schema`, `version`, `checkpoint_id`, and `files`. `files` has exactly one entry for each of
`events.ndjson`, `cards.ndjson`, `sprints.ndjson`, and `export.json`. Every entry records its path,
lowercase SHA-256 digest and byte count; the three NDJSON entries also record their non-blank line count.
`checkpoint_id` is the lowercase SHA-256 digest of those canonical file-entry values. `export.json`
continues to be a card/sprint summary, never proof of the cut by itself.

The writer validates all four files first, synthesizing an empty `events.ndjson` when there are no board
events, then hashes and validates the staged manifest. It removes any prior manifest before replacing the
four files and renames the new manifest last. A directory copied during that interval therefore has no
manifest, or after the final rename has a complete matching cut; it cannot carry a manifest authenticating
the prior files mixed with the new ones.

`verify_analytics_checkpoint(directory)` is directory-only and read-only. It does not read a live board,
dispatcher, provider, transcript, comment or runtime state, returns only verified checkpoint metadata and
rejects unknown schemas, missing or extra files, duplicate entries, malformed metadata, digest/count
mismatches and stale card/sprint summaries. A projection must call it before parsing any analytics rows.
Eventless or unsealed historical checkpoints remain valid recovery input under the existing restore rules,
but are deliberately not analytics v1 input; the writer does not backfill seals into history.

## Failure and divergence

Push failure (network, forge or auth unavailable during the push window) is fail-closed on the
checkpoint, not on the work. Local commits continue, the dispatcher keeps running, and the growing
checkpoint lag is visible in `status` and `doctor`. The next 30-minute push retries.

Remote divergence (the remote has commits that are not local) leaves the auto-writer fast-forward
only. Force-push and history rewriting are forbidden. On a non-fast-forward the push stops, `status`
raises a `remote diverged` alarm, and the operator resolves it.

## Secrets

The host `runtime.env` is mode `0600`, is not part of the checkpoint, and may hold materialised
installation secrets. Board transport is separate: `board-transport.env` is ordinary local
configuration, is not restored from the encrypted store, and never needs a recovery phrase. Forge
access and interactive head logins stay in the operator's password manager and are never copied to the
host by the product.

The secret store (`secretary/secret_store.py`, `secrets/` in the instance repository) is a separate,
recoverable canon on the same repository: a metadata catalog and versioned envelopes tracked in Git
next to board and memory, travelling out with the same push. The only things the repository never
contains are the raw installation key (`secrets/installation.key`, gitignored, `0600`) and the recovery
phrase, which is generated once at `secret init`, shown to the operator and stored nowhere by the
product. With the phrase, the key is rebuilt and values return byte for byte; without it, `recover`
prints a locked/missing report and writes nothing. Losing the phrase means reissuing the secrets, not
losing the rest of the installation.

Security boundary: a trusted single-user host. Board and memory endpoints listen on loopback. External
tokens are protected by host access control, `.gitignore` and the secret scan of `state/`, not by
at-rest encryption. A private key on the same host as the data does not protect that data from host
compromise; what at-rest encryption did protect was copies that left the host, in Git and offsite, and
this contract removes those.

The checkpoint distinguishes ordinary configuration from secret material. Its exact-value scan reads values
whose runtime variable names identify credentials (`*_TOKEN`, `*_PAT`, `*_IDENTITY`, `*_KEY`, `*_SECRET`,
passwords, credentials, auth or webhooks), plus URLs that embed user info. When its installation key is
present, it also reads catalog values for those sensitive names or for values that independently match a
credential shape. This keeps legacy whole-file imports from treating `SECRETARY_DATA_DIR`,
`TA_SECRETARY_REPO`, or a plain `KANBOARD_URL` as secrets: a board URL without userinfo is configuration,
not a credential. A locked or incomplete secret store is reported by `doctor` but does not itself halt
checkpointing; runtime-file and pattern scans still run. Before protocol text reaches the board or
audit, the same credential-specific redactor replaces real values; it does not mangle ordinary long
identifiers or restored historical text. Known token and webhook formats remain a second fail-closed
scan layer.

The installation key belongs to the installation user, the same user that owns the host and the
installation, not a narrower role. Centralising secrets in one store neither narrowed nor widened the
trust boundary: any process that could previously read `runtime.env` can read the installation key today
and open everything in the store with it. The product does not promise worker isolation; a broker,
grants and per-worker access to a subset of secrets are outside this contract and are not implemented.
"Secret store" does not mean the secrets are better protected from anyone else on the same host than
they were in `runtime.env`. It means they are observable, versioned and recoverable without retyping
values.

## Observability

`status` and `doctor` show checkpoint freshness: the time and hash of the last commit, the time of the
last successful push, the last operation attempt and its age, checkpoint lag in minutes and commits,
the reason the gate is blocked, and the `remote diverged` state. The attempt timestamp belongs to the
current operation outcome; it does not replace the timestamp of the last successful push.

A blocked checkpoint degrades the production tick and its durable telemetry. The dispatcher still
contains the failure and retries the checkpoint on the next tick, but unit health and the steward must
not read the tick as healthy while the recoverable snapshot cannot be written.

`status --json` carries a `secret_store` section: whether the store is initialised, how many secrets it
holds, when the catalog last changed, whether a usable installation key exists, and a summary of
materialisation targets — without a single value, key or phrase in any form. `doctor` raises a finding
when catalog and values diverge, when the key is missing or unusable while the catalog is non-empty, or
when the key's permissions are wider than `0600`. A healthy store and a completely absent one both
produce no findings.

## Fresh install and recovery

Install the product with the memory extra first. On Ubuntu 24.04, `secretary bootstrap` installs the
pinned board and session-manager runtimes, writes deterministic `board-transport.env`, and creates the
Pipeline board.
`secretary install` installs neither and fail-closed checks both runtimes before changing live state.

On a clean host, bootstrap creates the checkout, deterministic local board transport and the Pipeline
board without a recovery phrase or manual entry of board credentials:

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

The supported command clones only the current default-branch checkpoint: depth 1, one branch and no
tags. Recovery consumes the current canon tree, so historical objects are not an input to board,
memory, project or host reconstruction. Git creates the clone in a private sibling staging directory,
and recovery verifies its origin, checked-out branch, upstream, exact tip and shallow boundary before
atomically adopting the requested path. A timeout or operator interruption kills and reaps the clone's
complete process group, including pack helpers, removes its operation-scoped credential capability and
staging directory, and leaves an absent or empty requested target as it began.

Later recovery of that checkout fetches the tracked branch without tags and merges `@{u}` with
`--ff-only`. Git obtains only the commits needed to connect the old shallow checkpoint to the new tip;
the checkout remains shallow. An unchanged tip is idempotent. New local checkpoint commits remain
ordinary descendants and `CheckpointPusher` can publish them fast-forward only without old remote
history. Recovery never shallows, resets or replaces an existing checkout, and never silently
unshallows one.

`runtime.env` stays a gitignored ordinary file with mode `0600` when an installation needs it for
other materialised secrets. `board-transport.env` is also gitignored, but its contents are non-secret
and bootstrap recreates its default on a clean host. Neither file is added to a checkpoint commit.

`recover` runs one supported sequence:

1. Opens the secret store, if this instance repository has one, before reading `runtime.env`. With
   `--recovery-phrase-file` or `--recovery-phrase-stdin` (or an interactive prompt on a TTY when the key
   is not yet on disk), the installation key is rebuilt and values are materialised into the files the
   catalog names, including `runtime.env` if any secret materialises there. Without the phrase this step
   writes nothing and reports locked/missing, and `runtime.env` stays whatever is already on disk.
2. Crosses the recovery ownership barrier. The instance checkout, secrets, locks and declared data root
   are handed to `--installation-user` before a restored installation key can be consumed by that user's
   Git or remote-execution child. A present key must remain a regular non-symlink mode-`0600` file owned by
   that user. On retry, an already-restored key crosses the same barrier before checkout reuse, then the
   barrier closes again over any files materialised by this invocation.
3. Checks the remote and checkout, materialised credentials (when any), board reachability and the
   installed session manager. Board transport is created or read independently of that secret step. If
   `runtime.env` did not appear in step 1 and there is no store at all, it remains a manual operator
   step only for any other required host configuration.
4. Materialises `state/board` and `state/runs` from the checkpoint into a new local data plane. The
   derived JSON forms are built from the NDJSON, and counters are verified before any live write.
5. Idempotently imports the board and rebuilds the memory export and index from `state/memory/facts`.
   The board import also restores sprint entities: if the export carries them, `restore-board` creates
   the sprints board and returns each entity whole — goal, Definition of Done, repositories, product,
   issues, reservations, status,
   budget, current card, resume, entries and the source's audit metadata. There is no need to recreate a
   sprint after recovery. A restored entity occupies a new board row, so its own dates describe the
   restore while the source dates are read from its audit metadata. Recovery rewrites what the export
   holds and never validates a sprint the way opening one is validated: an entity without a product, its
   issues or its reservations comes back without those metadata keys rather than with empty ones. Parity
   compares whether each of the three fields is there at all, not only what it holds: a restored entity
   that gained an empty `product` its export never carried is a lossy write and fails the check.
   Card creation and initialization have one restore-only bulk transaction boundary for Task, Product and Issue
   rows. Recovery validates the full normalized plan first and stages a deterministic per-card audit obligation
   before the first backend mutation. One preloaded board schema, swimlane map and authoritative reference
   inventory feed native `createTask` batches carrying final title, description, reference, column and swimlane;
   bounded metadata and placement batches apply the remaining state. Kanboard batches are not transactions. A
   mixed error, duplicate/missing/malformed response id, lost reply or interruption is reconciled against a fresh
   whole-board inventory and batched metadata. Only exact individually proved rows commit, and a retry writes only
   absent or incomplete obligations. Duplicate references, a conflicting title/description or a committed card
   that no longer matches fail closed. Released per-card create/restore events remain valid resume evidence, and
   recovery never clears audit/progress or rotates a namespace still bound to the target.
   Before staging any card, recovery serializes every possible single create, metadata/state and closure payload
   against the same 1 MiB call limit used by the batch transport. An oversized payload therefore names its
   reference and phase without leaving a pending occurrence. A valid JSON-RPC error member or invalid mutation
   result is a definite backend rejection; fresh evidence identifies applied siblings, but an absent or incomplete
   rejected member reports the rejection rather than generic uncertainty. Lost transport replies and malformed or
   incomplete aggregate documents remain ambiguous and retain their pending obligations for retry.
   Card and sprint comment history has a restore-only bulk boundary. Recovery reads normalized history in
   bounded batches and writes ordered waves with at most one next occurrence per entity in a JSON-RPC batch;
   ordinary interactive comment commands retain their read-before/read-after protocol. Every occurrence is
   staged under its stable restore request id before a write. A mixed result, lost aggregate response or
   interruption leaves only the affected occurrences pending; a fresh bounded history read proves applied
   occurrences before audit append and only unproved occurrences may be retried. Identical bodies retain their
   exported multiplicity and order through the body digest plus duplicate-occurrence ordinal. Pending evidence
   carries the body only while the backend outcome is ambiguous; committed audit does not. Restore progress is
   complete only after fresh authoritative card and sprint snapshots prove full content and order parity. The
   pinned backend's `(date_creation, id) ASC` producer order, positive-integer `createComment` result and
   disposable timeout canary are recorded in
   [Kanboard comment contract evidence](evidence/kanboard-comment-contract-v1.2.46.md). Comment reads and writes
   use separate 50-call caps inside the general 200-call/1 MiB batch policy. Reads cap the number of histories,
   not the bytes or length of one history; a very long single history is reread once per ordered wave and may
   fail the 30-second transport timeout closed with its exact pending obligations intact.
   Archived rows may carry historical positions that overlap active rows. Recovery therefore closes archived
   rows in bounded batches only after their comments are proven, then takes an authoritative board snapshot and
   reconciles the relative order of active rows in each `(column, swimlane)` group against normalized
   `(position, reference)` order. Only mismatched groups move. Each group owns one deterministic
   `restored_order` request and pending audit record; a retry re-reads the group, resumes at its first mismatch
   and commits only after a final authoritative read proves the complete order. Replaying the earlier
   per-card `restored` request is not placement proof because that request may already be committed and return
   without executing a move. This separate post-close boundary repairs a preserved `board_parity=failed`
   target without clearing progress, recreating rows or comments, or repeating unrelated writes. A malformed
   or lost move response is reconciled from the live group before any success is recorded, and an already
   repaired third run performs no placement mutation. The existing final content and order parity checks remain
   the fail-closed completion gate; absolute positions of archived rows are deliberately not compared.
6. Attempts every missing project checkout from the registry and creates the non-secret managed
   runtime-home files for the agent CLIs. Each clone uses the same remote-execution boundary as the
   private instance checkout. GitHub HTTPS uses the supplied bootstrap capability when present,
   otherwise the recovered managed-store credential; ambient helpers are disabled. Local/file and SSH
   keep their declared semantics, while other HTTPS hosts are refused. Provider authentication stays
   manual.
7. Runs the pre-host materialiser. It regenerates the installed head snapshot and source pin, commits the
   pair locally, and attempts normal managed fast-forward-only checkpoint publication before synchronizing
   role skills and recreating all role worktrees. A publication failure is a distinct degraded recovery
   result, not success, but it does not stop the remaining safe recovery steps. Ordinary `secretary upgrade`
   still stops at this boundary. When the materializer runs under `sudo`, role worktrees and their Git
   administrative directories are assigned to
   `--installation-user`, so the user services can read and update them.
8. Rebuilds the pipeline worktree's live JSONL run source from the checkpointed normalized journal,
   after those worktrees and skills exist but before any dispatcher unit is installed or started.
9. Applies host units and session-manager automations, performs any required memory recovery, then
   verifies restore status. A desired but unavailable project remains in the host plan: an existing
   matching managed Orca registration is preserved and a missing or drifted registration is reported
   deferred. At a project-consuming dispatch boundary, the task project id resolves to an enabled binding
   before that exact checkout is inspected; an unavailable binding is rejected before its worker or reviewer
   process or project worktree. Unknown ids and registered inventory-only projects remain distinct outcomes.
   Observers consume the dedicated observer repository, not the sprint's canonical repository roots or
   project-id reservations, so an unavailable reserved project does not leave a sprint headless. Automations
   are installation-global and do not schedule project work. Healthy projects continue normally. Heads are
   connected after bootstrap as a separate step.

The ordering is intentional: recovery first reconstructs the normalized board and run exports,
validates their NDJSON and counters, and only then rebuilds the board, pipeline journal and managed
runtime. `heads/source.yaml` is provenance for the installed heads snapshot: it records the canon,
checkout and revision the snapshot came from and supports the read-only host-packaging lookup.
Recovery selects the product root to materialise from `--product-root` or the configured/default root;
the pin does not select that checkout or protect the snapshot from a different selected host checkout.

Parity is checked separately for cards and for sprints, and both checks are fail-closed: a mismatch
leaves recovery unfinished and visible in `doctor` rather than silently counting the restore as
successful. A live backend holding a sprint entity that is not in the export stops the restore instead of
being overwritten.

Project failures are isolated after board and memory recovery. Text and JSON output contain one row per
binding with its project id, target state, transport classification, outcome (`cloned`, `unchanged` or
`failed`), sanitized code/reason and retryability. If any row fails, recovery still performs safe host
finalization and the installation-user ownership handoff, then exits non-zero with `status: degraded`.
Invalid global installation configuration, board/sprint parity, memory corruption, unsafe host
materialization and operator interruption remain fatal boundaries and are not converted into project rows.

Remote checkpoint publication is a second recovery-only degraded boundary. The generated head-registry pair
is committed before the managed push. A disabled or unavailable destination, credential refusal, or remote
divergence leaves the command non-zero and reports the retained local commit while safe later materializer,
pipeline-state, ownership and reconcile steps complete. The push and full recovery are never labelled healthy.
With an unchanged compatible remote, rerunning recovery publishes that same commit fast-forward and clears the
publication degradation without an empty duplicate commit. If the remote advanced independently, retry preserves
both tips and reports divergence; recovery never resets, rebases, deletes, or force-pushes either history.

Running `secretary recover` again is safe: the checkout is fast-forward only, completed board import and
memory indexing are skipped when the board, run, memory-fact and binding identity still matches, successful checkouts
are left untouched, and only missing/failed checkouts and their dependent host resources are retried. The
non-secret `recovery-progress.json` in the data directory records that identity, completed core phases and
sanitized project outcomes. Project rows are diagnostic, write-only history: filesystem checkout truth is
the sole retry authority. It is derived state owned by recovery; do not edit it or registry bindings to
force a retry. Re-run the same supported `secretary recover` command instead. A restored instance
stays recoverable itself: its own audit records about the restore go into the next checkpoint, and a
later recovery into another empty backend writes its events under a new namespace instead of counting
someone else's as already applied. That namespace lives in the restore state file, so retrying one
recovery stays idempotent.

The recovery identity length-delimits every canonical path, entry type and content value before hashing, so
different fact-tree shapes cannot alias through adjacent byte sequences. The low-level `restore-reconcile`
diagnostic intentionally returns a non-zero `degraded` result while any configured checkout is unavailable and
does not mark reconcile complete. Repair the checkout by rerunning the supported recovery command; do not edit
the progress or managed-state files.

A non-empty target that is not a valid instance repository may be debris from a pre-fix interrupted
clone. Recovery will not infer that it is healthy or overwrite it. Inspect and preserve it if needed,
then remove that failed partial target outside Secretary or select a fresh `--instance-dir` and rerun
the same supported command. A dirty checkout, different origin, invalid repository or non-fast-forward
relationship is likewise left untouched and reported as a refusal.

A checkpoint taken before sprint entities entered the export still restores: it has no sprints file, which
reads as an installation without sprints, and `doctor` stays green on a completed restore. Terminals,
worktrees, the vector index, generated units and host caches are not copied from the remote. No object
store or separate backup host is required.

`secretary recover --dry-run` checks the checkout, credentials, runtime prerequisites and checkpoint
integrity, then prints the steps as `would-change`. The preview writes no local data plane, does not touch
the board, and runs neither the memory reindex nor the host materialiser.

For the PO-only live drill, record the candidate SHA, run the dry-run with the same mode-0600 bootstrap and
recovery-phrase inputs planned for the clean target, then run recovery once. Save its text and JSON output,
verify all project rows and the core ownership/status result, repair only the external cause of any retryable
project failure, and rerun the identical recovery command until it reaches `status: ok`. Confirm that the
second run reports the board and memory unchanged and does not duplicate comments. Do not activate test
workers, push from the recovered instance, edit credential files or recovery state, or reuse the shared
recovery host/VPN. Bulk board-import performance remains separate work and is not validated by this drill.

## Repairing historical duplicate card references

Released task creation before product commit `d9e872b` derived an implicit card reference from the new
Kanboard row ID. Because row IDs and project reference counters are independent, a later row could reuse an
archived row's reference. The supported repair retains the older owner and reassigns the later row only when
the later backend ID equals the duplicated numeric suffix, its exact `created` audit event binds that backend
ID, both records are tasks with complete metadata, and no active or ambiguous dependent state exists. Numeric
coincidence without that producer and audit evidence is not authority.

Preview is read-only and enumerates active and archived Pipeline rows. It prints backend IDs, record type,
state, a bounded title summary, the retention evidence, proposed collision-free references, refusals, and a
hash of the complete observed plan:

```bash
secretary task repair-references-preview --instance INSTANCE --data-dir DATA_DIR
```

Apply names that plan and every proposed reassignment by exact backend identity. Put a non-secret explanation
in `REASON_FILE`; do not put it on the command line:

```bash
secretary task repair-references-apply --role po --instance INSTANCE --data-dir DATA_DIR \
  --plan-id PLAN_ID --task-id BACKEND_ID --request-id REQUEST_ID --reason-file REASON_FILE
```

Repeat `--task-id` for every row proposed by the preview. Apply takes the normal allocation lock, compares the
whole preview before its first write, stages all audit intents, then updates each exact row and records
`reference_repair` metadata plus an append-only `reference_repaired` event. A retry with the same request ID,
plan, IDs and reason resumes or proves the same effects. It never deletes, merges, reopens or moves a card;
titles, descriptions, comments, metadata, closed state and position remain unchanged. A target acquired by
another row before apply, mixed record types, missing producer evidence, active work, reference-bearing
task/sprint or current run-state companions, or another concurrent board revision fails closed. After a
committed backend change, pending audit intentionally blocks export until the identical retry or
`task reconcile-audit` proves and completes it. If another card claims a still-untouched row's proposed target
after an interruption, either recovery command preserves that card and reallocates the repair target under the
normal allocation lock; the committed provenance records every superseded allocation. A row already changed
by the repair is never reallocated around a claimant. Rollback is an operator-reviewed follow-up operation
after audit reconciliation, never a hand edit of checkpoint files, pending audit, or Kanboard storage.

For the post-merge live repair, the PO runs this exact sequence: preview and retain its plan output; apply that
plan to the listed backend IDs; let the managed checkpoint writer publish; verify the private remote branch SHA
equals the reported local checkpoint SHA; then rerun recovery into the isolated drill board and verify parity.
If publication reports a restorable-board or parity failure, stop before the drill and investigate the live
board. Never edit `cards.ndjson` as the source of truth.

## Manual recovery sprint closeout

The manual recovery and durability sprint was deployed in two production batches:
`dab7508` (recovery, reference allocation and checkpoint safety) followed by `5f79500`
(dispatcher-owned exact-SHA gate receipts and sparse observer wakes). Deployment validation recorded a green suite of
1,978 tests and role-skill delivery synchronized across all nine targets, with zero missing targets and
zero drift.

The deferred owner-scoped work is intentionally outside this closeout: provision the OpenRouter credential
and retain/test the installation recovery phrase. Neither is required to inspect checkpoint health, but both
are required for its respective provider or secret-recovery path.

### Live LLM canary — completed normal path, recovery edge still open

Production sprint `sprint:1402` completed a real single-card self-hosted cycle on 2026-08-10. Card
`secretary-1403` used `claude-opus` as worker and `codex-reviewer` as independent reviewer, produced
candidate `75fd95168d663e19c7654964482b5d279f288c53`, passed the GitHub exact-SHA gate and GREEN review,
and merged through PR #184 as `b8fe6d190815c1186a00a35c1f8fe5a4b78e7bff`. The sprint closed
itself after 26 minutes with one worker claim, one gate, one reviewer launch, zero budget events and
zero manual prompts. The delivered increment made the unit suite's `TA_PIPELINE_STATE_DIR` hermetic.

This evidence closes the normal live candidate-to-review path; it does not claim that every recovery
edge ran. The reviewer started on its first attempt, so the post-green reviewer-only retry remains a
live-host follow-up. Observer wake delivery separately returned `pane-stayed-ready` four times around
Assessment and completion. Those failures did not repeat worker, gate or review work, and the observer
still released and closed the sprint, but the final sprint resume omitted the delivery failures. The
board tracks the transport defect (`issue:13dd4d88df6b33cfb98f`), telemetry omission
(`issue:83ac17afc53248340f4c`) and unexercised reviewer-retry edge
(`issue:0091f54306e6ee1aad69`) separately.

A missing `secrets/installation.key` reported by `secretary doctor` remains a fail-closed
secret-recovery finding. It is not healthy, and it must not be recovered, materialised or otherwise
changed merely to run a canary whose selected subscription heads are already authenticated.

For later real sprints, keep recording card reference; resolved worker and reviewer heads; candidate SHA;
dispatcher-owned exact-SHA gate receipt; observer decision; checkpoint publication; worker/gate/reviewer launch
counts; delivery failures; and manual prompts. A successful normal launch is not evidence that a retry
path ran. Do not freeze production merely to preserve the old canary premise, and do not inject a failure
that mutates candidate history or weakens the gate.

Fresh mode does not accept an existing installation user or checkout. It refuses with an explicit choice:
`--recover` for the same installation, or a separate adopt workflow for a live host. Recover does not
overwrite a dirty checkout, a different remote, an arbitrary non-empty data target or an unowned host
resource. Fully adopting an existing live host is not part of this flow.

## Relation to the cold archive

The Git checkpoint is the only recovery contract. A manual cold archive remains available for raw material
and compatibility, with no timer, no offsite transport and no `doctor` gate. Scheduled archive backups,
offsite transfer and archive-age checks are not part of the product.

## Not covered

- Moving configuration into a control-plane database.
- Automating provider credentials and head authorisation.
- A mandatory object-store transport, a full archive of transcripts and artifacts, a public plugin API.
