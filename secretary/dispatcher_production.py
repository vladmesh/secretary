"""Production dispatcher loop for the shared Ready queue."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

from secretary._fsutil import try_file_lock, write_json
from secretary.checkpoint import checkpoint_snapshot
from secretary.dispatcher_pause_ops import auto_resume_expired_freeze
from secretary.dispatcher_state import (
    DispatcherRecord,
    attempt_request_id as _attempt_request_id,
    new_attempt_id,
    now_rfc3339,
    record_divergence,
    request_token,
)
from secretary.tasks import TaskError


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
    pilot = runtime.state.load()
    payload = runtime.production_state.load()
    legacy_pause = runtime.legacy_pause.snapshot()
    return {
        "status": "ok",
        "step": "production-observe",
        "phase": payload.get("phase", "new"),
        "owner": payload.get("owner", ""),
        "cutover_phase": pilot.get("phase", "new"),
        "cutover_committed": pilot.get("phase") == "cutover_committed",
        "legacy_pause": legacy_pause.to_json(),
        "pause": runtime.pause.summary(),
        "records": list((payload.get("records") or {}).keys()),
        "divergences": list((payload.get("controlled_divergences") or [])),
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

        records = runtime.production_state.records(payload)
        payload.update({
            "version": 1,
            "mode": "production",
            "phase": "production",
            "owner": runtime.owner,
        })
        payload.setdefault("owner_acquired_at", now_rfc3339())
        payload["last_tick_started_at"] = now_rfc3339()

        outcomes, errors, active_blocked = _advance_active(runtime, records, payload)
        # Drain: the cards already in flight keep riding their cycle above, nothing new is claimed.
        claims_allowed = pause.get("mode") != "drain"
        if claims_allowed and not active_blocked:
            try:
                ready_outcome = _production_claim_ready(runtime, records, payload)
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
        runtime.production_state.save(payload)
        result = {
            "status": "ok" if not errors else "degraded",
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
        return result


def _frozen_tick(
    runtime: Any, pause: dict[str, Any], auto_resume: dict[str, Any] | None
) -> dict[str, Any]:
    """A frozen tick moves no card, and still writes and pushes the checkpoint.

    A freeze stops the pipeline, not durability. Returning before the snapshot would turn every
    freeze into a hole in the checkpoint history and a push lag that only grows, and the restore path
    is exactly what an operator reaches for after the incident the freeze was called for.
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
    checkpoint = _write_checkpoint(runtime)
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
        result["checkpoint"] = checkpoint
    push = _push_checkpoint(runtime, payload)
    if push is not None:
        payload["checkpoint_push"] = push
        result["checkpoint_push"] = push
    if checkpoint is not None or push is not None:
        payload["last_frozen_tick_at"] = now_rfc3339()
        runtime.production_state.save(payload)
    return result


def _write_checkpoint(runtime: Any) -> dict[str, Any] | None:
    """Commit the normalized `state/` snapshot at the end of the tick.

    The gate is fail-closed on the checkpoint, not on the tick: a blocked
    snapshot leaves its reason in state and the next tick retries.
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

    Fail-closed on the checkpoint, not on the work: a push that cannot land
    records why, the lag keeps growing in plain sight, and the tick still
    reports on the cards it moved.
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
    """A dry tick reached the point where the real tick would have written.

    The operation and its arguments are the probe's actual result: they say what
    the next real tick will do, which is the only evidence that the tick's
    decision logic still works end to end.
    """

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

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ProbeHost:
    """Stands in for the command host. Path arithmetic passes; effects abort.

    ``gate_check`` is listed as an effect even though it mostly reads: it runs
    the project's setup and test commands, which is far too expensive and far
    too side-effecting for a health probe that a timer may call every minute.
    """

    EFFECTS = (
        "prepare_worker",
        "restart_worker",
        "verify_worker_result",
        "gate_check",
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

    A wrapper object would not work: the tick's own methods are bound to the
    real runtime, so ``self.writer`` inside them would still reach the live
    board. A shallow copy shares every collaborator by reference and rebinds
    those methods to an object whose writer, host and state cannot write.
    """
    probe = copy.copy(runtime)
    probe.writer = _ProbeWriter(runtime.writer)
    probe.host = _ProbeHost(runtime.host)
    probe.production_state = _ProbeState(runtime.production_state)
    return probe


def production_probe(runtime: Any) -> dict[str, Any]:
    """Run a real tick with every write replaced by an abort.

    This is the health gate, so it has to fail for the same reasons the real
    tick fails: it takes the same singleton lock, runs the same mutation guards,
    scans the same task states and drives the same ``_tick_task`` decision for
    each of them. The only difference is that the first write per task raises
    instead of landing, and the state file is never saved.
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

        active = _production_tasks(runtime, {"in_progress", "validate"})
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
) -> tuple[list[dict[str, Any]], list[dict[str, str]], bool]:
    outcomes: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    active_blocked = False
    for task in _production_tasks(runtime, {"in_progress", "validate"}):
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
            active_blocked = True
        outcomes.append(outcome)
    return outcomes, errors, active_blocked


def _production_mutation_guard(runtime: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    cutover = runtime.state.load()
    if cutover.get("phase") != "cutover_committed":
        return {
            "status": "blocked",
            "step": "production-guard",
            "reason": "production cutover is not committed",
            "cutover_phase": cutover.get("phase", "new"),
        }
    if not cutover.get("legacy_decommissioned"):
        legacy_guard = runtime._legacy_pause_guard("production-guard")
        if legacy_guard is not None:
            legacy_guard["reason"] = "old dispatcher hard freeze is not confirmed: " + str(
                legacy_guard.get("reason") or ""
            )
            return legacy_guard
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
    return runtime._tick_task(task, records, payload, attempt_id)


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
) -> dict[str, Any] | None:
    active_code_projects = {
        str(task.get("project") or "")
        for task in _production_tasks(runtime, {"in_progress", "validate"})
        if task.get("type") == "code" and task.get("project") and not is_steward_report(task)
    }
    skipped: list[dict[str, str]] = []
    for task in _production_tasks(runtime, {"ready"}):
        if is_steward_report(task):
            skipped.append({"ref": task["ref"], "reason": "steward report is not claimable"})
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
