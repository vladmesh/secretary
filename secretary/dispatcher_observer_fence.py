"""The pre-advance observer fence: a sprint's cards stop before they move, not after.

A declared observer is load-bearing.  The tick used to reconcile records and advance every active
card first and look at observer health afterwards, so a sprint whose observer was dead spent the
whole tick moving cards past the role that was supposed to be watching them, and only then noticed.
This runs first and is pure: it reads the sprint board, the head registry and the observer records,
decides which sprints cannot currently be observed, and returns that set.  It launches nothing,
probes nothing and mutates no card.

What a fence excludes is the affected sprint's own work: its reservations, the projects of its
linked cards, and the cards themselves.  A silent observer on one sprint must not stop another
sprint's projects, and a card with no sprint at all is nobody's to fence.

`{kind: none}` passes.  A sprint that declares no observer is not a sprint whose observer is
missing, and nothing is launched or probed for it.

The fence clears on confirmed adoption: a record for that sprint, on the declared profile, with a
live pid.  The launch happens later in the same tick, so the clearing is normally a later tick's.
"""

from __future__ import annotations

import hashlib
from typing import Any

from secretary.dispatcher_observer import (
    ObserverRecord,
    load_observers,
    observer_alive,
    observer_decision,
    stage_event,
    commit_event,
)
from secretary.dispatcher_state import now_rfc3339
from secretary.dispatcher_types import HostError
from secretary.sprint_observer import KIND_HEAD, KIND_NONE, ObserverMetadataError
from secretary.tasks import TaskError

EVENT_FENCED = "observer_fence_raised"
EVENT_CLEARED = "observer_fence_cleared"

FENCE_STATE = "observer_fence"

# Why the sprint cannot be observed right now. `observer_*` reasons come from the metadata itself
# and are corruption; the rest describe a declared head that is not there.
REASON_NO_RECORD = "observer_not_launched"
REASON_DEAD = "observer_head_dead"
REASON_MISMATCH = "observer_head_mismatch"
REASON_DEFERRED = "observer_launch_deferred"
REASON_ABANDONED = "observer_handle_abandoned"
REASON_BOARD_UNAVAILABLE = "sprint_board_unavailable"


