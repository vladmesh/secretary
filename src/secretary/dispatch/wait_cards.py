"""Wait cards: the dispatcher advances them once per tick, with no head (secretary-1790).

A `wait` card (`board/wait_card.py`) waits for one fact: a GitHub Actions run concluding, another card
reaching one of a named set of states, or a point in time. When the dispatcher claims a Ready one it
cuts no workspace, launches nothing and runs no broad check or Git preflight: the card moves to In
progress and each tick after that observes the target once.

- A GitHub run is only read, `GET repos/{repo}/actions/runs/{id}` through the gate's `_gh_api`, the
  same one read per tick the CI gate makes. Nothing here starts, reruns or dispatches a workflow.
- Everything the wait knows is the card's own `wait_state` field, rewritten only when it changed. A
  new dispatcher process reads it back and continues; there is no watcher to lose.
- The first terminal fact is frozen into `wait_state.result` before anything is delivered, and it
  is never overwritten: `target_reached` (a run's conclusion, `failure` included; the card state
  reached; the time), `cancelled` (`task cancel`), `deadline_passed`, or `source_unreachable` (a
  definitive 404/410 or no access, a card that does not exist, or transient errors lasting the
  wait's transient window).

Delivery is keyed by (card, address, frozen result) and recorded on the card only after the
receiving side accepted it; a delivery repeated after a crash carries the same key, and the receiving
side makes it a no-op:

- `po-session:<id>`: one input through the PO service (`source: dispatcher`) under a request id
  derived from that key, which the service deduplicates. A service that does not answer postpones
  the delivery to the next tick; a closed or unknown session refuses it for good, and the card says so.
- `dependents`: one dispatcher comment on every card whose `blocked_by` names the wait card, under a
  request id derived from the key; on an outcome other than `target_reached`, a Ready dependent is
  also Blocked with the outcome as its reason. Until then the claim pass leaves such a card in Ready
  (:func:`pending_wait_blockers`).
- `observer`: the terminal move itself, last: Done for `target_reached`, Blocked for every other
  outcome, with the result as the move's comment. The observer's wake on Done/Blocked is the delivery.

A wait card's Blocked, and a dependent's, carry `wait_outcome` in their transition data: an outcome,
not a pipeline restart, so the sprint budget does not charge them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from secretary.board import wait_card
from secretary.board.completion_evidence import is_wait
from secretary.board.production_rights import WAIT_KIND, WAIT_OUTCOME_INPUT, card_facts
from secretary.board.terminal_taxonomy import normalize_terminal_taxonomy
from secretary.board.wait_card import (
    ACCEPTED,
    CANCELLED,
    DEADLINE_PASSED,
    DEPENDENTS,
    REFUSED,
    SOURCE_UNREACHABLE,
    TARGET_CARD,
    TARGET_REACHED,
    TARGET_RUN,
    WaitSpec,
    WaitState,
    parse_utc,
    utc_text,
)
from secretary.dispatch.gate import _HTTP_STATUS_RE, GateTransportError, _gh_api
from secretary.dispatch.helpers import _worker_id
from secretary.dispatch.po_cards import _UNANSWERED_CODES, DISPATCHER_SOURCE, _already_submitted
from secretary.dispatch.state import DispatcherRecord, request_token
from secretary.dispatch.state import attempt_request_id as _attempt_request_id
from secretary.dispatch.state import new_attempt_id as _new_attempt_id
from secretary.dispatch.state import record_attempt as _record_attempt
from secretary.dispatch.types import HostError
from secretary.po.client import OutcomeUnknown, PoServiceError, ServiceRefused, ServiceUnavailable
from secretary.po.store import PoStoreError, RequestConflict, SessionClosed, SessionNotFound
from secretary.tasks import TaskError

WAIT_STEP = "wait-card"
#: Request-id actions of a delivery, each under `delivery_request_id(<card>, <action>, <address>, <key>)`.
PO_DELIVERY = "po"
DEPENDENT_COMMENT = "dependent-comment"
DEPENDENT_BLOCKED = "dependent-blocked"
TERMINAL_MOVE = "terminal"
WAIT_MALFORMED_ACTION = "wait-malformed-blocked"
#: The fields of a run the wait reads, and the one status that ends it.
_RUN_JQ = "{status, conclusion, html_url, created_at, updated_at, run_started_at}"
_RUN_COMPLETED = "completed"
#: The cards a wait's outcome can still reach: every column but Done.
_DEPENDENT_STATES = {"issues", "ready", "in_progress", "validate", "assessment", "blocked"}


def utcnow() -> datetime:
    """The dispatcher's clock for waits; tests replace it."""
    return datetime.now(UTC)


