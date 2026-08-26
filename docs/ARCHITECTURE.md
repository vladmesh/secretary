# Architecture

`secretary` is a substrate for several interchangeable agent heads. It owns the task and memory
protocols, the dispatcher lifecycle, installation contracts and recovery. The providers' native CLIs
do the actual work on top of that.

## Source layout and module boundaries

Product source uses a `src/` layout. The repository root is the product and deployment boundary;
`src/secretary` is the primary Python package. Keeping importable code below `src/` prevents a test or
operator command from accidentally importing an uninstalled checkout merely because its current
directory is the repository root. Packaging, scripts, documentation, examples and tests remain at the
repository root because they are product assets rather than importable runtime code.

`src/triggered_agents` is a temporary historical namespace, not a second product boundary. It was
absorbed from a separate repository and now provides runtime primitives used directly by Secretary.
The dependency evidence makes the intended ownership unambiguous: the Secretary package is already a
large consumer of that runtime, while only production telemetry crosses back into Secretary. New
shared runtime code therefore belongs to `secretary`, and no new dependency from `secretary` to
`triggered_agents` should be introduced. A later migration will move those primitives under the main
namespace while preserving the installed `triggered-agents` command as a compatibility surface.

The target package layout is feature-first:

```text
src/secretary/
  cli/                 command parsing and rendering
  board/               board protocol, models and adapters
  tasks/               task lifecycle
  sprints/             sprint lifecycle and observation
  dispatch/            production dispatcher orchestration
  projects/            registered projects: their bindings and adapter-owned contracts
  runtime/             heads, sessions and prompt delivery
  automations/         curator, retro and steward services
  memory/              facts, journal, index and MCP service
  installation/        bootstrap, upgrade and host reconciliation
  backup/              checkpoint, backup and restore
  secrets/             secret storage and recovery
  infra/               filesystem, process, environment and path helpers
  schemas/             packaged data contracts
```

This tree is a migration destination, not a claim that the current flat package already conforms to
it. Moves happen one feature at a time with compatibility imports where an installed command or
integration depends on an old module path. In particular, the large dispatcher and task modules must
be decomposed by lifecycle responsibility rather than split by file size alone.

Dependencies point inward from entry points to feature APIs: CLI and automation modules may call
feature services; orchestration may call task, sprint and runtime APIs; adapters implement protocols
owned by the feature that consumes them. Feature code must not import CLI modules, and shared runtime
must not import an application module merely to discover configuration. Configuration or paths needed
by runtime are passed in or exposed through a small shared interface. These rules remove the current
telemetry cycle and keep `infra` from becoming a second application layer.

## Storage boundary

```text
product repository     CLI, runtime, schemas, tests, generic skills
instance repository    one private repository per installation: config + portable checkpoint
data directory         local mutable and derived runtime data plane
```

