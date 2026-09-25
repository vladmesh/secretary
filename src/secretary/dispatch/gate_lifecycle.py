"""Mechanical validation gate lifecycle and bounded recovery.

This module owns the dispatcher state machine from asking the mechanical gate through
green/red/pending/transport/infrastructure outcomes. Review and Assessment remain separate;
release/merge terminal blocking is package-owned by dispatch.release_lifecycle, and shared
head vitality remains owned by dispatch.wait_vitality.
"""

from __future__ import annotations

import time
from typing import Any

from secretary.dispatch import attempt_accounting, release_lifecycle
from secretary.dispatch.gate import (
    GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS,
    GATE_PENDING_STALL_SECONDS,
    GATE_TRANSPORT_MAX_ATTEMPTS,
    GateResult,
)
from secretary.dispatch.gate import _fingerprint as _gate_fingerprint
from secretary.dispatch.gate import reset_infrastructure_reruns as _reset_infrastructure_reruns
from secretary.dispatch.gate import validation_ci as _validation_ci
from secretary.dispatch.gate_receipt import AcceptedGreenGate
from secretary.dispatch.head_vitality_episode import VitalityVerdict
from secretary.dispatch.helpers import _gate_red_repeat_count, scrub_host_output
from secretary.dispatch.state import DispatcherRecord, PersistedGateReceipt
from secretary.dispatch.state import attempt_request_id as _attempt_request_id
from secretary.dispatch.types import (
    STOPPED_BY_REPLACEMENT,
    GateTransportError,
    HostError,
    ProjectGitAccessError,
)
from secretary.dispatch.wait_vitality import execute_recovery_intent as _execute_recovery_intent
from secretary.dispatch.wait_vitality import recovery_policy_outcome as _recovery_policy_outcome
from secretary.dispatch.wait_vitality import (
    reduce_and_store_vitality_episode as _reduce_and_store_vitality_episode,
)
from secretary.dispatch.worker_continuation import begin_red_transition as _begin_red_transition


def run_gate(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any] | None:
    """Run the mechanical gate before the reviewer. Returns None (gate green: fall through to
    review this same tick) or a tick outcome (red bounced the card to the worker, pending is
    waiting on CI, or the gate infra failed and the card is Blocked)."""
    ref = task["ref"]
    if record.worker_continuation.validation_move_pending:
        # The move committed but the checkpoint did not; close it before a red gate acts.
        record.worker_continuation.confirm_validation_move()
        records[ref] = record
        runtime.save_records(payload, records)
    try:
        result = runtime.host.gate_check(task, record)
    except GateTransportError as exc:
        retry = gate_transport_retry(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            exc,
            step="gate",
        )
        if retry is not None:
            return retry
        return block_gate_transport(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            step="gate",
            action="gate-transport-blocked",
        )
    except HostError as exc:
        runtime.host.stop(record)
        attempt_accounting.terminal_effect(runtime, 
            task,
            record,
            target="blocked",
            reason=f"validation gate failed: {scrub_host_output(str(exc))}",
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "gate-blocked", ref),
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason="gate",
        )
        records.pop(ref, None)
        runtime.save_records(payload, records)
        outcome: dict[str, Any] = {
            "status": "blocked",
            "step": "gate",
            "pilot_ref": ref,
            "reason": "validation gate failed",
        }
        if isinstance(exc, ProjectGitAccessError):
            # A refused credential is its own determinate class, never the transport retry.
            outcome["git_access_refusal"] = {"project": exc.project, "code": exc.code}
        return outcome
    gate_answered(runtime, ref, record, records, payload)
    if result.status == "green":
        return accept_green_gate(runtime, 
            task, record, records, payload, attempt_id, result, stage="initial"
        )
    if result.status == "pending":
        return gate_pending(runtime, task, record, records, payload, attempt_id, result)
    return gate_red_to_worker(runtime, task, record, records, payload, attempt_id, result, phase="gate")


