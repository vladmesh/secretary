# Protocols

`--instance` accepts either an instance directory or a direct path to `instance.yaml`. The instance
holds installation configuration and the portable checkpoint in `state/`; the data directory holds
local mutable and derived runtime state.

## Checks and host ownership

```bash
python3 -m secretary doctor --instance INSTANCE
python3 -m secretary doctor --offline --instance INSTANCE
python3 -m secretary doctor --instance INSTANCE --host-fixture DIR
```

`doctor` is always read-only. A normal run checks config, data and live inventory; `--offline` keeps
only config and data; `--host-fixture` replaces live inventory with a deterministic fixture. A fixture
cannot be combined with `--offline`. Exit code `0` means the check completed with no findings, `1`
means findings (or warnings under `--strict`), `2` means invalid input or unreachable inventory.
Without `--strict`, warnings alone stay green.

Live parity is derived from the same desired state as `reconcile`: each project checkout is checked
against the normalised absolute path from its binding, including a path outside the projects root; the
projects root itself is only needed to find unmanaged checkouts. An unreachable or unnormalisable
expected checkout makes project inventory unavailable and yields code `2` rather than a missing-on-host
finding. Unit files, session-manager registrations and the required enabled/active state of
long-running services and timers are checked. A missing resource or an unhealthy required runtime state
is a finding and code `1`; a oneshot service may be inactive. Units listed in `foreign_units` are
excluded from managed parity and are not conflicts.

```bash
python3 -m secretary reconcile plan --instance INSTANCE [--host-fixture DIR]
python3 -m secretary reconcile adopt --instance INSTANCE --logical-id ID [--yes]
```

`reconcile plan` reads desired state and inventory, applies nothing and writes no manifest.
`--offline` is deliberately rejected. Code `0` means a plan without conflicts, `1` means conflicts, `2`
means invalid input or unreachable inventory.

`reconcile adopt` touches one existing desired session-manager registration. It checks the name and the
normalised repository path, shows a fingerprint, and stays a preview without `--yes`. A confirmed run
atomically adds a managed record without changing the session manager, systemd or worktrees. Unit
resources are not adopted through this path.

## Tasks

The public path to the board is `secretary task`. A card carries a `ref`, project, type, state,
dependency, claim, routing, workspace, retry and audit metadata:

```text
Issues → ready → in_progress → validate → done
                         └────────────→ blocked
```

```bash
python3 -m secretary task list --project PROJECT
python3 -m secretary task show --ref PROJECT-N
python3 -m secretary task list --sprint sprint:ID
python3 -m secretary task create --role po --project PROJECT --type code \
  --title TITLE --state ready --head codex-extra --codex-mode exec --sprint sprint:ID
python3 -m secretary task archive --role po --ref PROJECT-N \
  --reason-file REASON.md --request-id REQUEST_ID
python3 -m secretary task edit --role po --ref PROJECT-N \
  --body-file SPEC.md --head codex-terra --review-head claude-opus
python3 -m secretary task create --role po --project PROJECT --type code --title HOTFIX \
  --sprint-override --sprint-override-reason-file REASON.md
```

`create` accepts `--description` or `--body-file`, plus dependency, workspace and routing fields.
On a Pipeline with the Issues column, a new execution task requires `--sprint`: the sprint must be
open and the task project must be one of its reservations. A closed sprint and an unreserved project
are separate errors, both before the first backend write. Tasks never accept product priority;
`--priority` is rejected rather than ignored. Execution tasks are created in Ready, never in Issues.
Old task-shaped Ideas remain readable after the supported board migration but are fail-closed: only
the PO may explicitly triage one to Ready, which marks it as a task without inventing Product, issue
kind or priority. `--codex-mode` is valid only for a worker profile on a `codex` adapter. Without an
override, launch mode comes from the head profile.

