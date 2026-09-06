# Changelog

Changes an operator or a caller has to know about: a command whose output moved, a protocol
document that gained or lost a field, a precondition that became stricter. Not a commit log —
the git history is that, and it is better at it. Newest first.

## 2026-09-06 — the sprint state protocol (secretary-1573, sprint:1431)

**New.** `secretary.webproto.sprint_reads.SprintReadLayer.sprint_list(statuses=…)`: every sprint of
the installation with what each one is doing, in one sprint-board pass, one Pipeline listing and one
read of the dispatcher's production state — whatever the number of sprints. Documented in
[PROTOCOLS](PROTOCOLS.md#listing-them-all); the operator scenario is in
[OPERATIONS](OPERATIONS.md#what-is-running-right-now).

**Two answers that were wrong are now right.**

- A closed or stopped sprint no longer reads as one that is working. Its current card is kept — it
  is where the sprint got to — and `work.current_task.live` is `false` with a reason saying the card
  is the record of a sprint that ended.
- A closed or stopped sprint's observer is no longer reported as `not_started` ("the sprint is saved
  and the production tick holds no observer for it yet"). `observer.launch.state` has a sixth value,
  `ended`: the tick stopped that head and dropped its record. Both defects were live on this
  installation on roughly sixty closed sprints.

**Changed output.** `secretary sprint list` and `secretary sprint status` are now clients of
`sprint_list` and `sprint_state` rather than readers of their own, so they print those documents:

- `sprint list` prints one `sprint_list` document instead of a bare JSON array of sprint rows. The
  rows are under `sprints.items`, each with the sections above beside the fields it had.
- `sprint status` prints one `sprint` document. What it used to print at the top level has moved:
  `cards` → `work.cards.states`, `degraded_cards` → `work.degraded_cards.items`,
  `resume_freshness` → `work.decision.freshness.value`, `current_task` → `sprint.value.current_task`
  (and `work.current_task`), `budget`, `product`, `issues`, `reservations` and `executors` →
  `sprint.value.*`. The full observer row is now `observer.launch.record`, which keeps `delivery`:
  the `delivery_id` / `through_event` pair an observer copies into its acknowledging resume is
  unchanged, at that path. `stop_reason` is no longer a field of its own; a stopped sprint says the
  same thing in `work.waiting.reason`.
- Both commands now refuse with the exit statuses `secretary web-read` uses: `2` for `not_found` and
  `validation`, `1` for `backend_unavailable`.
- **Stricter precondition:** both now resolve the installation the way every `webproto` operation
  does, so an instance config that does not validate is a `backend_unavailable` refusal rather than a
  read that proceeds from a board client alone.

`secretary sprint show` is unchanged.

**Source isolation in `work.waiting` and `work.checks`.** An answer one source has already given is
never shadowed by a different source that refused. A current card the Pipeline listing holds in
Blocked reports `blocked` with its `blocked_by`, sourced from `cards`, even when
`dispatcher/production-state.json` cannot be read; a card in Ready, Issues or Done reports `waiting`
from the board wherever the dispatcher has nothing to add. `unknown` is left for what only the
production state can settle — whether an active column really has a head behind it — and its reason
names the column the board did establish. `checks` answers `not_applicable` from the sprint row for
a sprint that has ended or has no current card, rather than under the production state's
availability. The order is documented in [PROTOCOLS](PROTOCOLS.md#what-a-sprint-is-doing) and held
by `tests/test_web_sprint_protocol.py::WaitingSourceIsolationTests`.

**Also changed.** `SprintReader.statuses` is now `list` + `linked_cards` + `status_views`, which is
the same call in three public pieces so a caller that must keep the two board reads apart can still
get exactly this view instead of deriving a second one. Its per-sprint view gained the `resume`
entry beside the freshness verdict, which `secretary status` reports too.
