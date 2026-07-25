"""Pause, resume and pause-status for the production dispatcher.

The contract is the legacy `dispatcher.pause()` docstring, now carried out over the records the
production dispatcher actually keeps (`<data_dir>/dispatcher/production-state.json`) instead of the
legacy `cards.json`, which has been empty since 2026-07-21.

Head transitions live here rather than in the tick because pause and resume are operator commands
that run between ticks: they take the production tick lock, so a tick in flight finishes before a
freeze stops its heads, and the next tick sees a settled flag.
"""

from __future__ import annotations

import os
import time
from typing import Any

from secretary._fsutil import file_lock
from secretary.dispatcher_helpers import _last_marker
from secretary.dispatcher_pause import (
    PAUSE_MODES,
    auto_resume_status,
    clear_legacy_mirror,
    normalize_pause_mode,
    on_resume_text,
    pause_payload,
    write_legacy_mirror,
)
from secretary.dispatcher_review import end_review_pane, start_review
from secretary.dispatcher_state import DispatcherRecord, now_rfc3339
from secretary.dispatcher_types import DispatcherError
from secretary.tasks import TaskError

WORKER_MARKERS = {"report:done", "report:blocked"}
REVIEW_MARKERS = {"review:green", "review:red"}


def pause(
    runtime: Any,
    *,
    mode: str,
    actor: str,
    reason: str,
    exclude_workspaces: list[str] | None = None,
) -> dict[str, Any]:
    """Set the pipeline-wide pause. Freeze also stops the live heads of every tracked card.

    Idempotent in the same mode. Pausing in the other mode while already paused is refused: a drain
    quietly upgraded to a freeze would stop a head whose card the caller still expects to be riding
    its cycle unattended. Resume first, then pause in the new mode.

    `exclude_workspaces` is the narrow initiator exception a freeze needs when the caller is itself
    a head: `secretary backup create` runs from a worker workspace and must keep it alive while
    everything else is stopped. An excluded head is neither stopped nor relaunched on resume.
    """
    resolved = normalize_pause_mode(mode)
    if not resolved:
        raise DispatcherError(
            "validation", f"unknown pause mode {mode!r} (one of {', '.join(PAUSE_MODES)})", 2
        )
    actor = (actor or "").strip()
    reason = (reason or "").strip()
    if not actor:
        raise DispatcherError("validation", "pause requires a non-empty actor", 2)
    if not reason:
        raise DispatcherError("validation", "pause requires a non-empty reason", 2)

    with file_lock(runtime.production_state.tick_lock):
        current = runtime.pause.load()
        current_mode = normalize_pause_mode(current.get("mode"))
        if current_mode == resolved:
            return {**pause_status(runtime), "step": "pause", "action": "noop"}
        if current_mode:
            raise DispatcherError(
                "pause_conflict",
                f"pipeline is already paused ({current_mode}), resume before pausing {resolved}",
                3,
            )
        since = now_rfc3339()
        stopped_worker: list[str] = []
        stopped_reviewer: list[str] = []
        excluded: list[str] = []
        warnings: list[str] = []
        if resolved == "freeze":
            payload = runtime.production_state.load()
            if _state_unreadable(payload):
                # The records cannot be read, so no head can be identified — and writing the state
                # back would replace an unreadable file with an empty one. The flag still goes down:
                # the next tick reads it and advances nothing, which is what a freeze is for.
                warnings.append(
                    "production state is unreadable: the flag is set but no head was stopped"
                )
            else:
                records = runtime.production_state.records(payload)
                stopped_worker, stopped_reviewer, excluded = _freeze_heads(
                    runtime, records, _excluded_paths(exclude_workspaces)
                )
                runtime.production_state.put_records(payload, records)
                runtime.production_state.save(payload)
        mirror = write_legacy_mirror(mode=resolved, actor=actor, reason=reason, since=since)
        runtime.pause.save(
            pause_payload(
                mode=resolved,
                actor=actor,
                reason=reason,
                since=since,
                stopped_worker=stopped_worker,
                stopped_reviewer=stopped_reviewer,
                excluded_worker=excluded,
                legacy_mirror=mirror,
            )
        )
    status = pause_status(runtime)
    status["warnings"] = [*status.get("warnings", []), *warnings]
    return {**status, "step": "pause", "action": "paused"}


def resume(runtime: Any, *, actor: str) -> dict[str, Any]:
    """Clear the pause and put back what a freeze stopped.

    A drain never stopped anything, so lifting it is just dropping the flag. A freeze relaunches the
    stopped worker and reviewer heads in their existing workspaces and hands every wait watchdog a
    fresh window, so the frozen stretch does not read afterwards as a head that went silent.

    Report-first, like the legacy resume: a card whose head reported while frozen is left for the
    next tick to move instead of getting a fresh head launched into work that is already finished.
    """
    with file_lock(runtime.production_state.tick_lock):
        return resume_locked(runtime, actor=actor)


