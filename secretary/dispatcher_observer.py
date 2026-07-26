"""Observer-head lifecycle: one head per open sprint, reconciled by the production tick.

An observer is not the interactive secretary session. It is a dispatcher-launched role beside
worker and reviewer: its own head profile, its own workspace, its own terminal handle and the
role-scoped environment `role_env` hands a dispatcher-launched head. It never claims a card and
never appears in a card record, so the per-project claim gate and the card cycle are untouched.

The tick reconciles the observer records against the sprint board on every run:

  open sprint, live pid    -> nothing
  open sprint, dead pid    -> relaunch, counted as a relaunch in the record and in the audit log
  open sprint, no record   -> launch
  closed or gone sprint    -> stop the head and drop the record

A stop the host refused is not a stop. The record then stays as `stop-pending` with its terminal
handle, the tick retries it, and `observer_stopped` is written only once the terminal is actually
gone: dropping the record on a failed stop would leave a live head with nothing pointing at it.

Every lifecycle event is staged on disk before the host call it describes and committed to the log
after it, the same order `TaskWriter` uses for a card. Storage that refuses the commit does not
propagate: the staged copy is what `TaskAudit.reconcile()` repairs later, the record is written
regardless, and the outcome says `audit: pending`. An exception escaping here instead would leave a
terminal that is running with no record pointing at it, and the next tick would open a second head
on the same sprint.

Liveness is the same pid heartbeat the worker/reviewer watchdog uses (`head_process_status` over
`pid_file_path`). A pid file that does not exist yet is not evidence of death: a head that has just
been launched has not written it, so an unknown pid counts as alive for a short grace window and as
dead afterwards.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from secretary.dispatcher_state import now_rfc3339, request_token
from secretary.dispatcher_watchdog import (
    head_process_status,
    initial_output_stall_seconds,
    pid_file_path,
)
from secretary.dispatcher_types import HostError
from secretary.tasks import TaskError

OBSERVER_ROLE = "observer"
OBSERVER_WATCHDOG_KIND = "observer"
# Used only when the head registry carries no `role_defaults.observer` key. Named here rather than
# resolved to the worker's default: an observer must never silently inherit another role's head.
OBSERVER_HEAD_FALLBACK = "codex-observer"
OBSERVER_PROMPT_FILE = "SPRINT.md"
# The role skill the head opens once it is up. What the observer does inside its session is the
# skill's business, not the dispatcher's.
OBSERVER_SKILL = "run-sprint"

# Audit event kinds. Launch and relaunch are distinct kinds rather than one kind with a counter,
# so a respawn after a dead pid is readable in the log without joining it against the record.
EVENT_LAUNCHED = "observer_launched"
EVENT_RELAUNCHED = "observer_relaunched"
EVENT_STOPPED = "observer_stopped"
EVENT_DEFERRED = "observer_launch_deferred"

# A stop that the host refused. The head may still be running, so the record survives with its
# handle and the tick keeps retrying until the terminal is gone.
STATE_STOP_PENDING = "stop-pending"
STATE_PAUSE_STOP_PENDING = "pause-stop-pending"
STATE_STOPPED_BY_PAUSE = "stopped-by-pause"
PENDING_STOP_STATES = (STATE_STOP_PENDING, STATE_PAUSE_STOP_PENDING)


def observer_pid_file(reference: str) -> str:
    return pid_file_path(OBSERVER_WATCHDOG_KIND, reference)


@dataclass
class ObserverRecord:
    """One observer head as the dispatcher last left it."""

    sprint: str
    # Identifies this record, not this sprint. A record is dropped when its sprint closes or
    # vanishes, and the same reference can come back to the board later; without a per-record token
    # the second lifecycle would rebuild the request ids of the first one and its real launch and
    # stop would be deduplicated away as retries.
    generation: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    head: str = ""
    workspace: str = ""
    handle: str = ""
    pid_file: str = ""
    # How many times a head has been brought up for this sprint. 1 is the first launch, every
    # value above it is a respawn after a dead pid, which is what tells the two apart in the record.
    launches: int = 0
    state: str = "pending"
    launched_at: float = 0.0
    last_action: str = ""
    last_action_at: float = 0.0
    deferred_reason: str = ""
    stopped_reason: str = ""
    paused_at: float = 0.0
    run: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "sprint": self.sprint,
            "generation": self.generation,
            "head": self.head,
            "workspace": self.workspace,
            "handle": self.handle,
            "pid_file": self.pid_file,
            "launches": self.launches,
            "state": self.state,
            "launched_at": self.launched_at,
            "last_action": self.last_action,
            "last_action_at": self.last_action_at,
            "deferred_reason": self.deferred_reason,
            "stopped_reason": self.stopped_reason,
            "paused_at": self.paused_at,
            "run": dict(self.run),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "ObserverRecord":
        run = payload.get("run")
        return cls(
            sprint=str(payload.get("sprint") or ""),
            generation=str(payload.get("generation") or "") or uuid.uuid4().hex[:12],
            head=str(payload.get("head") or ""),
            workspace=str(payload.get("workspace") or ""),
            handle=str(payload.get("handle") or ""),
            pid_file=str(payload.get("pid_file") or ""),
            launches=_int(payload.get("launches")),
            state=str(payload.get("state") or "pending"),
            launched_at=_float(payload.get("launched_at")),
            last_action=str(payload.get("last_action") or ""),
            last_action_at=_float(payload.get("last_action_at")),
            deferred_reason=str(payload.get("deferred_reason") or ""),
            stopped_reason=str(payload.get("stopped_reason") or ""),
            paused_at=_float(payload.get("paused_at")),
            run=dict(run) if isinstance(run, dict) else {},
        )


def load_observers(payload: dict[str, Any]) -> dict[str, ObserverRecord]:
    raw = payload.get("observers")
    if not isinstance(raw, dict):
        return {}
    return {
        str(ref): ObserverRecord.from_json(record)
        for ref, record in raw.items()
        if isinstance(record, dict)
    }


def put_observers(payload: dict[str, Any], observers: dict[str, ObserverRecord]) -> None:
    payload["observers"] = {ref: record.to_json() for ref, record in sorted(observers.items())}


def observer_alive(record: ObserverRecord, *, now: float | None = None) -> dict[str, Any]:
    """Whether the head recorded here is still running, and on what evidence.

    `known: False` means the pid file is not readable — a head that has just been launched has not
    written its pid yet. That is not death, so it reads as alive until the grace window the
    watchdog already uses for a pane that has produced no output at all has passed.
    """
    now = time.time() if now is None else now
    status = head_process_status(record.pid_file or observer_pid_file(record.sprint))
    if status.get("known"):
        return {"alive": bool(status.get("alive")), "reason": "pid", "pid_known": True}
    grace = initial_output_stall_seconds()
    if record.launched_at and now - record.launched_at <= grace:
        return {"alive": True, "reason": "pid-not-written-yet", "pid_known": False}
    return {"alive": False, "reason": "pid-file-is-unreadable", "pid_known": False}


def observer_snapshot(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Observer state as an operator reads it, without opening a transcript."""
    rows = []
    for ref, record in sorted(load_observers(payload).items()):
        liveness = observer_alive(record)
        rows.append({
            "sprint": ref,
            "head": record.head,
            "state": record.state,
            "workspace": record.workspace,
            "handle": record.handle,
            "launches": record.launches,
            "alive": liveness["alive"],
            "pid_known": liveness["pid_known"],
            "last_action": record.last_action,
            "last_action_at": record.last_action_at,
            "deferred_reason": record.deferred_reason,
            "stopped_reason": record.stopped_reason,
            "paused": record.paused_at > 0,
        })
    return rows


