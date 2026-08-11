# Roadmap

This roadmap separates what works now from the gaps that remain. Milestones describe useful product
states rather than commitments to a release date.

## Current baseline

The product implements a production dispatcher, a memory service and a Git-backed checkpoint. The task
lifecycle, the worker/reviewer loop, the memory journal, project onboarding, host planning and the
checkpoint recovery primitives are implemented. The checkpoint passes validation, is committed on the
production tick and pushed fast-forward only with an RPO of at most 30 minutes; publishing instance-repo
changes, the checkpoint writer and the pusher are serialised by one writer lock.

Normalised board and run state and the memory-facts canon live in `state/` of the private instance
repository. The local data plane holds mutable and derived runtime state and a rebuildable memory index;
it is not a second recovery repository.

A recoverable secret store keeps installation credentials next to board and memory: the canonical values
are encrypted, are rebuilt from a single recovery phrase, and travel out with the same push. Secrets are
part of the recovery contract alongside board, memory and operational configuration.

Background roles are materialised from the product canon: packaged units and each role's
`automation.toml` drive the curator, steward and retro roles, their timers and their session-manager
automations, with no hand-copied generated files. The instance holds the portable desired config for the
materialiser. Archive backup, offsite transfer and the backup timer are out of the recovery contract;
the archive remains a manual optional cold archive.

The product restores the installation user, config and state, and the local data plane from a private
remote through the current `install` / `recover` flow. The flow accepts host-only credentials and
rebuilds the board, memory index and role worktrees, then applies the materialiser on a clean target.
Bootstrap installs the pinned board and session-manager runtimes on Ubuntu 24.04. A clean-machine
end-to-end gate and full adoption of an existing live host remain open parts of Milestone 1.

## Sprints today

A sprint is the product's unit of work: an entity on the sprints board holds the goal, Definition of Done,
repositories, status, budget, current card and resume entry. The current implementation links Pipeline
cards directly to the sprint.

The production dispatcher launches one observer head per open sprint and runs its lifecycle. The observer
recovers its work from the sprint entity and the live board, and writes a structured resume entry after
significant transitions. The budget comes from the durable audit of linked cards: the signal threshold
calls for reconsidering the plan, the hard threshold moves the sprint to `stopped`.

While a sprint is open, its repositories belong to the observer as the only product writer. Changing linked
cards from outside requires `--sprint-override` and a recorded reason. The `open-sprint` role skill
prepares the goal, Definition of Done and repositories, after which the product command creates the entity.

The card model separates the durable product plane from execution:

- a Product owns issues and sprints and groups one or more projects;
- an issue requires a product, a kind (`bug`, `feature`, `question` or `improvement`) and a priority
  (`P0` through `P3`);
- the first Pipeline column is Issues, and issues cannot move into task columns;
- a sprint takes one or more issues, reserves projects and creates tasks only while it is running;
- tasks are execution records, not product backlog, and are archived when their sprint closes;
- only the PO explicitly closes an issue after checking the sprint outcome and product invariants.

