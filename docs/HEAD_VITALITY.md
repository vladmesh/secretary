# Head vitality

Head vitality answers "is the head working?" as three independent observation axes, fused over time
with hysteresis, then turned into a recovery intent. Modules:

- `src/secretary/dispatch/head_vitality.py`: observation vocabulary and snapshots;
- `src/secretary/dispatch/head_vitality_episode.py`: the episode reducer;
- `src/secretary/dispatch/head_vitality_guard.py`: the destructive-step guard;
- `src/secretary/dispatch/head_vitality_policy.py`: the recovery policy.

`HeadRuntime` owns the lifecycle boundary. The local-pty backend owns delivery, turn lease, drain and
stop atomically. The Orca legacy backend has a weaker conditional stop.

## Central invariant

Every external observation has a TOCTOU window: the head may start a new turn between the last reading
and any action. Agreeing channels reduce noise but never close the window.

> An observation reports facts. Fusion forms suspicion. Policy chooses intent. A runtime that
> atomically owns delivery decides whether an intervention is safe.

Local-pty performs the final admission/lease/epoch check inside its own lock against durable supervisor
evidence. Orca readiness, terminal output and filesystem fingerprints never grant a kill capability by
themselves; `OrcaLegacyHeadRuntime.stop_if_quiescent` is a best-effort fence around the external
observation window.

## Three independent axes

```text
Process  = Running | Suspended | Dead | Unknown      does a kernel process exist?
Turn     = Active  | Idle    | Unknown               is a turn in flight?
Progress = Advancing| Quiet  | Stagnant | Unknown    is the work moving?
```

A pid heartbeat answers only Process, a provider cursor only Progress, a pane only Turn. An axis a
snapshot cannot answer stays `Unknown`, and that survives serialisation: `Process=Running` with
`Turn=Unknown` is a valid state.

`Stagnant` is reserved for conclusions over time. No single observation produces it: one unchanged
cursor is `Quiet`.

## Snapshots

A `VitalitySnapshot` is one channel's reading of one head run at one instant.

- **Identity-bound.** Every snapshot carries the `HeadRun.run_id` it was proven against. A source whose
  attestation names another run degrades to `Unknown`/unavailable, never `Dead`. A snapshot never
  combines a new run's pid with an old run's cursor.
- **Unavailable is not no progress.** A missing pid file, refused pane probe or unreadable journal
  yields `Unknown` axes, `availability=unavailable` and a bounded reason. A broken channel freezes
  knowledge; it never counts as stall evidence.
- **Pane readings are advisory.** They fill only `Turn` and are stamped `source=pane_advisory`.
  Readiness says whether a pane accepts input, not whether the head may be stopped.

| Source | Producer wrapped | Axes answered |
|---|---|---|
| `pid_heartbeat` | `dispatcher_watchdog.head_process_status` | Process |
| `provider_cursor` | `dispatcher_tui.provider_progress_for_run` | Progress |
| `pane_advisory` | pane readiness (`{"idle": bool}`, from `pane_host.Pane`) | Turn |

Mappings:

- heartbeat `live-match` with `/proc` state `T` → `Suspended`; dead, zombie, or a pid reaped between the
  signal check and the `/proc` read → `Dead`; otherwise live → `Running`; missing, unreadable or
  mismatched → `Unknown` + unavailable;
- cursor moved since this run's previous snapshot → `Advancing`; unchanged → `Quiet`; unadmitted,
  foreign or unreadable → `Unknown` + unavailable; the first observation of a source records its cursor
  without a progress opinion;
- pane ready → `Turn=Idle`; busy → `Active`; unanswerable → `Unknown`.

Snapshots are frozen dataclasses with `to_json`/`from_json`. A payload with an unknown version or axis
value raises instead of being normalised.

## Episodes

`VitalityEpisode` is the persisted hysteresis layer. `reduce_vitality(previous, snapshots, now,
thresholds, retained=..., answer_owed_since=...)` folds one tick's snapshots for a run into a durable
verdict. It is pure and deterministic: no I/O, no clock; the caller owns `now`.

