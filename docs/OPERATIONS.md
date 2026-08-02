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

## Install and check the code

```bash
python3 -m pip install .
python3 -m pip install '.[memory]'
python3 -m unittest
```

The first form installs the CLI, and the second adds the memory runtime. Host bootstrap currently
supports Ubuntu 24.04. It installs the pinned board and session-manager runtimes; `secretary install`
or `secretary recover` then applies the instance, as described in [Recovery](RECOVERY.md).

`secretary status --instance <dir>` gives the current summary of an installation. Its `--json` form is a
structured snapshot of services and timers, active attempts, checkpoint, memory and host resources, and
writes no state. `doctor` answers a different question: which invariants are broken. It stays a strict
check and its `--json` form returns a structured list of findings. Changing the host still requires
`reconcile plan` and a separate confirmed apply.

## Runtime secrets

Installation secrets live in a recoverable store (`secretary secret init/set/import`, the `secrets/`
directory of the private repository) and are materialised from there into env files. The `runtime.env`
next to `instance.yaml` can be one such target: the canonical values then live in the store and the file
is a materialised copy. Whether a given installation has been moved to materialisation is shown by
`secretary status --json` under `secret_store.materialize`; the product does not do this on its own, the
operator runs `secret import`. Either way the file is `0600`, is gitignored in the private repository,
and is part of no checkpoint or archive payload. `secretary shell` receives the whole file for a trusted
operator session; dispatcher-launched workers and reviewers receive only allowlisted board credentials
and non-secret runtime switches through the role-environment wrapper.

