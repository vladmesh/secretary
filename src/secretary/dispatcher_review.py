"""Review launch recovery helpers for dispatcher runtimes."""

from __future__ import annotations

import time
from typing import Any

from secretary.dispatcher_helpers import scrub_host_output
from secretary.dispatcher_launch import (
    REVIEW_ROLE,
    STAGE_REVIEW,
    WORKER_ROLE,
    BringUpFailure,
    bring_up_blocked_action,
    bring_up_blocked_reason,
    bring_up_terminal_reason,
    LAUNCH_DELIVERY_MAX_ATTEMPTS,
    busy_launch_delivery,
    classify_bring_up_failure,
    clear_launch_intent,
    confirm_launch_intent,
    defer_busy_launch_delivery,
    defer_launch_delivery,
    forget_role_head,
    launch_aborted,
    launch_deferred,
    launch_delivery_receipt,
    launch_intent_unwritable,
    launch_left_a_head,
    mark_launch_aborted,
    pane_state_label,
    reset_launch_attempts,
    undelivered_launch_delivery,
    write_launch_intent,
)
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_state import attempt_request_id as _attempt_request_id
from secretary.dispatcher_tui import (
    DELIVERY_RECEIPT_REFUSED,
    READINESS_BLOCKED,
    READINESS_BUSY,
    READINESS_READY,
    delivery_readiness_state,
    terminal_readiness,
)
from secretary.dispatcher_types import (
    STOPPED_BY_DISPATCHER,
    HeadLaunchAborted,
    HostError,
    review_pane_label,
)
from secretary.dispatcher_watchdog import (
    head_run_process_status as _head_run_process_status,
)
from secretary.dispatcher_watchdog import (
    heartbeat_is_dead as _heartbeat_is_dead,
)
from secretary.dispatcher_watchdog import (
    heartbeat_is_live_match as _heartbeat_is_live_match,
)
from secretary.dispatcher_watchdog import (
    heartbeat_is_mismatch as _heartbeat_is_mismatch,
)
from secretary.dispatcher_watchdog import (
    initial_output_stall_seconds as _initial_output_stall_seconds,
)
from secretary.dispatcher_watchdog import (
    pid_file_path as _pid_file_path,
)
from secretary.dispatcher_watchdog import (
    review_infra_retry_attempts as _review_infra_retry_attempts,
)
from secretary.dispatcher_watchdog import (
    review_launch_abort_stuck_ticks as _review_launch_abort_stuck_ticks,
)
from secretary.dispatcher_watchdog import (
    wait_cycle_token as _wait_cycle_token,
)
from secretary.dispatcher_worker_lifecycle import head_run_binding
from triggered_agents.runtime.pane_host import (
    OrcaSessionHost,
    PaneHostError,
    WorkspaceInventory,
)


def candidate_sha(record: DispatcherRecord) -> str:
    """The checkout the green gate attested, as the receipt itself recorded it."""
    attestation = record.gate_attestation if isinstance(record.gate_attestation, dict) else {}
    return str(attestation.get("validated_sha") or "")


def review_infrastructure_retry(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    payload: dict[str, Any],
    reason: str,
) -> dict[str, Any] | None:
    """Hold a green candidate over a reviewer that could not be started. None past the ceiling.

    A reviewer pane that will not split says nothing about the code, and the candidate behind it is
    still the one a green exact-SHA gate accepted. Nothing here decides anything about the candidate:
    the record stays exactly as the green gate left it and the state goes back to `review_starting`,
    which is the one state whose recovery launches a reviewer and only a reviewer. Only the count
    moves, and past the ceiling the caller blocks the card for an operator.
    """
    ref = task["ref"]
    if record.gate_state != "green":
        # No green candidate to preserve. Whatever failed here belongs to the ordinary failure
        # path, which is where the caller goes when this answers None.
        return None
    attempts = record.review_infra_failures + 1
    limit = _review_infra_retry_attempts()
    record.review_infra_failures = attempts
    record.review_infra_error = reason
    record.state = "review_starting"
    records[ref] = record
    runtime.save_records(payload, records)
    if attempts >= limit:
        return None
    sha = candidate_sha(record)
    return {
        "status": "degraded",
        "step": "review",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id or attempt_id,
        "action": "review-infrastructure-retry",
        "attempts": attempts,
        "candidate_sha": sha,
        "report_generation": record.report_generation,
        "reason": (
            f"the reviewer could not be started over the green candidate "
            f"{sha[:12] or '(sha unavailable)'}; this is a review-stage infrastructure failure, "
            f"not a verdict, so the gate receipt, the worker report and the held worker session "
            f"stay and retry {attempts} of {limit} launches the reviewer again: {reason}"
        ),
    }