`archive` closes an execution task in the backend and removes it from ordinary active listings without
deleting board history. It is PO-only, requires a non-empty reason, writes append-only audit and
supports idempotent retry through `--request-id`. Only a card with no live work can be archived:
in-progress and validate cards, and cards with an active claim, are rejected. A card closed from Done
stays a satisfied dependency; a card closed from any other column is not Done and does not unblock
anything. It cannot close a Product or Issue: use `secretary issue close` for the latter.

`edit` replaces a card's spec in place: `--title`, `--description`/`--body-file` (the full new text, not
a diff), `--head`, `--review-head`. PO, dispatcher and observer may edit, but an ordinary card is only
editable in Ideas, Ready or Blocked: on an active card the worker is working against a snapshot of the
task document, so an edit goes through preempt and requeue rather than a silent swap. The `edited` audit
event records the old and new digests; the full text of past versions is recoverable from the Git history
of the board export in the checkpoint. Comments stay the dialogue of an attempt; the spec lives only in
the description.

## Products and issues

`secretary product` and `secretary issue` use typed records in the existing Pipeline backend. They do
not introduce a file or a second board as a competing source of truth, so normal board export,
checkpoint and restore carry their metadata and comments.

```bash
python3 -m secretary product create --role po --id secretary --project secretary --title Secretary
python3 -m secretary issue create --role po --product secretary --kind feature --priority P2 --title TITLE
python3 -m secretary issue list --product secretary
python3 -m secretary issue show --ref issue:123
python3 -m secretary issue update-priority --role po --ref issue:123 --priority P1 --reason REASON
python3 -m secretary issue close --role po --ref issue:123 --reason resolved
```

A Product id is stable and its non-empty project set must contain only ids registered under the
instance `projects/` directory. Product ids cannot be duplicated. Every new issue requires its
Product, one kind (`bug`, `feature`, `question`, `improvement`) and one priority (`P0` through `P3`).
Priority changes require a non-empty reason, add an `[issue:priority]` board comment and append a
durable audit event. Only the PO may close an issue, using exactly one of `resolved`, `invalid`,
`duplicate` or `wont_do`; closure archives the backend record but leaves its comments and audit
history available through `issue show --ref` and checkpoint recovery. `issue list --closed` includes
both open and closed issues; without it the list contains only open issues.

A Product and an Issue are not execution tasks and never enter the execution columns: `move` and `claim`
both reject one before any write, whatever column it currently sits in. Work on an issue is a separate
card the PO creates in Ready.

A record belongs to the board rather than to one project, so every Product and Issue row is created in a
single lane: the board's first active swimlane in the board's own order, position first and the swimlane
id as the tie-break. The order is a property of the board, so concurrent writers and retries choose the
same lane, and a board without named swimlanes keeps Kanboard's implicit default lane.

Every Product and Issue write is staged before it touches the backend, and a staged write that is neither
finished nor dropped blocks checkpoint and board export. A refusal that a retry cannot turn into a success
therefore has to end the transaction rather than leave it: a `createTask` the backend declines is reported
as `backend_rejected`, and once the board shows no row of that request the staged document is dropped with
it. `validation` and `closed` refusals before the first backend write end the same way.

A staged write that did reach the backend stays, and belongs to its own request id. Its supported repair is:

```bash
python3 -m secretary product transaction list
python3 -m secretary product transaction retry --request-id REQUEST_ID
python3 -m secretary product transaction discard --request-id REQUEST_ID
python3 -m secretary product transaction adopt --path FILE
```

`retry` finishes the staged operation exactly where it stopped and commits its one audit event; a request
already committed is answered with its record. `discard` drops a transaction only after reading the board:
a create whose row exists and a priority or close change whose board comment exists are refused as
`live_write` and have to be retried instead. `adopt` files a transaction document that lives outside the
journal back under its own request id, which is how a document carried out of the journal comes back into
`retry` or `discard`. The commands cover Product and Issue writes alike; the journal is one.

## Sprints

A sprint is a data entity on a separate `Secretary sprints` board, not a Pipeline card. One board task
is one sprint. The board is created lazily and idempotently, so a repeat call creates no duplicate. A
reference has the form `sprint:ID`, a separate namespace from the `PROJECT-N` card convention.

