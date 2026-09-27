"""Delegated cards: a card returns its result to the PO session that cut it (secretary-1792).

A card created inside a PO turn carries its origin (`board/po_origin.py`). When it settles in Done or
Blocked, the dispatcher's tick delivers one input to that session, through the path every dispatcher
result takes to a PO session (`dispatch/po_delivery.py`): a closed or missing origin session gets a
successor, a service that does not answer postpones the delivery to the next tick, and nothing is given
up. Each delivery also rings the owner's bell once (`delegated_card_settled`, a notice).

Exactly once per terminal transition. The key is the card and the audit event of the transition into
its current column: the input's request id and the notice's dedup key are derived from it, and the
delivery is recorded on the card (`po_return.deliveries`) only after the service took it and the notice
was written. A crash anywhere in between repeats the same submit (the service answers the id it holds
as the earlier input) and the same notice (a no-op on its key); a card moved to Blocked again later is
a new transition and a new key.

Two exceptions. A wait card delivers through its own return addresses and never here. A
decision/operation card the dispatcher handed to its origin session (or that session's successor,
`po_return.executor`) was completed in a turn of that session, which already has the result: its Done
is recorded as `skipped`, with no input and no notice; its Blocked is delivered.

The pass reads the Done and Blocked cards once per tick and the audit of each delegated one it has not
settled: a Done card whose Done was already returned (or skipped) is not read again, since a Done card
leaves Done only by hand.
"""

from __future__ import annotations

import re
from typing import Any

from secretary.board import owner_events
from secretary.board import po_origin as origin_field
from secretary.board.completion_evidence import is_wait
from secretary.board.production_rights import DELEGATED_RESULT_INPUT, PO_CARD_KINDS, card_facts
from secretary.dispatch.po_delivery import deliver, open_successor
from secretary.dispatch.state import request_token
from secretary.tasks import TaskError, recorded_card_transition

STEP = "origin-return"
#: Request-id actions: `dispatcher-origin-return-<card>-<event id>` for the input, and
#: `dispatcher-origin-successor-<card>-<closed session>` for a successor of the origin's line.
RETURN_ACTION = "origin-return"
SUCCESSOR_ACTION = "origin-successor"
#: The longest result an input quotes; the rest is on the card.
RESULT_LIMIT = 6000
_LINK_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/(?:pull/\d+|actions/runs/\d+(?:/job/\d+)?)")
_REPORT_MARKERS = ("report:done", "report:blocked")


def return_request_id(reference: str, event_id: str) -> str:
    """The input's request id: the card and the terminal transition's event, nothing else."""
    return "-".join(request_token(part) for part in ("dispatcher", RETURN_ACTION, reference, event_id))


def successor_request_id(reference: str, closed: str) -> str:
    """The request id that opens the successor of `closed` in this card's origin line."""
    return "-".join(request_token(part) for part in ("dispatcher", SUCCESSOR_ACTION, reference, closed))


def notice_key(reference: str, event_id: str) -> str:
    return f"{owner_events.DELEGATED_CARD_SETTLED}:{reference}:{event_id}"


def record_return_state(runtime: Any, reference: str, state: origin_field.ReturnState) -> None:
    runtime.writer.record_po_return(
        role="dispatcher", actor=runtime.owner, reference=reference, state=state.text()
    )


def succeed_origin(
    runtime: Any, task: dict[str, Any], origin: dict[str, str], state: origin_field.ReturnState, closed: str
) -> tuple[str, str]:
    """The session that succeeds `closed` in the card's origin line: `(session, "")`, or `("", why)`.

    One successor per closed session, recorded in `po_return.successors` (route before the call, id
    right after), shared by the out-of-sprint execution of a decision/operation card and by every
    result returned to the origin, so neither opens a second one.
    """
    ref = task["ref"]
    record = state.successors.setdefault(closed, {})
    session, why = open_successor(
        runtime,
        reference=ref,
        sprint_ref=str(task.get("sprint") or ""),
        closed=closed,
        record=record,
        persist=lambda: record_return_state(runtime, ref, state),
        request_id=successor_request_id(ref, closed),
    )
    if not record:
        state.successors.pop(closed, None)
    return session, why