def review_infrastructure_blocked_reason(
    record: DispatcherRecord, reason: str, failure: BringUpFailure
) -> str:
    """What the operator reads when a reviewer bring-up will not be retried.

    The green candidate's own sentence is the reviewer-specific half and stays: the card was never
    reworked and its receipt still stands, so it is the reviewer that gets relaunched. The class,
    the cause and the evidence beneath it come from the shared classifier, in the same words the
    worker path writes and the same words this tick's outcome carries.
    """
    if not failure.infrastructure:
        return f"review bring-up failed: {reason}\n{failure.clause()}"
    sha = candidate_sha(record)
    return (
        f"reviewer infrastructure failed on {record.review_infra_failures} consecutive launch "
        f"attempts over a green candidate; the card was never reworked and its gate receipt for "
        f"{sha or '(sha unavailable)'} still stands, so relaunch the reviewer rather than the "
        f"worker: {reason}\n{failure.clause()}"
    )


def review_infrastructure_failure(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    payload: dict[str, Any],
    reason: str,
    outcome_reason: str,
    exc: BaseException | None = None,
) -> dict[str, Any]:
    """One bounded path for a proven-headless reviewer bring-up failure over a green candidate.

    The classification is the worker path's, made by the same call: `exc` is the failure as the host
    raised it where there is one, and the preflight cases that have no exception (a head resource
    that is not ready, a launch intent that could not be written) pass their `reason` as evidence.
    Only an infrastructure outcome is held for another launch — a card whose own bring-up contract
    is broken is not made whole by relaunching a reviewer over it.
    """
    ref = task["ref"]
    failure = classify_bring_up_failure(
        exc,
        record,
        REVIEW_ROLE,
        stage=STAGE_REVIEW,
        attempt_id=record.attempt_id or attempt_id,
        detail=reason,
    )
    if failure.infrastructure:
        held = review_infrastructure_retry(
            runtime, task, records, record, attempt_id, payload=payload, reason=reason
        )
        if held is not None:
            return dict(held, **failure.outcome_fields(held["reason"]))
    blocked_reason = review_infrastructure_blocked_reason(record, reason, failure)
    runtime.terminal_effect(
        task,
        record,
        target="blocked",
        reason=blocked_reason,
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            bring_up_blocked_action("review-blocked", failure),
            ref,
            _wait_cycle_token(record),
        ),
        terminal_state="blocked",
        disposition="blocked",
        blocked_reason=bring_up_terminal_reason(failure),
    )
    records[ref] = record
    runtime.save_records(payload, records)
    return {
        "status": "blocked",
        "step": "review",
        "pilot_ref": ref,
        "reason": outcome_reason,
        **failure.outcome_fields(blocked_reason),
    }


def worktree_panes(host: Any, workspace: str) -> list[Any]:
    """One worktree's pane inventory, for a caller whose decision depends on reading it.

    The dispatcher runtime keeps its own inventory seam; a read-only observer holding nothing but
    the JSON transport gets the public Orca adapter. Both reach a refusal the same way, as a
    `HostError`, because an inventory that could not be read is not an empty worktree and neither
    caller may turn that difference into a missing pane.
    """
    inventory = getattr(host, "_worktree_terminals_or_raise", None)
    if callable(inventory):
        return list(inventory(workspace))
    return orca_worktree_panes(host._run_json, workspace)


def orca_worktree_panes(run_json: Any, workspace: str) -> list[Any]:
    """Ask the installed session manager itself, for a caller with no inventory seam of its own."""
    try:
        return list(OrcaSessionHost(run_json).panes(workspace))
    except PaneHostError as exc:
        raise HostError(str(exc)) from None


def orca_workspace_inventory(run_json: Any, workspace: str) -> WorkspaceInventory:
    """The panes of a worktree together with what the session manager's renderer draws there.

    The same refusal contract as `orca_worktree_panes`: an inventory that could not be read is a
    `HostError` and never an empty worktree. What the renderer could not answer is carried inside
    the inventory instead, because a readable pane list beside an unreadable layout is a real state
    and the caller has to be able to say so.
    """
    try:
        return OrcaSessionHost(run_json).workspace_inventory(workspace)
    except PaneHostError as exc:
        raise HostError(str(exc)) from None


def pane_matcher(record: DispatcherRecord, *, kind: str, task_ref: str):
    """The predicate that re-finds one role's pane in a worktree inventory.

    Identity first and the label last: `terminal list` can hand back a different handle alias for
    the same pty, so the persisted leaf is the strongest token, the handle the next one, and the
    reviewer's label only the fallback for a pane whose identity was never persisted. Shared with
    every read-only observer so the pane a status command reports on is exactly the pane the
    watchdog would have found.
    """
    if kind == "review":
        if record.review_leaf:
            return lambda pane: pane.leaf == record.review_leaf
        if record.review_handle:
            return lambda pane: pane.handle == record.review_handle
        label = review_pane_label(task_ref)
        return lambda pane: pane.title == label
    if record.worker_leaf:
        return lambda pane: pane.leaf == record.worker_leaf
    return lambda pane: bool(record.handle and pane.handle == record.handle)


