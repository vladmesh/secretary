"""The e2e stage: an adapter-declared GitHub workflow run on a code card's candidate (secretary-1795).

Placement. For a `code` card of a project whose adapter declares `validation.e2e` (`dispatch/e2e.py`),
the stage runs on the exact SHA that just passed the merge gate, and before the card becomes
releasable: in `review_verdict.park_green_verdict`, after the green review verdict (or right after
green CI when the card's review is `skipped`) and before the park in Assessment or the release; and
again in the release audit (`release_lifecycle.release_parked`), where a SHA that already has a green
run is not dispatched again. A red review never reaches it, so rework rounds spend no runs.

Dispatch, exactly once per candidate SHA. The card's `e2e` field (`board/e2e_record.py`) is the
stage's record. A run record is written as an intent (card, SHA, dispatch id) before the
`workflow_dispatch` call, which asks GitHub for the run it starts (`return_run_details`); the run id in
the answer is recorded right after it, and the run's `head_sha` is checked against the candidate. A
record that exists for the SHA is continued, never dispatched again: after a crash between the call and
that record the run is looked up by event, branch, SHA and creation time (`e2e.matching_runs`); several
matches Block the card with the candidates listed, and none within the identification window Blocks
it too. A dispatch GitHub refuses Blocks the card with GitHub's answer.

Wait. Once the run is identified the dispatcher creates a `wait` card for it (`dispatch/wait_cards.py`),
in the code card's sprint, with the adapter's deadline and the return address `card:<ref>`, under a
request id derived from the dispatch id, so a repeat after a crash is the same card. There is no
other poller: while the wait card waits, this stage only reads its frozen result. The merge gate is
read before the dispatch and accepted once, after the stage is green, by the caller; it is not re-read
while a run is underway.

Outcomes, read off the wait's frozen result:

- conclusion `success`: the stage is green for this SHA; the card proceeds (Assessment or release);
- conclusion `failure`: rework, like a red gate (`gate_lifecycle.gate_red_to_worker`), with the run
  URL, the failed jobs and steps and the gate's bounded `--log-failed` fragment; in the release audit,
  where no rework round is open, Blocked with the same evidence;
- any other conclusion, and every wait outcome other than `target_reached`: Blocked, classified as
  infrastructure (`blocked_reason: infrastructure`), with the outcome and the link.

A run whose result Blocked the card records that move's request id; once it is committed the run's
pass is over, and a card brought back to the same SHA may spend a new run. Every run counts towards
the interim cap of :data:`E2E_RUN_CAP` per card; at the cap the stage does not start and the card is
Blocked with `e2e run cap reached (3)`.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from secretary.board import e2e_record, wait_card
from secretary.board.completion_evidence import has_candidate
from secretary.board.e2e_record import E2E_RUN_CAP, FAILURE, REFUSED, SENT, SUCCESS, E2eRun, E2eState
from secretary.dispatch import attempt_accounting
from secretary.dispatch.e2e import (
    E2E_IDENTIFY_SECONDS,
    DispatchRefused,
    E2eDeclaration,
    declared_e2e,
    dispatch_workflow,
    matching_runs,
    red_evidence,
    run_head_sha,
)
from secretary.dispatch.gate import GateResult, _name_with_owner
from secretary.dispatch.gate import _fingerprint as _gate_fingerprint
from secretary.dispatch.helpers import _legacy_worker_branch, safe_one_line, scrub_host_output
from secretary.dispatch.state import DispatcherRecord, request_token
from secretary.dispatch.state import attempt_request_id as _attempt_request_id
from secretary.dispatch.types import HostError
from secretary.tasks import TaskError

#: The phase a red run's rework is opened under: its request id carries `gate-red`, so the sprint
#: charges it as the red CI it is.
E2E_PHASE = "e2e-gate"
E2E_FAILURE_REASON = "e2e-failure"


def utcnow() -> datetime:
    """The stage's clock; tests replace it."""
    return datetime.now(UTC)


def applies(task: dict[str, Any]) -> bool:
    """A code card with a candidate; a card of unknown kind reads as code, as everywhere else."""
    return has_candidate(task) and str(task.get("type") or "code") == "code"


def stage_request_id(action: str, dispatch_id: str) -> str:
    """A request id bound to one run (its dispatch id names the card): the same after any crash,
    restart or lost dispatcher record."""
    return "-".join(request_token(part) for part in ("dispatcher", action, dispatch_id))


