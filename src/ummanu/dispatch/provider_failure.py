"""A head whose first turn ended on a provider error is a provider verdict, not a stall (secretary-1799).

On 2026-09-25 a Codex reviewer's first turn ended 35 s after launch with
`task_complete.error = "unexpected status 401 Unauthorized: Incorrect API key provided"`. Nothing read
it: the wait tick aged the idle head into `review-stall-suspected`, confirmed the stall eleven
minutes later, respawned the reviewer into the same dead provider (the same 401 in 28 s) and blocked
the card for the operator, while the reviewer's fallback head on another provider was healthy.

This module is the verdict that path was missing, and it runs ahead of the stall path on every wait
tick (`wait_vitality.wait_watchdog`):

* **Detection** (`provider_failure_for_run`, run-bound and read-only): the head's own session says
  its first turn ended on a provider error -- 401/403, 429, 5xx, or a connection its client gave up
  on -- and the head produced no report or verdict (the wait tick only runs while it owes one). For
  Codex the source is the run's bound rollout journal, for Claude the run's bound session transcript
  and, when no transcript can be bound, the text at the bottom of the head's PTY screen. Only the
  first turn: a head that has completed a clean turn is out of scope and keeps today's path.
* **Handling** (`provider_failure_outcome`), in this order: the head's resource is recorded
  `unavailable` in `resource_health.json` (replacing its cached probe verdict), the head is stopped,
  the same role is relaunched on the next launchable head of the card's chain (`resolve_head_chain`
  from the card's head override or the role default), and one card comment plus the tick outcome
  name the head, the resource, the error and the head switched to. No round, respawn, red-review
  count or sprint budget is charged, and nothing here moves a card to Blocked.
* **Empty chain**: a worker's card goes back to Ready with `provider unavailable: <resource>` and
  is claimed again once a head of its chain is launchable (the claim-time walk decides that); a
  reviewer's card stays in Validate with no reviewer and the same visible reason, and
  `review.start_review` launches the reviewer once a chain head is launchable. The worker's
  candidate, gate receipt and report stay as they are.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ummanu.dispatch.helpers import safe_one_line
from ummanu.dispatch.launch import STAGE_RESPAWN, WORKER_ROLE
from ummanu.dispatch.launch import clear_launch_intent as _clear_launch_intent
from ummanu.dispatch.launch import launch_intent_unwritable as _launch_intent_unwritable
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.state import attempt_request_id as _attempt_request_id
from ummanu.dispatch.types import STOPPED_BY_PROVIDER_FAILURE, HostError
from ummanu.dispatch.watchdog import reset_idle as _reset_idle
from ummanu.head_health import HeadChoice
from ummanu.runtime.head import HeadRun, HeadRunError
from ummanu.runtime.provider_errors import (
    ProviderError,
    claude_first_turn_failure,
    codex_first_turn_failure,
    screen_turn_failure,
)

#: The action token of a worker card sent back to Ready because no head of its chain can run. The
#: sprint budget reads it off the move's request id and charges nothing for it
#: (`production._budget_event_type`): the card is waiting for a provider, not being restarted.
PROVIDER_UNAVAILABLE_READY_ACTION = "provider-unavailable-ready"
#: The status a detected failure records on the head's resource.
PROVIDER_FAILURE_STATUS = "unavailable"


def is_provider_unavailable_return(request_id: str) -> bool:
    """Whether a Ready move was this module's empty-chain return, read back off its request id."""
    return f"-{PROVIDER_UNAVAILABLE_READY_ACTION}-" in f"{request_id}-"


# ----------------------------------------------------------------------------------------------
# Detection: read-only, bound to the exact HeadRun.
# ----------------------------------------------------------------------------------------------


def _none(reason: str = "") -> dict[str, Any]:
    return {"state": "none", **({"reason": reason} if reason else {})}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"state": "unavailable", "reason": reason}


def _failed(run: HeadRun, error: ProviderError) -> dict[str, Any]:
    return {
        "state": "failed",
        "run_id": run.run_id,
        "head": run.spec.profile_id,
        "adapter": run.spec.adapter,
        "resource": run.spec.resource or "",
        "error": error.to_json(),
    }


