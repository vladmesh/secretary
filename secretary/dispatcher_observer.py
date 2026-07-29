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

A bring-up that fails once its terminal is already up is not a headless sprint either. The host
hands the handle back with the failure (`ObserverLaunchAborted`), the record keeps it marked as an
abandoned handle, and the next tick closes that terminal before it opens the replacement. A live
pid behind an abandoned handle does not read as a working observer: that head never got its sprint.

A stop the host refused is not a stop. The record then stays as `stop-pending` with its terminal
handle, the tick retries it, and `observer_stopped` is written only once the terminal is actually
gone: dropping the record on a failed stop would leave a live head with nothing pointing at it.

Every lifecycle event is staged on disk before the host call it describes and committed to the log
after it, the same order `TaskWriter` uses for a card. Storage that refuses the commit does not
propagate: the staged copy is what `TaskAudit.reconcile()` repairs later, the record is written
regardless, and the outcome says `audit: pending`. An exception escaping here instead would leave a
terminal that is running with no record pointing at it, and the next tick would open a second head
on the same sprint.

The record itself is fixed the same way, and for the same reason. A launch intent — sprint,
generation, head, attempt, the workspace and pid file the head will get — is written into the
production state and flushed to disk *before* `prepare_observer` is called, not with the rest of the
tick at the end of it. State that cannot be written means no head is launched at all, so a failing
data plane costs the sprint a tick rather than putting a second head on it; a tick that dies between
the host call and its own end leaves the intent behind, and the next tick resolves it from the pid
file: a live pid is adopted as the head of that sprint (its handle is gone, so the stop goes by
workspace), a pid that is not there yet waits out the same grace window every fresh head gets, and
a dead one is relaunched after the workspace's terminals are closed.

Liveness is the same pid heartbeat the worker/reviewer watchdog uses (`head_process_status` over
`pid_file_path`). A pid file that does not exist yet is not evidence of death: a head that has just
been launched has not written it, so an unknown pid counts as alive for a short grace window and as
dead afterwards.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from secretary.dispatcher_state import now_rfc3339, request_token
from secretary.dispatcher_watchdog import (
    head_process_status,
    initial_output_stall_seconds,
    pid_file_path,
)
from secretary.dispatcher_types import HostError
from secretary.role_skills import skill_delivery
from secretary.tasks import TaskError

OBSERVER_ROLE = "observer"
OBSERVER_WATCHDOG_KIND = "observer"
# Used only when the head registry carries no `role_defaults.observer` key. Named here rather than
# resolved to the worker's default: an observer must never silently inherit another role's head.
OBSERVER_HEAD_FALLBACK = "codex-observer"
OBSERVER_PROMPT_FILE = "SPRINT.md"
# The role skill the head opens once it is up, owned by the `observer` role in
# `skills/manifest.toml`. What the observer does inside its session is the skill's business, not
# the dispatcher's: the prompt points at the file and never restates it.
OBSERVER_SKILL = "observe-sprint"

# A finished Codex TUI keeps its wrapper process alive. Its screen is the positive signal needed
# to nudge it for a new durable card event. A card in Ready, In progress or Validate is always an
# ordinary wait, never an idle queue.
OBSERVER_EVENT_WATCHDOG_DEFAULT_SECONDS = 30 * 60
OBSERVER_WAKE_RETRY_INITIAL_SECONDS = 30
OBSERVER_WAKE_RETRY_MAX_SECONDS = 5 * 60
_OBSERVER_QUEUE_FINISHED_RE = re.compile(r"\bWorked for\s+\d", re.IGNORECASE)

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


