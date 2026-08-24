"""Production dispatcher loop for the shared Ready queue."""

from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path
from typing import Any

from secretary._fsutil import try_file_lock, write_json
from secretary.board.models import Event, EventKind
from secretary.checkpoint import checkpoint_snapshot
from secretary.dispatcher_launch import (
    REVIEW_ROLE,
    WORKER_ROLE,
    forget_role_head,
    launch_intent,
    stop_launch_intent,
)
from secretary.dispatcher_observer import (
    observer_snapshot,
    reconcile_observers,
    retry_pending_observer_stops,
)
from secretary.dispatcher_observer_fence import fenced_task, observer_fence
from secretary.dispatcher_pause_ops import auto_resume_expired_freeze
from secretary.dispatcher_state import (
    DispatcherRecord,
    close_divergence,
    divergence_is_open,
    is_claim_skip,
    new_attempt_id,
    now_rfc3339,
    record_divergence,
    request_token,
)
from secretary.dispatcher_state import (
    attempt_request_id as _attempt_request_id,
)
from secretary.dispatcher_types import STOPPED_BY_RECONCILIATION, HostError
from secretary.sprints import SprintWriter, budget_thresholds
from secretary.tasks import ACTIVE_STATES, TaskError

# Durable telemetry of terminal production ticks, written into production-state.json under
# `tick_telemetry` (secretary-833). It is the only current record of how a tick ENDED: the
# `last_tick_*` timestamps say when one ran, not whether it did any good, so a dispatcher failing
# every minute looks exactly like a healthy one to anything reading them.
#
# Two readers outside this module consume it, both through
# `triggered_agents.runtime.production_telemetry`:
#   * `python3 -m triggered_agents health` builds the pipeline line from `last`/`last_healthy_at`,
#     so a fresh failing tick can never read as a healthy one.
#   * the steward's `scan` reports one signal per INCIDENT — a continuous run of unhealthy ticks —
#     against its own watermark, keyed on the monotonic `incident_total`/`recovery_total` counters,
#     so an ordinary healthy tick in between never consumes an incident the steward has not looked
#     at yet, and a second failing tick of the same outage does not open a second one. `generation`
#     says which history those counters belong to.
# Keep the shape and the meaning of those fields in step with that reader.
TICK_TELEMETRY_UNHEALTHY_KEPT = 50
TICK_TELEMETRY_ERRORS_KEPT = 5
TICK_TELEMETRY_DEGRADATIONS_KEPT = 5
# A frozen tick moves no card on purpose, and the probe already reports a freeze as ok rather than
# as a dispatcher that cannot work — telemetry says the same, otherwise every freeze would read as
# an outage for as long as it lasts.
HEALTHY_TICK_STATUSES = frozenset({"ok", "skipped"})
# An action outcome saying the dispatcher could not finish the operation it started: a head of an
# unresolved launch that would not stop (`launch-intent-stop-unconfirmed`), a runtime it could not
# reach. Nothing raises on those paths, so `errors` stays empty and the tick used to return `ok`
# and record itself healthy right over the degradation (secretary-833 review, round 3).
#
# `critical` is here for the opposite reason to `blocked`. An observer fence stops a sprint's whole
# project because the role that was supposed to be watching it is not there, which is the state the
# pipeline health line exists to show. A tick that fenced a sprint and still reported `ok` would put
# the sprint's cards on hold with nothing anywhere saying so.
#
# `blocked` is deliberately not here. A card parked in Blocked is the dispatcher doing its job:
# the board carries the reason, the steward already reports it as a `new_blocked` signal, and the
# tick's exit code drives the systemd unit's result — a correctly blocked card must not put the
# production unit into `failed` and the pipeline health line into RED.
DEGRADED_ACTION_STATUSES = frozenset({"degraded", "failed", "critical"})


def degraded_actions(outcomes: Any) -> list[dict[str, Any]]:
    """Action outcomes of a tick that report a failed operation, in the order they happened."""
    return [
        outcome
        for outcome in (outcomes or [])
        if isinstance(outcome, dict)
        and str(outcome.get("status") or "") in DEGRADED_ACTION_STATUSES
    ]


def _counter(value: Any) -> int:
    """A monotonic counter read back from state, defaulting to 0 for anything unusable."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def record_tick_telemetry(payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Fold one terminal tick outcome into `payload["tick_telemetry"]` and return it.

    Called with the result the tick is about to return, right before the state is saved, so the
    durable record and the returned outcome cannot disagree. A tick that never reaches a save
    deliberately records nothing: taking the state file would write outside the lock or across an
    ownership fence, and both leave their own evidence already.
    """
    telemetry = payload.get("tick_telemetry")
    telemetry = dict(telemetry) if isinstance(telemetry, dict) else {}
    # Identity of this telemetry history, minted once and then carried forever. `unhealthy_total`
    # alone cannot tell a reader that the state file was replaced: a restore or a rebuilt
    # installation starts a different history whose counter may land on the same number the
    # steward's watermark already holds, and every failure after it would be deduped away against
    # a count from a history that no longer exists (secretary-833 review, round 4). The generation
    # changes exactly when the history does, which is what the reader keys its reset on.
    if not str(telemetry.get("generation") or ""):
        telemetry["generation"] = uuid.uuid4().hex
    seq = _counter(telemetry.get("tick_seq")) + 1
    status = str(result.get("status") or "")
    errors = [error for error in (result.get("errors") or []) if isinstance(error, dict)]
    # Health is read off the action outcomes as well as the top-level status, not off the status
    # alone: a caller that builds `ok` while one of its actions reports a failed operation would
    # otherwise store a healthy tick over it, and health, the unhealthy ring and the steward's
    # counter would all keep reading green through the degradation.
    degradations = degraded_actions(result.get("actions"))
    entry = {
        "seq": seq,
        "at": now_rfc3339(),
        "status": status,
        "step": str(result.get("step") or ""),
        "healthy": status in HEALTHY_TICK_STATUSES and not degradations,
        "reason": str(result.get("reason") or ""),
        "actions": len(result.get("actions") or []),
        "error_count": len(errors),
        "degraded_count": len(degradations),
        # The diagnostic, not just the count: what the steward and an operator need is which
        # operation could not finish and on which card, and the outcome is gone once the tick
        # returns. Bounded for the same reason as `errors`.
        "degradations": [
            {
                # Observer outcomes name their subject `sprint`, card ones `ref`/`pilot_ref`.
                # All three land in the same field, or a degraded observer action would be
                # recorded without the one thing an operator needs to find the head.
                "ref": str(outcome.get("ref") or outcome.get("pilot_ref")
                           or outcome.get("sprint") or ""),
                "step": str(outcome.get("step") or ""),
                "status": str(outcome.get("status") or ""),
                "action": str(outcome.get("action") or ""),
                "reason": str(outcome.get("reason") or ""),
            }
            for outcome in degradations[:TICK_TELEMETRY_DEGRADATIONS_KEPT]
        ],
        # Bounded on purpose: the diagnostic value is in what failed and why, and an unbounded
        # copy of every error of every tick would grow the state file the whole pipeline reads
        # and writes each minute.
        "errors": [
            {
                "ref": str(error.get("ref") or ""),
                "code": str(error.get("code") or ""),
                "message": str(error.get("message") or ""),
            }
            for error in errors[:TICK_TELEMETRY_ERRORS_KEPT]
        ],
    }
    telemetry["tick_seq"] = seq
    telemetry["last"] = entry
    unhealthy = [item for item in (telemetry.get("unhealthy") or []) if isinstance(item, dict)]
    if entry["healthy"]:
        telemetry["last_healthy_at"] = entry["at"]
    else:
        unhealthy.append(entry)
        telemetry["unhealthy_total"] = _counter(telemetry.get("unhealthy_total")) + 1
    telemetry["unhealthy"] = unhealthy[-TICK_TELEMETRY_UNHEALTHY_KEPT:]
    telemetry.setdefault("unhealthy_total", _counter(telemetry.get("unhealthy_total")))
    _record_incident(telemetry, entry)
    payload["tick_telemetry"] = telemetry
    return telemetry