def observer_fence(runtime: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Decide which sprints are fenced this tick, before any card is touched.

    Returns the fenced sprint refs, the projects those sprints hold, the card refs to leave alone,
    and the outcomes to report.  A sprint board that cannot be read fences nothing: an unreachable
    board is not evidence that an observer is dead, and stopping every project on it would turn a
    Kanboard blip into a pipeline-wide halt.
    """
    try:
        open_sprints = {
            str(sprint.get("ref") or ""): sprint
            for sprint in runtime.sprints.list(statuses={"open"})
            if str(sprint.get("ref") or "")
        }
    except (TaskError, HostError) as exc:
        return {
            "sprints": set(), "projects": set(), "refs": set(),
            "outcomes": [{
                "status": "degraded",
                "step": "observer-fence",
                "action": REASON_BOARD_UNAVAILABLE,
                "reason": getattr(exc, "message", str(exc)),
            }],
        }
    observers = load_observers(payload)
    state = payload.get(FENCE_STATE)
    state = dict(state) if isinstance(state, dict) else {}

    fenced: dict[str, dict[str, Any]] = {}
    outcomes: list[dict[str, Any]] = []
    for ref in sorted(open_sprints):
        verdict = _sprint_verdict(runtime, open_sprints[ref], observers.get(ref))
        if verdict is None:
            outcomes.extend(_clear(runtime, state, ref))
            continue
        fenced[ref] = verdict
        outcomes.append(_raise(runtime, state, open_sprints[ref], verdict))

    for ref in sorted(set(state) - set(fenced)):
        # A sprint that closed or vanished while fenced: the fence has nothing left to hold.
        outcomes.extend(_clear(runtime, state, ref))

    if state:
        payload[FENCE_STATE] = state
    else:
        payload.pop(FENCE_STATE, None)

    projects: set[str] = set()
    for ref in fenced:
        projects |= _sprint_projects(open_sprints[ref])
    return {
        "sprints": set(fenced),
        "projects": projects,
        "refs": _fenced_card_refs(runtime, set(fenced), projects),
        "outcomes": outcomes,
    }


def _sprint_verdict(
    runtime: Any, sprint: dict[str, Any], record: ObserverRecord | None,
) -> dict[str, Any] | None:
    """Why this sprint is fenced, or None when it is free to run."""
    try:
        decision = observer_decision(runtime, sprint)
    except ObserverMetadataError as exc:
        return {"reason": exc.reason, "message": exc.message, "head": ""}
    if decision["kind"] == KIND_NONE:
        return None
    if decision["kind"] != KIND_HEAD:
        # An unmigrated row on an installation the cutover has not reached. The old reader is what
        # is in force for it, and fencing it would stop the pipeline for the state it has always
        # been in.
        return None
    head = str(decision["head"])
    if record is None:
        return {
            "reason": REASON_NO_RECORD,
            "message": f"declared observer {head} has not been launched",
            "head": head,
        }
    if record.head and record.head != head:
        return {
            "reason": REASON_MISMATCH,
            "message": f"declared observer {head} is not the running head {record.head}",
            "head": head,
        }
    if record.abandoned_handle:
        return {
            "reason": REASON_ABANDONED,
            "message": f"declared observer {head} has an abandoned terminal from a failed bring-up",
            "head": head,
        }
    if record.state in {"deferred", "pending", "launching"}:
        return {
            "reason": REASON_DEFERRED,
            "message": (
                f"declared observer {head} is not up: {record.deferred_reason or record.state}"
            ),
            "head": head,
        }
    liveness = observer_alive(record)
    if not liveness["alive"]:
        return {
            "reason": REASON_DEAD,
            "message": f"declared observer {head} is not running ({liveness['reason']})",
            "head": head,
        }
    return None


def _raise(
    runtime: Any, state: dict[str, Any], sprint: dict[str, Any], verdict: dict[str, Any],
) -> dict[str, Any]:
    """Open or keep the fence on one sprint, writing the critical fact once per reason."""
    ref = str(sprint.get("ref") or "")
    previous = state.get(ref)
    previous = previous if isinstance(previous, dict) else None
    if previous is not None and previous.get("reason") == verdict["reason"]:
        return {
            "status": "critical",
            "step": "observer-fence",
            "sprint": ref,
            "action": "observer-fenced",
            "observer_reason": verdict["reason"],
            "reason": verdict["message"],
            "since": previous.get("since"),
            "projects": sorted(_sprint_projects(sprint)),
        }
    since = now_rfc3339()
    request_id = _fence_request_id(ref, str(verdict["reason"]), since)
    # Durable before the cards are held back, in the order every other dispatcher effect uses: the
    # log carries why a sprint stopped even if this tick does not live to save its state.
    event = stage_event(
        runtime, EVENT_FENCED, ref, request_id,
        {
            "observer_reason": verdict["reason"],
            "head": verdict.get("head") or "",
            "projects": sorted(_sprint_projects(sprint)),
            "message": verdict["message"],
        },
        outcome="critical",
    )
    audited = commit_event(runtime, event)
    state[ref] = {
        "reason": verdict["reason"], "since": since, "head": verdict.get("head") or "",
        "request_id": request_id,
    }
    outcome = {
        "status": "critical",
        "step": "observer-fence",
        "sprint": ref,
        "action": "observer-fenced",
        "observer_reason": verdict["reason"],
        "reason": verdict["message"],
        "since": since,
        "projects": sorted(_sprint_projects(sprint)),
    }
    if not audited:
        outcome["audit"] = "pending"
    return outcome


def _clear(runtime: Any, state: dict[str, Any], ref: str) -> list[dict[str, Any]]:
    """Drop a fence whose sprint has an adopted observer again. Nothing when it was never up."""
    previous = state.pop(ref, None)
    if not isinstance(previous, dict):
        return []
    request_id = _fence_request_id(ref, "cleared", str(previous.get("since") or ""))
    event = stage_event(
        runtime, EVENT_CLEARED, ref, request_id,
        {"observer_reason": previous.get("reason"), "since": previous.get("since")},
    )
    audited = commit_event(runtime, event)
    outcome = {
        "status": "ok",
        "step": "observer-fence",
        "sprint": ref,
        "action": "observer-fence-cleared",
        "observer_reason": previous.get("reason"),
        "since": previous.get("since"),
    }
    if not audited:
        outcome["audit"] = "pending"
    return [outcome]


def _fence_request_id(ref: str, reason: str, since: str) -> str:
    digest = hashlib.sha256(f"{ref}\n{reason}\n{since}".encode("utf-8")).hexdigest()[:32]
    return f"dispatcher-observer-fence-{digest}"


def _sprint_projects(sprint: dict[str, Any]) -> set[str]:
    return {str(project) for project in sprint.get("reservations") or [] if str(project)}


def _fenced_card_refs(runtime: Any, sprints: set[str], projects: set[str]) -> set[str]:
    """Every card the fenced sprints hold, by their link and by their reserved projects.

    Read once and in full, rather than per sprint, because the set has to be complete: the tick
    hands it to reconciliation, which decides whether a record is orphaned, and a card missing
    from here would have its heads settled while the fence is up. An unreadable board yields
    nothing, and the by-project check on the tick's own task reads still holds.
    """
    if not sprints and not projects:
        return set()
    try:
        cards = runtime.reader.list()
    except (TaskError, HostError):
        return set()
    return {
        str(card.get("ref") or "")
        for card in cards
        if str(card.get("ref") or "")
        and (
            str(card.get("sprint") or "") in sprints
            or (str(card.get("project") or "") and str(card.get("project") or "") in projects)
        )
    }


def fenced_task(fence: dict[str, Any], task: dict[str, Any]) -> bool:
    """Whether one card belongs to a fenced sprint.

    Two ways in, because a card can be linked to the sprint by reference or merely sit in one of
    the projects that sprint reserved. Both are the fenced sprint's work; a card with neither is
    another sprint's, or nobody's, and keeps running.
    """
    if not fence.get("sprints"):
        return False
    if str(task.get("ref") or "") in fence.get("refs", set()):
        return True
    if str(task.get("sprint") or "") in fence.get("sprints", set()):
        return True
    project = str(task.get("project") or "")
    return bool(project) and project in fence.get("projects", set())
