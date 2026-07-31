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

  live pid    -> adopt it. The record becomes what the launch would have written, and no second
                 head is launched.
  no pid yet  -> leave the intent alone. A head that has just started has not written its
                 heartbeat, and that is not evidence of death, so it waits out the same grace
                 window every fresh head gets.
  dead pid    -> the launch left nothing running. Whatever it may have left in the workspace is
                 stopped, the intent is dropped, and the ordinary path relaunches — into the round
                 the intent reserved, since a rework's round is over whether or not its head lived.

The intent is held for the whole bring-up, not only up to the host call. Once the host answers, the
pane it opened and the configuration it launched go into the intent, and only when the record has
everything — pane identity, routing event, its own save — is the intent spent. Everything in that
tail runs over a process that already exists, so a failure there is ambiguous rather than a launch
that did not happen, and an adoption reads the launch's own snapshot instead of asking a registry
that may have been edited since.

State that cannot be written is a launch that does not happen: the caller answers a failed intent
write by not touching the host at all. A failing data plane then costs the card a tick instead of
giving it two heads. A bring-up that fails once its terminal is already up is not a headless card
either: the host reports it as `HeadLaunchAborted` with the pane it opened, the intent stays on
disk carrying that handle, and the same resolution settles it a tick later.

A head adopted this way usually has no pane handle, which is why `command_terminal_status` falls
back to the pid heartbeat when a record carries no pane identity for its role. Without that the
wait watchdog would read the adopted head as a missing terminal and respawn it: the double launch
this module exists to prevent, one tick later. For the same reason the heartbeat stays on the
record as that role's identity (`worker_pid_file` / `review_pid_file`): every stop the lifecycle
makes afterwards — the freeze before review, a respawn, a red verdict, a pipeline freeze — goes
through it when there is no pane to close, and one that quietly stopped nothing would put a second
process on the checkout just as surely.

Nothing here assumes a stop worked. A head the host will not confirm gone keeps its record and its
intent, and the tick that wanted to replace it returns `*-stop-unconfirmed` instead: an
unconfirmed stop followed by a launch is the same two heads by another route.
"""

from __future__ import annotations

import time
from typing import Any

from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_types import HeadLaunchAborted, HostError
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
    previous_workspace_settled = record.workspace_settled
    reserved = record.attempt_round if round_number is None else round_number
    record.workspace_settled = False
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
        record.workspace_settled = previous_workspace_settled
        return f"{type(exc).__name__}: {exc}"
    return None


def confirm_launch_intent(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    *,
    handle: str = "",
    run: dict[str, Any] | None = None,
) -> None:
    """Put what the finished host call knows about the head into its intent, on disk.

    The pane and the launch snapshot exist only once the host has answered, and everything the tick
    still owes that head afterwards — its pane leaf, its routing record, its own save — runs against
    a process that is already up and can refuse. Writing them into the intent here is what lets a
    recovery adopt that head with the configuration it actually launched on, instead of inventing
    one from a registry that may have been edited since.

    A refused write is not a problem: the pre-launch intent is already on disk and names the same
    head, so recovery still finds it and only its routing snapshot falls back to the registry.
    """
    intent = dict(launch_intent(record))
    if not intent:
        return
    if handle:
        intent["handle"] = handle
    if run:
        intent["run"] = dict(run)
    intent["launched"] = True
    record.launch_intent = intent
    records[ref] = record
    _persist_quietly(runtime, payload, records)


def launch_left_a_head(record: DispatcherRecord) -> bool:
    """Does this launch's heartbeat prove a head exists, whatever the failure claimed?

    A host reporting an ordinary failure is claiming that no head of this bring-up is running. The
    heartbeat is the one piece of evidence that can contradict it, and where it does, the intent has
    to survive: a record dropped over a live process is the second head this contour prevents.
    """
    intent = launch_intent(record)
    if not intent:
        return False
    status = head_process_status(str(intent.get("pid_file") or ""))
    return bool(status.get("known") and status.get("alive"))


def role_field(role: str, suffix: str) -> str:
    return f"{'review' if role == REVIEW_ROLE else 'worker'}_{suffix}"


def clear_launch_intent(record: DispatcherRecord) -> None:
    """Take back an intent whose host call has answered. The caller's own save persists it.

    The heartbeat the intent named stays on the record as that role's head identity. It is what
    every later stop falls back on when the pane handle is missing — an adopted head never had one
    — and without it a freeze, a respawn or a red-verdict bounce would quietly stop nothing and
    open a second process beside the one still running.
    """
    intent = launch_intent(record)
    role = str(intent.get("role") or "")
    pid_file = str(intent.get("pid_file") or "")
    if role and pid_file:
        setattr(record, role_field(role, "pid_file"), pid_file)
    record.launch_intent = {}


def forget_role_head(record: DispatcherRecord, role: str) -> None:
    """Drop every pointer to one role's head, once it is confirmed gone."""
    if role == REVIEW_ROLE:
        record.review_handle = ""
        record.review_leaf = ""
        record.review_pid_file = ""
        return
    record.handle = ""
    record.worker_leaf = ""
    record.worker_pid_file = ""
    # Nothing is left to resume, but a red transition already opened over that head still owes the
    # card its replacement and outlives the pointers.
    record.worker_continuation.drop_session()