def command_terminal_status(
    host: Any, task: dict[str, Any], record: DispatcherRecord, *, kind: str
) -> dict[str, Any]:
    """Return the tracked pane's liveness and its last output time.

    A failed inventory raises instead of looking like a missing pane, so the wait watchdog can report
    a degraded runtime without restarting a head on a transport failure. The public Orca session
    adapter owns the inventory call, so this read path only requires the host's JSON transport; it
    does not make a read-only status adapter pretend to be a dispatcher runtime.
    """
    if host.mode == "noop":
        return {"known": True, "live": True, "reason": "noop"}
    if not record.workspace:
        raise HostError(f"{kind} workspace is unavailable")
    terminals = worktree_panes(host, record.workspace)
    matches = pane_matcher(record, kind=kind, task_ref=str(task["ref"]))
    for terminal in terminals:
        if not matches(terminal):
            continue
        run = record.review_head_run if kind == "review" else record.worker_head_run
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        pid_status = _head_run_process_status(
            _pid_file_path(kind, task["ref"]),
            run=run,
            role=kind,
            task=f"card:{task['ref']}",
            leaf=leaf,
        )
        # Fence a live foreign heartbeat before treating a disconnected pane as terminal.
        if _heartbeat_is_mismatch(pid_status):
            return {
                "known": True,
                "live": True,
                "reason": "heartbeat-identity-mismatch",
                "identity_mismatch": True,
                "pid_confirmed": False,
            }
        if not terminal.connected:
            # Pass classification to vitality reduction with the dropped-pane observation.
            return {
                "known": True,
                "live": False,
                "reason": "disconnected",
                "pid_status": dict(pid_status),
            }
        if _heartbeat_is_dead(pid_status):
            # The pane is connected and Orca kept its wrapping shell open, but the head process
            # itself is gone (secretary-751): a provider crash or a killed runtime, not silence.
            return {
                "known": True,
                "live": False,
                "reason": "process-exited",
                "pid_status": dict(pid_status),
            }
        activity = terminal.last_output_at or None
        # Only observed provider cursors refresh liveness; tui-idle is independent.
        try:
            provider_progress = getattr(
                host, "provider_progress", lambda _task, _record, _kind: {"state": "unavailable"}
            )(task, record, kind)
        except Exception:
            provider_progress = {"state": "unavailable", "reason": "provider-progress probe failed"}
        provider_progress = _admitted_provider_progress_for_status(
            provider_progress,
            record.review_head_run if kind == "review" else record.worker_head_run,
        )
        if (
            str(provider_progress.get("state") or "") == "observed"
            and str(provider_progress.get("admission") or "") == "accepted"
        ):
            try:
                observed_at = float(provider_progress.get("observed_at") or 0.0)
            except (TypeError, ValueError):
                observed_at = 0.0
            if observed_at:
                activity = max(activity or 0.0, observed_at)
        pid_confirmed = _heartbeat_is_live_match(pid_status)
        status = {
            "known": True,
            "live": True,
            "reason": "live",
            "last_activity": activity,
            # A pid-heartbeat that proves this exact process still runs; only this — not a
            # silent pane — should let a wait watchdog trust liveness past the timing ceilings.
            "pid_confirmed": pid_confirmed,
            # The raw classification, passed through so the shadow vitality reduction can build
            # its own snapshot without a second /proc probe. No existing consumer reads it.
            "pid_status": dict(pid_status),
            "provider_progress": dict(provider_progress),
        }
        if pid_confirmed:
            # Whether the head is working or waiting at its prompt. Only asked of a process the
            # heartbeat proves is running, because that is the one case where no timing ceiling
            # applies and silence has to be told apart from a finished turn (secretary-1063). The
            # key is absent when the question could not be answered, which is not the same as a
            # busy head: the caller falls back to its timing ceilings for that.
            work = _pane_work_state(host, terminal.handle)
            if work:
                status["idle"] = work != "working"
                status["idle_reason"] = work
        return status
    # Missing inventory does not beat an exact live heartbeat; never respawn beside it.
    run = record.review_head_run if kind == "review" else record.worker_head_run
    leaf = record.review_leaf if kind == "review" else record.worker_leaf
    pid_status = _head_run_process_status(
        _pid_file_path(kind, task["ref"]),
        run=run,
        role=kind,
        task=f"card:{task['ref']}",
        leaf=leaf,
    )
    if _heartbeat_is_mismatch(pid_status):
        return {
            "known": True,
            "live": True,
            "reason": "heartbeat-identity-mismatch",
            "identity_mismatch": True,
            "pid_confirmed": False,
        }
    if _heartbeat_is_live_match(pid_status):
        # The classification rides along (a suspended head behind a lost pane must be
        # seen as Suspended, never aged as Unverifiable), and the pid answers the
        # process axis alone: no pane flag exists on this shape.
        return {
            "known": True,
            "live": True,
            "reason": "pid",
            "pid_confirmed": True,
            "pid_status": dict(pid_status),
        }
    if _heartbeat_is_dead(pid_status):
        # The pane vanished AND the heartbeat names a gone process: the reclaim is
        # evidence-backed, so the classification rides along and the reduction sees Dead.
        return {
            "known": True,
            "live": False,
            "reason": "missing-terminal",
            "pid_status": dict(pid_status),
        }
    if not pid_status.get("known"):
        # `pid_file_path`'s own contract: the dispatcher clears the pid file before every fresh
        # launch and the new head writes it "the moment it starts", so a respawn opens a window in
        # which neither identity answers — the handle/leaf just written may alias to nothing in the
        # inventory (the case above) and the heartbeat has not been written yet either. The observer
        # path already grants a launch grace window for exactly this reading (`observer_alive`); the
        # worker/reviewer path did not, so a watchdog tick landing in that window read a live,
        # just-(re)launched head as missing-terminal and, being the second such tick, escalated
        # straight to Blocked (secretary-1158).
        started_at = record.review_started_at if kind == "review" else record.worker_started_at
        if started_at and time.time() - started_at <= _initial_output_stall_seconds():
            return {"known": True, "live": True, "reason": "pid-not-written-yet", "pid_confirmed": False}
    # A lost pane plus live heartbeat is observation failure, not death.
    return {
        "known": True,
        "live": bool(pid_status.get("alive")) if pid_status.get("known") else False,
        "reason": "missing-terminal",
        "pid_status": dict(pid_status),
    }