```text
HealthyActive   a non-advisory source showed advancement now
HealthyQuiet    alive, no advancement yet, below every threshold
Suspended       /proc shows the process parked on a stop signal (not a stall)
Retained        the same parked process, held by the dispatcher's own retention
SuspectedStall  strong quiet outlived suspect_after
ConfirmedStall  strong quiet additionally outlived confirm_after
Dead            the heartbeat names a gone or unreaped process
Unverifiable    no strong channel answered; nothing may be concluded
```

Reducer rules (pinned in `tests/test_head_vitality_episode.py`):

- Snapshots naming another run are dropped and noted in `basis`. When all snapshots agree on a new run
  id, a fresh episode starts.
- `Dead` outranks everything. `Suspended` freezes the stall clocks: suspended time feeds no threshold,
  and quiet references shift past the frozen span on resume.
- With `retained=True` a parked process reduces to `Retained` instead of `Suspended`; the freeze is the
  same. `Dead` still outranks it, and a retained process that is running again is not `Retained`.
- Advancement from any non-advisory source ends a suspected or confirmed episode, resets phase
  timestamps, stamps `last_progress_at`/`last_progress_source` and bumps `activity_epoch`.
- An unavailable source freezes its evidence and is tracked in `unavailable_since` until it answers.
  When every strong source (pid and provider) is dark the verdict is `Unverifiable`, except that an
  already confirmed episode stays confirmed.
- A caller-declared rejected report the head has not answered (`answer_owed_since`), followed by an
  advisory turn seen to end, raises `HealthyQuiet` to `SuspectedStall` at once. Without an owed answer
  advisory readings carry no weight.
- Advisory pane readings only corroborate in `basis`; they never drive a stall verdict.
- Quiet is measured from `max(last_progress_at, quiet_since)`, or `started_at` if neither is set, not
  from the observing tick. The ladder climbs `HealthyQuiet` → `SuspectedStall` → `ConfirmedStall` as
  that age crosses `suspect_after` and then `suspect_after + confirm_after`.
- Confirmation is sticky. Only progress, suspension, death or identity change ends it.

### Pid-only evidence

- **No progress source has ever answered.** The pid is the only witness, and process existence is not
  liveness. Sustained "running, nothing else" ages to `SuspectedStall` at `suspect_after` and
  `ConfirmedStall` at `suspect_after + confirm_after`, measured from `started_at`. The absent provider
  is neither progress nor quiet.
- **A progress source this episode has witnessed** (it left a cursor) and is now dark keeps the freeze,
  bounded by `dark_ceiling`.

### Dark sources

A witnessed progress source is dark when it answers unavailable **or** produces no snapshot on a tick
(`basis` says `absent@<source>`). The wait tick's status can carry a live `pid_status` with no provider
channel, for example `reason: "pid"` (exact live heartbeat whose pane the worktree inventory no longer
lists) or `reason: "disconnected"`. A source that never answered is not treated as dark; that is the
pid-only case above.

Darkness freezes the stall clock for at most `dark_ceiling`, measured from the start of the current
outage (a source that answers again leaves `unavailable_since`; its next outage starts a new window):

- **inside the window** an earned `SuspectedStall`/`ConfirmedStall` stands, and a healthy episode stays
  `HealthyQuiet` with the dark channel's diagnostic as reason; `basis` says `dark:<n>s@<source>`;
- **past the window** the episode ages on the pid alone, as in the pid-only case, with a reason naming
  the dark source and its duration. An earned confirmation is preserved.

For the guard, "not dark" means "answered on the most recent reduction".

### Quiet restart after a nudge

The report nudge (`_prompt_worker_report`) stamps `quiet_since = now`. The head is charged only with
silence after it was asked; `last_progress_at` is kept. Records without the field have no restart.

### Retention

`/proc` state `T` has two possible owners. The dispatcher parks a finished worker itself:
`host.retain_worker` sends SIGSTOP on `report:done` and `WorkerContinuation.begin_retention` records it,
so the worker stops editing while CI and the reviewer own the checkout and a red verdict can resume the
same conversation.

`_reduce_and_store_vitality_episode` passes `record.worker_continuation.retained` for the worker head
(the review head always passes `False`). `Retained` earns no rung: no SIGCONT, no nudge, no operator
escalation, and the guard refuses every destructive step (`retained`). A head stopped without an
active retention follows the `Suspended` ladder.