def accept_green_gate(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    result: GateResult,
    *,
    stage: str,
) -> dict[str, Any] | None:
    """Validate and persist every green gate through one exact-SHA policy boundary."""
    ref = task["ref"]
    accepted = AcceptedGreenGate.accept(
        result.attestation,
        current_sha=runtime.host.head_commit(record),
        gate_mode=_validation_ci(runtime.host, task),
        noop=getattr(runtime.host, "mode", "real") == "noop",
    )
    if not accepted.valid:
        if stage == "initial":
            return _block_missing_gate_receipt(runtime, task, record, records, payload, attempt_id)
        step = "assessment" if stage == "release" else "review"
        return release_lifecycle.block_merge_path(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            action=f"{stage}-gate-receipt-blocked",
            reason=f"{stage} gate reported green without a valid exact-SHA receipt",
            step=step,
            outcome=f"{stage} gate receipt unavailable",
        )
    record.gate_state = "green"
    record.gate_pending_since = 0.0
    _reset_infrastructure_reruns(record)
    record.gate_attestation = PersistedGateReceipt(accepted.receipt)
    records[ref] = record
    runtime.save_records(payload, records)
    if accepted.receipt is not None and stage in {"assessment", "release"}:
        label = "Assessment delivery" if stage == "assessment" else "release audit"
        audit_key = accepted.receipt.command_or_check_set_digest[:12]
        if stage == "assessment":
            audit_key = f"{record.review_baseline}-{audit_key}"
        closing = (
            "The observer consumes this fresh receipt, the worker report and the reviewer "
            "verdict before opening code or running any check."
            if stage == "assessment"
            else "Exact-SHA pre-merge gate receipt is valid; merge follows as a separate effect."
        )
        runtime.writer.comment(
            role="dispatcher",
            actor=runtime.owner,
            reference=ref,
            body=(
                f"## Mechanical gate attestation — {label}\n\n"
                + accepted.receipt.render()
                + (
                    "\n\nReview/base reconciliation: "
                    f"reviewed SHA `{record.review_reconciliation['reviewed_sha']}`; "
                    f"HEAD `{record.review_reconciliation['head_sha']}`; "
                    f"base SHA `{record.review_reconciliation['base_sha']}`; "
                    f"{record.review_reconciliation['reviewed_paths']} reviewed paths; "
                    "reviewed paths unchanged."
                    if stage == "release" and record.review_reconciliation is not None
                    else ""
                )
                + f"\n\n{closing}"
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                f"gate-attestation-{stage}",
                ref,
                audit_key,
            ),
        )
    return None