def mark_launch_aborted(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    exc: HeadLaunchAborted,
) -> None:
    """Keep the intent of a bring-up that failed with a pane already open.

    The host could not promise that nothing of that head is running, so the intent survives with
    whatever identity the failure carried. The next tick reads the heartbeat and either adopts the
    head — pane included, so its lifecycle is whole — or stops what is left of it. A persist that
    refuses here is not a problem: the pre-launch intent is already on disk and names the same pid
    file, which is the identity recovery actually settles the question with.
    """
    intent = dict(launch_intent(record))
    if not intent:
        return
    if exc.handle:
        intent["handle"] = exc.handle
    intent["aborted"] = True
    record.launch_intent = intent
    records[ref] = record
    _persist_quietly(runtime, payload, records)


def launch_aborted(
    *, step: str, ref: str, attempt_id: str, role: str, reason: str
) -> dict[str, Any]:
    """The outcome of a bring-up that may have left a head running and could not confirm it."""
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{role}-launch-aborted",
        "reason": f"launch may have left a head running: {reason}",
    }


def head_stop_unconfirmed(
    *, step: str, ref: str, attempt_id: str, role: str, reason: str
) -> dict[str, Any]:
    """The outcome of a tick that refused to launch because a stop was not confirmed.

    A head the host would not promise is gone is a head that may still be editing the checkout, so
    nothing takes its place this tick. The card keeps its record and the next tick retries the stop.
    """
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{role}-stop-unconfirmed",
        "reason": f"the previous head could not be confirmed stopped: {reason}",
    }


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
        failure = stop_launch_intent(runtime, record, intent, role)
        if failure is None:
            keep_reserved_round(runtime, record, intent)
        _persist_quietly(runtime, payload, records)
        if failure is not None:
            return head_stop_unconfirmed(
                step=step,
                ref=ref,
                attempt_id=record.attempt_id,
                role=role,
                reason=failure,
            )
        return None
    return _adopt_launch_intent(runtime, task, records, payload, record, intent, role, step)


def keep_reserved_round(runtime: Any, record: DispatcherRecord, intent: dict[str, Any]) -> None:
    """Carry the round a dropped worker intent reserved onto the record it was written over.

    A rework reserves the next round before the host call, while the record still carries the round
    the red result closed. Dropping such an intent — the launch left nothing running, or a freeze
    stopped what it left — is not the same as giving the reservation back: the round is over either
    way, and the relaunch that follows belongs to the new one. Without this the respawn runs the
    rework inside the round that rejected it, and its routing and its verdict land there too.

    Only for an intent that opens a round. A claim or a respawn continues the round the record
    already names, and its state is the ordinary path's to decide.
    """
    if str(intent.get("role") or "") == REVIEW_ROLE:
        return
    reserved = int(intent.get("round") or 0)
    if not intent.get("opens_round") or not reserved or record.attempt_round == reserved:
        return
    runtime.open_worker_round(record, round_number=reserved)
    # The state the rework bring-up would have written. The head is not up, so the wait watchdog
    # owns the relaunch from here, and it does it inside the round opened above.
    record.state = "claimed"


def stop_launch_intent(
    runtime: Any, record: DispatcherRecord, intent: dict[str, Any], role: str
) -> str | None:
    """End whatever a launch left behind and take its intent back. Returns the failure, or None.

    Called both for a launch whose head is not there — the pane can outlive the process, and the
    relaunch reuses this workspace — and wherever a card leaves the cycle with an intent still on
    it. Either way the intent survives an unconfirmed stop: a head the host will not promise is
    gone must keep a pointer, or a later requeue starts a second process in the same checkout.

    The identity is whatever the launch got as far as recording: the pane the failure handed back,
    the heartbeat the head wrote for itself, and failing both the workspace itself. That last case
    is a head nothing can name individually — a raw `SECRETARY_DISPATCHER_*_COMMAND` override runs
    without the heartbeat wrapper — and stopping the workspace is all that can still reach it.
    """
    _remember_launch_identity(record, intent, role)
    handle = getattr(record, "review_handle" if role == REVIEW_ROLE else "handle", "")
    pid_file = getattr(record, role_field(role, "pid_file"), "")
    # The path is known before the head exists, so it says nothing on its own; a heartbeat that can
    # be read is what proves this launch left something a role-scoped stop can address.
    named = bool(handle) or bool(head_process_status(pid_file).get("known"))
    try:
        if role == REVIEW_ROLE and named:
            # Imported here rather than at module scope: `dispatcher_review` writes the reviewer's
            # intent through this module, and a top-level import either way would be a cycle.
            from secretary.dispatcher_review import end_review_pane

            end_review_pane(runtime.host, record)
        else:
            # Everything this card runs in that workspace. For the worker that is the ordinary
            # answer; for a reviewer nothing can name, it is the last one, and it costs only the
            # worker head that a reviewer bring-up shuts down anyway.
            runtime.host.stop_workspace(record)
            forget_role_head(record, WORKER_ROLE)
            forget_role_head(record, REVIEW_ROLE)
    except HostError as exc:
        return f"{type(exc).__name__}: {exc}"
    record.launch_intent = {}
    return None


