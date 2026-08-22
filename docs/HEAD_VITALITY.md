# Head vitality observations

The dispatcher's destructive history (secretary-1063, cards codegen-orchestrator-1194..1197) came
from collapsing one question — "is the head working?" — into a single boolean and then acting on it.
The head-vitality model splits that question into three independent axes, observed as pure data and
fused later with hysteresis. This page describes the observation vocabulary introduced in
`src/secretary/dispatch/head_vitality.py`, the episode reducer
(`head_vitality_episode.py`), and — since S1-5 — the recovery policy (`head_vitality_policy.py`)
that turns a persisted verdict into an intent the dispatcher executes under the guard. The runtime
boundary that would own delivery atomically remains future work (see the plan "Head vitality,
собственный runtime и постепенный уход от Orca").

## The central invariant

Every external observation has a TOCTOU window: between the last reading and any action the head
may start a new turn. Two agreeing channels narrow the noise but never close the window.
Consequently:

> An observation reports facts. Fusion forms suspicion. Policy chooses intent. Only a runtime that
> atomically owns delivery can make an intervention safe.

Orca readiness, terminal output and filesystem fingerprints can never by themselves grant a kill
capability.

## Three independent axes

```text
Process  = Running | Suspended | Dead | Unknown      does a kernel process exist?
Turn     = Active  | Idle    | Unknown               is a turn in flight?
Progress = Advancing| Quiet  | Stagnant | Unknown    is the work moving?
```

The axes are independent on purpose: a pid heartbeat answers the first and nothing else, a provider
cursor speaks only to the third, a pane answer only to the second. A snapshot that cannot answer an
axis leaves it `Unknown`, and that absence survives serialisation — `Process=Running` with
`Turn=Unknown` is a meaningful, representable state, not missing data.

`Stagnant` exists in the vocabulary for the reducer's conclusions over time. No single observation
may ever produce it: one unchanged cursor is `Quiet`.

## Snapshots

A `VitalitySnapshot` is one channel's reading of one head run at one instant. Its rules:

- **Identity-bound.** Every snapshot carries the `HeadRun.run_id` it was proven against. A source
  whose attestation names another live run degrades to `Unknown`/`Unavailable`, never to `Dead`:
  "not mine" says nothing about death. A snapshot never combines a new run's pid with an old run's
  provider cursor.
- **Unavailable ≠ no progress.** A missing pid file, a refused pane probe or an unreadable journal
  yields `Unknown` axes plus `availability=unavailable` and a bounded reason. A broken channel
  freezes knowledge; it never spends stall evidence.
- **Pane readings are advisory.** They fill the `Turn` axis alone and are stamped
  `source=pane_advisory`. Readiness answers whether a pane will accept input, not whether the head
  behind it may be stopped.

## Sources today

| Source | Producer wrapped | Axes answered |
|---|---|---|
| `pid_heartbeat` | `dispatcher_watchdog.head_process_status` | Process |
| `provider_cursor` | `dispatcher_tui.provider_progress_for_run` | Progress |
| `pane_advisory` | pane readiness (`{"idle": bool}`, from `pane_host.Pane`) | Turn |

Mappings worth naming:

- heartbeat `live-match` with `/proc` state `T` → `Process=Suspended`; dead or zombie → `Dead`;
  live otherwise → `Running`; missing/unreadable/mismatched → `Unknown` + unavailable;
- cursor moved since this run's previous snapshot → `Advancing`; unchanged → `Quiet`; unadmitted,
  foreign or unreadable → `Unknown` + unavailable; first observation of a source records its cursor
  without a progress opinion;
- pane ready (`idle`) → `Turn=Idle`; busy → `Active`; unanswerable → `Unknown`.

Snapshots are frozen dataclasses with `to_json`/`from_json`; a payload whose version or axis names
fall outside the vocabulary raises instead of being silently normalised, because damaged evidence
must stay visible rather than be read back as words nobody wrote.

## Episodes: the verdict ladder

The persisted `VitalityEpisode` (`src/secretary/dispatch/head_vitality_episode.py`) is the
hysteresis layer from the plan's "Vitality reducer": `reduce_vitality(previous, snapshots, now,
thresholds)` folds one tick's snapshots for a run into a durable conclusion.

```text
HealthyActive   a non-advisory source showed advancement now
HealthyQuiet    alive, no advancement yet, below every threshold
Suspended       /proc has the process parked on a stop signal (NOT a stall)
SuspectedStall  strong quiet outlived suspect_after
ConfirmedStall  strong quiet additionally outlived confirm_after
Dead            the heartbeat names a gone or unreaped process
Unverifiable    no strong channel answered; nothing may be concluded
```

Rules the reducer enforces (each pinned by a named test in `tests/test_head_vitality_episode.py`):

- Snapshots naming another run are dropped and noted in `basis`; when every snapshot agrees on a
  new run id, that identity change starts a fresh episode instead of splicing new facts into an
  old head's history.
- `Dead` outranks everything. `Suspended` is its own verdict and freezes the stall clocks:
  suspended time feeds no threshold, and quiet references shift past the frozen span on resume.
- Advancement from any non-advisory source ends a suspected or confirmed episode immediately,
  resets the phase timestamps, stamps `last_progress_at`/`last_progress_source`, and bumps
  `activity_epoch`.
- An unavailable source freezes its evidence: it never counts as no progress, never advances a
  stall timer, and is tracked in `unavailable_since` until it answers again. When every strong
  source (pid + provider) is dark the verdict is `Unverifiable` — except that an already-confirmed
  episode is not laundered back to health by its observers going blind.
- Advisory pane readings corroborate in `basis` only; they can never drive a stall verdict.
- Quiet accumulates from `last_progress_at` (or episode start), not from the observing tick: ticks
  are irregular, the quiet they sample is not. The ladder climbs HealthyQuiet → SuspectedStall →
  ConfirmedStall as the reference age crosses `suspect_after` and then `confirm_after`.
- Confirmation is sticky: one more quiet tick does not bounce it back. Only real progress,
  suspension, death, or an identity change ends a confirmed episode.
- The reducer is pure and deterministic: same inputs, same episode, no I/O, no clock. The caller
  owns `now`.

### Pid-only evidence ages (the issue 656 decision)

S1-2 left one branch open: what an episode concludes when its *only* strong witness is the pid
heartbeat — Running, with no progress source ever heard from. S1-3 settled it (pinned by
`test_pid_only_running_with_no_progress_for_hours_confirms_the_stall`), separating two cases:

- **A progress source this episode has witnessed** — it left a cursor, or sits dark in
  `unavailable_since` — keeps the freeze semantics unchanged. Its silence is unavailability:
  `Unavailable != no progress`, so a broken channel must never age a live head toward its death.
- **No progress source has ever answered**: the pid is the only witness, and bare process
  existence is not proof of liveness (`issue:06dcf6cb`). The pid's sustained answer of "running,
  and nothing else" ages HealthyQuiet → SuspectedStall at `suspect_after` → ConfirmedStall at
  `suspect_after + confirm_after`, measured from `started_at` (no advancement was ever seen). The
  absent provider contributes no vote of its own: it is neither progress (the reference never
  moves) nor quiet (no `quiet:<n>s` basis token names it).

Since S1-4 this is no longer shadow-only: the reduction is the wait tick's decision input, and
since S1-5 the recovery policy consumes the persisted episode (including a dark source's reason,
which rides on `episode.reason`) on the wait path and in the gate phase.
One reachability note for sizing the production risk: today's wait tick always attaches a
provider snapshot to the status it reduces — even `{"state": "unavailable"}` witnesses the
source — so the pid-only aging arm is reachable through the builders and a future policy, but
is not produced by the current wiring. A wired tick with an unadmitted provider lands in the
freeze arm instead; S1-4 must decide which arm each wired shape deserves.

## Regression table

Each historical incident is replayed tick by tick through the S1-1 snapshot builders fed with
producer payload dicts (the shapes `head_process_status`, `provider_progress_for_run` and pane
readiness put on the wire) and folded by the reducer in
`tests/test_head_vitality_regression.py`, asserting exactly when the ladder crosses each rung
under `DEFAULT_VITALITY_THRESHOLDS`. The asymmetry-of-cost principle behind every row: a false
"working" costs an idle hour; a false kill loses a live round.

| Incident | Test | Required verdict / behaviour |
|---|---|---|
| `issue:b5195041` (board 951, secretary-1420): idle ~380s ×3 against a live Codex transcript; nudge → respawn → Blocked on a working head | `IssueB5195041CodexTranscriptBlindnessTests` | provider Advancing ⇒ `HealthyActive`; advisory pane-idle alone never leaves `Unverifiable`; no destructive verdict while the transcript moved |
| `issue:3e7abdf9` (board 997, secretary-1423): wait-for-readiness timeout on a working head read as transport refusal; retained worker replaced | `Issue3e7abdf9BusyReadAsUnavailableTests` | busy pane + Running + Advancing ⇒ `HealthyActive`; readiness Unavailable is Turn-axis only and never stall evidence; provider unknown ⇒ `HealthyQuiet`, never Dead/ConfirmedStall |
| `issue:8f86ed63` (board 1010, secretary-1428): 11 busy-retry cycles in an hour, rollout frozen since 06:50, composer stale ("busy is readiness, not liveness") | `Issue8f86ed63BusyMasksStallTests` | Running + admitted Quiet over the hour ⇒ `SuspectedStall` at +300s, `ConfirmedStall` at +900s from last progress; the busy pane corroborates in `basis` only |
| `issue:fe04011b` (board 1156, codegen-orchestrator-1197): worker+child in `T (stopped)` 27 min, revived by SIGCONT; ticks wrote `gate-pending ok`, six-hour ceiling applied | `IssueFe04011bStoppedWorkerSixHourCeilingTests`; gate-phase twin: `IssueFe04011bLegacyGatePendingTests` | `/proc` state `T` ⇒ `Suspended` within one tick; stall clocks frozen for the whole stop; never ConfirmedStall, never Dead. Since S1-5 the gate-pending tick runs the same reduction + policy, so a suspended head gets its SIGCONT within one tick instead of waiting out `GATE_PENDING_STALL_SECONDS` |
| codegen-orchestrator-1194 (board card, sprint 1148): reviewer spawn failed 49 min, 45× identical deterministic `terminal_split_source_not_found` with a live terminal | `CodegenOrchestrator1194DeterministicSplitFailureTests` | not a vitality question: snapshot Unavailable with a deterministic reason keeps `Unverifiable` forever — but since S1-5 the recovery policy counts identical authoritative sightings and escalates to the operator at 3 (the flipped test drives the real reduction + policy through the incident's own timeline); a heuristic reason repeated just as often earns only observation |
| `issue:06dcf6cb` (board 656): umbrella contract — child-process existence ≠ liveness | `Issue06dcf6cbUmbrellaLivenessContractTests` | pid-only Running with no progress evidence ages ⇒ SuspectedStall ⇒ ConfirmedStall (see "Pid-only evidence ages" above) |

The legacy decision path itself is characterised in `tests/test_head_vitality_legacy_path.py`:
what the wait tick and gate do for b5195041, 3e7abdf9 and fe04011b. Since S1-4 the b5195041
characterisation is a REAL assertion (a transcript that advances every tick is never prompted,
never respawned); since S1-5 fe04011b's gate-phase characterisation is a REAL assertion too
(the pending-gate tick SIGCONTs a suspended worker within one tick). No `expectedFailure`
markers remain in the vitality suites.

### Thresholds

Defaults are comparability choices, not authority: `suspect_after = IDLE_STALL_DEFAULT`
(secretary-1063's five-minute readiness-idle window, where today's machinery first treats idle as
actionable) and `confirm_after = 2×IDLE_STALL_DEFAULT`, echoing the watchdog's principle that a
destructive-looking conclusion wants evidence separated in time. Both are far below the six-hour
worker-report ceiling whose uncritical application produced the incidents this sprint exists to
remove. A later policy card owns whatever the production numbers become.

### Shadow mode

S1-4 promoted the shadow reduction into the decision input (see "Decision path and destructive
guard" below); the historical contract it grew from is kept here because the promotion preserved
it. The worker/review wait tick computes and persists each role's episode (`worker_vitality_episode`,
`review_vitality_episode` on the dispatcher record) from values it already holds, and logs one
durable comment per verdict change (keyed on the transition itself, so a flapping verdict cannot
flood the card). A tick whose status carries none of the observed sources (the noop host, a runtime-unavailable
probe) runs no reduction and writes nothing — an episode is only ever the record of something
actually observed. A reduction failure degrades to "no episode" with a comment — the reduction must
never break the tick hosting it; the caller then decides as it would with no episode at all.

## Decision path and destructive guard

Since card S1-4 the wait tick's decision is the persisted episode's verdict — the pane-idle fence,
the `pid_confirmed and idle` branch and the pure clock ceilings on this path are gone. The
reduction runs on every wait tick (including not-live shapes: a heartbeat that names a gone
process reduces to `Dead`; a vanished pane over a live process is an observation failure that
waits). Verdict → action:

| Verdict | Wait-tick action |
|---|---|
| `HealthyActive`, `HealthyQuiet`, `Unverifiable` | `wait`. Fresh evidence of life renews the outer `worker_waiting_since` window; nothing is nudged or signalled. A recovered suspension also lands here, with the recovery ladder cleared. |
| `Suspended` | **The recovery policy owns this arm (S1-5)** — see the section below: one identity-fenced SIGCONT per suspension span, a response window, then operator escalation. Never a stop. |
| `SuspectedStall` | At most one idempotent report nudge per round generation (the existing `_prompt_worker_report` machinery), then visible degradation (`{kind}-stall-suspected`). A suspicion never destroys. |
| `ConfirmedStall` | The existing recovery path: one report prompt if the round has not spent it, else `_trigger_wait_watchdog` → respawn once → escalate to Blocked. Only from this verdict. |
| `Dead` | The existing not-live handling: reclaim via `_trigger_wait_watchdog`. |
| No episode / `Unverifiable`, ceiling elapsed | **Operator escalation, head untouched** (`_escalate_unobservable_wait`): one idempotent durable comment naming the evidence gap plus a degraded `{kind}-unobserved-wait-escalated` outcome. An unobservable wait is bounded by escalation, NOT by replacement — the guard refuses every destructive step for such a run, so the pre-S1-4 behaviour (reclaim on the clock alone) is gone on purpose. Before the ceilings speak, the recovery policy is consulted once more: an authoritative deterministic refusal on file escalates after N identical sightings instead of waiting out any ceiling (the 1194 class). |

### The guard

Every watchdog-driven destructive step passes through
`secretary.dispatch.head_vitality_guard.assert_destructive_allowed` before anything is stopped,
killed, respawned or replaced. Refusal classes: `missing-episode` (nothing was observed — a step
nobody observed acts on nobody's evidence), `foreign-run` (the episode names another HeadRun than
the one being acted on), `healthy-active`, `healthy-quiet`, `unverifiable`, `suspended`,
`suspected-stall`, and `pid-only-ceiling-unelapsed` (below). Allowed only for `ConfirmedStall`
and `Dead`.

**Belt-and-braces for the first production release:** a confirmation earned by the pid-only aging
arm (issue 656 — the provider source never answered at all) additionally requires the role's
ordinary outer ceiling (`WORKER_REPORT_STALL_DEFAULT` class) to have elapsed since the episode
began accumulating. Such a stall is therefore acted on strictly later than the old clock-only
machinery would have, never earlier. Confirmations earned on witnessed strong quiet are not held.
A raising reducer fails safe to `wait` + one comment.

A refusal produces a degraded `{kind}-guard-refused` outcome plus one idempotent durable comment
keyed on the wait-cycle token — never a silent no-op loop without telemetry.

- **Guarded entry points (watchdog-driven):**
  `dispatcher.DispatcherRuntime._trigger_wait_watchdog` — the verdict-driven recovery entry point;
  fences both its arms (`_respawn_wait`, `_escalate_wait`) through `_guard_or_wait`.
  The evidence-shaped branches of the no-episode fallback (`no output since launch`; `no terminal
  progress`) keep their pre-vitality triggers because they act on what a source actually said;
  their destructive steps run under `_trigger_wait_watchdog` and are fenced like every other.
  The pure clock branch of that same fallback no longer destroys at all: it escalates to the
  operator (see the verdict table).
  The confirmed-stop paths reached from these two (`_stop_worker_confirmed`,
  `_end_review_pane_confirmed`) run only underneath a guarded entry.

**Intentionally NOT guarded (legitimate without any episode):**

- Operator-initiated stops (`CommandHostRuntime.stop_head` invoked by an explicit operator
  command) — a human decided.
- Card-lifecycle stops: card Done/Blocked transitions, drain, the review bring-up's freeze of the
  worker (`_adopt_launch_intent`'s confirmed stop) — the lifecycle owns the head.
- Launch-recovery stops in `dispatcher_launch.resolve_launch_intent` (settling a launch whose tick
  died) — they act on durable launch intents and heartbeat identity, not vitality verdicts.

Call-site coverage lives in `tests/test_head_vitality_guard_sites.py`: each guarded path is driven
with the guard patched to refuse (no destructive host call may happen) and once through the real
guard (the step must happen); each unguarded stop asserts the guard symbol is never consulted.
Unit tests for every refusal class live in `tests/test_head_vitality_guard.py`.

## Recovery policy

Since card S1-5 the plan's third layer exists: `src/secretary/dispatch/head_vitality_policy.py`.
It consumes **only** a persisted `VitalityEpisode` — never raw signals, never a pane API — and
returns a `RecoveryDecision`: an intent from `RecoveryIntent`, the ladder rung it leaves the head
on, and structured detail for telemetry. It executes nothing; the dispatcher executes intents, and
the destructive guard of the previous section remains the last fence. The policy can never kill:
no input shape produces anything beyond `escalate_operator`, and a suspended process is alive by
the kernel's own word.

### The rung table (as implemented)

Rung state persists on the episode itself (`recovery_rung`, reserved since S1-2, plus the sibling
fields `recovery_span_started_at` and `deterministic_refusals`) through the record's existing
serialisation, so a dispatcher restart resumes the same rung instead of restarting the ladder:

| Rung | Verdict | Intent | Meaning |
|---|---|---|---|
| 0 | Healthy\*, Unverifiable, Dead | `observe` | Nothing earned. A suspension that recovered lands here too: the ladder clears with it, so a future span starts fresh. |
| 1 | `SuspectedStall` | `nudge` | The suspicion was seen; its single idempotent nudge is spent by the S1-4 wait-tick arm (`_prompt_worker_report`), unchanged. |
| 2→3 | `Suspended`, fresh span | `sigcont` | One identity-fenced SIGCONT per span (see below), then the response window opens. |
| 3 | `Suspended`, window running | `observe` | Inside the response window; the reduction flips the verdict to Healthy\*/Suspected the moment the head actually resumes. |
| 4 | `Suspended`, window expired | `escalate_operator` | One durable comment asking a human to look. Holds for the rest of the span (no re-firing). **Never kill** — the guard refuses destruction on this verdict regardless. |
| — | deterministic refusal ×N | `escalate_operator` | Skips the retry ladder entirely after `deterministic_refusal_limit` (3) identical authoritative sightings. |

A suspension **span** is identified by the reducer's own freeze stamp (`stall_frozen_since`): it
starts when the kernel first shows the process parked and clears when it runs again. Every
SIGCONT-rung decision keys on that stamp — within one span nothing re-fires, across spans the
ladder restarts, on recovery it resets to 0. This is what makes repeated identical observations
free and the policy safe to call from every tick, wait path and gate phase alike.

### The SIGCONT execution

`DispatcherRuntime._sigcont_head` is the only signal this path can send. Before signalling it
re-verifies identity at send time through `guard_head_run_identity` (pid + boot id + proc start
time + expected HeadRun, the same fence `_confirm_head_process_gone` uses): a mismatched,
unreadable or vanished heartbeat sends nothing — resuming somebody else's process group is worse
than leaving our own parked one parked one more tick. Delivery follows `_signal_head`'s group
rule (signal the head's own process group when it has one, else the pid). SIGCONT only, ever:
SIGTERM/SIGKILL stay behind their own guarded entries. The send is one durable comment naming the
span and the response window (`{kind}-vitality-sigcont`), idempotent via the span stamp in the
request id.

### The response window

Default five minutes (`SECRETARY_HEAD_SUSPENSION_RESPONSE_SECONDS`,
`SUSPENSION_RESPONSE_WINDOW_DEFAULT`). The scale is deliberate: it must outlast several tick
cadences so a resumed head gets multiple ticks to show life before the second rung fires; it must
stay far below the six-hour ceilings whose uncritical application produced fe04011b; and the
incident itself — 27 minutes unnoticed in `T` — bounds well under a window of this size while a
resuming head loses nothing. Expiry escalates to the operator and touches nothing.

### Gate-phase coverage

`_gate_pending` no longer watches only its clock (issue fe04011b). While CI sits non-terminal,
each pending tick runs the same vitality reduction + recovery policy for the worker head that the
report wait runs: `T` is seen within one tick and SIGCONT'd; an expired response window reaches
the operator in minutes. Any probe failure degrades to the ordinary gate behaviour — the gate
keeps working over an unobservable head exactly as before. `GATE_PENDING_STALL_SECONDS` stays as
the OUTER escalation ceiling for the CI rollup itself (Blocked for a human, non-destructive per
S1-4 semantics); it is simply no longer the first thing to notice a stopped process.
`WORKER_REPORT_STALL_DEFAULT` keeps the same role on the report-wait path: an outer ceiling for
unobservable heads, while observable ones are decided by evidence.

### Deterministic refusal reasons (the allowlist)

The plan: identical heuristic reasons N times are not evidence; only authoritative deterministic
classes may skip ranks. `DETERMINISTIC_TERMINAL_REASONS` is the explicit allowlist — what
qualifies is a refusal naming a property of THIS launch, which retrying cannot change: invalid
configuration, missing executable, authentication rejected, resource exhausted, and the incident's
own `terminal_split_source_not_found`. Matching is token-in-bounded-string against the diagnostic
the producer put on the snapshot (the reducer now carries a dark source's reason onto the episode).
After three identical sightings (`deterministic_refusal_limit`) the policy returns
`escalate_operator` and the dispatcher writes one comment and stops re-sending — minutes into what
was a 49-minute silent loop. Timing/availability/transport refusals deliberately do NOT qualify:
they repeat whenever their cause persists, and counting them would let one dark channel fast-track
a live head to escalation — the exact inversion of "Unavailable ≠ no progress". A changed attempt
(a tick where no deterministic reason appears) resets the count, as real progress ends a stall.

Tests: policy rungs and idempotency in `tests/test_head_vitality_policy.py`; real-process
execution (a genuinely SIGSTOPed child resumed once via SIGCONT alone, a foreign-identity
heartbeat signalled by nobody) in `tests/test_head_vitality_policy_execution.py`; wait-tick and
gate-phase ticks in `tests/test_head_vitality_wait_decisions.py` and
`tests/test_head_vitality_legacy_path.py`.

**What remains for Sprint 2+** (vocabulary exists, nothing wired): `request_drain`, safe
replacement on a quiescent boundary, same-profile respawn as a policy rung, runtime failover, and
`block` with evidence — all waiting on the HeadRuntime admission/drain/stop boundary. The
`ConfirmedStall` recovery path is unchanged in this sprint: respawn once, then escalate.

