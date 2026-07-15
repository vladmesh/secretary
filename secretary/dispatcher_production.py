"""Production dispatcher loop for the shared Ready queue."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from secretary._fsutil import try_file_lock, write_json
from secretary.dispatcher_state import (
    DispatcherRecord,
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
        "records": list((payload.get("records") or {}).keys()),
        "divergences": list((payload.get("controlled_divergences") or [])),
    }


def production_tick(runtime: Any) -> dict[str, Any]:
    with try_file_lock(runtime.production_state.tick_lock) as acquired:
        if not acquired:
            return {
                "status": "blocked",
                "step": "production-tick",
                "reason": "production dispatcher singleton lock is held",
            }
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
        if not active_blocked:
            try:
                ready_outcome = _production_claim_ready(runtime, records, payload)
            except Exception as exc:
                errors.append(_unexpected_error("", exc))
            else:
                if ready_outcome is not None:
                    outcomes.append(ready_outcome)

        runtime.production_state.put_records(payload, records)
        payload["last_tick_finished_at"] = now_rfc3339()
        runtime.production_state.save(payload)
        return {
            "status": "ok" if not errors else "degraded",
            "step": "production-tick",
            "owner": runtime.owner,
            "actions": outcomes,
            "errors": errors,
        }


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
    legacy_guard = runtime._legacy_pause_guard("production-guard")
    if legacy_guard is not None:
        legacy_guard["reason"] = "old dispatcher hard freeze is not confirmed: " + str(legacy_guard.get("reason") or "")
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
    mismatch = _production_active_mismatch(task, record, payload)
    if mismatch is not None:
        return mismatch
    attempt_id = record.attempt_id if record is not None and record.attempt_id else production_adopt_attempt_id(ref)
    return runtime._tick_task(task, records, payload, attempt_id)


def _production_active_mismatch(
    task: dict[str, Any],
    record: DispatcherRecord | None,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if record is None:
        return None
    actual_worker = task.get("claim", {}).get("worker")
    if actual_worker in (None, record.worker):
        return None
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
        try:
            outcome = runtime._claim(task, records, payload, attempt_id)
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
