# Changelog

Changes an operator or a caller has to know about: a command whose output moved, a protocol
document that gained or lost a field, a precondition that became stricter. Not a commit log —
the git history is that, and it is better at it. Newest first.

## 2026-09-06 — one place enforces source isolation (secretary-1574, sprint:1431)

**The rule, and where it now lives.** *A source that refused, or that was never read, may not delete,
shadow or fabricate an answer another source already gave; every section says which source answered
it.* It is enforced in one place, `secretary.webproto.section`, which every section of every sprint
document is assembled through: a rule that needs a source that did not answer is not run, a refusal
can only produce the section's declared no-claim shape, and a section assembled anywhere else cannot
reach a document. Documented in [PROTOCOLS](PROTOCOLS.md#one-place-says-which-source-answered).

**Corrected: the CLI precondition of the previous entry.** `secretary sprint list` and
`secretary sprint status` answer from the board again when `instance.yaml` does not validate, as long
as the transport is usable and `--data-dir` is explicit. The unvalidated config is reported as an
unavailable `installation` source in the document rather than as a refusal of the operation; only a
caller that gave no data directory still exits `1` with `backend_unavailable`. The "stricter
precondition" recorded in the entry below was wrong and is struck there: a caller must not lose an
answer it has, and "the config could not be validated" is one more source that refused. No rule about
sprint state moved back into `sprint_commands.py`.

**New document fields.**

- Every `source` object now carries `name` — which source answered that section (`installation`,
  `sprints`, `cards`, `journal`, `liveness`; on the catalogue, `catalogue`, `registry`, `heads`).
  Two available sources are otherwise indistinguishable, so without it "the section names its
  source" held only for the ones that failed.
- Both documents carry `journal.source` and `installation.source` beside `cards.source` and
  `liveness.source`, and `sprint status` now carries all four. The committed audit journal is a
  source of its own rather than part of the sprint board.
- `observer.declared` gained a `source` and a fourth state, `unknown`.

**Changed answers.**

- An unreadable `board/events.ndjson` marks `decision.freshness` unavailable and nothing else. It
  used to blank the whole sprint listing — `sprints.items: []` and `sprint.value: null` — attributed
  to a sprint-board failure that had not happened.
- An unreadable sprint board no longer produces an observer claim: `observer.declared` is `unknown`
  rather than `absent`, and `observer.launch` is `unavailable` sourced from the sprint board rather
  than `not_started` sourced from an available production state.
- A section that could not be answered now reports `null` where it used to report an empty list:
  `sprints.items` in the listing, and `products`, `issues`, `projects` and `heads` items in the
  sprint form's catalogue. An empty list under an `unavailable` source claimed the installation had
  none, which is the opposite of not knowing.
- With both the Pipeline listing and the production state unreadable, `work.waiting` is `unknown`
  sourced from `cards` rather than from `liveness`: a section that cannot answer names the first
  input the chain was missing.

**Also changed.** `SprintReader.status_views` takes an optional `audit` traversal, so a caller that
has already walked the committed journal — and has to keep that walk's failure apart from the
board's — hands it in and the call opens nothing. `secretary.sprints.audit_traversal` builds one.

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
- ~~**Stricter precondition:** both now resolve the installation the way every `webproto` operation
  does, so an instance config that does not validate is a `backend_unavailable` refusal rather than a
  read that proceeds from a board client alone.~~ **Withdrawn the same day, before this reached an
  operator** — see the entry above. It was never right: it lost a caller an answer it had, and
  documenting it did not preserve it.

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
