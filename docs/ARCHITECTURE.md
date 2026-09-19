# Architecture

`secretary` is a substrate for several interchangeable agent heads. It owns the task and memory
protocols, the dispatcher lifecycle, installation contracts and recovery. The actual work is done by
the providers' native CLIs.

Command and web contracts are in [Protocols](PROTOCOLS.md), runbooks in [Operations](OPERATIONS.md),
the PostgreSQL schema in [Board store](BOARD_STORE.md), checkpoint and restore in
[Recovery](RECOVERY.md), and the product goal in [Vision](VISION.md).

## Source layout and module boundaries

Product source uses a `src/` layout, so a test or command run from the repository root cannot import
an uninstalled checkout by accident. Packaging, scripts, docs, examples and tests stay at the root.

- `src/secretary` is the product package. Its flat root is closed: `tests/test_architecture.py`
  holds the list of existing flat modules, and a new module must go into a feature package. Current
  packages: `board`, `cutover`, `dispatch`, `infra`, `memory`, `po`, `projects`, `schemas`, `web`,
  `webfront`, `webproto`.
- `src/triggered_agents` is a legacy namespace. It holds runtime primitives Secretary uses directly
  (head runtimes, session-manager and delivery helpers, the mechanical-role driver, the curator,
  steward and retro agents). New shared runtime code goes into `secretary`. The only allowed imports
  from `triggered_agents` into `secretary` are the ones listed in the test: production telemetry and
  curator discovery reading `secretary.config`, and `secretary.sprints`.

The target package layout is feature-first. Modules move there one feature at a time, keeping
compatibility imports where an installed command depends on an old path:

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

The dispatcher is being split by lifecycle ownership. `dispatch.worker_continuation` owns
retained/red continuation: durable rework intent, replayable board move, delivery and its bounded
provider-progress recovery, and the confirmed-stop handoff to `dispatch.worker_launch`. Durable
models stay in `dispatch.worker_lifecycle`. In `_advance_worker`, red-transition replay runs before
report lookup; delivery recovery runs after `worker_report_marker` but before `handle_worker_report`.
This ordering lets a report prove a prior delivery without resending its prompt. Gate/review decisions
and Assessment parking stay outside this boundary. There are no implementation callbacks into the
dispatcher for the extracted continuation methods.

`dispatch.wait_vitality` owns the shared worker/reviewer wait state machine: vitality reduction,
recovery-policy rungs, suspension SIGCONT/operator escalation, guarded one-shot respawn, second-stall
blocking and the bounded unobservable-head escalation. `DispatcherRuntime` calls its four package
entry points from worker/review/gate orchestration; the module calls back only for the existing
worker/reviewer confirmed-stop lifecycle boundaries and routing/terminal effects. Mechanical gate
verdict, transport retry, bounded infrastructure rerun and pending-CI policy are package-owned by
`secretary.dispatch.gate_lifecycle`. `dispatch.review_verdict` owns durable review-verdict
acceptance and Assessment parking: marker consumption, reviewer-stop handoff, green/red bookkeeping,
the no-observer red ceiling, pre-park merge readiness, and park intent/replay. `dispatch.assessment_decision`
owns Assessment decision intake/replay plus rework/reslice execution, delegating only release back to
the separate release/merge state machine. Reviewer launch/wait, release/merge effects and
completion-evidence policy remain in `DispatcherRuntime` for later bounded extraction.

Dependency rules:

- Dependencies point inward, from entry points to feature APIs. CLI and automation modules call
  feature services. Orchestration calls task, sprint and runtime APIs. An adapter implements a
  protocol owned by the feature that consumes it.
- Feature code does not import CLI modules. Shared runtime does not import an application module to
  find configuration; configuration and paths are passed in.
- A board client is built only through `secretary.board.backend.board_client`, and a card audit only
  through `secretary.tasks.task_audit_for`. The few modules allowed to build `KanboardClient`,
  `TaskAudit` or `EventJournal` directly are listed, with reasons, in `tests/test_architecture.py`.

