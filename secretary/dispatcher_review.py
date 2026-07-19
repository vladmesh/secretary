"""Review launch recovery helpers for dispatcher runtimes."""

from __future__ import annotations

from typing import Any

from secretary.dispatcher_helpers import scrub_host_output
from secretary.dispatcher_state import DispatcherRecord, attempt_request_id as _attempt_request_id
from secretary.dispatcher_types import HostError


def command_review_running(host: Any, task: dict[str, Any], record: DispatcherRecord) -> bool:
    return _terminal_running(host, record, {f"{task['ref']} review"})


def command_worker_running(host: Any, task: dict[str, Any], record: DispatcherRecord) -> bool:
    """Worker liveness for the report watchdog. Both launch titles count: a first run is
    "<ref> worker", a rework run is "<ref> worker rework"."""
    ref = task["ref"]
    return _terminal_running(host, record, {f"{ref} worker", f"{ref} worker rework"})


def _terminal_running(host: Any, record: DispatcherRecord, titles: set[str]) -> bool:
    if host.mode == "noop":
        return False
    if not record.workspace:
        raise HostError("review workspace is unavailable")
    data = host._run_json([
        "orca",
        "terminal",
        "list",
        "--worktree",
        f"path:{record.workspace}",
        "--json",
    ])
    if data.get("ok") is False:
        raise HostError("orca terminal list failed")
    payload = data.get("result") if isinstance(data.get("result"), dict) else data
    terminals = payload.get("terminals") if isinstance(payload, dict) else []
    if not isinstance(terminals, list):
        raise HostError("orca terminal list returned an unsupported shape")
    return any(
        isinstance(terminal, dict)
        and terminal.get("connected") is not False
        and terminal.get("title") in titles
        for terminal in terminals
    )


def recover_review_launch(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    record: DispatcherRecord,
    attempt_id: str,
) -> dict[str, Any]:
    ref = task["ref"]
    try:
        running = runtime.host.review_running(task, record)
    except Exception as exc:
        runtime.writer.move(
            role="dispatcher",
            actor=runtime.owner,
            reference=ref,
            target="blocked",
            reason=f"review inventory failed: {scrub_host_output(str(exc))}",
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "review-inventory-blocked", ref),
        )
        records.pop(ref, None)
        return {"status": "blocked", "step": "review", "pilot_ref": ref, "reason": "review inventory failed"}
    if running:
        record.state = "reviewing"
        return {
            "status": "ok",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "waiting-review-verdict",
        }
    return start_review(runtime, task, records, record, attempt_id, action="review-restarted")


def start_review(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    action: str,
) -> dict[str, Any]:
    ref = task["ref"]
    try:
        record.handle = runtime.host.start_review(task, record)
    except Exception as exc:
        runtime.writer.move(
            role="dispatcher",
            actor=runtime.owner,
            reference=ref,
            target="blocked",
            reason=f"review bring-up failed: {scrub_host_output(str(exc))}",
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "review-blocked", ref),
        )
        records.pop(ref, None)
        return {"status": "blocked", "step": "review", "pilot_ref": ref, "reason": "host review failed"}
    record.state = "reviewing"
    return {
        "status": "ok",
        "step": "review",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": action,
    }