def run_stage(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    step: str,
    gate: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any] | None:
    """None when this card may proceed (no e2e declared, or a green run on this SHA), else the outcome.

    `gate` reads the merge gate without accepting it (None when it is green); it is asked once, before
    a new run is dispatched, so the run is dispatched on a SHA that passed it. A run already underway
    for this SHA is advanced without asking it: the SHA passed it at the dispatch, and the caller
    accepts the gate once, after the stage is green. The release audit passes none: the parked SHA
    passed it before the park, and the release reads it after the stage.
    """
    if not applies(task):
        return None
    ref = task["ref"]
    try:
        declaration = declared_e2e(runtime.host, str(task.get("project") or ""))
    except HostError as exc:
        return _block(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "e2e-declaration-blocked", ref),
            reason=f"The e2e stage cannot read this project's e2e declaration: {scrub_host_output(str(exc))}",
            step=step,
            outcome="e2e declaration unreadable",
            blocked_reason="gate",
        )
    if declaration is None:
        return None
    sha = runtime.host.head_commit(record)
    state = e2e_record.e2e_state(task)
    green = state.green(sha)
    if green is not None:
        _comment_green(runtime, task, state, green)
        return None
    run = state.latest(sha)
    if run is not None and run.closing and runtime.audit.committed_event(run.closing) is not None:
        # That run's pass ended Blocked, and the card is back on the same SHA: a new run may be spent.
        run = None
    if run is None:
        if gate is not None:
            gated = gate()
            if gated is not None:
                return gated
        if state.dispatched >= E2E_RUN_CAP:
            return _block(
                runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                request_id=_attempt_request_id(record.attempt_id or attempt_id, "e2e-cap-blocked", ref, sha),
                reason=(
                    f"e2e run cap reached ({E2E_RUN_CAP}): this card has dispatched {state.dispatched} e2e "
                    f"runs across its candidates, so none is dispatched for `{sha[:12]}`. Until the sprint "
                    "e2e budget exists, a card spends at most "
                    f"{E2E_RUN_CAP}; `task show` lists the runs."
                ),
                step=step,
                outcome=f"e2e run cap reached ({E2E_RUN_CAP})",
                blocked_reason="other",
            )
        started = _dispatch(runtime, task, record, attempt_id, state, declaration, sha, step=step)
        if isinstance(started, dict):
            return started
        run = started
    return _advance(runtime, task, record, records, payload, attempt_id, state, run, declaration, step=step)


# --- dispatch and identification -----------------------------------------------------------------


def _persist(runtime: Any, ref: str, state: E2eState) -> None:
    runtime.writer.record_e2e_state(role="dispatcher", actor=runtime.owner, reference=ref, state=state.text())


def _dispatch(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    attempt_id: str,
    state: E2eState,
    declaration: E2eDeclaration,
    sha: str,
    *,
    step: str,
) -> E2eRun | dict[str, Any]:
    """Write the intent, then dispatch once. The run record, or the outcome of a transport retry."""
    ref = task["ref"]
    try:
        repo = _name_with_owner(runtime.host, record.workspace)
    except HostError as exc:
        # Nothing is written and nothing dispatched yet: the next tick asks again.
        return {
            **_outcome(ref, attempt_id, "e2e-dispatching", step=step, sha=sha),
            "status": "degraded",
            "reason": f"the e2e stage could not name the repository: {scrub_host_output(str(exc))}",
        }
    run = E2eRun(
        dispatch_id=f"{ref}-e2e-{len(state.runs) + 1}-{secrets.token_hex(4)}",
        sha=sha,
        repo=repo,
        branch=_legacy_worker_branch(ref),
        workflow=declaration.workflow,
        intent_at=wait_card.utc_text(utcnow()),
        deadline=declaration.deadline,
    )
    state.runs.append(run)
    # The intent is on the card before anything reaches GitHub.
    _persist(runtime, ref, state)
    try:
        dispatched = dispatch_workflow(
            runtime.host, repo, declaration, branch=run.branch, dispatch_id=run.dispatch_id, sha=sha
        )
    except DispatchRefused as exc:
        run.dispatch = REFUSED
        run.dispatch_detail = safe_one_line(scrub_host_output(str(exc)), limit=1000)
        _close(
            run,
            f"The e2e workflow `{run.workflow}` could not be dispatched on `{run.branch}` @ `{sha[:12]}`: "
            f"{run.dispatch_detail}. No run started and none was charged to a worker round; the "
            "workflow, its `workflow_dispatch` trigger or the dispatcher's access has to be repaired.",
        )
    except HostError as exc:
        # No answer, or a rate limit: GitHub may or may not have taken it. The run is looked up from
        # here on, exactly as after a crash; it is never dispatched again.
        run.dispatch_detail = f"unconfirmed: {safe_one_line(scrub_host_output(str(exc)), limit=500)}"
    else:
        run.dispatch = SENT
        # GitHub's own answer names the run; an answer without one is looked up like a crash.
        if dispatched.run_id:
            run.run_id = dispatched.run_id
            run.run_url = f"https://github.com/{repo}/actions/runs/{dispatched.run_id}"
    _persist(runtime, ref, state)
    return run