def _block_missing_gate_receipt(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """A configured broad gate cannot turn green without exact-SHA evidence to hand on.

    Deliberately separate from ``ci:none``: local/github promised to execute a check and therefore
    fail closed if the SHA/base/check receipt cannot be materialized.
    """
    ref = task["ref"]
    runtime.host.stop(record)
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="blocked",
        reason=(
            "validation gate reported green but did not provide a valid exact-SHA receipt "
            "(SHA, base SHA and terminal checks); blocked rather than treating it as attested"
        ),
        request_id=_attempt_request_id(record.attempt_id or attempt_id, "gate-receipt-blocked", ref),
        terminal_state="blocked",
        disposition="blocked",
        blocked_reason="gate",
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {"status": "blocked", "step": "gate", "pilot_ref": ref, "reason": "gate receipt unavailable"}


def gate_red_to_worker(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    result: GateResult,
    *,
    phase: str,
) -> dict[str, Any]:
    """A red mechanical gate sends the card back to the worker (In progress) with a scrubbed
    comment, mirroring the review-red rework path. `phase` distinguishes the pre-review gate
    from the pre-merge re-check in the request-id and the log line."""
    ref = task["ref"]
    if result.failure_class == "topology":
        # The candidate was never offered to CI, and no round of rework could change that: the
        # pull request's base or the project's triggers are what is wrong (secretary-1541).
        # Sending the worker back over its own code would spend a round on the wrong file, so
        # this goes to a human with the cause named instead.
        return release_lifecycle.block_merge_path(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            action=f"{phase}-gate-topology-blocked",
            reason=(
                "the mechanical validation gate cannot produce this project's required CI for "
                f"this card: {scrub_host_output(result.summary)}. No check ran and no rework "
                "changes that; the card's integration base or the project's workflow triggers "
                "have to be repaired."
            ),
            step="gate",
            outcome="gate ci topology",
        )
    if result.failure_class == "infrastructure":
        return _retry_infrastructure_gate(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            result,
            phase=phase,
        )
    record.rejected_sha = runtime.host.head_commit(record)
    # `publication` is carried through instead of flattened to `substantive`: the candidate was
    # never offered to CI, and the record has to say so where a later tick reads the class.
    record.rejected_failure_class = (
        "publication" if result.failure_class == "publication" else "substantive"
    )
    record.rejected_failure_reason = result.failure_reason
    record.rejected_done_reports = 0
    detail = scrub_host_output(result.summary)
    log = scrub_host_output(result.log).strip()
    # A GateResult built without `fingerprint` (the review-freeze drift check) still gets a
    # SHA-independent identity here rather than losing repeat detection outright.
    fingerprint = result.fingerprint or _gate_fingerprint("fallback", log or detail)
    repeat = _gate_red_repeat_count(task, fingerprint)
    prefix = f"Repeat return (round {repeat + 1}, the reason has not changed). " if repeat else ""
    if result.failure_class == "publication":
        # The distinction the card has to show: nothing was validated, so this is not a red CI
        # run and rerunning it changes nothing until the branch itself is dealt with.
        body = (
            f"{prefix}The mechanical validation gate never reached CI: the candidate branch "
            f"could not be published — {detail}. No check ran. The card is back in In progress; "
            "the branch has to be reconciled before another report can publish it."
        )
    else:
        body = (
            f"{prefix}The mechanical validation gate is red: {detail}. The card is back in "
            f"In progress for rework."
        )
    if log:
        body += f"\nTail:\n```\n{log}\n```"
    body += f"\n<!-- gate-fingerprint: {fingerprint} -->"
    # The reviewer must be gone before a retained worker resumes, but the worker stays
    # suspended until the continuation is delivered or falls back to a replacement.
    unconfirmed = runtime._end_review_pane_confirmed(
        record,
        records,
        payload,
        ref,
        step="gate",
        attempt_id=attempt_id,
        initiator=STOPPED_BY_REPLACEMENT,
    )
    if unconfirmed is not None:
        return unconfirmed
    # The round ends with no reviewer verdict: the outcome names the gate, not a reviewer.
    return _begin_red_transition(runtime, 
        task,
        record,
        records,
        payload,
        attempt_id,
        phase=phase,
        move_reason=body,
        verdict_outcome=f"{phase}_red",
    )


def _retry_infrastructure_gate(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    result: GateResult,
    *,
    phase: str,
) -> dict[str, Any]:
    """Rerun an enumerated CI-service outage without opening a worker rework round.

    The worker has already reported done and is retained at this point.  Moving back to In
    progress would manufacture a round with no code action, and its ``gate-red`` request id
    would charge the sprint.  No board move here means neither happens.
    """
    ref = task["ref"]
    sha = runtime.host.head_commit(record)
    if record.gate_infrastructure_reruns_sha != sha:
        _reset_infrastructure_reruns(record)
        record.gate_infrastructure_reruns_sha = sha
    spent = record.gate_infrastructure_reruns
    if spent >= GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS:
        return _block_infrastructure_reruns_exhausted(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            result,
            phase=phase,
        )
    try:
        runtime.host.rerun_failed_ci(task, record, result)
    except GateTransportError as exc:
        retry = _gate_rerun_transport_retry(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            exc,
            step=phase,
        )
        if retry is not None:
            return retry
        exhausted = GateTransportError(
            "failed Actions rerun stayed unreachable for "
            f"{record.gate_rerun_transport_failures} consecutive attempts: "
            f"{record.gate_rerun_transport_error}"
        )
        return _block_infrastructure_rerun_unavailable(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            result,
            exhausted,
            phase=phase,
        )
    except HostError as exc:
        return _block_infrastructure_rerun_unavailable(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            result,
            exc,
            phase=phase,
        )
    record.rejected_sha = sha
    record.rejected_failure_class = "infrastructure"
    record.rejected_failure_reason = result.failure_reason
    # `_accept_stale_infrastructure_done` records one accepted stale report as a guard against
    # a later duplicate.  Do not erase that guard when the rerun itself returns red again.
    # Fresh worker reports and substantive reds reset it at their own state transitions.
    record.gate_infrastructure_reruns += 1
    record.gate_infrastructure_rerun_run_id = result.failed_run_id
    record.gate_infrastructure_rerun_reason = result.failure_reason
    record.gate_rerun_transport_failures = 0
    record.gate_rerun_transport_error = ""
    record.gate_pending_since = time.time()
    detail = scrub_host_output(result.summary)
    log = scrub_host_output(result.log).strip()
    fingerprint = result.fingerprint or _gate_fingerprint("infrastructure", log or detail)
    body = (
        "The mechanical validation gate is red from an infrastructure failure "
        f"({result.failure_reason or 'enumerated CI-service signature'}): {detail}. "
        f"Actions run {result.failed_run_id or 'unavailable'} was rerun ({record.gate_infrastructure_reruns}/"
        f"{GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS}) and the exact SHA stays in Validate until its "
        "new terminal result; no worker rework round or red_ci budget event was opened."
    )
    if log:
        body += f"\nTail:\n```\n{log}\n```"
    body += f"\n<!-- gate-fingerprint: {fingerprint} -->"
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=body,
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            "gate-infrastructure-rerun",
            ref,
            f"{sha}:{record.gate_infrastructure_reruns}:{fingerprint}",
        ),
    )
    records[ref] = record
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "gate",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": "gate-infrastructure-rerun",
        "reason": result.failure_reason,
    }


