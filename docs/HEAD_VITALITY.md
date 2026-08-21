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