def _identify(
    runtime: Any,
    task: dict[str, Any],
    attempt_id: str,
    state: E2eState,
    run: E2eRun,
    declaration: E2eDeclaration,
    *,
    step: str,
) -> dict[str, Any] | None:
    """Name the run and check its SHA; None once both are on the record, else the tick's outcome.

    A run GitHub's dispatch answer named is only checked (`head_sha`). One that no answer named (a crash
    after the POST, no answer, an answer without details) is looked up by event, branch, SHA and
    creation time. More than one match is never guessed: the card is Blocked with every candidate.
    """
    ref = task["ref"]
    error = ""
    try:
        if run.run_id:
            head_sha = run_head_sha(runtime.host, run.repo, run.run_id)
        else:
            found = matching_runs(
                runtime.host,
                run.repo,
                run.workflow,
                branch=run.branch,
                sha=run.sha,
                since=wait_card.parse_utc(run.intent_at, "intent_at"),
                dispatch_id=run.dispatch_id if declaration.dispatch_id_input else "",
            )
            if len(found) > 1:
                listed = ", ".join(
                    f"{candidate.get('html_url') or candidate['id']} (created {candidate.get('created_at')})"
                    for candidate in sorted(found, key=lambda candidate: int(candidate["id"]))
                )
                _close(
                    run,
                    f"The e2e run dispatched at {run.intent_at} for `{run.sha[:12]}` cannot be told apart: "
                    f"{len(found)} `{run.workflow}` workflow_dispatch runs on `{run.branch}` at that SHA "
                    f"were created since: {listed}. None is taken as this candidate's e2e result, and "
                    "nothing is dispatched again.",
                )
                _persist(runtime, ref, state)
                return None
            if found:
                run.run_id = int(found[0]["id"])
                run.run_url = f"https://github.com/{run.repo}/actions/runs/{run.run_id}"
            head_sha = str(found[0].get("head_sha") or "") if found else ""
    except HostError as exc:
        head_sha, error = "", safe_one_line(scrub_host_output(str(exc)), limit=500)
    if not head_sha:
        window_end = wait_card.parse_utc(run.intent_at, "intent_at") + timedelta(seconds=E2E_IDENTIFY_SECONDS)
        if utcnow() < window_end:
            return {
                **_outcome(ref, attempt_id, "e2e-identifying", step=step, sha=run.sha),
                "dispatch_id": run.dispatch_id,
                **({"run": run.run_url} if run.run_url else {}),
                **({"error": error} if error else {}),
            }
        what = (
            f"its run {run.run_url} could not be read"
            if run.run_id
            else f"no `{run.workflow}` workflow_dispatch run on `{run.branch}` at that SHA was found"
        )
        _close(
            run,
            f"The e2e run dispatched at {run.intent_at} for `{run.sha[:12]}` (dispatch id `{run.dispatch_id}`) "
            f"could not be identified: {what} within {E2E_IDENTIFY_SECONDS // 60} minutes"
            + (f" ({run.dispatch_detail})" if run.dispatch_detail else "")
            + (f"; last error: {error}" if error else "")
            + ". Nothing was dispatched a second time.",
        )
        _persist(runtime, ref, state)
        return None
    run.head_sha = head_sha
    if head_sha != run.sha:
        _close(
            run,
            f"The e2e run {run.run_url} ran on `{head_sha[:12]}`, not on the candidate `{run.sha[:12]}`: "
            f"`{run.branch}` moved between the dispatch and the run. It is not accepted as this "
            "candidate's e2e result.",
        )
    _persist(runtime, ref, state)
    return None