def _remember_launch_identity(
    record: DispatcherRecord, intent: dict[str, Any], role: str
) -> None:
    """Put the launch's own identity on the record, so the stop paths can reach its head."""
    pid_file = str(intent.get("pid_file") or "")
    handle = str(intent.get("handle") or "")
    # A first claim writes its intent before the record knows where the head runs, so the
    # workspace comes back off the intent here: without it the stop has no worktree to address.
    record.workspace = record.workspace or str(intent.get("workspace") or "")
    if pid_file:
        setattr(record, role_field(role, "pid_file"), pid_file)
    if not handle:
        return
    if role == REVIEW_ROLE:
        record.review_handle = record.review_handle or handle
    else:
        record.handle = record.handle or handle


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
    handle = str(intent.get("handle") or "")
    if role == REVIEW_ROLE:
        # A reviewer bring-up shuts the worker head down before it hands the pane back, and an
        # adopted one has to do the same: a worker still editing the checkout would leave the
        # verdict describing a tree that no longer exists. It goes through the same confirmed stop,
        # so a worker that will not die keeps the intent instead of being assumed gone.
        record.review_handle = handle
        record.review_pid_file = str(intent.get("pid_file") or "")
        try:
            runtime.host.freeze_worker(record)
        except HostError as exc:
            _persist_quietly(runtime, payload, records)
            return head_stop_unconfirmed(
                step=step,
                ref=ref,
                attempt_id=record.attempt_id,
                role=WORKER_ROLE,
                reason=f"{type(exc).__name__}: {exc}",
            )
        forget_role_head(record, WORKER_ROLE)
        record.state = "reviewing"
        record.review_leaf = ""
        record.review_started_at = record.review_progress_at = launched_at
        if not record.review_commit:
            # The worker is down and the reviewer writes no commits, so the checkout still sits
            # where the launch pinned it. The merge gate needs that sha to accept the verdict.
            record.review_commit = runtime.host.head_commit(record)
        deferred = _record_adopted_routing(
            runtime, task, records, payload, record, intent, role, step
        )
        if deferred is not None:
            return deferred
        clear_launch_intent(record)
    else:
        record.state = "claimed"
        reserved = int(intent.get("round") or 0)
        if intent.get("opens_round") and reserved:
            # The launch was a rework: it reserved the next round before calling the host, and the
            # adopted head belongs to that round. Opening it here is what keeps the rework's
            # routing and its verdict apart from the round the red result closed.
            runtime.open_worker_round(record, round_number=reserved)
        # Whatever pane identity the launch got as far as reporting. Usually none: the tick died
        # before the host answered. The heartbeat `clear_launch_intent` keeps is what the freeze,
        # the respawn and the red-verdict bounce then stop this head by.
        record.handle = handle
        record.worker_leaf = ""
        record.worker_started_at = record.worker_progress_at = launched_at
        deferred = _record_adopted_routing(
            runtime, task, records, payload, record, intent, role, step
        )
        if deferred is not None:
            return deferred
        clear_launch_intent(record)
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


def _record_adopted_routing(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    record: DispatcherRecord,
    intent: dict[str, Any],
    role: str,
    step: str,
) -> dict[str, Any] | None:
    """Give the adopted head its routing record. Returns the tick's outcome when that write fails.

    The head an interrupted tick launched is a head that ran, so the round owes it the same routing
    event every other bring-up writes: without it the verdict names only the other role, and the
    history reads as a round nobody worked. The snapshot is the launch's own where the intent got as
    far as carrying it, and the registry as it stands now otherwise.

    A journal that refuses keeps the intent: the head is adopted again next tick and the routing is
    retried then, which is one head with late telemetry instead of a head with none.
    """
    ref = task["ref"]
    run = intent.get("run") if isinstance(intent.get("run"), dict) else None
    try:
        if role == REVIEW_ROLE:
            runtime.record_review_routing(task, record, run)
        else:
            runtime.record_worker_routing(task, record, run)
    except Exception as exc:  # noqa: BLE001 — any journal refusal, whatever the plane called it
        records[ref] = record
        _persist_quietly(runtime, payload, records)
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": f"{role}-launch-adopt-deferred",
            "head": str(intent.get("head") or ""),
            "reason": f"the adopted head could not be recorded in the routing journal: {exc}",
        }
    return None


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