def reconcile_observers(
    runtime: Any, payload: dict[str, Any], *, pause_mode: str = ""
) -> list[dict[str, Any]]:
    """Bring the observer heads in line with the open sprints. Returns the tick's outcomes.

    Nothing at all happens when there is neither an open sprint nor a tracked observer: no record
    is written, no head call is made, and `payload` is left exactly as it was.
    """
    observers = load_observers(payload)
    try:
        open_sprints = {
            str(sprint.get("ref") or ""): sprint
            for sprint in runtime.sprints.list(statuses={"open"})
            if str(sprint.get("ref") or "")
        }
    except (TaskError, HostError) as exc:
        if not observers:
            return []
        # The board could not be asked. A record is never dropped on that evidence: an unreachable
        # sprint board must not read as a closed sprint and take a live head down with it.
        return [{
            "status": "degraded",
            "step": "observer-reconcile",
            "action": "sprint-board-unavailable",
            "reason": getattr(exc, "message", str(exc)),
        }]
    if not open_sprints and not observers:
        return []

    outcomes: list[dict[str, Any]] = []
    try:
        for ref in sorted(set(observers) - set(open_sprints)):
            outcomes.append(
                _stop_observer(runtime, observers, ref, reason="sprint is no longer open")
            )
        for ref in sorted(open_sprints):
            outcomes.append(_reconcile_open_sprint(runtime, observers, ref, pause_mode=pause_mode))
    finally:
        # Whatever went wrong above, the heads that were started or stopped before it are already
        # real. The records go back into the payload so the caller saves them: a lost record means
        # an unattended terminal and a second head on the same sprint next tick.
        put_observers(payload, observers)
    return outcomes