def provider_failure_for_run(run: HeadRun, *, local_pty_root: Path | None = None) -> dict[str, Any]:
    """Whether this exact run's first turn ended on a provider error. Never raises, writes nothing.

    `{"state": "failed", ...}` carries the classified, secret-free error; `none` is a source that
    answered and shows no such failure; `unavailable` is a source that could not answer, which is
    no evidence either way.
    """
    # Imported here: `tui` owns the run-bound source verification and imports the dispatcher's
    # lifecycle modules, which import this one.
    from ummanu.dispatch.tui import _claude_bound_source, _codex_bound_source

    adapter = run.spec.adapter
    if adapter == "codex":
        bound = _codex_bound_source(run)
        if isinstance(bound, dict):
            return _unavailable(str(bound.get("reason") or "Codex provider source is unavailable"))
        _source, _path, _stat, lines = bound
        if not any('"task_complete"' in line.raw for line in lines):
            return _none("no turn has completed yet")
        failure = codex_first_turn_failure(line.event for line in lines)
        return _failed(run, failure) if failure is not None else _none()
    if adapter == "claude":
        transcript = _claude_bound_source(run)
        if isinstance(transcript, dict):
            return _screen_failure(run, local_pty_root, str(transcript.get("reason") or ""))
        _source, path, _stat = transcript
        try:
            data = path.read_bytes()
        except OSError:
            return _screen_failure(run, local_pty_root, "Claude session transcript cannot be read")
        # The error record is the rare case; a transcript that holds none is answered without
        # parsing the rest of it.
        if b'"isApiErrorMessage":true' not in data and b'"isApiErrorMessage": true' not in data:
            return _none()
        failure = claude_first_turn_failure(_json_lines(data))
        return _failed(run, failure) if failure is not None else _none()
    return _unavailable(f"adapter {adapter!r} has no provider-failure source")


def _json_lines(data: bytes) -> list[Any]:
    records: list[Any] = []
    for line in data.splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


#: How long the screen read may wait on the head's supervisor before it counts as no answer.
_SCREEN_READ_TIMEOUT_SECONDS = 2.0


def _screen_failure(run: HeadRun, local_pty_root: Path | None, why: str) -> dict[str, Any]:
    """The PTY fallback for a Claude head with no readable transcript.

    Asked only of a local-pty head whose supervisor journal says its first turn is over (idle, turn
    1): the screen of a head still at work, or already on a later turn, answers nothing. The screen
    itself is read through the local-pty backend, the one module that reaches its substrate.
    """
    from ummanu.runtime.local_pty_head import head_run_screen_lines, head_run_turn_reading

    if local_pty_root is None:
        return _unavailable(why or "no session transcript and no PTY to read")
    reading = head_run_turn_reading(local_pty_root, run.run_id)
    if str(reading.get("state") or "") != "observed":
        return _unavailable(why or str(reading.get("reason") or "supervisor journal is unavailable"))
    if reading.get("turn") != "idle" or int(reading.get("turn_number") or 0) != 1:
        return _none("the head is not idle after its first turn")
    screen = head_run_screen_lines(local_pty_root, run.run_id, timeout=_SCREEN_READ_TIMEOUT_SECONDS)
    if str(screen.get("state") or "") != "observed":
        return _unavailable(str(screen.get("reason") or "the head's screen could not be read"))
    failure = screen_turn_failure(screen.get("lines") or [])
    return _failed(run, failure) if failure is not None else _none()


def provider_failure_for_persisted_run(run: Any, *, local_pty_root: Path | None = None) -> dict[str, Any]:
    """The same reading for a record's persisted HeadRun payload."""
    try:
        head_run = HeadRun.from_json(run)
    except (HeadRunError, AttributeError, KeyError, TypeError, ValueError):
        return _unavailable("persisted HeadRun is unavailable")
    try:
        return provider_failure_for_run(head_run, local_pty_root=local_pty_root)
    except Exception as exc:  # noqa: BLE001 - detection is advisory; a reader fault is no answer
        return _unavailable(f"provider-failure reader failed ({type(exc).__name__})")


# ----------------------------------------------------------------------------------------------
# Handling: the wait tick's provider verdict.
# ----------------------------------------------------------------------------------------------


def provider_failure_outcome(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    kind: str,
) -> dict[str, Any] | None:
    """This tick's provider-failure outcome for the head the wait watches, or None to carry on.

    None whenever the host has no reader, the reader could not answer, or it answered about
    another run: the ordinary wait path then decides exactly as before.
    """
    probe = getattr(runtime.host, "provider_failure", None)
    if not callable(probe):
        return None
    run_payload = record.review_head_run if kind == "review" else record.worker_head_run
    run_id = str(run_payload.get("run_id") or "")
    if not run_id:
        return None
    try:
        reading = probe(task, record, kind)
    except Exception:  # noqa: BLE001 - an unreadable source is no verdict; the wait path decides
        return None
    if not isinstance(reading, dict) or reading.get("state") != "failed":
        return None
    if str(reading.get("run_id") or run_id) != run_id:
        return None
    return _fall_back(runtime, task, record, records, payload, attempt_id, kind=kind, reading=reading)


def _head_resource(runtime: Any, head: str) -> str:
    try:
        return str(runtime.catalog.head_profile(head).get("resource") or "")
    except (AttributeError, HostError, KeyError, TypeError, ValueError):
        return ""