```bash
python3 -m secretary sprint create --role po --goal GOAL --dod-file DOD.md \
  --product PRODUCT_ID --issue issue:ID --project PROJECT_ID \
  --repository REPO --request-id REQUEST_ID
python3 -m secretary sprint list --status open
python3 -m secretary sprint show --ref sprint:ID
python3 -m secretary sprint status --ref sprint:ID
python3 -m secretary sprint comment --role worker --ref sprint:ID --body-file NOTE.md
python3 -m secretary sprint current-task --role dispatcher --ref sprint:ID --task PROJECT-N
python3 -m secretary sprint budget --role dispatcher --ref sprint:ID --type red_ci
python3 -m secretary sprint resume --role observer --ref sprint:ID --body-file RESUME.json
python3 -m secretary sprint reopen --role po --ref sprint:ID
python3 -m secretary sprint close --role po --ref sprint:ID
```

Stored fields are the goal, the Definition of Done text, repositories, the owning product, its issues,
the reserved projects, open/closed/stopped status, a
budget counter by event type, the current card and a structured resume entry. The six valid budget event
types are `red_review`, `blocked`, `red_ci`, `preempt`, `recreated_task` and `hotfix`. Production derives
them from durable card audit events: a red review, a move to Blocked, a red mechanical gate, a preempt of
an active card back to Ready, or a tagged recreation or hotfix creation. The card-event id becomes the
budget request id, so a repeated tick cannot charge it twice. Green cards and observer activity have no
matching event and do not move the counter.

A new sprint belongs to a Product, serves at least one of its open Issues and reserves at least one
registered project. `--product` names an existing Product, every `--issue` is an open Issue of that
Product, and every `--project` is an id from the instance project registry; `--repository` keeps its own
meaning as the write-guard scope. An Issue of another Product and a closed Issue are refused with their
own messages. One installation holds at most one open sprint: a second `create` is refused as
`sprint_conflict` naming the open one. A project another open sprint already reserves is refused before
that, as `resource_conflict` naming the project and its holder. Every one of these checks is a read, so a
refused sprint leaves no board row, no metadata and no audit event. A repeated `--request-id` still
returns the first event instead of colliding with the sprint it already opened.

Because the rules are reads of live state, `create` and `reopen` hold one exclusive lock on the data
directory (`sprints/admission.lock`) across the check and the write it admits. Two writers on the same
installation are serialized by it, so the second sees the sprint the first opened rather than a state
from before it. The lock is an admission gate only: it holds no sprint state and is released with the
write.

Admission runs in one fixed order, the one Product and Issue writes already use. `create` and `reopen`
are the two transitions into `open` and both run it, on that same staged-intent journal:

1. take the admission lock;
2. under it, settle the request id first. A committed or staged intent of the same request id comes back
   as it is, before any check of live state, so a repeat that overlaps the request it repeats is replayed
   rather than refused as a conflict with the sprint it opened itself. The same request id carrying a
   different payload is refused as `validation` before any side effect;
3. check product, issues, registry and both conflict rules only for a fresh request. A staged intent is
   resumed on the state it was admitted on, so a Product or Issue that changed after the refusal does not
   turn a repeat into a validation error;
4. apply the backend steps through the staged intent. Each one recognises what an earlier attempt of the
   same request already did. A metadata answer other than `True` is a backend refusal, not a success:
   the call reports `audit_pending` instead of `created` or `reopened`, the staged intent stays and the
   retry with the same request id finishes that same operation;
5. commit one audit event, however often the delivery repeats.

The sprint reference is written last, and writing it is what publishes the sprint: a row on the sprint
board counts as a sprint only once it carries one. An interrupted create is therefore never observed as
an open sprint without its product, issues and reservations.