def _record_incident(telemetry: dict[str, Any], entry: dict[str, Any]) -> None:
    """Fold one tick into the open incident, closing it when the tick is healthy again.

    An incident is one continuous run of unhealthy ticks: the tick that finds none open opens one and
    bumps `incident_total`, every unhealthy tick after it extends that record, and the first healthy
    tick closes it into `recovery` and bumps `recovery_total`. Both counters are monotonic within a
    generation, because a reader dedupes on them and a counter that can go down would let a later
    event consume an earlier one the reader has not seen.

    The tick that opened the incident is kept whole under `opened`, so its cause survives into the
    recovery record.
    """
    incident = telemetry.get("incident")
    incident = dict(incident) if isinstance(incident, dict) else None
    if entry["healthy"]:
        if incident:
            telemetry["recovery"] = {
                **incident,
                "recovered_seq": entry["seq"],
                "recovered_at": entry["at"],
                "recovered_status": entry["status"],
            }
            telemetry["recovery_total"] = _counter(telemetry.get("recovery_total")) + 1
            incident = None
    elif incident:
        incident["unhealthy_ticks"] = _counter(incident.get("unhealthy_ticks")) + 1
        incident["last_seq"] = entry["seq"]
        incident["last_at"] = entry["at"]
    else:
        incident = {
            "id": uuid.uuid4().hex,
            "opened_seq": entry["seq"],
            "opened_at": entry["at"],
            "last_seq": entry["seq"],
            "last_at": entry["at"],
            "unhealthy_ticks": 1,
            "opened": entry,
        }
        telemetry["incident_total"] = _counter(telemetry.get("incident_total")) + 1
    telemetry["incident"] = incident
    telemetry.setdefault("recovery", None)
    telemetry.setdefault("incident_total", _counter(telemetry.get("incident_total")))
    telemetry.setdefault("recovery_total", _counter(telemetry.get("recovery_total")))


def _record_failed_tick(runtime: Any, exc: BaseException) -> None:
    """Record a tick that died on an exception instead of returning a result.

    The tick that raises never reaches its own save, and a board outage raises on the very first
    read, so without this the pipeline keeps reporting the last healthy tick for the whole freshness
    window while nothing is moving. Written as a terminal unhealthy tick, in the same shape as a
    degraded one.

    The state is re-read rather than reused, because the payload the failed tick was mutating is half
    applied. Recording is best effort: the tick's own exception is the one that must reach the caller.
    """
    try:
        payload = runtime.production_state.load()
        record_tick_telemetry(payload, {
            "status": "failed",
            "step": "production-tick",
            "reason": f"tick raised {type(exc).__name__}",
            "errors": [{
                "ref": "",
                # TaskError carries the backend's own code (backend_unavailable and friends), which
                # is the one thing that tells an operator a board outage from a product bug.
                "code": str(getattr(exc, "code", "") or "") or "unexpected_error",
                "message": type(exc).__name__,
            }],
        })
        runtime.production_state.save(payload)
    except Exception:
        return


class ProductionState:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "dispatcher"
        self.path = self.root / "production-state.json"
        self.tick_lock = self.root / "production-tick.lock"
        self.run_lock = self.root / "production-run.lock"

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {"version": 1, "phase": "new"}
        except (OSError, ValueError, UnicodeError):
            payload = {"version": 1, "phase": "unavailable"}
        if not isinstance(payload, dict):
            payload = {"version": 1, "phase": "unavailable"}
        payload.setdefault("version", 1)
        payload.setdefault("mode", "production")
        payload.setdefault("phase", "new")
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        write_json(self.path, payload)

    def records(self, payload: dict[str, Any]) -> dict[str, DispatcherRecord]:
        raw = payload.get("records") or {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(ref): DispatcherRecord.from_json(record)
            for ref, record in raw.items()
            if isinstance(record, dict)
        }

    def put_records(self, payload: dict[str, Any], records: dict[str, DispatcherRecord]) -> None:
        payload["records"] = {ref: record.to_json() for ref, record in sorted(records.items())}


def production_observe(runtime: Any) -> dict[str, Any]:
    payload = runtime.production_state.load()
    return {
        "status": "ok",
        "step": "production-observe",
        "phase": payload.get("phase", "new"),
        "owner": payload.get("owner", ""),
        "pause": runtime.pause.summary(),
        "records": list((payload.get("records") or {}).keys()),
        "observers": observer_snapshot(payload),
        "resource_health": runtime.head_health.snapshot(),
        "divergences": list(payload.get("controlled_divergences") or []),
        "open_divergences": [
            divergence
            for divergence in (payload.get("controlled_divergences") or [])
            if isinstance(divergence, dict) and divergence_is_open(divergence)
        ],
        "checkpoint": checkpoint_snapshot(
            runtime.catalog.instance_dir,
            write_state=payload.get("checkpoint"),
            push_state=payload.get("checkpoint_push"),
        ),
    }