def _reconcile_open_sprint(
    runtime: Any, observers: dict[str, ObserverRecord], ref: str, *, pause_mode: str
) -> dict[str, Any]:
    record = observers.get(ref)
    if record is not None and record.handle and observer_alive(record)["alive"]:
        if record.state in PENDING_STOP_STATES:
            # The sprint is open again and the head that was to be stopped is still the head of
            # this sprint: the pending stop is moot, so the record reads as running once more.
            record.state = "running"
            record.stopped_reason = ""
            record.paused_at = 0.0
        return {
            "status": "ok",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-live",
            "head": record.head,
            "launches": record.launches,
        }
    if pause_mode == "drain":
        # A drain claims nothing new. A dead observer is a bring-up, so it waits for the resume
        # exactly like a Ready card does. The record is still written: an open sprint has to be
        # readable from outside with its head profile and the reason nothing is running on it,
        # and the resume then relaunches from that same record. Neither the readiness gate nor
        # the host is touched here — only the head profile is resolved, to fill the record.
        return _defer(
            runtime,
            observers,
            ref,
            record,
            head=_observer_head_or_blank(runtime),
            reason="pipeline is draining",
            action="observer-launch-skipped",
        )
    return _launch_observer(runtime, observers, ref, record)


def _observer_head_or_blank(runtime: Any) -> str:
    """The observer profile, or an empty string when the registry cannot name one.

    A registry that cannot answer must not cost the sprint its record: the row is still worth more
    without a head profile than not at all, and the launch path reports the same failure properly.
    """
    try:
        return runtime.catalog.observer_head()
    except HostError:
        return ""


def _launch_observer(
    runtime: Any,
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord | None,
) -> dict[str, Any]:
    record = record or ObserverRecord(sprint=ref)
    relaunch = record.launches > 0
    try:
        head = runtime.catalog.observer_head()
    except HostError as exc:
        return _defer(runtime, observers, ref, record, head="", reason=str(exc))
    readiness = runtime.head_readiness(head)
    if not readiness.launch_allowed:
        return _defer(
            runtime, observers, ref, record, head=head,
            reason=f"head resource {readiness.resource} is {readiness.status}: {readiness.reason}",
            readiness=readiness.to_json(),
        )
    if record.handle:
        # The pid is dead but the pane it ran in can still be there, the shell left behind that
        # `with_pid_heartbeat` exists to tell apart from a live head. Close it before opening the
        # next one, or every respawn leaves a ghost pane in the observer's workspace. A pane that
        # refuses to close parks the relaunch: two heads on one sprint is worse than none.
        if not stop_observer_head(runtime, record):
            return _defer(
                runtime, observers, ref, record, head=head,
                reason="previous observer terminal could not be stopped",
            )
        record.handle = ""
    try:
        # The prompt is rendered from the sprint as it reads right now, never from a copy taken
        # when the sprint was created: goal, DoD, repositories and current card all move.
        sprint = runtime.sprints.show(ref, include_cards=False)
    except (HostError, TaskError) as exc:
        return _defer(
            runtime, observers, ref, record, head=head,
            reason=f"observer bring-up failed: {getattr(exc, 'message', str(exc))}",
        )
    kind = EVENT_RELAUNCHED if relaunch else EVENT_LAUNCHED
    attempt = record.launches + 1
    request_id = observer_request_id(
        "relaunch" if relaunch else "launch", ref, record.generation, attempt
    )
    try:
        # Staged before the host is asked for anything, so a head that comes up while the process
        # dies mid-launch still has its event on disk for `TaskAudit.reconcile()` to pick up.
        event = stage_event(runtime, kind, ref, request_id, {"head": head, "launches": attempt})
    except OSError as exc:
        return _defer(
            runtime, observers, ref, record, head=head,
            reason=f"observer lifecycle event could not be staged: {exc}",
        )
    try:
        launched = runtime.host.prepare_observer(sprint, head, prompt=render_observer_prompt(sprint))
    except (HostError, TaskError) as exc:
        # Nothing came up, so the staged event describes a launch that never happened.
        discard_event(runtime, request_id)
        return _defer(
            runtime, observers, ref, record, head=head,
            reason=f"observer bring-up failed: {getattr(exc, 'message', str(exc))}",
        )
    now = time.time()
    record.head = head
    record.workspace = str(launched.get("workspace") or "")
    record.handle = str(launched.get("handle") or "")
    record.pid_file = str(launched.get("pid_file") or observer_pid_file(ref))
    record.run = launched.get("run") if isinstance(launched.get("run"), dict) else {}
    record.launches += 1
    record.state = "running"
    record.launched_at = now
    record.last_action = "relaunched" if relaunch else "launched"
    record.last_action_at = now
    record.deferred_reason = ""
    record.stopped_reason = ""
    record.paused_at = 0.0
    observers[ref] = record
    if event is not None:
        event["payload"]["workspace"] = record.workspace
    outcome = {
        "status": "ok",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-relaunched" if relaunch else "observer-launched",
        "head": head,
        "workspace": record.workspace,
        "launches": record.launches,
    }
    return _with_audit(outcome, commit_event(runtime, event))


