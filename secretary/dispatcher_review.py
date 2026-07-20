"""Review launch recovery helpers for dispatcher runtimes."""

from __future__ import annotations

from typing import Any

from secretary.dispatcher_helpers import scrub_host_output
from secretary.dispatcher_state import DispatcherRecord, attempt_request_id as _attempt_request_id
from secretary.dispatcher_types import HostError, legacy_review_pane_label, review_pane_label
from secretary.dispatcher_watchdog import wait_cycle_token as _wait_cycle_token


def command_review_running(host: Any, task: dict[str, Any], record: DispatcherRecord) -> bool:
    """Is the reviewer pane for this card still up?

    Identity comes from the persisted pane first: the handle the split returned, and the leafId it
    resolved to (`terminal list` can answer with a different handle alias for the same pty, so the
    leaf is the token that survives that). The label is only a fallback for a pane whose handle was
    never persisted — a tick killed between the split and the state write, or a card adopted from a
    dispatcher that predates persisted reviewer handles. It cannot be the primary check: the
    reviewer head overwrites its terminal title with its own OSC sequence seconds after launch."""
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
    labels = {review_pane_label(task["ref"]), legacy_review_pane_label(task["ref"])}
    live = [
        terminal
        for terminal in terminals
        if isinstance(terminal, dict) and terminal.get("connected") is not False
    ]
    if record.review_handle or record.review_leaf:
        return any(
            (record.review_handle and terminal.get("handle") == record.review_handle)
            or (record.review_leaf and terminal.get("leafId") == record.review_leaf)
            for terminal in live
        )
    return any(terminal.get("title") in labels for terminal in live)


def end_review_pane(host: Any, record: DispatcherRecord) -> None:
    """Close the reviewer's pane and forget it. Used wherever the reviewer's lifecycle ends on its
    own — a red verdict, a respawn after a silent reviewer — so the next bring-up cannot mistake a
    stale handle for a live pane, and so the worker's workspace survives untouched."""
    host.stop_review(record)
    record.review_handle = ""
    record.review_leaf = ""
    record.review_commit = ""


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
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                "review-inventory-blocked",
                ref,
                _wait_cycle_token(record),
            ),
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
        launch = runtime.host.start_review(task, record)
    except Exception as exc:
        runtime.writer.move(
            role="dispatcher",
            actor=runtime.owner,
            reference=ref,
            target="blocked",
            reason=f"review bring-up failed: {scrub_host_output(str(exc))}",
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id, "review-blocked", ref, _wait_cycle_token(record)
            ),
        )
        records.pop(ref, None)
        return {"status": "blocked", "step": "review", "pilot_ref": ref, "reason": "host review failed"}
    record.review_handle = launch.handle
    record.review_leaf = launch.leaf
    record.review_commit = launch.commit
    # The worker head is gone: its pane was shut down so the reviewer judges a checkout nothing is
    # still editing. A red verdict launches a fresh worker into the same workspace.
    record.handle = ""
    record.state = "reviewing"
    return {
        "status": "ok",
        "step": "review",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": action,
    }