An unfinished create holds nothing. Instead of holding the installation it compensates: a step refused
after the row was created takes that row back, so no unreferenced row is left behind, and only when the
backend also refuses that does the row stay for the repair to pick up. Before it publishes, a resumed
create or reopen re-checks both conflict rules; if another sprint took the slot or the project meanwhile, the
repeat is refused as `sprint_conflict` or `resource_conflict` naming that sprint and publishes nothing.
The losing request is then filed again as a fresh one. Like a staged Product or Issue write, an
unfinished sprint create blocks the checkpoint until it is retried or dropped.

Sprints created before ownership existed carry none of these fields. They stay readable, exportable and
restorable exactly as they are, and nothing fills the fields in for them: `show`, `status`, the board
export and the checkpoint record leave the three fields out rather than answering `""` and `[]`. `reopen` re-checks every rule
above, so such a sprint is refused with a message that names what it lacks; the supported move is to open
a new sprint that owns its issues. `reopen` is refused the same way when the sprint's own issues have
since been closed or its projects are held elsewhere.

`sprint close` freezes the active cards linked to that sprint. It archives its terminal Done tasks with
the normal task archive audit, leaves linked non-terminal cards on the board, and returns both lists.
The Done transition clears the completed worker claim and its resolved routing fields, so that stale
ownership does not prevent normal terminal archival; `archive` still refuses a live claim.
Cards without that `sprint_ref` are not considered. Product and Issue records are never closure targets,
including if malformed metadata links one to the sprint, so an Issue remains open until the PO calls
`issue close`. The close request is staged: retrying the same request id after a lost archive or status
reply resumes the same task set, does not archive a task twice, and records one sprint close event.
Legacy sprints without reservations are closed without retroactively archiving cards.

Installation config may set `sprint_budget.signal` and `sprint_budget.hard`; defaults are 3 and 6. The
schema resolves omitted values to those defaults before rejecting a hard limit below the signal limit.
Each charge is a `budget_recorded` audit event; the charge that stops a sprint is paired with a
`budget_hard_stopped` event carrying the hard limit and the triggering card-event identity. `show`
returns the thresholds and `signal_reached`/`hard_reached` with the totals. The signal appears in a newly
launched observer prompt but does not stop work. At the hard limit the dispatcher marks the sprint
`stopped`, stops its observer and skips new linked claims; active cards continue their normal cycle. Only
`sprint reopen --role po` clears the stop.

`sprint resume` accepts JSON with required string fields `selected_step`, `selected_why`,
`rejected_alternatives`, `current_task`, `dod_state` and `next_safe_step`. It is stored separately from
normal comments and carries a `[sprint:resume]` marker. `show` and `status` compute freshness from card
audit records: missing data is `resume_missing`; a resume may trail a successful non-routing, non-guard-denied card event for up to
five minutes, then is `resume_stale`. Neither command reads an observer transcript. The dispatcher records a
durable delivery batch before it wakes or replaces an observer. An observer acknowledges it by passing the
matching `--delivery-id` and `--through-event` from `status` to `sprint resume`; those values are audit payload,
not part of the six stored resume fields. `secretary status --json` exposes the
same entity-derived state for every sprint in `installation.sprints.items`, including stopped status and
its reason, budget, resume freshness and observer state. If the live board cannot be read, that fact is
reported in `installation.sprints.error`.

`task create --sprint` records the sprint reference in Pipeline-card metadata. `task show` and
`task list` expose it as `sprint`, and `task list --sprint` filters by it. `sprint show` derives its
`cards` list from that live card metadata rather than storing a duplicate list. New links and comments
are refused after a sprint is closed. `current-task` additionally requires that the selected card already
carries this sprint reference.

An open sprint holds all of its `repositories`: only its observer may create a card in such a project, and
only with `--sprint` naming that sprint. Observer and dispatcher may move and edit linked cards, so the
ordinary claim, report and review cycle gains no extra step. The PO may create, move or edit only with an
explicit `--sprint-override` plus a non-empty `--sprint-override-reason-file`; the reason text is stored
as its own field in the durable audit. Without the flag the PO gets `sprint_write_forbidden`, as do retro,
steward and every other role. The refusal names the holding sprint and asks the caller to write through
its entity. The refusal itself is audited as `sprint_guard_denied` and is not duplicated when the same
request id is retried.

