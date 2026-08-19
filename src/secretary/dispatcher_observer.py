"""Observer-head lifecycle: one head per open sprint, reconciled by the production tick.

An observer is a dispatcher-launched role beside worker and reviewer, with its own head profile,
workspace, terminal handle and role-scoped environment. It never claims a card and never appears
in a card record, so the per-project claim gate and the card cycle are untouched.

The tick reconciles the observer records against the sprint board on every run:

  open sprint, live pid    -> nothing
  open sprint, dead pid    -> relaunch, counted as a relaunch in the record and in the audit log
  open sprint, no record   -> launch
  closed or gone sprint    -> stop the head and drop the record

A bring-up that fails once its terminal is already up is not a headless sprint: the host hands
the handle back with the failure (`ObserverLaunchAborted`), the record keeps it marked as an
abandoned handle, and the next tick closes that terminal before opening the replacement. A stop
the host refused is not a stop either: the record stays `stop-pending` and `observer_stopped` is
written only once the terminal is actually gone.

Every lifecycle event is staged on disk before the host call it describes and committed to the
log after it, the same order `TaskWriter` uses for a card. Storage that refuses the commit does
not propagate: the staged copy is what `TaskAudit.reconcile()` repairs later, the record is
written regardless, and the outcome says `audit: pending`.

The record itself is fixed the same way. A launch intent — sprint, generation, head, attempt,
workspace and pid file — is written into the production state and flushed to disk *before*
`prepare_observer` is called, so state that cannot be written costs the sprint a tick rather
than putting a second head on it, and a tick that dies mid-launch leaves an intent the next tick
resolves from the pid file.

Liveness is the same pid heartbeat the worker and reviewer get. A pid file that does not exist
yet is not evidence of death: an unknown pid counts as alive for a short grace window.

The acknowledgement deadline says how long one sent delivery may stay unacknowledged before it
is sent again; it is armed by the delivery and never compared against the age of the event. A
current Codex HeadRun uses its own persisted provider cursor while the pane is non-idle: fresh
progress outranks `tui-idle`. The legacy turn ceiling remains only for observers with no
attested provider source.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from secretary.codex_provider_events import CodexProviderSourceError
from secretary.dispatcher_launch import merge_launch_head_run
from secretary.dispatcher_state import now_rfc3339, request_token
from secretary.dispatcher_tui import (
    COMPOSER_EMPTY,
    COMPOSER_UNKNOWN,
    READINESS_BUSY,
    delivery_readiness_state,
)
from secretary.dispatcher_types import HostError
from secretary.dispatcher_watchdog import (
    head_run_process_status,
    heartbeat_is_live_match,
    heartbeat_is_mismatch,
    initial_output_stall_seconds,
    pid_file_path,
)
from secretary.infra.env import positive_int
from secretary.dispatcher_worker_lifecycle import (
    CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS,
    ContinuationLivenessState,
    WorkerContinuationLiveness,
    head_run_binding,
)
from secretary.role_env import observer_binding
from secretary.role_skills import skill_delivery
from secretary.sprint_observer import (
    KIND_HEAD,
    KIND_NONE,
    REASON_UNKNOWN_PROFILE,
    ObserverMetadataError,
    executable_observer,
)
from secretary.tasks import TaskError, is_significant_observer_event
from triggered_agents.runtime import head as head_ops
from triggered_agents.runtime.codex_preflight import CodexFanoutRecordingError

OBSERVER_ROLE = "observer"
OBSERVER_PID_KIND = "observer"
# Used only when the head registry carries no `role_defaults.observer` key. Named here rather than
# resolved to the worker's default: an observer must never silently inherit another role's head.
OBSERVER_HEAD_FALLBACK = "codex-observer"
OBSERVER_PROMPT_FILE = "SPRINT.md"
# The role skill the head opens once it is up, owned by the `observer` role in
# `skills/manifest.toml`. What the observer does inside its session is the skill's business, not
# the dispatcher's: the prompt points at the file and never restates it.
OBSERVER_SKILL = "observe-sprint"

# A head that has finished its turn keeps its wrapper process alive. Orca reporting the pane ready
# for input is the positive signal needed to nudge it for a new durable card event. A card in
# Ready, In progress or Validate is always an ordinary wait, never an idle head.
#
# How long one delivery may stay unacknowledged before it is sent again. Armed by the delivery, so
# it measures the silence of this head on this batch and nothing else.
OBSERVER_ACK_DEADLINE_DEFAULT_SECONDS = 30 * 60
# Compatibility ceiling for observers without an attested provider-progress source.  A current
# Codex HeadRun never consults it: its exact provider cursor drives the durable no-progress ladder.
# Keep this for historical non-Codex/no-source records until they leave the fleet.
OBSERVER_TURN_CEILING_DEFAULT_SECONDS = 3 * 60 * 60
OBSERVER_WAKE_RETRY_INITIAL_SECONDS = 30
OBSERVER_WAKE_RETRY_MAX_SECONDS = 5 * 60
# How many refused deliveries of one batch are retried on the live head before the sprint pays for
# a replacement instead. Backoff alone would keep an unreachable head forever.
OBSERVER_WAKE_MAX_ATTEMPTS_DEFAULT = 3

# Audit event kinds. Launch and relaunch are distinct kinds rather than one kind with a counter,
# so a respawn after a dead pid is readable in the log without joining it against the record.
EVENT_LAUNCHED = "observer_launched"
EVENT_RELAUNCHED = "observer_relaunched"
EVENT_STOPPED = "observer_stopped"
EVENT_DEFERRED = "observer_launch_deferred"
EVENT_PROVIDER_FANOUT_BLOCKED = "observer_codex_provider_fanout_blocked"

# A stop that the host refused. The head may still be running, so the record survives with its
# handle and the tick keeps retrying until the terminal is gone.
STATE_STOP_PENDING = "stop-pending"
STATE_PAUSE_STOP_PENDING = "pause-stop-pending"
STATE_STOPPED_BY_PAUSE = "stopped-by-pause"
PENDING_STOP_STATES = (STATE_STOP_PENDING, STATE_PAUSE_STOP_PENDING)


def observer_pid_file(reference: str) -> str:
    return pid_file_path(OBSERVER_PID_KIND, reference)


class ObserverLaunchAborted(HostError):
    """A bring-up that failed after the head's terminal had already been created.

    An empty `handle` means nothing of that head is left running. A non-empty one means the host
    could not close the terminal it opened, so the head is assumed alive: the caller keeps the
    handle in the record and retries the stop.
    """

    def __init__(
        self,
        message: str,
        *,
        handle: str = "",
        leaf: str = "",
        workspace: str = "",
        pid_file: str = "",
        run: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.handle = handle
        self.leaf = leaf
        self.workspace = workspace
        self.pid_file = pid_file
        self.run = dict(run or {})
        # What the delivery boundary saw of the prompt this bring-up failed to hand over, so the
        # sprint keeps the evidence of a launch delivery exactly as it keeps a wake's.
        self.evidence = dict(evidence or {})


class DeliveryStage(str, Enum):
    """The durable state of one observer event-delivery batch."""

    IDLE = "idle"
    WAITING_FOR_IDLE = "waiting_for_idle"
    DELIVERY_INTENT = "delivery_intent"
    AWAITING_ACK = "awaiting_ack"
    RETRY_DEFERRED = "retry_deferred"


class ObserverWakeLiveness(WorkerContinuationLiveness):
    """The exact-run provider-progress episode for one observer event batch.

    Records no provider text and is never rebound from a workspace scan: a new observer HeadRun
    opens a new episode, while a recovered episode accepts only the binding it already names.
    """


@dataclass
class ObserverDelivery:
    """One causal delivery cursor, independent of observer process lifecycle.

    `through_event` is fixed before a terminal send or replacement launch, and events appended after
    the intent are deliberately left for the next batch.
    """

    stage: DeliveryStage = DeliveryStage.IDLE
    acknowledged_through: str = ""
    acknowledged_delivery_id: str = ""
    acknowledged_resume_id: str = ""
    pending_from: str = ""
    delivery_id: str = ""
    method: str = ""
    through_event: str = ""
    # The newest resume event known when the intent was persisted.  An empty cursor is meaningful:
    # it says there was no resume yet, so the first one appended afterwards can acknowledge this
    # delivery.  Legacy records set `resume_cursor_known` false and fail closed until the
    # acknowledgement deadline.
    resume_cursor: str = ""
    resume_cursor_known: bool = True
    sent_at: float = 0.0
    # When this batch started being held by a head that has not been seen ready for input.  The
    # turn ceiling is measured from here, so a batch waiting on a head that never goes idle is
    # bounded whether or not the prompt was ever delivered.
    held_since: float = 0.0
    deadline: float = 0.0
    attempts: int = 0
    next_at: float = 0.0
    reason: str = ""
    # Everything below is sprint evidence rather than delivery state, and that is the difference
    # that matters: `attempts` is the current batch's bounded retry counter and is reset by an
    # acknowledgement, by a replacement head and by the next batch, while these are cumulative
    # over the whole sprint and are never cleared. Without them a sprint whose wakes were refused
    # three times and then acknowledged reads, at closeout, exactly like one where every wake
    # landed first time — which is what sprint:1402 reported.
    #
    # They are scoped to the observer's own wakes on purpose. A reviewer that failed to come up on
    # a card is a different subject with its own counters on the card's record, and the two were
    # being read as one: "the reviewer launched fine" is not an answer about whether the observer
    # was ever reached.
    wake_attempts: int = 0
    wake_failures: int = 0
    launch_delivery_attempts: int = 0
    launch_delivery_failures: int = 0
    last_failure_reason: str = ""
    last_failure_at: float = 0.0
    last_failure_method: str = ""
    # The bounded, content-free evidence of the last wake that was attempted: terminal identity,
    # payload size and hash, the stage it reached, the composer and output fingerprints around it
    # and why it stopped. Never the prompt.
    last_evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "acknowledged_through": self.acknowledged_through,
            "acknowledged_delivery_id": self.acknowledged_delivery_id,
            "acknowledged_resume_id": self.acknowledged_resume_id,
            "pending_from": self.pending_from,
            "delivery_id": self.delivery_id,
            "method": self.method,
            "through_event": self.through_event,
            "resume_cursor": self.resume_cursor,
            "resume_cursor_known": self.resume_cursor_known,
            "sent_at": self.sent_at,
            "held_since": self.held_since,
            "deadline": self.deadline,
            "attempts": self.attempts,
            "next_at": self.next_at,
            "reason": self.reason,
            "wake_attempts": self.wake_attempts,
            "wake_failures": self.wake_failures,
            "launch_delivery_attempts": self.launch_delivery_attempts,
            "launch_delivery_failures": self.launch_delivery_failures,
            "last_failure_reason": self.last_failure_reason,
            "last_failure_at": self.last_failure_at,
            "last_failure_method": self.last_failure_method,
            "last_evidence": dict(self.last_evidence),
        }

    @classmethod
    def from_json(cls, payload: Any) -> ObserverDelivery:
        if not isinstance(payload, dict):
            return cls()
        try:
            stage = DeliveryStage(str(payload.get("stage") or DeliveryStage.IDLE.value))
        except ValueError:
            stage = DeliveryStage.IDLE
        return cls(
            stage=stage,
            acknowledged_through=str(payload.get("acknowledged_through") or ""),
            acknowledged_delivery_id=str(payload.get("acknowledged_delivery_id") or ""),
            acknowledged_resume_id=str(payload.get("acknowledged_resume_id") or ""),
            pending_from=str(payload.get("pending_from") or ""),
            delivery_id=str(payload.get("delivery_id") or ""),
            method=str(payload.get("method") or ""),
            through_event=str(payload.get("through_event") or ""),
            resume_cursor=str(payload.get("resume_cursor") or ""),
            resume_cursor_known=bool(payload.get("resume_cursor_known", True)),
            sent_at=_float(payload.get("sent_at")),
            held_since=_float(payload.get("held_since")),
            deadline=_float(payload.get("deadline")),
            attempts=_int(payload.get("attempts")),
            next_at=_float(payload.get("next_at")),
            reason=str(payload.get("reason") or ""),
            wake_attempts=_int(payload.get("wake_attempts")),
            wake_failures=_int(payload.get("wake_failures")),
            launch_delivery_attempts=_int(payload.get("launch_delivery_attempts")),
            launch_delivery_failures=_int(payload.get("launch_delivery_failures")),
            last_failure_reason=str(payload.get("last_failure_reason") or ""),
            last_failure_at=_float(payload.get("last_failure_at")),
            last_failure_method=str(payload.get("last_failure_method") or ""),
            last_evidence=(
                dict(payload["last_evidence"])
                if isinstance(payload.get("last_evidence"), dict)
                else {}
            ),
        )


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
    # This record's bring-up rendered the sprint binding into its head's command line. False on a
    # record written before the binding existed, which is the only fact that tells a head able to
    # write from one whose every write is refused as `observer_identity_unbound`: the process
    # carries the binding or it does not, and no probe of a running head can ask it.
    bound: bool = False
    state: str = "pending"
    launched_at: float = 0.0
    last_action: str = ""
    last_action_at: float = 0.0
    deferred_reason: str = ""
    stopped_reason: str = ""
    paused_at: float = 0.0
    # The last time the pane was confirmed ready explains why a live head is waiting for a card
    # event.
    idle_since: float = 0.0
    idle_reason: str = ""
    # The event cursor is dispatcher-owned state. A resume entry stays its established six-field
    # document; this delivery machine carries the causal acknowledgement for its fixed batch.
    delivery: ObserverDelivery = field(default_factory=ObserverDelivery)
    # Launch failures are retried on the same bounded schedule as terminal nudges.  They are
    # separate because a sprint can be headless without having a linked-card event to wake for.
    launch_attempts: int = 0
    launch_next_at: float = 0.0
    run: dict[str, Any] = field(default_factory=dict)
    # `run` is routing telemetry.  This is the actual runtime HeadRun used to bind the heartbeat.
    head_run: dict[str, Any] = field(default_factory=dict)
    # Exact-HeadRun provider progress for an event delivery held by this observer.  A missing or
    # malformed value is historical/unknown, never permission to bind today's workspace journal.
    wake_liveness: ObserverWakeLiveness = field(default_factory=ObserverWakeLiveness)
    # The terminal episode of the immediately retired HeadRun.  This is audit-only: all decisions
    # use ``wake_liveness``, which is always bound to the current HeadRun after a replacement.
    retired_wake_liveness: dict[str, Any] = field(default_factory=dict)

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
            "bound": self.bound,
            "state": self.state,
            "launched_at": self.launched_at,
            "last_action": self.last_action,
            "last_action_at": self.last_action_at,
            "deferred_reason": self.deferred_reason,
            "stopped_reason": self.stopped_reason,
            "paused_at": self.paused_at,
            "idle_since": self.idle_since,
            "idle_reason": self.idle_reason,
            "delivery": self.delivery.to_json(),
            "launch_attempts": self.launch_attempts,
            "launch_next_at": self.launch_next_at,
            "run": dict(self.run),
            "head_run": dict(self.head_run),
            "wake_liveness": self.wake_liveness.to_json(),
            "retired_wake_liveness": dict(self.retired_wake_liveness),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ObserverRecord:
        run = payload.get("run")
        delivery = ObserverDelivery.from_json(payload.get("delivery"))
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
            # No default beyond False: a record written before this field existed describes a head
            # launched without a binding, and reading it as bound is exactly the head that would be
            # left running and refused on every write.
            bound=bool(payload.get("bound")),
            state=str(payload.get("state") or "pending"),
            launched_at=_float(payload.get("launched_at")),
            last_action=str(payload.get("last_action") or ""),
            last_action_at=_float(payload.get("last_action_at")),
            deferred_reason=str(payload.get("deferred_reason") or ""),
            stopped_reason=str(payload.get("stopped_reason") or ""),
            paused_at=_float(payload.get("paused_at")),
            idle_since=_float(payload.get("idle_since")),
            idle_reason=str(payload.get("idle_reason") or ""),
            delivery=delivery,
            launch_attempts=_int(payload.get("launch_attempts")),
            launch_next_at=_float(payload.get("launch_next_at")),
            run=dict(run) if isinstance(run, dict) else {},
            head_run=(dict(payload["head_run"]) if isinstance(payload.get("head_run"), dict) else {}),
            wake_liveness=ObserverWakeLiveness.from_json(payload.get("wake_liveness")),
            retired_wake_liveness=(
                dict(payload["retired_wake_liveness"])
                if isinstance(payload.get("retired_wake_liveness"), dict) else {}
            ),
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

    `known: False` means the pid file is not readable, which a head that has just been launched has
    not written yet. That is not death, so it reads as alive until the grace window has passed.
    """
    now = time.time() if now is None else now
    status = head_run_process_status(
        record.pid_file or observer_pid_file(record.sprint),
        run=record.head_run,
        role="observer",
        task=f"sprint:{record.sprint}",
        leaf=record.leaf,
    )
    if heartbeat_is_mismatch(status):
        return {
            "alive": False,
            "reason": "heartbeat-identity-mismatch",
            "pid_known": True,
            "identity_mismatch": True,
        }
    if status.get("known"):
        return {"alive": heartbeat_is_live_match(status), "reason": "pid", "pid_known": True}
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
            "heartbeat_state": liveness["reason"],
            # A head adopted from a launch intent is watching its sprint, but its terminal handle
            # died with the tick that started it: it is stopped by workspace, not by handle.
            "handle_known": bool(record.handle),
            "abandoned_handle": record.abandoned_handle,
            # Whether the head that is up was launched with its sprint binding. False on a head
            # from before the binding: it is alive and every write of its role is refused, which
            # reads as a watched sprint from every other field.
            "bound": record.bound,
            "last_action": record.last_action,
            "last_action_at": record.last_action_at,
            "deferred_reason": record.deferred_reason,
            "stopped_reason": record.stopped_reason,
            "paused": record.paused_at > 0,
            "idle_since": record.idle_since,
            "idle_reason": record.idle_reason,
            "delivery": record.delivery.to_json(),
            "wake_liveness": record.wake_liveness.to_json(),
            "launch_attempts": record.launch_attempts,
            "launch_next_at": record.launch_next_at,
        })
    return rows