### Thresholds

| Threshold | Default |
|---|---|
| `suspect_after` | `IDLE_STALL_DEFAULT` (5 min) |
| `confirm_after` | `2 × IDLE_STALL_DEFAULT` (10 min) |
| `dark_ceiling` | `2 × IDLE_STALL_DEFAULT` (10 min) |
| suspension response window | 5 min (`SECRETARY_HEAD_SUSPENSION_RESPONSE_SECONDS`, `SUSPENSION_RESPONSE_WINDOW_DEFAULT`) |
| deterministic refusal limit | 3 |
| worker report outer ceiling | `WORKER_REPORT_STALL_DEFAULT` (6 h) |
| gate pending outer ceiling | `GATE_PENDING_STALL_SECONDS` (6 h) |

`dark_ceiling` must outlast a provider startup window, during which pane readiness and output cursor
cannot tell a starting head from a settled one, and stay far below the six-hour ceilings. A dark and
quiet head is nudged at `max(dark_ceiling, suspect_after)`.

### Verdict persistence

The worker/review wait tick persists each role's episode (`worker_vitality_episode`,
`review_vitality_episode` on the dispatcher record) and writes one durable comment per verdict change,
keyed on the transition. A tick whose status carries none of the observed sources runs no reduction and
writes nothing. A reduction failure degrades to "no episode" with a comment and never breaks the tick.

## Decision path

The wait tick decides from the persisted episode's verdict. The reduction runs on every wait tick,
including not-live shapes: a heartbeat naming a gone process reduces to `Dead`; a vanished pane over a
live process is an observation failure that waits.

| Verdict | Wait-tick action |
|---|---|
| `HealthyActive`, `HealthyQuiet`, `Unverifiable` | `wait`. Fresh evidence renews the outer `worker_waiting_since` window. A recovered suspension lands here with the ladder cleared. |
| `Retained` | `wait` only; the role's wait clock is renewed. |
| `Suspended` | Recovery policy: one identity-fenced SIGCONT per suspension span, a response window, then operator escalation. Never a stop. |
| `SuspectedStall` | At most one idempotent report nudge per round generation, then visible degradation (`{kind}-stall-suspected`). Never destructive. |
| `ConfirmedStall` | One report prompt if the round has not spent it, else `_trigger_wait_watchdog` → respawn once → escalate to Blocked. |
| `Dead` | Reclaim via `_trigger_wait_watchdog`. |
| No episode / `Unverifiable`, ceiling elapsed | Operator escalation, head untouched (`_escalate_unobservable_wait`): one idempotent durable comment naming the evidence gap and a degraded `{kind}-unobserved-wait-escalated` outcome. Before that, an authoritative deterministic refusal seen `deterministic_refusal_limit` times escalates without waiting for the ceiling. |

### The guard

Every watchdog-driven destructive step passes
`secretary.dispatch.head_vitality_guard.assert_destructive_allowed` before anything is stopped, killed,
respawned or replaced. It allows only `ConfirmedStall` and `Dead`. Refusal classes: `missing-episode`,
`foreign-run` (episode names another HeadRun), `healthy-active`, `healthy-quiet`, `unverifiable`,
`suspended`, `retained`, `suspected-stall`, `pid-only-ceiling-unelapsed`.

A confirmation earned with no progress source answering (pid-only, or a witnessed source now dark)
also requires the role's outer ceiling (`WORKER_REPORT_STALL_DEFAULT` class) to have elapsed since the
episode began; otherwise `pid-only-ceiling-unelapsed`. Only a confirmation on quiet that a progress
source is still answering for is acted on without that hold. A raising reducer fails safe to `wait`
plus one comment.

A refusal produces a degraded `{kind}-guard-refused` outcome and one idempotent durable comment keyed
on the wait cycle and refusal class. The body says only what that key names; live measurements (quiet,
dark sources, next deadline) stay on the episode and are read with `secretary head-status`.