def _block_infrastructure_reruns_exhausted(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    result: GateResult,
    *,
    phase: str,
) -> dict[str, Any]:
    ref = task["ref"]
    runtime.host.stop(record)
    reason = (
        "Mechanical gate remains red from infrastructure failure "
        f"({result.failure_reason or 'enumerated CI-service signature'}) after "
        f"{record.gate_infrastructure_reruns} Actions rerun(s) for HEAD "
        f"{record.gate_infrastructure_reruns_sha or runtime.host.head_commit(record)}; "
        "the bounded automatic recovery is exhausted."
    )
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="blocked",
        reason=reason,
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            f"{phase}-infrastructure-reruns-exhausted",
            ref,
            str(record.gate_infrastructure_reruns),
        ),
        terminal_state="blocked",
        disposition="blocked",
        # The failed gate is the terminal cause.  Its original runner
        # outage explains the bounded reruns, not this Blocked effect.
        blocked_reason="gate",
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {
        "status": "blocked",
        "step": phase,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": "gate-infrastructure-reruns-exhausted",
        "reason": result.failure_reason,
    }


def _block_infrastructure_rerun_unavailable(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    result: GateResult,
    exc: Exception,
    *,
    phase: str,
) -> dict[str, Any]:
    ref = task["ref"]
    runtime.host.stop(record)
    reason = (
        "Mechanical gate is red from infrastructure failure "
        f"({result.failure_reason or 'enumerated CI-service signature'}), but its failed Actions "
        f"run could not be rerun: {scrub_host_output(str(exc))}. Blocked rather than rereading "
        "the same terminal result."
    )
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="blocked",
        reason=reason,
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id, f"{phase}-infrastructure-rerun-blocked", ref
        ),
        terminal_state="blocked",
        disposition="blocked",
        # A rerun that cannot be requested leaves a gate obligation
        # unresolved; it is not a head bring-up infrastructure outcome.
        blocked_reason="gate",
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {
        "status": "blocked",
        "step": phase,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": "gate-infrastructure-rerun-blocked",
        "reason": result.failure_reason,
    }


