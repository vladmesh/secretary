"""State helpers for the production dispatcher."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from secretary.dispatcher_types import DispatcherError
from secretary.dispatcher_worker_lifecycle import WorkerContinuation, WorkerReportNudge


# Every way a claim can answer "not this card". A claim-skip is about the card in front of the
# scan and says nothing about the ones behind it, so the Ready pass records it and moves on to the
# next card — halting the pass would let one unclaimable card stop work that has somewhere to go,
# which is the failure this family-failover work exists to remove, only at queue scale.
#
# This set is the registry, not a convenience: the Ready scan reads it rather than comparing
# against one action, so adding a new kind of claim-skip means adding it here and nowhere else. A
# skip that is missing from it does not degrade, it stops the tick's whole Ready pass.
CLAIM_SKIP_RESOURCE_NOT_READY = "resource-not-ready"
CLAIM_SKIP_FAILOVER_COLLAPSE = "failover-collapses-roles"
CLAIM_SKIP_ACTIONS = frozenset({
    CLAIM_SKIP_RESOURCE_NOT_READY,
    CLAIM_SKIP_FAILOVER_COLLAPSE,
})


def is_claim_skip(outcome: dict[str, Any]) -> bool:
    """Whether a claim outcome is "not this card, next card" rather than the pass's answer."""
    return str(outcome.get("action") or "") in CLAIM_SKIP_ACTIONS


