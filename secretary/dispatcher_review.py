"""Review launch recovery helpers for dispatcher runtimes."""

from __future__ import annotations

import time
from typing import Any

from secretary.dispatcher_helpers import scrub_host_output
from secretary.dispatcher_launch import (
    REVIEW_ROLE,
    WORKER_ROLE,
    clear_launch_intent,
    confirm_launch_intent,
    forget_role_head,
    launch_aborted,
    launch_intent_unwritable,
    launch_left_a_head,
    mark_launch_aborted,
    write_launch_intent,
)
from secretary.dispatcher_state import DispatcherRecord, attempt_request_id as _attempt_request_id
from secretary.dispatcher_tui import READINESS_READY, terminal_readiness
from secretary.dispatcher_types import (
    HeadLaunchAborted,
    HostError,
    legacy_review_pane_label,
    review_pane_label,
)
from secretary.dispatcher_watchdog import (
    head_process_status as _head_process_status,
    pid_file_path as _pid_file_path,
    wait_cycle_token as _wait_cycle_token,
)


def command_terminal_status(
    host: Any, task: dict[str, Any], record: DispatcherRecord, *, kind: str
) -> dict[str, Any]:
    """Return the tracked pane's liveness and its last output time.

    A failed inventory raises instead of looking like a missing pane.  The wait watchdog can then
    report a degraded runtime without restarting a head on a transport failure.
    """
    if host.mode == "noop":
        return {"known": True, "live": True, "reason": "noop"}
    if not record.workspace:
        raise HostError(f"{kind} workspace is unavailable")
    data = host._run_json([
        "orca", "terminal", "list", "--worktree", f"path:{record.workspace}", "--json",
    ])
    if data.get("ok") is False:
        raise HostError("orca terminal list failed")
    payload = data.get("result") if isinstance(data.get("result"), dict) else data
    terminals = payload.get("terminals") if isinstance(payload, dict) else []
    if not isinstance(terminals, list):
        raise HostError("orca terminal list returned an unsupported shape")
    if kind == "review":
        labels = {review_pane_label(task["ref"]), legacy_review_pane_label(task["ref"])}
        pane_known = bool(record.review_handle or record.review_leaf)
        matches = lambda terminal: bool(
            (record.review_handle and terminal.get("handle") == record.review_handle)
            or (record.review_leaf and terminal.get("leafId") == record.review_leaf)
            or (not pane_known and terminal.get("title") in labels)
        )
    else:
        pane_known = bool(record.handle or record.worker_leaf)
        matches = lambda terminal: bool(
            (record.handle and terminal.get("handle") == record.handle)
            or (record.worker_leaf and terminal.get("leafId") == record.worker_leaf)
        )
    for terminal in terminals:
        if not isinstance(terminal, dict) or not matches(terminal):
            continue
        if terminal.get("connected") is False:
            return {"known": True, "live": False, "reason": "disconnected"}
        pid_status = _head_process_status(_pid_file_path(kind, task["ref"]))
        if pid_status.get("known") and not pid_status.get("alive"):
            # The pane is connected and Orca kept its wrapping shell open, but the head process
            # itself is gone (secretary-751): a provider crash or a killed runtime, not silence.
            return {"known": True, "live": False, "reason": "process-exited"}
        last = terminal.get("lastOutputAt")
        try:
            activity = float(last) / 1000.0 if last else None
        except (TypeError, ValueError):
            activity = None
        supplemental = getattr(host, "codex_tui_activity", lambda _task, _record, _kind: None)(
            task, record, kind
        )
        if supplemental:
            activity = max(activity or 0.0, float(supplemental))
        pid_confirmed = bool(pid_status.get("known") and pid_status.get("alive"))
        status = {
            "known": True, "live": True, "reason": "live", "last_activity": activity,
            # A pid-heartbeat that proves this exact process still runs; only this — not a
            # silent pane — should let a wait watchdog trust liveness past the timing ceilings.
            "pid_confirmed": pid_confirmed,
        }
        if pid_confirmed:
            # Whether the head is working or waiting at its prompt. Only asked of a process the
            # heartbeat proves is running, because that is the one case where no timing ceiling
            # applies and silence has to be told apart from a finished turn (secretary-1063).
            status["idle"] = _pane_is_idle(host, str(terminal.get("handle") or ""))
        return status
    if not pane_known:
        # A head adopted from a launch intent (secretary-820): its bring-up outlived the tick that
        # started it, so no pane identity was ever persisted for it. The pid heartbeat proves the
        # process is running, and respawning it for want of a handle would be exactly the second
        # head the intent contour exists to prevent.
        pid_status = _head_process_status(_pid_file_path(kind, task["ref"]))
        if pid_status.get("known") and pid_status.get("alive"):
            return {"known": True, "live": True, "reason": "pid", "pid_confirmed": True}
    return {"known": True, "live": False, "reason": "missing-terminal"}


def _pane_is_idle(host: Any, handle: str) -> bool:
    """Is this pane waiting for input rather than working on a turn?

    Orca's `tui-idle`, the same readiness the delivery path waits on before it sends to any head
    and the same one the observer's lifecycle reads. It comes from the pane's own agent status,
    falling back to a quiescence window, so it answers for the claude and the codex adapter alike
    and reads no screen. A probe that cannot be answered is not an idle head: only a definite
    `ready` counts, so a runtime that cannot say leaves the wait exactly as it was.
    """
    if not handle:
        return False
    return terminal_readiness(handle, run_json=host._run_json) == READINESS_READY