def _create_wait(runtime: Any, task: dict[str, Any], state: E2eState, run: E2eRun) -> None:
    """The wait card for this run, created once: its request id is derived from the dispatch id."""
    ref = task["ref"]
    created = runtime.writer.create(
        role="dispatcher",
        actor=runtime.owner,
        project=str(task.get("project") or ""),
        task_type="wait",
        title=f"E2E run {run.workflow} for {ref} @ {run.sha[:12]}",
        description=(
            f"The dispatcher's e2e stage waits here for the `{run.workflow}` run it dispatched on "
            f"`{run.branch}` @ `{run.sha}` for {ref} (dispatch id `{run.dispatch_id}`). Its result "
            f"returns to {ref}, whose e2e record names this card."
        ),
        target="ready",
        sprint=str(task.get("sprint") or ""),
        wait={
            "run": run.run_url,
            "deadline": run.deadline,
            "returns": [wait_card.CARD_PREFIX + ref],
        },
        request_id=stage_request_id("e2e-wait", run.dispatch_id),
    )
    run.wait_ref = str(created["task"]["ref"])
    _persist(runtime, ref, state)


def _advance(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    state: E2eState,
    run: E2eRun,
    declaration: E2eDeclaration,
    *,
    step: str,
) -> dict[str, Any] | None:
    ref = task["ref"]
    if not run.closing and run.result is None:
        if not run.run_id or not run.head_sha:
            pending = _identify(runtime, task, attempt_id, state, run, declaration, step=step)
            if pending is not None:
                return pending
        if not run.closing and not run.wait_ref:
            try:
                _create_wait(runtime, task, state, run)
            except TaskError as exc:
                _close(
                    run,
                    f"The e2e run {run.run_url} was dispatched, but its wait card could not be created: "
                    f"{exc.code}: {exc.message}",
                )
                _persist(runtime, ref, state)
        if not run.closing:
            try:
                wait = runtime.reader.show(run.wait_ref)
            except TaskError as exc:
                if exc.code != "not_found":
                    raise
                _close(run, f"The wait card {run.wait_ref} of the e2e run {run.run_url} no longer exists.")
                _persist(runtime, ref, state)
            else:
                result = wait_card.wait_state(wait).result
                if result is None:
                    return _waiting(task, run, attempt_id, step=step, state=state, wait=wait)
                fact = result.get("fact") if isinstance(result.get("fact"), dict) else {}
                run.result = {
                    "outcome": str(result.get("outcome") or ""),
                    "conclusion": str(fact.get("conclusion") or ""),
                    "summary": str(result.get("summary") or ""),
                    "evidence": str(result.get("evidence") or run.run_url),
                    "key": str(result.get("key") or ""),
                }
                _persist(runtime, ref, state)
    if run.closing:
        return _block_run(runtime, task, record, records, payload, attempt_id, run, step=step)
    return _act(runtime, task, record, records, payload, attempt_id, state, run, step=step)


# --- outcomes ------------------------------------------------------------------------------------


def _act(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    state: E2eState,
    run: E2eRun,
    *,
    step: str,
) -> dict[str, Any] | None:
    """What the wait's frozen result does to the card."""
    ref = task["ref"]
    result = run.result or {}
    outcome = result.get("outcome") or ""
    conclusion = run.conclusion
    if outcome == wait_card.TARGET_REACHED and conclusion == SUCCESS:
        _comment_green(runtime, task, state, run)
        return None
    if outcome == wait_card.TARGET_REACHED and conclusion == FAILURE:
        summary, log, fingerprint = _red_evidence(runtime, run)
        if step == "assessment":
            # A release decision opens no rework round: the card stops with the evidence on it.
            _close(
                run,
                f"Observer decision: release. The e2e stage is red: {summary}. The card is Blocked "
                f"rather than released.\nTail:\n```\n{log}\n```",
            )
            _persist(runtime, ref, state)
            return _block_run(runtime, task, record, records, payload, attempt_id, run, step=step)
        from secretary.dispatch.gate_lifecycle import gate_red_to_worker

        return gate_red_to_worker(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            GateResult(
                "red",
                summary,
                log,
                fingerprint=fingerprint,
                failure_class="substantive",
                failure_reason=E2E_FAILURE_REASON,
            ),
            phase=E2E_PHASE,
        )
    if outcome == wait_card.TARGET_REACHED:
        what = f"concluded {conclusion or 'with no conclusion'}"
    else:
        what = f"was not waited out: its wait card ended {outcome} ({result.get('summary') or ''})"
    _close(
        run,
        f"The e2e run {run.run_url or run.dispatch_id} on `{run.sha[:12]}` {what}. That is the e2e "
        f"infrastructure, not the candidate's code: the card is Blocked, and no worker round is "
        f"charged. Wait card: {run.wait_ref}. Evidence: {result.get('evidence') or run.run_url}.",
    )
    _persist(runtime, ref, state)
    return _block_run(runtime, task, record, records, payload, attempt_id, run, step=step)