class ObserverLaunchAborted(HostError):
    """A bring-up that failed after the head's terminal had already been created.

    An empty `handle` means nothing of that head is left running. A non-empty one means the host
    could not close the terminal it opened, so the head has to be assumed alive: the caller keeps
    the handle in the record and retries the stop. Dropping it would leave a running head with
    nothing pointing at it, and the next tick would put a second head on the same sprint.
    """

    def __init__(
        self,
        message: str,
        *,
        handle: str = "",
        workspace: str = "",
        pid_file: str = "",
        run: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.handle = handle
        self.workspace = workspace
        self.pid_file = pid_file
        self.run = dict(run or {})


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
    # Orca can reissue a handle for the same PTY.  Its leafId remains stable, so keep it beside
    # the launch handle and use it to find the observer's current handle on later ticks.
    leaf: str = ""
    pid_file: str = ""
    # How many times a head has been brought up for this sprint. 1 is the first launch, every
    # value above it is a respawn after a dead pid, which is what tells the two apart in the record.
    launches: int = 0
    # The attempt number of a launch that is on disk but whose outcome is not. Non-zero only in
    # `launching` state: the tick writes it before it calls the host and clears it when the host
    # has answered, so a record found with it set is a bring-up whose tick did not survive.
    pending_launch: int = 0
    # A terminal of this head may still be up. True from the launch intent until a stop confirms
    # the head is gone, so the head is never left running with nothing willing to close it.
    head_possible: bool = False
    # The bring-up may have registered this record's workspace with Orca. Tracked apart from
    # `head_possible` because a bring-up that dies after `worktree create` leaves a workspace and
    # no head: the process is gone, the registration is not, and only the stop gives it back.
    workspace_live: bool = False
    # The recorded terminal is the leftover of a bring-up that failed and could not be closed. Its
    # head never got its prompt, so a live pid there is not a working observer: the terminal has to
    # be closed before this sprint counts as headed again.
    abandoned_handle: bool = False
    state: str = "pending"
    launched_at: float = 0.0
    last_action: str = ""
    last_action_at: float = 0.0
    deferred_reason: str = ""
    stopped_reason: str = ""
    paused_at: float = 0.0
    # The last confirmed queue-end explains why a live head is waiting for a card event.
    idle_since: float = 0.0
    idle_reason: str = ""
    # The event cursor is dispatcher-owned state.  A resume entry stays its established six-field
    # document; the dispatcher advances this cursor only after that entry postdates the event.
    acknowledged_event_id: str = ""
    wake_event_id: str = ""
    # True once a wake intent has been persisted for a terminal send.  This is deliberately
    # separate from the event id: an event first seen while the observer is busy must be checked
    # again as soon as that queue finishes, while a possibly delivered terminal send must not be
    # repeated before its acknowledgement or watchdog recovery window.
    wake_sent: bool = False
    wake_attempts: int = 0
    wake_next_at: float = 0.0
    wake_reason: str = ""
    # Launch failures are retried on the same bounded schedule as terminal nudges.  They are
    # separate because a sprint can be headless without having a linked-card event to wake for.
    launch_attempts: int = 0
    launch_next_at: float = 0.0
    run: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "sprint": self.sprint,
            "generation": self.generation,
            "head": self.head,
            "workspace": self.workspace,
            "handle": self.handle,
            "leaf": self.leaf,
            "pid_file": self.pid_file,
            "launches": self.launches,
            "pending_launch": self.pending_launch,
            "head_possible": self.head_possible,
            "workspace_live": self.workspace_live,
            "abandoned_handle": self.abandoned_handle,
            "state": self.state,
            "launched_at": self.launched_at,
            "last_action": self.last_action,
            "last_action_at": self.last_action_at,
            "deferred_reason": self.deferred_reason,
            "stopped_reason": self.stopped_reason,
            "paused_at": self.paused_at,
            "idle_since": self.idle_since,
            "idle_reason": self.idle_reason,
            "acknowledged_event_id": self.acknowledged_event_id,
            "wake_event_id": self.wake_event_id,
            "wake_sent": self.wake_sent,
            "wake_attempts": self.wake_attempts,
            "wake_next_at": self.wake_next_at,
            "wake_reason": self.wake_reason,
            "launch_attempts": self.launch_attempts,
            "launch_next_at": self.launch_next_at,
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
            leaf=str(payload.get("leaf") or ""),
            pid_file=str(payload.get("pid_file") or ""),
            launches=_int(payload.get("launches")),
            pending_launch=_int(payload.get("pending_launch")),
            # A record written before this field existed carries a handle when a head is up, so
            # the handle is what it falls back to rather than a blanket "nothing is running".
            head_possible=bool(payload.get("head_possible", bool(payload.get("handle")))),
            # Likewise: a record written before this field existed names a workspace only once a
            # bring-up has been through it, and the host reads Orca to tell a registered one from
            # a path it never learned about.
            workspace_live=bool(payload.get("workspace_live", bool(payload.get("workspace")))),
            abandoned_handle=bool(payload.get("abandoned_handle")),
            state=str(payload.get("state") or "pending"),
            launched_at=_float(payload.get("launched_at")),
            last_action=str(payload.get("last_action") or ""),
            last_action_at=_float(payload.get("last_action_at")),
            deferred_reason=str(payload.get("deferred_reason") or ""),
            stopped_reason=str(payload.get("stopped_reason") or ""),
            paused_at=_float(payload.get("paused_at")),
            idle_since=_float(payload.get("idle_since")),
            idle_reason=str(payload.get("idle_reason") or ""),
            acknowledged_event_id=str(payload.get("acknowledged_event_id") or ""),
            wake_event_id=str(payload.get("wake_event_id") or ""),
            wake_sent=bool(payload.get("wake_sent")),
            wake_attempts=_int(payload.get("wake_attempts")),
            wake_next_at=_float(payload.get("wake_next_at")),
            wake_reason=str(payload.get("wake_reason") or ""),
            launch_attempts=_int(payload.get("launch_attempts")),
            launch_next_at=_float(payload.get("launch_next_at")),
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
            "leaf": record.leaf,
            "launches": record.launches,
            "alive": liveness["alive"],
            "pid_known": liveness["pid_known"],
            # A head adopted from a launch intent is watching its sprint, but its terminal handle
            # died with the tick that started it: it is stopped by workspace, not by handle.
            "handle_known": bool(record.handle),
            "abandoned_handle": record.abandoned_handle,
            "last_action": record.last_action,
            "last_action_at": record.last_action_at,
            "deferred_reason": record.deferred_reason,
            "stopped_reason": record.stopped_reason,
            "paused": record.paused_at > 0,
            "idle_since": record.idle_since,
            "idle_reason": record.idle_reason,
            "acknowledged_event_id": record.acknowledged_event_id,
            "wake_event_id": record.wake_event_id,
            "wake_sent": record.wake_sent,
            "wake_attempts": record.wake_attempts,
            "wake_next_at": record.wake_next_at,
            "wake_reason": record.wake_reason,
            "launch_attempts": record.launch_attempts,
            "launch_next_at": record.launch_next_at,
        })
    return rows


def observer_event_watchdog_seconds() -> int:
    """Return the explicit maximum age of an unacknowledged card event."""
    try:
        value = int(
            os.environ.get("SECRETARY_OBSERVER_EVENT_WATCHDOG_SECONDS", "")
            or OBSERVER_EVENT_WATCHDOG_DEFAULT_SECONDS
        )
    except ValueError:
        return OBSERVER_EVENT_WATCHDOG_DEFAULT_SECONDS
    return value if value > 0 else OBSERVER_EVENT_WATCHDOG_DEFAULT_SECONDS