class _Unreachable(Exception):
    """The source answered that the target does not exist or is not accessible."""


class _Transient(Exception):
    """The source did not answer this time (network, 5xx, rate limit): retried next tick."""


def delivery_request_id(reference: str, action: str, address: str, key: str) -> str:
    """The request id of one delivery: the card, the action, the address and the frozen result."""
    return "-".join(request_token(part) for part in ("dispatcher", "wait", action, reference, address, key))


def pending_wait_blockers(
    runtime: Any, task: dict[str, Any], cache: dict[str, Any] | None = None
) -> list[str]:
    """The wait cards named by this card's `blocked_by` that are still waiting (Ready or In progress).

    Only wait-card blockers hold a card here; a blocker of any other kind, or one that cannot be
    read, is left to the claim's own predecessor rule, as before.
    """
    cache = {} if cache is None else cache
    pending: list[str] = []
    for ref in _blocker_refs(task):
        if ref not in cache:
            try:
                cache[ref] = runtime.reader.show(ref)
            except TaskError:
                cache[ref] = None
        blocker = cache[ref]
        if blocker is not None and is_wait(blocker) and blocker.get("state") in {"ready", "in_progress"}:
            pending.append(ref)
    return pending


def _blocker_refs(task: dict[str, Any]) -> list[str]:
    return [part.strip() for part in str(task.get("blocked_by") or "").split(",") if part.strip()]


