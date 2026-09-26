"""Decision and operation cards: the dispatcher hands them to the sprint's PO session (secretary-1758).

A `decision` or `operation` card is executed by the PO service, not by a head. When the dispatcher
claims one it cuts no workspace and launches nothing: it resolves the sprint's PO session
(`PoService.sprint_session`) and submits one input to it (`PoService.submit`, `source: dispatcher`)
carrying the card, the sprint's comments and the exact `task complete` command. The PO answers it in
that turn and completes the card itself; its Done wakes the sprint observer like any other.

Every request id is derived at claim from the card ref and the claim attempt and kept on the card's
dispatcher record (`PoSubmission`), so a resolve or submit the service did not answer is repeated
next tick under the same id: a fresh one could open a second PO session. A service that stays down
leaves the card In progress and the tick degraded; it is not a failure of the card.

After the submit the per-tick check reads the PO store, never the service: `po_requests` names the
turn the input became once the service claimed it. A turn that settled while the card is still In
progress Blocks the card; anything else waits, since the input may be queued behind a seed or
another turn. An input the service set aside in `po-queue/refused/` will never become a turn, so it
Blocks the card with the service's reason.

Every input carries the card's facts beside its text (`card_ref`, `kind`, `touches_production`,
`sprint_ref`; secretary-1764), frozen with the text since the service binds the submit id to both. The
service, and only the service, evaluates the sprint's production rights on them, and refuses nothing by
them (secretary-1769): an operation card's input is queued as a normal turn with the service's rights
section after its text. A production the sprint does not allow is the PO's to decide in that turn:
it records the allowance (`sprint allow-production`) and runs the operation, or hands the card to the
owner like any other.

The PO may hand the card to the owner inside its turn (`task handover`, secretary-1761). A card that
carries that mark is not Blocked when the turn settles: it waits for the owner. Each owner comment on
it after the handover becomes one follow-up input to the same PO session, carrying the reason, the
owner's comments since the handover in order and the completion command, under a request id derived
from the card ref and that comment's event id. The PO completes the card with `task complete`,
which takes the mark off.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Protocol

from secretary.board.completion_evidence import missing_completion_evidence
from secretary.board.owner_handover import (
    owner_answer_event_ids,
    owner_comments_since_handover,
    waiting_owner,
)
from secretary.board.production_rights import (
    CARD_INPUT,
    NO_PRODUCTION,
    OPERATION_KIND,
    OWNER_ANSWER_INPUT,
    card_facts,
    touches_production,
)
from secretary.board.terminal_taxonomy import normalize_terminal_taxonomy
from secretary.dispatch.helpers import _worker_id
from secretary.dispatch.state import (
    DispatcherRecord,
    PoSubmission,
    request_token,
)
from secretary.dispatch.state import (
    attempt_request_id as _attempt_request_id,
)
from secretary.dispatch.state import (
    new_attempt_id as _new_attempt_id,
)
from secretary.dispatch.state import (
    record_attempt as _record_attempt,
)
from secretary.po.client import OutcomeUnknown, PoServiceError, ServiceRefused, ServiceUnavailable
from secretary.po.queue import QueuedInput
from secretary.po.store import FAILED, INTERRUPTED, RUNNING, PoRequest, PoStoreError, RequestConflict, Turn

#: The source the PO service records for a dispatcher input (`secretary.po.queue.SOURCES`).
DISPATCHER_SOURCE = "dispatcher"
#: Dispatcher record states of a PO-executed card.
PO_SUBMITTING = "po_submitting"
PO_SUBMITTED = "po_submitted"
#: Request-id actions, each under `attempt_request_id(<claim attempt>, <action>, <card ref>)`.
PO_SESSION_ACTION = "po-session"
PO_SUBMIT_ACTION = "po-submit"
PO_COMPLETE_ACTION = "po-complete"
PO_HANDOVER_ACTION = "po-handover"
PO_BLOCKED_ACTION = "po-card-blocked"
#: The follow-up input carrying the owner's answer: `dispatcher-po-owner-answer-<card>-<event id>`.
PO_OWNER_ANSWER_ACTION = "po-owner-answer"
#: Service refusal codes that say nothing about the request: it is repeated, never failed.
_UNANSWERED_CODES = frozenset({"unavailable", "outcome_unknown"})


class PoChannel(Protocol):
    """What the dispatcher needs of the PO service and its store."""

    def sprint_session(self, *, sprint_ref: str, request_id: str) -> dict[str, Any]: ...

    def submit(
        self, *, session_id: str, text: str, request_id: str, source: str, card: dict[str, Any]
    ) -> dict[str, Any]: ...

    def request(self, request_id: str) -> PoRequest | None: ...

    def turn(self, session_id: str, seq: int) -> Turn: ...

    def queued(self, request_id: str) -> QueuedInput | None: ...

    def refused(self, request_id: str) -> dict[str, Any] | None: ...


class ServicePoChannel:
    """The installation's PO service socket and PO store. Construction does no I/O."""

    def __init__(self, data_dir: Path | str, instance_dir: Path | str | None) -> None:
        self.data_dir = Path(data_dir)
        self.instance_dir = instance_dir
        self._client: Any = None
        self._store: Any = None

    def _service(self) -> Any:
        if self._client is None:
            from secretary.po.client import PoServiceClient

            self._client = PoServiceClient(self.data_dir)
        return self._client

    def _po_store(self) -> Any:
        if self._store is None:
            if self.instance_dir is None:
                raise PoStoreError("the dispatcher names no instance, so it has no PO store to read")
            from secretary.po.store import PoStore

            try:
                self._store = PoStore.for_instance(self.instance_dir)
            except Exception as exc:  # credentials that cannot be read are an unanswered store
                raise PoStoreError(f"the PO store is not available: {type(exc).__name__}: {exc}") from exc
        return self._store

    def sprint_session(self, *, sprint_ref: str, request_id: str) -> dict[str, Any]:
        return self._service().sprint_session(sprint_ref=sprint_ref, request_id=request_id)

    def submit(
        self, *, session_id: str, text: str, request_id: str, source: str, card: dict[str, Any]
    ) -> dict[str, Any]:
        return self._service().submit(
            session_id=session_id, text=text, request_id=request_id, source=source, card=card
        )

    def request(self, request_id: str) -> PoRequest | None:
        return self._po_store().request(request_id)

    def turn(self, session_id: str, seq: int) -> Turn:
        return self._po_store().turn(session_id, seq)

    def _queue(self) -> Any:
        from secretary.po.queue import PoQueue

        return PoQueue(self.data_dir)

    def queued(self, request_id: str) -> QueuedInput | None:
        """The input still waiting in the service's queue under `request_id`, read from its directory."""
        from secretary.po.queue import QueueError

        try:
            return self._queue().find(request_id)
        except QueueError as exc:
            raise PoStoreError(str(exc)) from exc

    def refused(self, request_id: str) -> dict[str, Any] | None:
        """The input the service set aside in `refused/` under `request_id`, with its `reason`."""
        from secretary.po.queue import QueueError

        try:
            return self._queue().find_refused(request_id)
        except QueueError as exc:
            raise PoStoreError(str(exc)) from exc