def observer_queue_finished(screen: str) -> bool:
    """Whether a Codex TUI is back at its empty composer after completing a turn.

    `Worked for …` is Codex's completion footer.  Requiring a composer after it avoids treating a
    historical footer in a transcript as the current state.  Codex 0.145 sometimes leaves the
    framed footer as the final visible line instead of repainting the composer, so that final-line
    form is equally terminal.  This is intentionally only a positive signal: an unknown adapter or
    an unreadable screen remains live rather than being guessed idle.
    """
    composer = screen.rfind("›")
    if composer >= 0 and not screen[composer + 1:].strip():
        return bool(_OBSERVER_QUEUE_FINISHED_RE.search(screen[:composer]))
    return bool(re.search(r"\bWorked for\s+\d[^\n]*$", screen.rstrip(), re.IGNORECASE))


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
                _stop_observer(runtime, payload, observers, ref, reason="sprint is no longer open")
            )
        for ref in sorted(open_sprints):
            outcomes.append(
                _reconcile_open_sprint(runtime, payload, observers, ref, pause_mode=pause_mode)
            )
    finally:
        # Whatever went wrong above, the heads that were started or stopped before it are already
        # real. The records go back into the payload so the caller saves them: a lost record means
        # an unattended terminal and a second head on the same sprint next tick.
        put_observers(payload, observers)
    return outcomes


def _reconcile_open_sprint(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    *,
    pause_mode: str,
) -> dict[str, Any]:
    record = observers.get(ref)
    # A record still in `launching` is a bring-up whose tick did not live to record the outcome.
    # It is resolved before anything else, because until it is, neither "a head is running here"
    # nor "this sprint is headless" is known.
    unresolved_intent = record is not None and record.state == "launching"
    if record is not None and unresolved_intent:
        adopted = _adopt_launch_intent(runtime, payload, observers, ref, record)
        if adopted is not None:
            return adopted
        _abandon_launch_intent(runtime, ref, record)
    # A terminal left over from an aborted bring-up is skipped here whatever its pid says: that
    # head never received its sprint, and reading it as the live observer would park the sprint
    # forever on a head that is doing nothing.
    if (
        record is not None
        and not unresolved_intent
        and _head_may_be_running(record)
        and not record.abandoned_handle
        and observer_alive(record)["alive"]
    ):
        if record.state in PENDING_STOP_STATES:
            # The sprint is open again and the head that was to be stopped is still the head of
            # this sprint: the pending stop is moot, so the record reads as running once more.
            record.state = "running"
            record.stopped_reason = ""
            record.paused_at = 0.0
        event = _observer_event_state(runtime, ref, record)
        if event["pending"]:
            return _wake_for_event(runtime, payload, observers, ref, record, event)
        work = _observer_work_state(runtime, ref, record)
        if work["state"] == "idle":
            _mark_idle_grace(record, since=work["since"], reason=work["reason"])
            return {
                "status": "ok",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-idle",
                "head": record.head,
                "launches": record.launches,
                "reason": work["reason"],
            }
        if work["state"] == "waiting":
            _set_observer_state(record, "waiting", reason=work["reason"])
            return {
                "status": "ok",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-waiting",
                "head": record.head,
                "launches": record.launches,
            }
        if record.state in {"waiting", "idle-grace", "idle-recovering"}:
            _set_observer_state(record, "running")
        return {
            "status": "ok",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-live",
            "head": record.head,
            "launches": record.launches,
        }
    # A process recovery still happens, but only for work that an observer has not durably
    # acknowledged.  A completed, quiet queue with no new card event is deliberately left alone.
    if (
        record is not None
        and not unresolved_intent
        and record.launches > 0
        and record.state != STATE_STOPPED_BY_PAUSE
        and record.state != "pending"
    ):
        event = _observer_event_state(runtime, ref, record)
        if not event["pending"]:
            _set_observer_state(record, "idle", reason="no unacknowledged significant card event")
            return {
                "status": "ok",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-idle",
                "head": record.head,
                "launches": record.launches,
                "reason": event["reason"],
            }
    if pause_mode == "drain":
        # A drain claims nothing new. A dead observer is a bring-up, so it waits for the resume
        # exactly like a Ready card does. The record is still written: an open sprint has to be
        # readable from outside with its head profile and the reason nothing is running on it,
        # and the resume then relaunches from that same record. Neither the readiness gate nor
        # the host is touched here — only the head profile is resolved, to fill the record.
        return _defer(
            runtime,
            payload,
            observers,
            ref,
            record,
            head=_observer_head_or_blank(runtime),
            reason="pipeline is draining",
            action="observer-launch-skipped",
            # An intent nobody could resolve stays an intent through the drain: the resume then
            # closes what it may have started instead of opening a head beside it.
            keep_state=unresolved_intent,
            retry=False,
        )
    if (
        record is not None
        and record.state == "deferred"
        and not record.abandoned_handle
        and time.time() < record.launch_next_at
    ):
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-launch-deferred",
            "head": record.head,
            "reason": record.deferred_reason,
        }
    return _launch_observer(runtime, payload, observers, ref, record)