def observer_ack_deadline_seconds() -> int:
    """How long one delivery may stay unacknowledged before it is sent again."""
    return positive_int("SECRETARY_OBSERVER_ACK_DEADLINE_SECONDS", OBSERVER_ACK_DEADLINE_DEFAULT_SECONDS)


def observer_turn_ceiling_seconds() -> int:
    """How long one head may hold a batch while never being seen ready for input."""
    return positive_int("SECRETARY_OBSERVER_TURN_CEILING_SECONDS", OBSERVER_TURN_CEILING_DEFAULT_SECONDS)


def observer_wake_max_attempts() -> int:
    """How many refused wake deliveries of one batch are retried before the head is replaced."""
    return positive_int("SECRETARY_OBSERVER_WAKE_MAX_ATTEMPTS", OBSERVER_WAKE_MAX_ATTEMPTS_DEFAULT)


def reconcile_observers(
    runtime: Any, payload: dict[str, Any], *, pause_mode: str = ""
) -> list[dict[str, Any]]:
    """Bring the observer heads in line with the open sprints. Returns the tick's outcomes.

    Nothing happens when there is neither an open sprint nor a tracked observer. A fenced sprint is
    still reconciled: the fence stops the sprint's *cards* and clears only once an observer has been
    adopted, which is this pass, so excluding it would fence the sprint permanently.
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
                _reconcile_open_sprint(
                    runtime, payload, observers, ref,
                    pause_mode=pause_mode, sprint=open_sprints[ref],
                )
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
    sprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = observers.get(ref)
    sprint = sprint if sprint is not None else {}
    try:
        decision = observer_decision(runtime, sprint)
    except ObserverMetadataError as exc:
        # The fence has already stopped this sprint's cards and said so durably.  Nothing is
        # launched, nothing is probed and no live head is taken down on the strength of a value
        # nobody can read: a corrupt declaration is repaired by an operator, not guessed at here.
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-declaration-invalid",
            "reason": exc.message,
            "observer_reason": exc.reason,
        }
    if decision["kind"] == KIND_NONE:
        # A sprint that declares no observer gets no head and no probe.  A record left from an
        # earlier declaration is stopped, or the sprint would keep paying for a head it no longer
        # declares.
        if record is not None:
            return _stop_observer(
                runtime, payload, observers, ref, reason="sprint declares no observer"
            )
        return {
            "status": "ok",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-none",
            "reason": "sprint declares no observer",
        }
    if record is not None and record.state == "blocked":
        # A provider-policy refusal has no retry condition inside the dispatcher.  Keep this
        # sprint-visible record and typed evidence until an independently attested launch replaces
        # it; treating it as a normal deferred launch would quietly keep probing forbidden panes.
        return {
            "status": "blocked",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "codex-fanout-policy-blocked",
            "head": record.head,
            "policy_evidence": {"kind": "codex_provider_fanout", "state": "unknown"},
            "reason": record.deferred_reason,
        }
    if record is not None and _head_may_be_running(record):
        # Recovery has to re-install the exact launch binding before it looks at any new provider
        # event.  The source verifier rejects a missing/mismatched cursor rather than attributing
        # a same-workspace journal to this observer.
        failure = _poll_codex_provider_ingress(runtime, payload, observers, ref, record)
        if failure is not None:
            return failure
    # A record still in `launching` is a bring-up whose tick did not live to record the outcome.
    # It is resolved before anything else, because until it is, neither "a head is running here"
    # nor "this sprint is headless" is known.
    unresolved_intent = record is not None and record.state == "launching"
    if record is not None and unresolved_intent:
        adopted = _adopt_launch_intent(runtime, payload, observers, ref, record)
        if adopted is not None:
            return adopted
        _abandon_launch_intent(runtime, ref, record)
        # Abandoning is a state transition, not a reason for the rest of this tick to keep
        # treating the record as unresolved. In particular, the now-pending dead head must pass
        # the same durable-cursor gate as every other relaunch before the host is touched.
        unresolved_intent = record.state == "launching"
    # A head from before the sprint binding is retired here, whatever its pid says. Its writes are
    # all refused as `observer_identity_unbound`, so it is a live process that cannot work the
    # sprint, and nothing it does can bind it: the binding is in the environment it started with.
    # Stopping it is the changeover — this tick closes the terminal, the next one brings the head
    # back up bound. It comes before the drain check on purpose: a drained pipeline is a reason to
    # start nothing, not a reason to keep a head that can only be refused.
    if (
        record is not None
        and not unresolved_intent
        and not record.bound
        and record.launches > 0
        and _head_may_be_running(record)
    ):
        return _stop_observer(
            runtime, payload, observers, ref,
            reason="observer head predates the sprint binding and cannot authenticate its writes",
        )
    # A terminal left over from an aborted bring-up is skipped here whatever its pid says: that
    # head never received its sprint, and reading it as the live observer would park the sprint
    # forever on a head that is doing nothing.
    pending_event: dict[str, Any] | None = None
    event_state: dict[str, Any] | None = None
    # A durable cursor is a prerequisite for every existing head path, including a legacy record
    # left in `pending`. Otherwise a dead/missing head can be relaunched from an unknowable point
    # and replay or skip board work before the normal recovery branch gets a chance to validate it.
    if record is not None and not unresolved_intent and (
        record.delivery.acknowledged_through
        or (record.delivery.stage != DeliveryStage.IDLE and record.delivery.through_event)
    ):
        event_state = _observer_event_state(runtime, ref, record)
        if not event_state.get("known", True):
            _set_observer_state(record, "degraded", reason=event_state["reason"])
            return {"status": "degraded", "step": "observer-reconcile", "sprint": ref,
                    "action": "observer-cursor-unavailable", "head": record.head,
                    "reason": event_state["reason"]}
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
        event = event_state or _observer_event_state(runtime, ref, record)
        if not event.get("known", True):
            _set_observer_state(record, "degraded", reason=event["reason"])
            return {"status": "degraded", "step": "observer-reconcile", "sprint": ref,
                    "action": "observer-cursor-unavailable", "head": record.head,
                    "reason": event["reason"]}
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
        event = event_state or _observer_event_state(runtime, ref, record)
        if not event.get("known", True):
            _set_observer_state(record, "degraded", reason=event["reason"])
            return {"status": "degraded", "step": "observer-reconcile", "sprint": ref,
                    "action": "observer-cursor-unavailable", "head": record.head,
                    "reason": event["reason"]}
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
        pending_event = event
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
            head=_observer_head_or_blank(runtime, sprint),
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
    if record is None:
        # The first observer launch renders the current sprint board. Card history from before
        # that head existed is therefore its baseline, not a second turn immediately after its
        # own initial queue. Later transitions still enter the delivery machine normally.
        record = ObserverRecord(sprint=ref)
        baseline = _observer_event_state(runtime, ref, record)
        if baseline.get("pending"):
            record.delivery.acknowledged_through = str(baseline.get("event_id") or "")
    return _launch_observer(
        runtime,
        payload,
        observers,
        ref,
        record,
        pending_event=pending_event,
        head=str(decision["head"]),
    )


def _observer_event_state(runtime: Any, ref: str, record: ObserverRecord) -> dict[str, Any]:
    """Read semantic sprint work and acknowledge only the active delivery batch.

    A resume acknowledges only when its audit payload carries the active delivery's immutable
    marker, so an older turn cannot credit work it never received.
    """
    try:
        # Observer reconciliation needs cards and the event stream, but not the independently
        # rendered resume-freshness field. Skipping it keeps this path to one audit snapshot.
        sprint = runtime.sprints.show(ref, include_resume_freshness=False)
        cards = sprint.get("cards") if isinstance(sprint.get("cards"), list) else []
        refs = {
            str(card.get("ref") or "")
            for card in cards
            if isinstance(card, dict) and str(card.get("ref") or "")
        }
        events = runtime.audit.events()
    except (TaskError, HostError, OSError, ValueError, TypeError):
        return {"known": False, "pending": False, "reason": "linked card audit is unavailable"}
    resumes: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("ref") or "") == ref and str(event.get("kind") or "") == "resume_recorded":
            resumes.append(event)
    _acknowledge_delivery_from_resume(record.delivery, resumes)
    # Cursors written before semantic wakes were narrowed may name claim/report/routing telemetry.
    # Resolve that id in the complete stream first, then filter only later events; filtering first
    # makes the cursor disappear and replays the whole sprint forever.
    cursor = record.delivery.acknowledged_through
    cursor_at = _event_index(events, cursor)
    if cursor and cursor_at < 0:
        return {"known": False, "pending": False, "reason": "acknowledged observer cursor is unavailable"}
    active_legacy_at = -1
    if record.delivery.stage != DeliveryStage.IDLE and record.delivery.through_event:
        active_at = _event_index(events, record.delivery.through_event)
        if active_at < 0:
            return {"known": False, "pending": False, "reason": "active observer cursor is unavailable"}
        if not is_significant_observer_event(events[active_at], linked_refs=refs, sprint_ref=ref):
            active_legacy_at = active_at
    following = events[cursor_at + 1:] if cursor_at >= 0 else events
    significant = [
        event for event in following
        if is_significant_observer_event(event, linked_refs=refs, sprint_ref=ref)
    ]
    if active_legacy_at >= 0:
        # Retire the obsolete batch without skipping semantic work which arrived after the prior
        # acknowledged cursor. Only an empty semantic range permits advancing across the noise.
        preserved_cursor = cursor if significant else record.delivery.through_event
        _reset_delivery_to_idle(
            record.delivery,
            acknowledged_through=preserved_cursor,
            acknowledged_delivery_id=record.delivery.delivery_id,
            acknowledged_resume_id="",
        )
    if not significant:
        return {"known": True, "pending": False, "reason": "no significant linked-card event"}
    latest = significant[-1]
    latest_id = _event_id(latest)
    if not latest_id:
        return {"known": False, "pending": False, "reason": "latest card event has no durable id"}
    return {
        "known": True,
        "pending": True,
        "event_id": latest_id,
        "pending_from": _event_id(significant[0]),
        "occurred_at": str(latest.get("occurred_at") or ""),
        "age_seconds": _event_age_seconds(str(latest.get("occurred_at") or "")),
        "latest_resume_id": _event_id(resumes[-1]) if resumes else "",
        "reason": "latest significant linked-card event is not acknowledged",
    }


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


def _event_id(event: dict[str, Any] | None) -> str:
    if not isinstance(event, dict):
        return ""
    return str(event.get("event_id") or event.get("request_id") or "")


def _event_index(events: list[dict[str, Any]], event_id: str) -> int:
    if not event_id:
        return -1
    for index, event in enumerate(events):
        if _event_id(event) == event_id:
            return index
    return -1


def _reset_delivery_to_idle(
    delivery: ObserverDelivery,
    *,
    acknowledged_through: str,
    acknowledged_delivery_id: str,
    acknowledged_resume_id: str,
) -> None:
    delivery.stage = DeliveryStage.IDLE
    delivery.acknowledged_through = acknowledged_through
    delivery.acknowledged_delivery_id = acknowledged_delivery_id
    delivery.acknowledged_resume_id = acknowledged_resume_id
    delivery.pending_from = ""
    delivery.delivery_id = ""
    delivery.method = ""
    delivery.through_event = ""
    delivery.resume_cursor = ""
    delivery.resume_cursor_known = True
    delivery.sent_at = 0.0
    delivery.held_since = 0.0
    delivery.deadline = 0.0
    delivery.attempts = 0
    delivery.next_at = 0.0
    delivery.reason = ""


def _acknowledge_delivery_from_resume(
    delivery: ObserverDelivery, resumes: list[dict[str, Any]]
) -> None:
    """Advance only when the active batch's marker was written by its observer.

    A refused delivery counts here too: the marker only exists in a prompt that reached the head, so
    a resume naming it is proof of the turn whatever the dispatcher observed of the send.
    """

    if delivery.stage not in {
        DeliveryStage.DELIVERY_INTENT,
        DeliveryStage.AWAITING_ACK,
        DeliveryStage.RETRY_DEFERRED,
    }:
        return
    if not delivery.delivery_id or not delivery.through_event:
        return
    for resume in reversed(resumes):
        payload = resume.get("payload")
        actor = resume.get("actor")
        if not isinstance(payload, dict) or not isinstance(actor, dict):
            continue
        if str(actor.get("role") or "") != OBSERVER_ROLE:
            continue
        if (
            str(payload.get("delivery_id") or "") != delivery.delivery_id
            or str(payload.get("through_event") or "") != delivery.through_event
        ):
            continue
        resume_id = _event_id(resume)
        if not resume_id:
            continue
        _reset_delivery_to_idle(
            delivery,
            acknowledged_through=delivery.through_event,
            acknowledged_delivery_id=delivery.delivery_id,
            acknowledged_resume_id=resume_id,
        )
        return


def _new_delivery_intent(
    delivery: ObserverDelivery,
    *,
    method: str,
    through_event: str,
    resume_cursor: str,
    now: float,
    delivery_id: str = "",
) -> None:
    """Fix a high-water mark before an external observer action."""

    delivery.stage = DeliveryStage.DELIVERY_INTENT
    delivery.pending_from = ""
    delivery.delivery_id = delivery_id or "delivery-" + uuid.uuid4().hex
    delivery.method = method
    delivery.through_event = through_event
    delivery.resume_cursor = resume_cursor
    delivery.resume_cursor_known = True
    delivery.sent_at = now
    # A fresh send is a fresh turn: the head is being asked again, so the ceiling on how long it
    # may hold this batch without going idle starts over here too.
    delivery.held_since = now
    delivery.deadline = now + observer_ack_deadline_seconds()
    delivery.next_at = delivery.deadline
    delivery.reason = f"{method} delivery intent is persisted before external action"


def _prepare_launch_delivery(record: ObserverRecord, event: dict[str, Any]) -> str:
    """Put a replacement launch through the same state machine as a terminal nudge."""

    delivery = record.delivery
    now = time.time()
    active = delivery.stage in {DeliveryStage.DELIVERY_INTENT, DeliveryStage.AWAITING_ACK}
    retrying = delivery.stage == DeliveryStage.RETRY_DEFERRED
    _new_delivery_intent(
        delivery,
        method="launch",
        through_event=(delivery.through_event if active or retrying else str(event["event_id"])),
        resume_cursor=str(event.get("latest_resume_id") or ""),
        now=now,
        delivery_id=delivery.delivery_id if active or retrying else "",
    )
    return delivery.through_event


def _set_delivery_waiting(delivery: ObserverDelivery, event: dict[str, Any], *, reason: str) -> None:
    if delivery.stage == DeliveryStage.IDLE:
        delivery.stage = DeliveryStage.WAITING_FOR_IDLE
        delivery.pending_from = str(event.get("pending_from") or event.get("event_id") or "")
        # A batch waiting for a busy head is held by it, so the turn ceiling runs from here even
        # though no prompt has been delivered yet.
        delivery.held_since = time.time()
    delivery.reason = reason


def _evidence_of(carrier: Any) -> dict[str, Any]:
    """The bounded delivery evidence a host answer or failure carries, or nothing."""
    evidence = getattr(carrier, "evidence", None)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    return dict(evidence) if isinstance(evidence, dict) else {}


def _delivery_subject(method: str) -> str:
    """Which delivery this is, in the words the sprint's evidence keeps it under.

    A wake is a prompt sent into an observer head already up; a launch delivery is the prompt a
    replacement head is brought up with. Neither is a reviewer bring-up, which belongs to a card.
    """
    return "observer-launch" if method == "launch" else "observer-wake"


def _count_delivery_attempt(delivery: ObserverDelivery, method: str) -> None:
    """Count one attempt to put a batch in front of the observer, however it ends."""
    if method == "launch":
        delivery.launch_delivery_attempts += 1
    else:
        delivery.wake_attempts += 1


def _count_delivery_failure(
    delivery: ObserverDelivery,
    method: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Record one delivery that did not reach the head, as sprint evidence rather than as state.

    An attempt is counted with it: a delivery that failed before the pane was touched at all was
    still a turn the observer was owed and did not get.
    """
    _count_delivery_attempt(delivery, method)
    if method == "launch":
        delivery.launch_delivery_failures += 1
    else:
        delivery.wake_failures += 1
    delivery.last_failure_reason = reason
    delivery.last_failure_at = time.time()
    delivery.last_failure_method = _delivery_subject(method)
    if evidence:
        delivery.last_evidence = dict(evidence)