The index of open sprint repositories is kept locally next to the audit log. For a project outside any open
sprint it triggers no read of the sprints board. For a write into a held project the sprint is re-read
live: an unreachable board returns `sprint_guard_unavailable` rather than allowing the write. Closing or
stopping a sprint releases the hold.

Sprint mutations share the board event log and pending-audit recovery with card mutations. They carry the
sprint reference as `ref`, and a repeated `--request-id` returns the committed event without recording a
second one.

Every write command passes role guards and transition checks. A mutation first receives an append-only
pending audit event, is then checked against the live board, and only then counts as committed. An
unresolved pending write blocks a consistent export and the recovery checkpoint until `reconcile-audit`.

`report --kind done` checks `git status --porcelain` of the worker's workspace before writing anything and
refuses with `uncommitted` if there are uncommitted changes: the worker fixes that in its own session
instead of learning about it later from a blocked card. An untracked runtime tail is not counted as dirt,
`--kind blocked` is not gated because work in progress is legitimate there, and the dispatcher's after-the-
fact check stays as defence in depth.

The dispatcher also remembers the SHA that a mechanical gate or a red review rejected in the current
attempt. A `done` report on the same SHA does not move to Validate: the first such report sends the worker
back to rework in the same workspace, requiring a new commit. The second moves the card to Blocked so the
rework loop cannot spin forever. If the code deliberately does not change, for instance when the defect is
in a test or in the gate itself, the worker reports `--kind blocked` with the analysis instead of another
`done`.

The audit trail is always written to the installation's data directory: `--data-dir`, else
`SECRETARY_DATA_DIR`, else `data_dir` from instance config. A relative `data_dir` resolves against the
instance file, not the working directory, so a call from another project's workspace does not leave a data
directory there. If the data directory cannot be resolved, the command fails with a usage error rather than
writing next to the process.

### Routing telemetry per attempt

A card does not keep routing history: the resolved review head is cleared when it leaves Validate, and the
whole routing block is reset on a return to Ready. So "who was the worker and who was the reviewer on
attempt N" lives only in the append-only journal, as `kind: "routing"` events. The dispatcher writes them
without touching the backend: the event has no mutation, only a record written through the normal
pending/commit path, idempotent by request id.

An attempt (round) is one worker launch plus the review it earned. A claim opens attempt 1; each bounce
back to rework (a red verdict, a red gate) opens the next. Respawn, resume after a pause and a restart
after a rejected SHA stay inside their attempt. A return to Ready followed by a new claim adds an attempt
rather than overwriting the previous one: the number comes from the journal, not from dispatcher state, so
it survives both a lost record and a restore.

A return to Ready counts as such in both forms: an operator retry of an already blocked card, and an
ordinary preempt or requeue of a live card from in-progress or validate. The dispatcher issues the attempt
a new attempt id at that moment, otherwise a repeat claim would land on an already committed claim request
id, return the old event and leave the card in Ready. The previous attempt's heads are stopped, because the
new round enters the same workspace.

### Worker retention around the mechanical gate

After a worker reports `done`, the dispatcher suspends its live, addressable worker session before
moving the card to Validate. The record carries that retained state while the mechanical gate is
pending or running, so the worker cannot change the checkout during validation. A green gate
confirms the retained worker has stopped before an independent reviewer starts; the reviewer is
then the only head allowed to act on that checkout. The handoff stops that worker head and confirms
its heartbeat has exited; it does not stop every terminal in the worktree, so an existing connected
pane remains a split anchor for the reviewer.

