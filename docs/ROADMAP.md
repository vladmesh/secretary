# Roadmap

This roadmap describes a sequence of product states, not a queue of tickets. Each milestone lists the
outcome it must reach, the gate that decides whether it is done, and the questions still open.

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
remote through one supported `install` / `recover` sequence. The flow accepts host-only credentials and
rebuilds the board, memory index and role worktrees, then applies the materialiser on a clean target.
Bundled package transport for the board and session-manager runtimes, and full adoption of an existing
live host, remain the open parts of Milestone 1.

## Sprints today

A sprint is the product's unit of work: an entity on the sprints board holds the goal, Definition of Done,
repositories, status, budget, current card and resume entry. Linked cards stay on the Pipeline board and
carry a reference to the sprint.

The production dispatcher launches one observer head per open sprint and runs its lifecycle. The observer
recovers its work from the sprint entity and the live board, and writes a structured resume entry after
significant transitions. The budget comes from the durable audit of linked cards: the signal threshold
calls for reconsidering the plan, the hard threshold moves the sprint to `stopped`.

While a sprint is open, its repositories belong to the observer as the only product writer. Changing linked
cards from outside requires `--sprint-override` and a recorded reason. The `open-sprint` role skill
prepares the goal, Definition of Done and repositories, after which the product command creates the entity.

The model does not yet have closing cards or a coded completeness check, review levels or per-project
overlay deviations, an in-house CI runner for private repositories, or cleanup by owner label. Four gaps in
the loop itself are known and not yet closed:

- a stale resume entry is visible from outside but wakes nobody and raises no signal;
- observer liveness is measured by process, not by work: a head whose agent queue has ended stays
  `running` and holds the sprint still behind a green summary;
- a rework round leaves a dead terminal tab in the card's workspace;
- a change to the sprint contract sent as an entry on the entity does not reach the observer: it accepts
  material and questions, but not a narrowing of the Definition of Done.

## Milestone 1. Automatic fresh install

### Outcome

One bootstrap command installs the appliance on a supported clean VPS. The installer creates the chosen
dedicated OS user, the private instance repository and the local data plane, and installs the board,
session manager, memory service, dispatcher, background roles and schedules. No agent head is installed
automatically.

### User path

```text
install secretary
  -> choose installation user and private remote
  -> fill credentials/.env
  -> apply
  -> status
```

### Already implemented

- The product materialiser idempotently plans and applies packaged services and timers for the enabled
  components.
- Skills, units and the session-manager automations of background roles are derived from the product root
  and `automation.toml`; a repeat run keeps automation ids and unit names stable.
- Existing role worktrees are synchronised, but creating missing worktrees is still part of the unfinished
  clean-host flow.
- `doctor` and the materialiser's verify step show missing or drifted host resources.

The milestone stays open until there is a supported bootstrap command and a clean-host end-to-end run with
no pre-prepared checkouts, board or session-manager state.

### Acceptance gate

- A supported host has no pre-prepared home directory for the installation user, no checkouts, no board
  and no session-manager state.
- Every host path and resource name is derived from the instance and the discovered host context.
- The installer installs and configures the bundled board and session manager without a pre-prepared
  runtime.
- Memory, dispatcher, curator, steward, retro and the schedules come up through the materialiser with no
  hand-copied units and no editing of generated files.
- A repeat apply is idempotent, and an existing installation user triggers an explicit adopt/recover gate.
- An installation with no heads is a valid, observable state.

### Open questions

- The exact package and install transport, and the supported session-manager version.
- Minimum CPU, RAM and disk for the production memory profile.

## Milestone 2. Git-backed recovery

### Outcome

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

### Acceptance gate

- The checkpoint holds instance config, persona, the project and head registries, policies, the board
  export, memory facts and the necessary run and audit state.
- The vector index, terminals, worktrees, generated units and host-local caches are not canonical.
- A snapshot passes validation before commit and push; remote divergence and push failure stay fail-closed
  and visible in status together with the checkpoint lag.
- Losing the original VPS does not prevent restoring the board, memory, operational configuration and the
  installation's static secrets from the remote.
- Once parity is confirmed, the main UX and the documentation no longer require an archive bundle or
  offsite transport.

### Decisions taken

- The RPO is at most 30 minutes; the checkpoint push uses its own 30-minute window on top of the
  production tick.
- A cold archive of raw transcripts and artifacts is allowed only as a manual option, with no timer, no
  offsite transport and no effect on recovery readiness.

## Milestone 3. Head onboarding and explainable routing

### Outcome

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

### Acceptance gate

- The model distinguishes agent runtime, account, account pool and head profile; a runtime is not equated
  with a model provider.
- A card selects `light`, `standard` or `deep`, but may name a model or head explicitly.
- The router applies overrides and hard availability constraints first, then weighs capability, quota and
  reset state, and a preference for a different model family for review.
- An account in an `unknown` state is available optimistically; quota, auth and transient failures move it
  into an explainable circuit-breaker state.
- Every run records the resolved runtime, model, account and decision trace.

### Open questions

- Quota telemetry sources for each runtime.
- Policies for automatic account rotation, once there is operational data.

## Milestone 4. Daily control plane

### Outcome

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

### Acceptance gate

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

### Outcome

Dependencies on the board backend, the session manager and specific CLIs are confined behind checkable
contracts. This is not a public plugin API, but the ability to replace a backend without rewriting the task
and agent lifecycle.

### Acceptance gate

- A board adapter implements the normalised task model, transitions, audit, and the export and import
  contract.
- A session protocol creates and lists durable sessions, starts processes, streams output, accepts input,
  reports exit state, terminates process trees and reconciles orphaned state.
- Head adapters implement discover, install, probe, launch, delivery and observe without mixing in task
  routing.
- Every contract has a backend-independent contract suite.
- A failure of the session-manager UI does not destroy task state or recovery semantics.

### Open questions

- Keep the current session manager, move to an existing alternative, maintain a fork, or build a minimal
  in-house session backend.
- Whether there is a real need for a second board backend and a public extension API.

## Milestone 6. Public open-source release

### Outcome

A new user can install a supported release, walk the main path and understand its boundaries without
knowing the project's internal history.

### Acceptance gate

- There is a versioned package, release notes, a compatibility matrix, schema and data migrations, and
  rollback.
- A clean-VM end-to-end run covers install, head onboarding, project add, a worker/reviewer task, the Git
  checkpoint and recovery on a second target.
- The trusted single-user security boundary, credential scopes and agent host access are documented.
- The licence, contribution path, issue templates and minimum deployment requirements are published.
- The examples contain no private paths, accounts, projects or the author's historical repositories.

### Open questions

- What telemetry may be collected locally, and only opt-in.

## Later directions

After the main delivery path, Telegram and voice can be added as new entry channels, together with a remote
control plane for a phone, richer model-quality metrics and additional deployment profiles. Team work, a
multi-tenant SaaS and a billing model are not on the current roadmap.