def _admitted_provider_progress_for_status(value: Any, run: Any) -> dict[str, Any]:
    """Keep shared worker/reviewer liveness inside the same exact-HeadRun admission fence.

    The generic status seam receives a host response as data, so it must verify the response's run
    binding before it lets an opaque cursor renew either role's watchdog.
    """
    if not isinstance(value, dict):
        return {"state": "unavailable", "reason": "invalid provider-progress shape"}
    result: dict[str, Any] = {
        "state": str(value.get("state") or "unavailable")[:40],
        "reason": scrub_host_output(str(value.get("reason") or ""))[:240],
    }
    for name, limit in (
        ("admission", 40),
        ("source", 80),
        ("source_fingerprint", 64),
        ("cursor", 240),
        ("head_run_id", 120),
        ("head_run_fingerprint", 64),
    ):
        if name in value:
            result[name] = str(value.get(name) or "")[:limit]
    if "observed_at" in value:
        result["observed_at"] = str(value.get("observed_at") or "")[:64]
    if result["state"] != "observed" or result.get("admission") != "accepted":
        return result
    run_id, fingerprint = head_run_binding(run)
    if not run_id:
        return {
            "state": "unavailable",
            "reason": "persisted HeadRun is unavailable for provider-progress admission",
        }
    if result.get("head_run_id") != run_id or result.get("head_run_fingerprint") != fingerprint:
        return {
            "state": "identity_mismatch",
            "reason": "provider-progress observation does not name the persisted HeadRun",
        }
    source_fingerprint = str(result.get("source_fingerprint") or "")
    if (
        not result.get("source")
        or not result.get("cursor")
        or len(source_fingerprint) != 32
        or any(character not in "0123456789abcdef" for character in source_fingerprint.lower())
    ):
        return {
            "state": "unavailable",
            "reason": "provider-progress source admission is incomplete",
        }
    return result


def _pane_work_state(host: Any, handle: str) -> str:
    """Is this pane working on a turn, waiting for input, or held in a dialog? "" if unknowable.

    Orca's `tui-idle`, from the pane's own agent status falling back to a quiescence window, so it
    reads no screen and answers for every adapter. A pane held in a dialog counts as stopped rather
    than busy. The empty answer matters as much as the other three: a refused probe, a stale binding
    or a handle Orca no longer knows is neither working nor stopped, and the caller falls back to the
    timing ceilings.
    """
    if not handle:
        return ""
    readiness = terminal_readiness(handle, run_json=host._run_json)
    if readiness == READINESS_READY:
        return "idle"
    if readiness == READINESS_BLOCKED:
        return "dialog"
    return "working" if readiness == READINESS_BUSY else ""