def resolve_role_chain(runtime: Any, task: dict[str, Any], *, kind: str) -> HeadChoice:
    """The role's head, walked from the card's override or the role default over its chain."""
    from ummanu.dispatch.claim import resolve_head

    preferred = runtime.catalog.review_head(task) if kind == "review" else runtime.catalog.worker_head(task)
    return resolve_head(runtime, preferred)


def _fall_back(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    kind: str,
    reading: dict[str, Any],
) -> dict[str, Any]:
    ref = task["ref"]
    review = kind == "review"
    step = "review" if review else "advance"
    role = "reviewer" if review else "worker"
    head = record.review_head if review else record.head
    raw_error = reading.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    summary = safe_one_line(error.get("summary") or error.get("kind") or "provider error", limit=200)
    resource = str(reading.get("resource") or "") or _head_resource(runtime, head)
    failed_run = str(reading.get("run_id") or "")
    base = {"step": step, "pilot_ref": ref, "attempt_id": attempt_id}
    evidence = {
        "head": head,
        "resource": resource,
        "error": summary,
        "error_kind": str(error.get("kind") or ""),
        "error_status": error.get("status"),
        "error_source": str(error.get("source") or ""),
        "failed_run_id": failed_run,
    }

    # 1. The resource is recorded unavailable, replacing its cached probe verdict, so the walk
    # below and every claim within the TTL goes past it. Without that write the walk would hand
    # the role straight back to the provider that just refused it, so a refused write ends the tick
    # and the next one tries again from the same evidence.
    if resource:
        try:
            runtime.head_health.record(
                resource,
                PROVIDER_FAILURE_STATUS,
                f"provider error on the first turn of {role} head {head}: {summary}",
            )
        except Exception as exc:  # noqa: BLE001 - the cache writer has no narrower contract
            return {
                **base,
                "status": "degraded",
                "action": f"{kind}-provider-failure-unrecorded",
                **evidence,
                "reason": f"resource health could not be written ({type(exc).__name__}); retried next tick",
            }

    # 2. The head is stopped before anything replaces it.
    if review:
        unconfirmed = runtime._end_review_pane_confirmed(
            record,
            records,
            payload,
            ref,
            step=step,
            attempt_id=attempt_id,
            initiator=STOPPED_BY_PROVIDER_FAILURE,
        )
    else:
        unconfirmed = runtime._stop_worker_confirmed(record, ref, step=step, attempt_id=attempt_id)
    if unconfirmed is not None:
        return unconfirmed

    # 3. The same role, on the next launchable head of the card's chain.
    choice = resolve_role_chain(runtime, task, kind=kind)
    if choice.resolved and choice.head == head:
        # The walk can only land here when the head's resource could not be named or recorded, so
        # no resource verdict stands between it and the provider that refused it.
        choice = HeadChoice(choice.preferred, "", choice.readiness, choice.rejected)
    if not choice.resolved:
        return _empty_chain(runtime, task, record, records, payload, attempt_id, kind=kind, evidence=evidence)
    if review:
        return _relaunch_reviewer(runtime, task, record, records, payload, attempt_id, choice, evidence)
    return _relaunch_worker(runtime, task, record, records, payload, attempt_id, choice, evidence)


def _comment(
    runtime: Any, ref: str, record: DispatcherRecord, attempt_id: str, action: str, run_id: str, body: str
) -> None:
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=body,
        request_id=_attempt_request_id(record.attempt_id or attempt_id, action, ref, run_id),
    )


def _failure_sentence(role: str, evidence: dict[str, Any]) -> str:
    return (
        f"Provider failure ({role}): head {evidence['head']} ended its first turn on a provider error "
        f"from {evidence['resource'] or '(unnamed resource)'}: {evidence['error']}. The resource is "
        f"recorded `{PROVIDER_FAILURE_STATUS}` and the head was stopped."
    )