One open sprint per installation is the shipped default. A second one exists as a gated pilot behind the
instance setting `open_sprint_limit`, which accepts 1 or 2 and is absent by default: at 2 a second sprint
is admitted only when it can be proven to share nothing with the first. What that means exactly, restore
included, is in [Protocols](PROTOCOLS.md#the-open-sprint-limit); the operator's view, the limitations and
the rollback are in [Operations](OPERATIONS.md#the-two-sprint-pilot). The setting is on for this
installation and a pair has run side by side, each sprint with its own observer head, through the full
card cycle.

The pilot deliberately isolates admission and the per-sprint mechanisms (the declared observer, whose
writes of role `observer` are bound to its own sprint, the observer fence when the board is readable, the
budget and its hard stop, claim suppression by a blocked card) and nothing else.
Pause, the single production tick writer and the blind fence stay installation-wide, and so does the
reach of a head that declares a role other than `observer`; see
[Operations](OPERATIONS.md#the-two-sprint-pilot).

Raising the limit past two needs work that does not exist:

- pause and drain scoped to a sprint rather than to the installation, so one sprint can be stopped for
  repair without stopping the other.
- a way for a tick that cannot read the sprint board to fence less than everything, which needs a
  declaration it can check without that board. The same declaration would close the blind fence's
  current gap, where a sprint admitted since the last readable pass leaves an unlinked card in a project
  it reserves unfenced.
- the exact-string round trip of repository roots through the live Kanboard metadata calls, which is
  covered by in-memory fixtures only.

The model does not yet have typed execution gates, a coded completeness check, per-project overlay
deviations, an in-house CI runner for private repositories, or cleanup by owner label. Known defects in
the loop itself are prioritised issues on the board under the `secretary` product, not a list here.

## Current product-work programme and self-hosting status

The Product/Issue/Task foundation is in place and the production loop is developing itself. The original
manual cutover checklist is historical: production `sprint:1402` completed a real code increment through
worker, exact-SHA gate, independent review, observer release, dispatcher merge, task archival and sprint
close without a manual prompt or budget event. A failed later sprint is an incident and a source of new
Issues; it does not by itself roll the whole programme back to manual execution.

That successful normal cycle is not blanket recovery evidence. Live observer prompt delivery still showed
`pane-stayed-ready`, its failures were omitted from the final sprint summary, and the post-green
reviewer-only retry did not run because the reviewer launched normally. These are prioritised board Issues,
not reasons to describe self-hosting as pending.

After cutover, the sprint system implements its remaining programme through itself:

1. typed tasks: `code`, `research` and `operation`, with a versioned knowledge artifact required for every
   research task and a structured state-evidence report for an operation;
2. abstract routing levels (`low`, `medium`, `high`, `frontier`) resolved by the dispatcher into explicit
   family, model and effort, with append-only per-round telemetry and a single-family degraded mode;
3. task review convergence: a review-only budget with analysis on red review three, a hard decision on
   five, one exceptional sixth fix round, and then a mechanical wait for the owner;
4. durable product decision requests and `awaiting_decision` when the Definition of Done proves impossible
   or materially incomplete;
5. parallel product sprints beyond the gated two-sprint pilot: more than two at once, and an observer per
   sprint, both of which need observer calls bound to a sprint identity.

Reviewer verdict and release decision remain distinct. Review stays independent and may report every real
blocker. At the budget boundary the observer may accept a mechanically green, architecturally convergent
increment with follow-up issues, reslice it, or use the last fix round. It does not rewrite a red review
into green.

## Milestone 1. Reliable fresh install

### Goal

A short bootstrap and install flow creates the appliance on a clean Ubuntu 24.04 VPS. It creates the
chosen dedicated OS user, the private instance repository and the local data plane, and installs the
board, session manager, memory service, dispatcher, background roles and schedules. Agent heads and
provider logins remain a separate operator choice.

### User path

```text
install secretary
  -> bootstrap host with installation user and private remote
  -> install a new instance or recover an existing one
  -> connect agent heads
  -> status
```

### Already implemented

- The product materialiser idempotently plans and applies packaged services and timers for the enabled
  components.
- `secretary bootstrap` installs pinned Kanboard and Orca runtimes, starts the board and generates its
  local credentials on Ubuntu 24.04.
- Skills, units and the session-manager automations of background roles are derived from the product root
  and `automation.toml`; a repeat run keeps automation ids and unit names stable.
- Missing role worktrees are created from the product checkout and existing ones are synchronised.
- `doctor` and the materialiser's verify step show missing or drifted host resources.

The milestone stays open until a clean-host end-to-end run exercises the whole path with no pre-prepared
checkouts, board or session-manager state.

### Done when

- The Ubuntu 24.04 test host has no pre-prepared home directory for the installation user, no
  checkouts, no board and no session-manager state.
- Every host path and resource name is derived from the instance and the discovered host context.
- The installer installs and configures the bundled board and session manager without a pre-prepared
  runtime.
- Memory, dispatcher, curator, steward, retro and the schedules come up through the materialiser with no
  hand-copied units and no editing of generated files.
- A repeat apply is idempotent, and an existing installation user triggers an explicit adopt/recover gate.
- An installation with no heads is a valid, observable state.

### Open questions

- The compatibility and upgrade policy for the pinned board and session-manager versions.
- Minimum CPU, RAM and disk for the production memory profile.

## Milestone 2. Git-backed recovery

### Goal

The private instance repository is used as an automatic durable checkpoint of configuration and normalised
state. The recovery contract requires no separate object store or backup host. Archive backup, offsite
transfer and the backup timer are out of the main path; the archive stays a manual optional cold archive
rather than a second equal contract.

### User path

```text
install secretary
  -> recover from private remote
  -> enter recovery phrase
  -> rebuild derived state
  -> status
```

### Already implemented

- The checkpoint holds the portable config and state canon, is validated before commit and written only on
  change.
- Push is fast-forward only, every 30 minutes. Failures and genuine remote divergence do not stop the work,
  but stay fail-closed for the checkpoint and are visible through `status` and `doctor` along with the lag.
- Concurrent feature publishing and checkpoint commits are serialised. Expected interleaving recovers
  automatically; foreign remote history stays a manual divergence.
- Derived state is excluded from the checkpoint; archive and offsite no longer appear in the main UX or in
  `doctor` gates.

The supported recover-from-private-remote flow and destructive-loss parity on a clean second target are
implemented. Milestone 2 is closed; the remaining packaging and live-adopt work belongs to Milestone 1.

### Done when

- The checkpoint holds instance config, persona, the project and head registries, policies, the board
  export, memory facts and the necessary run and audit state.
- The vector index, terminals, worktrees, generated units and host-local caches are not canonical.
- A snapshot passes validation before commit and push; remote divergence and push failure stay fail-closed
  and visible in status together with the checkpoint lag.
- Losing the original VPS does not prevent restoring the board, memory, operational configuration and the
  installation's static secrets from the remote.
- Once parity is confirmed, the main UX and the documentation no longer require an archive bundle or
  offsite transport.

### Decisions

- The RPO is at most 30 minutes; the checkpoint push uses its own 30-minute window on top of the
  production tick.
- A cold archive of raw transcripts and artifacts is allowed only as a manual option, with no timer, no
  offsite transport and no effect on recovery readiness.

## Milestone 3. Head onboarding and explainable routing

### Goal

The owner adds heads after bootstrap. The system discovers an installed CLI or offers to install it, runs
the external auth flow, checks capabilities and creates runnable profiles. Routing picks a head and an
account without a neural model in the loop.

### User path

```text
secretary head add
  -> discover or install runtime
  -> authenticate account
  -> create account pool and profile
  -> probe
```

### Done when

- The model distinguishes agent runtime, account, account pool and head profile; a runtime is not equated
  with a model provider.
- Every task selects one required abstract level (`low`, `medium`, `high` or `frontier`). The observer sees
  levels and model families, not concrete model names or effort values.
- The router applies overrides and hard availability constraints first, then weighs capability, quota and
  reset state, and a preference for a different model family for review.
- Worker and reviewer receive the same level. The dispatcher resolves both to explicit profiles, models
  and efforts and may swap their families after a convergence signal.
- If one provider budget is already exhausted, work continues in a visible single-family degraded mode;
  predicting budget exhaustion and rotating accounts in advance is not part of the first routing step.
- An account in an `unknown` state is available optimistically; quota, auth and transient failures move it
  into an explainable circuit-breaker state.
- Every run records the requested level and the resolved family, runtime, profile, model, explicit effort,
  account, outcome and decision trace. Automatic model-quality scoring is a separate later task.

### Open questions

- Quota telemetry sources for each runtime.
- Policies for automatic account rotation, once there is operational data.

## Milestone 4. Daily control plane

### Goal

The operator manages projects, settings and runtime through one product interface instead of assembling
low-level commands by hand. The CLI stays the first interface; the board and a live terminal view cover
work and observation.

### User path

```text
add project
  -> scan
  -> propose adapter
  -> provision
  -> gate
  -> smoke card
```

### Done when

- A high-level project workflow folds the current add, provision and gate stages into a resumable flow.
- `secretary status` combines services, schedules, heads, quota state, projects, cards, memory and
  checkpoint freshness; `doctor` stays the strict invariant check.
- Install, start, stop, logs, upgrade and uninstall are available through the product CLI.
- Schedules and their single owner are configured centrally and applied idempotently.
- Settings change through validated operations, even while instance-repository files stay canonical.

### Open questions

- When a separate web control plane is needed.
- When Git-backed config should be replaced by a control-plane database.

## Milestone 5. Protocol runtime boundaries

### Goal

Dependencies on the board backend, the session manager and specific CLIs are confined behind checkable
contracts. This is not a public plugin API, but the ability to replace a backend without rewriting the task
and agent lifecycle.

### Done when

- A board adapter implements the normalised task model, transitions, audit, and the export and import
  contract.
- A session protocol creates and lists durable sessions, starts processes, streams output, accepts input,
  reports exit state, terminates process trees and reconciles orphaned state.
- Head adapters implement discover, install, probe, launch, delivery and observe without mixing in task
  routing.
- Every contract has a backend-independent contract suite.
- A failure of the session-manager UI does not destroy task state or recovery semantics.

### Progress (2026-08-11)

The delivery seam of the session protocol exists and is in production. `PaneHost` states the three
operations delivery needs (send, read, wait_idle) with the current session manager as its one
implementation; prompt payloads travel as durable documents with only a bounded nudge through the
pane (`prompt_document.py`), turns are confirmed against the provider's own transcript rather than
the screen, and an unproven delivery never closes a pane — teardown happens only against exact
retained identity with the initiator recorded. Screen-state heuristics (idle/readiness
reconciliation) were rejected as a direction, not merely deferred: the screen is a secondary hint,
never the deciding signal. The wider head contract this seam grows toward — spec/run split, an
event stream out, an escalation ladder instead of a liveness poll — is recorded in the instance
knowledge decision `2026-08-10-golova-protocol-brainstorm.md`.

The head's own lifecycle now sits on that seam too (secretary-1412). `SessionHost` extends
`PaneHost` with the pane verbs a head's life needs — open, split, rename, close, inventory,
stop-by-workspace — and `triggered_agents/runtime/head` states the three operations over it:
`spawn(spec, workspace, task_ref)`, `nudge(run, pointer)`, `stop(run, initiator)`. A `HeadRun`
carries an identity the session manager cannot move by aliasing a pane handle, the lifecycle
`spawned → working → finishing → exited`, and the initiator of its stop, durably. `tests/
test_head_operations.py` runs all three against a fake host with no session manager installed,
which is the first backend-independent contract suite this milestone asks for, and the production
dispatcher's worker path — bring-up, delivery, stop — goes through those operations rather than
beside them. Worktree registration and computer use remain session-manager-specific, the reviewer
path still closes its own pane, and the second `SessionHost` implementation that would settle the
backend question by measurement has not been started.

### Open questions

- Keep the current session manager, move to an existing alternative, maintain a fork, or build a minimal
  in-house session backend. The honest way to decide remains a second `PaneHost` implementation
  running the same pipeline, so bug classes are attributed by measurement rather than argument.
- Whether there is a real need for a second board backend and a public extension API.

## Milestone 6. First supported release

### Goal

A new user can install a versioned release, walk the main path and understand its boundaries without
knowing the project's internal history. The repository, licence and contribution path are already
public; this milestone is about making a release supportable rather than making the code visible.

### Done when

- There is a versioned package, release notes, a compatibility matrix, schema and data migrations, and
  rollback.
- A clean-VM end-to-end run covers install, head onboarding, project add, a worker/reviewer task, the Git
  checkpoint and recovery on a second target.
- The trusted single-user security boundary, credential scopes and agent host access remain documented.
- Minimum deployment requirements are published.
- The examples contain no private paths, accounts, projects or the author's historical repositories.

### Open questions

- What telemetry may be collected locally, and only opt-in.

## Later directions

After the main delivery path, Telegram and voice can be added as new entry channels, together with a remote
control plane for a phone, richer model-quality metrics and additional deployment profiles. Team work, a
multi-tenant SaaS and a billing model are not on the current roadmap.