Guarded entry point: `DispatcherRuntime._trigger_wait_watchdog`, which fences both arms
(`_respawn_wait`, `_escalate_wait`) through `_guard_or_wait`. The no-episode fallback's evidence
branches (`no output since launch`, `no terminal progress`) keep their triggers but act only under
`_trigger_wait_watchdog`; its pure clock branch escalates without destroying. `_stop_worker_confirmed`
and `_end_review_pane_confirmed` run only beneath a guarded entry.

Not guarded, because they do not act on vitality:

- operator-initiated stops (`CommandHostRuntime.stop_head` from an explicit operator command);
- card-lifecycle stops: Done/Blocked transitions, drain, the review bring-up's confirmed worker freeze
  (`_adopt_launch_intent`);
- launch-recovery stops in `dispatcher_launch.resolve_launch_intent`, which act on durable launch
  intents and heartbeat identity.

Tests: refusal classes in `tests/test_head_vitality_guard.py`; call-site coverage in
`tests/test_head_vitality_guard_sites.py` (guarded paths with the guard forced to refuse and through the
real guard; unguarded stops never consult it).

## Recovery policy

`head_vitality_policy.py` consumes only a persisted `VitalityEpisode` and returns a `RecoveryDecision`:
an intent, the rung it leaves the head on, and telemetry detail. It executes nothing; the dispatcher
executes intents under the guard. No input produces anything beyond `escalate_operator`.

### Rungs

Rung state persists on the episode (`recovery_rung`, `recovery_span_started_at`,
`deterministic_refusals`), so a dispatcher restart resumes the same rung.

| Rung | Verdict | Intent | Meaning |
|---|---|---|---|
| — | `Retained` | `observe` | Checked first, before the deterministic-refusal path. Resets the ladder to 0. |
| 0 | Healthy\*, `Unverifiable`, `Dead` | `observe` | Nothing earned. A recovered suspension clears the ladder here. |
| 1 | `SuspectedStall` | `nudge` | The wait tick spends the single nudge (`_prompt_worker_report`). |
| 2→3 | `Suspended`, new span | `sigcont` | One identity-fenced SIGCONT per span; the response window opens. |
| 3 | `Suspended`, window running | `observe` | The reduction flips the verdict as soon as the head resumes. |
| 4 | `Suspended`, window expired | `escalate_operator` | One durable comment; holds for the rest of the span. Never kill. |
| — | deterministic refusal ×N | `escalate_operator` | After `deterministic_refusal_limit` (3) identical authoritative sightings. |

A suspension span is keyed on the reducer's freeze stamp (`stall_frozen_since`): it starts when the
process is first seen parked and clears when it runs. Within a span nothing re-fires; a new span
restarts the ladder; recovery resets it to 0. Repeated identical observations are therefore free.

The policy does not yet route rungs through `request_drain`, safe replacement at a quiescent boundary,
same-profile respawn, runtime failover or evidence-backed `block`. `ConfirmedStall` recovery is respawn
once, then escalate.

### SIGCONT execution

`DispatcherRuntime._sigcont_head` is the only signal this path sends. At send time it re-verifies
identity through `guard_head_run_identity` (pid, boot id, proc start time, expected HeadRun); a
mismatched, unreadable or vanished heartbeat sends nothing. Delivery follows `_signal_head`: the head's
own process group when it has one, else the pid. SIGTERM/SIGKILL stay behind their guarded entries.
Each send writes one durable comment (`{kind}-vitality-sigcont`) naming the span and response window,
idempotent via the span stamp in the request id.

The response window must outlast several ticks so a resumed head can show life. Expiry escalates to the
operator and touches nothing.

### Gate phase

While CI is non-terminal, each `_gate_pending` tick runs the same reduction and policy for the worker
head: a stopped process is seen and SIGCONT'd within one tick, and an expired window reaches the operator.
A probe failure falls back to ordinary gate behaviour. `GATE_PENDING_STALL_SECONDS` is the outer
ceiling for the CI rollup (Blocked for a human, non-destructive); `WORKER_REPORT_STALL_DEFAULT` plays the
same role on the report-wait path.

### Deterministic refusal reasons