def complete_command(reference: str, kind: str, request_id: str) -> str:
    """The exact command the PO runs to complete the card, as its input quotes it."""
    return (
        f"python3 -P -m secretary task complete --ref {reference} --role po --kind {kind} "
        f"--body-file <file> --request-id {request_id}"
    )


def handover_command(reference: str, request_id: str) -> str:
    """The exact command the PO runs to hand the card to the owner instead of completing it."""
    command = (
        f"python3 -P -m secretary task handover --ref {reference} --role po --to owner --reason-file <file>"
    )
    return f"{command} --request-id {request_id}" if request_id else command


def completion_sections(kind: str) -> tuple[str, str]:
    from secretary.board.completion_evidence import PO_COMPLETION_SECTIONS

    first, second = PO_COMPLETION_SECTIONS[kind]
    return first, second


def render_po_card_input(task: dict[str, Any], sprint: dict[str, Any], submission: PoSubmission) -> str:
    """The one input a decision/operation card becomes in its sprint's PO session."""
    reference = str(task.get("ref") or "")
    kind = submission.kind
    first, second = completion_sections(kind)
    lines = [
        (
            f"The dispatcher hands you {kind} card {reference} of {submission.sprint_ref}. Answer it in "
            "this turn and complete the card before the turn ends; a turn that ends with the card still "
            "In progress Blocks it, unless you handed it to the owner in this turn."
        ),
        "",
        f"Card: {reference} ({kind}): {task.get('title') or ''}",
        *_production_lines(submission),
        "",
        "## Card body",
        "",
        str(task.get("description") or "").strip() or "(empty)",
        "",
        f"## Comments of {submission.sprint_ref}, in board order",
        "",
    ]
    comments = [comment for comment in sprint.get("comments") or [] if isinstance(comment, dict)]
    if not comments:
        lines.append("(none)")
    for comment in comments:
        lines += [
            f"### {comment.get('created_at') or 'undated'}",
            "",
            str(comment.get("body") or "").rstrip(),
            "",
        ]
    lines += [
        "",
        "## Complete the card",
        "",
        (
            f"Write a body file with two non-empty sections, `## {first}` and `## {second}` (a command "
            "or an observation someone can repeat), then run exactly:"
        ),
        "",
        "    " + complete_command(reference, kind, submission.complete_request_id),
        "",
        "## Or hand it to the owner",
        "",
        (
            "Only when a person is needed: money, a key or access only the owner holds, or a product "
            "decision that is the owner's. An architecture fork is yours to decide. Write the reason "
            "(what the owner has to decide or do) to a file, run exactly this and end the turn; the card "
            "stays In progress and waits, and the owner's answer comes back to this session:"
        ),
        "",
        "    " + handover_command(reference, submission.handover_request_id),
        "",
        "Keep the turn short; anything long-running becomes a card.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _production_lines(submission: PoSubmission, *, owner_answer: bool = False) -> list[str]:
    """What an operation card's input says about the production it touches; nothing for a decision.

    The card's own input points at the PO service's rights section, which the service adds after the
    text: only the service evaluates the rule. The owner's answer to a handed-over card is not
    evaluated again, so its line says the owner decided.
    """
    production = submission.card.get("touches_production")
    if submission.kind != OPERATION_KIND or not production:
        return []
    if production == NO_PRODUCTION:
        return [f"Touches production: {NO_PRODUCTION}. Touch no production in this turn."]
    if owner_answer:
        return [
            (
                f"Touches production: {production}. You handed the card to the owner, and the owner "
                "decided on it in the answer below; touch no other production in this turn."
            )
        ]
    return [
        (
            f"Touches production: {production}. Whether the sprint allows it is in the PO service's "
            "production rights section at the end of this input; touch no other production in this turn."
        )
    ]


def po_card_facts(task: dict[str, Any], submission: PoSubmission, *, input: str = CARD_INPUT) -> dict[str, Any]:
    """The facts an input about this card carries beside its text (`PoService.submit`)."""
    return card_facts(
        card_ref=str(task.get("ref") or ""),
        kind=submission.kind,
        touches_production=touches_production(task),
        sprint_ref=submission.sprint_ref,
        input=input,
    )


def owner_answer_request_id(reference: str, event_id: str) -> str:
    """The follow-up input's request id: the card ref and the owner comment's event id, nothing else."""
    return "-".join(request_token(part) for part in ("dispatcher", PO_OWNER_ANSWER_ACTION, reference, event_id))


def render_owner_answer_input(
    task: dict[str, Any], submission: PoSubmission, mark: dict[str, str], answers: list[dict[str, str]]
) -> str:
    """The follow-up input the owner's answer on a handed-over card becomes in the same PO session."""
    reference = str(task.get("ref") or "")
    kind = submission.kind
    first, second = completion_sections(kind)
    lines = [
        (
            f"The owner answered {kind} card {reference} of {submission.sprint_ref}, which you handed to "
            f"the owner on {mark['since']}. Complete the card in this turn if the answer settles it. If "
            "it does not, say on the card what is still missing and end the turn: the card keeps waiting "
            "for the owner and is not Blocked."
        ),
        "",
        f"Card: {reference} ({kind}): {task.get('title') or ''}",
        *_production_lines(submission, owner_answer=True),
        "",
        "## Why you handed it to the owner",
        "",
        mark["reason"],
        "",
        "## The owner's comments since the handover, in order",
        "",
    ]
    for answer in answers:
        lines += [f"### {answer['created_at'] or 'undated'}", "", answer["body"] or "(empty)", ""]
    lines += [
        "",
        "## Complete the card",
        "",
        (
            f"Write a body file with two non-empty sections, `## {first}` and `## {second}` (a command "
            "or an observation someone can repeat), then run exactly:"
        ),
        "",
        "    " + complete_command(reference, kind, submission.complete_request_id),
        "",
        "Keep the turn short; anything long-running becomes a card.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def claim_po_card(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """Claim a Ready decision/operation card and submit it; no workspace, no head."""
    ref = task["ref"]
    claim_id = _attempt_request_id(attempt_id, "claim", ref)
    if records.get(ref) is not None or runtime.audit.committed_event(claim_id) is not None:
        # A card back in Ready (after a Blocked, say) is a new attempt with new request ids, so the
        # previous attempt's input is never replayed into this one.
        attempt_id = _new_attempt_id()
        _record_attempt(payload, attempt_id, ref, runtime.owner, runtime.owner)
        payload["attempt_id"] = attempt_id
        claim_id = _attempt_request_id(attempt_id, "claim", ref)
    runtime.writer.claim(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        worker=_worker_id(task),
        request_id=claim_id,
    )
    claimed = runtime.reader.show(ref)
    record = _po_record(claimed, attempt_id)
    records[ref] = record
    runtime.save_records(payload, records)
    if not record.po_submission.sprint_ref:
        return _block(
            runtime,
            claimed,
            records,
            payload,
            attempt_id,
            f"a {record.po_submission.kind} card names no sprint, so there is no PO session to execute it",
        )
    return advance_po_card(runtime, claimed, records, payload, attempt_id)


def _po_record(task: dict[str, Any], attempt_id: str, *, worker: str = "") -> DispatcherRecord:
    ref = task["ref"]
    return DispatcherRecord(
        worker=worker or _worker_id(task),
        workspace="",
        handle="",
        head="",
        review_head="",
        attempt_id=attempt_id,
        comment_baseline=len(task.get("comments") or []),
        review_baseline=0,
        state=PO_SUBMITTING,
        claimed_at=time.time(),
        po_submission=PoSubmission(
            kind=str(task.get("type") or ""),
            sprint_ref=str(task.get("sprint") or ""),
            session_request_id=_attempt_request_id(attempt_id, PO_SESSION_ACTION, ref),
            submit_request_id=_attempt_request_id(attempt_id, PO_SUBMIT_ACTION, ref),
            complete_request_id=_attempt_request_id(attempt_id, PO_COMPLETE_ACTION, ref),
            handover_request_id=_attempt_request_id(attempt_id, PO_HANDOVER_ACTION, ref),
        ),
    )


def _recovered_record(runtime: Any, task: dict[str, Any]) -> DispatcherRecord | None:
    """Rebuild a lost record from the dispatcher's own claim of the card, or None when there is none.

    The claim's request id carries the claim attempt, and every other id is derived from it, so the
    rebuilt record repeats exactly the requests the lost one made.
    """
    ref = task["ref"]
    prefix, suffix = "dispatcher-", f"-claim-{request_token(ref)}"
    attempt = ""
    for event in runtime.audit.events(ref):
        request_id = str(event.get("request_id") or "")
        if (
            request_id.startswith(prefix)
            and request_id.endswith(suffix)
            and len(request_id) > len(prefix + suffix)
        ):
            attempt = request_id[len(prefix) : -len(suffix)]
    if not attempt:
        return None
    worker = str((task.get("claim") or {}).get("worker") or "")
    return _po_record(task, attempt, worker=worker)


def advance_po_card(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """One tick of a decision/operation card, whatever column it stands in."""
    ref = task["ref"]
    state = str(task.get("state") or "")
    if state == "ready":
        return claim_po_card(runtime, task, records, payload, attempt_id)
    if state != "in_progress":
        record = records.pop(ref, None)
        if record is not None:
            runtime.save_records(payload, records)
        return _closed(task, attempt_id, record)
    record = records.get(ref)
    if record is None or not record.po_submission:
        record = _recovered_record(runtime, task)
        if record is None:
            return _block(
                runtime,
                task,
                records,
                payload,
                attempt_id,
                "the card is In progress but the dispatcher never claimed it, so it was never submitted "
                "to the PO; move it back to Ready to submit it",
            )
        records[ref] = record
        runtime.save_records(payload, records)
    if not record.po_submission.submitted:
        return _submit(runtime, task, record, records, payload)
    if waiting_owner(task) is not None:
        return _await_owner(runtime, task, record, records, payload)
    return _settle(runtime, task, record, records, payload)


def _submit(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    ref = task["ref"]
    submission = record.po_submission
    step = "resolve"
    try:
        if not submission.session_id:
            answer = runtime.po.sprint_session(
                sprint_ref=submission.sprint_ref, request_id=submission.session_request_id
            )
            submission.session_id = str(answer["session_id"])
            submission.session_outcome = "created" if answer.get("created") else "recorded"
            runtime.save_records(payload, records)
        step = "submit"
        if not submission.card:
            submission.card = po_card_facts(task, submission)
            runtime.save_records(payload, records)
        if not submission.text:
            sprint = runtime.sprints.show(submission.sprint_ref, include_resume_freshness=False)
            submission.text = render_po_card_input(task, sprint, submission)
            runtime.save_records(payload, records)
        answer = runtime.po.submit(
            session_id=submission.session_id,
            text=submission.text,
            request_id=submission.submit_request_id,
            source=DISPATCHER_SOURCE,
            card=submission.card,
        )
    except (ServiceUnavailable, OutcomeUnknown) as exc:
        return _unanswered(runtime, task, record, records, payload, step, exc)
    except ServiceRefused as exc:
        if exc.code in _UNANSWERED_CODES:
            return _unanswered(runtime, task, record, records, payload, step, exc)
        return _refused(runtime, task, record, records, payload, step, exc)
    except RequestConflict as exc:
        # The submit id is this attempt's own: a conflict says it already carries an input, which
        # is the one this record composed before it was lost and rebuilt (from the card and sprint as
        # they are now, so its text may differ). Anything else is refused.
        answer = (
            _already_submitted(runtime, submission.submit_request_id, submission.session_id)
            if step == "submit"
            else None
        )
        if answer is None:
            return _refused(runtime, task, record, records, payload, step, exc)
    except (PoServiceError, PoStoreError) as exc:
        return _refused(runtime, task, record, records, payload, step, exc)
    submission.submitted = True
    seq = answer.get("seq")
    submission.seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
    submission.unanswered = 0
    submission.last_error = ""
    record.state = PO_SUBMITTED
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "po-card",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": "po-card-submitted",
        "po_session": submission.session_id,
        "po_session_outcome": submission.session_outcome,
        "po_request_id": submission.submit_request_id,
        "seq": submission.seq,
    }


def _already_submitted(runtime: Any, request_id: str, session_id: str) -> dict[str, Any] | None:
    """What a request id this record owns already is at the service, or None when it is not ours.

    A turn in `po_requests`, or an input still pending in the queue for the same session: both are
    the input this record submitted before it lost its answer, whatever the text says now.
    """
    try:
        known: PoRequest | None = runtime.po.request(request_id)
        if known is not None:
            return {"seq": known.seq, "queued": known.seq is None}
        queued = runtime.po.queued(request_id)
    except PoStoreError:
        return None
    if queued is not None and queued.session_id == session_id:
        return {"seq": None, "queued": True}
    return None


def _settle(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    ref = task["ref"]
    submission = record.po_submission
    try:
        if submission.seq is None:
            known = runtime.po.request(submission.submit_request_id)
            if known is None and (set_aside := runtime.po.refused(submission.submit_request_id)) is not None:
                return _block(
                    runtime,
                    task,
                    records,
                    payload,
                    record.attempt_id,
                    "the PO service set the card's input aside and will not run it: "
                    f"{set_aside.get('reason') or 'no reason recorded'}",
                )
            if known is None or known.seq is None:
                return _waiting(record, ref, "po-card-queued", "the input waits in the PO queue")
            submission.seq = int(known.seq)
            runtime.save_records(payload, records)
        turn = runtime.po.turn(submission.session_id, submission.seq)
    except PoStoreError as exc:
        return {
            "status": "degraded",
            "step": "po-card",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": "po-store-unanswered",
            "reason": f"the PO store did not answer for the card's turn: {exc}",
        }
    if turn.state == RUNNING:
        return _waiting(record, ref, "po-card-turn-running", f"PO turn {turn.session_id}/{turn.seq} runs")
    # The turn has settled. The card read at the top of the tick may predate the PO's completion,
    # which lands inside that very turn, so the card is read again before anything is decided.
    current = runtime.reader.show(ref)
    if current.get("state") != "in_progress":
        records.pop(ref, None)
        runtime.save_records(payload, records)
        return _closed(current, record.attempt_id, record)
    if waiting_owner(current) is not None:
        # The PO handed the card to the owner in that turn: it waits for the owner, not Blocked.
        return _await_owner(runtime, current, record, records, payload)
    return _block(
        runtime,
        current,
        records,
        payload,
        record.attempt_id,
        f"PO turn {turn.session_id}/{turn.seq} ended {turn.state} without completing the card",
    )


def _await_owner(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """A card the PO handed to the owner: wait, and pass each owner answer on to the PO once.

    Nothing polls the owner. The latest owner comment after the handover names the follow-up input
    (`owner_answer_request_id`); a new one replaces the previous follow-up, which carried fewer
    comments. The same comment always gives the same id and text, so a repeat, a lost answer or a
    rebuilt record never makes a second input.
    """
    ref = task["ref"]
    submission = record.po_submission
    mark = waiting_owner(task) or {}
    try:
        answered = owner_answer_event_ids(runtime.audit.events(ref))
    except Exception as exc:  # noqa: BLE001 - an audit that does not answer is retried next tick
        return {
            "status": "degraded",
            "step": "po-card",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": "po-owner-answer-unread",
            "reason": f"the card's audit could not be read for the owner's answer: {exc}",
        }
    if not answered:
        return _waiting(
            record, ref, "po-card-waiting-owner", f"handed to the owner: {mark.get('reason') or ''}"
        )
    request_id = owner_answer_request_id(ref, answered[-1])
    if submission.owner_request_id != request_id:
        comments = owner_comments_since_handover(task.get("comments") or [])
        submission.owner_event_id = answered[-1]
        submission.owner_request_id = request_id
        submission.owner_text = render_owner_answer_input(task, submission, mark, comments)
        submission.owner_submitted = False
        runtime.save_records(payload, records)
    if submission.owner_submitted:
        ended = None
        try:
            known = runtime.po.request(request_id)
            set_aside = None if known is not None else runtime.po.refused(request_id)
            if known is not None and known.seq is not None:
                # The follow-up's turn: settled without completing the card (it is still marked),
                # it says so, instead of reading as with the PO for good (secretary-1770).
                state = runtime.po.turn(known.session_id, int(known.seq)).state
                ended = state if state in {FAILED, INTERRUPTED} else None
        except PoStoreError as exc:
            set_aside, unread = None, exc
        else:
            unread = None
        if set_aside is not None:
            return _block(
                runtime,
                task,
                records,
                payload,
                record.attempt_id,
                "the PO service set the owner's answer aside and will not run it: "
                f"{set_aside.get('reason') or 'no reason recorded'}",
            )
        if ended is not None:
            return _waiting(
                record,
                ref,
                "po-card-owner-answer-turn-ended",
                f"handed to the owner: {mark.get('reason') or ''}; the owner's answer reached the PO, "
                f"but its turn {'failed' if ended == FAILED else 'was interrupted'}; a new owner comment "
                "sends it again",
            )
        return _waiting(
            record,
            ref,
            "po-card-owner-answered",
            f"handed to the owner: {mark.get('reason') or ''}; the owner's answer is with the PO"
            + (f" (the PO store did not answer: {unread})" if unread is not None else ""),
        )
    step = "owner answer"
    try:
        runtime.po.submit(
            session_id=submission.session_id,
            text=submission.owner_text,
            request_id=request_id,
            source=DISPATCHER_SOURCE,
            card=po_card_facts(task, submission, input=OWNER_ANSWER_INPUT),
        )
    except (ServiceUnavailable, OutcomeUnknown) as exc:
        return _unanswered(runtime, task, record, records, payload, step, exc)
    except ServiceRefused as exc:
        if exc.code in _UNANSWERED_CODES:
            return _unanswered(runtime, task, record, records, payload, step, exc)
        return _refused(runtime, task, record, records, payload, step, exc)
    except RequestConflict as exc:
        if _already_submitted(runtime, request_id, submission.session_id) is None:
            return _refused(runtime, task, record, records, payload, step, exc)
    except (PoServiceError, PoStoreError) as exc:
        return _refused(runtime, task, record, records, payload, step, exc)
    submission.owner_submitted = True
    submission.unanswered = 0
    submission.last_error = ""
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "po-card",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": "po-owner-answer-submitted",
        "po_session": submission.session_id,
        "po_request_id": request_id,
        "owner_event_id": submission.owner_event_id,
    }


def _waiting(record: DispatcherRecord, ref: str, action: str, reason: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "step": "po-card",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": action,
        "reason": reason,
    }


def _unanswered(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    step: str,
    exc: Exception,
) -> dict[str, Any]:
    """The service did not answer: the same request is repeated next tick, and the card waits."""
    submission = record.po_submission
    submission.unanswered += 1
    submission.last_error = f"{step}: {type(exc).__name__}: {exc}"[:500]
    runtime.save_records(payload, records)
    return {
        "status": "degraded",
        "step": "po-card",
        "pilot_ref": task["ref"],
        "attempt_id": record.attempt_id,
        "action": "po-service-unanswered",
        "reason": f"the PO service did not answer the {step}; it is repeated next tick with the same "
        f"request id: {exc}",
        "unanswered": submission.unanswered,
    }


def _refused(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    step: str,
    exc: Exception,
) -> dict[str, Any]:
    """The service refused the resolve or the submit outright: nothing will run the card."""
    record.po_submission.last_error = f"{step}: {type(exc).__name__}: {exc}"[:500]
    return _block(
        runtime,
        task,
        records,
        payload,
        record.attempt_id,
        f"the PO service refused the {step} of this card: {exc}",
    )


def _block(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    reason: str,
) -> dict[str, Any]:
    ref = task["ref"]
    runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        target="blocked",
        reason=reason,
        request_id=_attempt_request_id(attempt_id, PO_BLOCKED_ACTION, ref),
        terminal_taxonomy=normalize_terminal_taxonomy(
            disposition="blocked", blocked_reason="other"
        ).to_record(),
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {
        "status": "blocked",
        "step": "po-card",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": PO_BLOCKED_ACTION,
        "reason": reason,
    }


def _closed(task: dict[str, Any], attempt_id: str, record: DispatcherRecord | None) -> dict[str, Any]:
    """A decision/operation card that left In progress: its dispatcher record, if any, is closed."""
    return {
        "status": "ok",
        "step": "po-card",
        "pilot_ref": task["ref"],
        "attempt_id": record.attempt_id if record is not None else attempt_id,
        "action": "po-card-closed",
        "state": str(task.get("state") or ""),
        "completion": completion_state(task),
    }


def completion_state(task: dict[str, Any]) -> str:
    """`recorded` when the card carries its PO completion record, else what it lacks."""
    missing = missing_completion_evidence(task)
    return f"missing [{missing}]" if missing else "recorded"


__all__ = [
    "DISPATCHER_SOURCE",
    "PO_BLOCKED_ACTION",
    "PO_COMPLETE_ACTION",
    "PO_HANDOVER_ACTION",
    "PO_OWNER_ANSWER_ACTION",
    "PO_SESSION_ACTION",
    "PO_SUBMITTED",
    "PO_SUBMITTING",
    "PO_SUBMIT_ACTION",
    "PoChannel",
    "ServicePoChannel",
    "advance_po_card",
    "claim_po_card",
    "complete_command",
    "completion_state",
    "handover_command",
    "owner_answer_request_id",
    "po_card_facts",
    "render_owner_answer_input",
    "render_po_card_input",
]