def _red_evidence(runtime: Any, run: E2eRun) -> tuple[str, str, str]:
    """The red run as a gate verdict: `(summary, log fragment, fingerprint)`."""
    evidence = red_evidence(runtime.host, run.repo, run.run_id, run.run_url)
    failed = "; ".join(
        f"job «{safe_one_line(job) or '?'}»"
        + (", step " + ", ".join(f'"{safe_one_line(step)}"' for step in steps) if steps else "")
        for job, steps in evidence.jobs
    )
    summary = (
        f"e2e workflow `{run.workflow}` run {run.run_url} concluded failure on `{run.branch}` @ "
        f"`{run.sha[:12]}`; failed: {failed or 'no failed job was listed'}"
    )
    if evidence.note:
        summary += f" ({evidence.note})"
    fragment = evidence.fragment
    log = fragment.text if fragment.available else f"log unavailable: {fragment.reason}"
    first_job, first_steps = evidence.jobs[0] if evidence.jobs else ("", ())
    fingerprint = _gate_fingerprint(
        "e2e",
        run.workflow,
        first_job,
        fragment.step or ",".join(first_steps),
        fragment.text or fragment.reason,
    )
    return scrub_host_output(summary), scrub_host_output(log).strip(), fingerprint


def _comment_green(runtime: Any, task: dict[str, Any], state: E2eState, run: E2eRun) -> None:
    """The green result on the card, once per run: the park or the release that follows carries it."""
    ref = task["ref"]
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            "## E2E — green\n\n"
            f"The e2e workflow `{run.workflow}` run {run.run_url} concluded success on the candidate "
            f"`{run.sha}` (`{run.branch}`, dispatch id `{run.dispatch_id}`, wait card {run.wait_ref}). "
            f"Runs dispatched for this card: {state.dispatched} of {E2E_RUN_CAP}."
        ),
        request_id=stage_request_id("e2e-green", run.dispatch_id),
    )


def _close(run: E2eRun, reason: str) -> None:
    run.closing = stage_request_id("e2e-blocked", run.dispatch_id)
    run.closing_reason = reason


def _block_run(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    run: E2eRun,
    *,
    step: str,
) -> dict[str, Any]:
    """The Blocked move this run's record already names, with its recorded reason.

    A red run Blocked in the release audit is the candidate failing its check (`gate`); every other end
    of a run is the e2e infrastructure's.
    """
    return _block(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        request_id=run.closing,
        reason=run.closing_reason,
        step=step,
        outcome="e2e " + (run.status() if run.result or run.dispatch == REFUSED else "run unavailable"),
        blocked_reason="gate" if run.result is not None and run.conclusion == FAILURE else "infrastructure",
    )


def _block(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    request_id: str,
    reason: str,
    step: str,
    outcome: str,
    blocked_reason: str = "infrastructure",
) -> dict[str, Any]:
    """Blocked with the heads down, like every other merge-path block; a run's end is infrastructure.

    The cap (`other`) and an unreadable declaration (`gate`) are not the e2e infrastructure failing.
    """
    ref = task["ref"]
    runtime.host.stop(record)
    verdict = record.worker_continuation.verdict_outcome
    attempt_accounting.terminal_effect(
        runtime,
        task,
        record,
        target="blocked",
        reason=reason,
        request_id=request_id,
        terminal_state="blocked",
        disposition="blocked",
        verdict=verdict if verdict in {"green", "red", "blocked"} else "missing",
        blocked_reason=blocked_reason,
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {"status": "blocked", "step": step, "pilot_ref": ref, "reason": outcome}


def _waiting(
    task: dict[str, Any],
    run: E2eRun,
    attempt_id: str,
    *,
    step: str,
    state: E2eState,
    wait: dict[str, Any],
) -> dict[str, Any]:
    view = wait_card.wait_view(wait) or {}
    return {
        **_outcome(task["ref"], attempt_id, "e2e-waiting", step=step, sha=run.sha),
        "run": run.run_url,
        "wait_card": run.wait_ref,
        "deadline": view.get("deadline"),
        "observation": (view.get("last_observation") or {}).get("text"),
        "runs_dispatched": state.dispatched,
    }


def _outcome(ref: str, attempt_id: str, action: str, *, step: str, sha: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": action,
        "sha": sha,
    }


__all__ = [
    "E2E_FAILURE_REASON",
    "E2E_PHASE",
    "applies",
    "run_stage",
    "stage_request_id",
    "utcnow",
]