Only authoritative deterministic reasons may skip rungs. `DETERMINISTIC_TERMINAL_REASONS` is the
allowlist of refusals naming a property of this launch that retrying cannot change: invalid
configuration, missing executable, authentication rejected, resource exhausted, and
`terminal_split_source_not_found`. Matching is token-in-bounded-string against the diagnostic on the
snapshot (a dark source's reason is carried onto the episode). Timing, availability and transport
refusals do not qualify: counting them would let one dark channel fast-track a live head to
escalation. A tick with no deterministic reason resets the count.

Reviewer bring-up handles `terminal_split_source_not_found` before the policy sees it: the token can
occur before or after Orca attempts a child. It opens one standalone pane in the same worktree only when
before/after worktree inventories show no pane appeared; otherwise it fails closed.

Tests: rungs and idempotency in `tests/test_head_vitality_policy.py`; real-process SIGCONT and
foreign-identity refusal in `tests/test_head_vitality_policy_execution.py`; wait-tick and gate-phase
decisions in `tests/test_head_vitality_wait_decisions.py` and `tests/test_head_vitality_legacy_path.py`.

## Regression invariants

Each invariant is replayed tick by tick through the snapshot builders, fed the producer payload shapes
(`head_process_status`, `provider_progress_for_run`, pane readiness) and folded by the reducer under
`DEFAULT_VITALITY_THRESHOLDS`. A false "working" costs idle time; a false kill loses a live round, so a
verdict that can stop a head needs strong admitted evidence.

| Invariant | Tests |
|---|---|
| Provider `Advancing` ⇒ `HealthyActive`; advisory pane-idle alone never leaves `Unverifiable`; no destructive verdict while the transcript moves; such a head is never prompted or respawned. | `IssueB5195041CodexTranscriptBlindnessTests`, `IssueB5195041LegacyIdlePathTests` |
| Busy pane + `Running` + `Advancing` ⇒ `HealthyActive`; readiness unavailable is Turn-only and never stall evidence; unknown provider ⇒ `HealthyQuiet`, never `Dead`/`ConfirmedStall`. | `Issue3e7abdf9BusyReadAsUnavailableTests`, `Issue3e7abdf9LegacyBusyReadinessTests` |
| `Running` + admitted `Quiet` ⇒ `SuspectedStall` at +300 s and `ConfirmedStall` at +900 s from last progress; a busy pane only corroborates. | `Issue8f86ed63BusyMasksStallTests` |
| `/proc` state `T` ⇒ `Suspended` within one tick; stall clocks frozen; never `ConfirmedStall` or `Dead`; the gate-pending tick SIGCONTs a non-retained suspended worker within one tick. | `IssueFe04011bStoppedWorkerSixHourCeilingTests`, `IssueFe04011bLegacyGatePendingTests` |
| A repeated deterministic reason with a live terminal keeps `Unverifiable` and escalates after 3 identical sightings; a repeated heuristic reason earns only observation. | `CodegenOrchestrator1194DeterministicSplitFailureTests`; `ReviewPaneTests.test_reviewer_falls_back_when_connected_anchor_is_not_split_capable` |
| A confirmed retention ⇒ `Retained`: no SIGCONT or other rung, so a red gate reuses the suspended session; `Dead` still outranks it. | `Issue02fe04d7RetainedWorkerTests` |
| A dark progress source freezes only for `dark_ceiling`, then `SuspectedStall` (spending the nudge) and `ConfirmedStall`; the reason names the dark source; nothing is stopped before the outer ceiling. | `Secretary1517Tests`, `Secretary1517WaitTickTests` |
| A status with no provider channel (`reason: "pid"`, `"disconnected"`) after the provider answered once is stamped `absent@provider_cursor`, takes the `dark_ceiling` window, and a confirmation is held behind the outer ceiling. | `ProviderLessStatusShapesTests` |
| Pid-only `Running` with no progress evidence ages to `SuspectedStall` then `ConfirmedStall`. | `Issue06dcf6cbUmbrellaLivenessContractTests` |

Reducer timelines live in `tests/test_head_vitality_regression.py` and
`tests/test_head_vitality_episode.py`; wait-tick and gate behaviour in
`tests/test_head_vitality_legacy_path.py` and `tests/test_head_vitality_wait_decisions.py`. The
vitality suites carry no `expectedFailure` markers.