def claim_wait_card(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """Claim a Ready wait card; no workspace, no head, no preflight. It then advances in this tick."""
    ref = task["ref"]
    claim_id = _attempt_request_id(attempt_id, "claim", ref)
    if runtime.audit.committed_event(claim_id) is not None:
        # A wait card back in Ready (moved by hand) is claimed again under a new attempt.
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
    return advance_wait_card(runtime, runtime.reader.show(ref), records, payload, attempt_id)


def advance_wait_card(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """One tick of a wait card, whatever column it stands in. It keeps no dispatcher record."""
    ref = task["ref"]
    column = str(task.get("state") or "")
    if column == "ready":
        return claim_wait_card(runtime, task, records, payload, attempt_id)
    if records.pop(ref, None) is not None:
        runtime.save_records(payload, records)
    if column != "in_progress":
        return _outcome(ref, attempt_id, "wait-closed", state=column, wait=wait_card.wait_view(task))
    spec = wait_card.wait_spec(task)
    if spec is None:
        reason = "the wait card carries no well-formed wait spec, so there is nothing to wait for"
        runtime.writer.move(
            role="dispatcher",
            actor=runtime.owner,
            reference=ref,
            target="blocked",
            reason=reason,
            request_id=_attempt_request_id(attempt_id, WAIT_MALFORMED_ACTION, ref),
            terminal_taxonomy=normalize_terminal_taxonomy(
                disposition="blocked", blocked_reason="other"
            ).to_record(),
        )
        return {**_outcome(ref, attempt_id, WAIT_MALFORMED_ACTION, reason=reason), "status": "blocked"}
    now = utcnow()
    known = wait_card.wait_state(task)
    state = WaitState.from_json(known.to_json())
    if not state.since:
        state.since = utc_text(now)
    if state.result is None:
        _observe(runtime, task, spec, state, now)
    if state.text() != known.text():
        _record(runtime, ref, state)
    if state.result is None:
        return _outcome(
            ref,
            attempt_id,
            "wait-waiting",
            observation=state.observation,
            error=state.error,
            deadline=spec.deadline,
        )
    return _deliver(runtime, task, spec, state, attempt_id)


def _record(runtime: Any, ref: str, state: WaitState) -> None:
    runtime.writer.record_wait_state(
        role="dispatcher", actor=runtime.owner, reference=ref, state=state.text()
    )


# --- observation ---------------------------------------------------------------------------------


def _freeze(
    state: WaitState, outcome: str, fact: dict[str, Any], summary: str, evidence: str, now: datetime
) -> None:
    result = {
        "outcome": outcome,
        "fact": fact,
        "summary": summary,
        "evidence": evidence,
        "frozen_at": utc_text(now),
    }
    result["key"] = wait_card.result_key(result)
    state.result = result


def _observe(runtime: Any, task: dict[str, Any], spec: WaitSpec, state: WaitState, now: datetime) -> None:
    """Observe the target once and freeze the first terminal fact, if there is one."""
    cancel = wait_card.wait_cancel(task)
    if cancel is not None:
        _freeze(
            state,
            CANCELLED,
            {
                "cancelled_at": cancel["at"],
                "by": cancel["by"],
                "role": cancel["role"],
                "reason": cancel["reason"],
            },
            f"cancelled by {cancel['role']} {cancel['by']} at {cancel['at']}: {cancel['reason']}",
            "",
            now,
        )
        return
    target = spec.target
    deadline = parse_utc(spec.deadline, "deadline")
    try:
        if target.kind == TARGET_RUN:
            reached = _observe_run(runtime, spec, state, now)
        elif target.kind == TARGET_CARD:
            reached = _observe_card(runtime, spec, state, now)
        else:
            at = parse_utc(target.at, "at")
            reached = now >= at
            _observed(state, f"waiting for {target.at}" if not reached else f"{target.at} arrived", now)
            if reached:
                _freeze(state, TARGET_REACHED, {"at": target.at}, f"the time {target.at} arrived", "", now)
    except _Unreachable as exc:
        _freeze(
            state,
            SOURCE_UNREACHABLE,
            {"error": str(exc)},
            f"the source is unreachable: {exc}",
            target.link,
            now,
        )
        return
    except _Transient as exc:
        text = str(exc)[:500]
        if not state.error_since:
            state.error_since = utc_text(now)
        if state.error != text:
            state.error, state.error_at = text, utc_text(now)
        window_end = parse_utc(state.error_since, "error_since") + timedelta(
            seconds=spec.transient_window_seconds
        )
        # The window never runs past the deadline: a deadline that comes first ends the wait below.
        if now >= window_end and window_end <= deadline:
            _freeze(
                state,
                SOURCE_UNREACHABLE,
                {"error": text, "since": state.error_since, "window_seconds": spec.transient_window_seconds},
                f"the source did not answer from {state.error_since} for "
                f"{_duration(spec.transient_window_seconds)}: {text}",
                target.link,
                now,
            )
            return
    else:
        state.error = state.error_since = state.error_at = ""
        if reached:
            return
    if state.result is None and now >= deadline:
        last = f"; last observation: {state.observation}" if state.observation else ""
        _freeze(
            state,
            DEADLINE_PASSED,
            {"deadline": spec.deadline, "last_observation": state.observation, "last_error": state.error},
            f"the deadline {spec.deadline} passed with no result{last}",
            target.link,
            now,
        )


def _observed(state: WaitState, text: str, now: datetime) -> None:
    """The observation, stamped when the dispatcher first saw it (so an unchanged one writes nothing)."""
    if state.observation != text:
        state.observation, state.observed_at = text, utc_text(now)


def _observe_run(runtime: Any, spec: WaitSpec, state: WaitState, now: datetime) -> bool:
    target = spec.target
    path = f"repos/{target.repo}/actions/runs/{target.run_id}"
    try:
        run = _gh_api(runtime.host, path, jq=_RUN_JQ)
    except GateTransportError as exc:
        raise _Transient(f"GitHub did not answer GET {path}: {exc}") from None
    except HostError as exc:
        raise _classified(path, str(exc)) from None
    if not isinstance(run, dict) or not run.get("status"):
        raise _Transient(f"GitHub answered GET {path} with no run status")
    status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    if status != _RUN_COMPLETED:
        _observed(state, f"run {target.repo}#{target.run_id} is {status}", now)
        return False
    fact = {
        "status": status,
        "conclusion": conclusion,
        "html_url": str(run.get("html_url") or target.link),
        "created_at": str(run.get("created_at") or ""),
        "run_started_at": str(run.get("run_started_at") or ""),
        "updated_at": str(run.get("updated_at") or ""),
    }
    summary = f"run {target.repo}#{target.run_id} concluded {conclusion or 'with no conclusion'}"
    _observed(state, summary, now)
    _freeze(state, TARGET_REACHED, fact, summary, fact["html_url"], now)
    return True


def _classified(path: str, message: str) -> Exception:
    """A GitHub answer that is not a run: definitive for 404/410 and no access, transient otherwise."""
    match = _HTTP_STATUS_RE.search(message)
    code = (match.group(1) or match.group(2)) if match else ""
    limited = "rate limit" in message.lower()
    if code in {"404", "410"} or (code == "403" and not limited):
        return _Unreachable(f"GitHub answered GET {path} with HTTP {code}: {message}")
    return _Transient(f"GitHub answered GET {path}: {message}")


def _observe_card(runtime: Any, spec: WaitSpec, state: WaitState, now: datetime) -> bool:
    target = spec.target
    try:
        card = runtime.reader.show(target.ref)
    except TaskError as exc:
        if exc.code == "not_found":
            raise _Unreachable(f"card {target.ref} does not exist") from None
        raise _Transient(f"card {target.ref} could not be read: {exc.message}") from None
    column = str(card.get("state") or "")
    if column not in target.states:
        _observed(state, f"{target.ref} is {column}", now)
        return False
    summary = f"{target.ref} reached {column}"
    _observed(state, summary, now)
    _freeze(state, TARGET_REACHED, {"ref": target.ref, "state": column}, summary, "", now)
    return True


def _duration(seconds: int) -> str:
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m" if not rest else f"{seconds}s"


# --- delivery ------------------------------------------------------------------------------------


def _deliver(
    runtime: Any, task: dict[str, Any], spec: WaitSpec, state: WaitState, attempt_id: str
) -> dict[str, Any]:
    """Deliver the frozen result to every address still owed it; the terminal move comes last."""
    ref = task["ref"]
    result = state.result or {}
    postponed: list[str] = []
    for address in wait_card.pending_addresses(spec, state):
        if address == DEPENDENTS:
            answer = _deliver_dependents(runtime, task, result)
        else:
            answer = _deliver_po(runtime, task, spec, result, address)
        status, detail = answer
        if status is None:
            postponed.append(f"{address}: {detail}")
            continue
        state.deliveries[address] = {"status": status, "at": utc_text(utcnow()), "detail": detail}
        # Recorded only after the receiving side took it; a crash before this line repeats the
        # delivery under the same key, and the receiving side makes the repeat a no-op.
        _record(runtime, ref, state)
    if postponed:
        return {
            **_outcome(
                ref, attempt_id, "wait-delivery-postponed", outcome=result.get("outcome"), postponed=postponed
            ),
            "status": "degraded",
            "reason": "a return address did not take the wait's result; it is repeated next tick under the same "
            "key: " + "; ".join(postponed),
        }
    outcome = str(result.get("outcome") or "")
    reached = outcome == TARGET_REACHED
    record = render_outcome_record(ref, spec, state)
    runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        target="done" if reached else "blocked",
        reason=record,
        request_id=delivery_request_id(ref, TERMINAL_MOVE, wait_card.OBSERVER, str(result.get("key") or "")),
        **(
            {}
            if reached
            else {
                "terminal_taxonomy": normalize_terminal_taxonomy(
                    disposition="blocked", blocked_reason="other"
                ).to_record()
            }
        ),
        wait_outcome=outcome,
    )
    return _outcome(
        ref,
        attempt_id,
        "wait-target-reached" if reached else "wait-ended",
        outcome=outcome,
        summary=result.get("summary"),
        state="done" if reached else "blocked",
    )


def _deliver_po(
    runtime: Any, task: dict[str, Any], spec: WaitSpec, result: dict[str, Any], address: str
) -> tuple[str | None, str]:
    """One input to the named PO session. `(None, why)` postpones it; the service is not answering."""
    ref = task["ref"]
    session_id = address[len(wait_card.PO_SESSION_PREFIX) :]
    request_id = delivery_request_id(ref, PO_DELIVERY, address, str(result.get("key") or ""))
    try:
        runtime.po.submit(
            session_id=session_id,
            text=render_po_input(ref, spec, result),
            request_id=request_id,
            source=DISPATCHER_SOURCE,
            card=card_facts(
                card_ref=ref,
                kind=WAIT_KIND,
                touches_production=None,
                sprint_ref=str(task.get("sprint") or ""),
                input=WAIT_OUTCOME_INPUT,
            ),
        )
    except (ServiceUnavailable, OutcomeUnknown) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except ServiceRefused as exc:
        if exc.code in _UNANSWERED_CODES:
            return None, f"{exc.code}: {exc}"
        return REFUSED, f"the PO service refused it ({exc.code}): {exc}"
    except RequestConflict as exc:
        # This key already carries an input: the one an earlier tick submitted before it lost the answer.
        if _already_submitted(runtime, request_id, session_id) is not None:
            return ACCEPTED, request_id
        return REFUSED, f"request id {request_id} is bound to another input: {exc}"
    except (SessionClosed, SessionNotFound) as exc:
        return REFUSED, f"PO session {session_id} takes no input: {exc}"
    except (PoServiceError, PoStoreError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return ACCEPTED, request_id


def _deliver_dependents(runtime: Any, task: dict[str, Any], result: dict[str, Any]) -> tuple[str | None, str]:
    """One comment on each card held by this wait; on another outcome, a Ready one is Blocked too."""
    ref = task["ref"]
    key = str(result.get("key") or "")
    outcome = str(result.get("outcome") or "")
    try:
        cards = runtime.reader.list(states=set(_DEPENDENT_STATES))
        dependents = [card for card in cards if ref in _blocker_refs(card) and card.get("ref") != ref]
        for dependent in sorted(dependents, key=lambda card: str(card.get("ref") or "")):
            other = str(dependent["ref"])
            body = render_dependent_comment(ref, result)
            runtime.writer.comment(
                role="dispatcher",
                actor=runtime.owner,
                reference=other,
                body=body,
                request_id=delivery_request_id(ref, DEPENDENT_COMMENT, other, key),
            )
            if outcome != TARGET_REACHED and dependent.get("state") == "ready":
                runtime.writer.move(
                    role="dispatcher",
                    actor=runtime.owner,
                    reference=other,
                    target="blocked",
                    reason=f"blocked by wait card {ref}, which ended {outcome}: {result.get('summary') or ''}",
                    request_id=delivery_request_id(ref, DEPENDENT_BLOCKED, other, key),
                    terminal_taxonomy=normalize_terminal_taxonomy(
                        disposition="blocked", blocked_reason="other"
                    ).to_record(),
                    wait_outcome=outcome,
                )
    except TaskError as exc:
        return None, f"{exc.code}: {exc.message}"
    return ACCEPTED, ", ".join(str(card["ref"]) for card in dependents) or "(none)"


def _result_lines(spec: WaitSpec, result: dict[str, Any]) -> list[str]:
    lines = [
        f"Target: {spec.target.describe()}",
        f"Outcome: {result.get('outcome')}",
        f"Result: {result.get('summary') or ''}",
    ]
    fact = result.get("fact") if isinstance(result.get("fact"), dict) else {}
    if spec.target.kind == TARGET_RUN and result.get("outcome") == TARGET_REACHED:
        lines.append(f"Conclusion: {fact.get('conclusion') or 'none'}")
        for name in ("created_at", "run_started_at", "updated_at"):
            if fact.get(name):
                lines.append(f"{name}: {fact[name]}")
    if result.get("evidence"):
        lines.append(f"Evidence: {result['evidence']}")
    return lines


def render_po_input(reference: str, spec: WaitSpec, result: dict[str, Any]) -> str:
    """The one input a wait's result becomes in a PO session; the same result always renders the same."""
    lines = [
        f"Wait card {reference} ended: {result.get('outcome')}. You named this session as a return address.",
        "",
        f"Card: {reference}",
        *_result_lines(spec, result),
        "",
        "Nothing else is sent for this wait. Decide what follows from it in this turn, or cut a card for it.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def render_dependent_comment(reference: str, result: dict[str, Any]) -> str:
    outcome = str(result.get("outcome") or "")
    follows = (
        "This card is claimable now."
        if outcome == TARGET_REACHED
        else "This card is Blocked with that outcome as its reason."
    )
    lines = [
        f"[wait:{outcome}] {reference}",
        "",
        f"The wait card this card is blocked by ended {outcome}: {result.get('summary') or ''}",
    ]
    if result.get("evidence"):
        lines.append(f"Evidence: {result['evidence']}")
    return "\n".join([*lines, "", follows]) + "\n"


def render_outcome_record(reference: str, spec: WaitSpec, state: WaitState) -> str:
    """The wait card's completion comment, carried by its terminal move."""
    result = state.result or {}
    lines = [f"[wait:{result.get('outcome')}]", "", *_result_lines(spec, result), ""]
    lines.append("Delivered:")
    for address in spec.returns:
        if address == wait_card.OBSERVER:
            lines.append(f"- {address}: this comment and the card's terminal column")
            continue
        record = state.deliveries.get(address) or {}
        lines.append(f"- {address}: {record.get('status') or 'pending'} ({record.get('detail') or ''})")
    return "\n".join(lines).rstrip() + "\n"


def _outcome(ref: str, attempt_id: str, action: str, **fields: Any) -> dict[str, Any]:
    return {
        "status": "ok",
        "step": WAIT_STEP,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": action,
        **fields,
    }


__all__ = [
    "DEPENDENT_BLOCKED",
    "DEPENDENT_COMMENT",
    "PO_DELIVERY",
    "TERMINAL_MOVE",
    "WAIT_STEP",
    "advance_wait_card",
    "claim_wait_card",
    "delivery_request_id",
    "pending_wait_blockers",
    "render_dependent_comment",
    "render_outcome_record",
    "render_po_input",
    "utcnow",
]