## Storage boundary

```text
product repository     CLI, runtime, schemas, tests, generic skills
instance repository    one private repository per installation: config + portable checkpoint
data directory         local mutable and derived runtime data plane
board store            live cards, sprints, products, issues and their audit
```

The product repository holds no real project bindings, credentials, cards or host-local state. No
product path names a user or a checkout.

The instance repository holds persona, project bindings, adapters, policies and head profiles.
`state/` in it holds the recovery canon: board, runs, memory facts and knowledge documents.
`secrets/` holds a metadata catalog and sealed values. The raw installation key and the recovery
phrase are never stored there ([Recovery](RECOVERY.md#secrets)). The host `runtime.env` is a separate
`0600` file, gitignored and outside every checkpoint and archive; a value registered in the store
makes the file a materialised copy.

The data directory holds task audit files, dispatcher state, derived exports and indexes, search
logs, raw dumps, transcripts and artifacts. The SQLite and vector index, worktrees, terminals and
generated host resources are derived and are not checkpointed.

The live board has two implementations of `TaskReader`/`TaskWriter`: Kanboard over JSON-RPC and the
PostgreSQL board store. A process must pick one explicitly from `SECRETARY_CARD_BACKEND`
(`kanboard` or `postgres`; missing, empty and unknown values refuse), once per process. The PostgreSQL schema, transactions and migrations are in
[Board store](BOARD_STORE.md). Backup components follow the same backend: normalised board and
process state is common, plus `raw_board` for Kanboard or `postgres_dump` for PostgreSQL. Backup code
does not read ORM rows. It asks the board client for normalised state and the `board-store.env`
resolver for the owner connection; dump/restore runs in `board/postgres_recovery.py`.

Product configuration reaches an installation one way. `secretary upgrade` generates
`heads/heads.yaml` from the installation's head canon (its own `heads/heads.toml`, else the product
default), writes `heads/source.yaml` recording the canon, its owner, checkout, revision and snapshot
digest, and commits and pushes both. A live tick reads only that installed pair, so editing a product
working tree changes nothing until the next `upgrade`. The gap shows in `secretary status` under
`installation.head_registry`.

An installation is named by `--instance` or `SECRETARY_INSTANCE`, the checkout by `--product-root`.
Every other path (skill targets, shell entry points, role worktrees, runtime env file) hangs off the
home of the account that owns the installation: the owner of the instance directory, or
`--runtime-user`. A run as root therefore writes the paths the units name, not `/root`. Full order:
[Operations](OPERATIONS.md#path-precedence).

A fresh instance checkout is a depth-1, single-branch, no-tags snapshot of the remote default-branch
tip, validated in a private stage and adopted atomically. Recovery then fetches only new history and
fast-forwards. The one accepted local divergence is a head-registry checkpoint commit kept by an
earlier degraded recovery, which is merged with upstream without pushing. Details:
[Operations](OPERATIONS.md#recovery) and [Recovery](RECOVERY.md#fresh-install-and-recovery). A manual
cold archive is optional and plays no part in recovery readiness.

## Runtime flow

```text
operator / automation
          │
          ▼
  secretary task and sprint protocol ────────> board backend
          │                            ▲
          ▼                            │
 production dispatcher ──> HeadRuntime ──> runtime backend ──> native agent CLI
          │
          └──── run/audit state ──────┘

agent heads ── Bearer grant + HeadRun heartbeat ──> memory MCP/index <── facts journal <── curator
```

Every supported board write goes through `secretary task` or `secretary sprint`, which apply role
guards, transitions and append-only audit ([Protocols](PROTOCOLS.md#tasks)). Cards and sprints are
exported to the checkpoint and restored from it as separate sets
([Recovery](RECOVERY.md#what-the-checkpoint-contains)). Card references are recovery identities, not
backend row ids. Export and restore share one board validator, so the checkpoint never holds canon
that restore would refuse.

The dispatcher resolves routing, drives the worker and reviewer lifecycle, and checks board,
workspace, report and review state before each transition. A substantive reviewer verdict parks the
card in Assessment with the reviewer stopped and the worker held; the merge or next round runs only on
a tick that carries out a recorded observer decision. Mechanical outcomes resolve in Validate.

Sprint budget comes from the durable audit of linked cards, not from the observer. The dispatcher
writes one budget event per source event. At the hard limit the sprint becomes `stopped` and a
`budget_hard_stopped` event is written. Observer reconciliation reads only open sprints, so that stop
removes the live head without touching claimed cards. The sprint's resume entry is structured
metadata; its freshness is computed against card audit.

Standing agents: curator runs on its generic triggered-agent entrypoint. Steward and retro enter
`secretary.dispatch.standing_agent`, which supplies task-backed ports for steward reports and retro
Done retention. The generic runtime owns only the port interfaces and does not import Secretary.

### Head runtime ownership

`HeadRuntime` is the lifecycle boundary for the dispatcher and the mechanical-role driver. Its verbs
(start, deliver, observe, request drain, stop, conditional stop) return typed receipts; callers do not
infer success from a pane, a socket write or process existence. The backend is chosen per head
profile from the closed set `orca-legacy | local-pty` (absent means `orca-legacy`), and
`triggered_agents.runtime.head_runtime_backends` is the only place a name becomes a backend.

- `OrcaLegacyHeadRuntime` runs heads in Orca panes. Its readiness probe and conditional stop narrow
  races but cannot make observe-then-stop atomic.
- `LocalPtyHeadRuntime` runs a per-run supervisor that owns the process group, PTY, Unix socket and
  a versioned append-only journal. Delivery, drain and stop share one lock. The supervisor's status
  frame is the live source for turn, admission and journal sequence; a bounded 64 KiB journal tail is
  the fallback after it exits. Uncertainty closes admission; confirmed process death never creates a
  permanent lease.

Mechanical scheduler units are `Type=oneshot` ticks with `KillMode=process`, so a supervised head
outlives the tick. The next tick controls it through the runtime's identity-fenced drain/stop. The
role's `AgentState` keeps the `HeadRun`; starting over a matching live run is refused, while a run
confirmed dead can be replaced. Every supervised process has a run directory, identity record and
socket.

Head liveness and recovery rules are in [Head vitality](HEAD_VITALITY.md).

### Card workspaces

On the Orca backend one card occupies one Orca worktree. The worker has its own terminal. The
reviewer opens as a split pane in the same worktree; the standalone-terminal fallback and its
fail-closed rules are in [Operations](OPERATIONS.md#worker-and-reviewer-launch-intent). When review
starts, the worker's head is stopped and its commit recorded, and the merge gate refuses a green
verdict if the checkout has moved since. Orca decides where the worktree lives (from the binding's
`orca_binding`). A returned worktree is accepted only if Orca's record ties it to this project's
repository and this card's workspace name; otherwise it is removed before bring-up fails. Head
rendering and delivery are adapter-specific, but not a stable plugin API.

The dispatcher owns only `.secretary-task-env/venv` in a card worktree; `.venv` belongs to the
project adapter. It claims the environment with an owner record, adds its workspace paths to Git's
`info/exclude`, and never writes production package paths into either environment. One immutable
`ProductionRuntime` value binds the production interpreter, product root and `secretary` import at
workspace creation, launch, gate, release and teardown; a mismatch keeps the worktree and blocks the
card. Head-visible Secretary commands name the absolute production interpreter with `-P`. Details:
[Operations](OPERATIONS.md#dispatcher-task-python-isolation).

Before any interactive Codex head launches (worker, reviewer, observer or service agent), one
preflight answers the CLI's first-run questions: it marks the workspace trusted, appends only missing
entries, and stops bring-up with a reason if a path is held at a different trust level. Then the pane
is created, readiness awaited, the prompt delivered and the turn confirmed. A workspace that cannot be
prepared fails before anything starts. Operator `secretary shell` sessions skip the preflight.

## The read layer

`secretary.webproto` answers the operator's questions (what the system is doing, what a card is
doing, what happened to it) for every transport: the `web-read` CLI and the web dashboard. A
transport only maps typed errors to its own codes and renders snapshots.

The layer collects no facts of its own. Health is `collect_status` (what `secretary status --json`
prints); projects are the validated bindings; cards come from `TaskReader`; history is the board's
append-only audit, so a cursor is a position in that audit; agents are dispatcher production state
plus launch heartbeats.

Constraints, enforced by tests:

- nothing under `webproto` imports HTTP, sockets, a framework or a template engine;
- read operations never write the board, dispatcher state, audit or installation, and take no actor;
- liveness is process state, never pane, terminal or window state.

Protocol, schema, states and cursors: [Protocols](PROTOCOLS.md#reading-the-pipeline).

## The product runtime

The other half of `secretary.webproto` raises a real worker head for a card and a reviewer head from
its result, and owns their workspace, process, pid, logs and outcome.

It does not use Orca. The start and result-reading paths use no Orca CLI, RPC, terminal or repository
inventory, and a test enforces this. It reuses existing parts: `LocalPtyHeadRuntime` through
`head_runtime_backends.build_head_runtime`, the watchdog's launch-identity heartbeat for liveness,
the supervisor journal for exit status, the head registry and `head.command.render_head_command` for
the command, `codex_preflight` and `claude_env` for first-run preparation, and the board's audit
for run events. New parts: a `git worktree` workspace, a durable run record and one admission gate.

Invariants:

- Run phases `claimed → raising → raised → settled` move in one function, and every spawn and close
  goes through it. The record that can find and stop a head is durable before the spawn. The record
  alone is enough to stop the head. An unconfirmed cleanup is recorded as unresolved, and admission
  refuses a second run beside an unresolved one.
- "The run is over" is a boolean set by the lifecycle: the process is confirmed gone, or none was
  spawned. It is the only thing admission checks. The run's outcome is derived separately and may be
  `source_unavailable`.
- Raising a head and publishing its start, and settling a run and publishing its end, are separate
  durable writes. Every repeat and every read of a settled run republishes what it owes. Events are
  pure functions of the run record. A request id owns an operation and its inputs.
- `webproto.admission.admit` is the one ownership check: a product run is never a second owner of a
  dispatcher attempt or a card in a sprint's reserved projects.

Operations, idempotency, ownership and outcomes: [Protocols](PROTOCOLS.md#running-the-pipeline).

## The web transport

`secretary.web` serves the dashboard, card, sprint, project and history pages and a JSON API over
HTTP, using the standard-library `http.server`. It is a transport like `web-read`/`web-run`: each
entry of `secretary.web.app.ROUTES` is one `secretary.webproto` operation, and one table
(`secretary.web.statuses`) maps protocol codes to HTTP status. It holds no snapshot, state derivation,
liveness rule or mutation of its own. A missing fact is added to the layer, not to a page.

Constraints:

- `secretary.web` imports nothing from the product except `secretary.webproto`.
- No session state. Cursors belong to the client, and repeated POSTs carry the client's request id,
  so a retry never raises a second head.
- Every public `webproto` operation is wrapped by `webproto.boundary.ProtocolBoundary`, which turns an
  implementation failure into `backend_unavailable`. One unreadable source marks its page section
  unavailable instead of failing the page.
- Loopback only. The service has no authentication and its POST routes start heads and change
  state. A non-loopback bind is refused before a socket exists, after resolving the name and
  checking every resulting address.
- A run is shown as two facts: what its process did (outcome and whether it is over) and what it
  produced (result document, verdict, exit status).

`secretary-web.service` runs it on `127.0.0.1:8787`. A product run started through it must name a
head-registry profile that declares `local-pty`.

Routes, status table, cursors: [Protocols](PROTOCOLS.md#serving-the-pipeline-locally). Running it:
[Operations](OPERATIONS.md#the-local-web-transport).

## The guarded front

External access is TLS plus one password, and the product contains no authentication code.
`secretary.webfront` renders a Caddy configuration (Caddy from the Ubuntu archive) that terminates
TLS, checks the owner's password with `basicauth *` against a bcrypt hash, and proxies to loopback.

- The front is the only public listener; the application cannot bind anywhere else, and pages call
  the read layer in-process, so there is no internal HTTP surface.
- `secretary.webfront.guard` parses the rendered file and reports every entry of
  `secretary.web.app.ROUTES` that would be answered before the password check.
- The password and its hash live in the installation secret store. The rendered file is `0600` state
  under the data directory; rotation is `set-password`, `render`, restart, with no commit.

Commands: [Protocols](PROTOCOLS.md#publishing-the-pipeline-the-guarded-front). Runbook:
[Operations](OPERATIONS.md#the-published-web-front).

## The sprint observer head

Each open sprint gets its own observer head. It never claims cards, never appears in card records
and never takes the per-project claim gate; the dispatcher runs the sprint's cards independently.
Operator view and states: [Operations](OPERATIONS.md#sprint-observer-heads). Sprint contract, the
declared observer and the observer fence: [Protocols](PROTOCOLS.md#sprints).

The production tick runs an observer reconciliation pass against the sprints board:

- open sprint, no live head: launch one;
- open sprint, live head: nothing (one head per sprint);
- open sprint whose head's launch identity positively shows a dead process: launch a replacement, on
  the persisted launch backoff. A missing or unreadable identity is not death;
- closed or vanished sprint: stop the head and drop the record;
- unreadable sprints board: change nothing.

A stop the host rejected is not a stop: the record stays `stop-pending` with its handle, and a
relaunch waits until the old terminal is closed.

The head profile is the sprint's declared `sprint_observer`, resolved against the installed
`heads/heads.yaml` without fallback; an unknown profile fences the sprint's projects.
`role_defaults.observer` never chooses it. The profile is interactive (one session per sprint). Launch
passes the same resource-readiness gate as a card claim, uses the ordinary head-command renderer and
role environment wrapper, and gets only role-scoped environment, not the whole `runtime.env`.

The observer workspace is cut from a dispatcher-owned empty repository without a remote
(`<data>/dispatcher/observer-root/observers`, created on first use and registered with Orca). It never
gets a project checkout. Reconciliation neither creates nor deletes this repository, and `doctor`
accepts its registration only at that path. Stopping closes all terminals of the workspace, then
removes the worktree registration.

The launch prompt is rendered from the live sprint entity and points to the `observe-sprint` role
skill by path without repeating it. The skill lives in `skills/` of this repository, is registered as
the `observer` role in `skills/manifest.toml` and is delivered by `secretary role-skills sync` to the
shell of every profile a sprint may declare. If the skill is missing from the target shell, launch is
deferred with a reason naming the file.

### Wakes and delivery

Liveness uses the same pid heartbeat as workers and reviewers. Readiness is Orca's `tui-idle`,
classified as ready, busy, blocked (dialog) or unanswerable. A head that cannot be addressed or
probed goes into the bounded failure path, not a wait.

A committed, significant event on a linked card opens a durable delivery batch with an immutable
high-water mark, written before a nudge or replacement. Only an observer resume carrying that
delivery's audit marker acknowledges the batch. An active card alone does not create a turn. A batch
is redelivered when the head is seen ready without acknowledging it, or when its acknowledgement
deadline passes; a head never ready for input is bounded by a turn ceiling. Failed wakes retry a
bounded number of times on the live head, after which the head is replaced and the same delivery
marker is carried into the new launch. Cumulative wake and launch-delivery counts and the last
failure's bounded evidence (never prompt text) stay on the observer record while the sprint is open.

All interactive heads share one delivery path, whichever provider or role:

- The pane receives one short line with the absolute path of a task document (the worker's `TASK.md`,
  the reviewer's private review document under run artifacts). Nothing from a card description
  reaches a terminal write.
- Delivery has observed stages: payload written, Enter taken, turn observed, caller acknowledgement.
  The pane is fingerprinted before and after the send (composer content and Orca's opaque output
  cursor). A composer still holding the payload is re-entered, an empty one is rewritten, and an
  advanced cursor counts as a taken turn. Retries are bounded; host refusals surface as delivery
  failures with evidence.
- A turn is confirmed from the provider's own local session record (Claude or Codex). The status line
  may confirm a turn but never refute one.
- An unconfirmed delivery does not close the pane. Bring-up returns an abort with evidence and keeps
  the launch intent; the next tick adopts the head or stops it through its retained identity.
- Reviewer bring-up failures go through one recorder in `start_review`, which stores
  `review_delivery_failures` and `review_delivery_evidence` on the card without changing routing.

### Durability

Lifecycle events go to the durable audit keyed by sprint reference and are deduplicated by request id,
which includes the record generation. As with card writes, the event is staged, the host is called,
then the event is committed. A staging failure cancels the action. A commit failure keeps the effect,
leaves the event pending for `secretary task reconcile-audit`, and reports a pending audit.

The launch intent (sprint, generation, profile, attempt, workspace, pid file) is flushed to production
state before the host call; unwritable state means no launch. A tick that dies after the host call
leaves the intent, and the next tick adopts a live pid, waits out the startup window, or closes the
workspace terminals and relaunches.

A freeze stops observer heads and records the reason; resume brings them back through reconciliation.
A drain leaves live heads alone and launches none, but a sprint opened during a drain still gets a
deferred record.

## Memory plane

Facts are markdown records under `state/memory/facts` in the instance repository. The curator writes
through `secretary memory propose/commit/supersede`, which commits only `state/memory` under the
shared instance-repository writer lock. The butler may only `propose`; `commit` and `supersede` belong
to the curator, secretary and operator roles ([Protocols](PROTOCOLS.md#memory)). Other heads read
through MCP. The NDJSON export and the SQLite/vector index in the data directory are rebuilt from the
canon, and one index writer publishes at a time.

Unresolved cross-project conclusions sit under `state/memory/facts/po-review` as scope `review:po`.
The interactive PO, curator and retro see them; worker, reviewer, observer and steward grants do not.
A reviewed item is superseded into its final scope.

The shipped `packaging/memory/product-secretary` pack feeds the same canon at
`state/memory/facts/product-secretary` (scope `product:secretary`). Its manifest, paths and SHA-256
digests are verified before it is materialised. `state/memory/packs/product-secretary.json` records
the installed digest and owned fact ids; a local fact with a shipped id is refused. Incremental index
reconciliation reuses embeddings with unchanged id and digest. The ledger is `pending` until the
export is handed to the memory daemon's runtime user, then `ready`, so a failed handoff is retried by
the next upgrade.

The embedding model runs locally and is the appliance's main memory consumer
([Operations](OPERATIONS.md#system-requirements)).

## Knowledge planes

Where a record goes depends on its length and purpose:

- the Pipeline board holds executable work: cards, specs, states;
- curated memory (`state/memory/facts`) holds the short current conclusion a head receives through
  `memory_search`;
- knowledge (`state/knowledge`) holds the long reasoning behind it: brainstorms, decision logs,
  incident write-ups.

Sections directly under `state/knowledge` belong to the installation. A connected project's documents
live under `state/knowledge/projects/<project id>/<section>/`, with the id from `projects/`. A product
repository carries contracts and code; the reasoning behind its development is installation state.

Knowledge is not indexed, not returned by `memory_search` and never loaded wholesale into a head's
context. Format is free markdown. Writes go through `secretary knowledge write`, which owns only
`state/knowledge`, takes the shared instance-repository writer lock and refuses documents containing
secrets ([Protocols](PROTOCOLS.md#knowledge)).

## Ownership and security

- The security profile assumes one trusted host owner. Agents are not isolated as untrusted tenants.
- `doctor` reads config, data and host inventory and never changes the host. `status` and `doctor`
  share one recovery projection; it does not decrypt values, update the probe cache or launch heads.
- `reconcile plan` computes desired state. A matching name or prefix confers no ownership without a
  managed manifest or a product-written marker. The observer root's session-manager registration is
  created lazily by the dispatcher, not by reconciliation.
- Store-registered secrets reach instance Git only as encrypted envelopes. The raw installation key,
  the recovery phrase and `runtime.env` stay out of Git. Facts, exports and diagnostics carry no
  secrets.
- Private instance-remote and project Git go through one product-owned remote-execution boundary. HTTPS
  children clear ambient credential helpers and use explicit bootstrap input or the managed envelope;
  checkpoint probes and pushes use only the managed envelope. Local/file remotes are plain Git, SSH is
  manual bypass, and HTTPS hosts other than `github.com` are refused.
- Root-run recovery hands the instance and data roots to the installation account at one named
  ownership barrier, before that account's first secret-consuming Git child, and verifies the restored
  key is a regular `0600` file owned by it.
- Head-registry materialisation commits locally before a fast-forward-only publish. Upgrade and
  checkpoint stop when publication fails; only recovery continues as degraded and keeps the commit.
  There is no reset, rebase, force-push or ambient credential fallback.
- Recovery isolates one binding's provisioning failure; core failures and interruption are not
  isolated. `ProjectAvailability` carries unavailable checkouts into host planning and dispatch; an
  unavailable binding gates its own worker and reviewer, never observers. The installation stays
  degraded until every binding is available and the recovery checkpoint is published.
- Task audit and pending writes fail closed: an unfinished board mutation blocks export and the
  checkpoint.
- Restoring a normalised board into Kanboard uses bounded JSON-RPC batches (at most 200 calls and
  1 MiB per document, 50 calls for comment reads and writes). Each card or comment has a durable
  `restored_bulk` obligation in `TaskAudit` before any write. Aggregate replies are never treated as
  atomic: rows are proved from fresh inventory, and replay mutates nothing that is already proved.
  Comment waves hold at most one occurrence per entity, and comment order relies on pinned Kanboard
  v1.2.46 returning comments in `(date_creation, id) ASC` order. Test coverage:
  [Testing](TESTING.md#normalized-board-bulk-recovery).

## Cutover controller

`secretary.cutover` orchestrates the Kanboard-to-PostgreSQL cutover out of existing product
operations. It does not implement its own migration, import, parity, backup, checkpoint, pause or
backend selection. Every completed phase is fsynced to the data plane before the next one starts, and
a failure is durable and leaves the global freeze in place.

- One installation-wide `CutoverLock` serialises mutation. State identity is a digest of installed
  revision, provenance, paths, source fingerprint, counts, parity and phase vocabulary, and the
  confirmation token derives from it. Retries accept only the original actor, reason, revision and
  identity.
- While a cutover is in flight, public writers are fenced; only controller children carrying the
  state identity pass. Terminal states release the fence.
- `prepare-successor` is a separate OID-driven state machine under the same lock. It writes durable
  intent before every PostgreSQL effect and completed evidence after verification, and refuses any
  database-name-to-OID mapping it did not record. Its terminal authority is immutable recovered history
  plus a release receipt bound to the predecessor plan and archive.

Commands, phases and tokens: [Protocols](PROTOCOLS.md#secretary-cutover). Runbook:
[Operations](OPERATIONS.md#postgresql-board-store-cutover). Recovery boundary:
[Recovery](RECOVERY.md#cutover-controller-state).