def auto_resume_expired_freeze(runtime: Any, *, source: str) -> dict[str, Any] | None:
    """Lift an automation-owned freeze that outlived its TTL. None when there is nothing to lift.

    The legacy `dispatcher.pause()` contract: a freeze set by automation (`secretary-backup` above
    all) expires, a freeze set by a person does not. `secretary backup create` resumes in a `finally`,
    so a backup that is killed, or that runs longer than the TTL, would otherwise leave the
    dispatcher frozen forever — stopped heads, no watchdog recovery, and no tick to notice.

    Called by the tick, which already holds the tick lock, so this takes no lock of its own and
    resume runs in the same critical section as the check.
    """
    decision = auto_resume_status(runtime.pause.load())
    if not decision.get("eligible"):
        return None
    outcome = {**decision, "source": source}
    try:
        result = resume_locked(runtime, actor="auto-resume")
    except Exception as exc:  # noqa: BLE001 — a failed recovery is reported, it does not kill the tick
        return {**outcome, "resumed": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        **outcome,
        "resumed": True,
        "relaunched": result.get("relaunched", []),
        "parked": result.get("parked", []),
        "skipped": result.get("skipped", []),
    }


def resume_locked(runtime: Any, *, actor: str) -> dict[str, Any]:
    """`resume` without taking the tick lock, for callers that already hold it."""
    state = runtime.pause.load()
    mode = normalize_pause_mode(state.get("mode"))
    if not mode:
        runtime.pause.clear()
        return {**pause_status(runtime), "step": "resume", "action": "noop"}
    buckets: dict[str, list[str]] = {"relaunched": [], "parked": [], "skipped": []}
    warnings: list[str] = []
    if mode == "freeze":
        payload = runtime.production_state.load()
        if _state_unreadable(payload):
            warnings.append(
                "production state is unreadable: the pause is lifted but no head was relaunched"
            )
        else:
            records = runtime.production_state.records(payload)
            buckets = _resume_heads(runtime, state, records)
            _refresh_watchdog_windows(records)
            runtime.production_state.put_records(payload, records)
            payload["resumed_at"] = now_rfc3339()
            payload["resumed_by"] = actor or runtime.owner
            runtime.production_state.save(payload)
    mirror = clear_legacy_mirror(state)
    runtime.pause.clear()
    status = pause_status(runtime)
    status["warnings"] = [*status.get("warnings", []), *warnings]
    return {
        **status,
        "step": "resume",
        "action": "resumed",
        "resumed_mode": mode,
        "legacy_mirror": mirror,
        **buckets,
    }


def pause_status(runtime: Any) -> dict[str, Any]:
    """Pause state of the dispatcher that actually runs, with the flag's path in the live data
    plane. Per-card head lines say whether a head is missing because a freeze stopped it or because
    it is simply not up yet, so a pause never reads as a dead head."""
    state = runtime.pause.load()
    mode = normalize_pause_mode(state.get("mode"))
    payload = runtime.production_state.load()
    records = runtime.production_state.records(payload)
    stopped_worker = list(state.get("stopped_worker") or [])
    stopped_reviewer = list(state.get("stopped_reviewer") or [])
    excluded_worker = list(state.get("excluded_worker") or [])
    warnings: list[str] = []
    if state.get("corrupt"):
        warnings.append(f"pause file is unreadable and read as not paused: {runtime.pause.path}")
    out: dict[str, Any] = {
        "status": "ok",
        "step": "pause-status",
        "paused": bool(mode),
        "mode": mode,
        "pause_file": str(runtime.pause.path),
        "dispatcher": {
            "kind": "production",
            "phase": str(payload.get("phase") or "new"),
            "owner": str(payload.get("owner") or ""),
            "state_file": str(runtime.production_state.path),
        },
        "since": str(state.get("since") or ""),
        "actor": str(state.get("actor") or ""),
        "reason": str(state.get("reason") or ""),
        "stopped_worker": stopped_worker,
        "stopped_reviewer": stopped_reviewer,
        "excluded_worker": excluded_worker,
        "on_resume": on_resume_text(mode, stopped_worker, stopped_reviewer),
        # Operator-visible answer to "will this lift itself?": an automation-owned freeze expires,
        # a freeze a person set is a maintenance window and is held until they resume it.
        "auto_resume": auto_resume_status(state),
        "legacy_mirror": state.get("legacy_mirror") if isinstance(state.get("legacy_mirror"), dict) else {},
        "heads": [_head_line(ref, record) for ref, record in sorted(records.items())],
        "warnings": warnings,
    }
    return out


def _head_line(ref: str, record: DispatcherRecord) -> dict[str, Any]:
    return {
        "ref": ref,
        "state": record.state,
        "worker": _head_state(record.handle, record.paused_worker_at),
        "reviewer": _head_state(record.review_handle, record.paused_reviewer_at),
        "workspace": record.workspace,
    }


