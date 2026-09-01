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
  in `export.json` match the line counts;
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
last successful push, checkpoint lag in minutes and commits, the reason the gate is blocked, and the
`remote diverged` state.

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

`runtime.env` stays a gitignored ordinary file with mode `0600` when an installation needs it for
other materialised secrets. `board-transport.env` is also gitignored, but its contents are non-secret
and bootstrap recreates its default on a clean host. Neither file is added to a checkpoint commit.

`recover` runs one supported sequence:

1. Opens the secret store, if this instance repository has one, before reading `runtime.env`. With
   `--recovery-phrase-file` or `--recovery-phrase-stdin` (or an interactive prompt on a TTY when the key
   is not yet on disk), the installation key is rebuilt and values are materialised into the files the
   catalog names, including `runtime.env` if any secret materialises there. Without the phrase this step
   writes nothing and reports locked/missing, and `runtime.env` stays whatever is already on disk.
2. Checks the remote and checkout, materialised credentials (when any), board reachability and the
   installed session manager. Board transport is created or read independently of that secret step. If
   `runtime.env` did not appear in step 1 and there is no store at all, it remains a manual operator
   step only for any other required host configuration.
3. Materialises `state/board` and `state/runs` from the checkpoint into a new local data plane. The
   derived JSON forms are built from the NDJSON, and counters are verified before any live write.
4. Idempotently imports the board and rebuilds the memory export and index from `state/memory/facts`.
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
5. Clones missing project checkouts from the registry remotes and creates the non-secret managed
   runtime-home files for the agent CLIs. Provider authentication stays manual.
6. Runs the pre-host materialiser: synchronizes role skills and recreates all role worktrees. When it
   runs under `sudo`, role worktrees and their Git administrative directories are assigned to
   `--installation-user`, so the user services can read and update them.
7. Rebuilds the pipeline worktree's live JSONL run source from the checkpointed normalized journal,
   after those worktrees and skills exist but before any dispatcher unit is installed or started.
8. Applies host units and session-manager automations, performs any required memory recovery, then
   verifies restore status. Heads are connected after bootstrap as a separate step.

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

Running `secretary recover` again is safe: the checkout is fast-forward only, the board restore checks
parity, the memory index is rebuilt, and the materialiser is a no-op on a second pass. A restored instance
stays recoverable itself: its own audit records about the restore go into the next checkpoint, and a
later recovery into another empty backend writes its events under a new namespace instead of counting
someone else's as already applied. That namespace lives in the restore state file, so retrying one
recovery stays idempotent.

A checkpoint taken before sprint entities entered the export still restores: it has no sprints file, which
reads as an installation without sprints, and `doctor` stays green on a completed restore. Terminals,
worktrees, the vector index, generated units and host caches are not copied from the remote. No object
store or separate backup host is required.

`secretary recover --dry-run` checks the checkout, credentials, runtime prerequisites and checkpoint
integrity, then prints the steps as `would-change`. The preview writes no local data plane, does not touch
the board, and runs neither the memory reindex nor the host materialiser.

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
