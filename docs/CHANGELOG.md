# Changelog

Changes an operator or a caller has to know about: a command whose output moved, a protocol
document that gained or lost a field, a precondition that became stricter. Not a commit log —
the git history is that, and it is better at it. Newest first.

## 2026-09-06 — the pause is reachable as a protocol operation, and its scope is readable first (secretary-1576, sprint:1431)

**Two new protocol operations and two new reads.** `pause_drain(actor, reason)` sets the pipeline-wide
soft pause; `pause_resume(actor)` lifts whatever pause is set and reports what it put back;
`pause_state()` is the pause state read; `pause_scope()` answers what a pause command would reach
*before* it is issued. Documents are `kind: pause_command`, `pause_state` and `pause_scope`, published
as the `web-pause` schema. Every rule stays in `secretary.dispatcher_pause_ops` — the tick lock, the
same-mode no-op, the `pause_conflict` refusal, the head stop and relaunch, the legacy mirror and the
auto-resume TTL — and no second flag, lock or store was added. See
[PROTOCOLS](PROTOCOLS.md#the-pause-as-protocol-operations) and
[OPERATIONS](OPERATIONS.md#read-the-scope-first-then-decide).

**New command.** `secretary pause-scope --instance INSTANCE` prints the scope read: that the pause is
pipeline-wide, which dispatcher and which files a command would write, which sprints are open, every
card on the Pipeline board with the sprint that holds it (`null` where none does), which heads are
running, and — separately — what a drain does not stop versus what a freeze would. It is a read: no
flag, no lock, no head, no wake, no write. Product and Issue records are not listed as cards: such a
record never takes a claim, so a pause reaches none of them.

**`secretary pause-status` output has changed shape.** It is now a client of `pause_state` and prints
that document. The fields moved into sections that each name the source that answered them: `paused`,
`mode`, `since`, `actor`, `stopped_worker`/`stopped_reviewer`/`stopped_observer`, `on_resume` and
`auto_resume` are under `state`; the per-card head lines are `heads.cards` and the sprint observers
are `heads.observers`; the flag path is `target.pause_file`. The pause's own reason is `state.pause_reason`,
so it is never confused with the `reason` a refused source carries. `secretary backup create` reads the
new shape and treats a pause state it could not establish as paused.

**`secretary pause drain` and `secretary resume` output has changed shape too.** They print the
`pause_command` document: what the command did (`action` is `paused`, `noop` or `resumed`, with
`changed` as the boolean), the `restored` lists for a resume, any warnings, and the pause state read
inside the same answer. Exit statuses are unchanged: a `pause_conflict` is `owner_conflict` and still
exits `3`, a validation refusal `2`, an unavailable backend `1`.

**`secretary pause freeze` is untouched.** There is deliberately no freeze operation in the layer:
`pause_drain` takes no mode, and no default, fallback, retry or convenience path turns a request for a
soft pause into a freeze. The freeze command keeps the implementation and the output it had, and the
existing refusal to change mode while paused is preserved.

**A config that does not validate keeps exiting 2.** The pause commands reached the dispatcher
through `runtime_from_args`, whose `invalid_instance` exits 2, so the layer's typed code for it is
`validation` and the status is unchanged on all four commands. In the other direction: with an
explicit `--data-dir`, `pause-status` and `pause-scope` now answer from the flag and the dispatcher
state and report `installation` as an unavailable source, where the old path refused — a caller that
supplied the missing information gains an answer, and no refusal changed its status.

**A durable file that parses but cannot be converted is an unavailable source, not an exception.** A
pause flag whose `stopped_worker` is a number, or a production record whose `attempt_round` is not an
integer, marks its source unavailable and leaves every other section standing. The set of failures
that means "this source could not answer" now lives in one place both the pause and the sprint reads
import, `secretary.webproto.sources.SOURCE_FAILURES`.

**Nothing says a soft pause stops a head, or that a pause is per sprint.** Every document carries an
`extent` object stating that the pause is one pipeline-wide flag with no per-sprint form — including a
document where every source refused — and a `modes` object stating what a drain does not stop and what
a freeze would. A drain leaves the `stopped_*` lists empty and the running heads reported as running.

## 2026-09-06 — a PO comment is saved, identified, and honestly reported as delivered (secretary-1575, sprint:1431)

**Two new protocol operations.** `sprint_comment(request_id, actor, reference, body, role)` puts one
comment on a sprint entity and answers with `kind: sprint_comment`: the durable `comment_id`, whether
this call `saved` it or found it already saved, and the delivery document below.
`sprint_comment_delivery(ref, comment_id)` reads what happened to a saved comment
(`kind: sprint_comment_delivery`). Both are in
[PROTOCOLS](PROTOCOLS.md#commenting-on-a-running-sprint); the operator scenario is in
[OPERATIONS](OPERATIONS.md#a-po-comment-on-a-running-sprint).

**`secretary sprint comment` output has changed shape.** It is now a client of `sprint_comment` and
prints that operation's document (`kind`, `request_id`, `ref`, `comment_id`, `saved`, `delivery`)
instead of the writer's `{"action": "commented", "sprint": …, "event_id": …}`. `event_id` is now
`comment_id`, and the whole sprint record is no longer inlined — read it with `sprint status` or
`sprint show`. Refusals are typed protocol codes and map to the exit statuses `web-read` uses
(`not_found`/`validation` → 2, `owner_conflict` → 3, `backend_unavailable` → 1), which is the same
table `secretary web-run` uses and leaves a comment on a closed or stopped sprint exiting `3` as it
did. What `SprintWriter.comment` decides is unchanged.

**New command.** `secretary sprint comment-delivery --ref sprint:ID --comment-id evt_…` prints the
delivery document. It is a read: no wake, no nudge, no retry, no head launch, no write to the
dispatcher's state.

**`--request-id` on a comment is now the retry handle in the full sense.** A repeat with the same id
writes no second comment, appends no second audit event, and causes no second observer wake or head
launch — that is `SprintWriter._write`'s existing audit claim, and no second request index was added
beside it. New: a repeat that reuses an id over a *different* body, sprint, role or actor is refused
with `validation` instead of being answered with the first comment's result.

**Delivery is not acceptance, and the documents say so.** `delivery.state` is one of `saved`,
`waiting`, `handed_over`, `error` and `unknown`, derived only from the dispatcher's existing cursors;
`unknown` is never folded into another answer. Every delivery document carries an `acceptance` object
that is always `established: false` and names `issue:cf5c9f03ee0f92d3d347` as the deferred mechanism.
Nothing in the operations, the schema, the CLI output or the documentation states or implies that the
observer read, accepted or took a comment into account.

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
