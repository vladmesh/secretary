"""State helpers for the production dispatcher."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from secretary.dispatcher_types import DispatcherError
from secretary.dispatcher_worker_lifecycle import (
    WorkerContinuation,
    WorkerContinuationLiveness,
    WorkerReportNudge,
)

# ``VitalityEpisode`` is imported lazily in ``DispatcherRecord.from_json``: the episode module
# reads the heartbeat vocabulary from ``dispatcher_watchdog``, whose own imports reach back into
# this module, so an eager import here would close a cycle at load time. A deferred import keeps
# the record's typed persistence without making the state module an ancestor of the vocabulary.

if TYPE_CHECKING:
    from secretary.dispatch.head_vitality_episode import VitalityEpisode

    # Registry of claim skips: Ready records these and continues scanning.
CLAIM_SKIP_RESOURCE_NOT_READY = "resource-not-ready"
CLAIM_SKIP_FAILOVER_COLLAPSE = "failover-collapses-roles"
CLAIM_SKIP_ACTIONS = frozenset(
    {
        CLAIM_SKIP_RESOURCE_NOT_READY,
        CLAIM_SKIP_FAILOVER_COLLAPSE,
    }
)


class OutcomeTerminalPath(str, Enum):
    """Whether a terminal effect follows the round's accepted worker report.

    This is deliberately independent of the report handoff.  The handoff may
    be unavailable precisely when the terminal obligation needs to say that
    its forward lineage is incomplete.
    """

    NO_ACCEPTED_REPORT = "no_accepted_report"
    FOLLOWS_ACCEPTED_REPORT = "follows_accepted_report"


def outcome_terminal_path(value: Any, *, state: str) -> OutcomeTerminalPath:
    """Read the durable path, conservatively classifying pre-field records."""
    if value in {path.value for path in OutcomeTerminalPath}:
        return OutcomeTerminalPath(str(value))
    if value not in (None, ""):
        raise DispatcherError("invalid_outcome_terminal_path", f"unknown outcome terminal path {value!r}")
    # A record from before the explicit field can already have accepted a
    # report and left In progress.  Its state is the dispatcher-owned path
    # fact, never a source-handoff or marker lookup.
    if state in {"validate", "review_starting", "reviewing", "assessment"}:
        return OutcomeTerminalPath.FOLLOWS_ACCEPTED_REPORT
    return OutcomeTerminalPath.NO_ACCEPTED_REPORT


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
    # Durable report round advances only when a new round opens, never on respawn.
    # The head each role was preferred on when the claim had to leave that preference behind
    # (secretary-1165), empty when it did not. The claim walks the canon's fallback chain when the
    # preferred head's resource is red or spent, and `head`/`review_head` above then name another
    # family. Kept here because the preference is a claim-time fact: re-reading `role_defaults` at
    # document-build time would answer a different question, and an operator who repoints a role
    # mid-attempt would turn a faithful record into a false one.
    preferred_head: str = ""
    preferred_review_head: str = ""
    report_generation: int = 0
    # Frozen when the dispatcher accepts a worker report, before any source
    # handoff is consulted.  Every later terminal effect reads this one typed
    # classification, so losing a report handoff cannot redefine the path.
    outcome_terminal_path: OutcomeTerminalPath = OutcomeTerminalPath.NO_ACCEPTED_REPORT
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
    # the gate instead of coasting on a stale pass, except for an observer-directed report-only
    # research continuation whose accepted receipt still names its unchanged candidate. gate_pending_since
    # stamps when a github CI rollup first went non-terminal, driving the pending watchdog.
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
    # The rerun is a second backend operation after an answered red result.  It has the same
    # transport ceiling, but keeps its own consecutive count so that rereading the red result does
    # not erase an unanswered rerun POST on the following tick.
    gate_rerun_transport_failures: int = 0
    gate_rerun_transport_error: str = ""
    # Recovery of a classified CI-service red is bounded separately from transport retries.  It is
    # anchored to the SHA and exact Actions run the gate reran, so a rework starts clean while an
    # unchanged checkout cannot spin on the old terminal check-run.
    gate_infrastructure_reruns_sha: str = ""
    gate_infrastructure_reruns: int = 0
    gate_infrastructure_rerun_run_id: str = ""
    gate_infrastructure_rerun_reason: str = ""
    # Gate-authored PR identity lives outside editable PR text; absence forbids refresh.
    gate_pr_authorship: dict[str, Any] = field(default_factory=dict)
    # The card branch and object id the gate last published (secretary-1540).  A held worker
    # rebases, so publication is a rewrite of the ref the dispatcher itself wrote; this durable
    # observation is the lease that rewrite is fenced against, and a remote sitting anywhere else
    # is a foreign push the gate refuses instead of clobbering.
    gate_published_ref: dict[str, Any] = field(default_factory=dict)
    # Dispatcher-owned CI invocation for a base-identical research candidate.  The SHA and
    # discovered Actions run id survive a restart, so a later tick polls this invocation instead
    # of creating another run or accepting an unrelated base check.
    gate_workflow_dispatch: dict[str, Any] = field(default_factory=dict)
    # Last checkout rejected by a mechanical gate or red review in this attempt.  The class and
    # reason come from the gate's structured result, before any card comment is made.  A same-SHA
    # report after an infrastructure red may retry that gate; every other same-SHA report is still
    # the stale-result safeguard and returns to rework once before escalating.
    rejected_sha: str = ""
    rejected_failure_class: str = "substantive"
    rejected_failure_reason: str = ""
    rejected_done_reports: int = 0
    # When the dispatcher last put a question to the worker head that the head has not answered:
    # the instant a done report was bounced back to rework (secretary-1543). It is a fact about
    # the board conversation, not an observation of the head, and the vitality reducer takes it as
    # a declared input -- with an ended turn and no progress since, it is an explicit stall signal
    # instead of something the outer ceiling notices hours later. Cleared when a report is
    # accepted, and when a replacement head that never saw the rejection takes over.
    worker_answer_owed_since: float = 0.0
    # Reviewer leaf is stable across handle aliases; its commit fences verdicts to its checkout.
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
    # Heartbeats preserve head identity across lost pane handles; clear only on confirmed stop.
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
    # Provider progress during a retained red continuation.  This stays beside the continuation
    # rather than inside its transient delivery stage so the terminal outcome remains auditable
    # after a confirmed stop hands the card to its one replacement head.
    worker_continuation_liveness: WorkerContinuationLiveness = field(
        default_factory=WorkerContinuationLiveness
    )
    # Shadow vitality episodes are telemetry only; `None` means no episode was recorded.
    worker_vitality_episode: VitalityEpisode | None = None
    review_vitality_episode: VitalityEpisode | None = None
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
    # The worker head's own run, as the three head operations keep it (secretary-1412): an identity
    # that survives Orca aliasing its pane handle, the lifecycle it has reached, and — once a stop
    # has begun — who initiated that stop. `worker_run` beside it is the routing snapshot of the
    # configuration the head launched with; this is the state of the head itself, and it is durable
    # for the same reason the pane identity is: the process that spawned a head is not necessarily
    # the process that ends it, and a restarted dispatcher must still be able to say who was
    # ending this one.
    worker_head_run: dict[str, Any] = field(default_factory=dict)
    # The reviewer's own run, kept for exactly the same reasons (secretary-1414). The reviewer is
    # the head this dispatcher stops most often and from the most places — a red verdict, a stalled
    # reviewer's respawn, a pipeline freeze, launch recovery, reconciliation — and until it had a
    # run of its own, none of those left a record of who was ending it.
    review_head_run: dict[str, Any] = field(default_factory=dict)
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
    # Reviewer prompt deliveries this card lost, and the bounded evidence of the last one, from
    # the same delivery boundary the observer's wakes go through. Unlike the counter above these
    # are not reset by a reviewer that later takes the checkout: a card whose first reviewer never
    # received its prompt must still read that way afterwards, which is the whole point of keeping
    # delivery evidence rather than delivery state. Payload size and hash only, never prompt text.
    review_delivery_failures: int = 0
    review_delivery_evidence: dict[str, Any] = field(default_factory=dict)
    # Same bounded evidence for worker launch, rework and one-turn continuation delivery.  It is
    # retained across recovery so an attempted body/submit pair is never mistaken for an absent
    # prompt when the next tick chooses whether a head may be replaced.
    worker_delivery_failures: int = 0
    worker_delivery_evidence: dict[str, Any] = field(default_factory=dict)
    # Launch intent is persisted before host creation and cleared after its answer.
    launch_intent: dict[str, Any] = field(default_factory=dict)
    # A card standing in an active execution state with no worker identity and no launch debt
    # (secretary-1544).  Written before the recovery decides, so a tick that cannot finish the
    # decision still leaves the degradation on the record instead of an empty handle that reads
    # as work in progress.  Cleared by the replacement launch that ends the episode.
    worker_headless: dict[str, Any] = field(default_factory=dict)

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
            "gate_rerun_transport_failures": self.gate_rerun_transport_failures,
            "gate_rerun_transport_error": self.gate_rerun_transport_error,
            "gate_infrastructure_reruns_sha": self.gate_infrastructure_reruns_sha,
            "gate_infrastructure_reruns": self.gate_infrastructure_reruns,
            "gate_infrastructure_rerun_run_id": self.gate_infrastructure_rerun_run_id,
            "gate_infrastructure_rerun_reason": self.gate_infrastructure_rerun_reason,
            "gate_pr_authorship": dict(self.gate_pr_authorship),
            "gate_published_ref": dict(self.gate_published_ref),
            "gate_workflow_dispatch": dict(self.gate_workflow_dispatch),
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
            "outcome_terminal_path": self.outcome_terminal_path.value,
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
            "worker_answer_owed_since": self.worker_answer_owed_since,
            "rejected_failure_class": self.rejected_failure_class,
            "rejected_failure_reason": self.rejected_failure_reason,
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
            "worker_continuation_liveness": self.worker_continuation_liveness.to_json(),
            "worker_vitality_episode": (
                self.worker_vitality_episode.to_json() if self.worker_vitality_episode is not None else None
            ),
            "review_vitality_episode": (
                self.review_vitality_episode.to_json() if self.review_vitality_episode is not None else None
            ),
            "worker_respawns": self.worker_respawns,
            "worker_started_at": self.worker_started_at,
            "worker_head_run": dict(self.worker_head_run),
            "review_head_run": dict(self.review_head_run),
            "worker_run": self.worker_run,
            "review_run": self.review_run,
            "worker_launch_attempts": self.worker_launch_attempts,
            "review_launch_attempts": self.review_launch_attempts,
            "review_launch_aborts": self.review_launch_aborts,
            "review_infra_failures": self.review_infra_failures,
            "review_infra_error": self.review_infra_error,
            "review_delivery_failures": self.review_delivery_failures,
            "review_delivery_evidence": dict(self.review_delivery_evidence),
            "worker_delivery_failures": self.worker_delivery_failures,
            "worker_delivery_evidence": dict(self.worker_delivery_evidence),
            "worker_headless": dict(self.worker_headless),
            "launch_intent": dict(self.launch_intent),
            "worker_waiting_since": self.worker_waiting_since,
            "workspace": self.workspace,
            "workspace_settled": self.workspace_settled,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> DispatcherRecord:
        # Refuse obsolete flat continuation fields; interpreting them as absent is unsafe.
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
        state = str(payload.get("state") or "claimed")
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
            worker_head_run=_run_snapshot(payload.get("worker_head_run")),
            review_head_run=_run_snapshot(payload.get("review_head_run")),
            worker_run=_run_snapshot(payload.get("worker_run")),
            review_run=_run_snapshot(payload.get("review_run")),
            launch_intent=_run_snapshot(payload.get("launch_intent")),
            comment_baseline=int(payload.get("comment_baseline") or 0),
            review_baseline=int(payload.get("review_baseline") or 0),
            # A record written before the generation existed carries its round key in
            # `review_baseline`, which is the number the worker in that checkout was handed. Taking
            # it over is what keeps the first generation this dispatcher opens above every id the
            # previous one issued for the round still running.
            report_generation=int(payload.get("report_generation") or payload.get("review_baseline") or 0),
            report_decision=str(payload.get("report_decision") or ""),
            outcome_terminal_path=outcome_terminal_path(payload.get("outcome_terminal_path"), state=state),
            state=state,
            claimed_at=float(payload.get("claimed_at") or time.time()),
            gate_state=str(payload.get("gate_state") or ""),
            gate_pending_since=float(payload.get("gate_pending_since") or 0.0),
            gate_attestation=_run_snapshot(payload.get("gate_attestation")),
            gate_transport_failures=int(payload.get("gate_transport_failures") or 0),
            gate_transport_error=str(payload.get("gate_transport_error") or ""),
            gate_rerun_transport_failures=int(payload.get("gate_rerun_transport_failures") or 0),
            gate_rerun_transport_error=str(payload.get("gate_rerun_transport_error") or ""),
            gate_infrastructure_reruns_sha=str(payload.get("gate_infrastructure_reruns_sha") or ""),
            gate_infrastructure_reruns=int(payload.get("gate_infrastructure_reruns") or 0),
            gate_infrastructure_rerun_run_id=str(payload.get("gate_infrastructure_rerun_run_id") or ""),
            gate_infrastructure_rerun_reason=str(payload.get("gate_infrastructure_rerun_reason") or ""),
            gate_pr_authorship=_run_snapshot(payload.get("gate_pr_authorship")),
            gate_published_ref=_run_snapshot(payload.get("gate_published_ref")),
            gate_workflow_dispatch=_run_snapshot(payload.get("gate_workflow_dispatch")),
            rejected_sha=str(payload.get("rejected_sha") or ""),
            rejected_failure_class=str(payload.get("rejected_failure_class") or "substantive"),
            rejected_failure_reason=str(payload.get("rejected_failure_reason") or ""),
            rejected_done_reports=int(payload.get("rejected_done_reports") or 0),
            worker_answer_owed_since=float(payload.get("worker_answer_owed_since") or 0.0),
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
            review_delivery_failures=int(payload.get("review_delivery_failures") or 0),
            review_delivery_evidence=(
                dict(payload["review_delivery_evidence"])
                if isinstance(payload.get("review_delivery_evidence"), dict)
                else {}
            ),
            worker_delivery_failures=int(payload.get("worker_delivery_failures") or 0),
            worker_delivery_evidence=(
                dict(payload["worker_delivery_evidence"])
                if isinstance(payload.get("worker_delivery_evidence"), dict)
                else {}
            ),
            worker_headless=(
                dict(payload["worker_headless"]) if isinstance(payload.get("worker_headless"), dict) else {}
            ),
            worker_waiting_since=float(payload.get("worker_waiting_since") or 0.0),
            worker_respawns=int(payload.get("worker_respawns") or 0),
            worker_started_at=float(payload.get("worker_started_at") or 0.0),
            worker_progress_at=float(payload.get("worker_progress_at") or 0.0),
            worker_idle_since=float(payload.get("worker_idle_since") or 0.0),
            worker_idle_confirmations=int(payload.get("worker_idle_confirmations") or 0),
            # Absent on every record written before the prompt existed, which is exactly a round
            # that has not spent one: the empty value opens the same single prompt for it.
            worker_report_nudge=WorkerReportNudge.from_json(payload.get("worker_report_nudge")),
            worker_continuation=WorkerContinuation.from_json(payload.get("worker_continuation")),
            worker_continuation_liveness=WorkerContinuationLiveness.from_json(
                payload.get("worker_continuation_liveness")
            ),
            # Absence is "no episode yet", not an empty one: a record from before the field
            # existed, or a role whose head has never been observed, carries no claim. A present
            # but damaged payload raises - a corrupt shadow verdict must stop the load rather
            # than be silently dropped, or the record would look observed when it was not.
            worker_vitality_episode=(
                _vitality_episode_from_json(payload["worker_vitality_episode"])
                if payload.get("worker_vitality_episode") is not None
                else None
            ),
            review_vitality_episode=(
                _vitality_episode_from_json(payload["review_vitality_episode"])
                if payload.get("review_vitality_episode") is not None
                else None
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


def _vitality_episode_from_json(payload: Any) -> VitalityEpisode:
    """Load one persisted vitality episode, importing its module lazily.

    See the import note at the top of this module: the episode vocabulary sits above
    ``dispatcher_watchdog`` in the dependency order, so the record reads it at call time instead
    of at load time.
    """
    from secretary.dispatch.head_vitality_episode import VitalityEpisode

    return VitalityEpisode.from_json(payload)


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
    attempts.append(
        {
            "attempt_id": attempt_id,
            "pilot_ref": reference,
            "owner": owner,
            "started_at": now_rfc3339(),
            "started_by": actor,
        }
    )


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
