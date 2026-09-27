"""Delegated cards: a card returns its result to the PO session that cut it (secretary-1792).

A card created inside a PO turn carries its origin (`board/po_origin.py`). When it settles in Done or
Blocked, the dispatcher's tick delivers one input to that session, through the path every dispatcher
result takes to a PO session (`dispatch/po_delivery.py`): a closed or missing origin session gets a
successor, a service that does not answer postpones the delivery to the next tick, and nothing is given
up. Each delivery also rings the owner's bell once (`delegated_card_settled`, a notice).

Exactly once per terminal transition. :func:`pending_returns` is the one rule and the only place that
decides what is returned: every audited transition into Done or Blocked whose event id is not in
`po_return.deliveries`, in audit order. Nothing reads the card's current column or "the latest" event to
choose one. Every card with an origin is a candidate, whatever its column, so a card reopened before the
dispatcher saw its Done still returns that Done. Pending returns are made oldest first, each its own
submit (idempotent by request id), notice through the bell's strict writer (`owner_events.record_strict`)
and, only when both were accepted, record on the card; each is keyed by its own event id and rendered
from that event (the column it entered, when, its result), never from the card's present state. The
first that does not complete stops that card's pass for the tick, and the later ones wait for the next;
none is skipped because a later one exists. A notice that failed records nothing, and the next tick
repeats both under the same keys: the submit is the earlier input to the service, the notice a no-op on
its key. A crash anywhere in between is repaired the same way. An installation with no board store at
all has no bell to wait for, and the delivery completes without it.

The cursor. A tick reads the audit of a candidate only when it may have changed: the dispatcher keeps,
in its own state (`CURSORS_KEY`), the card's `moved_at` (its `date_moved`, which every column move sets
and nothing else writes) as of its last complete pass, and a card whose `moved_at` is unchanged is not
read. A new transition into Done or Blocked is always a move. The cursor is kept only for a pass that
started `CURSOR_MARGIN_SECONDS` after that move's second, so a move during or after the pass is seen. It
is a cache: lost with the dispatcher's state, every candidate is read once more, and
`po_return.deliveries` alone decides what is owed.

Two exceptions, both decided per event. A wait card delivers through its own return addresses and
never here (:func:`pending_returns` owes it nothing). A Done whose completion transition records the PO
session that ran `task complete` (`po_session`, from that turn's `SECRETARY_PO_SESSION`), where that
session is the origin or one of its recorded successors, was completed in a turn of the origin's line,
which already has the result: it is recorded as `skipped`, with no input and no notice, so it is never
pending again. Anything else is delivered: a completion by another session, one that ran outside a PO
turn, any other Done, and every Blocked. The session the dispatcher handed the card to
(`po_return.executor`) proves nothing and is not asked.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from secretary.board import owner_events
from secretary.board import po_origin as origin_field
from secretary.board.completion_evidence import is_wait
from secretary.board.production_rights import DELEGATED_RESULT_INPUT, card_facts
from secretary.dispatch.po_delivery import deliver, open_successor
from secretary.dispatch.state import request_token
from secretary.tasks import PO_SESSION_KEY, TaskError, recorded_card_transition

STEP = "origin-return"
#: Request-id actions: `dispatcher-origin-return-<card>-<event id>` for the input, and
#: `dispatcher-origin-successor-<card>-<closed session>` for a successor of the origin's line.
RETURN_ACTION = "origin-return"
SUCCESSOR_ACTION = "origin-successor"
#: The longest result an input quotes; the rest is on the card.
RESULT_LIMIT = 6000
_LINK_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/(?:pull/\d+|actions/runs/\d+(?:/job/\d+)?)")
_REPORT_MARKERS = ("report:done", "report:blocked")
#: The dispatcher state key of the per-card cursors (`reconcile_origin_returns`).
CURSORS_KEY = "origin_return_cursors"
#: How long after a card's last move a pass must start before its cursor is kept (`_cursor_holds`).
CURSOR_MARGIN_SECONDS = 2


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


@dataclass(frozen=True)
class PendingReturn:
    """One audited transition into Done or Blocked whose result the origin session is still owed."""

    index: int  # its position in the card's audit, which is the order returns are made in
    event: dict[str, Any]
    key: str  # its event id: the input's request id and the notice's dedup key are derived from it
    column: str  # the column it entered, `done` or `blocked`, whatever the card's column is now


def event_key(event: dict[str, Any]) -> str:
    """The key of one audited transition: its event id (its request id for a record that names none)."""
    return str(event.get("event_id") or event.get("request_id") or "")


def pending_returns(
    card: dict[str, Any], events: list[dict[str, Any]], state: origin_field.ReturnState
) -> list[PendingReturn]:
    """Every audited transition into Done or Blocked not yet in `po_return.deliveries`, in audit order.

    The one rule, and the only code that decides what is returned: nothing reads the card's current
    column or picks "the latest" event. A card with no origin, and a wait card (it returns through its
    own addresses), owes nothing. Pure: no reads, no writes.
    """
    if origin_field.po_origin(card) is None or is_wait(card):
        return []
    pending: list[PendingReturn] = []
    for index, event in enumerate(events):
        moved = recorded_card_transition(event)
        if moved is None or moved[1] not in origin_field.TERMINAL_STATES:
            continue
        key = event_key(event)
        if key and key not in state.deliveries:
            pending.append(PendingReturn(index, event, key, moved[1]))
    return pending


def reconcile_origin_returns(runtime: Any, cursors: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return every delegated card's owed results to its origin session, oldest first; one tick's pass.

    Every card with an origin is a candidate, whatever its column: a card in Ready, In progress or
    Validate may still owe the return of an earlier Done or Blocked. `cursors` (the dispatcher's own
    state, `CURSORS_KEY`) keeps, per card, the `moved_at` of its last complete pass: a card that has
    not moved since is not read again (see `_cursor_holds`). It is a cache only: without it every
    candidate's audit is read, and `po_return.deliveries` alone decides what is owed.
    """
    outcomes: list[dict[str, Any]] = []
    cards = sorted(runtime.reader.delegated_cards(), key=lambda task: str(task.get("ref") or ""))
    if cursors is not None:
        listed = {str(task.get("ref") or "") for task in cards}
        for ref in [ref for ref in cursors if ref not in listed]:
            del cursors[ref]
    for task in cards:
        ref = str(task.get("ref") or "")
        origin = origin_field.po_origin(task)
        if origin is None or is_wait(task):
            continue
        moved_at = task.get("moved_at")
        if cursors is not None and _cursor_holds(cursors.get(ref), moved_at):
            continue
        started = time.time()
        try:
            returned, complete = _return_card(runtime, task, origin)
        except TaskError as exc:
            returned, complete = (
                [
                    _outcome(
                        ref, "origin-return-unread", status="degraded", reason=f"{exc.code}: {exc.message}"
                    )
                ],
                False,
            )
        outcomes += returned
        if cursors is None:
            continue
        if complete and isinstance(moved_at, int) and started >= moved_at + CURSOR_MARGIN_SECONDS:
            cursors[ref] = {"moved_at": moved_at}
        else:
            cursors.pop(ref, None)
    return outcomes