A red gate first returns the card to In progress and updates `TASK.md` with the failure and the
next report identity. The dispatcher persists a pending-delivery boundary before SIGCONT, then
checkpoints confirmation only after the provider durably records the continuation user turn.
Terminal activity is a recovery hint for records without that boundary, not the delivery proof.
Recovery after a crash cannot mistake the previous `done` report for a new completion, replay an incomplete
delivery as if it were confirmed, or overwrite a confirmed continuation. A crash after SIGCONT but
before delivery replays the prompt, while a turn already underway is checkpointed and not sent
again. When the retained
provider session is still live and accepts delivery, the same terminal and session continue the
rework. Codex TUI and Claude interactive workers support this path; one-shot Codex exec workers
do not. The routing record and card comment name this as a reused continuation with the worker
profile, model, effort, reason and timestamp. A dead session, an unavailable continuation
transport, or a lost handle is an explicit fallback: the dispatcher confirms the old worker has
stopped, writes a durable launch intent, and starts exactly one replacement. Retention and stop
signal the head's private process group, so its helpers are frozen too. An unconfirmed stop never
permits a second writer in the workspace.

```json
{"kind": "routing", "ref": "PROJECT-N", "payload": {
  "attempt": 2, "attempt_id": "...", "phase": "verdict", "outcome": "red",
  "heads": [{"role": "worker", "head": "codex", "head_source": "card",
             "adapter": "codex", "model": "gpt-5.6-terra", "model_source": "profile",
             "effort": "default", "codex_mode": "exec",
             "resource": "openai-sub", "account": "openai-subscription"}]}}
```

`phase` is `worker` (worker launch), `review` (reviewer launch) or `verdict` (the attempt's outcome,
carrying both heads), so worker/reviewer pairs group by outcome without a join. A verdict `outcome` is
`green` or `red` from the reviewer; a mechanical-gate bounce closes the attempt with its own value
(`gate_red`, `merge-gate_red`, `review-freeze_red`) so a return to rework is not attributed to whoever
reviewed it. If the reviewer already returned green and the merge gate then bounced the card, both events
stay in the journal.

The actual head matches the requested one. The decision is made once, at claim time, from the card override
or from `role_defaults`, and there is no substitution at launch: the dispatcher has no health-based
switching and no fallback chains. So a record carries one head per role plus `head_source`, saying where
its id came from: `card`, `role_default`, or `record` (the head pinned in the card's dispatcher record when
it was claimed earlier).

Because the decision is made once, the attempt keeps it. A dispatcher that lost its record takes the head
pair from the card's own resolved worker and reviewer fields when adopting, rather than resolving the
override and `role_defaults` again: otherwise a role default changed mid-attempt would hand the review to a
different head and the journal would honestly record a head nobody claimed the attempt with. If the head
pinned at claim time has disappeared from the registry, nothing is launched: the card moves to Blocked with
the reason that the claimed head is unavailable, the dispatcher record is dropped, and nothing is appended
to the journal. Substituting the current `role_defaults` would be exactly the launch-time swap the
installation does not have, so the decision is left to a person.

A profile name is not a historical key: several profiles can be one model at different effort levels, a
profile may pin no model at all, and profiles get repinned. So each head carries its full launch
configuration, captured at launch and never re-read from the registry. The bring-up itself takes the
snapshot and the dispatcher writes it to the journal as is. The registry is re-read only for an adopted
card whose launch happened in a previous life of the dispatcher.

`model_source` says where the model came from, and `model` is empty only when the source says so
explicitly. A profile with no model launches its CLI without a model flag and the CLI picks one; at launch
the same sources are read in the same order the CLI uses. If the model is pinned nowhere, the value stays
empty under a `cli_default` source, meaning "chosen by the runtime" rather than a silent omission. The
launch record rejects an empty model under any other source.

Those sources are read from the environment the head will actually get, not the dispatcher's own. A head
command goes through the role-environment wrapper, which drops every `runtime.env` variable outside the
role allowlist, so the snapshot reads the role launch environment. Otherwise the journal would record a
model that never reached the CLI.

Every launch inside an attempt writes its own event: respawn after silence, restart after a pause, rework.
The request id includes a digest of the configuration, so relaunching the same head commits once, while a
launch on a different adapter, model, effort or resource adds a second event and replaces the attempt's
active head. The verdict always carries the head that earned it.

