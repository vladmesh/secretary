"""Durable launch intent for the dispatcher-launched worker and reviewer heads.

Same contour as the observer's (`dispatcher_observer`) and for the same reason. A head is a real
process from the moment the host is asked for one, but `DispatcherRecord` only learns its workspace,
its pane and its routing from the `save_records` at the end of the launch path. A state write that
refuses in that window leaves a running head with nothing pointing at it, and the next tick reads
the card as headless and opens a second one.

So the launch is fixed on disk first. The intent names the role, the round and attempt it belongs
to, the workspace the head runs in and the pid file it writes its heartbeat to. Workspace and pid
file are path arithmetic over the card reference and the worker id, so both are known before the
host answers, which is exactly what a tick that dies mid-launch never gets to see. With them the
next tick can settle the only question that matters, "is the head of that launch alive", without
the terminal handle the lost tick never persisted. The round is the round the new head belongs to,
which for a rework is the next one: it is reserved here, before the host call, so an adoption
resumes the rework rather than the round the red result closed.

Resolution runs before anything else the tick would do with the card:

  live pid    -> adopt it. The record becomes what the launch would have written minus the pane
                 handle, and no second head is launched.
  no pid yet  -> leave the intent alone. A head that has just started has not written its
                 heartbeat, and that is not evidence of death, so it waits out the same grace
                 window every fresh head gets.
  dead pid    -> the launch left nothing running. The intent is dropped, whatever it may have left
                 in the workspace is closed, and the ordinary path relaunches.

State that cannot be written is a launch that does not happen: the caller answers a failed intent
write by not touching the host at all. A failing data plane then costs the card a tick instead of
giving it two heads.

A head adopted this way has no pane handle, which is why `command_terminal_status` falls back to
the pid heartbeat when a record carries no pane identity for its role. Without that the wait
watchdog would read the adopted head as a missing terminal and respawn it: the double launch this
module exists to prevent, one tick later.
"""

from __future__ import annotations

import time
from typing import Any

from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_watchdog import (
    head_process_status,
    initial_output_stall_seconds,
    pid_file_path,
)

WORKER_ROLE = "worker"
REVIEW_ROLE = "review"

# What "the data plane refused this write" looks like. Deliberately not bare `Exception`: a launch
# that cannot be recorded is answered by not launching, and anything that is not a storage failure
# (a bug in the record, the health probe's own abort signal) has to keep travelling instead of
# being reported to the operator as a full disk.
STORAGE_ERRORS = (OSError, ValueError, TypeError, UnicodeError)


def launch_pid_file(role: str, reference: str) -> str:
    """The heartbeat file the head of this role writes. Known before the head exists."""
    return pid_file_path(role, reference)


def launch_intent(record: DispatcherRecord) -> dict[str, Any]:
    intent = getattr(record, "launch_intent", None)
    return intent if isinstance(intent, dict) and intent.get("role") else {}


def write_launch_intent(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    *,
    role: str,
    action: str,
    head: str,
    workspace: str,
    round_number: int | None = None,
) -> str | None:
    """Fix one bring-up on disk before the host is called. Returns the failure, or None.

    The record is left exactly as it was when the write fails, so a caller that answers with "no
    host call this tick" leaves nothing behind for the next one to misread.

    `round_number` is the round the head being launched will belong to. A rework opens a new one,
    and it has to be reserved here rather than after the host call: the round the intent carries is
    the round an adoption resumes, and a rework recovered on the round its red verdict ended would
    merge two rounds and their routing into one.
    """
    previous = dict(getattr(record, "launch_intent", None) or {})
    reserved = record.attempt_round if round_number is None else round_number
    record.launch_intent = {
        "role": role,
        "action": action,
        "head": head,
        "workspace": workspace,
        "pid_file": launch_pid_file(role, ref),
        "attempt_id": record.attempt_id,
        "round": reserved,
        "opens_round": bool(reserved) and reserved != record.attempt_round,
        "respawns": int(getattr(record, f"{role}_respawns", 0) or 0),
        "at": time.time(),
    }
    records[ref] = record
    try:
        runtime.save_records(payload, records)
    except STORAGE_ERRORS as exc:
        record.launch_intent = previous
        return f"{type(exc).__name__}: {exc}"
    return None


def clear_launch_intent(record: DispatcherRecord) -> None:
    """Take back an intent whose host call has answered. The caller's own save persists it."""
    record.launch_intent = {}


def launch_intent_unwritable(
    *, step: str, ref: str, attempt_id: str, role: str, reason: str
) -> dict[str, Any]:
    """The outcome of a tick that refused to launch because it could not record the launch."""
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{role}-launch-intent-unwritable",
        "reason": f"launch intent could not be persisted: {reason}",
    }


