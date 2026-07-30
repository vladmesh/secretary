# Git-centric recovery

This is the single recovery contract. The private installation Git repository is the durable
checkpoint of configuration and portable state. Moving to a new machine requires installing the
product, access to that repository, and re-entering the credentials it deliberately does not hold. A
separate bundle or object-store transport is not part of the main path.

## Topology

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
  `heads.yaml` snapshot, and the `source.yaml` pin recording which canon, checkout and revision it
  was made from; after a restore the pin shows which product version the
  installation was running, and bringing the snapshot up to a new checkout is `secretary upgrade`'s
  job;
- board export: `state/board/cards.ndjson`, `state/board/sprints.ndjson`,
  `state/board/events.ndjson`, `state/board/export.json`;
- run and audit state: `state/runs/runs.ndjson`, `claims.json`, `watermarks.json`, `export.json`;
- memory facts: `state/memory/facts/**`;
- knowledge documents: `state/knowledge/**` (free-form markdown, see
  [Architecture](ARCHITECTURE.md#knowledge-planes)).

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

The board export is kept only as NDJSON, for line-wise diffs. The JSON duplicates are not part of the
checkpoint.

Pipeline cards, including Product and Issue records, and sprint entities go into the checkpoint as separate sets: sprints live on their own
board and are not part of the card export, so the writer reads them in its own pass instead of
deriving them from cards that reference a sprint. A sprint record carries its reference, goal,
Definition of Done, repositories, owning product, issues, reserved projects, status, budget by event
type, current card, resume entry, all entries
on the entity and the source's audit metadata. A sprint closed before a sprint owned a product has none
of those three fields on its row, so its record omits them instead of storing an empty value, and a
checkpoint written before they existed omits them the same way. A sprint the checkpoint catches in
`opening` is recorded with that status and with whatever it had written by then, which may be no goal and
none of the ownership fields; the export invents nothing for it. Derived values (budget totals,
installation thresholds, resume freshness) are not stored: they are recomputed from the record and
configuration.

## Layout

```text
<private repository>/
  instance.yaml, persona/, projects/, adapters/, heads/, policies/   config, committed by the operator
  state/                                                             state, committed by the auto-writer
    board/   cards.ndjson, sprints.ndjson, events.ndjson, export.json
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
  move, the tick skips the commit;
- push to the remote every 30 minutes;
- durable RPO on machine loss is 30 minutes. Local commits give fine-grained history and fast local
  rollback, but they do not survive the machine.

## Writers

Four writers touch the repository, each with its own pathspec:

- tick writer: `state/board`, `state/runs`, at the end of a dispatcher tick under the tick lock;
- memory writer: `state/memory`, on `propose`/`commit`/`supersede`;
- knowledge writer: `state/knowledge`, on `secretary knowledge write`;
- secret writer: `secrets/` (plus `.gitignore` on first `init`), on `secret init/set/import/remove`.
  `secret list` and `secret materialize` are not part of this writer.

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

## Failure and divergence

Push failure (network, forge or auth unavailable during the push window) is fail-closed on the
checkpoint, not on the work. Local commits continue, the dispatcher keeps running, and the growing
checkpoint lag is visible in `status` and `doctor`. The next 30-minute push retries.

Remote divergence (the remote has commits that are not local) leaves the auto-writer fast-forward
only. Force-push and history rewriting are forbidden. On a non-fast-forward the push stops, `status`
raises a `remote diverged` alarm, and the operator resolves it.

## Secrets

The host `runtime.env` is mode `0600`, is not part of the checkpoint, and holds only the
machine-generated board URL, API user and API token. Forge access and interactive head logins stay in
the operator's password manager and are never copied to the host by the product.

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

`status --json` carries a `secret_store` section: whether the store is initialised, how many secrets it
holds, when the catalog last changed, whether a usable installation key exists, and a summary of
materialisation targets — without a single value, key or phrase in any form. `doctor` raises a finding
when catalog and values diverge, when the key is missing or unusable while the catalog is non-empty, or
when the key's permissions are wider than `0600`. A healthy store and a completely absent one both
produce no findings.

## Fresh install and recovery

Install the product with the memory extra first. On Ubuntu 24.04, `secretary bootstrap` installs the
pinned board and session-manager runtimes, generates `runtime.env` and creates the Pipeline board.
`secretary install` installs neither and fail-closed checks both runtimes before changing live state.

On a clean host, bootstrap creates the checkout, a local `0600` `runtime.env` and the Pipeline board
without manual entry of board credentials:

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

`runtime.env` stays a gitignored ordinary file with mode `0600`. Bootstrap generates it on a fresh
install, prints no values and adds it to no commit.

`recover` runs one supported sequence:

1. Opens the secret store, if this instance repository has one, before reading `runtime.env`. With
   `--recovery-phrase-file` or `--recovery-phrase-stdin` (or an interactive prompt on a TTY when the key
   is not yet on disk), the installation key is rebuilt and values are materialised into the files the
   catalog names, including `runtime.env` if any secret materialises there. Without the phrase this step
   writes nothing and reports locked/missing, and `runtime.env` stays whatever is already on disk.
2. Checks the remote and checkout, credentials, board reachability and the installed session manager. If
   `runtime.env` did not appear in step 1 and there is no store at all, it remains a manual operator
   step.
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
   issues or its reservations comes back without those metadata keys rather than with empty ones, and an
   entity exported as `opening` comes back as `opening` rather than as an open sprint. Parity
   compares whether each of the three fields is there at all, not only what it holds: a restored entity
   that gained an empty `product` its export never carried is a lossy write and fails the check.
5. Clones missing project checkouts from the registry remotes and creates the non-secret managed
   runtime-home files for the agent CLIs. Provider authentication stays manual.
6. Runs the same materialiser as `secretary upgrade`: recreates role worktrees, installs units,
   registers session-manager resources and applies automations.
7. Checks restore status. Heads are connected after bootstrap as a separate step.

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