def end_review_pane(host: Any, record: DispatcherRecord, initiator: str = STOPPED_BY_DISPATCHER) -> None:
    """Close the reviewer's pane and forget it. Used wherever the reviewer's lifecycle ends on its own
    — a red verdict, a respawn after a silent reviewer — so the next bring-up cannot mistake a stale
    handle for a live pane, and so the worker's workspace survives untouched.

    `initiator` is who is ending it and every caller names one. The pane pointers are dropped
    afterwards; the run itself stays on the record, because the initiator it carries is what makes
    the stop readable after the head is gone.

    A stop the host will not confirm raises, and the record keeps pointing at that reviewer: every
    caller opens something in the same checkout right after.
    """
    host.stop_review(record, initiator)
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
        status = runtime.host.review_status(task, record)
    except Exception as exc:
        # Inventory silence cannot prove the reviewer is absent. Preserve launch ambiguity and ask
        # the same liveness question next tick; never launch beside a possibly-live head.
        return {
            "status": "degraded",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id or attempt_id,
            "action": "review-inventory-unavailable",
            "reason": scrub_host_output(str(exc)),
        }
    if status.get("identity_mismatch"):
        # A readable live heartbeat can still name a foreign process.  Do not adopt it into the
        # review state or reset the launch episode; this record remains the un-attributed run.
        return {
            "status": "degraded",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id or attempt_id,
            "action": "review-heartbeat-identity-mismatch",
            "reason": "review heartbeat names a live process with a mismatching launch identity",
        }
    if status.get("live"):
        record.state = "reviewing"
        # A reviewer is on the checkout: whatever stuck launches came before belong to an episode
        # that is over, so the abort ceiling starts fresh for the next one (issue:aa9a8ae4), and so
        # does the infrastructure hold this card may have been retrying under (secretary-1401).
        record.review_launch_aborts = 0
        record.review_infra_failures = 0
        record.review_infra_error = ""
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