def production_tick(runtime: Any) -> dict[str, Any]:
    with try_file_lock(runtime.production_state.tick_lock) as acquired:
        if not acquired:
            return {
                "status": "blocked",
                "step": "production-tick",
                "reason": "production dispatcher singleton lock is held",
            }
        pause = runtime.pause.summary()
        auto_resume: dict[str, Any] | None = None
        if pause.get("mode") == "freeze":
            auto_resume = auto_resume_expired_freeze(runtime, source="tick")
            if auto_resume is not None and auto_resume.get("resumed"):
                pause = runtime.pause.summary()
        if pause.get("mode") == "freeze":
            return _frozen_tick(runtime, pause, auto_resume)
        payload = runtime.production_state.load()
        guard = _production_mutation_guard(runtime, payload)
        if guard is not None:
            return guard

        # Past the guard the state is this dispatcher's to write, so every way out of the tick
        # from here on leaves a durable record, including the ways that raise. A Kanboard outage
        # makes the first board read inside raise TaskError; without this the preceding healthy
        # tick would keep answering for the pipeline until it aged out of the freshness window.
        try:
            return _production_tick_body(runtime, payload, pause, auto_resume)
        except Exception as exc:
            _record_failed_tick(runtime, exc)
            raise


def _committing_records(
    runtime: Any, payload: dict[str, Any], records: dict[str, DispatcherRecord]
):
    """Lend the host a flush of this tick's records, for the span the tick holds them.

    A head lifecycle transition has to be durable *before* the host call it describes, and the host
    is the one object that has the record in hand and not the file it lives in.
    """
    return runtime.host.committing(lambda: runtime.save_records(payload, records))


def _production_tick_body(
    runtime: Any,
    payload: dict[str, Any],
    pause: dict[str, Any],
    auto_resume: dict[str, Any] | None,
) -> dict[str, Any]:
    """The tick proper, from the first board read to the durable record of how it ended."""
    records = runtime.production_state.records(payload)
    with _committing_records(runtime, payload, records):
        return _production_tick_work(runtime, payload, records, pause, auto_resume)


def _production_tick_work(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    pause: dict[str, Any],
    auto_resume: dict[str, Any] | None,
) -> dict[str, Any]:
    """Everything the tick does with the records it has loaded."""
    payload.update({
        "version": 1,
        "mode": "production",
        "phase": "production",
        "owner": runtime.owner,
    })
    payload.setdefault("owner_acquired_at", now_rfc3339())
    payload["last_tick_started_at"] = now_rfc3339()

    observer_errors: list[dict[str, str]] = []
    # Before anything moves. A sprint whose declared observer is corrupt, dead or unresolvable
    # holds its own reservations, and the cards in them are not advanced, reconciled or claimed
    # this tick. Pure: it reads the board and the records, and launches nothing.
    try:
        fence = observer_fence(runtime, payload)
    except Exception as exc:
        # The fence is the thing that decides what may move, so a fence that could not finish
        # decides nothing — and an empty fence is not "nothing", it is "everything may move". The
        # tick ends here instead, before reconciliation, advancement, budget accounting, observer
        # reconciliation and Ready claims. A sprint whose critical outcome could not be staged
        # (an unwritable audit, a full volume) is exactly the sprint whose cards must not advance.
        return _fence_failed_tick(runtime, payload, exc)
    fence_outcomes = list(fence.get("outcomes") or [])

    cycle = _production_tasks(runtime, set(ACTIVE_STATES))
    active_tasks = [task for task in cycle if not fenced_task(fence, task)]
    active_refs = {str(task.get("ref") or "") for task in active_tasks}
    # A fenced card drops out of `active_refs`, which is exactly the shape reconciliation reads as
    # an orphaned record and settles. Its refs are handed over separately so the record survives
    # the fence untouched instead of being cleaned up as work that left the cycle.
    fenced_refs = set(fence.get("refs") or ()) | {
        str(task.get("ref") or "") for task in cycle if fenced_task(fence, task)
    }
    reconcile_outcomes = _reconcile_production(
        runtime, records, payload, active_refs, fenced_refs=fenced_refs, fence=fence
    )
    # Distinct from `last_tick_started_at`/`last_tick_finished_at`, which existed before
    # reconciliation did: those are stamped by every tick regardless of code version, so a
    # pre-deployment host with an old dispatcher would otherwise read as "reconciliation ran"
    # on the strength of a field that predates the reconciliation pass itself.
    payload["last_reconciled_at"] = now_rfc3339()
    outcomes, errors, blocked_scopes = _advance_active(runtime, records, payload, active_tasks)
    outcomes = fence_outcomes + reconcile_outcomes + outcomes
    try:
        outcomes += _reconcile_sprint_budget(runtime)
    except Exception as exc:
        errors.append(_unexpected_error("", exc))
    # Reconcile after budget accounting. A hard limit reached by the card work above then
    # stops an already-live observer in this tick and prevents a replacement launch.
    try:
        outcomes += reconcile_observers(
            runtime, payload, pause_mode=str(pause.get("mode") or "")
        )
    except Exception as exc:
        observer_errors.append(_unexpected_error("", exc))
    errors = observer_errors + errors
    # Drain: the cards already in flight keep riding their cycle above, nothing new is claimed.
    claims_allowed = pause.get("mode") != "drain"
    if claims_allowed:
        try:
            ready_outcome = _production_claim_ready(
                runtime, records, payload, fence=fence, blocked_scopes=blocked_scopes
            )
        except Exception as exc:
            errors.append(_unexpected_error("", exc))
        else:
            if ready_outcome is not None:
                outcomes.append(ready_outcome)

    runtime.production_state.put_records(payload, records)
    checkpoint = _write_checkpoint(runtime)
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
    push = _push_checkpoint(runtime, payload)
    if push is not None:
        payload["checkpoint_push"] = push
    payload["last_tick_finished_at"] = now_rfc3339()
    # The tick ends degraded on a failed operation it caught as well as on one that raised:
    # reconciliation and the active pass both return `degraded` outcomes without adding an error
    # (a launch head that would not stop), and reporting that tick as `ok` told the unit, the
    # health line and the steward alike that nothing had happened.
    # A checkpoint gate protects the only recoverable copy of the live board.
    # The card work may continue for this tick, but reporting it as healthy hid
    # an active durability incident from the unit health line and steward.
    checkpoint_blocked = bool(checkpoint and checkpoint.get("status") == "blocked")
    if checkpoint_blocked:
        outcomes.append(_checkpoint_degradation(checkpoint))
    result = {
        "status": "ok" if not errors and not degraded_actions(outcomes) and not checkpoint_blocked else "degraded",
        "step": "production-tick",
        "owner": runtime.owner,
        "actions": outcomes,
        "errors": errors,
    }
    if pause.get("paused"):
        result["pause"] = pause
    if auto_resume is not None:
        result["auto_resume"] = auto_resume
    if checkpoint is not None:
        result["checkpoint"] = checkpoint
    if push is not None:
        result["checkpoint_push"] = push
    # Recorded before the save, off the result this call is about to return: the durable
    # record of how the tick ended and the answer its caller gets are the same object.
    record_tick_telemetry(payload, result)
    runtime.production_state.save(payload)
    return result