The product repository holds no real project bindings, credentials, cards or host-local state. The
instance repository holds persona, project bindings, adapters, policies and head profiles. Its
`secrets/` directory keeps a metadata catalog and sealed values in Git next to board and memory; the
only things it never contains are the raw installation key and the recovery phrase, see
[Recovery](RECOVERY.md#secrets). The host `runtime.env` is a separate `0600` file, gitignored and
outside any checkpoint or archive payload; its values may be registered in the store, in which case
the file becomes a materialised copy.

Product configuration reaches an installation one way: `secretary upgrade` generates
`heads/heads.yaml` from the installation's head canon — its own `heads/heads.toml` when it has one,
else the product's small portable default — and writes `heads/source.yaml` next to it, recording
that canon, which side owns it, the checkout and revision it was taken from, and a digest of the
snapshot. Upgrade commits and pushes the two generated files as one recovery-canon update. A live
tick reads and validates that installed pair without consulting a product checkout, so editing the
canon in a product working tree neither changes installation behaviour nor stops it. The cost is
that a change reaches the installation only through `upgrade`, and the gap is visible in
`secretary status` under `installation.head_registry`.

No path in the product names a user or a checkout. An installation is named by `--instance` or
`SECRETARY_INSTANCE`, the checkout to install by `--product-root`, and anything neither of them
covers hangs off a home: the shipped skill target roots, the shell entry points, the role
worktrees, the runtime env file. Which home is the one question a materializer has to answer before
it writes, and the answer is the account that owns the installation — the owner of the instance
directory, or `--runtime-user`. That is the same account the units are rendered for, so a repair
run as root materializes the paths those units name rather than filling `/root`. The full order is
in [Operations](OPERATIONS.md#path-precedence). So a checkout is installable by any account, and one
that has been pointed at a particular checkout or installation stays pointed there.

`state/` in the instance repository holds the normalised recovery canon: board, runs, memory facts
and knowledge documents. The data directory holds task audit, dispatcher state, derived
exports and indexes, search logs, raw dumps, transcripts and artifacts. The SQLite and vector index,
worktrees, terminals and generated host resources are derived and stay out of the checkpoint.

The Git-backed checkpoint is described in [Recovery](RECOVERY.md). A manual cold archive remains an
optional tool for raw material and compatibility, and takes no part in recovery readiness.

## Runtime flow

```text
operator / automation
          │
          ▼
  secretary task and sprint protocol ────────> board backend
          │                            ▲
          ▼                            │
 production dispatcher ──> head adapter ──> session ──> native agent CLI
          │
          └──── run/audit state ──────┘

agent heads ── memory_search ──> MCP/index <── facts journal <── curator
```

Kanboard is the current live store for cards and sprints. Cards live on the `Pipeline` board; each
sprint is a separate task on a `Secretary sprints` board. Both sets go into and come back from the
checkpoint as separate sets (see [Recovery](RECOVERY.md#what-the-checkpoint-contains)), so the live
store stays operational rather than being the only place a sprint contract exists. Every supported
write goes through `secretary task` or `secretary sprint`, which apply role guards, transitions and
append-only audit. The dispatcher resolves routing, drives the worker and reviewer lifecycle, and
checks board, workspace, report and review state before each transition.

A substantive reviewer verdict is not itself an effect. It parks the card in Assessment with the
reviewer stopped and the worker of the round held, and the merge or the next round runs only on the
tick that performs a decision the observer recorded (see [Tasks](PROTOCOLS.md#tasks)). Mechanical
outcomes keep resolving in Validate, so the decision point sits where direction is decided and
nowhere else.

Sprint budget is not the observer's self-report. The dispatcher reads the durable audit of the cards
linked to a sprint and writes one budget event per source-event identity. Thresholds come from
instance configuration, with defaults substituted before validation; the hard limit moves the entity
to `stopped` and emits a separate durable `budget_hard_stopped` event carrying the reason. Observer
reconciliation reads only open sprints, so such a stop takes down the live head and prevents a new
one without touching cards that are already claimed. The resume entry lives in sprint metadata as a
structured record, and its freshness is derived by comparing it against card audit, not against
terminal history. `secretary status --json` reads the same entities and the live board.

Orca is the current session manager and live terminal UI. One card occupies one worktree: the worker
gets its own terminal, the reviewer starts as a separate split pane in the same worktree, and both
handles are kept apart in dispatcher state. A split rather than a second terminal, because on a
headless server a freshly created terminal arrives as a background surface and does not materialise
in a worktree the client already has open. Orca's `terminal_split_source_not_found` can arrive before
or after its child attempt, so the dispatcher snapshots the pane inventory before the split and reads
it again after that refusal. Only an unchanged inventory permits a standalone reviewer terminal in
the same worktree; a newly visible pane leaves the launch fail-closed, avoiding a duplicate reviewer.
The tick records the successful standalone route with `reviewer_fallback_reason`. Any other split
failure remains fail-closed, because it may have already started a child and opening another pane
would duplicate it. When review starts, the worker's head is stopped and its
commit is recorded: the merge gate will not accept a green verdict if the checkout moved since.
Where that worktree lives is Orca's answer, not the dispatcher's: workspaces are namespaced by the
session manager's repo registration, which is the binding's `orca_binding`, and the Secretary project
id has no say in the path. A returned worktree is accepted only when the session manager's own record
says it belongs to this project's registered repository and carries this card's workspace name; one
that does not is removed again before the bring-up fails, so a rejection leaves nothing registered.
Launch and cleanup still depend on Orca's specific API; a target session protocol is a roadmap
milestone. Head-specific rendering and delivery are confined to adapters, but that contract is not
yet a stable plugin API.

Before launching a head its CLI's first-run questions are answered on its behalf. Otherwise an
interactive head sits in a dialog instead of working: it never goes idle, the prompt is never
delivered, and bring-up ends up deferred. Only the missing entries are appended, foreign ones are
left alone, and a path held at a different trust level stops bring-up with a visible reason rather
than being silently overwritten.

This is one preflight, not one per launcher. Every interactive Codex head follows the same order —
ensure the workspace is trusted, create the pane, wait for readiness, deliver the prompt, confirm
the turn — whether the dispatcher launches it for a worker, reviewer or observer, or a background
tick launches it for a service agent. A launch command's own directory-trust flags state the intent
but do not answer the question on their own, so the preflight runs before the pane exists; a
workspace that cannot be prepared fails there, with nothing started and nothing recorded as having
run. An operator-launched `secretary shell` session is excluded on purpose: a human is sitting in
front of that terminal and can answer the dialog themselves.

## The sprint observer head

An open sprint gets its own observer head. It is neither an interactive session nor a worker: an
observer never claims cards, never appears in card records and never occupies the per-project claim
gate. The sprint's cards are claimed and executed by the dispatcher independently of it.

The same production tick that drives cards runs a separate reconciliation pass against the sprints
board:

- open sprint with no live head: launch one;
- open sprint with a live head: do nothing, exactly one head per sprint;
- open sprint whose head's own launch identity says the process is dead: launch a replacement,
  whether or not a card event is waiting for it. A quiet queue is a reason not to wake a head that
  is there, never a reason to leave the sprint without one — the fence over that sprint's cards
  reads the same heartbeat and holds until a head is adopted, so leaving a dead one alone stopped
  the sprint until an operator commented on one of its cards. Only positive death counts: a launch
  identity that is missing, half-written or unreadable answers nothing, and a bring-up on it would
  open a second head beside a live one. A replacement that is itself found dead is bounded by the
  same persisted backoff a failed launch uses, so a head that dies at every bring-up costs one
  attempt per backoff window rather than one per tick, and a bring-up the host refused is retried
  on that same backoff: the quiet-queue answer never overwrites a record that still owes a launch;
- closed or vanished sprint: stop the head and drop the record;
- unreachable sprints board: touch nothing, an unanswered board is not a closed sprint.

A stop the host rejected does not count as a stop. The record keeps its handle in a `stop-pending`
state, no stop event is written, and the tick retries until the terminal is really closed. Dropping
the record earlier would leave a live head that nothing points at. For the same reason a relaunch
after a dead pid is deferred if the old terminal could not be closed: two heads on one sprint is
worse than none.

The head profile is the sprint's own: `sprint_observer` holds one concrete profile or `none`, and the
launch resolves that profile against the installation's `heads/heads.yaml` snapshot without any fallback.
A sprint declaring a profile the snapshot does not have is corrupt and fences its own projects rather than
launching on a default. `role_defaults.observer` never decides what a sprint runs; it only labels an
observer record that is filled in with no sprint to read.
The profile is interactive, because an observer is one continuous session for the whole
sprint: a one-shot launch would exit after the first turn and the tick would read that as a dead
head. The same resource-readiness gate that runs before claiming a card runs before launch; an
unavailable resource means a deferred launch, and the next tick tries again.

The head is launched through the ordinary head-command renderer and the role environment wrapper, so
an observer gets exactly the environment a dispatcher-launched role is entitled to (board credentials
and the instance and product-root pointers), not the whole `runtime.env`. It has its own workspace
and its own terminal handle.

That workspace is registered with the session manager like a worker's: a terminal is only handed out
for a registered workspace selector, and a directory the session manager does not know about is not a
selector. The observer gets no project checkout: its workspace is cut from a separate empty
repository without a remote, which the dispatcher creates on first use. That repository is a managed
resource of the installation, derived from the data directory when the production dispatcher
component is enabled. Reconciliation neither creates nor deletes this lazy resource, and `doctor`
accepts its registration only at that path; before the first observer, its absence is normal.

Stopping goes through the workspace: stopping terminals by worktree kills all of the observer's panes
at once, after which the worktree registration is removed. A closed sprint leaves neither terminal nor
workspace behind. The launch prompt is rendered from the live sprint entity at launch time: reference,
goal, Definition of Done text, repositories, status, current card, budget state and the path to the
role skill.

### The observer role and its skill

What an observer does while running a sprint is defined by the `observer` role in the role-skill
registry (`skills/manifest.toml`) and its `observe-sprint` skill. The canon lives in this repository
and is delivered to shells by the same `secretary role-skills sync` as every other role skill. The skill
must be delivered to the shell of whichever profile a sprint declares, so a sprint opened on a profile from
another shell also needs a delivery target for that shell. `role_defaults` decides delivery for the other
roles, not the observer's profile.

The skill is self-sufficient for a fresh head with no transcript: recovering state from the sprint
entity and the live board, checking the Definition of Done against current `main` before each new
card, how to pick the next step, doing its own research instead of filing a research card, exactly
one fresh card at a time, a reviewer from a different family than the worker, watching a card to a
terminal state, reading reports and verdicts, the classes of Blocked, the narrow conditions for a
hotfix, behaviour at budget thresholds, the limits of the role, and a closing checklist. Writing a
resume entry after every significant transition is a requirement of the skill.

The launch prompt references that file by path and does not repeat its instructions: two texts about
one job drift apart, and the head follows the stale one.

A head is never launched blind. Before preparing an observer, the tick checks that the role skill is
present in the shell of the head being launched. If it is missing, the registry is unreadable, or that
shell has no `observer` target at all, the launch is deferred with a reason naming the missing file.
The reason lands in the observer record and is visible in the external summary. A head without
instructions would improvise a sprint from a single entity, which is worse than a sprint waiting for
the next tick.

Liveness uses the same pid heartbeat as worker and reviewer. A live head can still have finished its
turn, so the dispatcher also asks Orca whether the pane is ready for input and reads its terminal
output time. Readiness is `tui-idle`, the same signal the delivery path waits on before it sends to
any head, so the answer does not depend on which provider the observer profile names. It has three
answers, not two: ready, busy (a pane that is working, or the condition unmet before the probe's
deadline), blocked (a pane held in a dialog, which is not ready and is not working on a prompt
either) and unanswerable. Orca leaves the command with a non-zero status for a busy pane as well as
for a failure, so the answer is read from the body it printed rather than from the outcome. A pane
nothing can be sent to at all, because the record has no handle, because Orca no longer lists that
terminal or because it is disconnected, is not a busy head either: it enters the same bounded
failure path. A probe that cannot be answered is not a busy head; it enters the
same bounded failure path as a refused delivery, because a head nobody can ask about is not one the
sprint can wait on. A ready pane exposes `idle-grace` in the observer record, but does not relaunch
it by itself. A committed,
successful non-routing, non-guard-denied event on a linked card opens one durable delivery batch. Its immutable high-water mark is
written before a nudge or replacement launch, and only an observer resume carrying that delivery's exact audit
marker can acknowledge the specific batch, including one whose delivery was refused: that marker
only exists in a prompt that reached the head, so its resume ends the batch rather than earning a
second turn. Events
that arrive before the intent coalesce; later events wait for the next batch. A dead head is replaced only for
pending work. An unacknowledged batch is sent again as soon as its head is seen ready for input, and
otherwise once its acknowledgement deadline (30 minutes by default) expires; a head that is never ready for
input at all is bounded instead by the far longer turn ceiling, after which the delivery fails. Failed
wakes carry their reason and bounded retry time in the observer record, and a batch that has spent
those retries is delivered by replacing the head rather than by retrying it again.

An active card does not itself create another observer turn. Once the pane is ready for input again,
the head is `idle-grace` even if its linked card remains active; the next significant durable card
event wakes it. If that event arrived while the head was still working, the dispatcher keeps the
pending event and checks again on later ticks, delivering the nudge as soon as the pane is ready.

The wake itself goes through the one delivery path every interactive head has, whatever provider it
runs and whichever role owns it: a Codex launch, a worker or reviewer continuation and an observer
wake are the same primitive. Delivery there is four observed stages, not one: the payload written
into the pane, the Enter taken, a turn observed, and the caller's own acknowledgement. `terminal
send` answering `accepted: true` with a byte count is only the first, and Orca calls a pane holding
an unsent composer `tui-idle` because it really is idle — which is how a prompt that was pasted and
never entered reported as delivered (`issue:13dd4d88df6b33cfb98f`). So the pane is fingerprinted on
both sides of the send: what the composer holds, and where its output has got to. That position is
Orca's own `nextCursor`, kept opaque and only compared: the retained tail is a bounded window, so a
quick turn can append output the tail no longer shows and a repainting TUI can print without the
returned lines differing at all. Only a runtime that answers a read without a cursor falls back to
a digest of the tail, and the evidence says which of the two it was. A composer that is holding what
the send put there is re-entered; one that is empty with nothing printed is written again; a pane
that went to work, or whose cursor advanced, has taken a turn — including one that began and ended
between two readiness probes, which readiness alone reports as a pane that stayed ready. Retries
stay bounded and a delivery that runs out of them refuses upwards with the stage it reached. So
does the transport itself: a `terminal wait` or `terminal send` the host refuses is a delivery that
did not happen, and it leaves the boundary as an evidence-carrying delivery failure rather than as
a bare host error a caller could only persist as prose. A worker or reviewer continuation is delivered once
its own head's turn has visibly started, which is that role's long-standing criterion, and an
observer wake stops one stage earlier for the same reasons rather than under a weaker rule.
Observer delivery
acceptance is deliberately separate from its causal acknowledgement: the terminal path proves that
the prompt was taken, then returns without polling the audit log; the next normal reconciliation
reads one audit snapshot and closes the batch only if it contains a resume naming that delivery.
A wake the pane never took reaches the tick outcome and the
delivery record as an explicit failure with its reason. That failure is retried on the live head a
bounded number of times (`SECRETARY_OBSERVER_WAKE_MAX_ATTEMPTS`, 3 by default) on the existing
backoff; once they are spent, the batch goes to the ordinary replacement path, which stops that head
before it opens the next one and carries the same delivery marker into the new launch, so the resume
that finally arrives still acknowledges the batch that was owed.

What was delivered, and what was not, is sprint evidence rather than delivery state. The batch's own
retry counter is reset by an acknowledgement, by a replacement head and by the next batch, and has
to be. Beside it the observer record keeps cumulative counts that nothing resets while the sprint is
open: wake attempts and failures, launch-delivery attempts and failures, the last failure's reason
and subject, and the last attempt's bounded evidence — terminal identity, payload size and hash,
the stage reached, the composer and output fingerprints, and why it stopped. Never the prompt text.
The counts are wake-scoped and launch-scoped on purpose: a reviewer that failed to come up on a card
is a different subject with its own counters on that card's record, and "the reviewer launched
normally" was the answer that hid three refused observer wakes from a sprint's closing resume
(`issue:83ac17afc53248340f4c`). Every prompt a bring-up puts in front of a head counts as a launch
delivery, including the first launch of a sprint, which carries no batch at all — a launch with
nothing owed writes no retry state, because there is nothing to redeliver, but it is still counted
and its evidence still kept. They reach the head that has to report them through the wake message
and through a replacement head's launch document, and they are readable from outside in
`sprint status` (`observer.delivery`) and in `secretary status` (`delivery_failures`).

What the input channel carries is bounded: a task is a document on disk and the pane receives one
short line naming its absolute path. Both roles are nudged that way and both bring-ups are told the
document they nudged at: the worker at the `TASK.md` in its checkout, the reviewer at a review kept
out of the checkout. The reviewer was the first for which the rule was enforced rather than merely
followed; the worker followed it in the prompt it sent and not in the record it kept, which is how a
bring-up came to answer an unconfirmed pointer by closing the pane behind it. Its review — description, dispatcher-owned exact-SHA gate receipt, verdict
commands, some 12 KiB of it — is written under the run artifacts, private, because a workspace's
identity is the tracked diff and untracked files a receipt hashes to say which code it is evidence
for. The nudge is derived from that path alone, so nothing a card description carries (an ESC, a
bracketed-paste terminator, the CRLF the board's web form submits) can reach a terminal write, and
there is no payload left for a composer to swallow: the 24 consecutive `payload-left-in-composer`
failures that stopped two products on `codegen-orchestrator-1165` were all large pastes, and short
lines have never failed. A retry re-renders the same document and sends the same pointer, so the
head always opens the round's current task. The delivery record says which mode it was and which
document it named, and never what the document says.

The pane's fate is not decided by that delivery's classification, whichever role's head is in it. A
nudge the boundary could not confirm is ambiguous by construction — the head may well have taken it
— so the bring-up hands the pane back as an abort with its evidence rather than closing it, the
launch intent stays on disk, and the next tick either adopts that head or stops it through its own
retained identity with the cleanup recorded as the initiator. Closing a pane on a classification is
what killed reviewers that had their task in hand, and then six consecutive Claude workers on
`codegen-orchestrator-1166` that had taken their prompt and started work.

What the classification is made of is the head's own provider record, not its screen. Claude and
Codex both persist a user turn locally, and a turn recorded after the send boundary is the proof;
the pane's status line is a secondary hint that may confirm and may never condemn. That asymmetry is
not a preference. Both readings are claims about another product's format, and both had gone stale
at once by 2026-08-11: `~/.claude/projects` names a project directory after the workspace path with
every non-alphanumeric character replaced, so a `codegen_orchestrator` workspace was being looked
for under a directory Claude Code has never written, while the status-line pattern still expected a
spinner glyph and a `(4s · ↑ 13.2k tokens)` suffix that the current version does not paint. Neither
could fire, so every delivery was unproven — and the bring-up that trusted that verdict killed the
heads. The rules are held against the real catalogue and the real pane text in
`tests/test_dispatcher_tui.py`, because a unit test that builds its fixture with the same rule it is
asserting cannot detect the drift that matters.

The reviewer keeps the same evidence on its own record. A reviewer prompt that the shared boundary
saw fail leaves `review_delivery_failures` and `review_delivery_evidence` on the card, because the
pane cannot be asked afterwards for what it did with the prompt. Every reviewer
bring-up failure goes through one recorder in `start_review`, before any branch takes a transition,
writes a launch intent, decides a retry or returns an outcome — including the ambiguous abort, where
the pane is still open and the intent has to be kept: the evidence is written first, and refusing to
open a second reviewer still outranks the ordinary infrastructure retry. One point means no branch
can forget and none can count twice. A failure carrying no evidence records none: a split that would
not open is an infrastructure failure, and it must not be tallied as a prompt that was refused.
It changes no routing:
the card still takes the infrastructure-retry transition it took before, with its green gate
receipt, candidate SHA, report round and held worker untouched, and unlike the infrastructure
counter beside it the delivery evidence is not cleared by a reviewer that later takes the checkout.

All lifecycle events go to the same durable audit log keyed by the sprint reference and are deduplicated
by request id. The record's generation is part of that id, so a sprint reappearing on the board starts a
fresh cycle of events instead of being deduplicated against the previous one.

Ordering matches card writes: the event is staged first, then the host is called, and only then is
the event committed. Storage that fails at staging cancels the action itself, because an unrecordable
action is worse than a deferred one. Storage that fails at commit is not surfaced: the effect already
happened, the record is saved anyway, the event stays pending and is repaired by
`secretary task reconcile-audit`, and the tick outcome reports a pending audit. Otherwise a live
terminal would have no record and the next tick would launch a second head onto the same sprint.

The record itself is persisted the same way and for the same reason. The launch intent — sprint,
record generation, head profile, attempt number, workspace and the future head's pid file — is
written to production state and flushed to disk before the host call, not at the end of the tick.
Unwritable state means the head is not launched at all: a data-plane failure costs the sprint a tick,
not a second head. A tick that dies between the host call and its own end leaves the intent on disk,
and the next tick resolves it from the pid file: a live pid is adopted as this sprint's head, a pid
file that has not appeared yet within the usual startup window is not treated as death, and anything
else closes the workspace's terminals and launches again.

Pause behaviour: a freeze stops observer heads along with everything else and records the reason;
resume clears the mark and the head returns on the next tick through the same reconciliation. A
drain leaves a live head alone and launches no new ones, but a sprint opened during a drain still
gets a deferred record with its head profile and reason, because an open sprint must be visible from
outside.

## Memory plane

Facts are markdown records under `state/memory/facts` in the instance repository. The curator is the
writer role and writes through `secretary memory propose/commit/supersede`; the protocol commits only
`state/memory` under the shared instance-repository writer lock. The butler is a proposer, not a writer:
it may stage a fact for the curator inbox with `secretary memory propose`, and canonical `commit` and
`supersede` stay with the curator, secretary and operator roles
([Protocols](PROTOCOLS.md#memory)). Other heads read through MCP. The
NDJSON export and the SQLite/vector index in the data directory are rebuilt from the canon. Only one
index writer may publish derived state at a time.

The embedding model is loaded locally and is the appliance's main memory consumer; see
[Operations](OPERATIONS.md#system-requirements) for what has and has not been established about the
supported minimum.

## Knowledge planes

Knowledge is split across three planes, and "where does this go" is decided by the length and purpose
of the record. The Pipeline board holds executable work: cards, specs, states. Curated memory
(`state/memory/facts`) holds the short current conclusion a head should receive in context through
`memory_search`. Knowledge (`state/knowledge`) holds the long reasoning and context the conclusion
came from: brainstorms, decision logs, incident write-ups.

A document is scoped by who it belongs to. Sections directly under `state/knowledge` hold the
installation's own reasoning: its decisions, its incidents, its runbooks. A connected project keeps
its documents under `state/knowledge/projects/<project id>/<section>/`, where the project id is the
one in `projects/`. The split matters because a connected repository is not the place for the
reasoning behind its own development: a product repository carries contracts and code, while the
history of why the work was scoped that way is recoverable state of the installation that drove it.

Knowledge is not indexed, does not appear in `memory_search` and is never loaded into a head's context
wholesale. A document is read on purpose when the history of a question is needed. The format is free:
ordinary tracked markdown, no frontmatter or metadata required. Writes go through
`secretary knowledge write`; that writer owns only `state/knowledge`, takes the shared
instance-repository writer lock and scans the document for secrets, so no manual `git commit` is
needed and it does not race the tick writer.

## Ownership and security

- The current security profile assumes one trusted owner of the host. Agents are not isolated as
  untrusted tenants.
- `doctor` reads config, data and host inventory, but never changes the host.
- `reconcile plan` computes desired state. A matching name or prefix confers no ownership without a
  managed manifest or a product-written marker.
- The lazy session-manager registration of the observer root belongs to installation config but not to
  reconciliation: the dispatcher creates it on the first observer. A matching display name at a
  different path does not become ownership and stays an unmanaged-on-host finding.
- Secrets registered in the store reach instance Git as encrypted envelopes and travel with the
  checkpoint; the raw installation key and the recovery phrase do not (see
  [Recovery](RECOVERY.md#secrets)). The host `runtime.env` is outside the store, and facts, exports and
  diagnostics carry no secrets.
- Task audit and pending writes are fail-closed: an unfinished board mutation blocks a consistent
  export and the recovery checkpoint.

Command contracts are in [Protocols](PROTOCOLS.md), runbooks in [Operations](OPERATIONS.md), and the
product goal in [Vision](VISION.md).