def delivery_evidence_summary(delivery: Any) -> str:
    """One line of delivery evidence for the head that has to report it, or an empty string.

    Deliberately counts and a reason, never a retry instruction: redelivery is the dispatcher's, and
    a head reading this is reporting history. It reads its numbers defensively because a host seam
    may hand it any record shape.
    """
    wake_failures = _int(getattr(delivery, "wake_failures", 0))
    launch_failures = _int(getattr(delivery, "launch_delivery_failures", 0))
    attempts = _int(getattr(delivery, "wake_attempts", 0)) + _int(
        getattr(delivery, "launch_delivery_attempts", 0)
    )
    failures = wake_failures + launch_failures
    if not attempts and not failures:
        return ""
    line = (
        f"observer delivery so far: {attempts} attempt(s), {failures} failed "
        f"({wake_failures} wake, {launch_failures} launch)"
    )
    reason = str(getattr(delivery, "last_failure_reason", "") or "")
    if reason:
        method = str(getattr(delivery, "last_failure_method", "") or "observer-wake")
        line += f"; last failure ({method}): {reason}"
    return line


def _defer_delivery(
    record: ObserverRecord,
    ref: str,
    reason: str,
    *,
    method: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist bounded external-delivery retry state without creating a busy loop."""

    delivery = record.delivery
    _count_delivery_failure(delivery, method or delivery.method or "nudge", reason, evidence)
    delivery.stage = DeliveryStage.RETRY_DEFERRED
    delivery.attempts += 1
    delay = min(
        OBSERVER_WAKE_RETRY_INITIAL_SECONDS * (2 ** (delivery.attempts - 1)),
        OBSERVER_WAKE_RETRY_MAX_SECONDS,
    )
    delivery.next_at = time.time() + delay
    delivery.deadline = 0.0
    delivery.reason = f"{reason}; retry in {delay}s"
    _set_observer_state(record, "wake-deferred", reason=delivery.reason)
    return {
        "status": "degraded",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-wake-deferred",
        "head": record.head,
        "delivery_id": delivery.delivery_id,
        "event_id": delivery.through_event,
        "reason": delivery.reason,
        "wake_failures": delivery.wake_failures,
        "launch_delivery_failures": delivery.launch_delivery_failures,
    }


def _wait_for_busy_delivery(
    record: ObserverRecord,
    ref: str,
    reason: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep an observed-working observer and its exact undelivered batch intact.

    The intent was already durable before the nudge. A `tui-idle` timeout with a busy body says the
    prompt was not sent, so it must not consume a failed-wake retry or arm an acknowledgement
    deadline; `WAITING_FOR_IDLE` puts it under the turn ceiling instead.
    """
    delivery = record.delivery
    delivery.stage = DeliveryStage.WAITING_FOR_IDLE
    delivery.deadline = 0.0
    delivery.next_at = 0.0
    if not delivery.held_since:
        delivery.held_since = time.time()
    delivery.reason = reason
    if evidence:
        delivery.last_evidence = dict(evidence)
    _set_observer_state(record, "waiting", reason=reason)
    return {
        "status": "degraded",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-wake-busy",
        "head": record.head,
        "delivery_id": delivery.delivery_id,
        "event_id": delivery.through_event,
        "reason": reason,
    }


def _fail_delivery(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    event: dict[str, Any],
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Refuse one wake, retry it a bounded number of times, then replace the head.

    Once the retries are spent the delivery takes the replacement path: it stops this head before it
    opens the next one, and carries the same `delivery_id` and `through_event` into the new launch,
    so a resume still acknowledges exactly the batch that was owed.
    """
    outcome = _defer_delivery(record, ref, reason, method="nudge", evidence=evidence)
    if record.delivery.attempts < observer_wake_max_attempts():
        return outcome
    attempts = record.delivery.attempts
    # The replacement opens its own delivery attempt count: the next failing batch gets the same
    # bounded retries before it costs the sprint another head.
    record.delivery.attempts = 0
    replaced = _launch_observer(runtime, payload, observers, ref, record, pending_event=event)
    unlaunched = str(replaced.get("reason") or "")
    replaced["reason"] = f"{reason}; the observer head was replaced after {attempts} failed wakes"
    if unlaunched:
        replaced["reason"] += f"; that replacement did not come up: {unlaunched}"
    return replaced


def _wake_pending(ref: str, record: ObserverRecord) -> dict[str, Any]:
    """A delivery that is on the head and still within its acknowledgement deadline."""
    return {
        "status": "ok",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-wake-pending",
        "head": record.head,
        "delivery_id": record.delivery.delivery_id,
        "event_id": record.delivery.through_event,
    }


def _redelivery_reason(
    status: dict[str, Any], delivery: ObserverDelivery, *, now: float
) -> str:
    """Why an active delivery is sent again, or an empty string to keep waiting for its deadline.

    The head being idle is the first reason and does not wait the deadline out. Idle evidence is the
    readiness signal plus a readable `last_activity`; a pane whose activity cannot be read says
    nothing about whether a turn ended, so it is not idle here.

    A delivery still in `DELIVERY_INTENT` is a third case: the tick died between persisting the
    intent and confirming the prompt landed, so a ready pane may simply be one that never received
    it. Only a delivery known to have been sent is redelivered on idleness; the deadline covers the
    other one.
    """
    if now >= delivery.deadline:
        return (
            "the acknowledgement deadline expired with this batch unacknowledged after "
            f"{int(now - delivery.sent_at)}s"
        )
    if delivery.stage != DeliveryStage.AWAITING_ACK:
        return ""
    try:
        # Read for its readability alone: the value is not compared against anything.
        float(status.get("last_activity"))
    except (TypeError, ValueError):
        return ""
    return (
        "the observer became idle without acknowledging this batch "
        f"{int(now - delivery.sent_at)}s after it was sent"
    )


def _observer_provider_source(record: ObserverRecord) -> tuple[head_ops.HeadRun | None, dict[str, Any]]:
    """Return the persisted Codex source, never a source found from the workspace."""
    stored = record.head_run if isinstance(record.head_run, dict) else {}
    try:
        run = head_ops.HeadRun.from_json(stored)
    except (head_ops.HeadRunError, head_ops.TaskRefError, TypeError, ValueError):
        return None, {}
    source = run.fanout_policy.get("provider_source")
    if run.spec.adapter != "codex" or not isinstance(source, dict):
        return run, {}
    return run, dict(source)


def _observer_has_provider_liveness_contract(record: ObserverRecord) -> bool:
    """Whether this record must use run-bound provider progress for its event wake."""
    _, source = _observer_provider_source(record)
    return bool(source) or record.wake_liveness.bound


def _precontract_unbound_observer_source(record: ObserverRecord) -> bool:
    """Recognise, but never repair, the pre-rollout unbound Codex source shape.

    A current preflight source carries the run descriptor and the exact pre-pane baseline that makes
    later binding safe. Accepting a journal from the workspace now would be a retroactive identity
    claim.
    """
    run, source = _observer_provider_source(record)
    if run is None or not source or source.get("state") != "unbound":
        return False
    run_id, fingerprint = head_run_binding(record.head_run)
    return not (
        run_id
        and str(source.get("run_id") or "") == run_id
        and str(source.get("head_run_fingerprint") or "") == fingerprint
        and str(source.get("workspace") or "") == run.workspace
        and str(source.get("role") or "") == OBSERVER_ROLE
        and source.get("task_ref") == run.task_ref.to_json()
        and str(source.get("root") or "")
        and isinstance(source.get("baseline"), list)
    )


def _begin_observer_wake_liveness(record: ObserverRecord) -> None:
    """Open one episode only at a new event-delivery boundary.

    An old terminal episode is replaced only after a later acknowledged batch opens a new delivery
    on the new HeadRun, never on an observation from an arbitrary same-workspace source.
    """
    if not _observer_has_provider_liveness_contract(record):
        return
    run_id, _ = head_run_binding(record.head_run)
    if not run_id:
        return
    if record.wake_liveness.bound or record.wake_liveness.terminal:
        record.wake_liveness = ObserverWakeLiveness.begin(record.head_run)
        return
    if (
        record.wake_liveness.state == ContinuationLivenessState.UNKNOWN
        and record.wake_liveness.reason == "missing"
    ):
        record.wake_liveness = ObserverWakeLiveness.begin(record.head_run)


def _observe_observer_wake_progress(record: ObserverRecord, runtime: Any, *, now: float) -> str:
    """Record one exact-run provider observation before any TUI readiness interpretation."""
    if not _observer_has_provider_liveness_contract(record):
        return "disabled"
    liveness = record.wake_liveness
    if not liveness.bound:
        # A malformed or historical episode cannot acquire a source halfway through a delivery.
        return "unknown"
    try:
        probe = getattr(runtime.host, "observer_provider_progress", None)
        evidence = (
            probe(record)
            if callable(probe)
            else {"state": "unavailable", "reason": "host has no observer provider-progress probe"}
        )
    except Exception as exc:
        evidence = {"state": "unavailable", "reason": f"provider-progress probe failed: {exc}"}
    return liveness.observe_provider(evidence, now, head_run=record.head_run)


def _observer_no_progress_evidence(record: ObserverRecord, status: dict[str, Any]) -> str:
    """Classify a stale exact cursor without making screen text liveness authority."""
    evidence = record.delivery.last_evidence if isinstance(record.delivery.last_evidence, dict) else {}
    evidence = {**evidence, **(status.get("delivery_evidence") or {})} if isinstance(
        status.get("delivery_evidence"), dict
    ) else evidence
    if str(evidence.get("reason") or "") == "payload-left-in-composer":
        return "completed_turn_residual_composer"
    composer_before = str(evidence.get("composer_before") or "")
    composer_after = str(evidence.get("composer_after") or "")
    cursor_before = str(evidence.get("cursor_before") or "")
    cursor_after = str(evidence.get("cursor_after") or "")
    if (
        composer_before
        and composer_before == composer_after
        and composer_before not in {COMPOSER_EMPTY, COMPOSER_UNKNOWN}
        and cursor_before
        and cursor_before == cursor_after
    ):
        return "completed_turn_residual_composer"
    return "active_or_unknown_turn"


def _replace_observer_for_no_progress(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    event: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Persist the terminal exact-run outcome before the fenced stop/relaunch path."""
    record.wake_liveness.terminalize("replacement", reason)
    observers[ref] = record
    if not _persist_quietly(runtime, payload, observers):
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-wake-liveness-persist-failed",
            "head": record.head,
            "reason": "observer provider-progress terminal outcome could not be persisted",
        }
    replaced = _launch_observer(runtime, payload, observers, ref, record, pending_event=event)
    original = str(replaced.get("reason") or "")
    replaced["reason"] = reason + (f"; {original}" if original else "")
    return replaced


def _adopt_precontract_unbound_observer(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    event: dict[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    """Replace an old unbound Codex observer without treating a workspace journal as its own."""
    if not record.wake_liveness.bound:
        record.wake_liveness = ObserverWakeLiveness.begin(record.head_run)
    record.wake_liveness.observe_provider(
        {
            "state": "unavailable",
            "reason": "pre-contract Codex observer source is unbound and has no exact baseline",
        },
        now,
        head_run=record.head_run,
    )
    _new_delivery_intent(
        record.delivery,
        method="replacement",
        through_event=str(event["event_id"]),
        resume_cursor=str(event.get("latest_resume_id") or ""),
        now=now,
        delivery_id=record.delivery.delivery_id,
    )
    return _replace_observer_for_no_progress(
        runtime,
        payload,
        observers,
        ref,
        record,
        event,
        reason=(
            "pre-contract Codex observer provider source is unbound; it cannot be rebound from "
            "workspace journal discovery and is being identity-fenced for replacement"
        ),
    )


def _turn_ceiling_overrun(delivery: ObserverDelivery, *, now: float) -> str:
    """Why a head that is never ready for input has held this batch too long, or an empty string.

    Not the acknowledgement deadline under another name: that one ends a silence on a head standing
    at its prompt, this one ends a delivery held by a head that still looks busy, which is why it is
    much longer — below the longest legitimate turn it would tear down an observer that is working.
    """
    if delivery.stage == DeliveryStage.IDLE:
        return ""
    if not delivery.held_since:
        # A record written before this ceiling existed. It runs from the first tick that sees the
        # head busy, never retroactively over a hold nobody was measuring.
        delivery.held_since = now
        return ""
    held = now - delivery.held_since
    ceiling = observer_turn_ceiling_seconds()
    if held < ceiling:
        return ""
    return (
        f"the observer head held this delivery for {int(held)}s without ever being ready for "
        f"input, past the {ceiling}s turn ceiling"
    )


def _wake_for_event(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Deliver one immutable event batch to a completed observer queue.

    A delivery already on the head is repeated for one of two reasons, and never because the event
    itself has aged: the head was seen idle without acknowledging the batch, or its acknowledgement
    deadline ran out. A head never seen idle is ended by the turn ceiling instead.
    """
    now = time.time()
    delivery = record.delivery
    event_id = str(event["event_id"])
    if delivery.stage == DeliveryStage.IDLE:
        _set_delivery_waiting(
            delivery, event, reason="observer has unacknowledged significant card work"
        )
        _begin_observer_wake_liveness(record)
    if _precontract_unbound_observer_source(record):
        return _adopt_precontract_unbound_observer(
            runtime, payload, observers, ref, record, event, now=now
        )
    provider_observation = _observe_observer_wake_progress(record, runtime, now=now)
    if provider_observation != "disabled":
        # The cursor and its admission are durable before a pane read can cause delivery,
        # replacement or a return that leaves this process.  A fresh cursor is stronger than an
        # idle screen and means this exact observer is already making progress on the same batch.
        observers[ref] = record
        if not _persist_quietly(runtime, payload, observers):
            return {
                "status": "degraded",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-wake-liveness-persist-failed",
                "head": record.head,
                "reason": "observer provider-progress evidence could not be persisted",
            }
        if provider_observation == "progressed":
            return {
                "status": "ok",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-wake-progressing",
                "head": record.head,
                "event_id": delivery.through_event or event_id,
                "reason": "an admitted exact-HeadRun provider cursor advanced",
            }
        if provider_observation in {"unknown", "unavailable"}:
            return {
                "status": "degraded",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-wake-liveness-unavailable",
                "head": record.head,
                "event_id": delivery.through_event or event_id,
                "reason": record.wake_liveness.reason or "observer provider progress was not admitted",
            }
    if delivery.stage == DeliveryStage.RETRY_DEFERRED and now < delivery.next_at:
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-wake-deferred",
            "head": record.head,
            "delivery_id": delivery.delivery_id,
            "event_id": delivery.through_event,
            "reason": delivery.reason,
        }
    active = delivery.stage in {DeliveryStage.DELIVERY_INTENT, DeliveryStage.AWAITING_ACK}
    try:
        status = getattr(runtime.host, "observer_status", lambda _record: {})(record)
    except (HostError, OSError, TypeError, ValueError) as exc:
        if active and now < delivery.deadline:
            # A terminal that could not be read says nothing about the head either way, and this
            # delivery's acknowledgement deadline is still running: waiting costs it nothing.
            return _wake_pending(ref, record)
        if delivery.stage == DeliveryStage.WAITING_FOR_IDLE:
            _new_delivery_intent(
                delivery,
                method="nudge",
                through_event=event_id,
                resume_cursor=str(event.get("latest_resume_id") or ""),
                now=now,
            )
        return _fail_delivery(
            runtime, payload, observers, ref, record, event,
            f"observer terminal could not be read: {exc}",
        )
    if not isinstance(status, dict) or not status.get("idle"):
        if provider_observation == "stalled" and _observer_has_provider_liveness_contract(record):
            liveness = record.wake_liveness
            liveness.no_progress_evidence = _observer_no_progress_evidence(record, status)
            attempts = liveness.note_busy(now)
            if attempts >= CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS:
                return _replace_observer_for_no_progress(
                    runtime,
                    payload,
                    observers,
                    ref,
                    record,
                    event,
                    reason=(
                        "the exact observer provider cursor remained unchanged through "
                        f"{attempts} bounded no-progress observations "
                        f"({liveness.no_progress_evidence})"
                    ),
                )
            _set_observer_state(
                record,
                "waiting",
                reason=(
                    "observer terminal is not ready and its exact provider cursor has not advanced "
                    f"({attempts}/{CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS})"
                ),
            )
            return {
                "status": "ok",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-wake-no-progress",
                "head": record.head,
                "event_id": delivery.through_event or event_id,
                "attempts": attempts,
                "reason": delivery.reason,
            }
        if not _observer_has_provider_liveness_contract(record):
            overrun = _turn_ceiling_overrun(delivery, now=now)
            if overrun:
                return _fail_delivery(runtime, payload, observers, ref, record, event, overrun)
        if active and now < delivery.deadline:
            return _wake_pending(ref, record)
        if delivery.stage == DeliveryStage.IDLE:
            _set_delivery_waiting(delivery, event, reason="observer terminal is not ready for a prompt")
        elif delivery.stage == DeliveryStage.WAITING_FOR_IDLE:
            delivery.reason = "observer terminal is not ready for a prompt"
        elif active:
            delivery.reason = (
                "the acknowledgement deadline expired and the observer terminal is still not ready "
                "for a prompt"
            )
        _set_observer_state(record, "waiting", reason=delivery.reason)
        return {
            "status": "ok",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-wake-waiting",
            "head": record.head,
            "event_id": delivery.through_event or event_id,
            "reason": delivery.reason,
        }
    redelivery = _redelivery_reason(status, delivery, now=now) if active else ""
    if active and not redelivery:
        return _wake_pending(ref, record)
    if active:
        # A repeat keeps the original high-water mark. A newer B may be visible to the observer,
        # but a resume for this turn still acknowledges only the unconfirmed A batch.
        _new_delivery_intent(
            delivery,
            method="nudge",
            through_event=delivery.through_event,
            resume_cursor=str(event.get("latest_resume_id") or ""),
            now=now,
        )
    elif delivery.stage == DeliveryStage.RETRY_DEFERRED:
        _new_delivery_intent(
            delivery,
            method="nudge",
            through_event=delivery.through_event or event_id,
            resume_cursor=str(event.get("latest_resume_id") or ""),
            now=now,
            delivery_id=delivery.delivery_id,
        )
    elif delivery.stage == DeliveryStage.WAITING_FOR_IDLE and delivery.delivery_id:
        # A direct delivery wait saw this same live pane busy before any send. Keep its marker and
        # high-water mark when it eventually becomes idle; a fresh id would make a later resume
        # unable to acknowledge the intent that was already written before that busy observation.
        _new_delivery_intent(
            delivery,
            method="nudge",
            through_event=delivery.through_event or event_id,
            resume_cursor=str(event.get("latest_resume_id") or ""),
            now=now,
            delivery_id=delivery.delivery_id,
        )
    else:
        _new_delivery_intent(
            delivery,
            method="nudge",
            through_event=event_id,
            resume_cursor=str(event.get("latest_resume_id") or ""),
            now=now,
        )
    observers[ref] = record
    if not _persist_quietly(runtime, payload, observers):
        return _defer_delivery(record, ref, "observer wake intent could not be persisted")
    try:
        # Delivery acceptance is a terminal concern. The causal acknowledgement is deliberately
        # out of band: the next normal reconciliation reads the observer's durable resume once,
        # instead of polling the complete audit stream while the prompt-delivery loop runs.
        accepted = runtime.host.nudge_observer(record)
    except (AttributeError, HostError, OSError, TypeError, ValueError) as exc:
        evidence = _evidence_of(exc)
        if delivery_readiness_state(evidence) == READINESS_BUSY:
            return _wait_for_busy_delivery(
                record,
                ref,
                "observer terminal became busy before its event wake could be delivered",
                evidence=evidence,
            )
        return _fail_delivery(
            runtime, payload, observers, ref, record, event,
            f"observer wake failed: {exc}",
            evidence=evidence,
        )
    _count_delivery_attempt(delivery, "nudge")
    delivery.last_evidence = _evidence_of(accepted) or delivery.last_evidence
    delivery.stage = DeliveryStage.AWAITING_ACK
    delivery.sent_at = now
    delivery.held_since = now
    delivery.deadline = now + observer_ack_deadline_seconds()
    delivery.next_at = delivery.deadline
    delivery.reason = redelivery or "observer was nudged for an unacknowledged card event"
    _set_observer_state(record, "running")
    outcome = {
        "status": "ok",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-redelivered" if redelivery else "observer-nudged",
        "head": record.head,
        "delivery_id": delivery.delivery_id,
        "event_id": delivery.through_event,
    }
    if redelivery:
        outcome["reason"] = redelivery
    return outcome


def _observer_work_state(runtime: Any, ref: str, record: ObserverRecord) -> dict[str, Any]:
    """Classify the live observer without turning an ordinary card wait into a restart.

    A pane Orca reports ready for input means the observer is idle even while a card is in flight:
    the card's next durable transition, not a periodic idle check, provides its next model turn.
    """
    try:
        runtime.sprints.show(ref, include_resume_freshness=False)
    except (HostError, TaskError):
        return {"state": "unknown", "reason": "the sprint could not be read for idle recovery"}
    try:
        status = getattr(runtime.host, "observer_status", lambda _record: {})(record)
    except (HostError, OSError, TypeError, ValueError):
        return {"state": "unknown", "reason": "the observer terminal could not be read for idle recovery"}
    if not isinstance(status, dict) or not status.get("idle"):
        return {"state": "unknown", "reason": "the observer terminal is not ready for a prompt"}
    try:
        last_activity = float(status.get("last_activity"))
    except (TypeError, ValueError):
        return {"state": "unknown", "reason": "the observer terminal has no activity timestamp"}
    age = time.time() - last_activity
    return {
        "state": "idle",
        "since": last_activity,
        "reason": (
            "the observer head finished its turn and its terminal has been quiet for "
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

    A launch intent counts: it is written before the host is called, so a record that carries one
    may well have a head behind it, and the workspace is where that head is, handle or no handle.
    """
    return bool(record.handle) or (record.head_possible and bool(record.workspace))


def _needs_teardown(record: ObserverRecord) -> bool:
    """Whether the stop still has something of this record to give back to Orca.

    A head that may be up is one such thing, a workspace the bring-up may have registered is the
    other, and they do not arrive or leave together.
    """
    return _head_may_be_running(record) or (record.workspace_live and bool(record.workspace))


def _bring_up_request_id(ref: str, generation: str, attempt: int) -> str:
    """The id `_launch_observer` gave attempt number `attempt`. The first one is a launch."""
    return observer_request_id(
        "relaunch" if attempt > 1 else "launch", ref, generation, attempt
    )


def _abandon_launch_intent(runtime: Any, ref: str, record: ObserverRecord) -> None:
    """Close the books on an intent whose head could not be found alive.

    An attempt the log already carries is spent, so the next bring-up is a relaunch with its own
    request id; an attempt that never got past its pending copy is retried as itself. Either way the
    record still says a terminal may be up, so the relaunch closes the workspace first.
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

    A readable live pid is this sprint's head, adopted with the attempt number the intent reserved.
    A head that has not written its pid yet is left as it is inside the grace window — killing one
    that is still starting would cut the sprint's one continuous session. Anything else returns None
    and the caller relaunches, closing whatever the intent left in the workspace first.
    """
    liveness = observer_alive(record)
    if liveness.get("identity_mismatch"):
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-heartbeat-identity-mismatch",
            "head": record.head,
            "reason": "observer heartbeat names a live process with a mismatching launch identity",
        }
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


def observer_decision(runtime: Any, sprint: dict[str, Any]) -> dict[str, Any]:
    """What this sprint's observer metadata says to run. Raises rather than guessing.

    Two answers, and no third:

          `head`   the sprint declares one concrete profile, and the registry has it
          `none`   the sprint declares that it runs without an observer

    There is no path back to the role default. `ObserverMetadataError` for an open row whose value is
    missing, unreadable, provenance rather than a declaration, or a profile the registry lacks.
    """
    value = executable_observer(sprint)
    if value["kind"] == KIND_NONE:
        return {"kind": KIND_NONE, "head": "", "value": value}
    head = str(value["profile"])
    try:
        runtime.catalog.observer_profile(head)
    except (HostError, TaskError) as exc:
        raise ObserverMetadataError(
            REASON_UNKNOWN_PROFILE,
            f"sprint {sprint.get('ref') or '?'} declares observer head {head!r}, which this "
            f"installation cannot resolve: {getattr(exc, 'message', str(exc))}",
        ) from None
    return {"kind": KIND_HEAD, "head": head, "value": value}


def _role_default_observer_head(runtime: Any) -> str:
    """The registry's observer default, for a record filled in without a sprint to read.

    Empty when the registry cannot answer; the launch path reports the same failure properly.
    """
    try:
        return runtime.catalog.observer_head()
    except HostError:
        return ""


def _observer_head_or_blank(runtime: Any, sprint: dict[str, Any] | None = None) -> str:
    """The head to fill a record with, without deciding anything the launch has to decide again.

    Corruption is not reported here: the fence and the launch path are where an unusable observer
    is answered for.
    """
    if sprint is not None:
        try:
            decision = observer_decision(runtime, sprint)
        except ObserverMetadataError:
            return ""
        return str(decision.get("head") or "")
    return _role_default_observer_head(runtime)


def observer_skill_delivery(runtime: Any, head: str) -> dict[str, Any]:
    """Whether the observer skill is in the shell this head runs in.

    The shell is the head's own adapter, read from the same registry the launch command is rendered
    from. A registry that cannot name the adapter reads as undelivered — a head brought up without
    its instructions is worse than a sprint that waits for the next tick.
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


def _declared_head(runtime: Any, ref: str) -> str:
    """The profile one open sprint declares, read live. Raises rather than substituting one."""
    sprint = runtime.sprints.show(ref, include_cards=False, include_resume_freshness=False)
    decision = observer_decision(runtime, sprint)
    if decision["kind"] == KIND_NONE:
        raise ObserverMetadataError(
            "observer_none", f"sprint {ref} declares no observer, so there is no head to launch"
        )
    head = str(decision.get("head") or "")
    if not head:
        raise HostError(f"no observer head could be resolved for sprint {ref}")
    return head


def _launch_observer(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord | None,
    *,
    pending_event: dict[str, Any] | None = None,
    head: str = "",
) -> dict[str, Any]:
    """Bring up one head for one sprint, on the profile that sprint declares.

    `head` is decided by the caller from the sprint's own metadata, so nothing between the
    declaration and the launch can substitute another profile.
    """
    record = record or ObserverRecord(sprint=ref)
    relaunch = record.launches > 0
    if not head:
        # The replacement path after a batch of refused wakes gets here without one. It resolves
        # the sprint's declaration again rather than reusing `record.head`: a re-pointed sprint
        # must move its head at the replacement, not keep the profile the first launch picked.
        try:
            head = _declared_head(runtime, ref)
        except ObserverMetadataError as exc:
            return _defer(runtime, payload, observers, ref, record, head="", reason=exc.message)
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
    if pending_event is not None and record.wake_liveness.bound and not record.wake_liveness.terminal:
        # Every replacement closes the old exact-run episode before the old process is touched.
        # The launch intent below will move this terminal value to audit-only storage and open a
        # fresh episode for the replacement HeadRun in the same durable write as that new binding.
        record.wake_liveness.terminalize(
            "replacement", "observer HeadRun was replaced while its event delivery was pending"
        )
        observers[ref] = record
        if not _persist_quietly(runtime, payload, observers):
            return {
                "status": "degraded", "step": "observer-reconcile", "sprint": ref,
                "action": "observer-wake-liveness-persist-failed", "head": record.head,
                "reason": "retiring observer wake-liveness outcome could not be persisted",
            }
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
        sprint = runtime.sprints.show(ref, include_cards=False, include_resume_freshness=False)
    except (HostError, TaskError) as exc:
        return _defer(
            runtime, payload, observers, ref, record, head=head,
            reason=f"observer bring-up failed: {getattr(exc, 'message', str(exc))}",
        )
    kind = EVENT_RELAUNCHED if relaunch else EVENT_LAUNCHED
    attempt = record.launches + 1
    request_id = _bring_up_request_id(ref, record.generation, attempt)
    delivery_event_id = ""
    if pending_event is not None:
        # The lifecycle launch is also this event's only permitted observer turn. The delivery
        # intent is saved by `_write_launch_intent` before `prepare_observer` reaches the host.
        delivery_event_id = _prepare_launch_delivery(record, pending_event)
    try:
        # Staged before the host is asked for anything, so a head that comes up while the process
        # dies mid-launch still has its event on disk for `TaskAudit.reconcile()` to pick up.
        event = stage_event(
            runtime,
            kind,
            ref,
            request_id,
            {
                "head": head,
                "launches": attempt,
                **(
                    {
                        "delivery_id": record.delivery.delivery_id,
                        "delivery_method": record.delivery.method,
                        "through_event": delivery_event_id,
                    }
                    if delivery_event_id
                    else {}
                ),
            },
        )
    except OSError as exc:
        if delivery_event_id:
            _defer_delivery(record, ref, f"observer launch event could not be staged: {exc}")
        return _defer(
            runtime, payload, observers, ref, record, head=head,
            reason=f"observer lifecycle event could not be staged: {exc}",
        )
    intent = _write_launch_intent(runtime, payload, observers, ref, record, head, attempt)
    if intent is not None:
        # State that cannot be written means no head: a launch nobody can record is exactly how a
        # sprint ends up with two of them.
        discard_event(runtime, request_id)
        if delivery_event_id:
            _defer_delivery(record, ref, "observer launch delivery intent could not be persisted")
        return _defer(
            runtime, payload, observers, ref, record, head=head,
            reason=f"observer launch intent could not be persisted: {intent}",
        )
    _bind_codex_provider_ingress(runtime, payload, observers, ref, record)
    try:
        launched = runtime.host.prepare_observer(
            sprint,
            head,
            prompt=render_observer_prompt(
                sprint, skill_path=_first_path(delivery), delivery=record.delivery,
            ),
            # The identity of the head being launched, read off its record: the sprint it is the
            # observer of and the generation that tells this lifecycle of that reference from the
            # previous one. Not `ref` and not the sprint document, so nothing between the record
            # and the head can hand it another sprint's name.
            identity=observer_binding(record.sprint or ref, record.generation),
            heartbeat_run_id=str(record.head_run.get("run_id") or ""),
        )
    except ObserverLaunchAborted as exc:
        # The bring-up failed with its terminal still up. The staged event is dropped, because no
        # observer was launched, but the handle is kept so the next tick closes that terminal
        # first and only then opens the replacement.
        discard_event(runtime, request_id)
        _clear_launch_intent(record, head_possible=bool(exc.handle))
        if delivery_event_id:
            _defer_delivery(
                record, ref, f"observer replacement launch failed: {exc}",
                method="launch", evidence=dict(exc.evidence),
            )
        elif exc.evidence:
            # No batch was owed, so there is no retry state to write — but a prompt was delivered
            # to that head and did not land, and that is the sprint's evidence either way.
            _count_delivery_failure(
                record.delivery, "launch", f"observer launch prompt failed: {exc}",
                dict(exc.evidence),
            )
        if exc.handle:
            record.handle = exc.handle
            record.leaf = exc.leaf
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
        if delivery_event_id:
            _defer_delivery(
                record,
                ref,
                f"observer replacement launch failed: {getattr(exc, 'message', str(exc))}",
                method="launch",
                evidence=_evidence_of(exc),
            )
        return _defer(
            runtime, payload, observers, ref, record, head=head,
            reason=f"observer bring-up failed: {getattr(exc, 'message', str(exc))}",
        )
    now = time.time()
    record.head = head
    record.workspace = str(launched.get("workspace") or "")
    record.handle = str(launched.get("handle") or "")
    record.leaf = str(launched.get("leaf") or "")
    record.pid_file = str(launched.get("pid_file") or observer_pid_file(ref))
    record.run = launched.get("run") if isinstance(launched.get("run"), dict) else {}
    record.head_run = (
        dict(launched["head_run"]) if isinstance(launched.get("head_run"), dict) else record.head_run
    )
    if delivery_event_id and not record.wake_liveness.bound:
        # Non-Codex/fake hosts may only return the complete immutable HeadRun after launch.  Bind
        # the pending delivery now, before this result can be persisted or any next-tick probe can
        # run. Production Codex normally did this in the preflight launch intent above.
        record.wake_liveness = ObserverWakeLiveness.begin(record.head_run)
    # The host has answered, so the create-time pane identity belongs on the durable launch intent
    # before the ordinary state commit below.  A crash in the rest of this branch then adopts this
    # exact pane rather than falling back to the create-time handle or another inventory lookup.
    observers[ref] = record
    _persist_quietly(runtime, payload, observers)
    record.abandoned_handle = False
    record.launches = attempt
    record.pending_launch = 0
    record.bound = True
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
    if launched.get("prompt_delivered"):
        # Every launch prompt is one launch delivery, batch or no batch. The first launch of a
        # sprint carries no pending event and still puts a prompt in front of a head; a sprint
        # that lost that prompt has to be able to say so at closeout.
        _count_delivery_attempt(record.delivery, "launch")
        evidence = launched.get("delivery_evidence")
        if isinstance(evidence, dict) and evidence:
            record.delivery.last_evidence = dict(evidence)
    if delivery_event_id:
        record.delivery.stage = DeliveryStage.AWAITING_ACK
        record.delivery.sent_at = now
        record.delivery.held_since = now
        record.delivery.deadline = now + observer_ack_deadline_seconds()
        record.delivery.next_at = record.delivery.deadline
        record.delivery.reason = "replacement launch is pending confirmation for an unacknowledged card event"
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
    if delivery_event_id:
        outcome["delivery_id"] = record.delivery.delivery_id
        outcome["event_id"] = delivery_event_id
    # A failure here is not a lost head: the intent on disk still names this launch, and the next
    # tick adopts the head it started instead of opening a second one.
    if not _persist_quietly(runtime, payload, observers):
        outcome["state"] = "pending"
        outcome["status"] = "degraded"
    return _with_audit(outcome, commit_event(runtime, event))


def _bind_codex_provider_ingress(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
) -> None:
    """Bind the observer's persisted HeadRun to the only provider-event ingress."""
    stored = record.head_run if isinstance(record.head_run, dict) else {}
    if not stored.get("run_id"):
        return
    try:
        run = head_ops.HeadRun.from_json(stored)
    except (head_ops.HeadRunError, head_ops.TaskRefError):
        return
    if run.spec.adapter != "codex" or not isinstance(run.fanout_policy.get("provider_source"), dict):
        return

    def persist(updated: head_ops.HeadRun) -> None:
        if not updated.same_run(run):
            raise HostError("observer provider event writer was handed another HeadRun")
        updated_json = updated.to_json()
        existing = record.head_run if isinstance(record.head_run, dict) else {}
        if existing.get("run_id"):
            updated_json = merge_launch_head_run(existing, updated_json)
        record.head_run = updated_json
        record.workspace = updated.workspace or record.workspace
        record.handle = updated.handle or record.handle
        record.leaf = updated.leaf or record.leaf
        record.pid_file = updated.pid_file or record.pid_file
        observers[ref] = record
        if not _persist_quietly(runtime, payload, observers):
            raise OSError("observer provider event state could not be durably saved")

    def stop(updated: head_ops.HeadRun, reason: str) -> None:
        # ``stop_observer_head`` asks the host to identity-fence the run before it signals or
        # removes the worktree.  A foreign heartbeat is kept on the record and never signalled.
        stop_observer_head(runtime, record)

    def block(evidence: dict[str, Any]) -> None:
        record.state = "blocked"
        record.deferred_reason = (
            "Codex provider fan-out policy blocked observer: "
            f"{evidence.get('state') or 'unknown'}; {evidence.get('reason') or 'provider event observed'}"
        )
        record.last_action = "codex-provider-fanout-blocked"
        record.last_action_at = time.time()
        observers[ref] = record
        _persist_quietly(runtime, payload, observers)
        record_event(
            runtime,
            EVENT_PROVIDER_FANOUT_BLOCKED,
            ref,
            observer_request_id("codex-provider-event-blocked", ref, record.generation, record.launches),
            {"policy_evidence": dict(evidence), "head": record.head},
        )

    runtime.host.configure_codex_provider_ingress(run, persist=persist, stop=stop, block=block)


def _poll_codex_provider_ingress(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
) -> dict[str, Any] | None:
    if _precontract_unbound_observer_source(record):
        # This record predates the descriptor/baseline contract.  In particular, do not install
        # an ingress which would scan the workspace and bind a journal retroactively; the pending
        # event path replaces this exact old HeadRun under its normal identity fence.
        return None
    _bind_codex_provider_ingress(runtime, payload, observers, ref, record)
    if not isinstance(record.head_run, dict) or not record.head_run.get("run_id"):
        # A record from before runtime HeadRuns cannot be identified as Codex from this field.
        # It is not promoted to an allowed policy state, but it remains on its established
        # non-provider lifecycle path rather than being misclassified as a Codex source failure.
        return None
    try:
        run = head_ops.HeadRun.from_json(record.head_run)
    except (head_ops.HeadRunError, head_ops.TaskRefError):
        # Older launch intents had a run id before the full HeadRun was returned by the host.
        # They cannot be identified as an attested Codex source, so leave their existing launch
        # recovery path intact.  Current Codex preflight records a complete source-bearing run.
        return None
    if run.spec.adapter != "codex" or not isinstance(run.fanout_policy.get("provider_source"), dict):
        return None
    try:
        runtime.host.poll_codex_provider_ingress(run)
    except (CodexProviderSourceError, CodexFanoutRecordingError) as exc:
        return {
            "status": "blocked", "step": "observer-reconcile", "sprint": ref,
            "action": "codex-provider-fanout-blocked",
            "policy_evidence": {"kind": "codex_provider_fanout", "state": "unknown"},
            "reason": str(exc),
        }
    return None


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

    The workspace and pid file are asked of the host rather than taken from its answer: they are
    path arithmetic over the sprint reference, and the answer is exactly what a tick that dies
    mid-launch never sees. The intent also says the workspace may become registered with Orca, which
    outlives the launch, and the stop is what gives it back.
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
    run_id = uuid.uuid4().hex
    preflight_run: dict[str, Any] | None = None
    attest = getattr(runtime.host, "preflight_codex_run", None)
    if callable(attest):
        try:
            candidate = attest(
                head,
                role=OBSERVER_ROLE,
                workspace=workspace,
                task_ref=head_ops.TaskRef.sprint(ref),
                pid_file=pid_file,
                run_id=run_id,
            )
        except Exception as exc:
            return f"codex-fanout-policy: {type(exc).__name__}: {exc}"
        preflight_run = candidate.to_json()
    record.head = head
    record.workspace = workspace
    record.pid_file = pid_file
    record.head_run = preflight_run or {"run_id": run_id}
    if record.delivery.stage != DeliveryStage.IDLE and record.wake_liveness.terminal:
        # The old episode remains readable as evidence, but never participates in decisions for
        # the replacement.  Opening the new episode in this launch-intent write means a crash
        # cannot expose the new HeadRun with the retired run's baseline or recovery ladder.
        record.retired_wake_liveness = record.wake_liveness.to_json()
        record.wake_liveness = ObserverWakeLiveness.begin(record.head_run)
    record.pending_launch = attempt
    record.head_possible = True
    record.workspace_live = True
    # The identity is rendered into the command this intent is written for, so a head adopted from
    # the intent is a bound head: the flag belongs to the launch, not to its confirmation.
    record.bound = True
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
            if name == "delivery":
                record.delivery = ObserverDelivery.from_json(value)
            elif name == "wake_liveness":
                record.wake_liveness = ObserverWakeLiveness.from_json(value)
            else:
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

    Only the deferral fields are written, so the next tick retries from the same state. `keep_state`
    leaves even the state alone, for a record whose unresolved launch intent is the one thing the
    next tick must still see. Failed launches use a persisted exponential deadline; a deliberate
    drain is retried when the drain ends.
    """
    record = record or ObserverRecord(sprint=ref)
    policy_refusal = reason.startswith("codex-fanout-policy:")
    record.head = record.head or head
    if policy_refusal:
        record.state = "blocked"
        retry = False
        action = "codex-fanout-policy-blocked"
    elif not keep_state:
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
        "status": "blocked" if policy_refusal else "skipped",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": action,
        "head": record.head,
        "reason": record.deferred_reason,
    }
    if policy_refusal:
        outcome["policy_evidence"] = {"kind": "codex_provider_fanout", "state": "unknown"}
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

    False means the host refused the request and the head must be assumed alive, so the caller keeps
    the record and retries.
    """
    if not _needs_teardown(record):
        return True
    if observer_alive(record).get("identity_mismatch"):
        # The recorded PID is a live foreign process.  Keep the record and let an operator resolve
        # the owned pane rather than signalling or replacing over it.
        return False
    try:
        runtime.host.stop_observer(record)
    except HostError:
        return False
    record.handle = ""
    record.leaf = ""
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
    record.leaf = ""
    record.abandoned_handle = False
    record.state = STATE_STOPPED_BY_PAUSE
    record.stopped_reason = reason
    record.paused_at = now
    record.last_action = "stopped-by-pause"
    record.last_action_at = now


def retry_pending_observer_stops(runtime: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Retry the stops the host refused. Returns one row per pending stop it tried.

    The reconciliation pass does not run while the pipeline is frozen, so without this a head the
    freeze failed to stop would sit alive and unattended until the resume. Each row carries a
    `status` like any other action outcome, so a stop refused again reads as a `degraded` action
    rather than failing silently into a row nobody classified.
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

    Resume launches nothing itself: the tick's own reconciliation is the single bring-up path.
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
    runtime: Any, kind: str, reference: str, request_id: str, payload: dict[str, Any],
    *, outcome: str = "success",
) -> dict[str, Any] | None:
    """Put one lifecycle event on durable disk before the host call it describes.

    Returns the staged event for `commit_event`, or None when there is nothing to commit. Raises
    OSError when the pending copy cannot be written, which the caller answers by not touching the
    host.
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
        "outcome": outcome,
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
    the launch intent that precedes every bring-up keeps that decision recoverable.
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
    happened, and `TaskAudit.reconcile()` appends the pending copy on the next repair pass.
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


def render_observer_prompt(
    sprint: dict[str, Any], *, skill_path: str = "", delivery: ObserverDelivery | None = None,
) -> str:
    """The observer's launch document: the sprint entity, and the skill that says what to do.

    Data and one pointer. How a sprint is led belongs to the role skill alone, because two texts
    about the same job drift apart and the head then follows the stale one.
    """
    ref = str(sprint.get("ref") or "")
    repositories = [str(repo) for repo in (sprint.get("repositories") or [])]
    current = str(sprint.get("current_task") or "")
    budget = sprint.get("budget") if isinstance(sprint.get("budget"), dict) else {}
    marker = (
        (delivery.delivery_id, delivery.through_event)
        if delivery is not None and delivery.delivery_id and delivery.through_event
        else ("", "")
    )
    sections = [
        f"# Sprint {ref}",
        "",
        "## No subagents",
        "",
        "Run this observer turn in this head only. Do not spawn, create, delegate to, or manage",
        "subagents or child agents. Use ordinary tools directly when needed.",
        "",
        f"You are the observer head of this sprint. Your instructions are the `{OBSERVER_SKILL}`",
        "role skill and nothing else in this file:",
        "",
        f"- {skill_path or OBSERVER_SKILL}",
        "",
        "Read it first. Everything below is the sprint entity as the board holds it right now; the",
        "live copy is `python3 -P -m secretary sprint show --ref " + ref + "`.",
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
    if marker[0]:
        sections.extend([
            "## Pending delivery acknowledgement",
            "",
            f"delivery_id: {marker[0]}",
            f"through_event: {marker[1]}",
            "",
        ])
    evidence = delivery_evidence_summary(delivery) if delivery is not None else ""
    if evidence:
        # A replacement head has no memory of the wakes its predecessor never received, and the
        # closing resume is written by whichever head is up at the end. The counts come with the
        # document so that head can report them instead of reporting zero.
        sections.extend([
            "## Delivery evidence",
            "",
            evidence,
            "",
            "Report these counts in your closing resume; do not retry delivery yourself.",
            "",
        ])
    return "\n".join(sections)


def observer_launch_prompt() -> str:
    """Short pointer handed to the head on its command line; the document is in the workspace."""
    return (
        f"You are the sprint observer. The sprint is in {OBSERVER_PROMPT_FILE} at the workspace "
        f"root. Read it first, then follow the `{OBSERVER_SKILL}` role skill. Do not spawn, "
        "create, delegate to, or manage subagents; perform the observer turn in this head only."
    )


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