The reading side is `secretary.routing_journal.attempts(events, ref)`: the sequence of attempts for a
finished card, with heads and outcome. These events go into the recovery checkpoint with the rest of the
event log and are restored on materialise.

## Production dispatcher

The production runtime runs as a single tick or a continuous loop:

```bash
python3 -m secretary dispatcher production-tick --instance INSTANCE
python3 -m secretary dispatcher production-observe --instance INSTANCE
python3 -m secretary dispatcher production-run --instance INSTANCE
```

The systemd timer uses the one-shot `production-tick`. The runtime handles only supported task
transitions, persists claim and review state, and checks the live board before recovery. The production
owner is recorded in dispatcher state; an owner mismatch, a dirty workspace, a missing report or an
unresolved audit state stops a transition instead of falling back silently.

## Pause

The pause is shared across the pipeline and sits on top of the product dispatcher:

```bash
python3 -m secretary pause drain|freeze --instance INSTANCE --reason "why"
python3 -m secretary resume --instance INSTANCE
python3 -m secretary pause-status --instance INSTANCE
```

`drain` stops claiming new cards and dispatching background roles, but cards already running finish their
cycle. `freeze` additionally stops live worker and reviewer heads (a stop, never a teardown) and freezes
the tick entirely: nothing advances and no watchdog fires on a head that was stopped on purpose. `resume`
brings stopped heads back up in the same workspaces, hands a card whose report already arrived to the next
tick, and restarts the watchdog windows.

The flag is `<data_dir>/dispatcher/pause.json`, read by every `production-tick`. Background roles read a
mirror flag, written and cleared by the same command.

During a freeze an operator can exclude their own workspace with `--exclude-workspace`; the manual archive
command uses this to freeze the pipeline from inside a worker.

A freeze set by an automation on the configured allowlist expires after a configurable TTL (45 minutes by
default): the tick checks this before skipping on freeze and lifts the pause through the ordinary `resume`
under the same tick lock. A freeze set by a person holds until an explicit `resume`. A frozen tick moves no
cards but still writes and pushes the checkpoint.

## Connecting a project

The current low-level onboarding has these stages:

```bash
python3 -m secretary project add ...
python3 -m secretary project provision-start ...
python3 -m secretary project provision-apply ...
python3 -m secretary project gate ...
```

A project's identity is set once by the top-level binding: `id`, `repo`, `adapter`, `default_branch`. The
binding's mutable `plane` and `policy` fields are not part of identity and are carried over into the
rewritten binding by a repeat `project add`. The scanner and provisioning prepare changes but do not
enable a binding. Enabling is allowed only through a passing gate tied to verified revisions, a provision
run and a write set. A higher-level resumable workflow is a roadmap milestone.