def _cursor_holds(cursor: Any, moved_at: Any) -> bool:
    """Whether the card is unchanged since its last complete pass: it has not moved since.

    `moved_at` is the card's `date_moved` (epoch seconds), which every column move sets and nothing
    else writes; a new transition into Done or Blocked is always a move. The cursor is kept only for a
    pass that started at least `CURSOR_MARGIN_SECONDS` after that second, so a move landing during or
    after the pass has a later `moved_at` and is read on the next tick.
    """
    return (
        isinstance(cursor, dict)
        and isinstance(moved_at, int)
        and not isinstance(moved_at, bool)
        and cursor.get("moved_at") == moved_at
    )


def _return_card(
    runtime: Any, task: dict[str, Any], origin: dict[str, str]
) -> tuple[list[dict[str, Any]], bool]:
    """Every pending return of one card, oldest first: `(outcomes, complete)`.

    Each is its own submit, strict notice and record. The first that does not complete stops the
    card's pass for the tick; the later ones wait for the next, and none is skipped for a later one.
    """
    events = runtime.audit.events(task["ref"])
    state = origin_field.return_state(task)
    outcomes: list[dict[str, Any]] = []
    for pending in pending_returns(task, events, state):
        outcome = _return_event(runtime, task, origin, state, events, pending)
        outcomes.append(outcome)
        if outcome["status"] != "ok":
            return outcomes, False
    return outcomes, True