def _observer_event_state(runtime: Any, ref: str, record: ObserverRecord) -> dict[str, Any]:
    """Return the latest meaningful linked-card event and advance a durable acknowledgement.

    The audit is the same source used by sprint resume freshness.  The cursor lives with the
    observer record because it describes dispatcher delivery, not a seventh field in the resume
    document.  An acknowledgement is the append-order relationship between the card event and a
    later durable `resume_recorded` event, not two wall-clock values with second precision.
    """
    try:
        sprint = runtime.sprints.show(ref)
        cards = sprint.get("cards") if isinstance(sprint.get("cards"), list) else []
        refs = {str(card.get("ref") or "") for card in cards if isinstance(card, dict)}
        events = runtime.audit.events()
    except (TaskError, HostError, OSError, ValueError, TypeError):
        return {"known": False, "pending": False, "reason": "linked card audit is unavailable"}
    latest: dict[str, Any] | None = None
    acknowledged: dict[str, Any] | None = None
    for event in events:
        if str(event.get("ref") or "") not in refs or not _significant_card_event(event):
            if str(event.get("ref") or "") == ref and str(event.get("kind") or "") == "resume_recorded":
                # The audit is append-only.  The last linked-card event seen before this record is
                # causally earlier than this resume even when their RFC3339 timestamps are equal.
                if latest is not None:
                    acknowledged = latest
            continue
        latest = event
    if latest is None:
        return {"known": True, "pending": False, "reason": "no significant linked-card event"}
    event_id = str(latest.get("event_id") or latest.get("request_id") or "")
    if not event_id:
        return {"known": False, "pending": False, "reason": "latest card event has no durable id"}
    if acknowledged is not None:
        acknowledged_id = str(acknowledged.get("event_id") or acknowledged.get("request_id") or "")
        if acknowledged_id:
            record.acknowledged_event_id = acknowledged_id
            if record.wake_event_id == acknowledged_id:
                record.wake_event_id = ""
                record.wake_sent = False
                record.wake_attempts = 0
                record.wake_next_at = 0.0
                record.wake_reason = ""
    return {
        "known": True,
        "pending": record.acknowledged_event_id != event_id,
        "event_id": event_id,
        "occurred_at": str(latest.get("occurred_at") or ""),
        "age_seconds": _event_age_seconds(str(latest.get("occurred_at") or "")),
        "reason": "latest significant linked-card event is not acknowledged",
    }


def _significant_card_event(event: dict[str, Any]) -> bool:
    """Routing records and failed guard writes do not ask the observer to make a new decision."""
    return (
        str(event.get("kind") or "") not in {"routing", "sprint_guard_denied"}
        and str(event.get("outcome") or "success") == "success"
    )


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _event_age_seconds(occurred_at: str) -> float:
    occurred = _timestamp(occurred_at)
    if occurred is None:
        return 0.0
    return max(0.0, time.time() - occurred.timestamp())