@dataclass
class DispatcherRecord:
    worker: str
    workspace: str
    handle: str
    head: str
    review_head: str
    attempt_id: str
    comment_baseline: int
    review_baseline: int
    state: str
    claimed_at: float
    # The report round the worker currently in this checkout was handed (secretary-1061). It keys
    # the report request ids and the report body path in TASK.md, and nothing else: it is durable
    # here before any TASK.md is written, it advances by one whenever a new report round opens
    # (claim, red gate, red review, stale-done bounce) and never on a respawn inside a round. A
    # command from a round that is over therefore names that round and never records a report of
    # this one, instead of deduping this round's report into silence the way one shared id did.
    # `review_baseline` used to carry this as well as its own job of indexing review markers; the
    # two are separate values now because a comment count is not a round.
    # The head each role was preferred on when the claim had to leave that preference behind
    # (secretary-1165), empty when it did not. The claim walks the canon's fallback chain when the
    # preferred head's resource is red or spent, and `head`/`review_head` above then name another
    # family. Kept here because the preference is a claim-time fact: re-reading `role_defaults` at
    # document-build time would answer a different question, and an operator who repoints a role
    # mid-attempt would turn a faithful record into a false one.
    preferred_head: str = ""
    preferred_review_head: str = ""
    report_generation: int = 0
    # The observer decision that opened the round `report_generation` names, empty when no decision
    # opened it (secretary-1064). Frozen here with the generation, in the same write, because the
    # worker of the round must be handed the adjudication its round was opened on: reading "the
    # latest decision comment" at document-build time answers a different question, and a decision
    # recorded while the round runs would silently replace the instruction. Assigned, like the
    # generation, whenever a red transition opens a round, so a gate-red round carries no stale
    # decision from the review round before it.
    report_decision: str = ""
    # Mechanical validation gate (secretary-633): "" until the gate is green for the current code
    # state, then "green". Reset to "" on every fresh entry to validate so a reworked card re-runs
    # the gate instead of coasting on a stale pass. gate_pending_since stamps when a github CI
    # rollup first went non-terminal, driving the pending watchdog.
    gate_state: str = ""
    gate_pending_since: float = 0.0
    # SHA-bound result of the last green mechanical gate.  It is an evidence receipt, not a
    # cache key: release still re-runs the gate immediately before merge.
    gate_attestation: dict[str, Any] = field(default_factory=dict)
    # Consecutive times the gate backend failed to answer at all (secretary-1164), and the last
    # such failure. A transport failure decides nothing about the card, so it is counted here and
    # retried on the next tick; only the exhausted count blocks the card, naming the transport.
    # Both reset the moment any answer — green, red or pending — comes back.
    gate_transport_failures: int = 0
    gate_transport_error: str = ""
    # Last checkout rejected by a mechanical gate or red review in this attempt. A worker that
    # reports done again at this exact SHA has not produced a new result, so the dispatcher can
    # return it to rework once and then escalate instead of looping forever.
    rejected_sha: str = ""
    rejected_done_reports: int = 0
    # Reviewer pane (secretary-651). The reviewer runs in its own split pane inside the worker's
    # worktree, so its terminal handle must be tracked apart from `handle` (the worker's) or
    # stopping one takes down the other and recovery cannot tell them apart. review_leaf is the
    # pane's leafId: `terminal list` can hand back a different handle alias for the same pty, so
    # the leaf is the stable token to re-find the pane by. review_commit pins the checkout the
    # reviewer was pointed at; the merge gate refuses a verdict once HEAD has moved off it.
    review_handle: str = ""
    review_leaf: str = ""
    review_commit: str = ""
    # Re-review packet: the last rejected checkout and the reviewer's prior blocker text.  These
    # survive the red transition so the next independent reviewer can inspect the delta rather
    # than rediscovering the full historical diff.
    previous_reviewed_sha: str = ""
    previous_blockers: str = ""
    # The worker pane has the same handle-alias problem as the reviewer pane.  Keep its leafId
    # too, so an inventory alias cannot turn a live worker into a missing-terminal respawn.
    worker_leaf: str = ""
    # Where each role's head writes its pid heartbeat (secretary-820). Recorded when the launch
    # intent is taken back, so it names the head that is actually running. This is the identity
    # that survives a lost pane handle: a head adopted from a launch intent has no handle, and
    # without a pid the stop paths (freeze before review, respawn, red-verdict rework, freeze)
    # would silently do nothing and a replacement head would start beside a live one. Cleared
    # together with the handle whenever that role's head is confirmed stopped.
    worker_pid_file: str = ""
    review_pid_file: str = ""
    # A Ready record keeps its workspace so the next claim can reuse the checkout. Once
    # reconciliation has stopped that workspace, remember the result separately from the head
    # identities so later ticks do not stop the same checkout again.
    workspace_settled: bool = False
    # Wait watchdogs (secretary-654): when the current wait for a worker report / review
    # verdict started, and how many times that wait has already respawned its head. Both
    # reset whenever the card enters a fresh wait of that kind.
    worker_waiting_since: float = 0.0
    worker_respawns: int = 0
    # Most recent output from the tracked head pane.  This is deliberately pane-scoped: output
    # from an unrelated shell in the same worktree must not keep a broken head alive.
    worker_started_at: float = 0.0
    worker_progress_at: float = 0.0
    # Since when the head has been ready for input with nothing delivered for the round being
    # waited on (secretary-1063), 0.0 when it is working or its readiness cannot be read. A head
    # that finished its turn and went back to its prompt holds a live pid, so this is the only
    # signal that separates it from one that is still thinking.
    worker_idle_since: float = 0.0
    # The idle watchdog only takes a destructive action after two separate dispatcher ticks
    # observe the same aged idle episode.  It replaces the former second, microsecond-adjacent
    # probe: a fresh tick gives a resumed turn a real chance to report busy.
    worker_idle_confirmations: int = 0
    # The one report prompt this round may spend on a confirmed-idle worker (secretary-1172),
    # before the watchdog stops or replaces it. Durable and keyed on the report generation, so the
    # bound survives a restart and belongs to the round rather than to a tick.
    worker_report_nudge: WorkerReportNudge = field(default_factory=WorkerReportNudge)
    # Durable worker ownership while validation has the checkout. This is deliberately one typed
    # state value rather than four optional fields whose combinations callers would have to infer.
    worker_continuation: WorkerContinuation = field(default_factory=WorkerContinuation)
    review_waiting_since: float = 0.0
    review_respawns: int = 0
    review_started_at: float = 0.0
    review_progress_at: float = 0.0
    review_idle_since: float = 0.0
    review_idle_confirmations: int = 0
    # Pause (secretary-731): when a freeze stopped this card's worker / reviewer head, 0.0 when it
    # did not. A head with an empty handle is otherwise indistinguishable from one that died, so
    # these are what let the tick log and pause-status say "stopped on purpose". Cleared on resume,
    # by the relaunch or by the decision not to relaunch.
    paused_worker_at: float = 0.0
    paused_reviewer_at: float = 0.0
    # Routing telemetry (secretary-716). attempt_round counts the card's worker rounds: claim opens
    # round 1, every rework bounce (red verdict, red gate) opens the next one. worker_run/review_run
    # are the launch snapshots of the heads currently serving that round, kept here so the verdict
    # record reports the configuration the heads actually started with rather than re-reading a
    # `heads.toml` that may have been edited since. Canon is the journal; this is the live copy.
    attempt_round: int = 0
    worker_run: dict[str, Any] = field(default_factory=dict)
    review_run: dict[str, Any] = field(default_factory=dict)
    # Deferred bring-ups (secretary-1163): how many launches of this role's head have been parked
    # over a pane that was not ready for its prompt. The same shape the observer's record carries
    # (`launch_attempts`), without its retry deadline: a worker or reviewer launch is retried by the
    # next dispatcher tick rather than on a backoff of its own, so the count is the whole fence.
    # Reset whenever that role's head does come up, so the bound covers one episode, not a card's
    # whole history.
    worker_launch_attempts: int = 0
    review_launch_attempts: int = 0
    # Aborted reviewer bring-ups (issue:aa9a8ae4): consecutive ticks whose reviewer launch came up
    # but could not confirm the worker was frozen, so it handed the pane back as
    # `review-launch-aborted` and kept its intent. Unlike a deferral this never blocks the card on
    # its own — the head may still be running — so without a bound it repeats silently. Past the
    # stuck ceiling one operator escalation is emitted. Reset the moment a reviewer does take the
    # checkout, so the count covers one stuck episode rather than the card's whole history.
    review_launch_aborts: int = 0
    # Reviewer infrastructure failures over a green candidate (secretary-1401): consecutive ticks
    # whose reviewer bring-up failed outright — a split that would not open, an inventory the
    # runtime would not answer — with no head left behind and nothing said about the candidate. A
    # reviewer that cannot be started is a failure of the review stage, not a verdict on the code,
    # so the card keeps this record: the gate receipt, the candidate SHA, the report round and the
    # held worker session all stay exactly as the green gate left them, and the next tick launches
    # the reviewer again against that same evidence. Only the ceiling blocks the card, for an
    # operator, and `review_infra_error` is what the last attempt failed on. Reset the moment a
    # reviewer does take the checkout, so the count covers one outage rather than the card's life.
    review_infra_failures: int = 0
    review_infra_error: str = ""
    # Durable launch intent (secretary-820): the bring-up this record is in the middle of, written
    # before the host is asked for a head and cleared once the host has answered. Empty at rest.
    # `dispatcher_launch` owns its shape and its recovery; nothing else reads inside it.
    launch_intent: dict[str, Any] = field(default_factory=dict)

    def owns_head(self, role: str | None = None) -> bool:
        """Whether this record still carries an identity that must be settled before replacement."""
        worker = bool(self.handle or self.worker_leaf or self.worker_pid_file)
        review = bool(self.review_handle or self.review_leaf or self.review_pid_file)
        if role == "worker":
            return worker
        if role == "review":
            return review
        return worker or review

    def needs_settling(self) -> bool:
        """Whether reconciliation still owes this record a confirmed stop."""
        return self.owns_head() or bool(self.workspace and not self.workspace_settled)

    def to_json(self) -> dict[str, Any]:
        return {
            "claimed_at": self.claimed_at,
            "comment_baseline": self.comment_baseline,
            "gate_pending_since": self.gate_pending_since,
            "gate_state": self.gate_state,
            "gate_attestation": dict(self.gate_attestation),
            "gate_transport_failures": self.gate_transport_failures,
            "gate_transport_error": self.gate_transport_error,
            "handle": self.handle,
            "head": self.head,
            "preferred_head": self.preferred_head,
            "preferred_review_head": self.preferred_review_head,
            "attempt_id": self.attempt_id,
            "attempt_round": self.attempt_round,
            "paused_reviewer_at": self.paused_reviewer_at,
            "paused_worker_at": self.paused_worker_at,
            "report_generation": self.report_generation,
            "report_decision": self.report_decision,
            "review_baseline": self.review_baseline,
            "review_commit": self.review_commit,
            "previous_reviewed_sha": self.previous_reviewed_sha,
            "previous_blockers": self.previous_blockers,
            "review_handle": self.review_handle,
            "review_head": self.review_head,
            "review_leaf": self.review_leaf,
            "review_idle_since": self.review_idle_since,
            "review_idle_confirmations": self.review_idle_confirmations,
            "review_progress_at": self.review_progress_at,
            "review_respawns": self.review_respawns,
            "review_started_at": self.review_started_at,
            "review_waiting_since": self.review_waiting_since,
            "rejected_done_reports": self.rejected_done_reports,
            "rejected_sha": self.rejected_sha,
            "state": self.state,
            "worker": self.worker,
            "worker_leaf": self.worker_leaf,
            "worker_pid_file": self.worker_pid_file,
            "review_pid_file": self.review_pid_file,
            "worker_idle_since": self.worker_idle_since,
            "worker_idle_confirmations": self.worker_idle_confirmations,
            "worker_report_nudge": self.worker_report_nudge.to_json(),
            "worker_progress_at": self.worker_progress_at,
            "worker_continuation": self.worker_continuation.to_json(),
            "worker_respawns": self.worker_respawns,
            "worker_started_at": self.worker_started_at,
            "worker_run": self.worker_run,
            "review_run": self.review_run,
            "worker_launch_attempts": self.worker_launch_attempts,
            "review_launch_attempts": self.review_launch_attempts,
            "review_launch_aborts": self.review_launch_aborts,
            "review_infra_failures": self.review_infra_failures,
            "review_infra_error": self.review_infra_error,
            "launch_intent": dict(self.launch_intent),
            "worker_waiting_since": self.worker_waiting_since,
            "workspace": self.workspace,
            "workspace_settled": self.workspace_settled,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "DispatcherRecord":
        # A record written before the continuation became one object carried the retention as flat
        # fields. Nothing reads them any more, so loading such a record would report "no
        # continuation" for a worker that is in fact frozen with a delivery pending: the lifecycle
        # would then reuse or drop a head it cannot see. There is no conversion here on purpose,
        # that is the compatibility promise this product dropped, so the load refuses instead.
        legacy = [
            field_name
            for field_name in (
                "worker_retained_at",
                "worker_resume_delivery",
                "worker_resume_phase",
                "worker_resume_sent_at",
            )
            if field_name in payload
        ]
        if legacy:
            raise DispatcherError(
                "unsupported_legacy_record",
                "unsupported legacy dispatcher record: flat continuation fields "
                f"{', '.join(legacy)}; this release stores the retention under "
                "'worker_continuation'. Let the recorded worker finish or clear the record "
                "before upgrading.",
            )
        return cls(
            worker=str(payload.get("worker") or ""),
            workspace=str(payload.get("workspace") or ""),
            handle=str(payload.get("handle") or ""),
            head=str(payload.get("head") or ""),
            review_head=str(payload.get("review_head") or ""),
            preferred_head=str(payload.get("preferred_head") or ""),
            preferred_review_head=str(payload.get("preferred_review_head") or ""),
            attempt_id=str(payload.get("attempt_id") or ""),
            attempt_round=int(payload.get("attempt_round") or 0),
            worker_run=_run_snapshot(payload.get("worker_run")),
            review_run=_run_snapshot(payload.get("review_run")),
            launch_intent=_run_snapshot(payload.get("launch_intent")),
            comment_baseline=int(payload.get("comment_baseline") or 0),
            review_baseline=int(payload.get("review_baseline") or 0),
            # A record written before the generation existed carries its round key in
            # `review_baseline`, which is the number the worker in that checkout was handed. Taking
            # it over is what keeps the first generation this dispatcher opens above every id the
            # previous one issued for the round still running.
            report_generation=int(
                payload.get("report_generation") or payload.get("review_baseline") or 0
            ),
            report_decision=str(payload.get("report_decision") or ""),
            state=str(payload.get("state") or "claimed"),
            claimed_at=float(payload.get("claimed_at") or time.time()),
            gate_state=str(payload.get("gate_state") or ""),
            gate_pending_since=float(payload.get("gate_pending_since") or 0.0),
            gate_attestation=_run_snapshot(payload.get("gate_attestation")),
            gate_transport_failures=int(payload.get("gate_transport_failures") or 0),
            gate_transport_error=str(payload.get("gate_transport_error") or ""),
            rejected_sha=str(payload.get("rejected_sha") or ""),
            rejected_done_reports=int(payload.get("rejected_done_reports") or 0),
            review_handle=str(payload.get("review_handle") or ""),
            review_leaf=str(payload.get("review_leaf") or ""),
            review_commit=str(payload.get("review_commit") or ""),
            previous_reviewed_sha=str(payload.get("previous_reviewed_sha") or ""),
            previous_blockers=str(payload.get("previous_blockers") or ""),
            worker_leaf=str(payload.get("worker_leaf") or ""),
            worker_pid_file=str(payload.get("worker_pid_file") or ""),
            review_pid_file=str(payload.get("review_pid_file") or ""),
            worker_launch_attempts=int(payload.get("worker_launch_attempts") or 0),
            review_launch_attempts=int(payload.get("review_launch_attempts") or 0),
            review_launch_aborts=int(payload.get("review_launch_aborts") or 0),
            review_infra_failures=int(payload.get("review_infra_failures") or 0),
            review_infra_error=str(payload.get("review_infra_error") or ""),
            worker_waiting_since=float(payload.get("worker_waiting_since") or 0.0),
            worker_respawns=int(payload.get("worker_respawns") or 0),
            worker_started_at=float(payload.get("worker_started_at") or 0.0),
            worker_progress_at=float(payload.get("worker_progress_at") or 0.0),
            worker_idle_since=float(payload.get("worker_idle_since") or 0.0),
            worker_idle_confirmations=int(payload.get("worker_idle_confirmations") or 0),
            # Absent on every record written before the prompt existed, which is exactly a round
            # that has not spent one: the empty value opens the same single prompt for it.
            worker_report_nudge=WorkerReportNudge.from_json(payload.get("worker_report_nudge")),
            worker_continuation=WorkerContinuation.from_json(
                payload.get("worker_continuation")
            ),
            review_waiting_since=float(payload.get("review_waiting_since") or 0.0),
            review_respawns=int(payload.get("review_respawns") or 0),
            review_started_at=float(payload.get("review_started_at") or 0.0),
            review_progress_at=float(payload.get("review_progress_at") or 0.0),
            review_idle_since=float(payload.get("review_idle_since") or 0.0),
            review_idle_confirmations=int(payload.get("review_idle_confirmations") or 0),
            paused_worker_at=float(payload.get("paused_worker_at") or 0.0),
            paused_reviewer_at=float(payload.get("paused_reviewer_at") or 0.0),
            workspace_settled=bool(payload.get("workspace_settled", False)),
        )


def _run_snapshot(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def now_rfc3339() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_attempt_id() -> str:
    return f"attempt-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:12]}"


def record_attempt(
    payload: dict[str, Any],
    attempt_id: str,
    reference: str,
    actor: str,
    owner: str,
) -> None:
    attempts = payload.setdefault("attempts", [])
    if not isinstance(attempts, list):
        attempts = []
        payload["attempts"] = attempts
    if any(isinstance(attempt, dict) and attempt.get("attempt_id") == attempt_id for attempt in attempts):
        return
    attempts.append({
        "attempt_id": attempt_id,
        "pilot_ref": reference,
        "owner": owner,
        "started_at": now_rfc3339(),
        "started_by": actor,
    })


def attempt_request_id(attempt_id: str, action: str, reference: str, suffix: str = "") -> str:
    parts = ["dispatcher", request_token(attempt_id or "attempt-missing"), action, reference]
    if suffix:
        parts.append(suffix)
    return "-".join(request_token(part) for part in parts)


def request_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")
    return token or "empty"


def claim_mismatch(
    task: dict[str, Any],
    worker: str,
    resolved_head: str,
    resolved_review_head: str,
) -> list[str]:
    mismatches = []
    if task.get("state") != "in_progress":
        mismatches.append("state")
    if task.get("claim", {}).get("worker") != worker:
        mismatches.append("worker")
    routing = task.get("routing", {})
    if routing.get("resolved_worker_head") != resolved_head:
        mismatches.append("resolved_head")
    if routing.get("resolved_review_head") != resolved_review_head:
        mismatches.append("resolved_review_head")
    return mismatches


def claim_actual(task: dict[str, Any]) -> dict[str, Any]:
    routing = task.get("routing", {})
    return {
        "state": task.get("state"),
        "worker": task.get("claim", {}).get("worker"),
        "resolved_head": routing.get("resolved_worker_head"),
        "resolved_review_head": routing.get("resolved_review_head"),
    }


def record_divergence(
    payload: dict[str, Any],
    attempt_id: str,
    reference: str,
    step: str,
    reason: str,
    *,
    expected: dict[str, Any],
    actual: dict[str, Any],
    details: list[str],
) -> dict[str, Any]:
    divergences = payload.setdefault("controlled_divergences", [])
    if not isinstance(divergences, list):
        divergences = []
        payload["controlled_divergences"] = divergences
    divergence = {
        "id": f"div_{uuid.uuid4().hex[:16]}",
        "at": now_rfc3339(),
        "attempt_id": attempt_id,
        "pilot_ref": reference,
        "step": step,
        "reason": reason,
        "expected": expected,
        "actual": actual,
        "details": details,
        # Opening rule: every divergence starts open. Closing rule lives with the
        # production tick (see `_reconcile_production` in dispatcher_production.py):
        # a divergence closes once its card leaves the active dispatcher cycle
        # (in_progress/validate), whatever state it lands in. A divergence with no
        # "status" is a pre-existing record from before this field existed and is
        # treated as open.
        "status": "open",
    }
    divergences.append(divergence)
    return divergence


def divergence_is_open(divergence: dict[str, Any]) -> bool:
    return divergence.get("status") != "closed"


def close_divergence(divergence: dict[str, Any], reason: str) -> None:
    divergence["status"] = "closed"
    divergence["closed_at"] = now_rfc3339()
    divergence["closed_reason"] = reason