def _frozen_tick(
    runtime: Any, pause: dict[str, Any], auto_resume: dict[str, Any] | None
) -> dict[str, Any]:
    """A frozen tick moves no card, and still writes and pushes the checkpoint.

    A freeze stops the pipeline, not durability: returning before the snapshot would turn every
    freeze into a hole in the checkpoint history and a push lag that only grows.
    """
    result: dict[str, Any] = {
        "status": "skipped",
        "step": "production-tick",
        "reason": "pipeline is frozen by pause",
        "pause": pause,
    }
    if auto_resume is not None:
        result["auto_resume"] = auto_resume
    payload = runtime.production_state.load()
    guard = _production_mutation_guard(runtime, payload)
    if guard is not None:
        # No state to write the snapshot's own bookkeeping into, so the checkpoint is not attempted:
        # the freeze is reported with the guard's reason instead of a snapshot that cannot be recorded.
        result["durability"] = {
            "status": "skipped",
            "reason": str(guard.get("reason") or "production state is not writable"),
        }
        return result
    try:
        return _frozen_tick_body(runtime, payload, result)
    except Exception as exc:
        # Same rule as the working tick: past the guard, a raise still leaves a durable record,
        # or a freeze whose checkpoint machinery is broken would read as a healthy pipeline.
        _record_failed_tick(runtime, exc)
        raise