def _reviewer_launch_aborted(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    attempt_id: str,
    exc: HeadLaunchAborted,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """The ambiguous reviewer bring-up: its pane is open and nothing can say the head is gone.

    "No reviewer exists" is exactly what cannot be claimed here, so the intent stays on disk with
    what the failure knew of that head. Blocking the card and dropping the record instead would leave
    a live reviewer with nothing pointing at it.
    """
    evidence = getattr(exc, "evidence", None)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    busy = delivery_readiness_state(exc) == READINESS_BUSY
    if busy:
        # The pane was observed working before any document nudge was sent.  Its live heartbeat is
        # not launch confirmation, so retain the exact run and schedule the delivery retry on the
        # intent before it reaches launch recovery.
        defer_busy_launch_delivery(record, evidence if isinstance(evidence, dict) else {})
    mark_launch_aborted(runtime, payload, records, ref, record, exc)
    record.state = "review_starting"
    if busy:
        records[ref] = record
        runtime.save_records(payload, records)
        delivery = busy_launch_delivery(record.launch_intent)
        delay = max(0, int(float(delivery.get("next_at") or 0.0) - time.time()))
        return {
            "status": "degraded",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id or attempt_id,
            "action": "review-launch-busy",
            "head": record.review_head,
            "attempts": int(delivery.get("attempts") or 0),
            "reason": (
                "the reviewer pane was busy before its document nudge was sent; its exact launch "
                f"intent is retained and retry is due in {delay}s"
            ),
        }
    # Count this abort and, once it has repeated past the ceiling, pull an operator in once.
    # The record and its intent are untouched — a head may still be running, so this never
    # blocks or drops the card — but a launch that cannot freeze its worker for this many ticks
    # is no longer a transient the steward's degraded line covers on its own (issue:aa9a8ae4).
    record.review_launch_aborts += 1
    _escalate_stuck_review_launch(runtime, task, record, attempt_id)
    records[ref] = record
    runtime.save_records(payload, records)
    return launch_aborted(
        step="review",
        ref=ref,
        attempt_id=record.attempt_id or attempt_id,
        role=REVIEW_ROLE,
        reason=scrub_host_output(str(exc)),
    )


def retry_busy_reviewer_launch_delivery(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    record: DispatcherRecord,
    intent: dict[str, Any],
    step: str,
) -> dict[str, Any] | None:
    """Retry an unaccepted reviewer nudge, over its exact live launch, before it can be adopted.

    "Unaccepted" is wider than "busy": a pane that was held in a dialog, one still starting its MCP
    servers and one that left the pointer sitting in its composer are the same fact here — the
    reviewer has not received the document — and they retry the *same* immutable pointer, at the
    same path, over the same run, rather than a rebuilt or duplicated one.

    A launch heartbeat only proves that the pane exists. Until the document nudge is confirmed it
    does not permit the adoption side effects: freezing the worker, recording routing, clearing the
    intent or setting the review lifecycle. The intent is also the retry's durable cursor.
    """
    delivery = undelivered_launch_delivery(intent)
    if not delivery:
        return None
    if int(delivery.get("attempts") or 0) >= LAUNCH_DELIVERY_MAX_ATTEMPTS:
        # The retry is bounded like everything else here. Past the ceiling the launch-recovery path
        # owns the head: it refuses the adoption and replaces the reviewer rather than nudging a
        # pane that has not taken a pointer in five attempts.
        return None
    ref = task["ref"]
    now = time.time()
    next_at = float(delivery.get("next_at") or 0.0)
    if next_at and now < next_at:
        state = str(delivery.get("state") or "")
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            # The outcome says which state is holding the pointer. A pane that was working and a
            # pane that never accepted the document are both deferred here and are not the same
            # fact, so they are not reported under the same word.
            "action": ("review-launch-busy" if state == READINESS_BUSY else "review-launch-undelivered"),
            "head": str(intent.get("head") or record.review_head),
            "readiness": state,
            "attempts": int(delivery.get("attempts") or 0),
            "reason": (
                "the reviewer document nudge is still deferred while its exact pane is "
                f"{pane_state_label(state)}; retry is due in {max(0, int(next_at - now))}s"
            ),
        }
    bind_ingress = getattr(runtime, "bind_codex_provider_ingress", None)
    if callable(bind_ingress):
        bind_ingress(record, records, payload, role=REVIEW_ROLE, reference=ref)
    try:
        retried = runtime.host.nudge_review_delivery(task, record, intent)
    except Exception as exc:  # noqa: BLE001 — evidence is the delivery boundary's contract
        evidence = getattr(exc, "evidence", None)
        if hasattr(evidence, "to_json"):
            evidence = evidence.to_json()
        if not isinstance(evidence, dict):
            evidence = {}
        _record_review_delivery_failure(record, exc)
        state = delivery_readiness_state(exc)
        if state == READINESS_BUSY:
            delay = defer_busy_launch_delivery(record, evidence, now=now)
            records[ref] = record
            runtime.save_records(payload, records)
            attempts = int(busy_launch_delivery(record.launch_intent).get("attempts") or 0)
            return {
                "status": "degraded",
                "step": step,
                "pilot_ref": ref,
                "attempt_id": record.attempt_id,
                "action": "review-launch-busy",
                "head": str(intent.get("head") or record.review_head),
                "attempts": attempts,
                "reason": (
                    "the reviewer pane remained busy before its document nudge was sent; its "
                    f"exact launch intent is retained and retry {attempts} waits {delay}s"
                ),
            }
        # Do not silently convert an unavailable, malformed or stale probe into busy.  The intent
        # stays on disk with the typed evidence; on the following tick the existing live-launch
        # recovery path owns its conservative stop/adoption decision.  The attempt is counted and
        # backed off under the same schedule, so an unreachable pane is bounded rather than retried
        # every tick until somebody notices.
        defer_launch_delivery(record, evidence, state=state or READINESS_BLOCKED, now=now)
        records[ref] = record
        runtime.save_records(payload, records)
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": "review-launch-delivery-unavailable",
            "head": str(intent.get("head") or record.review_head),
            "readiness": state,
            "reason": "the retained reviewer launch could not be nudged; its typed delivery "
            "evidence is retained for normal launch recovery",
        }
    if not isinstance(retried, dict):
        raise HostError("reviewer delivery retry returned no durable result")
    evidence = retried.get("delivery_evidence")
    head_run = retried.get("head_run")
    # A started turn with the document still in the composer is not a reviewer receipt, and it is
    # not an ambiguity either: the pointer was looked for and found sitting there. It is recorded
    # as the determinate refusal it is rather than as a pane that was busy.
    if (
        isinstance(evidence, dict)
        and bool(evidence.get("turn_confirmed"))
        and bool(evidence.get("payload_left_in_composer"))
    ):
        record.review_delivery_evidence = dict(evidence)
        delay = defer_launch_delivery(record, evidence, state=DELIVERY_RECEIPT_REFUSED, now=now)
        records[ref] = record
        runtime.save_records(payload, records)
        attempts = int(undelivered_launch_delivery(record.launch_intent).get("attempts") or 0)
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": "review-launch-undelivered",
            "head": str(intent.get("head") or record.review_head),
            "attempts": attempts,
            "reason": (
                "the reviewer transport observed a turn but the document remained in the "
                f"composer; its exact launch intent is retained and retry waits {delay}s"
            ),
        }
    if not isinstance(evidence, dict) or not bool(evidence.get("turn_confirmed")):
        raise HostError("reviewer delivery retry returned without confirmation evidence")
    if not isinstance(head_run, dict) or not head_run.get("run_id"):
        raise HostError("reviewer delivery retry returned without the launched head run")
    if evidence:
        record.review_delivery_evidence = dict(evidence)
    # Persist accepted delivery with exact run before reviewer adoption.
    confirmed = {
        **delivery,
        "state": "confirmed",
        "next_at": 0.0,
        "confirmed_at": now,
        "evidence": dict(evidence),
    }
    confirm_launch_intent(
        runtime,
        payload,
        records,
        ref,
        record,
        handle=str(retried.get("handle") or intent.get("handle") or ""),
        leaf=str(retried.get("leaf") or intent.get("leaf") or ""),
        head_run=dict(head_run),
        delivery=confirmed,
    )
    return None


