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

