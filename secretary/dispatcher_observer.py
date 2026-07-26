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


def observer_pid_file(reference: str) -> str:
    return pid_file_path(OBSERVER_WATCHDOG_KIND, reference)


@dataclass
class ObserverRecord:
    """One observer head as the dispatcher last left it."""

    sprint: str
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
    for ref in sorted(set(observers) - set(open_sprints)):
        outcomes.append(_stop_observer(runtime, observers, ref, reason="sprint is no longer open"))
    for ref in sorted(open_sprints):
        outcomes.append(_reconcile_open_sprint(runtime, observers, ref, pause_mode=pause_mode))
    put_observers(payload, observers)
    return outcomes


def _reconcile_open_sprint(
    runtime: Any, observers: dict[str, ObserverRecord], ref: str, *, pause_mode: str
) -> dict[str, Any]:
    record = observers.get(ref)
    if record is not None and record.handle and observer_alive(record)["alive"]:
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
        # exactly like a Ready card does.
        return {
            "status": "skipped",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-launch-skipped",
            "reason": "pipeline is draining",
        }
    return _launch_observer(runtime, observers, ref, record)


def _launch_observer(
    runtime: Any,
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord | None,
) -> dict[str, Any]:
    relaunch = record is not None and record.launches > 0
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
    if record is not None and record.handle:
        # The pid is dead but the pane it ran in can still be there, the shell left behind that
        # `with_pid_heartbeat` exists to tell apart from a live head. Close it before opening the
        # next one, or every respawn leaves a ghost pane in the observer's workspace.
        stop_observer_head(runtime, record)
        record.handle = ""
    try:
        # The prompt is rendered from the sprint as it reads right now, never from a copy taken
        # when the sprint was created: goal, DoD, repositories and current card all move.
        sprint = runtime.sprints.show(ref, include_cards=False)
        launched = runtime.host.prepare_observer(sprint, head, prompt=render_observer_prompt(sprint))
    except (HostError, TaskError) as exc:
        return _defer(
            runtime, observers, ref, record, head=head,
            reason=f"observer bring-up failed: {getattr(exc, 'message', str(exc))}",
        )
    now = time.time()
    record = record or ObserverRecord(sprint=ref)
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
    record_event(
        runtime,
        EVENT_RELAUNCHED if relaunch else EVENT_LAUNCHED,
        ref,
        observer_request_id("relaunch" if relaunch else "launch", ref, record.launches),
        {"head": head, "workspace": record.workspace, "launches": record.launches},
    )
    return {
        "status": "ok",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-relaunched" if relaunch else "observer-launched",
        "head": head,
        "workspace": record.workspace,
        "launches": record.launches,
    }


def _defer(
    runtime: Any,
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord | None,
    *,
    head: str,
    reason: str,
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
    record_event(
        runtime,
        EVENT_DEFERRED,
        ref,
        observer_request_id("deferred", ref, record.launches),
        {"head": record.head, "reason": reason, "launches": record.launches},
    )
    outcome = {
        "status": "skipped",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-launch-deferred",
        "head": record.head,
        "reason": reason,
    }
    if readiness is not None:
        outcome["readiness"] = readiness
    return outcome


def _stop_observer(
    runtime: Any, observers: dict[str, ObserverRecord], ref: str, *, reason: str
) -> dict[str, Any]:
    record = observers.pop(ref)
    stop_observer_head(runtime, record)
    record_event(
        runtime,
        EVENT_STOPPED,
        ref,
        observer_request_id("stop", ref, record.launches),
        {"head": record.head, "reason": reason, "launches": record.launches},
    )
    return {
        "status": "ok",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-stopped",
        "head": record.head,
        "reason": reason,
    }


def stop_observer_head(runtime: Any, record: ObserverRecord) -> bool:
    """Stop one observer head. A host that cannot be reached is not a failed tick."""
    if not record.handle:
        return False
    try:
        runtime.host.stop_observer(record)
    except HostError:
        return False
    return True


def freeze_observers(runtime: Any, payload: dict[str, Any], *, reason: str) -> list[str]:
    """Stop every observer head and say in the record why it is gone. Returns the stopped sprints."""
    observers = load_observers(payload)
    if not observers:
        return []
    stopped: list[str] = []
    now = time.time()
    for ref, record in sorted(observers.items()):
        if not record.handle:
            continue
        stop_observer_head(runtime, record)
        record.handle = ""
        record.state = "stopped-by-pause"
        record.stopped_reason = reason
        record.paused_at = now
        record.last_action = "stopped-by-pause"
        record.last_action_at = now
        record_event(
            runtime,
            EVENT_STOPPED,
            ref,
            observer_request_id("freeze-stop", ref, record.launches),
            {"head": record.head, "reason": reason, "launches": record.launches},
        )
        stopped.append(ref)
    put_observers(payload, observers)
    return stopped


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
        if record.state != "stopped-by-pause":
            continue
        record.state = "pending"
        record.paused_at = 0.0
        record.stopped_reason = ""
        resumed.append(ref)
    put_observers(payload, observers)
    return resumed


def observer_request_id(action: str, reference: str, launches: int) -> str:
    return "-".join(("dispatcher-observer", action, request_token(reference), str(launches)))


def record_event(
    runtime: Any, kind: str, reference: str, request_id: str, payload: dict[str, Any]
) -> str:
    """Append one lifecycle event to the durable task audit log, once per request id."""
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return ""
    committed = audit.committed_event(request_id)
    if committed is not None:
        return str(committed.get("event_id") or "")
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
    return audit.append(request_id, event)


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