def _record_review_delivery_failure(record: DispatcherRecord, exc: Exception) -> None:
    """Keep a reviewer prompt that did not land as durable card telemetry.

    Only a failure the delivery boundary evidenced counts: a split that would not open is a bring-up
    failure, not a prompt that was refused. What is kept is what the boundary saw, and it is never
    reset by a later reviewer, so a card cannot report that every prompt landed once one finally does.
    """
    evidence = getattr(exc, "evidence", None)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    if not isinstance(evidence, dict) or not evidence:
        return
    record.review_delivery_evidence = dict(evidence)
    # Preserve a successful receipt when a later reviewer-launch step aborts.
    if not bool(evidence.get("turn_confirmed")) and delivery_readiness_state(evidence) != READINESS_BUSY:
        record.review_delivery_failures += 1


def _escalate_stuck_review_launch(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    attempt_id: str,
) -> None:
    """Comment once when a reviewer launch has aborted past the stuck ceiling.

    The abort keeps the record on purpose, which is also what makes the loop silent. Past the ceiling
    this leaves one durable, operator-addressed note on the card. The request id is stable within the
    stuck episode and distinct across episodes, so the board carries one note per episode.
    """
    ceiling = _review_launch_abort_stuck_ticks()
    if record.review_launch_aborts < ceiling:
        return
    ref = task["ref"]
    # Keep idempotent abort-comment bodies stable across ticks.
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"⚠️ Reviewer launch has aborted for at least {ceiling} ticks running and is not "
            "recovering on its own: the reviewer pane came up and the bring-up could not be "
            "finished over it, so the card is stuck before review. An operator should look, and "
            "the dispatcher tick's own reason field carries what each attempt failed on."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            "review-launch-stuck",
            ref,
            _wait_cycle_token(record),
        ),
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
    try:
        readiness = runtime.head_readiness(record.review_head)
    except HostError as exc:
        if record.gate_state != "green":
            raise
        record.state = "review_starting"
        return review_infrastructure_failure(
            runtime,
            task,
            records,
            record,
            attempt_id,
            payload=payload,
            reason=scrub_host_output(str(exc)),
            outcome_reason="review resource check failed",
            exc=exc,
        )
    if not readiness.launch_allowed:
        record.state = "review_starting"
        if record.gate_state == "green":
            return review_infrastructure_failure(
                runtime,
                task,
                records,
                record,
                attempt_id,
                payload=payload,
                reason=readiness.reason,
                outcome_reason="review resource unavailable",
            )
        return {
            "status": "skipped",
            "step": "head-preflight",
            "action": "review-resource-not-ready",
            "pilot_ref": ref,
            "head": record.review_head,
            "readiness": readiness.to_json(),
            "reason": readiness.reason,
        }
    # Persist reviewer intent before pane split to prevent a crash-era duplicate.
    intent_kwargs: dict[str, Any] = {}
    prompt_document_path = getattr(runtime.host, "_prompt_document_path", None)
    if callable(prompt_document_path):
        # The real host writes the review packet outside the checkout. Its preflight descriptor
        # must carry that same pointer, not the historical in-worktree placeholder.
        intent_kwargs["document"] = str(prompt_document_path(REVIEW_ROLE, ref, record.review_baseline))
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
        **intent_kwargs,
    )
    if failure is not None:
        if failure.startswith("codex-fanout-policy:"):
            runtime.terminal_effect(
                task,
                record,
                target="blocked",
                reason=f"Codex provider fan-out policy refused reviewer preflight: {failure}",
                request_id=_attempt_request_id(
                    record.attempt_id or attempt_id, "codex-fanout-review-blocked", ref
                ),
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="provider",
            )
            records.pop(ref, None)
            runtime.save_records(payload, records)
            return {
                "status": "blocked",
                "step": "review",
                "pilot_ref": ref,
                "policy_evidence": {"kind": "codex_provider_fanout", "state": "unknown"},
                "reason": failure,
            }
        record.state = "review_starting"
        if record.gate_state == "green":
            return review_infrastructure_failure(
                runtime,
                task,
                records,
                record,
                attempt_id,
                payload=payload,
                reason=failure,
                outcome_reason="review launch intent unavailable",
            )
        return launch_intent_unwritable(
            step="review",
            ref=ref,
            attempt_id=record.attempt_id or attempt_id,
            role=REVIEW_ROLE,
            reason=failure,
        )
    # The durable intent owns the exact pre-pane HeadRun and provider binding.
    bind_ingress = getattr(runtime, "bind_codex_provider_ingress", None)
    if callable(bind_ingress):
        bind_ingress(record, records, payload, role=REVIEW_ROLE, reference=ref)
    try:
        launch = runtime.host.start_review(task, record)
    except Exception as exc:
        # Normalize and persist prompt evidence once; infrastructure failures carry none.
        _record_review_delivery_failure(record, exc)
        if isinstance(exc, HeadLaunchAborted):
            return _reviewer_launch_aborted(
                runtime, task, records, ref, record, attempt_id, exc, payload=payload
            )
        if launch_left_a_head(record):
            # A live exact heartbeat preserves intent for adoption or fenced stop.
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
        if record.gate_state == "green":
            # No-head reviewer failures share one infrastructure transition and counter.
            return review_infrastructure_failure(
                runtime,
                task,
                records,
                record,
                attempt_id,
                payload=payload,
                reason=scrub_host_output(str(exc)),
                outcome_reason="host review failed",
                exc=exc,
            )
        else:
            deferred = launch_deferred(
                record,
                exc,
                step="review",
                ref=ref,
                attempt_id=record.attempt_id or attempt_id,
                role=REVIEW_ROLE,
            )
        if record.gate_state != "green" and deferred is not None:
            # No green candidate to preserve, but a pane that is merely busy still defers: the card
            # keeps its record in `review_starting`, which is the state the next tick recovers the
            # launch from, so the round is delayed by a tick rather than lost to Blocked.
            record.state = "review_starting"
            records[ref] = record
            runtime.save_records(payload, records)
            return deferred
        # No green candidate to hold, and the bounded pane deferral is spent. The classification is
        # the same call the worker path makes, so the card's reason, the class in the transition and
        # this tick's outcome are one statement. Nothing is relaunched from here.
        failure = classify_bring_up_failure(
            exc,
            record,
            REVIEW_ROLE,
            stage=STAGE_REVIEW,
            attempt_id=record.attempt_id or attempt_id,
        )
        blocked_reason = bring_up_blocked_reason(
            "review bring-up failed", exc, record, REVIEW_ROLE, failure=failure
        )
        runtime.terminal_effect(
            task,
            record,
            target="blocked",
            reason=blocked_reason,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                bring_up_blocked_action("review-blocked", failure),
                ref,
                _wait_cycle_token(record),
            ),
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason=bring_up_terminal_reason(failure),
        )
        records.pop(ref, None)
        return {
            "status": "blocked",
            "step": "review",
            "pilot_ref": ref,
            "reason": "host review failed",
            **failure.outcome_fields(blocked_reason),
        }
    # Persist reviewer pane, launch snapshot, and HeadRun together before record adoption.
    confirm_launch_intent(
        runtime,
        payload,
        records,
        ref,
        record,
        handle=launch.handle,
        leaf=launch.leaf,
        run=launch.run,
        head_run=dict(launch.head_run),
    )
    record.review_handle = launch.handle
    record.review_leaf = launch.leaf
    record.review_commit = launch.commit
    if launch.delivery_evidence:
        # Successful and refused reviewer launches use the same durable, metadata-only receipt.
        # A later recovery therefore sees the actual transport version and submit count instead
        # of assuming the pane received the review because the split succeeded.
        record.review_delivery_evidence = dict(launch.delivery_evidence)
    # The verdict this pane issues belongs to this head, so the round records it now, from the
    # launcher's own snapshot (secretary-716). The intent is spent only once that has landed: a
    # journal that refuses here leaves the reviewer adoptable, and the adoption writes the routing
    # event the round would otherwise never get for it.
    runtime.record_review_routing(task, record, launch.run)
    clear_launch_intent(record)
    record.review_started_at = record.review_progress_at = time.time()
    reset_launch_attempts(record, REVIEW_ROLE)
    # The reviewer took the checkout, so any stuck-launch episode before it is over and its abort
    # ceiling starts fresh for the next one (issue:aa9a8ae4), as does any infrastructure hold the
    # card was retrying under (secretary-1401).
    record.review_launch_aborts = 0
    record.review_infra_failures = 0
    record.review_infra_error = ""
    # A retained worker is suspended, not gone: it keeps its pane and its heartbeat so a red
    # verdict can continue that same conversation, and the reviewer still judges a checkout
    # nothing is editing. Without retention the worker head was shut down for the reviewer, and
    # the record must stop naming a pane that no longer exists.
    if not record.worker_continuation.retained:
        forget_role_head(record, WORKER_ROLE)
    record.state = "reviewing"
    outcome = {
        "status": "ok",
        "step": "review",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": action,
    }
    if launch.fallback_reason:
        outcome["reviewer_fallback_reason"] = launch.fallback_reason
    return outcome