def _return_event(
    runtime: Any,
    task: dict[str, Any],
    origin: dict[str, str],
    state: origin_field.ReturnState,
    events: list[dict[str, Any]],
    pending: PendingReturn,
) -> dict[str, Any]:
    ref = task["ref"]
    column, key, terminal = pending.column, pending.key, pending.event
    at = str(terminal.get("occurred_at") or "")
    # Evaluated on this event: the PO session whose turn ran `task complete`, as the event records it.
    completer = str(_data(terminal).get(PO_SESSION_KEY) or "") if column == "done" else ""
    if completer and origin_field.in_line(completer, origin["session"], state):
        state.deliveries[key] = {
            "state": column,
            "status": origin_field.SKIPPED,
            "at": at,
            "session": completer,
            "detail": "completed in a turn of its origin session's line, which has the result",
        }
        record_return_state(runtime, ref, state)
        return _outcome(ref, "origin-return-skipped", state=column, event_id=key, session=completer)
    request_id = return_request_id(ref, key)
    text = render_origin_input(task, origin, events, pending.index)
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
    notice = owner_events.record_strict(
        owner_events.DELEGATED_CARD_SETTLED,
        ref,
        render_notice(task, origin, received, column),
        notice_key(ref, key),
        to=getattr(getattr(runtime, "reader", None), "client", None),
    )
    if notice == owner_events.FAILED:
        # Half a delivery is none: nothing is recorded, and the next tick repeats the submit (the
        # service's no-op on its request id) and the notice (a no-op on its dedup key).
        return _outcome(
            ref,
            "origin-return-notice-failed",
            status="degraded",
            state=column,
            event_id=key,
            session=received,
            reason="the origin session took the card's result but the owner's notice was not written; both "
            "are repeated next tick under the same keys",
        )
    # Recorded only after the session took it and the bell has it: a crash before this line repeats
    # both under the same keys, and each receiving side makes the repeat a no-op.
    state.deliveries[key] = {
        "state": column,
        "status": status,
        "notice": notice,
        "at": at,
        "session": received,
        "request_id": request_id,
    }
    record_return_state(runtime, ref, state)
    return _outcome(
        ref, "origin-returned", state=column, event_id=key, session=received, po_request_id=request_id
    )


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


def event_column(event: dict[str, Any]) -> str:
    """The column an audited transition entered, `""` for a record that is no transition."""
    moved = recorded_card_transition(event)
    return moved[1] if moved is not None else ""


def _result_lines(events: list[dict[str, Any]], index: int) -> list[str]:
    """The result of the transition at `index`, from that transition and the audit before it only.

    From the transition's own data where it carries one: a completion record (`[completion:...]`),
    the decision it names, the Blocked reason and classification. Otherwise, for a Done, the worker's
    last done report before it in its round.
    """
    terminal = events[index]
    reason = str(terminal.get("reason") or "").strip()
    report = _round_report(events, index)
    decision = str(_data(terminal).get("decision") or "").strip()
    decided = [f"Decision: {decision}", ""] if decision else []
    if event_column(terminal) == "done":
        if reason.startswith("[completion:"):
            return [*decided, "## Completion record", "", _bounded(reason)]
        if (
            report is not None
            and report.get("marker") == "report:done"
            and str(report.get("body") or "").strip()
        ):
            return [*decided, "## The worker's done report", "", _bounded(str(report["body"]))]
        return [*decided, "## Completion record", "", _bounded(reason) or "(the Done move carries no record)"]
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


def _links(events: list[dict[str, Any]], index: int) -> list[str]:
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

    Built from that transition and the audit before it, plus the card's identity (ref, kind, title,
    sprint), never from the card's present column: a card already reopened still returns the Done or
    Blocked it had. The same transition always renders the same text.
    """
    ref = str(task.get("ref") or "")
    kind = str(task.get("type") or "")
    settled = "Done" if event_column(events[index]) == "done" else "Blocked"
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
        *_result_lines(events, index),
    ]
    links = _links(events, index)
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


def render_notice(task: dict[str, Any], origin: dict[str, str], received: str, column: str) -> str:
    """The owner's notice: the card, the terminal state it entered and the session that took its result."""
    ref = str(task.get("ref") or "")
    settled = "Done" if column == "done" else "Blocked"
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
    "CURSORS_KEY",
    "CURSOR_MARGIN_SECONDS",
    "RETURN_ACTION",
    "STEP",
    "SUCCESSOR_ACTION",
    "PendingReturn",
    "event_column",
    "event_key",
    "notice_key",
    "pending_returns",
    "reconcile_origin_returns",
    "record_return_state",
    "render_notice",
    "render_origin_input",
    "return_request_id",
    "succeed_origin",
    "successor_request_id",
]
