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
another turn.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Protocol

from secretary.board.completion_evidence import missing_completion_evidence
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
from secretary.po.store import RUNNING, PoRequest, PoStoreError, RequestConflict, Turn

#: The source the PO service records for a dispatcher input (`secretary.po.queue.SOURCES`).
DISPATCHER_SOURCE = "dispatcher"
#: Dispatcher record states of a PO-executed card.
PO_SUBMITTING = "po_submitting"
PO_SUBMITTED = "po_submitted"
#: Request-id actions, each under `attempt_request_id(<claim attempt>, <action>, <card ref>)`.
PO_SESSION_ACTION = "po-session"
PO_SUBMIT_ACTION = "po-submit"
PO_COMPLETE_ACTION = "po-complete"
PO_BLOCKED_ACTION = "po-card-blocked"
#: Service refusal codes that say nothing about the request: it is repeated, never failed.
_UNANSWERED_CODES = frozenset({"unavailable", "outcome_unknown"})


class PoChannel(Protocol):
    """What the dispatcher needs of the PO service and its store."""

    def sprint_session(self, *, sprint_ref: str, request_id: str) -> dict[str, Any]: ...

    def submit(self, *, session_id: str, text: str, request_id: str, source: str) -> dict[str, Any]: ...

    def request(self, request_id: str) -> PoRequest | None: ...

    def turn(self, session_id: str, seq: int) -> Turn: ...


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

    def submit(self, *, session_id: str, text: str, request_id: str, source: str) -> dict[str, Any]:
        return self._service().submit(session_id=session_id, text=text, request_id=request_id, source=source)

    def request(self, request_id: str) -> PoRequest | None:
        return self._po_store().request(request_id)

    def turn(self, session_id: str, seq: int) -> Turn:
        return self._po_store().turn(session_id, seq)


def complete_command(reference: str, kind: str, request_id: str) -> str:
    """The exact command the PO runs to complete the card, as its input quotes it."""
    return (
        f"python3 -P -m secretary task complete --ref {reference} --role po --kind {kind} "
        f"--body-file <file> --request-id {request_id}"
    )


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
            "In progress Blocks it."
        ),
        "",
        f"Card: {reference} ({kind}): {task.get('title') or ''}",
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
        if not submission.text:
            sprint = runtime.sprints.show(submission.sprint_ref, include_resume_freshness=False)
            submission.text = render_po_card_input(task, sprint, submission)
            runtime.save_records(payload, records)
        answer = runtime.po.submit(
            session_id=submission.session_id,
            text=submission.text,
            request_id=submission.submit_request_id,
            source=DISPATCHER_SOURCE,
        )
    except (ServiceUnavailable, OutcomeUnknown) as exc:
        return _unanswered(runtime, task, record, records, payload, step, exc)
    except ServiceRefused as exc:
        if exc.code in _UNANSWERED_CODES:
            return _unanswered(runtime, task, record, records, payload, step, exc)
        return _refused(runtime, task, record, records, payload, step, exc)
    except RequestConflict as exc:
        # The submit id is this attempt's own: a conflict says it already carries an input, which
        # is the one this record composed before it was lost and rebuilt. Anything else is refused.
        known = _known_request(runtime, submission.submit_request_id) if step == "submit" else None
        if known is None:
            return _refused(runtime, task, record, records, payload, step, exc)
        answer = {"seq": known.seq, "queued": known.seq is None}
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


def _known_request(runtime: Any, request_id: str) -> PoRequest | None:
    try:
        return runtime.po.request(request_id)
    except PoStoreError:
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
    return _block(
        runtime,
        current,
        records,
        payload,
        record.attempt_id,
        f"PO turn {turn.session_id}/{turn.seq} ended {turn.state} without completing the card",
    )


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
    "render_po_card_input",
]
