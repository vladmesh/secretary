# Head vitality observations

The dispatcher's destructive history (secretary-1063, cards codegen-orchestrator-1194..1197) came
from collapsing one question — "is the head working?" — into a single boolean and then acting on it.
The head-vitality model splits that question into three independent axes, observed as pure data and
fused later with hysteresis. This page describes the observation vocabulary introduced in
`src/secretary/dispatch/head_vitality.py`; the reducer, recovery policy and runtime boundary that
consume it are later sprint work (see the plan "Head vitality, собственный runtime и постепенный
уход от Orca").

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

This is shadow-only until a policy card consumes episodes; nothing in today's watchdog reads it.
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
| `issue:fe04011b` (board 1156, codegen-orchestrator-1197): worker+child in `T (stopped)` 27 min, revived by SIGCONT; ticks wrote `gate-pending ok`, six-hour ceiling applied | `IssueFe04011bStoppedWorkerSixHourCeilingTests` | `/proc` state `T` ⇒ `Suspended` within one tick; stall clocks frozen for the whole stop; never ConfirmedStall, never Dead |
| codegen-orchestrator-1194 (board card, sprint 1148): reviewer spawn failed 49 min, 45× identical deterministic `terminal_split_source_not_found` with a live terminal | `CodegenOrchestrator1194DeterministicSplitFailureTests` | not a vitality question: snapshot Unavailable with a deterministic reason keeps `Unverifiable` forever; TODO(S1-5) test marks that such a reason must escalate fast inside one attempt |
| `issue:06dcf6cb` (board 656): umbrella contract — child-process existence ≠ liveness | `Issue06dcf6cbUmbrellaLivenessContractTests` | pid-only Running with no progress evidence ages ⇒ SuspectedStall ⇒ ConfirmedStall (see "Pid-only evidence ages" above) |

The legacy decision path itself is characterised in `tests/test_head_vitality_legacy_path.py`:
what the wait tick and gate do *today* for b5195041, 3e7abdf9 and fe04011b, with
`expectedFailure` marking the two behaviours that still contradict the plan (idle respawn without
provider evidence; suspension invisible behind a pending gate until six hours pass). S1-4 flips
those to real assertions when the watchdog switches onto episodes.

### Thresholds

Defaults are comparability choices, not authority: `suspect_after = IDLE_STALL_DEFAULT`
(secretary-1063's five-minute readiness-idle window, where today's machinery first treats idle as
actionable) and `confirm_after = 2×IDLE_STALL_DEFAULT`, echoing the watchdog's principle that a
destructive-looking conclusion wants evidence separated in time. Both are far below the six-hour
worker-report ceiling whose uncritical application produced the incidents this sprint exists to
remove. A later policy card owns whatever the production numbers become.

### Shadow mode

The worker/review wait tick computes and persists each role's episode (`worker_vitality_episode`,
`review_vitality_episode` on the dispatcher record) from values it already holds, and logs one
durable comment per verdict change (keyed on the transition itself, so a flapping verdict cannot
flood the card). **Nothing consults them**: no watchdog threshold, respawn,
stop or recovery path reads an episode. Shadow mode exists so the next card can compare the
reducer's verdicts against what the watchdog actually decided before any decision trusts them.
A tick whose status carries none of the observed sources (the noop host, a runtime-unavailable
probe) runs no reduction and writes nothing — an episode is only ever the record of something
actually observed. A reduction failure degrades to "no episode" with a comment — shadow code must
never break the tick hosting it.