def gate_answered(
    runtime: Any,
    ref: str,
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> None:
    """The backend answered, so the transport retry budget starts over."""
    if not record.gate_transport_failures and not record.gate_transport_error:
        return
    record.gate_transport_failures = 0
    record.gate_transport_error = ""
    records[ref] = record
    runtime.save_records(payload, records)


def gate_transport_retry(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    exc: GateTransportError,
    *,
    step: str,
) -> dict[str, Any] | None:
    """Count one unanswered gate question and, while the budget lasts, keep the card as it is.

    Returns the tick outcome of a deferred retry, or None once the attempts are spent and the
    caller must block the card. Nothing about the card moves here: no board move, no head stopped,
    no verdict or decision spent.
    """
    ref = task["ref"]
    record.gate_transport_failures += 1
    record.gate_transport_error = scrub_host_output(str(exc))
    attempts = record.gate_transport_failures
    records[ref] = record
    runtime.save_records(payload, records)
    if attempts >= GATE_TRANSPORT_MAX_ATTEMPTS:
        return None
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": "gate-transport-retry",
        "attempts": attempts,
        "max_attempts": GATE_TRANSPORT_MAX_ATTEMPTS,
        "reason": (
            f"the mechanical gate could not reach its backend "
            f"(attempt {attempts}/{GATE_TRANSPORT_MAX_ATTEMPTS}): "
            f"{record.gate_transport_error}; the card is unchanged and the gate is asked "
            f"again on the next tick"
        ),
    }


def _gate_rerun_transport_retry(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    exc: GateTransportError,
    *,
    step: str,
) -> dict[str, Any] | None:
    """Retry the answered-red recovery POST with the ordinary gate transport ceiling.

    The red check-run is a valid answer, but the subsequent rerun POST is a separate question.
    Its count cannot share the read's counter because the next red check-run would reset that
    counter before retrying the unanswered POST.
    """
    ref = task["ref"]
    record.gate_rerun_transport_failures += 1
    record.gate_rerun_transport_error = scrub_host_output(str(exc))
    attempts = record.gate_rerun_transport_failures
    records[ref] = record
    runtime.save_records(payload, records)
    if attempts >= GATE_TRANSPORT_MAX_ATTEMPTS:
        return None
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": "gate-rerun-transport-retry",
        "attempts": attempts,
        "max_attempts": GATE_TRANSPORT_MAX_ATTEMPTS,
        "reason": (
            f"the failed Actions rerun could not reach its backend "
            f"(attempt {attempts}/{GATE_TRANSPORT_MAX_ATTEMPTS}): "
            f"{record.gate_rerun_transport_error}; the card is unchanged and the rerun is "
            "asked again on the next tick"
        ),
    }


def block_gate_transport(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    step: str,
    action: str,
    prefix: str = "",
) -> dict[str, Any]:
    """The gate backend stayed unreachable for the whole retry budget: Blocked, saying so."""
    attempts = record.gate_transport_failures or GATE_TRANSPORT_MAX_ATTEMPTS
    last = record.gate_transport_error or "(no error text)"
    reason = (
        f"the mechanical gate could not reach its backend on {attempts} consecutive attempts, "
        f"so it never returned a verdict; this is a transport failure, not a red gate. "
        f"Last transport error: {last}"
    )
    return release_lifecycle.block_merge_path(runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        action=action,
        reason=f"{prefix}{reason}" if prefix else reason,
        step=step,
        outcome="gate transport unavailable",
    )