def command_review_running(host: Any, task: dict[str, Any], record: DispatcherRecord) -> bool:
    """Is the reviewer pane for this card still up?

    Identity comes from the persisted pane first: the handle the split returned, and the leafId it
    resolved to (`terminal list` can answer with a different handle alias for the same pty, so the
    leaf is the token that survives that). The label is only a fallback for a pane whose handle was
    never persisted — a tick killed between the split and the state write, or a card adopted from a
    dispatcher that predates persisted reviewer handles. It cannot be the primary check: the
    reviewer head overwrites its terminal title with its own OSC sequence seconds after launch."""
    return bool(command_terminal_status(host, task, record, kind="review").get("live"))


def end_review_pane(host: Any, record: DispatcherRecord) -> None:
    """Close the reviewer's pane and forget it. Used wherever the reviewer's lifecycle ends on its
    own — a red verdict, a respawn after a silent reviewer — so the next bring-up cannot mistake a
    stale handle for a live pane, and so the worker's workspace survives untouched.

    A stop the host will not confirm raises, and the record keeps pointing at that reviewer. Every
    caller of this opens something in the same checkout right after — a rework worker, a
    replacement reviewer — and a forgotten head that is still running would then be the second
    process on it."""
    host.stop_review(record)
    record.review_handle = ""
    record.review_leaf = ""
    record.review_pid_file = ""
    record.review_commit = ""


def recover_review_launch(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    payload: dict[str, Any],
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
    return start_review(
        runtime, task, records, record, attempt_id, action="review-restarted", payload=payload
    )


def start_review(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    ref = task["ref"]
    readiness = runtime.head_readiness(record.review_head)
    if not readiness.launch_allowed:
        record.state = "review_starting"
        return {
            "status": "skipped",
            "step": "head-preflight",
            "action": "review-resource-not-ready",
            "pilot_ref": ref,
            "head": record.review_head,
            "readiness": readiness.to_json(),
            "reason": readiness.reason,
        }
    # The reviewer's own durable launch intent (secretary-820). It goes to disk before the pane is
    # split, because the record does not learn the reviewer's handle until this function returns
    # and the caller saves: a tick that dies in between would otherwise leave a live reviewer that
    # the next tick cannot see, and would launch a second one beside.
    failure = write_launch_intent(
        runtime,
        payload,
        records,
        ref,
        record,
        role=REVIEW_ROLE,
        action=action,
        head=record.review_head,
        workspace=record.workspace,
    )
    if failure is not None:
        record.state = "review_starting"
        return launch_intent_unwritable(
            step="review",
            ref=ref,
            attempt_id=record.attempt_id or attempt_id,
            role=REVIEW_ROLE,
            reason=failure,
        )
    try:
        launch = runtime.host.start_review(task, record)
    except HeadLaunchAborted as exc:
        # The bring-up failed with the reviewer's pane already open, so "no reviewer exists" is
        # exactly what cannot be claimed here. The intent stays on disk with what the failure knew
        # of that head, and the next tick adopts it or stops it. Blocking the card and dropping the
        # record instead would leave a live reviewer with nothing pointing at it.
        mark_launch_aborted(runtime, payload, records, ref, record, exc)
        record.state = "review_starting"
        return launch_aborted(
            step="review",
            ref=ref,
            attempt_id=record.attempt_id or attempt_id,
            role=REVIEW_ROLE,
            reason=scrub_host_output(str(exc)),
        )
    except Exception as exc:
        if launch_left_a_head(record):
            # The host reported an ordinary failure, and the reviewer's own heartbeat says a process
            # of this bring-up is running anyway. The heartbeat wins: the intent stays, and the next
            # tick adopts that reviewer or stops it rather than the card being blocked over it.
            mark_launch_aborted(
                runtime,
                payload,
                records,
                ref,
                record,
                HeadLaunchAborted(str(exc), workspace=record.workspace),
            )
            record.state = "review_starting"
            return launch_aborted(
                step="review",
                ref=ref,
                attempt_id=record.attempt_id or attempt_id,
                role=REVIEW_ROLE,
                reason=scrub_host_output(str(exc)),
            )
        clear_launch_intent(record)
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
    # The reviewer is up: its pane and its launch configuration go into the intent on disk before
    # the record is told anything about it, so a tick that dies from here on is adopted with the
    # routing history of the head that actually ran.
    confirm_launch_intent(
        runtime, payload, records, ref, record, handle=launch.handle, run=launch.run
    )
    record.review_handle = launch.handle
    record.review_leaf = launch.leaf
    record.review_commit = launch.commit
    # The verdict this pane issues belongs to this head, so the round records it now, from the
    # launcher's own snapshot (secretary-716). The intent is spent only once that has landed: a
    # journal that refuses here leaves the reviewer adoptable, and the adoption writes the routing
    # event the round would otherwise never get for it.
    runtime.record_review_routing(task, record, launch.run)
    clear_launch_intent(record)
    record.review_started_at = record.review_progress_at = time.time()
    # A retained worker is suspended, not gone: it keeps its pane and its heartbeat so a red
    # verdict can continue that same conversation, and the reviewer still judges a checkout
    # nothing is editing. Without retention the worker head was shut down for the reviewer, and
    # the record must stop naming a pane that no longer exists.
    if not record.worker_continuation.retained:
        forget_role_head(record, WORKER_ROLE)
    record.state = "reviewing"
    return {
        "status": "ok",
        "step": "review",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": action,
    }