def _head_state(handle: str, paused_at: float) -> str:
    if handle:
        return "running"
    return "stopped-by-pause" if paused_at else "not-running"


def _state_unreadable(payload: dict[str, Any]) -> bool:
    return str(payload.get("phase") or "") == "unavailable"


def _excluded_paths(exclude_workspaces: list[str] | None) -> set[str]:
    return {os.path.abspath(os.path.expanduser(path)) for path in (exclude_workspaces or []) if path}


def _freeze_heads(
    runtime: Any, records: dict[str, DispatcherRecord], excluded_paths: set[str]
) -> tuple[list[str], list[str], list[str]]:
    """Stop the live heads a freeze must stop, and mark why they are gone.

    `stop` only, never `teardown`: the worktree and everything uncommitted in it stays exactly as
    the head left it. The reviewer's pane is closed through its own lifecycle helper first, so
    stopping the worktree's terminals cannot be mistaken later for a reviewer that vanished.
    """
    stopped_worker: list[str] = []
    stopped_reviewer: list[str] = []
    excluded: list[str] = []
    now = time.time()
    for ref, record in sorted(records.items()):
        if record.workspace and os.path.abspath(record.workspace) in excluded_paths:
            excluded.append(ref)
            continue
        if record.review_handle:
            end_review_pane(runtime.host, record)
            record.paused_reviewer_at = now
            stopped_reviewer.append(ref)
        if record.handle:
            runtime.host.stop(record)
            record.handle = ""
            record.paused_worker_at = now
            stopped_worker.append(ref)
    return stopped_worker, stopped_reviewer, excluded


def _resume_heads(
    runtime: Any, state: dict[str, Any], records: dict[str, DispatcherRecord]
) -> dict[str, list[str]]:
    relaunched: list[str] = []
    parked: list[str] = []
    skipped: list[str] = []

    for ref in list(state.get("stopped_reviewer") or []):
        record = records.get(ref)
        if record is None:
            skipped.append(f"{ref}:reviewer")
            continue
        if record.review_handle:
            # A resume that got this far and then failed to drop the flag is retried by the next
            # tick's TTL check; relaunching a head that is already back would double it.
            parked.append(f"{ref}:reviewer")
            continue
        record.paused_reviewer_at = 0.0
        task = _load_task(runtime, ref)
        if task is None or task.get("state") != "validate":
            skipped.append(f"{ref}:reviewer")
            continue
        if _last_marker(task, record.review_baseline, REVIEW_MARKERS):
            # The reviewer posted its verdict before the freeze stopped it. The next tick acts on
            # that verdict; a fresh reviewer would review a card that is already judged.
            parked.append(f"{ref}:reviewer")
            continue
        outcome = start_review(runtime, task, records, record, record.attempt_id, action="review-resumed")
        (relaunched if outcome.get("status") == "ok" else skipped).append(f"{ref}:reviewer")

    for ref in list(state.get("stopped_worker") or []):
        record = records.get(ref)
        if record is None:
            skipped.append(f"{ref}:worker")
            continue
        if record.handle:
            parked.append(f"{ref}:worker")
            continue
        record.paused_worker_at = 0.0
        task = _load_task(runtime, ref)
        if task is None or task.get("state") != "in_progress" or not record.workspace:
            # Not a card that wants a worker head any more: moved on during the freeze, or its
            # worker was already frozen for review. Either way the tick owns it from here.
            parked.append(f"{ref}:worker")
            continue
        if _last_marker(task, record.comment_baseline, WORKER_MARKERS):
            parked.append(f"{ref}:worker")
            continue
        try:
            record.handle = runtime.host.restart_worker(task, record)
        except Exception:  # noqa: BLE001 — one failed relaunch must not strand the others
            # Left with no handle and a fresh watchdog window: the card stays In progress under the
            # ordinary wait watchdog, which respawns it once and then escalates to Blocked.
            record.handle = ""
            skipped.append(f"{ref}:worker")
            continue
        record.state = "claimed"
        relaunched.append(f"{ref}:worker")

    return {"relaunched": relaunched, "parked": parked, "skipped": skipped}


def _refresh_watchdog_windows(records: dict[str, DispatcherRecord]) -> None:
    """Restart every open wait clock at resume.

    A freeze advances nothing, so the ceilings kept running against a pipeline that was stopped on
    purpose. Without this the first tick after a long freeze reads the pause as silence and starts
    respawning and blocking cards whose heads were fine.
    """
    now = time.time()
    for record in records.values():
        if record.worker_waiting_since:
            record.worker_waiting_since = now
        if record.review_waiting_since:
            record.review_waiting_since = now
        if record.gate_pending_since:
            record.gate_pending_since = now


def _load_task(runtime: Any, ref: str) -> dict[str, Any] | None:
    try:
        return runtime.reader.show(ref)
    except TaskError:
        return None