def _defer(
    runtime: Any,
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord | None,
    *,
    head: str,
    reason: str,
    action: str = "observer-launch-deferred",
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Park a launch without losing the sprint or damaging an existing record.

    Only the deferral fields are written: the head, workspace, handle and launch counter of a
    record that already exists stay as they were, so the next tick retries from the same state.
    """
    record = record or ObserverRecord(sprint=ref)
    record.head = record.head or head
    record.state = "deferred"
    record.deferred_reason = reason
    record.last_action = "launch-deferred"
    record.last_action_at = time.time()
    observers[ref] = record
    audited = record_event(
        runtime,
        EVENT_DEFERRED,
        ref,
        observer_request_id("deferred", ref, record.generation, record.launches),
        {"head": record.head, "reason": reason, "launches": record.launches},
    )
    outcome = {
        "status": "skipped",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": action,
        "head": record.head,
        "reason": reason,
    }
    if readiness is not None:
        outcome["readiness"] = readiness
    return _with_audit(outcome, audited)


def _stop_observer(
    runtime: Any, observers: dict[str, ObserverRecord], ref: str, *, reason: str
) -> dict[str, Any]:
    record = observers[ref]
    request_id = observer_request_id("stop", ref, record.generation, record.launches)
    try:
        event = stage_event(
            runtime,
            EVENT_STOPPED,
            ref,
            request_id,
            {"head": record.head, "reason": reason, "launches": record.launches},
        )
    except OSError as exc:
        # The event has to be on disk before the terminal is gone, so an unwritable audit parks
        # the stop rather than performing an unrecordable one. The head stays up and is retried.
        _mark_stop_pending(record, STATE_STOP_PENDING, reason)
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-stop-failed",
            "head": record.head,
            "reason": f"{reason}, and the stop could not be staged in the audit log: {exc}",
        }
    if not stop_observer_head(runtime, record):
        discard_event(runtime, request_id)
        _mark_stop_pending(record, STATE_STOP_PENDING, reason)
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-stop-failed",
            "head": record.head,
            "reason": f"{reason}, and the observer terminal could not be stopped",
        }
    observers.pop(ref)
    outcome = {
        "status": "ok",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-stopped",
        "head": record.head,
        "reason": reason,
    }
    return _with_audit(outcome, commit_event(runtime, event))


def _mark_stop_pending(record: ObserverRecord, state: str, reason: str) -> None:
    """Keep the head on the books after a refused stop, handle included, so the retry can find it."""
    now = time.time()
    record.state = state
    record.stopped_reason = reason
    record.last_action = "stop-failed"
    record.last_action_at = now
    if state == STATE_PAUSE_STOP_PENDING:
        record.paused_at = now


def stop_observer_head(runtime: Any, record: ObserverRecord) -> bool:
    """Stop one observer head. True when nothing of it is left running.

    A record with no terminal handle is already stopped. False means the host refused the request
    and the head must be assumed alive, so the caller keeps the record and retries.
    """
    if not record.handle:
        return True
    try:
        runtime.host.stop_observer(record)
    except HostError:
        return False
    return True


def freeze_observers(runtime: Any, payload: dict[str, Any], *, reason: str) -> dict[str, list[str]]:
    """Stop every observer head and say in the record why it is gone.

    Returns the sprints whose head is down under `stopped` and those whose stop the host refused
    under `failed`. A refused stop keeps its handle and is retried by the frozen tick.
    """
    observers = load_observers(payload)
    if not observers:
        return {"stopped": [], "failed": []}
    stopped: list[str] = []
    failed: list[str] = []
    try:
        for ref, record in sorted(observers.items()):
            if not record.handle:
                continue
            if _stop_for_pause(runtime, ref, record, reason):
                stopped.append(ref)
            else:
                failed.append(ref)
    finally:
        put_observers(payload, observers)
    return {"stopped": stopped, "failed": failed}


def _stop_for_pause(runtime: Any, ref: str, record: ObserverRecord, reason: str) -> bool:
    """Take one head down for a freeze. False leaves it on the books as a pending stop."""
    request_id = observer_request_id("freeze-stop", ref, record.generation, record.launches)
    try:
        event = stage_event(
            runtime,
            EVENT_STOPPED,
            ref,
            request_id,
            {"head": record.head, "reason": reason, "launches": record.launches},
        )
    except OSError:
        _mark_stop_pending(record, STATE_PAUSE_STOP_PENDING, reason)
        return False
    if not stop_observer_head(runtime, record):
        discard_event(runtime, request_id)
        _mark_stop_pending(record, STATE_PAUSE_STOP_PENDING, reason)
        return False
    _mark_stopped_by_pause(record, reason)
    commit_event(runtime, event)
    return True


def _mark_stopped_by_pause(record: ObserverRecord, reason: str) -> None:
    now = time.time()
    record.handle = ""
    record.state = STATE_STOPPED_BY_PAUSE
    record.stopped_reason = reason
    record.paused_at = now
    record.last_action = "stopped-by-pause"
    record.last_action_at = now


def retry_pending_observer_stops(runtime: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Retry the stops the host refused. Returns one row per pending stop it tried.

    The reconciliation pass does not run while the pipeline is frozen, so without this a head the
    freeze failed to stop would sit alive and unattended until the resume.
    """
    observers = load_observers(payload)
    pending = {
        ref: record
        for ref, record in sorted(observers.items())
        if record.state in PENDING_STOP_STATES and record.handle
    }
    if not pending:
        return []
    rows: list[dict[str, Any]] = []
    try:
        for ref, record in pending.items():
            reason = record.stopped_reason
            if record.state == STATE_PAUSE_STOP_PENDING:
                if _stop_for_pause(runtime, ref, record, reason):
                    rows.append(
                        {"sprint": ref, "action": "observer-stopped-by-pause", "reason": reason}
                    )
                else:
                    rows.append({"sprint": ref, "action": "observer-stop-failed", "reason": reason})
                continue
            request_id = observer_request_id("stop", ref, record.generation, record.launches)
            try:
                event = stage_event(
                    runtime,
                    EVENT_STOPPED,
                    ref,
                    request_id,
                    {"head": record.head, "reason": reason, "launches": record.launches},
                )
            except OSError:
                rows.append({"sprint": ref, "action": "observer-stop-failed", "reason": reason})
                continue
            if not stop_observer_head(runtime, record):
                discard_event(runtime, request_id)
                rows.append({"sprint": ref, "action": "observer-stop-failed", "reason": reason})
                continue
            observers.pop(ref)
            commit_event(runtime, event)
            rows.append({"sprint": ref, "action": "observer-stopped", "reason": reason})
    finally:
        put_observers(payload, observers)
    return rows


def resume_observers(payload: dict[str, Any]) -> list[str]:
    """Clear the freeze marks so the next tick brings the observers back.

    Resume does not launch anything itself: the tick's own reconciliation is the single bring-up
    path, and it already knows how to tell a dead head from a live one.
    """
    observers = load_observers(payload)
    if not observers:
        return []
    resumed: list[str] = []
    for ref, record in sorted(observers.items()):
        # A freeze-stop the host refused is cleared too: the sprint is running again, so the head
        # that survived the freeze is the head of this sprint and the tick can find it alive.
        if record.state not in (STATE_STOPPED_BY_PAUSE, STATE_PAUSE_STOP_PENDING):
            continue
        record.state = "pending"
        record.paused_at = 0.0
        record.stopped_reason = ""
        resumed.append(ref)
    put_observers(payload, observers)
    return resumed


def observer_request_id(action: str, reference: str, generation: str, launches: int) -> str:
    """One id per (record, action, launch counter): the same tick retried is the same request."""
    return "-".join(
        ("dispatcher-observer", action, request_token(reference), generation, str(launches))
    )


def stage_event(
    runtime: Any, kind: str, reference: str, request_id: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Put one lifecycle event on durable disk before the host call it describes.

    Returns the staged event for `commit_event`, or None when there is nothing to commit: no audit
    at all, or this request id is already in the log because the tick is a retry. Raises OSError
    when the pending copy cannot be written, which the caller answers by not touching the host.
    """
    audit = getattr(runtime, "audit", None)
    if audit is None or audit.committed_event(request_id) is not None:
        return None
    event = {
        "event_id": "evt_" + uuid.uuid4().hex,
        "schema_version": 1,
        "occurred_at": now_rfc3339(),
        "actor": {"role": "dispatcher", "id": str(getattr(runtime, "owner", "") or "dispatcher")},
        "kind": kind,
        "outcome": "success",
        "task_id": "",
        "ref": reference,
        "backend": {"kind": "dispatcher", "task_id": None, "revision": "n/a"},
        "request_id": request_id,
        "payload": payload,
    }
    audit.stage(request_id, event)
    return event


def commit_event(runtime: Any, event: dict[str, Any] | None) -> bool:
    """Move a staged event into the log. False means the pending copy is what carries it now.

    A refused commit is never raised at the caller: the effect the event describes has already
    happened, and losing the record of it costs more than a late audit line. `TaskAudit.reconcile()`
    appends the pending copy on the next repair pass.
    """
    audit = getattr(runtime, "audit", None)
    if audit is None or event is None:
        return True
    request_id = str(event["request_id"])
    try:
        # Re-staged first: the pending copy then carries the post-effect payload too, so a repair
        # pass writes the same line this commit would have.
        audit.stage(request_id, event)
        audit.append(request_id, event)
    except Exception:
        return False
    return True


def discard_event(runtime: Any, request_id: str) -> None:
    """Drop a staged event whose effect never happened, so no repair pass invents it."""
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return
    try:
        audit.discard(request_id)
    except OSError:
        pass


def record_event(
    runtime: Any, kind: str, reference: str, request_id: str, payload: dict[str, Any]
) -> bool:
    """Stage and commit an event with no host call to protect. False when neither landed."""
    try:
        event = stage_event(runtime, kind, reference, request_id, payload)
    except OSError:
        return False
    return commit_event(runtime, event)


def _with_audit(outcome: dict[str, Any], audited: bool) -> dict[str, Any]:
    """Say in the outcome when the event is only staged, so the tick does not read as clean."""
    if not audited:
        outcome["audit"] = "pending"
        outcome["status"] = "degraded"
    return outcome


def render_observer_prompt(sprint: dict[str, Any]) -> str:
    """The observer's launch document, rendered from the live sprint entity."""
    ref = str(sprint.get("ref") or "")
    repositories = [str(repo) for repo in (sprint.get("repositories") or [])]
    current = str(sprint.get("current_task") or "")
    budget = sprint.get("budget") if isinstance(sprint.get("budget"), dict) else {}
    sections = [
        f"# Sprint {ref}",
        "",
        "You are the observer head of this sprint. You are not the interactive secretary session,",
        "and you are never a worker or a reviewer: you do not claim cards and you do not implement",
        "them. Cards are claimed and executed by the dispatcher independently of you.",
        "",
        "## Goal",
        "",
        str(sprint.get("goal") or "(empty sprint goal)"),
        "",
        "## Definition of Done",
        "",
        str(sprint.get("definition_of_done") or "(empty definition of done)"),
        "",
        "## Repositories",
        "",
        "\n".join(f"- {repo}" for repo in repositories) or "- (none recorded)",
        "",
        "## Current card",
        "",
        current or "(none)",
        "",
        "## Budget",
        "",
        f"total {int(budget.get('total') or 0)} restart events recorded so far",
        "",
        "## Where to start",
        "",
        f"Follow the `{OBSERVER_SKILL}` role skill. Read the live sprint entity with:",
        "",
        f'PYTHONPATH="${{TA_SECRETARY_REPO:-/home/dev/secretary}}${{PYTHONPATH:+:$PYTHONPATH}}" python3 -m secretary sprint show --ref {ref}',
        "",
        f"and the cards linked to it with `python3 -m secretary task list --sprint {ref}`.",
        "",
    ]
    return "\n".join(sections)


def observer_launch_prompt() -> str:
    """Short pointer handed to the head on its command line; the document is in the workspace."""
    return (
        f"You are the sprint observer. The sprint is in {OBSERVER_PROMPT_FILE} at the workspace "
        f"root. Read it first, then follow the `{OBSERVER_SKILL}` role skill."
    )


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