def launch_intent_liveness(intent: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Whether the head this intent was written for is running, and on what evidence.

    `pid_known: False` means the heartbeat is not readable yet. A head launched seconds ago has not
    written one, so that reads as alive until the grace window the watchdog already uses for a pane
    that has produced no output at all has passed.
    """
    now = time.time() if now is None else now
    status = head_process_status(str(intent.get("pid_file") or ""))
    if status.get("known"):
        return {"alive": bool(status.get("alive")), "pid_known": True}
    started_at = float(intent.get("at") or 0.0)
    if started_at and now - started_at <= initial_output_stall_seconds():
        return {"alive": True, "pid_known": False}
    return {"alive": False, "pid_known": False}


def resolve_launch_intent(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Settle an unresolved bring-up before the tick acts on this card.

    Returns the tick's outcome when the intent takes the tick (adopted, or still coming up), and
    None when there is nothing pending or the launch left nothing running, in which case the
    ordinary path relaunches from a record that no longer claims a head.
    """
    ref = task["ref"]
    record = records.get(ref)
    if record is None:
        return None
    intent = launch_intent(record)
    if not intent:
        return None
    role = str(intent.get("role"))
    step = "review" if role == REVIEW_ROLE else "advance"
    liveness = launch_intent_liveness(intent)
    if liveness["alive"] and not liveness["pid_known"]:
        # Left exactly as it is: the next tick asks again, once the grace window has either
        # produced a heartbeat or run out. Killing a head that is still starting would cost the
        # card its session for no evidence at all.
        return {
            "status": "skipped",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": f"{role}-launch-pending",
            "head": str(intent.get("head") or ""),
            "reason": "a launch intent is still within its grace window and has written no pid yet",
        }
    if not liveness["alive"]:
        _drop_dead_intent(runtime, record, role)
        _persist_quietly(runtime, payload, records)
        return None
    return _adopt_launch_intent(runtime, task, records, payload, record, intent, role, step)


def _drop_dead_intent(runtime: Any, record: DispatcherRecord, role: str) -> None:
    """Close the books on a launch whose head is not there, terminal leftovers included."""
    clear_launch_intent(record)
    if role == REVIEW_ROLE:
        # Imported here rather than at module scope: `dispatcher_review` writes the reviewer's
        # intent through this module, and a top-level import either way would be a cycle.
        from secretary.dispatcher_review import end_review_pane

        end_review_pane(runtime.host, record)
        return
    # The worker's pane can outlive its process, and the relaunch reuses this workspace, so the
    # leftover goes before the replacement opens.
    runtime.host.stop(record)
    record.handle = ""
    record.worker_leaf = ""


def _adopt_launch_intent(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    record: DispatcherRecord,
    intent: dict[str, Any],
    role: str,
    step: str,
) -> dict[str, Any]:
    """Take the head of a launch whose tick did not survive as this card's head for that role."""
    ref = task["ref"]
    launched_at = float(intent.get("at") or 0.0) or time.time()
    record.workspace = record.workspace or str(intent.get("workspace") or "")
    clear_launch_intent(record)
    if role == REVIEW_ROLE:
        record.state = "reviewing"
        # A reviewer bring-up shuts the worker head down before it hands the pane back, so an
        # adopted reviewer says the same: one head on this checkout, not two.
        record.handle = ""
        record.worker_leaf = ""
        record.review_handle = ""
        record.review_leaf = ""
        record.review_started_at = record.review_progress_at = launched_at
        if not record.review_commit:
            # The worker is down and the reviewer writes no commits, so the checkout still sits
            # where the launch pinned it. The merge gate needs that sha to accept the verdict.
            record.review_commit = runtime.host.head_commit(record)
    else:
        record.state = "claimed"
        reserved = int(intent.get("round") or 0)
        if intent.get("opens_round") and reserved:
            # The launch was a rework: it reserved the next round before calling the host, and the
            # adopted head belongs to that round. Opening it here is what keeps the rework's
            # routing and its verdict apart from the round the red result closed.
            runtime.open_worker_round(record, round_number=reserved)
        record.handle = ""
        record.worker_leaf = ""
        record.worker_started_at = record.worker_progress_at = launched_at
    records[ref] = record
    _persist_quietly(runtime, payload, records)
    return {
        "status": "ok",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": f"{role}-launch-adopted",
        "head": str(intent.get("head") or ""),
        "reason": "a launch intent was left by a tick that did not finish; its head is alive",
    }


def _persist_quietly(
    runtime: Any, payload: dict[str, Any], records: dict[str, DispatcherRecord]
) -> bool:
    """Flush the records mid-tick. False means this tick's own save is what carries them.

    Never raised at the caller: an adoption that is not persisted is repeated by the next tick
    from the same intent, which is the same answer rather than a second head.
    """
    try:
        runtime.save_records(payload, records)
    except STORAGE_ERRORS:
        return False
    return True