The store does not promise worker isolation: it has no broker and no grants, and the installation key
opens every secret at once, with the same rights that previously read `runtime.env` (see
[Recovery](RECOVERY.md#secrets)).

`doctor` raises a finding when catalog and values diverge, when the key is missing or unusable while the
catalog is non-empty, and when the key's permissions are wider than `0600`.

Instance config holds no secret materialisation inputs. `reconcile` builds the host plan from bindings
and config and never decrypts the store.

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
python3 -m secretary data init --instance INSTANCE
python3 -m secretary data export --instance INSTANCE [--copy-transcripts]
python3 -m secretary data raw-kanboard-dump --instance INSTANCE \
  [--container cp-kanboard] [--source-path /var/www/app/data]
```

`data init` creates the local layout and manifest. The canon for memory facts is
`INSTANCE/state/memory/facts`; the data directory keeps its derived export and index. `data export`
writes normalised board, memory, run and transcript exports; without `--copy-transcripts` only a
transcript inventory is kept. `raw-kanboard-dump` creates a timestamped raw dump by copying out of the
container; it writes nothing to the live container and does not use the board API.

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

## Status and doctor

`secretary status --json --instance INSTANCE` is the read-only operational snapshot. It is safe to poll:
it reports managed services and timers, projects and configured heads, active dispatcher attempts, their
workspace, watchdog pane, progress and respawn state, sprint observer heads, pause state, checkpoint
freshness, memory index state, and host disk, memory and load. A live invocation uses the dispatcher's own
pane probe for watchdog liveness; `--offline` deliberately reports that liveness as unprobed.

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
python3 -m secretary project add PROJECT_PATH --instance "$INSTANCE"
python3 -m secretary project provision-start PROJECT_ID --instance "$INSTANCE"
# the provision agent writes result.yaml next to task.yaml, taking run_id and scanner head from the task
python3 -m secretary project provision-apply PROJECT_ID --instance "$INSTANCE"
python3 -m secretary project gate PROJECT_ID --instance "$INSTANCE"
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
project; an installation holds one open sprint at a time, and a project another open sprint reserves is
refused as a resource conflict.

The entity is created by the product command, as the `po` role:

```bash
python3 -m secretary sprint create --role po --actor <actor> \
  --goal "<one sentence>" --dod-file DOD.md \
  --product <product-id> --issue issue:<ID> --project <project-id> \
  --observer <head-profile|none> \
  --repository <repo> [--repository <repo>]
python3 -m secretary sprint show --ref sprint:<ID>
python3 -m secretary sprint status --ref sprint:<ID>
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
refused.

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
  pane is ready again delivers one nudge, without waiting for the watchdog;
- `observer-watchdog-woke` — an event remained unacknowledged for
  `SECRETARY_OBSERVER_EVENT_WATCHDOG_SECONDS` (30 minutes by default), so the fallback woke the observer;
- `observer-wake-deferred` — the event wake failed, including a prompt the pane never took after its
  retries; the observer row carries its reason and bounded retry. After
  `SECRETARY_OBSERVER_WAKE_MAX_ATTEMPTS` (3 by default) such failures the batch is delivered by
  replacing the head instead, which reads as `observer-relaunched` with the failure as its reason;
- `observer-relaunched` — a head with unacknowledged work had a dead pid and was replaced;
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

The head profile comes from the sprint's own `sprint_observer` field: one concrete profile, or `none` for
a sprint that runs without an observer (see [Protocols](PROTOCOLS.md#the-declared-observer)). It is never
read from `role_defaults.observer` — a sprint that declares a profile the registry does not have is fenced,
not silently launched on a default. The one exception is an installation that has not yet run the
[observer cutover](#sprint-observer-cutover): its rows carry no field, and until the migration those launch
from `role_defaults.observer` as they always did.
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

Liveness is the same pid heartbeat as for worker and reviewer. A freshly launched head has not written its pid
yet, so an unreadable pid file counts as alive for the duration of the initial-output window and dead
afterwards. There is no automatic repair for a hung (as opposed to dead) head; that case is for the operator.

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
  it first, from the pid file in the intent itself: a live pid gives a launch-adopted outcome (the head is
  accepted, there will be no second one), a missing pid file inside the initial-output window gives a
  launch-pending outcome (the head is still coming up and nobody touches it), and a dead pid or an expired
  window drops the intent, closes what is left in the workspace, and lets the ordinary path launch a head
  again into the round the intent reserved.

A third case is a launch that failed after the terminal was created: prompt delivery failed but the pane did
not close, or the reviewer came up but the worker head could not be stopped. The host returns that as a
distinct aborted outcome and the tick reports a launch-aborted action, status degraded. The card does not go to
Blocked and the record is not deleted: a live head would be left with no pointer to it. The intent stays on disk
together with the handle from the error, and the next tick resolves it like any other.

Everything the tick does with an already-launched head — reading its pane id, writing the routing event, saving
the record — happens while the intent is live, so a failure at those steps does not mean "there is no head" and
is reported the same aborted way. By then the intent holds the launch configuration, so an adopted head reaches
the routing journal with its own profile rather than whatever the registry holds now. A journal that fails at
that write gives an adopt-deferred outcome (degraded): the head stays adopted, the intent stays on disk, and the
next tick appends the journal entry.

A head adopted that way usually has no handle, because the tick that launched it did not survive to record one.
Its liveness is read from the pid heartbeat and reported as such in terminal status. It is also stopped that way
— before review starts, on respawn, on a red review and on freeze — which is why the role's pid file is kept in
the record.

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
causal acknowledgement, deadline, retry state and external-failure reason.

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
unfinished, naming the number of pending records. The staged writes are listed and repaired by their own
commands, and no file under `board/product-issue-transactions/` is ever moved by hand:

```bash
secretary product transaction list --data-dir DATA_DIR
secretary product transaction retry --request-id REQUEST_ID --data-dir DATA_DIR
secretary product transaction discard --request-id REQUEST_ID --data-dir DATA_DIR
```

`retry` is the first move: it resumes the operation where it stopped and commits its audit event. `discard`
is for a transaction the backend never accepted; it reads the board first and refuses with `live_write` when
the row or the board comment of that request already exists. A document that is already outside the journal
comes back with `secretary product transaction adopt --path FILE`, which files it under its own request id
and removes the copy, after which `retry` and `discard` see it again.

## Board column schema

Install creates the Pipeline columns and then refuses to reshape a board that already holds cards:
renaming a column in place would change what its cards mean, and removing one moves every card it
holds to the trash. A live board that predates a column therefore needs one explicit repair:

```bash
python3 -m secretary board migrate-assessment
```

It adds the `Assessment` column at position 5 of a board that carries the earlier six-column layout,
without moving, reordering or trashing a card and without renaming an existing column. It reads the
Kanboard credentials from the runtime environment, like `secretary task`. Every outcome is
retryable: a finished board reports `unchanged`, a run whose `addColumn` committed but whose answer
was lost leaves the six columns plus a trailing `Assessment` and the next run finishes that column
(`resumed`) instead of adding a second one, and any layout that is none of those three is refused
with all of them named. Every run proves that each card's column and position are unchanged before
it reports success. After it runs, install accepts the board unchanged.

`python3 -m triggered_agents pipeline setup` is not a migration and never was: it reconciles columns
by index, so it refuses a board that holds cards unless the layout already matches, and points here.

## Sprint observer cutover

Before this migration a sprint declared no observer and the dispatcher picked a head from
`role_defaults.observer`. After it every sprint row carries one explicit value and the reader is
strict. Those two facts cannot hold at different moments on a running installation: a strict reader
let loose on a row that predates the field calls the live sprint corrupt and fences its projects. So
the migration is one ordered sequence that stops the pipeline, and it is an operator action.

Look first, from anywhere, without stopping anything:

```bash
python3 -m secretary sprint migrate-observer --instance INSTANCE --dry-run
```

It prints the value it would write for every row: `{"kind":"head",…}` for the open sprint,
recovered provenance for each closed row whose last successful `observer_launched` /
`observer_relaunched` event names a head, and `migration_unknown` for a closed row that never
launched one. Read that list before going further; a closed row whose provenance looks wrong is
worth resolving now rather than after it is on the board.

Then the cutover proper:

```bash
python3 -m secretary pause freeze --instance INSTANCE --reason "observer cutover"
python3 -m secretary pause-status --instance INSTANCE   # confirm every head is stopped
python3 -m secretary sprint migrate-observer --instance INSTANCE
```

The freeze must carry no `--exclude-workspace`: a head left running keeps writing to the board this
migration rewrites. The command refuses before its first write if the pipeline is not frozen, if the
freeze carries exclusions, if any record still names a head, if the production state cannot be decoded, or if the head
registry cannot be read.

"Names a head" is the same rule the freeze stops by, not just a pane handle: a pane leaf, a pid
heartbeat, or an unresolved launch intent all count, for worker, reviewer and observer alike. A
head adopted from a launch intent never had a handle, and a stop the host refused leaves the record
pointing at its head on purpose — those are the heads most likely to still be writing.

The unreadable-state refusal matters more than it looks: `pause freeze` sets the flag and stops
*nothing* when the records are unreadable, so an empty decode is the one case where a live head is
both most likely and least visible. Repair
`DATA_DIR/dispatcher/production-state.json` first.

It then runs, in this order and no other:

1. pre-migration checkpoint, written and pushed to the remote while the tolerant reader is still in
   force, so there is a state to roll back to that describes the installation as it ran;
2. the immutable versioned inventory and journal, persisted under
   `DATA_DIR/sprints/observer-migration/`;
3. one idempotent write per row, under a request id derived from the inventory digest and the ref;
4. a strict full rescan of every row, including resolving each open row's declared head against
   the head registry, so a profile removed between the freeze and the cutover stops the migration
   instead of activating a reader that would fence the row it just migrated;
5. the durable `observer_migration_completed` audit event, which is what makes the installation
   migrated;
6. post-migration checkpoint, pushed again, now carrying that event;
7. the durable `observer_migration_activated` event, which records that this cutover reached that
   recovery point and closes its open interval;
8. the local strict-reader latch, `DATA_DIR/sprints/observer-strict.json`;
9. resume.

Both pushes are real pushes. The dispatcher's ordinary pusher runs on a 30-minute window and inside
it simply hands back the previous state; the cutover bypasses that window and refuses on any
outcome short of the commit being on the remote, because a checkpoint that never left the machine
is not a recovery point.

Any step that refuses leaves the freeze in force and the strict reader off. Rerun the same command:
provenance is read back from the journal rather than recomputed, a row that already carries exactly
the selected value is skipped without a backend write, and a row carrying a *different* value stops
the migration for an operator to resolve by hand. A rerun after a crash resumes from wherever the
sequence stopped and reports `already-migrated` only once the latch at step 7 is in place. Pass
`--no-resume` to keep the freeze after a successful cutover and lift it yourself with
`secretary resume --instance INSTANCE`.

The one case the command cannot decide is an open sprint whose running head it cannot prove: no
tracked observer record and no successful launch event, or the two disagreeing. It names the sprint
and refuses. Settle which head is running, then rerun. `--dry-run` reports the same refusals under
`refusals`, so a head that has left the registry is visible before the pipeline is stopped.

After the cutover, `secretary sprint create` and `secretary sprint reopen` both require
`--observer`, and an open sprint with missing or unreadable metadata fences its own projects with a
critical outcome instead of falling back to a role default.

### Why the completion event and not the marker file

Whether this installation is migrated is read from the `observer_migration_completed` event in
`state/board/events.ndjson`, not from the marker file. The audit log is checkpoint canon and a
replacement host gets it back (see [Recovery](RECOVERY.md#what-the-checkpoint-contains)); the marker
file is local runtime state and does not survive. Deriving the answer from the log is what stops a
recovered host from quietly returning to `role_defaults.observer` after a completed migration.

The event alone is not the whole predicate, because it is written at step 5 and the order is not
finished until step 7. On the host running the cutover there is a live interval in which the event
exists and the post-migration checkpoint has not been taken and pushed; strict there would be strict
before the recovery point the order requires. The cutover's own working set under
`DATA_DIR/sprints/observer-migration/` marks that interval, and it is local by construction — it is
not checkpoint canon, so a recovered host never has it and is never mistaken for a host mid-cutover.
The three states are:

| signals | reader |
| --- | --- |
| no completion event | tolerant |
| completion event, plus an inventory on this host with no activation naming it | tolerant — mid-cutover |
| the latch, or a completion event with no cutover in flight | strict |

The inventory is retained after a successful cutover on purpose — a retry reads it instead of
recomputing provenance — so its presence alone cannot mean "in flight". The `observer_migration_activated`
event is what closes the interval, and it is in the append-only log rather than in a file: losing or
damaging the local latch cannot take strictness away from an installation that finished its migration.

The marker is the latch over that fact: written last, so a rerun after a crash keys its resume on
it, and so the ordinary read is one stat rather than a scan.

The same rule decides what recovery accepts. A checkpoint taken *after* the migration must carry an
observer value on every row; one missing it is a corrupt export and the restore refuses before its
first backend write.

A checkpoint taken *before* the migration carries none. Its closed rows come back as they were, and
the installation comes back tolerant, with this migration still the way forward. Its one open row is
the exception, because an open sprint may never be published without an executable value: the
restore recovers that sprint's head from `state/board/events.ndjson`, which the checkpoint carries,
by the same latest-successful-launch rule the migration uses, and writes it before the reference
publishes the row. It is a per-row recovery of a fact, not the migration: no completion event is
written and the reader stays tolerant.

If that open row has no successful observer launch anywhere in the log, or its recovered head has
since left the head registry, the restore names the sprint and refuses. The repair is to declare the
value by hand in the checkpoint's `state/board/sprints.json` before restoring — add
`"observer": {"kind": "head", "profile": "<profile>"}` or `"observer": {"kind": "none"}` to that
row — and run the restore again.

## Recovery

The Git-backed checkpoint and the full recovery sequence are documented in
[Recovery](RECOVERY.md#fresh-install-and-recovery). On a clean replacement host, bootstrap the pinned
runtimes and use `recover` rather than `install`:

```bash
sudo secretary bootstrap --instance-remote REMOTE --instance-dir INSTANCE --installation-user INSTALL_USER
sudo secretary recover --instance-remote REMOTE --instance-dir INSTANCE --installation-user INSTALL_USER \
  --recovery-phrase-file PHRASE_FILE
```

The low-level `bootstrap --empty`, `restore-board`, `memory reindex`, `reconcile apply` and `restore-reconcile`
commands remain diagnostic primitives, not the main runbook.

## Optional cold archive

`backup create` and `backup verify` are a manual tool for dumping raw material, not a recovery contract. There is
no timer, no offsite transfer and no `doctor` gate for them.

```bash
python3 -m secretary backup create --instance INSTANCE --kind both
python3 -m secretary backup verify ARCHIVE.tar [--strict]
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

A release that fails is split on one fact, read from the remote rather than from where the failure
happened: whether the branch reached the base. It did not, and the card stays parked with the
failure recorded and the decision taken back down; it did, and the release is finished on top of a
merge that is now a fact, or the card goes to Blocked saying exactly that. A failed release never
sends a card back for rework and never publishes twice.

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
python3 -m secretary pause drain  --instance INSTANCE --reason "why"
python3 -m secretary pause freeze --instance INSTANCE --reason "why"
python3 -m secretary resume       --instance INSTANCE
python3 -m secretary pause-status --instance INSTANCE
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
without reading session text is the pid the head writes before exec: the launch command is wrapped so the shell
records its own pid and then `exec`s the head, replacing the process image without a fork, so the recorded pid stays
the head's pid for its whole life. On each waiting tick the dispatcher probes that pid with a null signal: a
connected pane whose pid no longer answers takes the same path as a missing or disconnected pane. The file lives
outside the workspace, like report and verdict bodies, under `SECRETARY_DISPATCHER_BODY_DIR` (default `/tmp`);
respawn deletes it before a new launch so a dead predecessor's pid is not read before the new head overwrites it.

If the pid probe confirms the head's process is alive, that is a positive liveness signal rather than merely an
absence of proof of death, and the tick skips both timeouts — the short first-output window and the long idle
ceiling — regardless of whether the last-output timestamp moved. Silence from a live head with a confirmed pid
proves nothing, so only a real process exit triggers respawn or Blocked for it. While the file does not exist yet —
a fresh launch has not run its write, or the runner does not provide this signal at all — that is read as neither
death nor confirmed life, and the tick keeps using the ordinary last-output checks. The only runner without the
signal is the raw command override, which substitutes a command bypassing the head registry and therefore gets no
heartbeat wrapper. For it, as for a session manager without a last-output timestamp, the long ceiling is the only
fallback, precisely because there is no way to confirm liveness independently of pane output.

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

A respawn writes a comment on the board, so the operator can tell a first stall from a card whose head has already
been restarted, without waiting for the final Blocked.

- `SECRETARY_INITIAL_OUTPUT_STALL_SECONDS` — the short first-output window, default 180 seconds.
- `SECRETARY_REVIEW_VERDICT_STALL_SECONDS` — the ceiling for a verdict after first output, default 5400 seconds.
- `SECRETARY_WORKER_REPORT_STALL_SECONDS` — the ceiling for a report after first output, default 21600 seconds.

All three are read at check time; garbage or a zero value falls back to the default, so a typo in a unit file does
not stop the dispatcher from starting.

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

## Background-role telemetry

```bash
python3 -m triggered_agents health
```

One line per role: whether its timer is active and how fresh its last healthy tick is. A non-zero exit means at
least one role is red.

The sources are the live data plane, not a checkout:

- curator, steward and retro write a run log through their shared agent state, that is, under `$TA_STATE/<agent>/`
  or, when that variable is unset (as it is in the packaged units), under the data directory. Healthy means the last
  event without an error result: the precheck writes an error every tick until the board and environment come up, so
  by the raw last event a dead role would look forever fresh.
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
a secret.

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
| `head-registry` | generate `heads/heads.yaml` from this installation's canon plus `heads/source.yaml`, naming that canon, its owner, and the checkout and revision it came from |
| `role-skills` | `role_skills sync` into the shells' skill directories |
| `role-worktrees` | fast-forward the role worktrees onto the base branch |
| `host` | `reconcile apply`: units from `packaging/systemd` plus session-manager registrations |
| `automations` | create or repoint session-manager automations from `automation.toml` |
| `memory` | restart the memory service if its code, dependencies or unit changed |
| `verify` | a repeat dry run: the second rollout must be a no-op |

Flags: `--no-pull` (re-materialise only), `--base-branch`, `--product-root`, `--runtime-user`,
`--json`.

### Upgrading from another checkout

`--product-root` names the checkout to install. Every step then reads that checkout and nothing
else: its `skills/manifest.toml` and `skills/roles/` tree, its `packaging/systemd` templates, its
`triggered_agents/agents/*/automation.toml` specs, the role worktrees it declares, and its head
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
| the role runtime env file | `TA_RUNTIME_ENV_FILE` / `SECRETARY_RUNTIME_ENV_FILE`, else `<instance>/runtime.env` |
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

A live tick reads the head registry only from the installation's own `heads/heads.yaml` and never looks into a product
checkout. The only operation that moves that file is `secretary upgrade`, which writes `heads/source.yaml` next to it
with the canon it generated the snapshot from and the checkout path and revision of the product that generated it. So
editing the product's head canon in a working tree (a branch, an uncommitted change, a half-finished refactor) has no
effect at all on a running installation.

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
head, and `role_defaults.observer` is read only on an installation that has not yet run the
[observer cutover](#sprint-observer-cutover). Each background role's `automation.toml` still carries a `head`, but only
as a last resort for a registry that routes that role nowhere. The packaged unit of every one of those roles exports
`SECRETARY_INSTANCE` and the path of its own `runtime.env`, so each process resolves the same installation's snapshot
rather than the host's default one. A head the dispatcher launches starts in a terminal Orca creates and inherits none
of that, so the launcher writes both names into the head's own command line. `SECRETARY_INSTANCE` from a `runtime.env`
never overrides either: which installation a role belongs to is decided by whoever started it, and `runtime.env` is a
file inside an installation.

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
python3 -m unittest
```

`--probe` is a real dry tick: it takes the same singleton lock, passes the same mutation guards, scans the same card
states and runs the same decision logic, but the first write turns into an abort and lands in the report as "what the
next tick would do". A green probe with a broken tick is impossible, because a broken tick fails here too.

### Head readiness

Before a new worker, reviewer or observer launch, the dispatcher reads the profile's resource from `heads/heads.yaml`
and runs its probe. The verdicts are cached in the data directory and can be inspected without running a card:

```bash
secretary dispatcher resource-health --instance <dir>
```

The check is cached for 300 seconds. That limits probe spend to one cheap call per resource per window, even though the
production tick may run more often. `ready` allows a launch. `unauthenticated` and `unavailable` leave a new card in
Ready until the next check, without a claim and without occupying a project slot. That choice avoids a claim/refuse
loop and does not turn a temporary provider problem into an operator's Blocked card. For a card already taken, a repeat
worker launch blocks it with the reason, preserving the attempt's context. For an observer head the same two verdicts
mean a deferred launch: the sprint stays open, the reason is visible in the observer record, and the next tick tries
again.

`unknown` means the probe itself could not be run or classified reliably. It is visible in the snapshot but does not
forbid a launch: a failure to observe does not prove the resource is down and must not stop the queue forever.

If a resource shows `unauthenticated`, re-authenticate that runtime's CLI in the runtime home the profile names, then
wait out the TTL or check the next tick. On `unavailable` do not restart cards: check the provider's status, wait for
the next TTL and re-read the readiness snapshot. Fallback routing is not part of this.