Diagnosing failures, recovering a stale disabled draft and verifying a passed result are described in
[Operations](OPERATIONS.md#connecting-a-project-gate-and-stale-input-recovery).

## Memory

Facts are stored flat as `memory/facts/global/<slug>.md` or `memory/facts/<project-dir>/<slug>.md`. One
fact is one distilled markdown record. The curator is the writer role; every other agent reads through
`memory_search`, `memory_get` and `memory_list`.

```bash
python3 -m secretary memory verify --instance INSTANCE
python3 -m secretary memory propose --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md
python3 -m secretary memory commit --instance INSTANCE --actor ACTOR --propose-id ID
python3 -m secretary memory supersede --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md --supersedes OLD-ID
python3 -m secretary memory reindex --instance INSTANCE
```

Writer operations require an actor and go through the journal protocol; direct edits bypass the audit
trail. `reindex` changes only the derived index and must not overlap another index writer. Model and
dimension come from instance configuration.

## Knowledge

Long recoverable documents (brainstorms, decision logs, incident write-ups) live in
`state/knowledge/<section>/<document>.md` for the installation itself, and in
`state/knowledge/projects/<project id>/<section>/<document>.md` for a connected project. How this
differs from curated memory and the board is described in
[Architecture](ARCHITECTURE.md#knowledge-planes).

```bash
python3 -m secretary knowledge write --instance INSTANCE --actor ACTOR \
  --path decisions/2026-07-25-sprint-1.md --file DOC.md
python3 -m secretary knowledge write --instance INSTANCE --actor ACTOR \
  --path projects/codegen-orchestrator/brainstorms/qa-node.md --file DOC.md
python3 -m secretary knowledge list --instance INSTANCE
```

Path segments are ASCII: letters, digits, `.`, `_` and `-`. A document imported from elsewhere under
a non-ASCII filename is renamed on the way in.

`write` replaces a document wholesale and commits only `state/knowledge` under the shared writer lock, so
no manual `git commit` is needed and it does not race the tick writer. A document containing a secret is
rejected with code 2 and nothing reaches disk. Rewriting identical content reports `changed: false` and
makes no commit.

## Secrets

```bash
python3 -m secretary secret init --instance INSTANCE
python3 -m secretary secret set --instance INSTANCE --id ID --scope SCOPE --purpose PURPOSE \
  --stdin [--environment VAR] [--materialize runtime-env|file [--materialize-path PATH]]
python3 -m secretary secret list --instance INSTANCE
python3 -m secretary secret import --instance INSTANCE --file ENV_FILE --scope SCOPE \
  --purpose PURPOSE [--materialize runtime-env|file [--materialize-path PATH]]
python3 -m secretary secret remove --instance INSTANCE --id ID
python3 -m secretary secret materialize --instance INSTANCE [--target runtime-env|file]
```

A secret value never travels through argv: `set` reads it from stdin or `--file`, and `import` takes a
`KEY=VALUE` env file (LF-separated, no comments or blank lines, one secret per variable). No command prints
a value: `list` returns catalog metadata only, and `import` and `materialize` print ids and variable names.
Reading a value stays an internal API until there is a safe consumer for it.

`secret init` is interactive by design. It refuses to run when stdin or stderr is not a terminal, and makes
that check before generating the recovery phrase rather than only before printing it, so the phrase cannot
reach a pipe, a file or a log. The phrase is printed once to stderr, the operator confirms they wrote it
down, screen and scrollback are cleared, and only then does `init` ask for a few words of the phrase back
before creating the store.

Layout of `secrets/` in the instance repository:

```text
secrets/
  catalog.yaml            open metadata: id, scope, purpose, materialize — tracked in Git
  installation-key.json   open KDF parameters and verifier for the installation key — tracked in Git
  values/<id>.enc.json    one encrypted envelope per secret — tracked in Git
  installation.key        the raw installation key, 0600, outside Git (.gitignore)
```

The store is the fourth writer of the instance repository, next to board/runs, memory and knowledge:
`init`, `set`, `import` and `remove` take the same repository lock and commit their own pathspec in a
single commit, so the catalog and the values it names cannot diverge in history. `list` takes no lock and
commits nothing. `materialize` takes the lock too, so it does not cross a writer mid-read, but it writes
only the materialised files outside `secrets/` and makes no commit: materialisation targets are not part of
the instance repository. The open part passes the same redaction gate as `state/`: a secret accidentally
pasted into a `purpose` field stops the write instead of reaching a commit. The encrypted envelope does not
go through that scan, because its body is ciphertext plus open decryption parameters.

Recovery is described in [Recovery](RECOVERY.md#secrets). With the recovery phrase the installation key is
rebuilt and materialisation targets are rewritten from the catalog. Without the phrase everything
non-secret is restored, and `recover` prints a `locked`/`missing` report and writes nothing: `locked` means
the value is encrypted but the key is absent, `missing` means the catalog names a secret whose envelope is
not in the repository.

The installation key belongs to the installation user, the same user that owns the host and the
installation, not to a narrower role. The store does not promise worker isolation: it has no broker and no
grants, and the installation key opens every secret at once, with the same rights that previously read
`runtime.env`.

Data-plane, archive-restore and unit runbooks are in [Operations](OPERATIONS.md).