def reconcile_origin_returns(runtime: Any) -> list[dict[str, Any]]:
    """Return every settled delegated card's result to its origin session once; one tick's pass."""
    outcomes: list[dict[str, Any]] = []
    cards = sorted(
        runtime.reader.list(states=set(origin_field.TERMINAL_STATES)),
        key=lambda task: str(task.get("ref") or ""),
    )
    for task in cards:
        origin = origin_field.po_origin(task)
        if origin is None or is_wait(task):
            continue
        state = origin_field.return_state(task)
        column = str(task.get("state") or "")
        if column == "done" and any(record.get("state") == "done" for record in state.deliveries.values()):
            continue
        try:
            outcome = _return_one(runtime, task, origin, state)
        except TaskError as exc:
            outcome = _outcome(
                task["ref"], "origin-return-unread", status="degraded", reason=f"{exc.code}: {exc.message}"
            )
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def _return_one(
    runtime: Any, task: dict[str, Any], origin: dict[str, str], state: origin_field.ReturnState
) -> dict[str, Any] | None:
    ref = task["ref"]
    column = str(task.get("state") or "")
    events = runtime.audit.events(ref)
    index = _terminal_index(events, column)
    if index is None:
        # The audit names no move into the column: there is no transition to key a delivery by.
        return None
    terminal = events[index]
    key = str(terminal.get("event_id") or terminal.get("request_id") or "")
    if not key or key in state.deliveries:
        return None
    at = str(terminal.get("occurred_at") or "")
    if (
        column == "done"
        and str(task.get("type") or "") in PO_CARD_KINDS
        and state.executor
        and origin_field.in_line(state.executor, origin["session"], state)
    ):
        state.deliveries[key] = {
            "state": column,
            "status": origin_field.SKIPPED,
            "at": at,
            "session": state.executor,
            "detail": "completed in a turn of its origin session, which has the result",
        }
        record_return_state(runtime, ref, state)
        return _outcome(ref, "origin-return-skipped", state=column, event_id=key, session=state.executor)
    request_id = return_request_id(ref, key)
    text = render_origin_input(task, origin, events, index)
    status, detail, received = deliver(
        runtime,
        session_id=origin_field.line_head(origin["session"], state),
        text=text,
        request_id=request_id,
        card=card_facts(
            card_ref=ref,
            kind=str(task.get("type") or ""),
            touches_production=None,
            sprint_ref=str(task.get("sprint") or ""),
            input=DELEGATED_RESULT_INPUT,
        ),
        successor=lambda closed: succeed_origin(runtime, task, origin, state, closed),
    )
    if status is None:
        return _outcome(
            ref,
            "origin-return-postponed",
            status="degraded",
            state=column,
            event_id=key,
            reason="the origin PO session did not take the card's result; it is repeated next tick under "
            f"the same request id: {detail}",
        )
    owner_events.record(
        owner_events.DELEGATED_CARD_SETTLED,
        ref,
        render_notice(task, origin, received),
        notice_key(ref, key),
        to=getattr(getattr(runtime, "reader", None), "client", None),
    )
    # Recorded only after the session took it and the bell rang: a crash before this line repeats
    # both under the same key, and each receiving side makes the repeat a no-op.
    state.deliveries[key] = {
        "state": column,
        "status": status,
        "at": at,
        "session": received,
        "request_id": request_id,
    }
    record_return_state(runtime, ref, state)
    return _outcome(
        ref, "origin-returned", state=column, event_id=key, session=received, po_request_id=request_id
    )


def _terminal_index(events: list[dict[str, Any]], column: str) -> int | None:
    """The index of the latest audited transition into `column`, or None."""
    found = None
    for index, event in enumerate(events):
        moved = recorded_card_transition(event)
        if moved is not None and moved[1] == column:
            found = index
    return found


def _data(event: dict[str, Any]) -> dict[str, Any]:
    for name in ("data", "payload"):
        value = event.get(name)
        if isinstance(value, dict):
            return value
    return {}