def gate_pending(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    result: GateResult,
    *,
    step: str = "gate",
    action: str = "gate-pending",
) -> dict[str, Any]:
    """CI is non-terminal (a check still running, or none posted yet). Wait, tracking how long the
    rollup has sat non-terminal; past GATE_PENDING_STALL_SECONDS escalate once to Blocked so a
    required check nothing ever posts does not leave the card unwatched forever.

    Since S1-5 the wait is no longer blind to the worker head it is waiting beside
    (issue fe04011b: a worker sat in `T (stopped)` for 27 minutes while every tick
    wrote ``gate-pending ok`` and only the six-hour ceiling applied). Each pending
    tick runs the same vitality reduction + recovery policy for the worker that the
    report-wait tick runs, so a suspended head sees its SIGCONT within one tick and an
    expired response window reaches the operator in minutes. The gate's own clock
    stays as the OUTER escalation ceiling for the CI rollup itself -- non-destructive
    per S1-4 semantics -- but it is no longer the first thing to notice a stopped
    process.

    What that watchdog may NOT do is undo the gate's own retention (secretary-1539).
    A worker parked by ``retain_worker`` on ``report:done`` is stopped on purpose and
    for exactly as long as this wait lasts; the reduction is told so, reports
    ``Retained`` rather than ``Suspended``, and this tick then leaves it alone. The
    ladder above stays in force for a head stopped by anyone else.
    """
    ref = task["ref"]
    now = time.time()
    if not record.gate_pending_since:
        record.gate_pending_since = now
        runtime.save_records(payload, records)
        return {
            "status": "ok",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": action,
        }
    # The worker head's vitality, observed and acted on exactly as the report wait does.
    # A reduction failure or an unobservable head degrades to None here, which leaves
    # this method on its ordinary path: the gate ceiling remains the outer bound for a
    # CI rollup nobody can see through, and no head is touched on a maybe.
    vitality = _worker_vitality_for_gate(runtime, task, record, records, payload)
    if vitality is not None:
        episode = vitality
        if episode.verdict is VitalityVerdict.RETAINED:
            # The worker is stopped because THIS card stopped it on `report:done`, and it
            # stays stopped until the gate's own verdict resumes or replaces it. Running the
            # recovery ladder here is what woke retained workers out from under a pending CI
            # rollup and then, on the red, cost them their session -- the reviewer's
            # `confirm_worker_retained` found the head running and the continuation fell back
            # to `replacement` (secretary-1539, codegen-orchestrator-1248). The gate's own
            # ceiling below is untouched: it bounds the CI rollup, not the head.
            pass
        elif episode.verdict is VitalityVerdict.SUSPENDED:
            return _execute_recovery_intent(runtime, 
                task,
                record,
                records,
                payload,
                attempt_id,
                episode=episode,
                kind="worker",
                now=now,
            )
        else:
            # Any other verdict still rides the policy once: a deterministic refusal on
            # file escalates fast even mid-gate, and a recovered suspension resets its rung.
            outcome = _recovery_policy_outcome(runtime, 
                task,
                record,
                records,
                payload,
                attempt_id,
                episode=episode,
                kind="worker",
                now=now,
            )
            if outcome is not None:
                return outcome
    if now - record.gate_pending_since <= GATE_PENDING_STALL_SECONDS:
        return {
            "status": "ok",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": action,
        }
    runtime.host.stop(record)
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="blocked",
        reason=(
            f"Mechanical gate: {scrub_host_output(result.summary)}. CI has been hanging with "
            f"no terminal result for longer than the threshold "
            f"({GATE_PENDING_STALL_SECONDS}s). Card moved to Blocked for a human."
        ),
        request_id=_attempt_request_id(record.attempt_id or attempt_id, f"{action}-stall", ref),
        terminal_state="blocked",
        disposition="blocked",
        blocked_reason="gate",
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {"status": "ok", "step": step, "pilot_ref": ref, "attempt_id": attempt_id, "to": "blocked"}


def _worker_vitality_for_gate(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> Any:
    """Reduce this tick's worker-head vitality while the card waits on the gate.

    The gate-pending path never called ``_wait_watchdog``, so it never built the status
    shape the reduction consumes; asking the host directly would duplicate
    ``command_terminal_status``. Instead the same seam the wait tick uses
    (``host.worker_status``) is probed here, guarded so ANY failure degrades to
    ``None`` -- the gate must keep working over an unobservable head exactly as it did
    before this card, with the gate ceiling as the outer bound. The returned episode
    (persisted by ``_reduce_and_store_vitality_episode``) is what the caller feeds the
    policy; the verdict table itself stays owned by the caller.
    """
    if getattr(runtime.host, "mode", "real") == "noop":
        return None
    try:
        status = runtime.host.worker_status(task, record)
    except Exception:  # noqa: BLE001 - a blind probe must never break the gate tick
        return None
    if not isinstance(status, dict) or (
        not isinstance(status.get("pid_status"), dict)
        and not isinstance(status.get("provider_progress"), dict)
        and "idle" not in status
    ):
        # Nothing was observed: no honest episode exists for this tick.
        return record.worker_vitality_episode
    try:
        return _reduce_and_store_vitality_episode(runtime, 
            task,
            record,
            records,
            payload,
            status,
            kind="worker",
            now=time.time(),
        )
    except Exception:  # noqa: BLE001 - shadow-mode failure degrades to no episode
        return None