def _wake_for_event(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Nudge one completed observer turn, with durable dedupe and bounded retry.

    A burst is represented by the newest event id while the first nudge is outstanding.  The
    observer rereads the board, so it consumes the whole burst in one turn instead of one turn per
    event.  A failed nudge is externally visible and waits for an exponential retry window.
    """
    now = time.time()
    event_id = str(event["event_id"])
    overdue = event["age_seconds"] >= observer_event_watchdog_seconds()
    if record.wake_sent and now < record.wake_next_at:
        return {
            "status": "ok",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-wake-pending",
            "head": record.head,
            "event_id": event_id,
        }
    if record.state == "wake-deferred" and now < record.wake_next_at:
        record.wake_event_id = event_id
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-wake-deferred",
            "head": record.head,
            "event_id": event_id,
            "reason": record.wake_reason,
        }
    try:
        status = getattr(runtime.host, "observer_status", lambda _record: {})(record)
    except (HostError, OSError, TypeError, ValueError) as exc:
        return _defer_event_wake(record, ref, event_id, f"observer terminal could not be read: {exc}")
    if not isinstance(status, dict) or not status.get("queue_finished"):
        record.wake_event_id = event_id
        record.wake_sent = False
        record.wake_reason = "observer has not confirmed a completed queue"
        record.wake_next_at = now + observer_event_watchdog_seconds()
        _set_observer_state(record, "waiting", reason=record.wake_reason)
        return {
            "status": "ok",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-wake-watchdog-waiting" if overdue else "observer-wake-waiting",
            "head": record.head,
            "event_id": event_id,
            "reason": record.wake_reason,
        }
    # The pending wake is durable before the terminal receives it. A dispatcher crash after the
    # send therefore conservatively waits for the watchdog instead of creating a duplicate turn.
    record.wake_event_id = event_id
    record.wake_sent = True
    record.wake_next_at = now + observer_event_watchdog_seconds()
    record.wake_reason = "event wake is pending confirmation"
    observers[ref] = record
    if not _persist_quietly(runtime, payload, observers):
        return _defer_event_wake(record, ref, event_id, "observer wake intent could not be persisted")
    try:
        runtime.host.nudge_observer(record)
    except (AttributeError, HostError, OSError, TypeError, ValueError) as exc:
        return _defer_event_wake(record, ref, event_id, f"observer wake failed: {exc}")
    record.wake_event_id = event_id
    record.wake_sent = True
    record.wake_attempts = 0
    record.wake_next_at = now + observer_event_watchdog_seconds()
    record.wake_reason = "observer was nudged for an unacknowledged card event"
    _set_observer_state(record, "running")
    return {
        "status": "ok",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-watchdog-woke" if overdue else "observer-nudged",
        "head": record.head,
        "event_id": event_id,
    }


def _defer_event_wake(record: ObserverRecord, ref: str, event_id: str, reason: str) -> dict[str, Any]:
    record.wake_event_id = event_id
    record.wake_sent = False
    record.wake_attempts += 1
    delay = min(
        OBSERVER_WAKE_RETRY_INITIAL_SECONDS * (2 ** (record.wake_attempts - 1)),
        OBSERVER_WAKE_RETRY_MAX_SECONDS,
    )
    record.wake_next_at = time.time() + delay
    record.wake_reason = f"{reason}; retry in {delay}s"
    _set_observer_state(record, "wake-deferred", reason=record.wake_reason)
    return {
        "status": "degraded",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-wake-deferred",
        "head": record.head,
        "event_id": event_id,
        "reason": record.wake_reason,
    }


def _observer_work_state(runtime: Any, ref: str, record: ObserverRecord) -> dict[str, Any]:
    """Classify the live observer without turning an ordinary card wait into a restart.

    A confirmed completed turn means the observer is idle even while a card is in flight. The
    card's next durable transition, not a periodic idle check, provides its next model turn.
    """
    try:
        runtime.sprints.show(ref)
    except (HostError, TaskError):
        return {"state": "unknown", "reason": "the sprint could not be read for idle recovery"}
    try:
        status = getattr(runtime.host, "observer_status", lambda _record: {})(record)
    except (HostError, OSError, TypeError, ValueError):
        return {"state": "unknown", "reason": "the observer terminal could not be read for idle recovery"}
    if not isinstance(status, dict) or not status.get("queue_finished"):
        return {"state": "unknown", "reason": "the observer terminal has no confirmed completed queue"}
    try:
        last_activity = float(status.get("last_activity"))
    except (TypeError, ValueError):
        return {"state": "unknown", "reason": "the observer terminal has no activity timestamp"}
    age = time.time() - last_activity
    return {
        "state": "idle",
        "since": last_activity,
        "reason": (
            "Codex completed its queue and the observer terminal has been quiet for "
            f"{int(age)}s with no unacknowledged significant card event"
        ),
    }


def _set_observer_state(record: ObserverRecord, state: str, *, reason: str = "") -> None:
    """Update an externally readable work state without touching lifecycle ownership fields."""
    if record.state == state and (not reason or record.idle_reason == reason):
        return
    record.state = state
    record.last_action = state
    record.last_action_at = time.time()


def _mark_idle_grace(record: ObserverRecord, *, since: float, reason: str) -> None:
    """Persist a completed queue while it waits for a new linked-card event."""
    if (
        record.state == "idle-grace"
        and record.idle_since == since
        and record.idle_reason == reason
    ):
        return
    now = time.time()
    record.state = "idle-grace"
    record.idle_since = since
    record.idle_reason = reason
    record.last_action = "idle-grace"
    record.last_action_at = now


def _head_may_be_running(record: ObserverRecord) -> bool:
    """Whether a terminal of this record's head may still be up.

    A launch intent counts. It is written before the host is called, so a record that carries one
    may well have a head behind it, and the workspace is where that head is, handle or no handle.
    """
    return bool(record.handle) or (record.head_possible and bool(record.workspace))


def _needs_teardown(record: ObserverRecord) -> bool:
    """Whether the stop still has something of this record to give back to Orca.

    A head that may be up is one such thing, a workspace the bring-up may have registered is the
    other, and the two do not arrive or leave together: a bring-up that fails after `worktree
    create` leaves the registration behind with no process at all.
    """
    return _head_may_be_running(record) or (record.workspace_live and bool(record.workspace))


def _bring_up_request_id(ref: str, generation: str, attempt: int) -> str:
    """The id `_launch_observer` gave attempt number `attempt`. The first one is a launch."""
    return observer_request_id(
        "relaunch" if attempt > 1 else "launch", ref, generation, attempt
    )


def _abandon_launch_intent(runtime: Any, ref: str, record: ObserverRecord) -> None:
    """Close the books on an intent whose head could not be found alive.

    An attempt the log already carries is a head that came up and has since died: it is spent, and
    the next bring-up is a relaunch with its own request id and its own line. An attempt that never
    got past its pending copy was not observed to launch anything, so it is simply retried as
    itself. Either way the record still says a terminal may be up, so the relaunch closes the
    workspace first.
    """
    attempt = record.pending_launch
    record.state = "pending"
    record.pending_launch = 0
    if not attempt:
        return
    request_id = _bring_up_request_id(ref, record.generation, attempt)
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return
    try:
        committed = audit.committed_event(request_id) is not None
    except Exception:
        committed = False
    if committed:
        record.launches = max(record.launches, attempt)


def _adopt_launch_intent(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
) -> dict[str, Any] | None:
    """Resolve a launch whose tick died before it could record the outcome.

    A pid that is readable and alive is this sprint's head: it is adopted with the attempt number
    the intent reserved, and the event staged before the host call is committed now.

    A head that has not written its pid yet is not a dead one. Inside the grace window the intent
    is simply left as it is: no stop, no replacement, and the next tick asks again. Killing a head
    that is still starting would cut the sprint's one continuous session for no reason.

    Anything else (a pid file still missing past the grace window, a dead pid) is not evidence of a
    head, so None is returned and the caller relaunches, closing whatever the intent may have left
    in the workspace first.
    """
    liveness = observer_alive(record)
    if not liveness["alive"]:
        return None
    if not liveness["pid_known"]:
        # Left exactly as it is, intent and all: the next tick resolves it once the grace window
        # has either produced a pid or run out.
        return {
            "status": "skipped",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-launch-pending",
            "head": record.head,
            "reason": "a launch intent is still within its grace window and has written no pid yet",
            "launches": record.launches,
        }
    attempt = record.pending_launch or record.launches or 1
    now = time.time()
    record.launches = max(record.launches, attempt)
    record.pending_launch = 0
    record.head_possible = True
    record.abandoned_handle = False
    record.state = "running"
    record.last_action = "adopted"
    record.last_action_at = now
    record.deferred_reason = ""
    observers[ref] = record
    audited = commit_staged_event(runtime, _bring_up_request_id(ref, record.generation, attempt))
    _persist_quietly(runtime, payload, observers)
    return _with_audit(
        {
            "status": "ok",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-adopted",
            "reason": "a launch intent was left by a tick that did not finish; its head is alive",
            "head": record.head,
            "launches": record.launches,
        },
        audited,
    )


def _observer_head_or_blank(runtime: Any) -> str:
    """The observer profile, or an empty string when the registry cannot name one.

    A registry that cannot answer must not cost the sprint its record: the row is still worth more
    without a head profile than not at all, and the launch path reports the same failure properly.
    """
    try:
        return runtime.catalog.observer_head()
    except HostError:
        return ""


def observer_skill_delivery(runtime: Any, head: str) -> dict[str, Any]:
    """Whether the observer skill is in the shell this head runs in.

    The shell is the head's own adapter, read from the same registry the launch command is rendered
    from: repointing `role_defaults.observer` at a profile of another shell moves the check with it.
    A registry that cannot name the adapter reads as undelivered — a head brought up without its
    instructions is worse than a sprint that waits for the next tick.
    """
    try:
        shell = str(runtime.catalog.observer_run(head).adapter or "")
    except (HostError, TaskError) as exc:
        return {
            "delivered": False,
            "paths": [],
            "reason": f"the shell of head {head!r} could not be resolved: "
                      f"{getattr(exc, 'message', str(exc))}",
        }
    if not shell:
        return {
            "delivered": False,
            "paths": [],
            "reason": f"head {head!r} names no adapter, so its shell is unknown",
        }
    return skill_delivery(OBSERVER_ROLE, OBSERVER_SKILL, shell)


def _launch_observer(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord | None,
) -> dict[str, Any]:
    record = record or ObserverRecord(sprint=ref)
    relaunch = record.launches > 0
    try:
        head = runtime.catalog.observer_head()
    except HostError as exc:
        return _defer(runtime, payload, observers, ref, record, head="", reason=str(exc))
    readiness = runtime.head_readiness(head)
    if not readiness.launch_allowed:
        return _defer(
            runtime, payload, observers, ref, record, head=head,
            reason=f"head resource {readiness.resource} is {readiness.status}: {readiness.reason}",
            readiness=readiness.to_json(),
        )
    # A head whose shell has no observer skill would come up with a prompt pointing at a file it
    # cannot open, and would improvise a sprint from the entity alone. The launch waits instead,
    # and the record says exactly which file is missing.
    delivery = observer_skill_delivery(runtime, head)
    if not delivery["delivered"]:
        return _defer(
            runtime, payload, observers, ref, record, head=head,
            reason=f"observer role skill is not available to this head: {delivery['reason']}",
        )
    if _head_may_be_running(record):
        # The pid is dead but the pane it ran in can still be there, the shell left behind that
        # `with_pid_heartbeat` exists to tell apart from a live head. Close it before opening the
        # next one, or every respawn leaves a ghost pane in the observer's workspace. A pane that
        # refuses to close parks the relaunch: two heads on one sprint is worse than none.
        if not stop_observer_head(runtime, record):
            return _defer(
                runtime, payload, observers, ref, record, head=head,
                reason="previous observer terminal could not be stopped",
            )
    try:
        # The prompt is rendered from the sprint as it reads right now, never from a copy taken
        # when the sprint was created: goal, DoD, repositories and current card all move.
        sprint = runtime.sprints.show(ref, include_cards=False)
    except (HostError, TaskError) as exc:
        return _defer(
            runtime, payload, observers, ref, record, head=head,
            reason=f"observer bring-up failed: {getattr(exc, 'message', str(exc))}",
        )
    kind = EVENT_RELAUNCHED if relaunch else EVENT_LAUNCHED
    attempt = record.launches + 1
    request_id = _bring_up_request_id(ref, record.generation, attempt)
    try:
        # Staged before the host is asked for anything, so a head that comes up while the process
        # dies mid-launch still has its event on disk for `TaskAudit.reconcile()` to pick up.
        event = stage_event(runtime, kind, ref, request_id, {"head": head, "launches": attempt})
    except OSError as exc:
        return _defer(
            runtime, payload, observers, ref, record, head=head,
            reason=f"observer lifecycle event could not be staged: {exc}",
        )
    intent = _write_launch_intent(runtime, payload, observers, ref, record, head, attempt)
    if intent is not None:
        # State that cannot be written means no head: a launch nobody can record is exactly how a
        # sprint ends up with two of them.
        discard_event(runtime, request_id)
        return _defer(
            runtime, payload, observers, ref, record, head=head,
            reason=f"observer launch intent could not be persisted: {intent}",
        )
    try:
        launched = runtime.host.prepare_observer(
            sprint,
            head,
            prompt=render_observer_prompt(sprint, skill_path=_first_path(delivery)),
        )
    except ObserverLaunchAborted as exc:
        # The bring-up failed with its terminal still up. The staged event is dropped, because no
        # observer was launched, but the handle is kept so the next tick closes that terminal
        # first and only then opens the replacement.
        discard_event(runtime, request_id)
        _clear_launch_intent(record, head_possible=bool(exc.handle))
        if exc.handle:
            record.handle = exc.handle
            record.workspace = exc.workspace or record.workspace
            record.pid_file = exc.pid_file or record.pid_file
            record.run = exc.run or record.run
            record.abandoned_handle = True
        return _defer(
            runtime, payload, observers, ref, record, head=head,
            reason=f"observer bring-up failed: {exc}",
        )
    except (HostError, TaskError) as exc:
        # Nothing came up, so the staged event describes a launch that never happened. A host that
        # got as far as opening a terminal reports it as `ObserverLaunchAborted` above, so there is
        # nothing left to close here either.
        discard_event(runtime, request_id)
        _clear_launch_intent(record, head_possible=False)
        return _defer(
            runtime, payload, observers, ref, record, head=head,
            reason=f"observer bring-up failed: {getattr(exc, 'message', str(exc))}",
        )
    now = time.time()
    record.head = head
    record.workspace = str(launched.get("workspace") or "")
    record.handle = str(launched.get("handle") or "")
    try:
        record.leaf = str(runtime.host.pane_leaf(record.workspace, record.handle) or "")
    except (HostError, OSError, TypeError, ValueError, AttributeError):
        # The returned handle is still enough when an older host cannot expose a leaf.  Retaining
        # the launch is safer than treating an identity lookup failure as a failed bring-up.
        record.leaf = ""
    record.abandoned_handle = False
    record.pid_file = str(launched.get("pid_file") or observer_pid_file(ref))
    record.run = launched.get("run") if isinstance(launched.get("run"), dict) else {}
    record.launches = attempt
    record.pending_launch = 0
    record.head_possible = True
    record.workspace_live = True
    record.state = "running"
    record.launched_at = now
    record.last_action = "relaunched" if relaunch else "launched"
    record.last_action_at = now
    record.deferred_reason = ""
    record.launch_attempts = 0
    record.launch_next_at = 0.0
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
    # A failure here is not a lost head: the intent on disk still names this launch, and the next
    # tick adopts the head it started instead of opening a second one.
    if not _persist_quietly(runtime, payload, observers):
        outcome["state"] = "pending"
        outcome["status"] = "degraded"
    return _with_audit(outcome, commit_event(runtime, event))


def _write_launch_intent(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    head: str,
    attempt: int,
) -> str | None:
    """Fix this launch on disk before the host is called. Returns the failure, or None on success.

    The workspace and the pid file are asked of the host rather than taken from its answer: they
    are pure path arithmetic over the sprint reference, and the answer is exactly what a tick that
    dies mid-launch never gets to see. With them in the record, the next tick can read the head's
    liveness and close its terminal without ever having held its handle.

    The intent also says the workspace may become registered with Orca, and that outlives the
    launch: a bring-up that fails after `worktree create` leaves a registration behind whether or
    not a head ever ran, and the stop is what gives it back.
    """
    previous = record.to_json()
    now = time.time()
    try:
        workspace = record.workspace or str(runtime.host.observer_workspace(ref))
        pid_file = record.pid_file or str(runtime.host.observer_pid_file(ref))
    except Exception as exc:
        # Without the workspace the head could not be found again, and without the pid file its
        # liveness could not be read: an intent that names neither is not worth launching against.
        return f"{type(exc).__name__}: {exc}"
    record.head = head
    record.workspace = workspace
    record.pid_file = pid_file
    record.pending_launch = attempt
    record.head_possible = True
    record.workspace_live = True
    record.state = "launching"
    record.launched_at = now
    record.last_action = "launching"
    record.last_action_at = now
    record.deferred_reason = ""
    record.stopped_reason = ""
    record.paused_at = 0.0
    observers[ref] = record
    try:
        put_observers(payload, observers)
        runtime.production_state.save(payload)
    except Exception as exc:
        for name, value in previous.items():
            setattr(record, name, value)
        return f"{type(exc).__name__}: {exc}"
    return None


def _clear_launch_intent(record: ObserverRecord, *, head_possible: bool) -> None:
    """Take back a launch intent whose host call answered. `head_possible` is what it answered."""
    record.pending_launch = 0
    record.head_possible = head_possible
    record.state = "pending"


def _defer(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord | None,
    *,
    head: str,
    reason: str,
    action: str = "observer-launch-deferred",
    readiness: dict[str, Any] | None = None,
    keep_state: bool = False,
    retry: bool = True,
) -> dict[str, Any]:
    """Park a launch without losing the sprint or damaging an existing record.

    Only the deferral fields are written: the head, workspace, handle and launch counter of a
    record that already exists stay as they were, so the next tick retries from the same state.
    `keep_state` leaves even the state alone, for a record whose unresolved launch intent is the
    one thing the next tick must still see. Failed launches use a persisted exponential deadline;
    a deliberate drain is retried when the drain ends rather than on that deadline.
    """
    record = record or ObserverRecord(sprint=ref)
    record.head = record.head or head
    if not keep_state:
        record.state = "deferred"
    if retry:
        record.launch_attempts += 1
        delay = min(
            OBSERVER_WAKE_RETRY_INITIAL_SECONDS * (2 ** (record.launch_attempts - 1)),
            OBSERVER_WAKE_RETRY_MAX_SECONDS,
        )
        record.launch_next_at = time.time() + delay
        record.deferred_reason = f"{reason}; retry in {delay}s"
    else:
        record.launch_attempts = 0
        record.launch_next_at = 0.0
        record.deferred_reason = reason
    record.last_action = "launch-deferred"
    record.last_action_at = time.time()
    observers[ref] = record
    audited = record_event(
        runtime,
        EVENT_DEFERRED,
        ref,
        observer_request_id("deferred", ref, record.generation, record.launches),
        {"head": record.head, "reason": record.deferred_reason, "launches": record.launches},
    )
    outcome = {
        "status": "skipped",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": action,
        "head": record.head,
        "reason": record.deferred_reason,
    }
    if readiness is not None:
        outcome["readiness"] = readiness
    _persist_quietly(runtime, payload, observers)
    return _with_audit(outcome, audited)


def _stop_observer(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    *,
    reason: str,
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
    _persist_quietly(runtime, payload, observers)
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

    A record that points at neither a terminal, nor a workspace with a head possibly in it, nor a
    workspace its bring-up may have registered is already stopped. False means the host refused the
    request and the head must be assumed alive, so the caller keeps the record and retries.
    """
    if not _needs_teardown(record):
        return True
    try:
        runtime.host.stop_observer(record)
    except HostError:
        return False
    record.handle = ""
    record.head_possible = False
    record.workspace_live = False
    record.abandoned_handle = False
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
            if not _head_may_be_running(record):
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
    record.abandoned_handle = False
    record.state = STATE_STOPPED_BY_PAUSE
    record.stopped_reason = reason
    record.paused_at = now
    record.last_action = "stopped-by-pause"
    record.last_action_at = now


def retry_pending_observer_stops(runtime: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Retry the stops the host refused. Returns one row per pending stop it tried.

    The reconciliation pass does not run while the pipeline is frozen, so without this a head the
    freeze failed to stop would sit alive and unattended until the resume.

    Each row carries a `status` like any other action outcome, so a stop the host refused again
    reads as a `degraded` action to the frozen tick that called this and lands in its telemetry.
    Without it the retry failed silently into a row nobody classified, and the freeze recorded
    itself as a healthy terminal tick over a head it could not take down (secretary-833 review,
    round 4).
    """
    observers = load_observers(payload)
    pending = {
        ref: record
        for ref, record in sorted(observers.items())
        if record.state in PENDING_STOP_STATES and _needs_teardown(record)
    }
    if not pending:
        return []
    rows: list[dict[str, Any]] = []
    try:
        for ref, record in pending.items():
            reason = record.stopped_reason
            if record.state == STATE_PAUSE_STOP_PENDING:
                if _stop_for_pause(runtime, ref, record, reason):
                    rows.append(_retry_row(ref, "observer-stopped-by-pause", reason, ok=True))
                else:
                    rows.append(_retry_row(ref, "observer-stop-failed", reason))
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
            except OSError as exc:
                rows.append(_retry_row(
                    ref, "observer-stop-failed",
                    f"{reason}, and the stop could not be staged in the audit log: {exc}",
                ))
                continue
            if not stop_observer_head(runtime, record):
                discard_event(runtime, request_id)
                rows.append(_retry_row(
                    ref, "observer-stop-failed",
                    f"{reason}, and the observer terminal could not be stopped",
                ))
                continue
            observers.pop(ref)
            commit_event(runtime, event)
            rows.append(_retry_row(ref, "observer-stopped", reason, ok=True))
    finally:
        put_observers(payload, observers)
    return rows


def _retry_row(ref: str, action: str, reason: str, *, ok: bool = False) -> dict[str, Any]:
    return {
        "status": "ok" if ok else "degraded",
        "step": "observer-stop-retry",
        "sprint": ref,
        "action": action,
        "reason": reason,
    }


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


def _persist_quietly(
    runtime: Any, payload: dict[str, Any], observers: dict[str, ObserverRecord]
) -> bool:
    """Flush the observer records mid-tick. False means only the tick's own save carries them now.

    Never raised at the caller: the head this record describes is already up or already gone, and
    the launch intent that precedes every bring-up is what keeps that decision recoverable.
    """
    put_observers(payload, observers)
    try:
        runtime.production_state.save(payload)
    except Exception:
        return False
    return True


def commit_staged_event(runtime: Any, request_id: str) -> bool:
    """Append an event that was staged by a tick which did not live to commit it.

    False when the log has neither a committed nor a pending copy, or refuses the append: the
    outcome then says `audit: pending` rather than claiming a line that is not in the log.
    """
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return True
    try:
        if audit.committed_event(request_id) is not None:
            return True
        event = audit.pending_event(request_id)
        if event is None:
            return False
        audit.append(request_id, event)
    except Exception:
        return False
    return True


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


def _first_path(delivery: dict[str, Any]) -> str:
    paths = delivery.get("paths") or []
    return str(paths[0]) if paths else ""


def render_observer_prompt(sprint: dict[str, Any], *, skill_path: str = "") -> str:
    """The observer's launch document: the sprint entity, and the skill that says what to do.

    The document carries data and one pointer. How a sprint is led belongs to the role skill alone,
    because two texts about the same job drift apart and the head then follows the stale one.
    """
    ref = str(sprint.get("ref") or "")
    repositories = [str(repo) for repo in (sprint.get("repositories") or [])]
    current = str(sprint.get("current_task") or "")
    budget = sprint.get("budget") if isinstance(sprint.get("budget"), dict) else {}
    sections = [
        f"# Sprint {ref}",
        "",
        f"You are the observer head of this sprint. Your instructions are the `{OBSERVER_SKILL}`",
        "role skill and nothing else in this file:",
        "",
        f"- {skill_path or OBSERVER_SKILL}",
        "",
        "Read it first. Everything below is the sprint entity as the board holds it right now; the",
        "live copy is `python3 -m secretary sprint show --ref " + ref + "`.",
        "",
        "## Reference",
        "",
        ref or "(unknown sprint reference)",
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
        "## Status",
        "",
        str(sprint.get("status") or "(unknown)"),
        "",
        "## Current card",
        "",
        current or "(none)",
        "",
        "## Budget",
        "",
        f"total {int(budget.get('total') or 0)} restart events recorded so far",
        "Signal threshold reached." if budget.get("signal_reached") else "Signal threshold not reached.",
        "Hard threshold reached." if budget.get("hard_reached") else "Hard threshold not reached.",
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