def _round_report(events: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    """The worker's last report before the terminal event, in the round the card last entered In progress."""
    report = None
    for event in events[:index]:
        moved = recorded_card_transition(event)
        if moved is not None and moved[1] == "in_progress":
            report = None
            continue
        if str(_data(event).get("marker") or "") in _REPORT_MARKERS:
            report = _data(event)
    return report


def _bounded(text: str) -> str:
    text = text.strip()
    if len(text) <= RESULT_LIMIT:
        return text
    return text[:RESULT_LIMIT].rstrip() + "\n\n(cut here; the whole text is on the card: `task show`)"


def _result_lines(task: dict[str, Any], events: list[dict[str, Any]], index: int) -> list[str]:
    terminal = events[index]
    reason = str(terminal.get("reason") or "").strip()
    report = _round_report(events, index)
    if str(task.get("state") or "") == "done":
        if reason.startswith("[completion:"):
            return ["## Completion record", "", _bounded(reason)]
        if (
            report is not None
            and report.get("marker") == "report:done"
            and str(report.get("body") or "").strip()
        ):
            return ["## The worker's done report", "", _bounded(str(report["body"]))]
        return ["## Completion record", "", _bounded(reason) or "(the Done move carries no record)"]
    classification = ""
    if report is not None and report.get("marker") == "report:blocked":
        classification = str(report.get("classification") or "")
    if not classification:
        taxonomy = _data(terminal).get("terminal_taxonomy")
        blocked = taxonomy.get("blocked_reason") if isinstance(taxonomy, dict) else None
        classification = f"board: {blocked}" if blocked else "unclassified"
    lines = ["## Why it is Blocked", "", _bounded(reason) or "(the Blocked move carries no reason)", ""]
    lines.append(f"Classification: {classification}")
    body = str(report.get("body") or "").strip() if report is not None else ""
    if report is not None and report.get("marker") == "report:blocked" and body and body != reason:
        lines += ["", "## The worker's blocked report", "", _bounded(body)]
    return lines


def _links(task: dict[str, Any], events: list[dict[str, Any]], index: int) -> list[str]:
    terminal = events[index]
    texts = [str(event.get("reason") or "") for event in events[: index + 1]]
    texts += [str(_data(event).get("body") or "") for event in events[: index + 1]]
    links = list(dict.fromkeys(match for text in texts for match in _LINK_RE.findall(text)))
    merge = _data(terminal).get("release_merge")
    lines = [f"- {link}" for link in links]
    if isinstance(merge, dict) and merge.get("merge_sha"):
        lines.append(f"- merged {merge['merge_sha']} onto {merge.get('base') or 'the base'}")
    return lines


def render_origin_input(
    task: dict[str, Any], origin: dict[str, str], events: list[dict[str, Any]], index: int
) -> str:
    """The one input a delegated card's terminal transition becomes in its origin session.

    Built from the card and its audit up to that transition, so the same transition always renders
    the same text.
    """
    ref = str(task.get("ref") or "")
    kind = str(task.get("type") or "")
    column = str(task.get("state") or "")
    settled = "Done" if column == "done" else "Blocked"
    request = f" (answering request {origin['request']})" if origin.get("request") else ""
    lines = [
        (
            f"Card {ref} ({kind}), which PO session {origin['session']} cut{request}, settled {settled}. "
            "This is its result, sent to this session once for this transition."
        ),
        "",
        f"Card: {ref} ({kind}): {task.get('title') or ''}",
        f"State: {settled}, at {events[index].get('occurred_at') or 'an unrecorded time'}",
        f"Sprint: {task.get('sprint') or 'none'}",
        "",
        *_result_lines(task, events, index),
    ]
    links = _links(task, events, index)
    if links:
        lines += ["", "## Links", "", *links]
    lines += [
        "",
        (
            "Decide what follows from it in this turn: answer the owner, or cut the next card. Keep the "
            "turn short; anything long-running becomes a card."
        ),
    ]
    return "\n".join(lines).rstrip() + "\n"


def render_notice(task: dict[str, Any], origin: dict[str, str], received: str) -> str:
    """The owner's notice: the card, its terminal state and the session that took its result."""
    ref = str(task.get("ref") or "")
    settled = "Done" if str(task.get("state") or "") == "done" else "Blocked"
    taken = (
        f"PO session {origin['session']}"
        if received == origin["session"]
        else f"PO session {received}, the successor of its origin session {origin['session']}"
    )
    return (
        f"Delegated card {ref} ({task.get('type') or 'card'}) settled {settled}: {task.get('title') or ''}. "
        f"Its result went to {taken}."
    )


def _outcome(ref: str, action: str, *, status: str = "ok", **fields: Any) -> dict[str, Any]:
    return {"status": status, "step": STEP, "pilot_ref": ref, "action": action, **fields}


__all__ = [
    "RETURN_ACTION",
    "STEP",
    "SUCCESSOR_ACTION",
    "notice_key",
    "reconcile_origin_returns",
    "record_return_state",
    "render_notice",
    "render_origin_input",
    "return_request_id",
    "succeed_origin",
    "successor_request_id",
]