def _frozen_tick_body(
    runtime: Any, payload: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    # Reconciliation does not run while frozen, so this is the only place a stop the host refused
    # during the freeze is retried. Nothing else about an observer is touched here.
    try:
        observer_stops = retry_pending_observer_stops(runtime, payload)
    except Exception as exc:
        observer_stops = []
        # A freeze itself is healthy; a retry that raised inside it is not, and telemetry keys
        # health off the status. Say so in both places rather than reporting the frozen tick as a
        # clean one that happens to carry an error field nobody reads.
        result["observer_stop_error"] = _unexpected_error("", exc)
        result["errors"] = [result["observer_stop_error"]]
        result["status"] = "degraded"
    if observer_stops:
        result["observer_stops"] = observer_stops
        # The retried stops are this tick's action outcomes, so a stop the host refused again is
        # read like any other degraded action: it turns the terminal tick degraded and its reason
        # reaches the durable record. Before that the row was classified by nobody and a freeze
        # sitting on a head it could not take down recorded itself healthy, leaving health OK and
        # the steward without a signal (secretary-833 review, round 4).
        result["actions"] = observer_stops
        if degraded_actions(observer_stops):
            result["status"] = "degraded"
    checkpoint = _write_checkpoint(runtime)
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
        result["checkpoint"] = checkpoint
        if checkpoint.get("status") == "blocked":
            result["status"] = "degraded"
            actions = list(result.get("actions") or ())
            actions.append(_checkpoint_degradation(checkpoint))
            result["actions"] = actions
    push = _push_checkpoint(runtime, payload)
    if push is not None:
        payload["checkpoint_push"] = push
        result["checkpoint_push"] = push
    payload["last_frozen_tick_at"] = now_rfc3339()
    # A freeze is a deliberate stop, so this tick is recorded as a healthy terminal one — and it
    # is saved even when nothing else about this tick changed the state, or a long freeze would
    # age the last healthy tick out and report the pipeline as dead instead of frozen.
    record_tick_telemetry(payload, result)
    runtime.production_state.save(payload)
    return result


def _checkpoint_degradation(checkpoint: dict[str, Any]) -> dict[str, str]:
    """Expose a durability gate failure as an ordinary degraded action."""
    return {
        "status": "degraded",
        "step": "checkpoint",
        "action": "blocked",
        "reason": str(checkpoint.get("reason") or "checkpoint gate blocked"),
    }


def _write_checkpoint(runtime: Any) -> dict[str, Any] | None:
    """Commit the normalized `state/` snapshot at the end of the tick.

    The gate is fail-closed on the checkpoint, not on the tick: a blocked snapshot leaves its reason
    in state and the next tick retries.
    """
    writer = getattr(runtime, "checkpoint", None)
    if writer is None:
        return None
    try:
        result = writer.write().to_json()
    except Exception as exc:
        result = {"status": "blocked", "reason": f"{type(exc).__name__}: {exc}"}
    result["at"] = now_rfc3339()
    return result


def _push_checkpoint(runtime: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Run the 30-minute push window at the end of the tick.

    Fail-closed on the checkpoint, not on the work: a push that cannot land records why, the lag
    keeps growing in plain sight, and the tick still reports on the cards it moved.
    """
    pusher = getattr(runtime, "checkpoint_push", None)
    if pusher is None:
        return None
    state = payload.get("checkpoint_push")
    state = dict(state) if isinstance(state, dict) else {}
    try:
        return pusher.push(state)
    except Exception as exc:
        state.update(
            {
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
                # Stamp the window too, or a pusher that raises every call turns
                # the tick into a retry loop against a broken remote.
                "attempted_epoch": time.time(),
                "attempted_at": now_rfc3339(),
                "failures": int(state.get("failures") or 0) + 1,
            }
        )
        return state


class ProbeAbort(Exception):
    """A dry tick reached the point where the real tick would have written."""

    def __init__(self, operation: str, detail: dict[str, Any]) -> None:
        super().__init__(operation)
        self.operation = operation
        self.detail = detail


class _ProbeWriter:
    """Stands in for the board writer. Every write aborts the task's probe."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def move(self, **kwargs: Any) -> None:
        raise ProbeAbort("move", {"ref": kwargs.get("reference", ""), "to": kwargs.get("target", "")})

    def claim(self, *args: Any, **kwargs: Any) -> None:
        raise ProbeAbort("claim", {"ref": kwargs.get("reference", "") or (args[0] if args else "")})

    def comment(self, *args: Any, **kwargs: Any) -> None:
        raise ProbeAbort("comment", {"ref": kwargs.get("reference", "") or (args[0] if args else "")})

    def routing(self, *args: Any, **kwargs: Any) -> None:
        raise ProbeAbort("routing", {"ref": kwargs.get("reference", "") or (args[0] if args else "")})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ProbeHost:
    """Stands in for the command host. Path arithmetic passes; effects abort.

    ``gate_check`` is listed as an effect even though it mostly reads: it runs the project's setup
    and test commands, far too expensive for a health probe a timer may call every minute.
    """

    EFFECTS = (
        "prepare_worker",
        "prepare_observer",
        "stop_observer",
        "restart_worker",
        # Settling an unresolved launch intent ends heads (a worker frozen for its adopted reviewer,
        # a workspace stopped because its launch left nothing running). A probe that walked those
        # paths for real would kill live heads while reporting what the tick "would" do.
        "stop_workspace",
        "stop_review",
        "freeze_worker",
        "retain_worker",
        "resume_worker",
        # Typing into a live head is an effect even though it stops nothing: a probe that ran it
        # would interrupt a working worker with a prompt about a round the probe is only modelling.
        "prompt_worker_report",
        "verify_worker_result",
        "gate_check",
        "rerun_failed_ci",
        "complete_green",
        "teardown",
        "stop",
    )

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def restore_workspace(self, task: dict[str, Any], worker: str) -> str:
        return self._inner.restore_workspace(task, worker)

    def __getattr__(self, name: str) -> Any:
        if name in self.EFFECTS:
            def effect(*args: Any, **kwargs: Any) -> Any:
                raise ProbeAbort(name, {})
            return effect
        return getattr(self._inner, name)


class _ProbeState:
    """Reads through to the real state; a save is an abort, never a write."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def save(self, payload: dict[str, Any]) -> None:
        raise ProbeAbort("save-state", {})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _probe_runtime(runtime: Any) -> Any:
    """The real runtime with only its writers swapped out.

    A wrapper object would not work: the tick's own methods are bound to the real runtime, so
    ``self.writer`` inside them would still reach the live board. A shallow copy shares every
    collaborator by reference and rebinds those methods to an object that cannot write.
    """
    probe = copy.copy(runtime)
    probe.writer = _ProbeWriter(runtime.writer)
    probe.host = _ProbeHost(runtime.host)
    probe.production_state = _ProbeState(runtime.production_state)
    return probe


def production_probe(runtime: Any) -> dict[str, Any]:
    """Run a real tick with every write replaced by an abort.

    This is the health gate, so it fails for the same reasons the real tick fails: same singleton
    lock, same mutation guards, same task states, same ``_tick_task`` decision. The only difference
    is that the first write per task raises instead of landing.
    """
    with try_file_lock(runtime.production_state.tick_lock) as acquired:
        if not acquired:
            return {
                "status": "blocked",
                "step": "production-probe",
                "reason": "production dispatcher singleton lock is held",
            }
        pause = runtime.pause.summary()
        if pause.get("mode") == "freeze":
            # A frozen pipeline is stopped on purpose, so the health gate reports it as such
            # instead of as a dispatcher that cannot move cards.
            return {
                "status": "ok",
                "step": "production-probe",
                "owner": runtime.owner,
                "reason": "pipeline is frozen by pause",
                "pause": pause,
                "would": [],
                "errors": [],
            }
        payload = runtime.production_state.load()
        guard = _production_mutation_guard(runtime, payload)
        if guard is not None:
            guard["step"] = "production-probe"
            return guard

        probe = _probe_runtime(runtime)
        records = runtime.production_state.records(payload)
        would: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        active = _production_tasks(runtime, set(ACTIVE_STATES))
        for task in active:
            if is_steward_report(task):
                continue
            would.append(_probe_one(probe, task, dict(records), dict(payload)))

        ready = [task for task in _production_tasks(runtime, {"ready"}) if not is_steward_report(task)]
        # The claim path is the one a health gate most needs to exercise: it is
        # where capacity, predecessors and per-project concurrency are decided,
        # and none of that is visible from the active scan. The real tick skips
        # it when an active task blocks, but an aborted probe cannot know that a
        # task would have blocked, so this is always evaluated and reported as
        # its own entry rather than as a prediction of the next tick's one move.
        # Under a drain the tick would not reach the claim path at all, so probing it would report
        # a move the next tick is not going to make.
        if pause.get("mode") != "drain":
            would.append(_probe_ready(probe, dict(records), dict(payload)))
        for entry in would:
            if entry.get("code"):
                errors.append({"ref": entry["ref"], "code": entry["code"], "message": entry.get("message", "")})

        return {
            "status": "ok" if not errors else "degraded",
            "step": "production-probe",
            "owner": runtime.owner,
            "active": [str(task.get("ref") or "") for task in active],
            "ready": [str(task.get("ref") or "") for task in ready],
            "pause": pause,
            "would": would,
            "errors": errors,
        }


def _probe_one(
    probe: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    ref = str(task.get("ref") or "")
    try:
        outcome = _production_tick_active(probe, task, records, payload)
    except ProbeAbort as abort:
        return {"ref": ref, "operation": abort.operation, "detail": abort.detail}
    except TaskError as exc:
        return {"ref": ref, "operation": "error", "code": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        return {"ref": ref, "operation": "error", "code": "unexpected_error", "message": exc.__class__.__name__}
    return {"ref": ref, "operation": "none", "status": outcome.get("status", ""), "step": outcome.get("step", "")}


def _probe_ready(
    probe: Any,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        outcome = _production_claim_ready(probe, records, payload)
    except ProbeAbort as abort:
        return {"ref": abort.detail.get("ref", ""), "operation": abort.operation, "detail": abort.detail}
    except TaskError as exc:
        return {"ref": "", "operation": "error", "code": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        return {"ref": "", "operation": "error", "code": "unexpected_error", "message": exc.__class__.__name__}
    if outcome is None:
        return {"ref": "", "operation": "none", "step": "production-claim", "status": "idle"}
    return {"ref": str(outcome.get("ref") or ""), "operation": "none", "step": "production-claim", "status": outcome.get("status", "")}


def production_run(
    runtime: Any,
    *,
    interval_seconds: float,
    max_interval_seconds: float,
    max_ticks: int | None = None,
) -> dict[str, Any]:
    interval_seconds = max(1.0, interval_seconds)
    max_interval_seconds = max(interval_seconds, max_interval_seconds)
    with try_file_lock(runtime.production_state.run_lock) as acquired:
        if not acquired:
            return {
                "status": "blocked",
                "step": "production-run",
                "reason": "production dispatcher run loop is already active",
            }
        ticks = 0
        failures = 0
        last: dict[str, Any] = {"status": "ok", "step": "production-run", "action": "start"}
        while max_ticks is None or ticks < max_ticks:
            try:
                last = runtime.production_tick()
                failures = 0 if last.get("status") == "ok" else failures + 1
            except Exception as exc:
                failures += 1
                last = {
                    "status": "degraded",
                    "step": "production-run",
                    "error": _unexpected_error("", exc),
                }
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            delay = min(max_interval_seconds, interval_seconds * (2 ** min(failures, 5)))
            time.sleep(delay)
        return {"status": "ok", "step": "production-run", "ticks": ticks, "last": last}


def production_adopt_attempt_id(reference: str) -> str:
    return "production-adopt-" + request_token(reference)


def is_steward_report(task: dict[str, Any]) -> bool:
    return task.get("extensions", {}).get("kanboard", {}).get("steward_report") == "1"


def _advance_active(
    runtime: Any,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    active_tasks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, set[str]]]:
    """Advance every active card, and report which scopes a blocked card closed for claims.

    The scopes are the blocked card's sprint and its project: a blocked card says nothing about the
    work of another sprint in another project, so the claim suppression it causes is keyed by those
    two rather than installation-wide.
    """
    outcomes: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    blocked_scopes: dict[str, set[str]] = {"sprints": set(), "projects": set()}
    for task in active_tasks:
        if is_steward_report(task):
            continue
        try:
            outcome = _production_tick_active(runtime, task, records, payload)
        except TaskError as exc:
            errors.append({"ref": str(task.get("ref") or ""), "code": exc.code, "message": exc.message})
            continue
        except Exception as exc:
            errors.append(_unexpected_error(str(task.get("ref") or ""), exc))
            continue
        if outcome.get("status") == "blocked":
            sprint_ref = str(task.get("sprint") or "")
            project = str(task.get("project") or "")
            if sprint_ref:
                blocked_scopes["sprints"].add(sprint_ref)
            if project:
                blocked_scopes["projects"].add(project)
        outcomes.append(outcome)
    return outcomes, errors, blocked_scopes


def _fence_failed_tick(
    runtime: Any, payload: dict[str, Any], exc: Exception
) -> dict[str, Any]:
    """End the tick without touching a card, and leave a durable record of why.

    The state is still saved: the fence may have opened or cleared fences and refreshed its snapshot
    before it raised, and those are what the next tick reads.
    """
    result = {
        "status": "critical",
        "step": "production-tick",
        "owner": runtime.owner,
        "action": "observer-fence-unavailable",
        "reason": (
            "the observer fence could not be evaluated, so no card was advanced, reconciled or "
            f"claimed this tick: {type(exc).__name__}: {exc}"
        ),
        "actions": [],
        "errors": [_unexpected_error("", exc)],
    }
    payload["last_tick_finished_at"] = now_rfc3339()
    record_tick_telemetry(payload, result)
    try:
        runtime.production_state.save(payload)
    except Exception:
        # Reporting the refusal matters more than recording it: the cards are already untouched.
        result["state_save"] = "failed"
    return result


def _reconcile_production(
    runtime: Any,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    active_refs: set[str],
    fenced_refs: set[str] | None = None,
    fence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Reconcile records and controlled divergences against the current board.

    `_advance_active` only looks at cards the board currently reports as in_progress/validate, so a
    record whose card left that cycle from outside the dispatcher is invisible to it forever. A record
    owns every head launched for the card, so reconciliation must settle those heads before it can
    remove the record.
    """
    outcomes: list[dict[str, Any]] = []
    state_cache: dict[str, str | None] = {}
    # A fenced sprint's records are left exactly as they are. Reconciliation stops heads and
    # removes records, and both are mutations of work the fence exists to hold still.
    fenced_refs = fenced_refs or set()
    fence = fence or {}
    task_cache: dict[str, dict[str, Any] | None] = {}

    def card(ref: str) -> dict[str, Any] | None:
        if ref not in task_cache:
            task_cache[ref] = _current_card(runtime, ref)
        return task_cache[ref]

    def card_state(ref: str) -> str | None:
        if ref not in state_cache:
            task = card(ref)
            state_cache[ref] = None if task is None else str(task.get("state") or "unknown")
        return state_cache[ref]

    def fenced(ref: str) -> bool:
        """Whether this record's card belongs to a fenced sprint, decided a second way.

        `fenced_refs` is the fence's own inventory. This asks the card the tick has just read, by its
        sprint link and its project, so a card missing from that inventory is still not settled while its
        sprint is fenced.
        """
        if ref in fenced_refs:
            return True
        task = card(ref)
        return task is not None and fenced_task(fence, task)

    for ref in sorted(ref for ref in records if ref not in active_refs):
        if fenced(ref):
            continue
        # `active_refs` is a snapshot taken before this pass; the board can move the card back
        # into the active cycle between that snapshot and this loop (a PO race). The snapshot is
        # only ever a reason to look, never proof of anything: only the live state fetched right
        # here, immediately before removal, decides whether the record is actually orphaned.
        state = card_state(ref)
        if state is None or state in ACTIVE_STATES:
            continue
        record = records[ref]
        if (
            state == "blocked"
            and record.gate_state == "green"
            and record.state == "review_starting"
            and record.review_infra_failures > 0
        ):
            # The infrastructure ceiling deliberately hands this exact candidate to an operator.
            # Stopping its retained worker and dropping its receipt one tick later would make the
            # instruction to relaunch only the reviewer impossible to follow.
            continue
        intent_action = str(launch_intent(record).get("action") or "")
        stopped = _stop_record_heads(runtime, record, ref, state)
        if stopped is not None:
            outcomes.append(stopped)
            continue
        # Keep the settled record for `_claim`: its workspace is the checkout the next attempt
        # must reuse, while every head and unresolved intent it used to own is now confirmed gone.
        if state == "ready":
            continue
        records.pop(ref)
        outcomes.append({
            "status": "ok",
            "step": "production-reconcile",
            "ref": ref,
            "action": "record-removed",
            "reason": "card left the active dispatcher cycle",
            "record_state": record.state,
            "card_state": state,
            **({"stopped_launch": intent_action} if intent_action else {}),
        })

    divergences = payload.get("controlled_divergences")
    open_refs = {
        str(divergence.get("pilot_ref") or "")
        for divergence in divergences
        if isinstance(divergence, dict) and divergence_is_open(divergence)
    } if isinstance(divergences, list) else set()
    for ref in sorted(open_refs - active_refs):
        if fenced(ref):
            continue
        state = card_state(ref)
        if state is None or state in ACTIVE_STATES:
            continue
        closed_ids = _close_divergences_for_ref(payload, ref, state)
        if closed_ids:
            outcomes.append({
                "status": "ok",
                "step": "production-reconcile",
                "ref": ref,
                "action": "divergences-closed",
                "reason": "card left the active dispatcher cycle",
                "card_state": state,
                "divergence_ids": closed_ids,
            })
    return outcomes


def _stop_record_heads(
    runtime: Any,
    record: DispatcherRecord,
    ref: str,
    card_state: str,
) -> dict[str, Any] | None:
    """Stop every head owned by one record, or preserve the record and report the refusal."""
    intent = launch_intent(record)
    if intent:
        failure = stop_launch_intent(runtime, record, intent, str(intent.get("role") or ""))
        if failure is not None:
            return {
                "status": "degraded",
                "step": "production-reconcile",
                "ref": ref,
                "action": "launch-intent-stop-unconfirmed",
                "reason": f"the head of an unresolved launch could not be stopped: {failure}",
                "record_state": record.state,
                "card_state": card_state,
            }
        # `stop_launch_intent` either settled the role through its saved identity or used the
        # legacy workspace fallback itself. Do not turn the former back into a workspace-wide
        # stop after it has just closed the one named head.
        record.workspace_settled = True
    if not record.needs_settling():
        return None
    try:
        # A recorded pane identity is narrower than the workspace. In particular a create-time
        # handle can alias to another head, so the host resolves a saved leaf to its current handle
        # before closing it. Keep the workspace-wide stop only for legacy records that never got
        # any role identity at all: there is no other safe way to settle an unnamed possible head.
        if record.owns_head(REVIEW_ROLE):
            runtime.host.stop_head(record, REVIEW_ROLE, STOPPED_BY_RECONCILIATION)
        if record.owns_head(WORKER_ROLE):
            runtime.host.stop_head(record, WORKER_ROLE, STOPPED_BY_RECONCILIATION)
        if not record.owns_head() and record.workspace and not record.workspace_settled:
            runtime.host.stop_workspace(record)
    except HostError as exc:
        return {
            "status": "degraded",
            "step": "production-reconcile",
            "ref": ref,
            "action": "head-stop-unconfirmed",
            "reason": f"the card left the active cycle, but its heads could not be stopped: {exc}",
            "record_state": record.state,
            "card_state": card_state,
        }
    forget_role_head(record, WORKER_ROLE)
    forget_role_head(record, REVIEW_ROLE)
    if record.workspace:
        record.workspace_settled = True
    return None


def _close_divergences_for_ref(payload: dict[str, Any], ref: str, card_state: str) -> list[str]:
    divergences = payload.get("controlled_divergences")
    if not isinstance(divergences, list):
        return []
    closed_ids: list[str] = []
    for divergence in divergences:
        if not isinstance(divergence, dict) or divergence.get("pilot_ref") != ref:
            continue
        if not divergence_is_open(divergence):
            continue
        close_divergence(divergence, f"card left the active dispatcher cycle (state={card_state})")
        closed_ids.append(str(divergence.get("id") or ""))
    return closed_ids


def _current_card(runtime: Any, ref: str) -> dict[str, Any] | None:
    """The card as the board has it right now, or None when it could not be asked.

    A `None` here means "skip this ref this tick", never "treat as gone": a transient backend error
    must not look like the card left the cycle. A card the board says is gone comes back as a
    `not_found` state.
    """
    try:
        return runtime.reader.show(ref)
    except TaskError as exc:
        return {"ref": ref, "state": "not_found"} if exc.code == "not_found" else None
    except Exception:
        return None


def _production_mutation_guard(runtime: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Whether this tick may write the production state at all.

    Two fences, both read off the production state itself: another owner holds the pipeline, or the
    state is in a phase this dispatcher does not write. An installation with no production state yet
    reads as phase `new` and passes, which is how the first tick takes ownership.
    """
    owner = str(payload.get("owner") or "")
    if owner and owner != runtime.owner:
        return {
            "status": "blocked",
            "step": "production-guard",
            "reason": "production ownership fence is held by another owner",
            "owner": owner,
        }
    phase = str(payload.get("phase") or "new")
    if phase not in {"new", "production"}:
        return {
            "status": "blocked",
            "step": "production-guard",
            "reason": "production state is not writable",
            "phase": phase,
        }
    return None


def _production_tasks(runtime: Any, states: set[str]) -> list[dict[str, Any]]:
    return sorted(runtime.reader.list(states=states), key=_task_sort_key)


def _production_tick_active(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    ref = task["ref"]
    task = runtime.reader.show(ref)
    record = records.get(ref)
    mismatch = _production_active_mismatch(runtime, task, record, records, payload)
    if mismatch is not None:
        return mismatch
    attempt_id = record.attempt_id if record is not None and record.attempt_id else production_adopt_attempt_id(ref)
    outcome = runtime._tick_task(task, records, payload, attempt_id)
    return outcome


def _production_active_mismatch(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord | None,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if record is None:
        return None
    actual_worker = task.get("claim", {}).get("worker")
    if actual_worker in (None, record.worker):
        return None
    stopped = _stop_record_heads(
        runtime, record, task["ref"], str(task.get("state") or "")
    )
    if stopped is not None:
        stopped["step"] = "production-recovery"
        stopped["reason"] = (
            "active task claim no longer matches production record, and its heads could not be "
            f"stopped: {stopped['reason']}"
        )
        return stopped
    runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=task["ref"],
        target="blocked",
        reason="production recovery blocked: active task claim no longer matches production record",
        request_id=_attempt_request_id(record.attempt_id, "active-mismatch-blocked", task["ref"]),
    )
    divergence = record_divergence(
        payload,
        record.attempt_id,
        task["ref"],
        "production-recovery",
        "active_claim_mismatch",
        expected={"worker": record.worker, "state": task.get("state")},
        actual={"worker": actual_worker, "state": task.get("state")},
        details=["worker"],
    )
    records.pop(task["ref"], None)
    return {
        "status": "blocked",
        "step": "production-recovery",
        "ref": task["ref"],
        "reason": "active task claim no longer matches production record",
        "divergence_id": divergence["id"],
    }


def _production_claim_ready(
    runtime: Any,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    fence: dict[str, Any] | None = None,
    blocked_scopes: dict[str, set[str]] | None = None,
) -> dict[str, Any] | None:
    fence = fence or {"sprints": set(), "projects": set(), "refs": set()}
    blocked_sprints = set((blocked_scopes or {}).get("sprints") or ())
    blocked_projects = set((blocked_scopes or {}).get("projects") or ())
    active_code_projects = {
        str(task.get("project") or "")
        for task in _production_tasks(runtime, set(ACTIVE_STATES))
        if task.get("type") == "code" and task.get("project") and not is_steward_report(task)
    }
    skipped: list[dict[str, str]] = []
    sprint_cache: dict[str, dict[str, Any]] = {}
    sprint_errors: dict[str, str] = {}
    for task in _production_tasks(runtime, {"ready"}):
        if is_steward_report(task):
            skipped.append({"ref": task["ref"], "reason": "steward report is not claimable"})
            continue
        if fenced_task(fence, task):
            skipped.append({
                "ref": task["ref"],
                "reason": "the sprint holding this project has no working declared observer",
            })
            continue
        sprint_ref = str(task.get("sprint") or "")
        project = str(task.get("project") or "")
        # A card that went blocked this cycle closes its own sprint and its own project to new
        # claims, and nothing beyond them: another sprint working on other projects keeps moving.
        if sprint_ref and sprint_ref in blocked_sprints:
            skipped.append({
                "ref": task["ref"],
                "reason": "this sprint has a card blocked in this cycle",
            })
            continue
        if project and project in blocked_projects:
            skipped.append({
                "ref": task["ref"],
                "reason": "this project has a card blocked in this cycle",
            })
            continue
        if sprint_ref:
            sprint = sprint_cache.get(sprint_ref)
            if sprint is None and sprint_ref not in sprint_errors:
                try:
                    sprint = runtime.sprints.show(sprint_ref, include_cards=False)
                except TaskError as exc:
                    sprint_errors[sprint_ref] = exc.message
                else:
                    sprint_cache[sprint_ref] = sprint
            if sprint_ref in sprint_errors:
                skipped.append({
                    "ref": task["ref"],
                    "reason": "linked sprint cannot be read: " + sprint_errors[sprint_ref],
                })
                continue
            assert sprint is not None
            if sprint.get("status") != "open":
                skipped.append({"ref": task["ref"], "reason": "linked sprint is stopped or closed"})
                continue
        if task.get("type") == "code" and task.get("project") in active_code_projects:
            skipped.append({
                "ref": task["ref"],
                "reason": "project has an active code task",
            })
            continue
        attempt_id = new_attempt_id()
        resume_workspaces = payload.get("resume_workspaces")
        resume_workspace = isinstance(resume_workspaces, dict) and task["ref"] in resume_workspaces
        try:
            outcome = runtime._claim(
                task,
                records,
                payload,
                attempt_id,
                resume_workspace=resume_workspace,
            )
        except TaskError as exc:
            if exc.code in {"capacity_reached", "claim_conflict", "predecessor_open"}:
                skipped.append({"ref": task["ref"], "reason": exc.message})
                continue
            raise
        # Every claim-skip, not one of them: the pass moves to the next Ready card whatever made
        # this one unclaimable. A skip the scan does not recognise falls through to the return
        # below and ends the pass, which stops cards that had somewhere to go — see
        # CLAIM_SKIP_ACTIONS for the registry a new skip has to join.
        if is_claim_skip(outcome):
            skipped.append({
                "ref": task["ref"],
                "reason": str(outcome.get("reason") or "head resource is not ready"),
            })
            continue
        if skipped:
            outcome["skipped_ready"] = skipped
        return outcome
    if skipped:
        return {
            "status": "skipped",
            "step": "production-claim",
            "reason": "no claimable Ready task",
            "skipped_ready": skipped,
        }
    return None


def _task_sort_key(task: dict[str, Any]) -> tuple[int, str, str]:
    return (int(task.get("position") or 0), str(task.get("ref") or ""), str(task.get("id") or ""))


def _unexpected_error(reference: str, exc: Exception) -> dict[str, str]:
    return {
        "ref": reference,
        "code": "unexpected_error",
        "message": exc.__class__.__name__,
    }


def _reconcile_sprint_budget(runtime: Any) -> list[dict[str, Any]]:
    """Charge each durable card event once, using its audit identity as the budget request id."""
    instance = getattr(runtime.catalog, "instance", {})
    thresholds = budget_thresholds(instance if isinstance(instance, dict) else None)
    writer = SprintWriter(
        runtime.reader.client,
        data_dir=Path(runtime.audit.board_dir).parent,
        thresholds=thresholds,
    )
    events = runtime.audit.events()
    committed = {str(event.get("request_id") or "") for event in events}
    outcomes: list[dict[str, Any]] = []
    sprint_cache: dict[str, str | None] = {}
    for event in events:
        reference = str(event.get("ref") or "")
        if not reference or reference.startswith("sprint:"):
            continue
        event_type = _budget_event_type(event)
        if event_type is None:
            continue
        identity = str(event.get("event_id") or event.get("request_id") or "")
        if not identity:
            continue
        request_id = "sprint-budget-" + identity
        if request_id in committed:
            continue
        sprint = _event_sprint(runtime, event, sprint_cache)
        if sprint is None:
            # A transient board failure must remain eligible for the next tick.  Only a successful
            # lookup that proves the card is unlinked gets a durable terminal marker below.
            continue
        if not sprint:
            _record_unlinked_budget_event(runtime, event, request_id, identity, event_type)
            continue
        result = writer.record_budget(
            role="dispatcher", actor=runtime.owner, reference=sprint, event_type=event_type,
            request_id=request_id, source_event_id=identity,
        )
        outcomes.append({
            "status": "ok", "step": "sprint-budget", "sprint": sprint,
            "ref": reference, "event_type": event_type,
            "hard_stopped": result["sprint"]["status"] == "stopped",
        })
    return outcomes


def _event_sprint(runtime: Any, event: dict[str, Any], cache: dict[str, str | None]) -> str | None:
    reference = str(event.get("ref") or "")
    if reference in cache:
        return cache[reference]
    payload = event.get("payload")
    if event.get("kind") == "created" and isinstance(payload, dict):
        sprint = str(payload.get("sprint") or "")
    else:
        try:
            sprint = str(runtime.reader.show(reference).get("sprint") or "")
        except TaskError:
            sprint = None
    cache[reference] = sprint
    return sprint


def _record_unlinked_budget_event(
    runtime: Any,
    event: dict[str, Any],
    request_id: str,
    source_event_id: str,
    event_type: str,
) -> None:
    """Durably remember that a budget-shaped card event has no sprint to charge.

    The audit request id is deliberately the same one a charge would use. A card's sprint link is
    assigned at creation and cannot appear later, so this is a terminal result.
    """
    runtime.audit.append(request_id, {
        "event_id": "evt_budget_unlinked_" + source_event_id,
        "schema_version": 1,
        "occurred_at": now_rfc3339(),
        "actor": {"role": "dispatcher", "id": runtime.owner},
        "kind": "budget_unlinked",
        "outcome": "success",
        "task_id": str(event.get("task_id") or ""),
        "ref": str(event.get("ref") or ""),
        "backend": dict(event.get("backend") or {}),
        "request_id": request_id,
        "payload": {"source_event_id": source_event_id, "event_type": event_type},
    })


def _budget_event_type(event: dict[str, Any]) -> str | None:
    payload = event.get("data") if event.get("record_type") == Event.RECORD_TYPE else event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    marker = str(payload.get("marker") or "")
    if event.get("kind") in {"verdict", EventKind.CARD_VERDICTED.value} and marker == "review:red":
        return "red_review"
    if event.get("record_type") == Event.RECORD_TYPE:
        transition = event.get("transition") if isinstance(event.get("transition"), dict) else {}
        target = str(transition.get("target") or "")
        source = str(transition.get("source") or "")
    elif event.get("kind") == "moved":
        target = str(payload.get("to") or "")
        source = str(payload.get("from") or "")
    else:
        target = source = ""
    if target:
        request_id = str(event.get("request_id") or "")
        if target == "blocked":
            return "blocked"
        if target == "ready" and source in ACTIVE_STATES:
            return "preempt"
        if target == "in_progress" and "gate-red" in request_id:
            return "red_ci"
    if event.get("kind") == "created":
        budget_event = str(payload.get("budget_event") or "")
        if budget_event in {"recreated_task", "hotfix"}:
            return budget_event
    return None