def _relaunch_reviewer(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    choice: HeadChoice,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    from ummanu.dispatch.review import start_review

    ref = task["ref"]
    record.review_head = choice.head
    record.preferred_review_head = choice.preferred if choice.substituted else ""
    record.review_provider_hold = ""
    record.state = "review_starting"
    outcome = start_review(
        runtime, task, records, record, attempt_id, action="review-provider-fallback", payload=payload
    )
    if outcome.get("status") != "ok":
        runtime.save_records(payload, records)
        return outcome
    now = time.time()
    record.review_waiting_since = now
    _reset_idle(record, "review")
    records[ref] = record
    runtime.save_records(payload, records)
    _comment(
        runtime,
        ref,
        record,
        attempt_id,
        "review-provider-fallback",
        evidence["failed_run_id"],
        _failure_sentence("reviewer", evidence)
        + f" The review was relaunched on {choice.head} ({choice.reason}). No round, respawn or "
        "budget was charged.",
    )
    return {
        **outcome,
        "action": "review-provider-fallback",
        **evidence,
        "switched_to": choice.head,
        "reason": f"{evidence['head']} failed on its provider ({evidence['error']}); relaunched on {choice.head}",
    }


def _relaunch_worker(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    choice: HeadChoice,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    from ummanu.dispatch.worker_launch import bring_up_worker_head, write_worker_relaunch_intent

    ref = task["ref"]
    step = "advance"
    record.head = choice.head
    record.preferred_head = choice.preferred if choice.substituted else ""
    failure = write_worker_relaunch_intent(
        runtime, payload, records, ref, record, action="worker-provider-fallback", task=task
    )
    if failure is not None:
        return _launch_intent_unwritable(
            step=step, ref=ref, attempt_id=record.attempt_id or attempt_id, role=WORKER_ROLE, reason=failure
        )
    launched, failed = bring_up_worker_head(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        step=step,
        stage=STAGE_RESPAWN,
        blocked_reason="worker provider fallback failed",
        blocked_action="worker-provider-fallback-blocked",
        blocked_request_suffix=evidence["failed_run_id"],
    )
    if launched is None:
        assert failed is not None
        return failed
    now = time.time()
    record.state = "claimed"
    # The replacement head never saw a bounced report, so it owes no answer for one.
    record.worker_answer_owed_since = 0.0
    runtime.record_worker_routing(task, record, launched.run)
    _clear_launch_intent(record)
    record.worker_started_at = record.worker_progress_at = now
    record.worker_waiting_since = now
    _reset_idle(record, "worker")
    records[ref] = record
    runtime.save_records(payload, records)
    _comment(
        runtime,
        ref,
        record,
        attempt_id,
        "worker-provider-fallback",
        evidence["failed_run_id"],
        _failure_sentence("worker", evidence)
        + f" The worker was relaunched on {choice.head} ({choice.reason}) with the same TASK.md: the "
        f"report round stays generation {record.report_generation}. No round, respawn or budget was "
        "charged.",
    )
    return {
        "status": "ok",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": "worker-provider-fallback",
        **evidence,
        "switched_to": choice.head,
        "reason": f"{evidence['head']} failed on its provider ({evidence['error']}); relaunched on {choice.head}",
    }


def provider_unavailable_reason(resource: str) -> str:
    """The visible reason a card waits for a provider, in the words the card and the tick share."""
    return f"provider unavailable: {resource or '(unnamed resource)'}"


def _empty_chain(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    kind: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    ref = task["ref"]
    reason = provider_unavailable_reason(evidence["resource"])
    if kind == "review":
        # Validate, no reviewer, the candidate untouched. `review_starting` is the one state whose
        # recovery launches a reviewer and only a reviewer; the hold tells `start_review` to walk
        # the chain again on each tick instead of spending the infrastructure retries.
        record.review_provider_hold = f"{reason} ({evidence['head']}: {evidence['error']})"
        record.state = "review_starting"
        record.review_started_at = 0.0
        record.review_waiting_since = 0.0
        _reset_idle(record, "review")
        records[ref] = record
        runtime.save_records(payload, records)
        _comment(
            runtime,
            ref,
            record,
            attempt_id,
            "review-provider-unavailable",
            evidence["failed_run_id"],
            _failure_sentence("reviewer", evidence)
            + f" No head of the reviewer's fallback chain can be launched, so the card stays in "
            f"Validate with no reviewer ({reason}); the reviewer is launched as soon as a head of its "
            "chain is launchable. The candidate and its gate receipt are kept.",
        )
        return {
            "status": "degraded",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "review-provider-unavailable",
            **evidence,
            "switched_to": "",
            "reason": reason,
        }
    runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        target="ready",
        reason=(
            f"{reason}: worker head {evidence['head']} ended its first turn on a provider error "
            f"({evidence['error']}) and no head of its fallback chain can be launched. The card is "
            "claimed again once one can."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id, PROVIDER_UNAVAILABLE_READY_ACTION, ref, evidence["failed_run_id"]
        ),
    )
    # The record stays: the stopped worker's checkout is the one the next claim reuses, and
    # reconciliation keeps a settled record for a card in Ready for exactly that.
    record.state = "claimed"
    _clear_launch_intent(record)
    records[ref] = record
    runtime.save_records(payload, records)
    return {
        "status": "degraded",
        "step": "advance",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": "worker-provider-unavailable",
        **evidence,
        "switched_to": "",
        "to": "ready",
        "reason": reason,
    }


def review_hold_choice(runtime: Any, task: dict[str, Any], record: DispatcherRecord) -> HeadChoice | None:
    """For a reviewer held on an empty chain, the head it may launch on now; None while none can.

    Walked again on every attempt, so a resource that recovers (its recorded verdict ages out and
    its probe answers) is found without anyone moving the card.
    """
    choice = resolve_role_chain(runtime, task, kind="review")
    return choice if choice.resolved else None
