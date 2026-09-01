"""Production dispatcher runtime."""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from secretary.board.events import project_verdict, render_marker_comment
from secretary.board.models import Event
from secretary.checkpoint import CheckpointPusher, CheckpointWriter
from secretary.codex_provider_events import (
    CodexProviderSourceError,
)
from secretary.config import DataDirError, instance_data_dir
from secretary.dispatch.attempt_usage import (
    attempt_usage_data as _attempt_usage_data,
)
from secretary.dispatch.attempt_usage import (
    attempt_usage_reason as _attempt_usage_reason,
)
from secretary.dispatch.attempt_usage import (
    attribute_phase as _attribute_phase,
)
from secretary.dispatch.attempt_usage import (
    causal_predecessor as _causal_predecessor,
)
from secretary.dispatch.attempt_usage import (
    collect_usage as _collect_usage,
)
from secretary.dispatch.attempt_usage import (
    provider_usage_source as _provider_usage_source,
)
from secretary.dispatch.head_vitality import (
    SnapshotSource as _SnapshotSource,
)
from secretary.dispatch.head_vitality import (
    snapshots_from_status as _snapshots_from_status,
)
from secretary.dispatch.head_vitality_episode import (
    DEFAULT_VITALITY_THRESHOLDS as _DEFAULT_VITALITY_THRESHOLDS,
)
from secretary.dispatch.head_vitality_episode import (
    VitalityVerdict as VitalityVerdict,
)
from secretary.dispatch.head_vitality_episode import (
    reduce_vitality as _reduce_vitality,
)
from secretary.dispatch.head_vitality_guard import (
    assert_destructive_allowed as _assert_destructive_allowed,
)
from secretary.dispatch.head_vitality_policy import (
    DEFAULT_RECOVERY_THRESHOLDS as _DEFAULT_RECOVERY_THRESHOLDS,
)
from secretary.dispatch.head_vitality_policy import (
    RUNG_ESCALATED as _RUNG_ESCALATED,
)
from secretary.dispatch.head_vitality_policy import (
    RecoveryIntent as _RecoveryIntent,
)
from secretary.dispatch.head_vitality_policy import (
    RecoveryThresholds as _RecoveryThresholds,
)
from secretary.dispatch.head_vitality_policy import (
    apply_rung_state as _apply_rung_state,
)
from secretary.dispatch.head_vitality_policy import (
    decide_recovery as _decide_recovery,
)
from secretary.dispatch.host import (  # noqa: F401  # Compatibility re-exports.
    DESTRUCTIVE_VERDICTS,
    HEAD_STOP_GRACE_SECONDS,
    HEAD_STOP_POLL_SECONDS,
    OBSERVER_REPO_BRANCH,
    OBSERVER_WORKSPACE_DIR,
    CommandHostRuntime,
    DispatcherHeadTransport,
    InstanceCatalog,
    LaunchedHead,
    _blocked_actions_and_their_infrastructure_twins,
    _body_file_instructions,
    _body_file_path,
    _continuation_note,
    _delivery_evidence_json,
    _durable_head_run,
    _gate_attestation_for_prompt,
    _head_runtime_name,
    _legacy_worker_branch,
    _record_worker_delivery_evidence,
    _report_nudge_prompt,
    _same_repo,
    _watchdog_kind,
)
from secretary.dispatch.review_context import (
    ReviewContextError,
    ReviewRoundContext,
    bind_review_context,
    open_review_round,
    require_review_context,
    require_verdict_review_context,
)
from secretary.dispatcher_gate import (
    GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS,
    GATE_PENDING_STALL_SECONDS,
    GATE_TRANSPORT_MAX_ATTEMPTS,
    GateResult,
)
from secretary.dispatcher_gate import (
    _fingerprint as _gate_fingerprint,
)
from secretary.dispatcher_gate import (
    validation_ci as _validation_ci,
)
from secretary.dispatcher_gate_receipt import (
    AcceptedGreenGate,
    GateReceipt,
)
from secretary.dispatcher_helpers import (
    RED_REVIEW_CEILING,
    _gate_red_repeat_count,
    _last_marker,
    _last_marker_body,
    _last_review_red_body,
    _report_adoption_baseline,
    _review_adoption_baseline,
    _round_report_ids,
    _round_report_marker,
    _spent_report_generations,
    _task_doc_decision,
    _task_doc_report_generation,
    _worker_id,
    scrub_host_output,
)
from secretary.dispatcher_helpers import (
    red_review_count as _red_review_count,
)
from secretary.dispatcher_helpers import (
    safe_one_line as _safe_one_line,
)
from secretary.dispatcher_launch import (
    REVIEW_ROLE,
    STAGE_CLAIM,
    STAGE_RESPAWN,
    STAGE_REWORK,
    WORKER_ROLE,
    BringUpFailure,
)
from secretary.dispatcher_launch import (
    bring_up_blocked_action as _bring_up_blocked_action,
)
from secretary.dispatcher_launch import (
    bring_up_blocked_reason as _bring_up_blocked_reason,
)
from secretary.dispatcher_launch import (
    classify_bring_up_failure as _classify_bring_up_failure,
)
from secretary.dispatcher_launch import (
    clear_launch_intent as _clear_launch_intent,
)
from secretary.dispatcher_launch import (
    confirm_launch_intent as _confirm_launch_intent,
)
from secretary.dispatcher_launch import (
    forget_role_head as _forget_role_head,
)
from secretary.dispatcher_launch import (
    head_stop_unconfirmed as _head_stop_unconfirmed,
)
from secretary.dispatcher_launch import (
    keep_reserved_round as _keep_reserved_round,
)
from secretary.dispatcher_launch import (
    launch_aborted as _launch_aborted,
)
from secretary.dispatcher_launch import (
    launch_deferred as _launch_deferred,
)
from secretary.dispatcher_launch import (
    launch_intent as _launch_intent,
)
from secretary.dispatcher_launch import (
    launch_intent_unwritable as _launch_intent_unwritable,
)
from secretary.dispatcher_launch import (
    launch_left_a_head as _launch_left_a_head,
)
from secretary.dispatcher_launch import (
    launch_pid_file as _launch_pid_file,
)
from secretary.dispatcher_launch import (
    mark_launch_aborted as _mark_launch_aborted,
)
from secretary.dispatcher_launch import (
    merge_launch_head_run as _merge_launch_head_run,
)
from secretary.dispatcher_launch import (
    reset_launch_attempts as _reset_launch_attempts,
)
from secretary.dispatcher_launch import (
    resolve_launch_intent as _resolve_launch_intent,
)
from secretary.dispatcher_launch import (
    write_launch_intent as _write_launch_intent,
)
from secretary.dispatcher_pause import ProductionPause
from secretary.dispatcher_pause_ops import (
    pause as _pause_pipeline,
)
from secretary.dispatcher_pause_ops import (
    pause_status as _pause_status,
)
from secretary.dispatcher_pause_ops import (
    resume as _resume_pipeline,
)
from secretary.dispatcher_production import (
    ProductionState,
)
from secretary.dispatcher_production import (
    production_observe as _production_observe,
)
from secretary.dispatcher_production import (
    production_probe as _production_probe,
)
from secretary.dispatcher_production import (
    production_run as _production_run,
)
from secretary.dispatcher_production import (
    production_tick as _production_tick,
)
from secretary.dispatcher_review import (
    end_review_pane as _end_review_pane,
)
from secretary.dispatcher_review import (
    recover_review_launch as _recover_review_launch,
)
from secretary.dispatcher_review import (
    start_review as _start_review,
)
from secretary.dispatcher_state import (
    CLAIM_SKIP_FAILOVER_COLLAPSE,
    CLAIM_SKIP_RESOURCE_NOT_READY,
    DispatcherRecord,
    now_rfc3339,
)
from secretary.dispatcher_state import (
    attempt_request_id as _attempt_request_id,
)
from secretary.dispatcher_state import (
    claim_actual as _claim_actual,
)
from secretary.dispatcher_state import (
    claim_mismatch as _claim_mismatch,
)
from secretary.dispatcher_state import (
    new_attempt_id as _new_attempt_id,
)
from secretary.dispatcher_state import (
    record_attempt as _record_attempt,
)
from secretary.dispatcher_state import (
    record_divergence as _record_divergence,
)
from secretary.dispatcher_state import (
    request_token as _request_token,
)
from secretary.dispatcher_tui import (
    COMPOSER_EMPTY,
    COMPOSER_UNKNOWN,
    READINESS_BUSY,
)
from secretary.dispatcher_tui import (
    delivery_readiness_state as _delivery_readiness_state,
)
from secretary.dispatcher_types import (
    STOPPED_BY_DISPATCHER,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_OPERATOR,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_RECONCILIATION,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_REPLACEMENT,
    STOPPED_BY_REVIEW_FREEZE,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_REVIEW_VERDICT,
    STOPPED_BY_WATCHDOG,
    DispatcherError,
    GateTransportError,
    HeadLaunchAborted,
    HostError,
)
from secretary.dispatcher_watchdog import (
    HeadRunIdentityMismatch as _HeadRunIdentityMismatch,
)
from secretary.dispatcher_watchdog import (
    guard_head_run_identity as _guard_head_run_identity,
)
from secretary.dispatcher_watchdog import (
    head_process_status as _head_process_status,
)
from secretary.dispatcher_watchdog import (
    head_run_process_status as _head_run_process_status,
)
from secretary.dispatcher_watchdog import (
    heartbeat_is_live_match as _heartbeat_is_live_match,
)
from secretary.dispatcher_watchdog import (
    initial_output_stall_seconds as _initial_output_stall_seconds,
)
from secretary.dispatcher_watchdog import (
    reset_idle as _reset_idle,
)
from secretary.dispatcher_watchdog import (
    reset_wait as _reset_wait,
)
from secretary.dispatcher_watchdog import (
    stall_seconds as _stall_seconds,
)
from secretary.dispatcher_watchdog import (
    suspension_response_window_seconds as _suspension_response_window_seconds,
)
from secretary.dispatcher_watchdog import (
    wait_cycle_token as _wait_cycle_token,
)
from secretary.dispatcher_worker_lifecycle import (
    BUSY_RETRY_INITIAL_SECONDS,
    CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS,
    ContinuationLivenessState,
    ContinuationProviderCondition,
    ContinuationRecoveryRung,
    WorkerContinuationLiveness,
)
from secretary.head_health import (
    HeadChoice,
    HeadHealth,
    HeadReadiness,
    resolve_head_chain,
)
from secretary.projects.contract import (
    CONTRACT_FIT,
    CONTRACT_REFUSED,
    CONTRACT_UNDECIDABLE,
    UNDECIDABLE_NO_REGISTERED_PROJECT,
    UNDECIDABLE_PROJECT_UNAVAILABLE,
    ContractUnusable,
    ContractVerdict,
)
from secretary.routing_journal import (
    MODEL_UNKNOWN,
    REVIEWER,
    WORKER,
    HeadRun,
)
from secretary.routing_journal import (
    attempts as _routing_attempts,
)
from secretary.routing_journal import (
    launched_head_run_snapshot as _launched_head_run_snapshot,
)
from secretary.routing_journal import (
    routing_payload as _routing_payload,
)
from secretary.routing_journal import (
    run_key as _run_key,
)
from secretary.sprints import SprintReader, budget_thresholds
from secretary.tasks import (
    KanboardClient,
    TaskAudit,
    TaskError,
    TaskReader,
    TaskWriter,
    standing_decision,
)
from triggered_agents.runtime import head as head_ops
from triggered_agents.runtime.codex_preflight import (
    CodexFanoutRecordingError,
)
from triggered_agents.runtime.head import (
    PYTHON_SAFE_PATH_FLAG as _PYTHON_SAFE_PATH_FLAG,
)
from triggered_agents.runtime.head import (
    HeadSpec,
)
from triggered_agents.runtime.launch_prefix import pythonpath_prefix

_PYTHONPATH_PREFIX = pythonpath_prefix()
_CONTROL_PLANE_TASK_COMMAND = f"{_PYTHONPATH_PREFIX} python3 {_PYTHON_SAFE_PATH_FLAG} -m secretary task"


def default_data_dir(instance_path: Path) -> Path:
    try:
        return instance_data_dir(_instance_file(instance_path))
    except DataDirError as exc:
        raise DispatcherError("invalid_instance", f"invalid instance: {exc}", 2) from None


def _instance_file(path: Path) -> Path:
    return path / "instance.yaml" if path.is_dir() else path


def _usage_fallback_snapshot(
    journal_role: str,
    record: DispatcherRecord,
    lifecycle_run: dict[str, Any],
    *,
    role: str,
) -> dict[str, Any]:
    """A minimal routing-shaped snapshot for a phase whose launch record was lost.

    Everything here comes from the head's own attested run, never from the registry as it reads
    now: the model is recorded as unresolved rather than as a value some later edit supplied.
    """
    spec = lifecycle_run.get("spec") if isinstance(lifecycle_run, dict) else None
    spec = spec if isinstance(spec, dict) else {}
    return HeadRun(
        role=journal_role,
        head=record.head if role == WORKER_ROLE else record.review_head,
        adapter=str(spec.get("adapter") or ""),
        model=str(spec.get("model") or ""),
        model_source=MODEL_UNKNOWN,
    ).to_json()


class DispatcherRuntime:
    def __init__(
        self,
        reader: TaskReader,
        writer: TaskWriter,
        audit: TaskAudit,
        data_dir: Path,
        catalog: InstanceCatalog,
        host: CommandHostRuntime,
        *,
        owner: str = "secretary-dispatcher",
        production_state: ProductionState | None = None,
        pause: ProductionPause | None = None,
        checkpoint: CheckpointWriter | None = None,
        checkpoint_push: CheckpointPusher | None = None,
        sprints: Any | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.audit = audit
        self.production_state = production_state or ProductionState(data_dir)
        self.pause = pause or ProductionPause(data_dir)
        self.catalog = catalog
        self.host = host
        self.owner = owner
        self.checkpoint = checkpoint
        self.checkpoint_push = checkpoint_push
        self.head_health = HeadHealth(catalog, data_dir)
        # Sprint entities live on their own board, so they need their own reader, not the card one.
        instance = getattr(catalog, "instance", {})
        limits = budget_thresholds(instance if isinstance(instance, dict) else None)
        self.sprints = (
            sprints
            if sprints is not None
            else SprintReader(reader.client, data_dir=Path(audit.board_dir).parent, thresholds=limits)
        )

    def head_readiness(self, head: str) -> HeadReadiness:
        return self.head_health.check(head)

    def _head_fallback(self, head: str) -> list[str] | None:
        """`head`'s fallback chain, or None when the registry does not describe it at all.

        None is not an empty chain. The existence question is answered here as one lookup and never
        put to a readiness probe, whose `HostError` for an undescribed head would escape the walk and
        take the tick's Ready pass with it.
        """
        try:
            return self.catalog.head_fallback(head)
        except HostError:
            return None

    def resolve_head(self, preferred: str) -> HeadChoice:
        """The head to actually launch for `preferred`, walking the canon's fallback chain.

        Substitution follows only the chain the canon writes down, and only at claim, where the
        decision is recorded on the card. When nothing in the chain is launchable the answer is an
        empty head: the caller claim-skips and the card waits in Ready.
        """
        return resolve_head_chain(preferred, self.head_readiness, self._head_fallback)

    def _require_head_ready(self, head: str) -> None:
        readiness = self.head_readiness(head)
        if not readiness.launch_allowed:
            raise HostError(f"head resource {readiness.resource} is {readiness.status}: {readiness.reason}")

    def bind_codex_provider_ingress(
        self,
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        *,
        role: str,
        reference: str,
    ) -> None:
        """Give a persisted Codex HeadRun its only provider-event ingress."""
        stored = record.worker_head_run if role == WORKER_ROLE else record.review_head_run
        intent = dict(record.launch_intent or {})
        if not isinstance(stored, dict) or not stored.get("run_id"):
            candidate = intent.get("head_run")
            stored = candidate if isinstance(candidate, dict) else {}
        if not stored.get("run_id"):
            return
        try:
            run = head_ops.HeadRun.from_json(stored)
        except (head_ops.HeadRunError, head_ops.TaskRefError):
            return
        if run.spec.adapter != "codex" or not isinstance(run.fanout_policy.get("provider_source"), dict):
            return

        def persist(updated: head_ops.HeadRun) -> None:
            if not updated.same_run(run):
                raise HostError("provider event writer was handed another HeadRun")
            updated_json = updated.to_json()
            if role == WORKER_ROLE:
                existing = record.worker_head_run
                if isinstance(existing, dict) and existing.get("run_id"):
                    updated_json = _merge_launch_head_run(existing, updated_json)
                record.worker_head_run = updated_json
                record.workspace = updated.workspace or record.workspace
                record.handle = updated.handle or record.handle
                record.worker_leaf = updated.leaf or record.worker_leaf
                record.worker_pid_file = updated.pid_file or record.worker_pid_file
            else:
                existing = record.review_head_run
                if isinstance(existing, dict) and existing.get("run_id"):
                    updated_json = _merge_launch_head_run(existing, updated_json)
                record.review_head_run = updated_json
                record.workspace = updated.workspace or record.workspace
                record.review_handle = updated.handle or record.review_handle
                record.review_leaf = updated.leaf or record.review_leaf
                record.review_pid_file = updated.pid_file or record.review_pid_file
            current_intent = dict(record.launch_intent or {})
            intent_run = current_intent.get("head_run")
            if isinstance(intent_run, dict) and str(intent_run.get("run_id") or "") == updated.run_id:
                current_intent["head_run"] = _merge_launch_head_run(intent_run, updated_json)
                record.launch_intent = current_intent
            records[reference] = record
            self.save_records(payload, records)

        def stop(updated: head_ops.HeadRun, reason: str) -> None:
            # The head operation re-reads the heartbeat identity before signalling. A mismatch is
            # swallowed here: the block still records the unknown source, no foreign process is hit.
            try:
                with self.host.committing(lambda: self.save_records(payload, records)):
                    self.host.stop_head(record, "worker" if role == WORKER_ROLE else "review")
            except HostError:
                return

        def block(evidence: dict[str, Any]) -> None:
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=reference,
                target="blocked",
                reason=(
                    "Codex provider fan-out policy blocked this head: "
                    f"{evidence.get('state') or 'unknown'}; {evidence.get('reason') or 'provider event observed'}"
                ),
                request_id=_attempt_request_id(
                    record.attempt_id, "codex-provider-event-blocked", reference, role, run.run_id
                ),
            )

        self.host.configure_codex_provider_ingress(run, persist=persist, stop=stop, block=block)

    def poll_codex_provider_ingress(
        self,
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        *,
        reference: str,
    ) -> dict[str, Any] | None:
        """Refresh advisory fan-out telemetry for recovered worker/reviewer runs."""
        for role, stored in (
            (WORKER_ROLE, record.worker_head_run),
            (REVIEW_ROLE, record.review_head_run),
        ):
            if not isinstance(stored, dict) or not stored.get("run_id"):
                continue
            try:
                run = head_ops.HeadRun.from_json(stored)
            except (head_ops.HeadRunError, head_ops.TaskRefError):
                continue
            if run.spec.adapter != "codex" or not isinstance(run.fanout_policy.get("provider_source"), dict):
                continue
            self.bind_codex_provider_ingress(record, records, payload, role=role, reference=reference)
            try:
                self.host.poll_codex_provider_ingress(run)
            except (CodexProviderSourceError, CodexFanoutRecordingError) as exc:
                return {
                    "status": "blocked",
                    "step": "codex-provider-event",
                    "pilot_ref": reference,
                    "attempt_id": record.attempt_id,
                    "policy_evidence": {"kind": "codex_provider_fanout", "state": "unknown"},
                    "reason": str(exc),
                }
        return None

    def pause_pipeline(
        self,
        *,
        mode: str,
        actor: str,
        reason: str,
        exclude_workspaces: list[str] | None = None,
    ) -> dict[str, Any]:
        return _pause_pipeline(
            self, mode=mode, actor=actor, reason=reason, exclude_workspaces=exclude_workspaces
        )

    def resume_pipeline(self, *, actor: str) -> dict[str, Any]:
        return _resume_pipeline(self, actor=actor)

    def pause_status(self) -> dict[str, Any]:
        return _pause_status(self)

    def production_observe(self) -> dict[str, Any]:
        return _production_observe(self)

    def production_tick(self) -> dict[str, Any]:
        return _production_tick(self)

    def production_probe(self) -> dict[str, Any]:
        return _production_probe(self)

    def production_run(
        self,
        *,
        interval_seconds: float,
        max_interval_seconds: float,
        max_ticks: int | None = None,
    ) -> dict[str, Any]:
        return _production_run(
            self,
            interval_seconds=interval_seconds,
            max_interval_seconds=max_interval_seconds,
            max_ticks=max_ticks,
        )

    def _tick_task(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
    ) -> dict[str, Any]:
        ref = task["ref"]
        # Staged usage obligations are deliberately not settled here: a card can finish its last
        # phase and leave `ACTIVE_STATES` in the same tick, so no per-card pass can be the site that
        # guarantees publication. `publish_pending_attempt_usage` owns that, over the whole pending
        # set, at the top of the production tick.
        #
        # A launch intent can outlive its tick. Re-establish the exact provider source before
        # adoption reads a heartbeat, not after a mismatched session was attributed to this card.
        record = records.get(ref)
        if record is not None:
            fanout = self.poll_codex_provider_ingress(record, records, payload, reference=ref)
            if fanout is not None:
                return fanout
        # A record carrying a launch intent is a bring-up whose tick did not live to record its
        # outcome. It is settled before anything else: until it is, neither "this card has a head"
        # nor "this card is headless" is known, and the wrong answer gives one workspace two heads.
        pending_launch = _resolve_launch_intent(self, task, records, payload)
        if pending_launch is not None:
            return pending_launch
        if task["state"] == "ready":
            resume_workspaces = payload.get("resume_workspaces")
            resume_workspace = isinstance(resume_workspaces, dict) and ref in resume_workspaces
            return self._claim(
                task,
                records,
                payload,
                attempt_id,
                resume_workspace=resume_workspace,
            )
        if task["state"] == "in_progress":
            return self._advance_worker(task, records, payload, attempt_id)
        if task["state"] == "validate":
            return self._advance_review(task, records, payload, attempt_id)
        if task["state"] == "assessment":
            return self._advance_assessment(task, records, payload, attempt_id)
        records.pop(ref, None)
        return {
            "status": "ok",
            "step": "tick",
            "action": "terminal-state",
            "state": task["state"],
            "pilot_ref": ref,
            "attempt_id": attempt_id,
        }

    def _failover_collapse(self, worker: HeadChoice, review: HeadChoice) -> dict[str, Any] | None:
        """The refusal when a failover would hand both roles to one head, else None.

        Only a failover can collapse the pair here: two roles pointed at one head by the canon itself
        is an installation's own decision and is not overruled.
        """
        if not review.resolved or review.head != worker.head:
            return None
        if not (worker.substituted or review.substituted):
            return None
        return {
            "status": "skipped",
            "step": "head-preflight",
            "action": CLAIM_SKIP_FAILOVER_COLLAPSE,
            "head": worker.head,
            "review_head": review.head,
            "readiness": worker.readiness.to_json(),
            "reason": (
                f"failover would run worker and reviewer on the same head {worker.head}: "
                f"worker {worker.reason}; reviewer {review.reason}"
            ),
            "failover": {"worker": worker.to_json(), "review": review.to_json()},
        }

    def _comment_head_failover(
        self, ref: str, attempt_id: str, worker: HeadChoice, review: HeadChoice
    ) -> None:
        """Write the substitution onto the card, once per claim, or do nothing."""
        lines = [
            f"{role} head {choice.head} instead of {choice.preferred}: {choice.reason}"
            for role, choice in (("Worker", worker), ("Reviewer", review))
            if choice.substituted
        ]
        if not lines:
            return
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            body="Head failover at claim. " + " ".join(lines),
            request_id=_attempt_request_id(attempt_id, "head-failover-comment", ref),
        )

    def _broad_check_contract_verdict(self, task: dict[str, Any]) -> ContractVerdict:
        """This card's broad-check contract, as one of the three named states (secretary-1458).

        Offline and cheap: the binding and the adapter beside it, read before anything is claimed.
        Every way of not getting an answer is a named state rather than a fall-through, because a
        fall-through is what "nothing came back, so the card may go" was made of. A card that names
        no registered project, and a project this installation cannot look up at all, are open
        questions about the registry — the paths that need the binding fail on it in their own
        words — and they are returned as such, not as approval.
        """
        project = str(task.get("project") or "")
        if not project:
            return ContractVerdict.as_undecidable(
                UNDECIDABLE_NO_REGISTERED_PROJECT,
                "",
                f"card {task.get('ref')!r} names no registered project, so it has no adapter and "
                "no broad-check contract to judge",
            )
        try:
            return self.catalog.broad_check_verdict(project)
        except HostError as exc:
            return ContractVerdict.as_undecidable(
                UNDECIDABLE_PROJECT_UNAVAILABLE,
                "",
                f"registered project {project!r} could not be read: {exc}",
            )

    def _contract_preflight_decision(
        self,
        task: dict[str, Any],
        verdict: ContractVerdict,
        *,
        attempt_id: str,
        head: str,
        review_head: str,
    ) -> tuple[BringUpFailure, str, ContractUnusable] | None:
        """What the verdict buys this card: the outcome that stops it, or None to issue it.

        Exhaustive over the three states by name, with no default branch that lets an unrecognised
        answer through as permission. Which state buys what is `projects.contract`'s decision and
        is only carried out here:

        * `refused` stops the card before it is issued — that is the guarantee the card exists for;
        * `undecidable` issues it, because the open question is a documented compatibility promise
          (a relative interpreter is resolved from a workspace that does not exist yet) and the
          side that will hold that tree answers it there. It is a decision with a name, not the
          absence of one;
        * `fit` issues it, as always.
        """
        if verdict.state == CONTRACT_REFUSED and verdict.refusal is not None:
            failure, reason = self._contract_preflight_outcome(
                task,
                attempt_id=attempt_id,
                head=head,
                review_head=review_head,
                refusal=verdict.refusal,
            )
            return failure, reason, verdict.refusal
        if verdict.state in (CONTRACT_FIT, CONTRACT_UNDECIDABLE):
            return None
        raise HostError(f"unreadable broad-check contract verdict {verdict.state!r}")

    def _contract_preflight_outcome(
        self,
        task: dict[str, Any],
        *,
        attempt_id: str,
        head: str,
        review_head: str,
        refusal: ContractUnusable,
    ) -> tuple[BringUpFailure, str]:
        """The typed infrastructure outcome for a card nobody can broad-check, decided before claim.

        Pure: it turns the refusal the preflight already read off the registry into the class, the
        evidence and the card's Blocked reason, and touches neither the board, the host nor the
        filesystem. That is what lets the claim and the transition it is the door to stand next to
        each other with nothing that can fail in between.

        This is the same outcome a bring-up that produced no head carries, made by the same
        classifier and written with the same durable action token: an installation whose registry
        cannot supply a usable contract is a failure of the host, not a verdict about the card. The
        two properties the card must have follow from that token alone rather than from anything
        here — the sprint budget reads it and counts the block as uncharged, and a block is not a
        retry, so no new attempt is opened and nothing is scheduled to come back.
        """
        detail = (
            f"the broad-check contract of registered project {task.get('project')!r} cannot "
            f"attest this card: {refusal.detail()}"
        )
        # The card has no record and will get none. The classifier reads one only to count the
        # bring-up attempts of a pane that was never ready, which this failure is not; the claim's
        # own identity is what the outcome carries.
        unclaimed = DispatcherRecord(
            worker=_worker_id(task),
            workspace="",
            handle="",
            head=head,
            review_head=review_head,
            attempt_id=attempt_id,
            comment_baseline=0,
            review_baseline=0,
            state="",
            claimed_at=0.0,
        )
        failure = _classify_bring_up_failure(
            None,
            unclaimed,
            WORKER_ROLE,
            stage=STAGE_CLAIM,
            attempt_id=attempt_id,
            detail=detail,
        )
        reason = (
            "the card was not given to a worker: this project's broad-check contract cannot "
            f"attest it, so no workspace and no head were created. {detail}\n{failure.clause()}"
        )
        return failure, reason

    def _contract_preflight_blocked(
        self,
        ref: str,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        *,
        attempt_id: str,
        refusal: ContractUnusable,
        failure: BringUpFailure,
        reason: str,
    ) -> dict[str, Any]:
        """Write the outcome decided before the claim, immediately after it.

        Nothing is computed here and nothing is read: the transition is the first statement made
        about the claimed card, and the dispatcher's own bookkeeping only follows it.
        """
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=reason,
            request_id=_attempt_request_id(
                attempt_id,
                _bring_up_blocked_action("contract-preflight-blocked", failure),
                ref,
            ),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {
            "status": "blocked",
            "step": "contract-preflight",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "reason": "broad-check contract preflight failed",
            "contract_refusal": refusal.evidence(),
            **failure.outcome_fields(reason),
        }

    def _claim(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        resume_workspace: bool = False,
    ) -> dict[str, Any]:
        ref = task["ref"]
        # Both heads are decided here, before anything is claimed, and both may be decided against
        # the card's preference. Nothing launchable at the end of either walk is a claim-skip: the
        # card stays in Ready and the outcome below names the dead resource.
        worker_choice = self.resolve_head(self.catalog.worker_head(task))
        if not worker_choice.resolved:
            return {
                "status": "skipped",
                "step": "head-preflight",
                "action": CLAIM_SKIP_RESOURCE_NOT_READY,
                "pilot_ref": ref,
                "head": worker_choice.preferred,
                "readiness": worker_choice.readiness.to_json(),
                "reason": worker_choice.reason,
                "failover": {"worker": worker_choice.to_json()},
            }
        review_choice = self.resolve_head(self.catalog.review_head(task))
        collapse = self._failover_collapse(worker_choice, review_choice)
        if collapse is not None:
            return dict(collapse, pilot_ref=ref)
        head = worker_choice.head
        review_head = review_choice.head or review_choice.preferred
        contract_verdict = self._broad_check_contract_verdict(task)
        # A card the dispatcher still holds a record for, back in Ready with its claim already
        # committed under the current attempt, is a re-run. An attempt id otherwise lives as long as
        # the record, so the claim would replay idempotently, return the old event and leave the card
        # Ready: every re-run gets a fresh identity before claiming. A committed claim with no record
        # is a genuine board divergence and still fails closed below.
        active = records.get(ref)
        requeued = active is not None
        retry_after_block = resume_workspace or any(
            self.audit.committed_event(_attempt_request_id(attempt_id, action, ref)) is not None
            for action in _blocked_actions_and_their_infrastructure_twins(
                "bringup-blocked",
                "worker-result-blocked",
                "worker-blocked",
                "worker-respawn-blocked",
                "worker-wait-stall",
                "rework-blocked",
                "contract-preflight-blocked",
                "gate-blocked",
                "gate-red-blocked",
                "gate-pending-stall",
                "merge-gate-blocked",
                "merge-gate-red-blocked",
                "merge-blocked",
                "release-drift-blocked",
                "release-failed-blocked",
                "review-blocked",
                "review-freeze-red-blocked",
                "review-inventory-blocked",
                "review-wait-stall",
                "stale-done-rework-blocked",
            )
        )
        if requeued and active is not None:
            # The preempted head can still be in the workspace the next round claims, and it is
            # stopped through the workspace, not the handle: an adopted head has no handle on record.
            if active.owns_head("review"):
                # A preempt out of Validate leaves the worker pane closed by `start_review` but the
                # reviewer up; left alone its verdict would land on the new attempt.
                unconfirmed = self._end_review_pane_confirmed(
                    active,
                    records,
                    payload,
                    ref,
                    step="claim",
                    attempt_id=attempt_id,
                    initiator=STOPPED_BY_REPLACEMENT,
                )
                if unconfirmed is not None:
                    return unconfirmed
            if active.needs_settling():
                unconfirmed = self._stop_worker_confirmed(active, ref, step="claim", attempt_id=attempt_id)
                if unconfirmed is not None:
                    return unconfirmed
        if retry_after_block or requeued:
            attempt_id = _new_attempt_id()
            _record_attempt(payload, attempt_id, ref, self.owner, self.owner)
            payload["attempt_id"] = attempt_id
        claim_request_id = _attempt_request_id(attempt_id, "claim", ref)
        worker_id = _worker_id(task)
        # Claim is the only board transition that can record a Ready refusal.
        contract_outcome = self._contract_preflight_decision(
            task,
            contract_verdict,
            attempt_id=attempt_id,
            head=head,
            review_head=review_head,
        )
        self.writer.claim(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            worker=worker_id,
            resolved_head=head,
            resolved_review_head=review_head,
            slug=task.get("workspace", {}).get("slug") or "",
            base_branch=task.get("workspace", {}).get("base_branch") or "",
            request_id=claim_request_id,
        )
        if contract_outcome is not None:
            failure, blocked_reason, refusal = contract_outcome
            return self._contract_preflight_blocked(
                ref,
                records,
                payload,
                attempt_id=attempt_id,
                refusal=refusal,
                failure=failure,
                reason=blocked_reason,
            )
        self._comment_head_failover(ref, attempt_id, worker_choice, review_choice)
        claimed = self.reader.show(ref)
        record = DispatcherRecord(
            worker=worker_id,
            workspace="",
            handle="",
            head=head,
            review_head=review_head,
            attempt_id=attempt_id,
            comment_baseline=len(claimed.get("comments") or []),
            review_baseline=0,
            report_generation=1,
            state="claim_verified",
            claimed_at=time.time(),
            preferred_head=worker_choice.preferred if worker_choice.substituted else "",
            preferred_review_head=(review_choice.preferred if review_choice.substituted else ""),
        )
        self.open_worker_round(record, round_number=self._journal_round(ref) + 1)
        records[ref] = record
        self.save_records(payload, records)
        return self._launch_worker_after_claim(
            claimed,
            record,
            records,
            payload,
            require_existing_workspace=retry_after_block,
        )

    def _launch_worker_after_claim(
        self,
        claimed: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        *,
        require_existing_workspace: bool = False,
    ) -> dict[str, Any]:
        ref = claimed["ref"]
        mismatch = _claim_mismatch(claimed, record.worker, record.head, record.review_head)
        if mismatch:
            divergence = _record_divergence(
                payload,
                record.attempt_id,
                ref,
                "claim",
                "claim_live_mismatch",
                expected={
                    "state": "in_progress",
                    "worker": record.worker,
                    "resolved_head": record.head,
                    "resolved_review_head": record.review_head,
                },
                actual=_claim_actual(claimed),
                details=mismatch,
            )
            return {
                "status": "blocked",
                "step": "claim",
                "pilot_ref": ref,
                "attempt_id": record.attempt_id,
                "reason": "claim live board mismatch",
                "divergence_id": divergence["id"],
            }
        live_head = _head_process_status(_launch_pid_file(WORKER_ROLE, ref))
        if live_head.get("known") and live_head.get("alive"):
            # This record belongs to the claim being opened now, so it has no HeadRun that can prove
            # the pre-existing heartbeat is its own. Signalling that workspace would turn an absence
            # of ownership into permission to stop it: keep the claim and make the ambiguity visible.
            return {
                "status": "degraded",
                "step": "claim",
                "action": "orphan-worker-heartbeat-unbound",
                "pilot_ref": ref,
                "attempt_id": record.attempt_id,
                "reason": "a live worker heartbeat has no durable HeadRun binding for this claim",
            }
        # The workspace is asked of the host rather than taken from its answer: with it and the pid
        # file the next tick can stop a head whose handle a tick dying mid-launch never recorded.
        failure = _write_launch_intent(
            self,
            payload,
            records,
            ref,
            record,
            role=WORKER_ROLE,
            action="claim",
            head=record.head,
            workspace=self.host.restore_workspace(claimed, record.worker),
        )
        if failure is not None:
            if failure.startswith("codex-fanout-policy:"):
                # No terminal was created. This is policy evidence, not a transient failure worth
                # retrying: a later tick with the same schema is the same prohibited launch.
                self.writer.move(
                    role="dispatcher",
                    actor=self.owner,
                    reference=ref,
                    target="blocked",
                    reason=f"Codex provider fan-out policy refused worker preflight: {failure}",
                    request_id=_attempt_request_id(record.attempt_id, "codex-fanout-blocked", ref),
                )
                records.pop(ref, None)
                self.save_records(payload, records)
                return {
                    "status": "blocked",
                    "step": "claim",
                    "pilot_ref": ref,
                    "attempt_id": record.attempt_id,
                    "policy_evidence": {"kind": "codex_provider_fanout", "state": "unknown"},
                    "reason": failure,
                }
            return _launch_intent_unwritable(
                step="claim", ref=ref, attempt_id=record.attempt_id, role=WORKER_ROLE, reason=failure
            )
        # The launch intent already contains the exact preflight HeadRun. Bind its provider source
        # before `prepare_worker` can create a pane, not after TASK.md has been delivered.
        self.bind_codex_provider_ingress(
            record,
            records,
            payload,
            role=WORKER_ROLE,
            reference=ref,
        )
        try:
            prepared = self.host.prepare_worker(
                claimed,
                record.worker,
                record.head,
                attempt_id=record.attempt_id,
                require_existing_workspace=require_existing_workspace,
                generation=record.report_generation,
                failover=bool(record.preferred_head),
                heartbeat_run_id=str((record.launch_intent or {}).get("run_id") or ""),
            )
        except (HeadLaunchAborted, HostError) as exc:
            aborted = self._worker_launch_failure(
                payload, records, ref, record, exc, step="claim", attempt_id=record.attempt_id
            )
            if aborted is not None:
                return aborted
            _clear_launch_intent(record)
            deferred = _launch_deferred(
                record,
                exc,
                step="claim",
                ref=ref,
                attempt_id=record.attempt_id,
                role=WORKER_ROLE,
            )
            if deferred is not None:
                records[ref] = record
                self.save_records(payload, records)
                return deferred
            # An infrastructure outcome blocks for a person; it is not a new attempt.
            failure = _classify_bring_up_failure(
                exc, record, WORKER_ROLE, stage=STAGE_CLAIM, attempt_id=record.attempt_id
            )
            reason = _bring_up_blocked_reason(
                "dispatcher bring-up failed", exc, record, WORKER_ROLE, failure=failure
            )
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="blocked",
                reason=reason,
                request_id=_attempt_request_id(
                    record.attempt_id, _bring_up_blocked_action("bringup-blocked", failure), ref
                ),
            )
            records.pop(ref, None)
            self.save_records(payload, records)
            return {
                "status": "blocked",
                "step": "claim",
                "pilot_ref": ref,
                "reason": "host bring-up failed",
                **failure.outcome_fields(reason),
            }
        record.workspace = prepared["workspace"]
        _record_worker_delivery_evidence(record, prepared.get("delivery_evidence"))
        # The intent carries the pane, the launch snapshot and this head's own run before the record
        # is told anything else: from here every failure is one over a worker that is already running.
        _confirm_launch_intent(
            self,
            payload,
            records,
            ref,
            record,
            handle=str(prepared.get("handle") or ""),
            leaf=str(prepared.get("leaf") or ""),
            run=prepared.get("run"),
            head_run=dict(prepared.get("head_run") or {}),
        )
        try:
            self._settle_worker_pane(
                ref,
                record,
                str(prepared.get("handle") or ""),
                str(prepared.get("leaf") or ""),
            )
        except HeadLaunchAborted as exc:
            return self._worker_launch_aborted(
                payload, records, ref, record, exc, step="claim", attempt_id=record.attempt_id
            )
        record.worker_started_at = record.worker_progress_at = time.time()
        record.state = "claimed"
        _reset_launch_attempts(record, WORKER_ROLE)
        resume_workspaces = payload.get("resume_workspaces")
        if isinstance(resume_workspaces, dict):
            resume_workspaces.pop(ref, None)
        records[ref] = record
        self.save_records(payload, records)
        # The worker is up: record the head running it from the launcher's own snapshot. An adopted
        # claim predating routing telemetry has no round, so this opens one from the journal. Spend
        # the intent only once that lands: a refusal leaves the head adoptable and its routing owed.
        self.record_worker_routing(claimed, record, prepared.get("run"))
        _clear_launch_intent(record)
        self.save_records(payload, records)
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            body=(
                f"Production dispatcher claimed {ref}, attempt {record.attempt_id}, "
                f"worker {record.worker}, workspace {prepared['workspace']}."
            ),
            request_id=_attempt_request_id(record.attempt_id, "claimed-comment", ref),
        )
        outcome = {
            "status": "ok",
            "step": "claim",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "worker": record.worker,
            "workspace": prepared["workspace"],
            "head": record.head,
            "review_head": record.review_head,
        }
        if record.preferred_head or record.preferred_review_head:
            # The tick says a head was substituted in the same line that says the card was claimed:
            # an operator must not have to open the card to see the work runs elsewhere.
            outcome["preferred_head"] = record.preferred_head
            outcome["preferred_review_head"] = record.preferred_review_head
        return outcome

    def _end_review_pane_confirmed(
        self,
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        ref: str,
        *,
        step: str,
        attempt_id: str,
        initiator: str,
    ) -> dict[str, Any] | None:
        """End the reviewer before a replacement head opens. Returns the tick's outcome on refusal."""
        try:
            _end_review_pane(self.host, record, initiator)
        except HostError as exc:
            return _head_stop_unconfirmed(
                step=step,
                ref=ref,
                attempt_id=record.attempt_id or attempt_id,
                role="review",
                reason=scrub_host_output(str(exc)),
            )
        return None

    def _stop_worker_confirmed(
        self,
        record: DispatcherRecord,
        ref: str,
        *,
        step: str,
        attempt_id: str,
    ) -> dict[str, Any] | None:
        """Stop this card's worker head before a replacement opens, or answer with the refusal."""
        try:
            if record.handle or record.worker_leaf or record.worker_pid_file:
                self.host.stop_head(record, "worker", STOPPED_BY_REPLACEMENT)
            else:
                # A preempted head can lose its own identity with a dispatcher crash while the
                # workspace is still known. An unnamed writer is ambiguity, never evidence.
                self.host.stop_workspace(record)
        except HostError as exc:
            return _head_stop_unconfirmed(
                step=step,
                ref=ref,
                attempt_id=record.attempt_id or attempt_id,
                role=WORKER_ROLE,
                reason=scrub_host_output(str(exc)),
            )
        _forget_role_head(record, WORKER_ROLE)
        # The session is gone; a red transition already opened over it is not, and is not dropped.
        record.worker_continuation.drop_session()
        return None

    def _worker_launch_aborted(
        self,
        payload: dict[str, Any],
        records: dict[str, DispatcherRecord],
        ref: str,
        record: DispatcherRecord,
        exc: HeadLaunchAborted,
        *,
        step: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        """A worker bring-up that failed with its terminal already open."""
        _mark_launch_aborted(self, payload, records, ref, record, exc)
        return _launch_aborted(
            step=step,
            ref=ref,
            attempt_id=record.attempt_id or attempt_id,
            role=WORKER_ROLE,
            reason=scrub_host_output(str(exc)),
        )

    def _worker_launch_failure(
        self,
        payload: dict[str, Any],
        records: dict[str, DispatcherRecord],
        ref: str,
        record: DispatcherRecord,
        exc: Exception,
        *,
        step: str,
        attempt_id: str,
    ) -> dict[str, Any] | None:
        """The aborted-launch outcome when this failure may have left a worker running, else None."""
        _record_worker_delivery_evidence(record, exc, failure=True)
        if not isinstance(exc, HeadLaunchAborted):
            if not _launch_left_a_head(record):
                return None
            exc = HeadLaunchAborted(
                str(exc),
                workspace=record.workspace,
                pid_file=_launch_pid_file(WORKER_ROLE, ref),
                evidence=_delivery_evidence_json(exc, "worker-launch"),
            )
        return self._worker_launch_aborted(
            payload, records, ref, record, exc, step=step, attempt_id=attempt_id
        )

    def _settle_worker_pane(self, ref: str, record: DispatcherRecord, handle: str, leaf: str) -> None:
        """Put the pane identity of a worker head that is already up onto its record."""
        record.handle = handle
        record.worker_leaf = leaf

    def _bring_up_worker_head(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        step: str,
        stage: str,
        blocked_reason: str,
        blocked_action: str,
        blocked_request_suffix: str = "",
    ) -> tuple[LaunchedHead | None, dict[str, Any] | None]:
        """Relaunch this card's worker in its own workspace, under the intent already on disk.

        The blocked transition is named rather than handed in whole, because the action token is
        where the outcome's class becomes durable: only the shared classifier below decides which
        of the two tokens this relaunch writes.
        """
        ref = task["ref"]
        try:
            self._require_head_ready(record.head)
            self.bind_codex_provider_ingress(
                record,
                records,
                payload,
                role=WORKER_ROLE,
                reference=ref,
            )
            launched = self.host.restart_worker(
                task, record, heartbeat_run_id=str((record.launch_intent or {}).get("run_id") or "")
            )
        except Exception as exc:  # noqa: BLE001 — classified by what it left running, not by type
            aborted = self._worker_launch_failure(
                payload, records, ref, record, exc, step=step, attempt_id=attempt_id
            )
            if aborted is not None:
                return None, aborted
            intent = dict(_launch_intent(record))
            _clear_launch_intent(record)
            deferred = _launch_deferred(
                record,
                exc,
                step=step,
                ref=ref,
                attempt_id=record.attempt_id or attempt_id,
                role=WORKER_ROLE,
            )
            if deferred is not None:
                # A rework reserved its round before the host call, and that round is over whether
                # or not its head lived: the deferred relaunch belongs to the round the rework opened.
                _keep_reserved_round(self, record, intent)
                # Nothing of this launch is running and the record names no head, so the next tick retries.
                records[ref] = record
                self.save_records(payload, records)
                return None, deferred
            return None, self._block_failed_worker_restart(
                ref=ref,
                record=record,
                records=records,
                payload=payload,
                attempt_id=attempt_id,
                step=step,
                stage=stage,
                reason=blocked_reason,
                action=blocked_action,
                request_suffix=blocked_request_suffix,
                error=exc,
            )
        # The head is up. Its pane, launch configuration and own run go into the intent before
        # anything else, so an adoption gets the run that launched rather than a fresh identity.
        _confirm_launch_intent(
            self,
            payload,
            records,
            ref,
            record,
            handle=launched.handle,
            leaf=launched.leaf,
            run=launched.run,
            head_run=dict(launched.head_run),
        )
        _record_worker_delivery_evidence(record, launched.delivery_evidence)
        try:
            self._settle_worker_pane(ref, record, launched.handle, launched.leaf)
        except HeadLaunchAborted as exc:
            return None, self._worker_launch_aborted(
                payload, records, ref, record, exc, step=step, attempt_id=attempt_id
            )
        _reset_launch_attempts(record, WORKER_ROLE)
        return launched, None

    def _worker_relaunch_intent(
        self,
        payload: dict[str, Any],
        records: dict[str, DispatcherRecord],
        ref: str,
        record: DispatcherRecord,
        *,
        action: str,
        round_number: int | None = None,
    ) -> str | None:
        """Fix a rework or respawn bring-up on disk before `restart_worker` is called."""
        return _write_launch_intent(
            self,
            payload,
            records,
            ref,
            record,
            role=WORKER_ROLE,
            action=action,
            head=record.head,
            workspace=record.workspace,
            round_number=round_number,
        )

    def _advance_worker(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
    ) -> dict[str, Any]:
        ref = task["ref"]
        record = records.get(ref)
        if record is None:
            try:
                record = self._adopt(task, attempt_id)
            except HostError as exc:
                return self._block_unresumable(task, records, payload, attempt_id, "advance", exc)
            records[ref] = record
            if record.worker_head_run:
                # A lost record can recover the live worker only from that worker's own launch
                # identity, already bound and re-checked by `_adopt`: this is a continuation of its
                # HeadRun, so a report written before the record was lost still advances the card here.
                record.state = "claimed"
            else:
                current_claim = _attempt_request_id(attempt_id, "claim", ref)
                if self.audit.committed_event(current_claim) is not None:
                    mismatch = _claim_mismatch(task, record.worker, record.head, record.review_head)
                    if not mismatch:
                        record.state = "claim_verified"
                        self.save_records(payload, records)
                        return self._launch_worker_after_claim(task, record, records, payload)
        if record.worker_continuation.red_transition_pending:
            # An open red transition outranks everything else. The board move may or may not have
            # committed before its tick died, so it is finished against the board as it is now.
            return self._complete_red_transition(task, record, records, payload, attempt_id, ref=ref)
        if record.state == "claim_verified":
            return self._launch_worker_after_claim(task, record, records, payload)
        # The round the dispatcher is holding, not merely the card's last report marker: a marker is
        # attributed to a round through the request id its command carried, which the audit keeps.
        marker = _round_report_marker(
            self.audit,
            ref,
            _round_report_ids(
                record.workspace, record.attempt_id or attempt_id, ref, record.report_generation
            ),
        )
        continuation = record.worker_continuation
        if continuation.delivery_pending:
            if marker in {"report:done", "report:blocked"}:
                # A report after the resume phase opened proves the continuation reached the retained
                # conversation: do not rewrite TASK.md or replay the prompt over a completed turn.
                continuation.confirm_delivery()
                records[ref] = record
                self.save_records(payload, records)
                return self._finish_retained_worker_resume(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    phase=continuation.phase or "gate",
                )
            # Progress is sampled before the persisted readiness backoff is interpreted. A new
            # provider cursor beats a busy pane and resets only that ladder, never the HeadRun.
            now = time.time()
            provider_observation = self._observe_retained_continuation_progress(task, record, now=now)
            blocked = self._block_unadmitted_continuation_liveness(
                task,
                record,
                records,
                payload,
                attempt_id,
                phase=continuation.phase or "gate",
                observation=provider_observation,
            )
            if blocked is not None:
                return blocked
            fresh_provider_progress = provider_observation == "progressed"
            pending = self._continuation_recovery_window(
                task,
                record,
                records,
                payload,
                attempt_id,
                phase=continuation.phase or "gate",
                fresh_provider_progress=fresh_provider_progress,
                now=now,
            )
            if pending is not None:
                return pending
            records[ref] = record
            self.save_records(payload, records)
            if (
                fresh_provider_progress
                and record.worker_continuation_liveness.recovery_rung
                != ContinuationRecoveryRung.SAFE_RECOVERY_RESUME_ONCE
            ):
                continuation.busy_next_at = now + BUSY_RETRY_INITIAL_SECONDS
                return _retained_worker_busy_deferred(
                    ref,
                    record,
                    attempt_id,
                    continuation.phase or "gate",
                    delay=BUSY_RETRY_INITIAL_SECONDS,
                )
            if not continuation.busy_retry_due(time.time()):
                return _retained_worker_busy_deferred(ref, record, attempt_id, continuation.phase or "gate")
            # Nothing is woken from here: the suspension is a fact of the tick that died, and
            # re-entering the transition is what asks the heartbeat again before reopening.
            return self._deliver_red_continuation(
                task, record, records, payload, attempt_id, phase=continuation.phase or "gate"
            )
        if continuation.delivery_confirmed:
            # The delivery was checkpointed and the tick died before the round it opened was
            # recorded; finishing it again keeps the rework off the round the verdict closed.
            return self._finish_retained_worker_resume(
                task, record, records, payload, attempt_id, phase=continuation.phase or "gate"
            )
        if marker == "report:done":
            if continuation.validation_move_pending:
                # Frozen and recorded before a tick died mid-move; the replay never wakes the worker.
                # The phase this report closed is the same one the dying tick accepted, so its
                # usage occurrence is finished here rather than lost with that tick.
                self.record_attempt_usage(ref, record, role=WORKER_ROLE, attempt_id=attempt_id)
                self.writer.move(
                    role="dispatcher",
                    actor=self.owner,
                    reference=ref,
                    target="validate",
                    reason="worker report:done",
                    request_id=_attempt_request_id(
                        record.attempt_id or attempt_id,
                        "worker-done",
                        ref,
                        str(record.report_generation),
                    ),
                )
                record.state = "validate"
                self.save_records(payload, records)
                return {
                    "status": "ok",
                    "step": "advance",
                    "pilot_ref": ref,
                    "attempt_id": attempt_id,
                    "to": "validate",
                }
            try:
                self.host.verify_worker_result(task, record)
            except HostError as exc:
                unconfirmed = self._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
                if unconfirmed is not None:
                    return unconfirmed
                self.writer.move(
                    role="dispatcher",
                    actor=self.owner,
                    reference=ref,
                    target="blocked",
                    reason=f"worker result is not durable: {scrub_host_output(str(exc))}",
                    request_id=_attempt_request_id(
                        record.attempt_id or attempt_id, "worker-result-blocked", ref
                    ),
                )
                records.pop(ref, None)
                return {
                    "status": "blocked",
                    "step": "advance",
                    "pilot_ref": ref,
                    "attempt_id": attempt_id,
                    "reason": "worker result is not durable",
                }
            current_sha = self.host.head_commit(record)
            retry_stale_no_diff_gate = False
            reuse_report_only_gate = False
            if current_sha and current_sha == record.rejected_sha:
                if record.rejected_failure_class == "infrastructure":
                    return self._accept_stale_infrastructure_done(
                        task,
                        record,
                        records,
                        payload,
                        attempt_id,
                        current_sha,
                    )
                retry_stale_no_diff_gate = self._can_retry_stale_no_diff_research_gate(
                    task, record, current_sha
                )
                reuse_report_only_gate = self._can_reuse_report_only_rework_gate(task, record, current_sha)
                if not (retry_stale_no_diff_gate or reuse_report_only_gate):
                    return self._reject_stale_done(task, record, records, payload, attempt_id, current_sha)
            # A no-diff research card gets one post-freeze chance to observe the dispatch it
            # already owns.  Count that report before Validate so a persistent wrong-SHA result
            # cannot reopen this exception forever: the next unchanged done report must still
            # reach _reject_stale_done's human-escalation bound.
            record.rejected_done_reports = 1 if retry_stale_no_diff_gate else 0
            # The report is accepted from here on. Account the worker phase it closes while the
            # head that wrote it is still on the record with its bound provider session.
            self.record_attempt_usage(ref, record, role=WORKER_ROLE, attempt_id=attempt_id)
            # The fresh worker-to-Validate round boundary. Whatever the previous round was judged
            # over ends here: the round this report opens binds its own candidate and base, and
            # inherits neither half.
            open_review_round(record, len(task.get("comments") or []))
            # Freeze before moving the board. A later tick may finish the idempotent move, but it
            # never leaves a completed worker writing while CI or a reviewer owns this checkout.
            try:
                self.host.retain_worker(record)
                continuation.begin_retention(time.time())
            except HostError:
                # A worker with no reusable conversation is still made safe by a confirmed stop.
                unconfirmed = self._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
                if unconfirmed is not None:
                    return unconfirmed
            if not reuse_report_only_gate:
                # Fresh code state: the mechanical gate must re-run before this report reaches review.
                record.gate_state = ""
                record.gate_pending_since = 0.0
                record.gate_transport_failures = 0
                record.gate_transport_error = ""
                self._reset_infrastructure_reruns(record)
            else:
                # No gate will run for this round, so this is where its context is bound instead.
                # The receipt still standing is the initial one this unchanged candidate passed —
                # a report-only correction reaches here only after a red review, which runs no
                # gate of its own and therefore replaces nothing.
                blocked = self._bind_review_context(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    step="advance",
                    receipt=GateReceipt.accept(record.gate_attestation, current_sha=current_sha),
                )
                if blocked is not None:
                    return blocked
            _reset_wait(record, "worker")
            _reset_wait(record, "review")
            records[ref] = record
            self.save_records(payload, records)
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="validate",
                reason="worker report:done",
                # Keyed on the generation the report closes, so this move and its replay after a
                # crash carry one id whatever the card's comment count has done since.
                request_id=_attempt_request_id(
                    record.attempt_id or attempt_id, "worker-done", ref, str(record.report_generation)
                ),
            )
            if continuation.validation_move_pending:
                continuation.confirm_validation_move()
            record.state = "validate"
            self.save_records(payload, records)
            return {
                "status": "ok",
                "step": "advance",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "to": "validate",
            }
        if marker == "report:blocked":
            # Before the stop, so the phase is accounted while its head is still described here.
            self.record_attempt_usage(ref, record, role=WORKER_ROLE, attempt_id=attempt_id)
            unconfirmed = self._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
            if unconfirmed is not None:
                return unconfirmed
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="blocked",
                reason="worker report:blocked",
                request_id=_attempt_request_id(record.attempt_id or attempt_id, "worker-blocked", ref),
            )
            records.pop(ref, None)
            return {
                "status": "ok",
                "step": "advance",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "to": "blocked",
            }
        watchdog = self._wait_watchdog(task, record, records, payload, attempt_id, kind="worker")
        if watchdog is not None:
            return watchdog
        return {
            "status": "ok",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "waiting-worker-report",
        }

    def _can_reuse_report_only_rework_gate(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        current_sha: str,
        *,
        observer_rework: bool | None = None,
    ) -> bool:
        """Whether an observer-directed research report correction may keep its green gate.

        A red review normally invalidates the gate for the next worker report. The one exception is
        a research round that the observer reopened to correct the report alone: its non-empty
        frozen decision proves that this is that round, and the receipt remains usable only when it
        still validates the exact rejected candidate. No marker body or worker-local check can stand
        in for the persisted dispatcher receipt here.
        """
        if observer_rework is None:
            observer_rework = bool(record.report_decision.strip())
        return (
            task.get("type") == "research"
            and observer_rework
            and record.rejected_failure_reason == "red-review"
            and bool(current_sha)
            and current_sha == record.rejected_sha
            and record.gate_state == "green"
            and bool(_gate_attestation_for_prompt(record, current_sha))
        )

    def _can_retry_stale_no_diff_research_gate(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        current_sha: str,
    ) -> bool:
        """Whether a stale no-diff gate may retry after the worker is frozen.

        The initial no-diff poll can see another branch's workflow-dispatch run before this card's
        own dispatch becomes visible. A fresh report is not evidence by itself: this narrow
        persisted-dispatch match only lets the ordinary, post-retention gate poll that request.
        Every other answer, including an old persisted receipt, takes the stale-done path.
        """
        return bool(
            not record.rejected_done_reports
            and self._is_stale_no_diff_research_gate_recovery(task, record, current_sha)
        )

    def _is_stale_no_diff_research_gate_recovery(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        current_sha: str,
    ) -> bool:
        """Whether this record identifies the narrow stale workflow-dispatch recovery."""
        dispatch = record.gate_workflow_dispatch
        return bool(
            task.get("type") == "research"
            and _validation_ci(self.host, task) == "github"
            and record.rejected_failure_class == "substantive"
            and record.rejected_failure_reason == "workflow-dispatch-head-sha-mismatch"
            and bool(current_sha)
            and current_sha == record.rejected_sha
            and isinstance(dispatch, dict)
            and dispatch.get("sha") == current_sha
            and dispatch.get("workflow") == "ci.yml"
        )

    def _accept_stale_infrastructure_done(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        sha: str,
    ) -> dict[str, Any]:
        """Let an infra-red SHA retry the gate without opening a no-op worker round.

        This is deliberately beside ``_reject_stale_done``: that safeguard remains intact for a
        red review and a substantive gate.  The class was persisted from the gate result, so this
        branch neither parses a card comment nor trusts a manual flag.
        """
        ref = task["ref"]
        if record.rejected_done_reports:
            return self._block_repeated_infrastructure_done(
                task,
                record,
                records,
                payload,
                attempt_id,
                sha,
            )
        # The report is accepted here, for the same round the first one opened: the occurrence
        # that round already owns is what a repeated report returns, not a second account.
        self.record_attempt_usage(ref, record, role=WORKER_ROLE, attempt_id=attempt_id)
        try:
            self.host.retain_worker(record)
            record.worker_continuation.begin_retention(time.time())
        except HostError:
            unconfirmed = self._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
            if unconfirmed is not None:
                return unconfirmed
        # A legacy/recovered record can still hand the worker an infra-classified stale SHA.  One
        # report is enough to return it to the real gate rerun path; another identical report has
        # no new evidence and must not reuse the same request id as a silent no-op tick.
        record.rejected_done_reports = 1
        record.gate_state = ""
        record.gate_pending_since = 0.0
        record.gate_transport_failures = 0
        record.gate_transport_error = ""
        self._reset_infrastructure_reruns(record)
        _reset_wait(record, "worker")
        _reset_wait(record, "review")
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            body=(
                f"The repeated done report for HEAD {sha} was accepted for automatic mechanical "
                f"gate retry: the previous red was classified from its CI step as infrastructure "
                f"({record.rejected_failure_reason or 'enumerated infrastructure signature'}). "
                "No worker rework round was opened."
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                "stale-done-infrastructure-retry",
                ref,
                str(record.report_generation),
            ),
        )
        record.comment_baseline = len(self.reader.show(ref).get("comments") or [])
        open_review_round(record, record.comment_baseline)
        records[ref] = record
        self.save_records(payload, records)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="validate",
            reason=(
                "worker report:done retries an infrastructure-classified mechanical gate on the same SHA"
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                "stale-done-infrastructure-validate",
                ref,
                str(record.report_generation),
            ),
        )
        record.state = "validate"
        self.save_records(payload, records)
        return {
            "status": "ok",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "to": "validate",
            "action": "stale-done-infrastructure-retry",
        }

    def _block_repeated_infrastructure_done(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        sha: str,
    ) -> dict[str, Any]:
        """A second stale infra report cannot add evidence after the accepted gate retry."""
        ref = task["ref"]
        unconfirmed = self._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
        if unconfirmed is not None:
            return unconfirmed
        reports = record.rejected_done_reports + 1
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=(
                f"The worker reported done {reports} times on unchanged infrastructure-classified "
                f"HEAD {sha} ({record.rejected_failure_reason or 'enumerated CI-service signature'}). "
                "One report already returned the SHA to the bounded Actions rerun path; a further "
                "identical report has no new gate evidence."
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                "stale-done-infrastructure-blocked",
                ref,
                str(reports),
            ),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {
            "status": "blocked",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "stale-done-infrastructure-blocked",
        }

    def _reject_stale_done(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        sha: str,
    ) -> dict[str, Any]:
        """Bounce one repeated rejected result, then leave the diagnosis to a human."""
        ref = task["ref"]
        rejected = record.rejected_done_reports + 1
        if rejected >= 2:
            unconfirmed = self._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
            if unconfirmed is not None:
                return unconfirmed
            record.rejected_done_reports = rejected
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="blocked",
                reason=(
                    f"The worker reported done twice with no new work: HEAD {sha} was already "
                    "rejected by the mechanical gate or by a red review. A human needs to look at "
                    "this."
                ),
                request_id=_attempt_request_id(
                    record.attempt_id or attempt_id,
                    "stale-done-blocked",
                    ref,
                    str(record.rejected_done_reports),
                ),
            )
            records.pop(ref, None)
            self.save_records(payload, records)
            return {
                "status": "blocked",
                "step": "advance",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "reason": "worker repeatedly reported rejected SHA",
            }

        # The rework worker opens in this same checkout, so the head that reported the stale done has
        # to be confirmed gone first; a refusal ends the tick before the comment and the relaunch.
        unconfirmed = self._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
        if unconfirmed is not None:
            return unconfirmed
        # Counted only once the bounce happens; a tick stopped at the refusal rejected nothing.
        record.rejected_done_reports = rejected
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            body=(
                f"The done report was rejected: HEAD {sha} was already rejected by the mechanical "
                "gate or by a red review. Do and commit new work, then report again. If the cause "
                "is a test or the gate itself and the code should not change, use "
                "report --kind blocked; another done on this SHA moves the card to Blocked."
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id, "stale-done-rework", ref, str(record.rejected_done_reports)
            ),
        )
        record.comment_baseline = len(self.reader.show(ref).get("comments") or [])
        open_review_round(record, record.comment_baseline)
        # The bounce restarts this attempt with a new TASK.md, so it is a new report round: without a
        # new generation the next done report would be deduped against the stale one just rejected.
        # The routing round does not move here, so this generation cannot be `attempt_round`.
        record.report_generation += 1
        # Nobody adjudicated this round: it was opened by the bounce, not an observer. The decision
        # that opened the previous one goes with it, or the document names a review this is not about.
        record.report_decision = ""
        _reset_wait(record, "worker")
        _reset_wait(record, "review")
        moved = self.reader.show(ref)
        failure = self._worker_relaunch_intent(payload, records, ref, record, action="stale-done-rework")
        if failure is not None:
            return _launch_intent_unwritable(
                step="advance",
                ref=ref,
                attempt_id=record.attempt_id or attempt_id,
                role=WORKER_ROLE,
                reason=failure,
            )
        launched, failed = self._bring_up_worker_head(
            moved,
            record,
            records,
            payload,
            attempt_id,
            step="advance",
            stage=STAGE_REWORK,
            blocked_reason="stale-result rework bring-up failed",
            blocked_action="stale-done-rework-blocked",
        )
        if launched is None:
            assert failed is not None
            return failed
        _record_worker_delivery_evidence(record, launched.delivery_evidence)
        record.state = "claimed"
        # A rejected done report earns no verdict, so this stays the same round.
        self.record_worker_routing(moved, record, launched.run)
        _clear_launch_intent(record)
        record.worker_started_at = record.worker_progress_at = time.time()
        records[ref] = record
        self.save_records(payload, records)
        return {
            "status": "ok",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "stale-done-rework",
        }

    def _advance_review(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
    ) -> dict[str, Any]:
        ref = task["ref"]
        record = records.get(ref)
        if record is None:
            try:
                record = self._adopt(task, attempt_id)
            except HostError as exc:
                return self._block_unresumable(task, records, payload, attempt_id, "review", exc)
            records[ref] = record
        if record.worker_continuation.parked:
            # The park's move or its checkpoint did not commit: the card is still in Validate with
            # the verdict recorded. Finish the park before the gate or any review marker is read.
            return self._complete_park(record, records, payload, attempt_id, ref=ref)
        if record.worker_continuation.red_transition_pending:
            # A red transition whose move did not commit is finished before the gate is read again,
            # before any review marker and before a reviewer starts: a rollup that has turned green
            # since cannot retract a red round this card is already owed.
            return self._complete_red_transition(task, record, records, payload, attempt_id, ref=ref)
        if _last_marker(task, record.review_baseline, {"review:green", "review:red"}):
            # A verdict is standing on this round, so its identity has to be readable before
            # anything acts on it. Missing, damaged or conflicting context is a fail-closed
            # lifecycle outcome here, not a tick that quietly waits for a verdict already on the
            # board, and not a pair rebuilt to fit an answer that is already written.
            try:
                # Read, never established. Once a verdict exists, the round's identity is a fact
                # that was fixed before it — recovering a pair now, from a reviewer document or
                # from any later receipt, would be inventing an identity to fit a verdict that is
                # already written. So this asks the boundary for the bound context and takes its
                # refusal, whether the context is absent, damaged, from another round, or
                # contradicted by the launch this dispatcher recorded for the round.
                context = require_verdict_review_context(self.host, task, record)
            except ReviewContextError as exc:
                return self.block_review_context(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    step="review",
                    reason=scrub_host_output(str(exc)),
                )
            marker = self._accepted_review_marker(task, record, context)
        else:
            marker = None
        if marker == "review:green":
            return self._park_green_verdict(task, record, records, payload, attempt_id, context)
        if marker == "review:red":
            # Only the reviewer's lifecycle ends here: a full `stop` would take the worktree's
            # terminals down, and this checkout is about to be parked and is never re-created from
            # base. An unconfirmed stop ends the tick before the card moves. The round's bound
            # candidate is what was judged; ending the reviewer's pane does not forget it.
            reviewed = context.candidate_sha
            unconfirmed = self._end_review_pane_confirmed(
                record,
                records,
                payload,
                ref,
                step="review",
                attempt_id=attempt_id,
                initiator=STOPPED_BY_REVIEW_VERDICT,
            )
            if unconfirmed is not None:
                return unconfirmed
            # The verdict is accepted here, whichever of the three red outcomes it takes: the
            # reviewer's pane is closed but its run, and the session it names, are still recorded.
            self.record_attempt_usage(ref, record, role=REVIEW_ROLE, attempt_id=attempt_id)
            record.rejected_sha = reviewed
            record.rejected_failure_class = "substantive"
            record.rejected_failure_reason = "red-review"
            record.rejected_done_reports = 0
            # The only point where both the last review body and the SHA it judged are available.
            # Keep them for the next review packet instead of reconstructing the card from base.
            record.previous_reviewed_sha = reviewed
            record.previous_blockers = _safe_one_line(_last_review_red_body(task) or "", limit=2000)
            if not self._parks_for_decision(task):
                # No observer to release it: the verdict acts on its own tick, and the worker that
                # wrote the code is still suspended, so the verdict goes to that conversation.
                # Except at the ceiling: a card nobody watches has to stop asking for more rounds.
                reds = _red_review_count(task)
                if reds >= RED_REVIEW_CEILING:
                    return self._block_red_review_ceiling(
                        task, record, records, payload, attempt_id, reds=reds
                    )
                return self._begin_red_transition(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    phase="review",
                    move_reason="review:red",
                    verdict_outcome="red",
                )
            # The worker of this round stays suspended through the park: the observer may send the
            # findings back to it, and that conversation is only worth keeping if nothing else writes.
            self._record_verdict_routing(ref, record, "red")
            return self._begin_park(
                task,
                record,
                records,
                payload,
                attempt_id,
                verdict_outcome="red",
                move_reason=(
                    "review:red. The card is parked in Assessment: the reviewer is stopped and "
                    "the worker of this round is held, waiting for a release, rework or reslice "
                    "decision."
                ),
            )
        # Mechanical gate: a fresh report clears the cheap CI/local gate before the expensive
        # reviewer is spawned. A review already in flight cleared the gate when it launched.
        if record.state not in ("review_starting", "reviewing") and record.gate_state != "green":
            gated = self._run_gate(task, record, records, payload, attempt_id)
            if gated is not None:
                return gated
        if record.state == "review_starting":
            return _recover_review_launch(self, task, records, record, attempt_id, payload=payload)
        if record.state != "reviewing":
            if record.worker_continuation.retained and not self.host.worker_retained_alive(record):
                # The record remembers a suspended worker the host cannot confirm is frozen.
                # Ambiguous liveness is never permission to leave it beside the reviewer, so the
                # confirmed stop runs before the reviewer launch intent is written.
                unconfirmed = self._stop_worker_confirmed(record, ref, step="review", attempt_id=attempt_id)
                if unconfirmed is not None:
                    return unconfirmed
                records[ref] = record
                self.save_records(payload, records)
            launch_request = _review_launch_request_id(ref, record.review_baseline)
            if self.audit.committed_event(launch_request) is not None:
                record.state = "review_starting"
                return _recover_review_launch(self, task, records, record, attempt_id, payload=payload)
            self.writer.comment(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                body=f"Dispatcher review launch requested for {ref}, review baseline {record.review_baseline}.",
                request_id=launch_request,
            )
            record.state = "review_starting"
            return _start_review(
                self, task, records, record, attempt_id, action="review-started", payload=payload
            )
        watchdog = self._wait_watchdog(task, record, records, payload, attempt_id, kind="review")
        if watchdog is not None:
            return watchdog
        return {
            "status": "ok",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "waiting-review-verdict",
        }

    def _accepted_review_marker(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        context: ReviewRoundContext,
    ) -> str | None:
        """The verdict of this review round, or ``None`` when none has been accepted yet.

        A verdict drives this card only when it says, in its own structured header, that it judged
        exactly the candidate and base this round was opened on, and when the marker comment that
        published it is on the card after this round's baseline. The comparison is against the
        round's bound context and nothing else: the dispatcher's gate receipts belong to their own
        stages and one of them may already have been replaced by an assessment or release gate
        over a base that moved after this reviewer started.

        A header that names another pair, an unstructured or historical verdict, and a staged
        event whose comment never landed are all "not accepted": the card keeps waiting, which is
        recoverable, rather than acting on a verdict about a different state of the code.
        """
        comments = (task.get("comments") or [])[record.review_baseline :]
        for raw in reversed(self.audit.events(task["ref"])):
            if raw.get("kind") != "card.verdict":
                continue
            try:
                projection = project_verdict(Event.from_record(raw))
            except (TypeError, ValueError):
                continue
            header = projection.header
            if projection.structure != "structured" or header is None:
                continue
            if not context.names_revisions(header.candidate_sha, header.base_sha):
                continue
            try:
                rendered = render_marker_comment(projection.event)
            except ValueError:
                continue
            if any(
                comment.get("marker") == f"review:{header.verdict}" and comment.get("body") == rendered
                for comment in comments
            ):
                return f"review:{header.verdict}"
        return None

    def _wait_watchdog(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        kind: str,
    ) -> dict[str, Any] | None:
        """Watch an open-ended wait without confusing a bad Orca inventory for a dead head."""
        if getattr(record, f"paused_{'reviewer' if kind == 'review' else 'worker'}_at"):
            return {
                "status": "ok",
                "step": "review" if kind == "review" else "advance",
                "pilot_ref": task["ref"],
                "attempt_id": attempt_id,
                "action": f"{kind}-paused",
            }
        runtime_reason = ""
        try:
            status = (
                self.host.review_status(task, record)
                if kind == "review"
                else self.host.worker_status(task, record)
            )
        except Exception as exc:
            # Orca may be down or between reconnects. That is no evidence this head died, so do not
            # restart it; it also cannot prove progress, so the ordinary wait ceiling stays.
            status = {"known": False, "live": True, "reason": "runtime-unavailable"}
            runtime_reason = scrub_host_output(str(exc))
        if status.get("identity_mismatch"):
            return {
                "status": "degraded",
                "step": "review" if kind == "review" else "advance",
                "pilot_ref": task["ref"],
                "attempt_id": attempt_id,
                "action": f"{kind}-heartbeat-identity-mismatch",
                "reason": "the heartbeat names a live process with a mismatching launch identity",
            }
        activity = status.get("last_activity")
        progress_at = float(getattr(record, f"{kind}_progress_at") or 0.0)
        if activity:
            updated = max(progress_at, float(activity))
            if updated != progress_at:
                progress_at = updated
                setattr(record, f"{kind}_progress_at", progress_at)
                self.save_records(payload, records)
        now = time.time()
        episode = self._reduce_and_store_vitality_episode(
            task, record, records, payload, status, kind=kind, now=now
        )
        # THE DECISION IS THE VERDICT (S1-4): the persisted episode -- reduced from this
        # very tick's observations on every shape the status carries, including the
        # not-live ones -- chooses between waiting, nudging and recovering. The old
        # not-live shortcut is gone: what used to be an unconditional reclaim is now
        # taken only when the reduction actually saw death (``Dead``), and a terminal
        # that vanished while the heartbeat stays live is decided by evidence, not by
        # the inventory.
        return self._decide_wait_by_verdict(
            task,
            record,
            records,
            payload,
            attempt_id,
            kind=kind,
            status=status,
            episode=episode,
            now=now,
            runtime_reason=runtime_reason,
            activity=activity,
            progress_at=progress_at,
        )

    def _decide_wait_by_verdict(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        kind: str,
        status: dict[str, Any],
        episode: Any,
        now: float,
        runtime_reason: str,
        activity: Any,
        progress_at: float,
    ) -> dict[str, Any] | None:
        """Turn this tick's vitality verdict into the wait tick's one decision.

        Verdict -> action, per the plan's recovery policy (nudge before destruction,
        ``wait`` whenever the evidence does not earn intervention):

        * ``HealthyActive`` / ``HealthyQuiet`` / ``Unverifiable`` / ``Suspended`` ->
          ``wait``. A quiet-below-threshold head is between turns; an unverifiable head
          has no strong witness; a suspended head is SIGCONT territory (S1-5). None of
          them may be nudged into a respawn by a clock.
        * ``SuspectedStall`` -> at most one idempotent report nudge per round, keyed on
          the round generation like every nudge; a suspicion never destroys.
        * ``ConfirmedStall`` / ``Dead`` -> the ordinary recovery path
          (``_trigger_wait_watchdog``), whose every destructive step re-checks the guard.
        * No episode at all (nothing was ever observed for this run), or ``Unverifiable``
          -> ``wait`` while the ceiling has not elapsed, then an OPERATOR escalation
          (``_escalate_unobservable_wait``): one idempotent durable comment plus a
          degraded outcome naming the evidence gap. Such a run is never destroyed -- a
          run nobody could observe is also a run nobody can prove dead -- so its wait is
          bounded by escalation, not by replacement. This differs from main before
          S1-4, which reclaimed such heads on the clock alone; that behaviour is what
          the guard now refuses.

        Evidence-shaped legacy branches inside this fallback (no output since launch; no
        terminal progress) keep their pre-vitality triggers because they act on what a
        source actually said, and their destructive steps are still fenced by the guard.
        """
        ref = task["ref"]
        expectation = _wait_expectation(kind)
        if kind == "worker":
            expectation = f"{expectation} for generation {record.report_generation}"
        verdict = episode.verdict if episode is not None else None

        def plain_wait() -> dict[str, Any] | None:
            if runtime_reason:
                return {
                    "status": "degraded",
                    "step": "review" if kind == "review" else "advance",
                    "pilot_ref": ref,
                    "attempt_id": attempt_id,
                    "action": f"{kind}-runtime-unavailable",
                    "reason": runtime_reason,
                }
            return None

        if verdict is VitalityVerdict.DEAD:
            # The heartbeat names a gone process: the existing not-live handling, from
            # the same evidence the reduction used.
            return self._trigger_wait_watchdog(
                task,
                record,
                records,
                payload,
                attempt_id,
                kind=kind,
                trigger="the pid heartbeat names a gone or unreaped process",
            )
        if verdict is VitalityVerdict.CONFIRMED_STALL:
            reason = (
                f"the {kind} head's vitality episode confirms a stall "
                f"({episode.reason or 'strong quiet past both thresholds'})"
                f" with no {expectation}"
            )
            if kind == "worker":
                # The confirmed boundary: ask once for the report before anything
                # destructive, exactly as the idle ladder did -- but only when the
                # episode itself says the head is stalled.
                prompted, reason = self._prompt_worker_report(
                    task, record, records, payload, attempt_id, trigger=reason
                )
                if prompted is not None:
                    return prompted
            # Degraded, not ok: an `ok` bounce would write healthy telemetry over the
            # one signal that says this card needs looking at before it reaches Blocked.
            return self._trigger_wait_watchdog(
                task,
                record,
                records,
                payload,
                attempt_id,
                kind=kind,
                trigger=reason,
                degraded=True,
            )
        if verdict is VitalityVerdict.SUSPECTED_STALL:
            # One idempotent nudge, then wait: the suspicion phase exists so a single
            # lost turn recovers conversationally instead of destructively.
            suspicion_basis = episode.reason or "strong quiet past the suspect threshold"
            if kind == "worker":
                prompted, trigger = self._prompt_worker_report(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    trigger=(
                        f"the {kind} head's vitality episode suspects a stall "
                        f"({suspicion_basis}) with no {expectation}"
                    ),
                )
                if prompted is not None:
                    return prompted
                # The prompt was already spent this round (or the head cannot take one):
                # carry the suspicion as visible degradation without escalating.
                return {
                    "status": "degraded",
                    "step": "review" if kind == "review" else "advance",
                    "pilot_ref": ref,
                    "attempt_id": attempt_id,
                    "action": f"{kind}-stall-suspected",
                    "reason": trigger,
                }
            return {
                "status": "degraded",
                "step": "review" if kind == "review" else "advance",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "action": f"{kind}-stall-suspected",
                "reason": (
                    f"the review head's vitality episode suspects a stall "
                    f"({suspicion_basis}) with no {expectation}"
                ),
            }
        if verdict is VitalityVerdict.SUSPENDED:
            # The recovery policy owns this arm (S1-5): one identity-fenced SIGCONT per
            # suspension span, then a bounded response window, then operator escalation --
            # never a stop. The comment is keyed per span so it cannot flood.
            return self._execute_recovery_intent(
                task,
                record,
                records,
                payload,
                attempt_id,
                episode=episode,
                kind=kind,
                now=now,
            )
        if verdict in (VitalityVerdict.HEALTHY_ACTIVE, VitalityVerdict.HEALTHY_QUIET):
            # Fresh evidence of life: renew the outer window and wait. No clock on this
            # path may act against what the evidence calls alive. A recovered suspension
            # lands here too; the policy's rung reset rides the same recovery decision,
            # persisted back onto this same role's episode slot.
            self._run_recovery_policy(
                task,
                record,
                records,
                payload,
                episode=episode,
                kind=kind,
                now=now,
            )
            setattr(record, f"{kind}_waiting_since", now)
            self.save_records(payload, records)
            return plain_wait()
        # Unverifiable, or no episode at all: nothing strong answered, so the honest
        # answer is that nobody knows. The plan forbids KILLING such a run -- and the
        # guard enforces exactly that, refusing every destructive step on this path --
        # but an unobservable wait is still bounded: once the role's outer ceiling has
        # elapsed with no verdict earned, the tick escalates to the OPERATOR (one
        # idempotent durable comment per wait cycle plus a degraded outcome). Escalation
        # is not replacement: the head is never touched here. The two legacy
        # evidence-shaped branches below keep their pre-vitality meaning because they
        # act only on what a source actually said (no output since launch; no terminal
        # progress), and even they are fenced by the guard.
        #
        # Before the ceilings speak, the policy gets its say: an authoritative
        # deterministic refusal riding this tick's unavailable snapshot (the 1194 class)
        # escalates after N identical sightings instead of waiting out any ceiling.
        policy_outcome = self._recovery_policy_outcome(
            task,
            record,
            records,
            payload,
            attempt_id,
            episode=episode,
            kind=kind,
            now=now,
        )
        if policy_outcome is not None:
            return policy_outcome
        stall = _stall_seconds(kind)
        waiting_since = float(getattr(record, f"{kind}_waiting_since") or 0.0)
        started_at = float(getattr(record, f"{kind}_started_at") or 0.0)
        pid_confirmed = bool(status.get("pid_confirmed"))
        if (
            not pid_confirmed
            and activity
            and started_at
            and float(activity) <= started_at
            and now - started_at > _initial_output_stall_seconds()
        ):
            return self._trigger_wait_watchdog(
                task,
                record,
                records,
                payload,
                attempt_id,
                kind=kind,
                trigger=f"no terminal output since launch for {int(now - started_at)}s",
            )
        if progress_at and now - progress_at > stall:
            return self._trigger_wait_watchdog(
                task,
                record,
                records,
                payload,
                attempt_id,
                kind=kind,
                trigger=f"no terminal output for {int(now - progress_at)}s",
            )
        if not waiting_since:
            setattr(record, f"{kind}_waiting_since", now)
            self.save_records(payload, records)
            return plain_wait()
        unobserved_for = now - waiting_since
        if unobserved_for >= stall:
            return self._escalate_unobservable_wait(
                task,
                record,
                attempt_id,
                kind=kind,
                seconds=int(unobserved_for),
                ceiling=stall,
                runtime_reason=runtime_reason,
            )
        return plain_wait()

    def _escalate_unobservable_wait(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        attempt_id: str,
        *,
        kind: str,
        seconds: int,
        ceiling: int,
        runtime_reason: str = "",
    ) -> dict[str, Any]:
        """Escalate an unobservable head to the operator WITHOUT touching it.

        Reached only from the no-episode/Unverifiable fallback of
        ``_decide_wait_by_verdict`` once the role's outer ceiling has elapsed on a wait
        nobody could observe. The plan's asymmetry forbids killing what nothing could
        read (the guard refuses it), but an operator must not inherit an unbounded silent
        wait either, so this is the bound: one durable comment per wait cycle (keyed like
        every watchdog comment, so it cannot flood) naming the evidence gap and the
        elapsed span, plus a degraded tick outcome. The head is not signalled, stopped or
        replaced; if it starts answering again the reduction earns a verdict and the
        ordinary table takes over.
        """
        ref = task["ref"]
        detail = f"; {runtime_reason}" if runtime_reason else ""
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            body=(
                f"Dispatcher wait watchdog ({kind}): nothing could observe this head for "
                f"{seconds}s (outer ceiling {ceiling}s) -- no readable heartbeat and no "
                f"provider answer{detail}. The head was NOT stopped or replaced: no "
                "evidence earned that. Escalating to the operator; the card keeps "
                "waiting until someone looks or the head becomes observable again."
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                f"{kind}-unobserved-wait",
                ref,
                _wait_cycle_token(record),
            ),
        )
        return {
            "status": "degraded",
            "step": "review" if kind == "review" else "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{kind}-unobserved-wait-escalated",
            "reason": (
                f"nothing could observe the head for {seconds}s "
                f"(ceiling {ceiling}s); escalated to the operator, head untouched"
            ),
        }

    def _recovery_thresholds(self) -> Any:
        """This installation's recovery-policy thresholds, read per call.

        The response window comes from the watchdog's env knob so operations can tighten it
        without a release; the deterministic-refusal limit stays at its small default.
        """
        return _RecoveryThresholds(
            response_window_seconds=float(_suspension_response_window_seconds()),
            deterministic_refusal_limit=_DEFAULT_RECOVERY_THRESHOLDS.deterministic_refusal_limit,
        )

    def _recovery_policy_decision(
        self,
        episode: Any,
        *,
        kind: str,
        now: float,
    ) -> tuple[Any, Any] | None:
        """Ask the policy what this tick should intend, and persist its rung state.

        Returns ``(decision, updated_episode)`` or ``None`` when there is nothing to decide
        (no episode). The rung write happens here, once, so every caller of the policy -- the
        wait tick and the gate phase alike -- persists exactly the same shape.
        """
        if episode is None:
            return None
        decision = _decide_recovery(episode, episode, now, self._recovery_thresholds())
        # An observe with unchanged state rewrites nothing: persisting per tick would churn
        # the record for zero information. Only a rung/refusal change is worth a save.
        if decision.intent is _RecoveryIntent.OBSERVE and (
            episode.recovery_rung == decision.rung and episode.deterministic_refusals == decision.refusals
        ):
            return decision, episode
        updated = _apply_rung_state(episode, decision)
        return decision, updated

    def _store_recovery_episode(
        self,
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        ref: str,
        *,
        kind: str,
        episode: Any,
    ) -> None:
        field_name = f"{kind}_vitality_episode"
        setattr(record, field_name, episode)
        records[ref] = record
        self.save_records(payload, records)

    def _recovery_policy_outcome(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        episode: Any,
        kind: str,
        now: float,
    ) -> dict[str, Any] | None:
        """Turn a policy decision into a tick outcome where the caller needs one.

        Used on arms that only escalate (the deterministic-refusal fast path): ``None``
        means "no escalation earned, carry on with the caller's own logic".
        """
        asked = self._recovery_policy_decision(episode=episode, kind=kind, now=now)
        if asked is None:
            return None
        decision, updated = asked
        if updated is not episode:
            self._store_recovery_episode(
                record,
                records,
                payload,
                task["ref"],
                kind=kind,
                episode=updated,
            )
        if decision.intent is not _RecoveryIntent.ESCALATE_OPERATOR:
            return None
        return self._escalate_recovery_to_operator(
            task,
            record,
            attempt_id,
            kind=kind,
            decision=decision,
            now=now,
        )

    def _run_recovery_policy(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        *,
        episode: Any,
        kind: str,
        now: float,
    ) -> None:
        """Let the policy observe a non-suspended verdict (rung reset after recovery).

        The wait-tick table already handled this verdict; the policy call exists purely to
        clear the persisted ladder when a suspension has resolved: the cleared episode comes
        back at ``RUNG_NONE``, and a later fresh suspension span climbs
        ``RUNG_SIGCONT_SENT`` -> ``RUNG_RESPONSE_WINDOW`` again instead of inheriting an old
        escalation.

        ``kind`` routes the persistence, exactly as everywhere else on this path: the reset
        is stored back onto the SAME role's episode slot the reduction read it from
        (``review_vitality_episode`` for the review head, the worker slot for the worker).
        Persisting across roles would park one run's episode in the other role's field --
        a foreign-run episode the destructive guard refuses as FOREIGN_RUN until some later
        ordinary reduction overwrites it, and a rung that never resets on the real subject.
        """
        asked = self._recovery_policy_decision(episode=episode, kind=kind, now=now)
        if asked is None:
            return
        _, updated = asked
        if updated is not episode:
            self._store_recovery_episode(
                record,
                records,
                payload,
                task["ref"],
                kind=kind,
                episode=updated,
            )

    def _execute_recovery_intent(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        episode: Any,
        kind: str,
        now: float,
    ) -> dict[str, Any]:
        """Execute one recovery-policy intent for a suspended head, safely.

        The Suspended arm's whole surface: ask the policy, persist its rung state, then act:

        * ``sigcont``  -> one identity-fenced SIGCONT (never SIGTERM/SIGKILL from this path)
          plus one durable comment naming the span; idempotent because the policy keys the
          intent on the freeze stamp and only returns it for a fresh span.
        * ``observe`` inside the window -> the plain degraded wait outcome, visible in
          telemetry but touching nothing.
        * ``escalate_operator`` -> one durable comment per span asking a human to look;
          the head is never signalled, stopped or replaced. The guard would refuse any
          destructive step on a Suspended verdict regardless; this path simply never asks.
        """
        ref = task["ref"]
        asked = self._recovery_policy_decision(episode=episode, kind=kind, now=now)
        if asked is None:
            return {
                "status": "degraded",
                "step": "review" if kind == "review" else "advance",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "action": f"{kind}-suspension-observed",
                "reason": "no vitality episode on file",
            }
        decision, updated = asked
        self._store_recovery_episode(record, records, payload, ref, kind=kind, episode=updated)
        step = "review" if kind == "review" else "advance"
        if decision.intent is _RecoveryIntent.SIGCONT:
            sent = self._sigcont_head(task, record, kind=kind, now=now)
            body = (
                f"Vitality ({kind}): the head's process is parked on a stop signal "
                f"(suspended since {time.strftime('%H:%M:%S', time.gmtime(decision.detail['span_started_at']))}). "
                + (
                    f"Sent one identity-fenced SIGCONT; holding a "
                    f"{int(decision.detail['response_window_seconds'])}s response window before escalating."
                    if sent
                    else "Could NOT verify the process identity, so nothing was signalled; "
                    "holding the response window and watching."
                )
                + " The head was not stopped."
            )
            self.writer.comment(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                body=body,
                request_id=_attempt_request_id(
                    record.attempt_id or attempt_id,
                    f"{kind}-vitality-sigcont",
                    ref,
                    suffix=_request_token(f"sigcont@{decision.detail['span_started_at']:.0f}"),
                ),
            )
            return {
                "status": "degraded",
                "step": step,
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "action": f"{kind}-sigcont-sent" if sent else f"{kind}-sigcont-fenced",
                "reason": decision.reason,
                "recovery": decision.to_json(),
            }
        if decision.intent is _RecoveryIntent.ESCALATE_OPERATOR:
            self._escalate_suspended_head(
                task,
                record,
                attempt_id,
                kind=kind,
                decision=decision,
            )
            return {
                "status": "degraded",
                "step": step,
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "action": f"{kind}-suspension-escalated",
                "reason": decision.reason,
                "recovery": decision.to_json(),
            }
        # Observe: inside the response window (or already escalated and holding).
        return {
            "status": "ok" if decision.rung < _RUNG_ESCALATED else "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{kind}-suspension-observed",
            "reason": decision.reason,
            "recovery": decision.to_json(),
        }

    def _sigcont_head(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        *,
        kind: str,
        now: float = 0.0,
    ) -> bool:
        """Send SIGCONT to this role's head process group, identity-fenced at send time.

        The ONLY signal this recovery path may send, and only after re-verifying through
        the heartbeat that the live process behind the pid file is still this exact HeadRun
        (`guard_head_run_identity` raises on a foreign live process, the same fence
        `_confirm_head_process_gone` uses before its signals). A mismatched, unreadable or
        vanished identity sends nothing: resuming somebody else's process group is worse
        than leaving our own parked one parked one more tick. Never SIGTERM/SIGKILL here --
        suspension is recoverable by definition, and the destructive paths keep their own
        guarded entries.
        """
        if getattr(self.host, "mode", "real") == "noop":
            return False
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        if not pid_file:
            return False
        run = record.review_head_run if kind == "review" else record.worker_head_run
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        try:
            status = _guard_head_run_identity(
                pid_file,
                run=run,
                role=kind,
                task=f"card:{task['ref']}",
                leaf=leaf,
            )
        except _HeadRunIdentityMismatch:
            return False
        if not _heartbeat_is_live_match(status):
            return False
        pid = int(status["pid"])
        try:
            # Same group rule as `_signal_head`: the terminal gives an interactive head
            # its own foreground process group, so the CONT reaches its helpers too; old
            # launches and focused tests may share OUR group, and signalling that would
            # wake the dispatcher itself rather than the head.
            group = os.getpgid(pid)
            if group != os.getpgrp():
                os.killpg(group, signal.SIGCONT)
            else:
                os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise HostError(f"head process {pid} could not be resumed: {exc}") from None
        return True

    def _escalate_suspended_head(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        attempt_id: str,
        *,
        kind: str,
        decision: Any,
    ) -> None:
        """One durable comment per suspension span: the response window expired."""
        detail = decision.detail or {}
        suspended_for = int(detail.get("suspended_for_seconds") or 0)
        window = int(detail.get("span_started_at") or 0)
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=task["ref"],
            body=(
                f"Dispatcher recovery policy ({kind}): the head stayed suspended for "
                f"{suspended_for}s -- past the response window even after SIGCONT. The "
                "process was NOT stopped or replaced: a suspended process is alive by "
                "the kernel's own word. Escalating to the operator; please look at the "
                f"head (pane handle {record.review_handle if kind == 'review' else record.handle}"
                f", heartbeat {record.review_pid_file if kind == 'review' else record.worker_pid_file})."
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                f"{kind}-suspension-window-expired",
                task["ref"],
                suffix=_request_token(f"suspended@{window:.0f}"),
            ),
        )

    def _escalate_recovery_to_operator(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        attempt_id: str,
        *,
        kind: str,
        decision: Any,
        now: float = 0.0,
    ) -> dict[str, Any]:
        """Escalate a deterministic terminal refusal class to the operator, touching nothing.

        Reached from the no-conclusion fallback once N identical authoritative refusals are
        on file (the 1194 contract): the attempt cannot succeed by retrying, so the ladder
        is skipped entirely. One comment per refusal count (idempotent via the request id),
        plus a degraded outcome. The head is never signalled or replaced from here.
        """
        detail = decision.detail or {}
        ref = task["ref"]
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            body=(
                f"Dispatcher recovery policy ({kind}): the same authoritative refusal "
                f"({detail.get('deterministic_class', 'deterministic')}) arrived "
                f"{detail.get('identical_refusals', '?')}x. Retrying cannot change it: the "
                "reason names a property of this launch (configuration, executable, "
                "credentials, quota), not a transient outage. Escalating to the operator "
                "instead of re-sending; nothing was stopped or replaced."
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                f"{kind}-deterministic-refusal",
                ref,
                str(detail.get("identical_refusals") or 0),
            ),
        )
        return {
            "status": "degraded",
            "step": "review" if kind == "review" else "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{kind}-deterministic-refusal-escalated",
            "reason": decision.reason,
            "recovery": decision.to_json(),
        }

    def _vitality_guard_decision(
        self,
        record: DispatcherRecord,
        *,
        kind: str,
        action: str,
        current_run_id: str = "",
    ) -> Any:
        """Ask the vitality guard whether this watchdog-driven step may proceed."""
        field_name = f"{kind}_vitality_episode"
        return _assert_destructive_allowed(
            getattr(record, field_name),
            action,
            time.time(),
            current_run_id=current_run_id
            or str(
                ((record.review_head_run if kind == "review" else record.worker_head_run) or {}).get("run_id")
                or ""
            ),
            pid_only_outer_ceiling_seconds=float(_stall_seconds(kind)),
        )

    def _guard_or_wait(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        kind: str,
        now: float,
        action: str,
        proceed: Callable[[], dict[str, Any]],
        current_run_id: str = "",
    ) -> dict[str, Any]:
        """Run one watchdog-driven destructive step through the vitality guard.

        Allowed -> the step runs as before. Refused -> the tick degrades to a visible,
        idempotent ``{kind}-guard-refused`` wait outcome naming the refusal class, and
        the destructive step does not happen. A refusal is never a silent no-op: the
        outcome carries it, and the durable comment is written once per cycle (keyed on
        the wait-cycle token, like every other watchdog comment).
        """
        decision = self._vitality_guard_decision(
            record,
            kind=kind,
            action=action,
            current_run_id=current_run_id,
        )
        if decision.allowed:
            return proceed()
        request_id = _attempt_request_id(
            record.attempt_id or attempt_id,
            f"{kind}-vitality-guard-refused",
            task["ref"],
            _wait_cycle_token(record),
        )
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=task["ref"],
            body=(
                f"Dispatcher wait watchdog refused ({decision.refusal.value}): "
                f"{decision.reason}. Nothing was stopped or replaced; the card keeps "
                "waiting."
            ),
            request_id=request_id,
        )
        return {
            "status": "degraded",
            "step": "review" if kind == "review" else "advance",
            "pilot_ref": task["ref"],
            "attempt_id": attempt_id,
            "action": f"{kind}-guard-refused",
            "reason": f"{decision.refusal.value}: {decision.reason}",
            "guard": decision.to_json(),
        }

    def _reduce_and_store_vitality_episode(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        status: dict[str, Any],
        *,
        kind: str,
        now: float,
    ) -> Any:
        """Reduce and persist one vitality episode for this role's head run; return it.

        This is S1-2's shadow reduction, promoted (card S1-4) into the wait tick's
        decision input. The contract grows by exactly one clause: the method now returns
        the episode it stored (``None`` when nothing was observed or the reduction
        failed) so the caller can decide from it -- every other property is unchanged.
        Every early return here leaves ``record`` untouched or unchanged, so a caller
        that gets ``None`` decides as it would have with no episode at all: a reduction
        failure degrades to "no episode" plus one comment and must never break the tick
        hosting it.

        Sources actually observed on this path, without any new host call:

        * ``pid_heartbeat`` -- only when the status carries ``pid_status`` (the raw
          ``head_process_status`` classification). The wait tick itself consumes derived booleans
          (`pid_confirmed`, `identity_mismatch`), so the classification is passed through by
          ``command_terminal_status`` alongside them; where it is absent the source stays
          Unavailable rather than being reconstructed from a boolean.
        * ``provider_cursor`` -- from ``status["provider_progress"]``, the already-admitted
          exact-HeadRun evidence ``command_terminal_status`` fetched; compared against the
          previous cursor persisted on the episode.
        * ``pane_advisory`` -- from the same status's ``idle`` flag, advisory by construction.
        """
        field_name = f"{kind}_vitality_episode"
        previous = getattr(record, field_name)
        run_payload = record.review_head_run if kind == "review" else record.worker_head_run
        run_id = str((run_payload or {}).get("run_id") or "")
        if not run_id:
            # Without a durable run identity there is nothing an episode may bind to. Leaving any
            # stale episode in place would misattribute it to a head nobody can name, so it is
            # dropped explicitly.
            if previous is not None:
                setattr(record, field_name, None)
            return None
        pid_status = status.get("pid_status")
        provider_progress = status.get("provider_progress")
        if (
            not isinstance(pid_status, dict)
            and not isinstance(provider_progress, dict)
            and "idle" not in status
        ):
            # Nothing was observed at all (the noop host, a runtime-unavailable tick): there is
            # no reduction to run and no episode to write, so return before saving anything.
            # Writing one would both rewrite the state file every such tick and stamp an
            # "observation" nobody made -- the same lie Unverifiable exists to avoid. The
            # caller decides this tick from ``None`` -- i.e. from the outer ceilings, the
            # pre-vitality behaviour for an unobservable head -- while the guard below any
            # destructive step still reads whatever verdict the record already carries.
            return None
        snapshots = _snapshots_from_status(
            status,
            run_id=run_id,
            previous_cursor=(
                (previous.evidence_cursors or {}).get(_SnapshotSource.PROVIDER_CURSOR.value, "")
                if previous is not None
                else ""
            ),
            observed_at=now,
        )
        try:
            episode = _reduce_vitality(previous, snapshots, now, _DEFAULT_VITALITY_THRESHOLDS)
        except Exception as exc:  # noqa: BLE001 - shadow mode must never break the hosting tick
            # Shadow mode may never break the tick that hosts it. A reduction failure is recorded
            # as no episode so the next tick starts clean, and nothing downstream changes --
            # including the decision, which then falls back to the outer ceilings.
            self.writer.comment(
                role="dispatcher",
                actor=self.owner,
                reference=task["ref"],
                body=f"Vitality shadow reduction failed and was skipped: {scrub_host_output(str(exc))[:160]}",
                request_id=_attempt_request_id(
                    record.attempt_id or "",
                    f"{kind}-vitality-error",
                    task["ref"],
                    suffix=_request_token(str(now)),
                ),
            )
            return None
        changed = previous is None or previous.verdict is not episode.verdict
        setattr(record, field_name, episode)
        records[task["ref"]] = record
        self.save_records(payload, records)
        if not changed:
            return episode
        # The request id names only what the comment claims -- the verdict transition itself --
        # so a flapping verdict cannot mint a fresh idempotency key every tick and turn shadow
        # logging into an unbounded comment stream.
        #
        # THE BODY IS A FUNCTION OF EXACTLY WHAT THE KEY NAMES (secretary-1477). The board's
        # identity for a comment is the digest of its body, so a stable key over a body that
        # moved is not an idempotent replay: it is
        # `validation: request id belongs to another operation or payload`, raised out of the
        # wait tick before it ever decides, costing the card its whole per-card advance for
        # that tick. This body therefore says only what `(kind, prev->cur)` already fixes.
        # The live measurement is NOT dropped, only unquoted here: the reduction's `basis` --
        # `quiet:<n>s@<source>`, `advisory:<active|idle>@pane_advisory` and the rest -- is
        # persisted on the episode this method just saved (`record.{kind}_vitality_episode`
        # in `dispatcher/production-state.json`) and is reported verbatim by
        # `secretary head-status` (`dispatch/head_status.py`, the row's `episode.basis`).
        # Anything a future edit wants to add here has to enter the suffix above with it.
        request_id = _attempt_request_id(
            record.attempt_id or "",
            f"{kind}-vitality-verdict",
            task["ref"],
            suffix=_request_token(
                f"{previous.verdict.value if previous else 'none'}->{episode.verdict.value}"
            ),
        )
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=task["ref"],
            body=(
                f"Vitality ({kind}): {episode.verdict.value}"
                + (f" (was {previous.verdict.value})" if previous is not None else " (first observation)")
                + ". The basis and the live measurement behind this verdict stay on the"
                + " durable vitality episode; read them with `secretary head-status`."
                + (
                    ""
                    if episode.verdict in DESTRUCTIVE_VERDICTS
                    else " Recorded only - does not authorise destruction."
                )
            ),
            request_id=request_id,
        )
        return episode

    def _prompt_worker_report(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        trigger: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """Spend this round's one report prompt on a confirmed-idle worker, or decline to.

        Hands back the tick's outcome and the trigger the caller carries on with. A `None` outcome
        means the watchdog carries on into its stop-and-replace path.

        The order is the durability contract: intent on disk, then the send, then the confirmation. A
        tick that dies in the middle leaves an intent that reads as spent, which is what stops a
        restart from typing the same prompt twice.
        """
        ref = task["ref"]
        nudge = record.worker_report_nudge
        generation = record.report_generation
        if nudge.spent(generation):
            return None, trigger
        if not self.host.worker_addressable(record):
            return None, trigger
        nudge.begin(generation, time.time())
        records[ref] = record
        self.save_records(payload, records)
        try:
            self.host.prompt_worker_report(task, record)
        except HostError as exc:
            _record_worker_delivery_evidence(record, exc, failure=True)
            records[ref] = record
            self.save_records(payload, records)
            return None, f"{trigger}, and the report prompt was refused: {scrub_host_output(str(exc))}"
        nudge.confirm()
        # The prompted head owns a fresh idle window AND a fresh stall episode: charging it
        # with the episode that produced the prompt would escalate on the next tick before
        # the worker could have answered. The episode restarts its quiet reference at now,
        # keeping the run identity and history, so the ladder must re-earn suspicion from
        # the moment the worker was actually asked.
        _reset_idle(record, "worker")
        episode = record.worker_vitality_episode
        if episode is not None:
            record.worker_vitality_episode = replace(
                episode,
                verdict=VitalityVerdict.HEALTHY_QUIET,
                suspected_since=0.0,
                confirmed_since=0.0,
                started_at=time.time(),
                updated_at=time.time(),
                reason="report prompt delivered; the quiet clock restarts here",
            )
        records[ref] = record
        self.save_records(payload, records)
        # Persisted before the comment: a raising writer must not leave the prompt unrecorded.
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            body=(
                f"Dispatcher wait watchdog: {trigger}. The worker head was asked once to run the "
                f"report command for generation {generation}. The round, its TASK.md and its owner "
                "are unchanged. Another idle episode in this round stops the head instead."
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id, "worker-report-prompt", ref, str(generation)
            ),
        )
        return {
            # Degraded, not ok: a card whose worker had to be reminded is not moving on its own.
            "status": "degraded",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "worker-report-prompted",
            "reason": trigger,
        }, trigger

    def _trigger_wait_watchdog(
        self,
        task,
        record,
        records,
        payload,
        attempt_id,
        *,
        kind: str,
        trigger: str,
        stall: int | None = None,
        degraded: bool = False,
    ):
        """The verdict-driven recovery entry point (S1-4): respawn once, then escalate.

        Reached ONLY from decisions the persisted vitality episode drove -- a ``Dead``
        or ``ConfirmedStall`` verdict in the wait tick. The destructive step itself is
        fenced by the vitality guard inside ``_guard_or_wait`` before
        ``_respawn_wait``/``_escalate_wait`` run, so a stale episode, a foreign run or a
        verdict that does not authorise destruction turns into a visible wait instead of
        a stop.
        """
        action = (
            f"{kind}-escalate" if int(getattr(record, f"{kind}_respawns") or 0) >= 1 else f"{kind}-respawn"
        )
        return self._guard_or_wait(
            task,
            record,
            records,
            payload,
            attempt_id,
            kind=kind,
            now=time.time(),
            action=action,
            proceed=lambda: (
                self._respawn_wait(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    kind=kind,
                    now=time.time(),
                    trigger=trigger,
                    degraded=degraded,
                )
                if action == f"{kind}-respawn"
                else self._escalate_wait(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    kind=kind,
                    stall=_stall_seconds(kind) if stall is None else stall,
                    trigger=trigger,
                )
            ),
        )

    def _respawn_wait(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        kind: str,
        now: float,
        trigger: str,
        degraded: bool = False,
    ) -> dict[str, Any]:
        ref = task["ref"]
        step = "review" if kind == "review" else "advance"
        if kind == "review":
            # Only the reviewer is stalled; its pane goes and the workspace stays. A stall is not a
            # death, so an unconfirmed stop ends the tick rather than adding a second reviewer.
            unconfirmed = self._end_review_pane_confirmed(
                record,
                records,
                payload,
                ref,
                step=step,
                attempt_id=attempt_id,
                initiator=STOPPED_BY_WATCHDOG,
            )
            if unconfirmed is not None:
                return unconfirmed
            # One bring-up path for the reviewer, shared with the normal launch and the recovery path.
            outcome = _start_review(
                self, task, records, record, attempt_id, action="review-respawned", payload=payload
            )
            if outcome.get("status") != "ok":
                self.save_records(payload, records)
                return outcome
        else:
            # Same as the reviewer above: a silent worker is not a dead one.
            unconfirmed = self._stop_worker_confirmed(record, ref, step=step, attempt_id=attempt_id)
            if unconfirmed is not None:
                return unconfirmed
            failure = self._worker_relaunch_intent(payload, records, ref, record, action="worker-respawn")
            if failure is not None:
                return _launch_intent_unwritable(
                    step=step,
                    ref=ref,
                    attempt_id=record.attempt_id or attempt_id,
                    role=WORKER_ROLE,
                    reason=failure,
                )
            launched, failed = self._bring_up_worker_head(
                task,
                record,
                records,
                payload,
                attempt_id,
                step=step,
                stage=STAGE_RESPAWN,
                blocked_reason="worker respawn failed",
                blocked_action="worker-respawn-blocked",
                blocked_request_suffix=_wait_cycle_token(record),
            )
            if launched is None:
                assert failed is not None
                return failed
            record.state = "claimed"
            # A respawn is a real bring-up: a repinned profile lands a different configuration.
            self.record_worker_routing(task, record, launched.run)
            _clear_launch_intent(record)
            record.worker_started_at = record.worker_progress_at = now
        if kind == "review":
            record.review_started_at = record.review_progress_at = now
        # Persist the restart before commenting: there is no try/except here, so a raising comment
        # would escape with the head respawned and respawns still 0, and the escalation never comes.
        setattr(record, f"{kind}_waiting_since", now)
        # The replacement head owns its own readiness; it is not charged with what it replaces.
        _reset_idle(record, kind)
        respawns = int(getattr(record, f"{kind}_respawns") or 0) + 1
        setattr(record, f"{kind}_respawns", respawns)
        records[ref] = record
        self.save_records(payload, records)
        # Leave a trace, or the operator cannot tell a first stall from an already-restarted head.
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            body=(
                f"Dispatcher wait watchdog: {trigger}, "
                f"respawned the {kind} head (respawn {respawns})."
                + (
                    " The report round did not move: the same TASK.md is back in the checkout, "
                    f"with the report commands for generation {record.report_generation}."
                    if kind == "worker"
                    else ""
                )
                + " Another stall escalates to Blocked."
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                f"{kind}-respawn",
                ref,
                f"{_wait_cycle_token(record)}-{respawns}",
            ),
        )
        return {
            # A head that is alive, idle and has delivered nothing is the pipeline failing to move a
            # card: `degraded` is what puts it in the telemetry an operator and the steward read.
            "status": "degraded" if degraded else "ok",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{kind}-respawned",
            **({"reason": trigger} if degraded else {}),
        }

    def _escalate_wait(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        kind: str,
        stall: int,
        trigger: str,
    ) -> dict[str, Any]:
        ref = task["ref"]
        step = "review" if kind == "review" else "advance"
        if kind == "review":
            # The reviewer may still hold the checkout when its second stall escalates. End it
            # through the same confirmed boundary; a refused stop leaves the record for the retry.
            unconfirmed = self._end_review_pane_confirmed(
                record,
                records,
                payload,
                ref,
                step=step,
                attempt_id=attempt_id,
                initiator=STOPPED_BY_WATCHDOG,
            )
            if unconfirmed is not None:
                return unconfirmed
            # Review starts over a retained worker: settle that role before dropping the record.
            unconfirmed = self._stop_worker_confirmed(record, ref, step=step, attempt_id=attempt_id)
        else:
            unconfirmed = self._stop_worker_confirmed(record, ref, step=step, attempt_id=attempt_id)
        if unconfirmed is not None:
            return unconfirmed
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=(f"wait watchdog: {trigger} after respawn (ceiling {stall}s), blocked for the operator"),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id, f"{kind}-wait-stall", ref, _wait_cycle_token(record)
            ),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {"status": "ok", "step": step, "pilot_ref": ref, "attempt_id": attempt_id, "to": "blocked"}

    def _run_gate(
        self,
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
            self.save_records(payload, records)
        try:
            result = self.host.gate_check(task, record)
        except GateTransportError as exc:
            retry = self._gate_transport_retry(
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
            return self._block_gate_transport(
                task,
                record,
                records,
                payload,
                attempt_id,
                step="gate",
                action="gate-transport-blocked",
            )
        except HostError as exc:
            self.host.stop(record)
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="blocked",
                reason=f"validation gate failed: {scrub_host_output(str(exc))}",
                request_id=_attempt_request_id(record.attempt_id or attempt_id, "gate-blocked", ref),
            )
            records.pop(ref, None)
            self.save_records(payload, records)
            return {"status": "blocked", "step": "gate", "pilot_ref": ref, "reason": "validation gate failed"}
        self._gate_answered(ref, record, records, payload)
        if result.status == "green":
            return self._accept_green_gate(
                task, record, records, payload, attempt_id, result, stage="initial"
            )
        if result.status == "pending":
            return self._gate_pending(task, record, records, payload, attempt_id, result)
        return self._gate_red_to_worker(task, record, records, payload, attempt_id, result, phase="gate")

    def _accept_green_gate(
        self,
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
            current_sha=self.host.head_commit(record),
            gate_mode=_validation_ci(self.host, task),
            noop=getattr(self.host, "mode", "real") == "noop",
        )
        if not accepted.valid:
            if stage == "initial":
                return self._block_missing_gate_receipt(task, record, records, payload, attempt_id)
            step = "assessment" if stage == "release" else "review"
            return self._block_merge_path(
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
        self._reset_infrastructure_reruns(record)
        record.gate_attestation = accepted.persisted_payload()
        if stage == "initial":
            # The one place an initial receipt validates a review round's identity. Every later
            # stage writes its receipt above and stops there: an assessment or release gate is
            # evidence about its own stage, and the base it names may legitimately have moved
            # since this round was opened.
            blocked = self._bind_review_context(
                task,
                record,
                records,
                payload,
                attempt_id,
                step="review",
                receipt=accepted.receipt,
                unattested=self.unattested_gate(task),
            )
            if blocked is not None:
                return blocked
        records[ref] = record
        self.save_records(payload, records)
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
            self.writer.comment(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                body=(
                    f"## Mechanical gate attestation — {label}\n\n"
                    + accepted.receipt.render()
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

    def unattested_gate(self, task: dict[str, Any]) -> bool:
        """Whether this card's mechanical gate explicitly attests nothing.

        `ci:none` promises no execution, and a noop host executes nothing at all. Only these two
        may open a review round on a freshly resolved base rather than on a receipt: everywhere
        else, a round with no exact-SHA evidence for its base has no identity to give a reviewer.
        """
        return _validation_ci(self.host, task) == "none" or getattr(self.host, "mode", "real") == "noop"

    def _bind_review_context(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        step: str,
        receipt: GateReceipt | None = None,
        unattested: bool = False,
        recorded_launch: bool = False,
    ) -> dict[str, Any] | None:
        """Bind this round's review context, or answer with the tick's fail-closed outcome."""
        try:
            bind_review_context(
                self.host,
                task,
                record,
                receipt=receipt,
                unattested=unattested,
                recorded_launch=recorded_launch,
            )
        except ReviewContextError as exc:
            return self.block_review_context(
                task, record, records, payload, attempt_id, step=step, reason=scrub_host_output(str(exc))
            )
        return None

    def block_review_context(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        step: str,
        reason: str,
    ) -> dict[str, Any]:
        """The one visible outcome for a review round whose identity cannot be established.

        Reached from binding, from reviewer bring-up, from launch-intent adoption and from a
        verdict standing over a round with no context. All of them mean the same thing — nobody
        can say which candidate over which base this round is about — and none of them may be
        answered by guessing, by merging, or by a tick that repeats the same failure forever. The
        card goes to Blocked naming the contradiction, which is a question an operator can answer.
        """
        ref = task["ref"]
        self.host.stop(record)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=f"review round context is unavailable: {reason}",
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "review-context-blocked", ref),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {
            "status": "blocked",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "reason": "review context unavailable",
        }

    def _block_missing_gate_receipt(
        self,
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
        self.host.stop(record)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=(
                "validation gate reported green but did not provide a valid exact-SHA receipt "
                "(SHA, base SHA and terminal checks); blocked rather than treating it as attested"
            ),
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "gate-receipt-blocked", ref),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {"status": "blocked", "step": "gate", "pilot_ref": ref, "reason": "gate receipt unavailable"}

    def _gate_red_to_worker(
        self,
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
        if result.failure_class == "infrastructure":
            return self._retry_infrastructure_gate(
                task,
                record,
                records,
                payload,
                attempt_id,
                result,
                phase=phase,
            )
        current_sha = self.host.head_commit(record)
        preserve_stale_no_diff_retry = bool(
            result.failure_reason == "workflow-dispatch-head-sha-mismatch"
            and self._is_stale_no_diff_research_gate_recovery(task, record, current_sha)
        )
        record.rejected_sha = current_sha
        record.rejected_failure_class = "substantive"
        record.rejected_failure_reason = result.failure_reason
        if not preserve_stale_no_diff_retry:
            record.rejected_done_reports = 0
        detail = scrub_host_output(result.summary)
        log = scrub_host_output(result.log).strip()
        # A GateResult built without `fingerprint` (the review-freeze drift check) still gets a
        # SHA-independent identity here rather than losing repeat detection outright.
        fingerprint = result.fingerprint or _gate_fingerprint("fallback", log or detail)
        repeat = _gate_red_repeat_count(task, fingerprint)
        prefix = f"Repeat return (round {repeat + 1}, the reason has not changed). " if repeat else ""
        body = (
            f"{prefix}The mechanical validation gate is red: {detail}. The card is back in "
            f"In progress for rework."
        )
        if log:
            body += f"\nTail:\n```\n{log}\n```"
        body += f"\n<!-- gate-fingerprint: {fingerprint} -->"
        # The reviewer must be gone before a retained worker resumes, but the worker stays
        # suspended until the continuation is delivered or falls back to a replacement.
        unconfirmed = self._end_review_pane_confirmed(
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
        return self._begin_red_transition(
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
        self,
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
        sha = self.host.head_commit(record)
        if record.gate_infrastructure_reruns_sha != sha:
            self._reset_infrastructure_reruns(record)
            record.gate_infrastructure_reruns_sha = sha
        spent = record.gate_infrastructure_reruns
        if spent >= GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS:
            return self._block_infrastructure_reruns_exhausted(
                task,
                record,
                records,
                payload,
                attempt_id,
                result,
                phase=phase,
            )
        try:
            self.host.rerun_failed_ci(task, record, result)
        except GateTransportError as exc:
            retry = self._gate_rerun_transport_retry(
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
            return self._block_infrastructure_rerun_unavailable(
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
            return self._block_infrastructure_rerun_unavailable(
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
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
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
        self.save_records(payload, records)
        return {
            "status": "ok",
            "step": "gate",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "gate-infrastructure-rerun",
            "reason": result.failure_reason,
        }

    @staticmethod
    def _reset_infrastructure_reruns(record: DispatcherRecord) -> None:
        record.gate_infrastructure_reruns_sha = ""
        record.gate_infrastructure_reruns = 0
        record.gate_infrastructure_rerun_run_id = ""
        record.gate_infrastructure_rerun_reason = ""
        record.gate_rerun_transport_failures = 0
        record.gate_rerun_transport_error = ""

    def _block_infrastructure_reruns_exhausted(
        self,
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
        self.host.stop(record)
        reason = (
            "Mechanical gate remains red from infrastructure failure "
            f"({result.failure_reason or 'enumerated CI-service signature'}) after "
            f"{record.gate_infrastructure_reruns} Actions rerun(s) for HEAD "
            f"{record.gate_infrastructure_reruns_sha or self.host.head_commit(record)}; "
            "the bounded automatic recovery is exhausted."
        )
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=reason,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                f"{phase}-infrastructure-reruns-exhausted",
                ref,
                str(record.gate_infrastructure_reruns),
            ),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {
            "status": "blocked",
            "step": phase,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "gate-infrastructure-reruns-exhausted",
            "reason": result.failure_reason,
        }

    def _block_infrastructure_rerun_unavailable(
        self,
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
        self.host.stop(record)
        reason = (
            "Mechanical gate is red from infrastructure failure "
            f"({result.failure_reason or 'enumerated CI-service signature'}), but its failed Actions "
            f"run could not be rerun: {scrub_host_output(str(exc))}. Blocked rather than rereading "
            "the same terminal result."
        )
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=reason,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id, f"{phase}-infrastructure-rerun-blocked", ref
            ),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {
            "status": "blocked",
            "step": phase,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "gate-infrastructure-rerun-blocked",
            "reason": result.failure_reason,
        }

    def _begin_red_transition(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        phase: str,
        move_reason: str,
        verdict_outcome: str,
        decision: str = "",
        decision_body: str = "",
    ) -> dict[str, Any]:
        """The only way a card goes back to In progress for rework.

        The order lives here and nowhere else: the intent is on disk, with its phase, the report
        baseline it was opened against and the reason the card is moving, before anything observable
        moves; the board moves; and only then is it decided whether the round's own session takes the
        continuation or a replacement does. Holding a session is deliberately not a precondition.
        """
        ref = task["ref"]
        baseline = len(task.get("comments") or [])
        # The round this transition opens is reserved here, with the intent and before the move:
        # completion must read that generation rather than compute it, or a re-entered completion
        # hands one rework round two generations. The observer's instruction is frozen in the same
        # write, so what the round is for cannot be re-read from a newer decision comment.
        record.worker_continuation.begin_red_transition(
            phase,
            baseline,
            move_reason,
            verdict_outcome,
            decision,
            reserved_generation=record.report_generation + 1,
            decision_body=decision_body,
        )
        records[ref] = record
        self.save_records(payload, records)
        return self._complete_red_transition(task, record, records, payload, attempt_id, ref=ref)

    def _complete_red_transition(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        ref: str,
    ) -> dict[str, Any]:
        """Finish the open red transition from the board as it is now.

        The move is keyed on the baseline the intent was opened against, so the tick that already moved
        the card and the tick recovering from a crash before that move run the same call and the card
        moves once. Nothing here re-reads the verdict: the transition carries its own reason.
        """
        continuation = record.worker_continuation
        phase = continuation.phase or "gate"
        baseline = continuation.report_baseline
        if not continuation.decision:
            # A transition performing a decision is the second half of a round whose verdict was
            # already recorded at the park; recording it again would overwrite that outcome.
            self._record_verdict_routing(ref, record, continuation.verdict_outcome)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="in_progress",
            reason=continuation.move_reason,
            # The board refuses to take a card out of Assessment without a decision; a red gate
            # moving out of Validate carries none and is refused nothing.
            decision=continuation.decision,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id, f"{phase}-red", ref, str(baseline)
            ),
        )
        moved = self.reader.show(ref)
        # The previous round's report stays behind this baseline, so no tick reads it as this one's.
        record.comment_baseline = max(len(moved.get("comments") or []), baseline)
        # Where the next verdict is scanned from, so the one just acted on is not read again. The
        # judged round's identity ends with it: the rework binds its own candidate and base.
        open_review_round(record, record.comment_baseline)
        # The rework's generation is the one this transition reserved before the move: assigned,
        # never advanced. A legacy transition without a reservation falls back to the advance it
        # was written with.
        record.report_generation = continuation.reserved_generation or record.report_generation + 1
        # And the instruction that round is opened on, from the same transition. Always assigned,
        # never merged: a red gate has no decision, and inheriting the prior round's would hand a
        # worker an adjudication of review findings its code has already answered.
        record.report_decision = continuation.decision_body
        current_sha = self.host.head_commit(record)
        reuse_report_only_gate = self._can_reuse_report_only_rework_gate(
            task, record, current_sha, observer_rework=continuation.decision == "rework"
        )
        if not reuse_report_only_gate:
            record.gate_state = ""
            record.gate_pending_since = 0.0
            record.gate_attestation = {}
            record.gate_transport_failures = 0
            record.gate_transport_error = ""
            self._reset_infrastructure_reruns(record)
        _reset_wait(record, "review")
        _reset_wait(record, "worker")
        records[ref] = record
        self.save_records(payload, records)
        return self._deliver_red_continuation(moved, record, records, payload, attempt_id, phase=phase)

    def _deliver_red_continuation(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        phase: str,
    ) -> dict[str, Any]:
        """Hand a red verdict back to the session that wrote the code, or to one replacement.

        The order is the same for the gate and for the review: the suspension is re-confirmed at the
        moment of use, the delivery boundary is durable before the worker is woken, and every way out
        that cannot reuse the session goes through a confirmed stop first.
        """
        ref = task["ref"]
        continuation = record.worker_continuation
        step = "review" if phase == "review" else "gate"
        opening_delivery = not continuation.delivery_pending
        fresh_provider_progress = False
        if continuation.delivery_pending:
            now = time.time()
            provider_observation = self._observe_retained_continuation_progress(task, record, now=now)
            blocked = self._block_unadmitted_continuation_liveness(
                task,
                record,
                records,
                payload,
                attempt_id,
                phase=phase,
                observation=provider_observation,
            )
            if blocked is not None:
                return blocked
            fresh_provider_progress = provider_observation == "progressed"
            pending = self._continuation_recovery_window(
                task,
                record,
                records,
                payload,
                attempt_id,
                phase=phase,
                fresh_provider_progress=fresh_provider_progress,
                now=now,
            )
            if pending is not None:
                return pending
            if (
                record.worker_continuation_liveness.recovery_rung
                == ContinuationRecoveryRung.SAFE_RECOVERY_RESUME_ONCE
            ):
                if not record.worker_continuation_liveness.allow_safe_recovery_resume_once():
                    record.worker_continuation_liveness.terminalize(
                        "replacement", "safe recovery resume was already spent"
                    )
                    records[ref] = record
                    self.save_records(payload, records)
                    return self._restart_red_worker(
                        task,
                        record,
                        records,
                        payload,
                        attempt_id,
                        continuation_reason="safe recovery resume was already spent",
                        phase=phase,
                    )
                # A once-only capability: persist spending it before delivery touches the pane.
                records[ref] = record
                self.save_records(payload, records)
        if continuation.retained:
            try:
                # The suspension was confirmed on a past tick; a SIGCONT from terminal recovery or
                # an operator since makes this a second writer. Ask the heartbeat again here.
                self.host.confirm_worker_retained(record)
            except HostError as exc:
                reason = scrub_host_output(str(exc))
                unconfirmed = self._stop_worker_confirmed(record, ref, step=step, attempt_id=attempt_id)
                if unconfirmed is not None:
                    return unconfirmed
                return self._restart_red_worker(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    continuation_reason=reason,
                    phase=phase,
                    worker_stopped=True,
                )
            if opening_delivery:
                # Persist the delivery boundary before waking the worker, or a tick that dies after
                # delivery replays with the old done marker read as the new round's completion.
                continuation.begin_delivery(phase, time.time())
                record.worker_continuation_liveness = WorkerContinuationLiveness.begin(record.worker_head_run)
                # Establish the provider cursor before SIGCONT: the first observation is a baseline.
                provider_observation = self._observe_retained_continuation_progress(
                    task, record, now=time.time()
                )
                blocked = self._block_unadmitted_continuation_liveness(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    phase=phase,
                    observation=provider_observation,
                )
                if blocked is not None:
                    return blocked
                fresh_provider_progress = provider_observation == "progressed"
                records[ref] = record
                self.save_records(payload, records)
                pending = self._continuation_recovery_window(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    phase=phase,
                    fresh_provider_progress=fresh_provider_progress,
                    now=time.time(),
                )
                if pending is not None:
                    return pending
            else:
                # An already-open boundary: recreating liveness would make no-progress unbounded.
                records[ref] = record
                self.save_records(payload, records)
            try:
                self.host.resume_worker(task, record)
            except HostError as exc:
                if _delivery_readiness_state(exc) == READINESS_BUSY:
                    # The boundary saw an owned pane working before it sent anything: neither
                    # acknowledgement nor a dead-head vote, so keep the continuation and retry.
                    _record_worker_delivery_evidence(record, exc)
                    liveness = record.worker_continuation_liveness
                    # The pre-read provider cursor is the precedence rule: a fresh rollout keeps this
                    # HeadRun and only restarts its no-progress ladder. A busy `tui-idle` does not.
                    if fresh_provider_progress:
                        continuation.busy_attempts = liveness.busy_attempts
                        continuation.busy_next_at = time.time() + BUSY_RETRY_INITIAL_SECONDS
                        records[ref] = record
                        self.save_records(payload, records)
                        return _retained_worker_busy_deferred(
                            ref,
                            record,
                            attempt_id,
                            phase,
                            delay=BUSY_RETRY_INITIAL_SECONDS,
                        )
                    if liveness.state != ContinuationLivenessState.STALLED:
                        # The first exact-source cursor is a persisted baseline, not evidence of a
                        # stall: keep the head and schedule, spending no no-progress attempt.
                        continuation.busy_next_at = time.time() + BUSY_RETRY_INITIAL_SECONDS
                        records[ref] = record
                        self.save_records(payload, records)
                        return _retained_worker_busy_deferred(
                            ref,
                            record,
                            attempt_id,
                            phase,
                            delay=BUSY_RETRY_INITIAL_SECONDS,
                        )
                    liveness.no_progress_evidence = _continuation_no_progress_evidence(record, liveness.state)
                    liveness.note_busy(time.time())
                    continuation.busy_attempts = max(0, liveness.busy_attempts - 1)
                    delay = continuation.defer_busy(time.time())
                    # `defer_busy` owns the persisted retry deadline, liveness owns the bounded
                    # episode count: keep them in sync, never beyond this HeadRun's evidence.
                    continuation.busy_attempts = liveness.busy_attempts
                    records[ref] = record
                    self.save_records(payload, records)
                    bounded = self._advance_no_progress_continuation(
                        task,
                        record,
                        records,
                        payload,
                        attempt_id,
                        phase=phase,
                    )
                    if bounded is not None:
                        return bounded
                    return _retained_worker_busy_deferred(ref, record, attempt_id, phase, delay=delay)
                _record_worker_delivery_evidence(record, exc, failure=True)
                records[ref] = record
                self.save_records(payload, records)
                return self._restart_red_worker(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    continuation_reason=scrub_host_output(str(exc)),
                    phase=phase,
                )
            continuation.confirm_delivery()
            records[ref] = record
            self.save_records(payload, records)
            return self._finish_retained_worker_resume(
                task, record, records, payload, attempt_id, phase=phase
            )
        # Same reservation as the retained branch: the rework round is fixed on disk with the
        # intent, so adoption resumes it rather than the round the verdict closed.
        return self._restart_red_worker(
            task,
            record,
            records,
            payload,
            attempt_id,
            continuation_reason="no retained worker session was available",
            phase=phase,
        )

    def _observe_retained_continuation_progress(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        *,
        now: float,
    ) -> str:
        """Persist provider progress before a continuation interprets `tui-idle`."""
        try:
            evidence = getattr(
                self.host,
                "provider_progress",
                lambda _task, _record, _kind: {
                    "state": "unavailable",
                    "reason": "host has no provider-progress probe",
                },
            )(task, record, "worker")
        except Exception as exc:
            evidence = {
                "state": "unavailable",
                "reason": f"provider-progress probe failed: {scrub_host_output(str(exc))}",
            }
        liveness = record.worker_continuation_liveness
        if not liveness.bound and record.worker_continuation.busy_attempts:
            # An old busy count is audit data, never an exact-source observation for the ladder.
            liveness.legacy_busy_attempts = max(
                liveness.legacy_busy_attempts,
                record.worker_continuation.busy_attempts,
            )
        observation = liveness.observe_provider(evidence, now, head_run=record.worker_head_run)
        if liveness.admitted:
            record.worker_continuation.busy_attempts = liveness.busy_attempts
        if observation == "progressed":
            record.worker_continuation.busy_next_at = 0.0
        return observation

    def _block_unadmitted_continuation_liveness(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        phase: str,
        observation: str,
    ) -> dict[str, Any] | None:
        """Take the explicit safe outcome when the liveness trust boundary is unprovable."""
        if (
            observation in {"baseline", "stalled", "progressed"}
            and record.worker_continuation_liveness.admitted
        ):
            return None
        if observation == ContinuationProviderCondition.LEGACY_UNBOUND_V1.value:
            return self._restart_red_worker(
                task,
                record,
                records,
                payload,
                attempt_id,
                continuation_reason="Codex provider source remained legacy-unbound for v1 progress",
                phase=phase,
            )
        ref = task["ref"]
        reason = record.worker_continuation_liveness.reason or "provider source was not admitted"
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=(
                "retained continuation liveness is unprovable; preserving the exact HeadRun "
                f"without recovery: {reason}"
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                "continuation-liveness-unavailable",
                ref,
                phase,
            ),
        )
        records[ref] = record
        self.save_records(payload, records)
        return {
            "status": "blocked",
            "step": "review" if phase == "review" else "gate",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{phase}-red-continuation-liveness-unavailable",
        }

    def _continuation_recovery_window(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        phase: str,
        fresh_provider_progress: bool,
        now: float,
    ) -> dict[str, Any] | None:
        """Honor the recorded safe-recovery response window before another pane interaction."""
        liveness = record.worker_continuation_liveness
        ref = task["ref"]
        if liveness.terminal_outcome == "identity_fenced":
            # The stop path is the only component allowed to resolve this: it either confirms the
            # old HeadRun stopped and launches one replacement, or refuses. Neither takes a pane.
            return self._restart_red_worker(
                task,
                record,
                records,
                payload,
                attempt_id,
                continuation_reason="continuation liveness HeadRun identity is fenced",
                phase=phase,
            )
        if liveness.recovery_rung != ContinuationRecoveryRung.SAFE_RECOVERY_RESPONSE_WINDOW:
            return None
        if now < liveness.recovery_response_deadline:
            records[ref] = record
            self.save_records(payload, records)
            return _retained_worker_recovery_window(
                ref,
                record,
                attempt_id,
                phase,
                remaining=max(0, int(liveness.recovery_response_deadline - now)),
            )
        if fresh_provider_progress:
            liveness.recovery_rung = ContinuationRecoveryRung.SAFE_RECOVERY_RESUME_ONCE
            records[ref] = record
            self.save_records(payload, records)
            return None
        liveness.terminalize("replacement", "safe recovery response window showed no provider progress")
        records[ref] = record
        self.save_records(payload, records)
        return self._restart_red_worker(
            task,
            record,
            records,
            payload,
            attempt_id,
            continuation_reason="safe recovery response window showed no provider progress",
            phase=phase,
        )

    def _advance_no_progress_continuation(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        phase: str,
    ) -> dict[str, Any] | None:
        """Spend the sole safe-recovery rung, then take one identity-fenced terminal outcome."""
        liveness = record.worker_continuation_liveness
        if not liveness.admitted or liveness.state != ContinuationLivenessState.STALLED:
            return self._block_unadmitted_continuation_liveness(
                task,
                record,
                records,
                payload,
                attempt_id,
                phase=phase,
                observation=liveness.state.value,
            )
        if liveness.busy_attempts < CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS:
            return None
        if liveness.recovery_rung == ContinuationRecoveryRung.SAFE_RECOVERY_RESPONSE_WINDOW:
            return None
        if liveness.recovery_rung == ContinuationRecoveryRung.SAFE_RECOVERY_RESUME_ONCE:
            # The recovery's one authorised return to ordinary delivery has already been spent.
            if liveness.recovery_resume_used:
                liveness.terminalize("replacement", "safe recovery resume was already spent")
            else:
                return None
        if liveness.recovery_rung == ContinuationRecoveryRung.SAFE_RECOVERY_PENDING:
            # The intent was durable before the capability was called, and after a crash there we
            # cannot tell whether the provider acted. Spend the safe rung rather than retry it.
            liveness.terminalize(
                "replacement", "safe recovery response was unconfirmed after dispatcher recovery"
            )
            records[task["ref"]] = record
            self.save_records(payload, records)
        if not liveness.terminal:
            # Intent first: a death inside the capability must not make the next process retry it.
            liveness.begin_safe_recovery(time.time())
            records[task["ref"]] = record
            self.save_records(payload, records)
            try:
                result = getattr(
                    self.host,
                    "safe_recover_worker_continuation",
                    lambda *_args: {
                        "state": "unavailable",
                        "reason": "host has no provider/terminal-safe recovery capability",
                    },
                )(task, record, liveness.to_json())
            except Exception as exc:
                result = {"state": "unavailable", "reason": scrub_host_output(str(exc))}
            valid_recovery = (
                isinstance(result, dict)
                and str(result.get("state") or "") == "recovered"
                and bool(result.get("safe"))
                and str(result.get("head_run_id") or "") == liveness.head_run_id
            )
            if valid_recovery:
                # The only extension point for a future provider API: its response is recorded
                # before waiting, and it cannot tunnel a raw interrupt through a terminal command.
                liveness.safe_recovery_response_window(time.time(), 30.0)
                records[task["ref"]] = record
                self.save_records(payload, records)
                return _retained_worker_recovery_window(
                    task["ref"],
                    record,
                    attempt_id,
                    phase,
                    remaining=30,
                )
            reason = (
                str(result.get("reason") or "safe recovery capability is unavailable")
                if isinstance(result, dict)
                else "safe recovery capability returned an invalid shape"
            )
            liveness.recovery_rung = ContinuationRecoveryRung.SAFE_RECOVERY_UNAVAILABLE
            liveness.terminalize("replacement", f"safe recovery unavailable: {reason}")
            records[task["ref"]] = record
            self.save_records(payload, records)
        return self._restart_red_worker(
            task,
            record,
            records,
            payload,
            attempt_id,
            continuation_reason=(
                "provider progress remained absent after bounded continuation recovery: "
                f"{record.worker_continuation_liveness.reason}"
            ),
            phase=phase,
        )

    def _finish_retained_worker_resume(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        phase: str,
    ) -> dict[str, Any]:
        ref = task["ref"]
        step = "review" if phase == "review" else "gate"
        if record.worker_continuation_liveness.bound:
            record.worker_continuation_liveness.terminalize(
                "reused", "retained continuation delivery was confirmed"
            )
        record.worker_continuation.clear()
        record.state = "claimed"
        rework_round = record.attempt_round + 1
        retained_run = dict(record.worker_run)
        self.open_worker_round(record, round_number=rework_round)
        self.record_worker_routing(task, record, retained_run)
        self._record_worker_continuation(ref, record, "reused", phase, "retained worker resumed")
        record.worker_started_at = record.worker_progress_at = time.time()
        records[ref] = record
        self.save_records(payload, records)
        return {
            "status": "ok",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{phase}-red-reused-worker",
        }

    def _restart_red_worker(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        continuation_reason: str,
        phase: str,
        worker_stopped: bool = False,
    ) -> dict[str, Any]:
        """Launch the red-verdict fallback only after its worker was conclusively stopped."""
        ref = task["ref"]
        review = phase == "review"
        step = "review" if review else "gate"
        blocked_kind = "rework-blocked" if review else f"{phase}-red-blocked"
        action = "rework-started" if review else f"{phase}-red-rework"
        continuation = "replacement"
        if record.worker_continuation_liveness.bound and not record.worker_continuation_liveness.terminal:
            record.worker_continuation_liveness.terminalize("replacement", continuation_reason)
        # Unconditional on purpose: a record written by an older dispatcher, or adopted after a
        # crash, may lack the retained timestamp while its worker lives. Ambiguity is no permission.
        if not worker_stopped:
            unconfirmed = self._stop_worker_confirmed(record, ref, step=step, attempt_id=attempt_id)
            if unconfirmed is not None:
                return unconfirmed
        rework_round = record.attempt_round + 1
        # The launch intent takes the transition over from here: it is durable, reserves the rework
        # round, and recovery adopts or relaunches exactly one head. Hand it over in the same write,
        # or both can owe this card a worker. The handover is real only on disk: restoring the held
        # transition after a failed intent write keeps In progress from having no durable worker debt.
        held_transition = replace(record.worker_continuation)
        record.worker_continuation.clear()
        failure = self._worker_relaunch_intent(
            payload, records, ref, record, action=f"{phase}-red-rework", round_number=rework_round
        )
        if failure is not None:
            record.worker_continuation = held_transition
            return _launch_intent_unwritable(
                step=step,
                ref=ref,
                attempt_id=record.attempt_id or attempt_id,
                role=WORKER_ROLE,
                reason=failure,
            )
        launched, failed = self._bring_up_worker_head(
            task,
            record,
            records,
            payload,
            attempt_id,
            step=step,
            stage=STAGE_REWORK,
            blocked_reason="rework bring-up failed",
            blocked_action=blocked_kind,
        )
        if launched is None:
            assert failed is not None
            return failed
        record.state = "claimed"
        self.open_worker_round(record, round_number=rework_round)
        self.record_worker_routing(task, record, launched.run)
        self._record_worker_continuation(ref, record, continuation, phase, continuation_reason)
        _clear_launch_intent(record)
        record.worker_started_at = record.worker_progress_at = time.time()
        records[ref] = record
        self.save_records(payload, records)
        return {"status": "ok", "step": step, "pilot_ref": ref, "attempt_id": attempt_id, "action": action}

    def _record_worker_continuation(
        self, ref: str, record: DispatcherRecord, mode: str, phase: str, reason: str
    ) -> None:
        """Leave the red-verdict ownership decision on the card with its frozen launch snapshot."""
        run = record.worker_run
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            body=(
                f"Dispatcher {phase} red continuation: {mode}; worker profile {run.get('head') or record.head}, "
                f"model {run.get('model') or 'unknown'}, effort {run.get('effort') or 'default'}; "
                f"reason: {reason}; timestamp: {now_rfc3339()}."
            ),
            request_id=_attempt_request_id(
                record.attempt_id, f"{phase}-red-continuation", ref, str(record.attempt_round)
            ),
        )

    def _block_unresumable(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        step: str,
        error: Exception,
    ) -> dict[str, Any]:
        """A claimed card the dispatcher cannot pick back up on the head it was claimed with."""
        ref = task["ref"]
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=f"claimed head is unavailable: {scrub_host_output(str(error))}",
            request_id=_attempt_request_id(attempt_id, "adopt-head-blocked", ref),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {
            "status": "blocked",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "reason": "claimed head is unavailable",
        }

    def _block_failed_worker_restart(
        self,
        *,
        ref: str,
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        step: str,
        stage: str,
        reason: str,
        action: str,
        request_suffix: str = "",
        error: Exception,
    ) -> dict[str, Any]:
        """Block a failed rework launch while retaining the workspace's resume provenance."""
        failure = _classify_bring_up_failure(
            error, record, WORKER_ROLE, stage=stage, attempt_id=record.attempt_id or attempt_id
        )
        blocked_reason = _bring_up_blocked_reason(reason, error, record, WORKER_ROLE, failure=failure)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=blocked_reason,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                _bring_up_blocked_action(action, failure),
                ref,
                request_suffix,
            ),
        )
        resume_workspaces = payload.setdefault("resume_workspaces", {})
        if isinstance(resume_workspaces, dict):
            resume_workspaces[ref] = record.attempt_id or attempt_id
        records.pop(ref, None)
        self.save_records(payload, records)
        return {
            "status": "blocked",
            "step": step,
            "pilot_ref": ref,
            "reason": reason,
            **failure.outcome_fields(blocked_reason),
        }

    def _gate_answered(
        self,
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
        self.save_records(payload, records)

    def _gate_transport_retry(
        self,
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
        self.save_records(payload, records)
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
        self,
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
        self.save_records(payload, records)
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

    def _block_gate_transport(
        self,
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
        return self._block_merge_path(
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

    def _gate_pending(
        self,
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
        """
        ref = task["ref"]
        now = time.time()
        if not record.gate_pending_since:
            record.gate_pending_since = now
            self.save_records(payload, records)
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
        vitality = self._worker_vitality_for_gate(task, record, records, payload)
        if vitality is not None:
            episode = vitality
            if episode.verdict is VitalityVerdict.SUSPENDED:
                return self._execute_recovery_intent(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    episode=episode,
                    kind="worker",
                    now=now,
                )
            # Any other verdict still rides the policy once: a deterministic refusal on
            # file escalates fast even mid-gate, and a recovered suspension resets its rung.
            outcome = self._recovery_policy_outcome(
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
        self.host.stop(record)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=(
                f"Mechanical gate: {scrub_host_output(result.summary)}. CI has been hanging with "
                f"no terminal result for longer than the threshold "
                f"({GATE_PENDING_STALL_SECONDS}s). Card moved to Blocked for a human."
            ),
            request_id=_attempt_request_id(record.attempt_id or attempt_id, f"{action}-stall", ref),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {"status": "ok", "step": step, "pilot_ref": ref, "attempt_id": attempt_id, "to": "blocked"}

    def _worker_vitality_for_gate(
        self,
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
        if getattr(self.host, "mode", "real") == "noop":
            return None
        try:
            status = self.host.worker_status(task, record)
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
            return self._reduce_and_store_vitality_episode(
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

    def _parks_for_decision(self, task: dict[str, Any]) -> bool:
        """Whether a substantive verdict on this card waits for a decision, or acts at once."""
        reference = str(task.get("sprint") or "")
        if not reference:
            return False
        try:
            sprint = self.sprints.show(reference)
        except (TaskError, HostError):
            return False
        if str(sprint.get("status") or "") != "open":
            return False
        observer = sprint.get("observer")
        if not isinstance(observer, dict):
            return False
        return str(observer.get("kind") or "") == "head" and bool(observer.get("profile"))

    def _merge_readiness(
        self, task: dict[str, Any], record: DispatcherRecord, context: ReviewRoundContext
    ) -> tuple[str, GateResult | None, str]:
        """Everything mechanical that must hold before this checkout may be merged, read once.

        Returns one of "drift", "transport", "failed", "pending", "red" or "green". Both sides of the
        seam ask it: Validate before parking a green verdict, and the release again immediately before
        the merge. "transport" is deliberately not "failed" — a backend that could not be reached says
        nothing about the checkout, so the caller retries rather than deciding the card on silence.

        The round's identity is not one of the answers, because it is a precondition of asking: every
        answer here is about a candidate, and only the bound context says which candidate that is. So
        each caller reaches the identity boundary itself and hands the result in — Validate through
        the verdict it is reading, the release through the context the park left behind — and a round
        that cannot produce one never gets as far as a gate.
        """
        drift = self._review_drift(task, record, context)
        if drift:
            return "drift", None, drift
        try:
            result = self.host.gate_check(task, record)
        except GateTransportError as exc:
            return "transport", None, str(exc)
        except HostError as exc:
            return "failed", None, scrub_host_output(str(exc))
        if result.status == "green":
            return "green", result, ""
        if result.status == "pending":
            return "pending", result, ""
        return "red", result, ""

    def _park_green_verdict(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        context: ReviewRoundContext,
    ) -> dict[str, Any]:
        """A green review verdict parks the card; it does not merge it.

        Except on a card with nobody to park it for, where this is also the merge path — so the
        context is the one the caller already required to read the verdict at all, carried here
        rather than looked up again from a record the same tick has not changed.
        """
        ref = task["ref"]
        # Recorded before the gate: this round's head pair is a fact a red re-check cannot undo.
        self._record_verdict_routing(ref, record, "green")
        self.record_attempt_usage(ref, record, role=REVIEW_ROLE, attempt_id=attempt_id)
        kind, result, detail = self._merge_readiness(task, record, context)
        if kind == "transport":
            retry = self._gate_transport_retry(
                task,
                record,
                records,
                payload,
                attempt_id,
                GateTransportError(detail),
                step="review",
            )
            if retry is not None:
                return retry
            return self._block_gate_transport(
                task,
                record,
                records,
                payload,
                attempt_id,
                step="review",
                action="merge-gate-transport-blocked",
            )
        if kind == "drift":
            # The gate was never asked here; the bounce clears the record's gate state itself.
            return self._gate_red_to_worker(
                task, record, records, payload, attempt_id, GateResult("red", detail), phase="review-freeze"
            )
        self._gate_answered(ref, record, records, payload)
        if kind == "failed":
            return self._block_merge_path(
                task,
                record,
                records,
                payload,
                attempt_id,
                action="merge-gate-blocked",
                reason=f"merge gate failed: {detail}",
                step="review",
                outcome="merge gate failed",
            )
        if kind == "pending":
            if result is None:
                return self._block_merge_path(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    action="merge-gate-result-blocked",
                    reason="merge gate returned pending without a result payload",
                    step="review",
                    outcome="merge gate result unavailable",
                )
            return self._gate_pending(
                task,
                record,
                records,
                payload,
                attempt_id,
                result,
                step="review",
                action="merge-gate-pending",
            )
        if kind != "green":
            if result is None:
                return self._block_merge_path(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    action="merge-gate-result-blocked",
                    reason="merge gate returned a non-green state without a result payload",
                    step="review",
                    outcome="merge gate result unavailable",
                )
            return self._gate_red_to_worker(
                task, record, records, payload, attempt_id, result, phase="merge-gate"
            )
        if result is None:
            return self._block_merge_path(
                task,
                record,
                records,
                payload,
                attempt_id,
                action="merge-gate-result-blocked",
                reason="merge gate returned green without a result payload",
                step="review",
                outcome="merge gate result unavailable",
            )
        parks = self._parks_for_decision(task)
        blocked = self._accept_green_gate(
            task,
            record,
            records,
            payload,
            attempt_id,
            result,
            stage="assessment" if parks else "release",
        )
        if blocked is not None:
            return blocked
        if not parks:
            # No observer to release it, so the green verdict merges on its own tick.
            return self._release_effect(
                task,
                record,
                records,
                payload,
                attempt_id,
                step="review",
                move_reason="review:green",
            )
        # The checkout must be quiet while the card waits, so the reviewer's pane goes here. Its
        # pane address is all that is forgotten: the round's context outlives it, and is what the
        # release decision will still be checked against.
        unconfirmed = self._end_review_pane_confirmed(
            record,
            records,
            payload,
            ref,
            step="review",
            attempt_id=attempt_id,
            initiator=STOPPED_BY_REVIEW_VERDICT,
        )
        if unconfirmed is not None:
            return unconfirmed
        return self._begin_park(
            task,
            record,
            records,
            payload,
            attempt_id,
            verdict_outcome="green",
            move_reason=(
                "review:green. The card is parked in Assessment: the mechanical gate is green "
                "and the merge waits for a release, rework or reslice decision."
            ),
        )

    def _begin_park(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        verdict_outcome: str,
        move_reason: str,
    ) -> dict[str, Any]:
        """The only way a substantive verdict leaves Validate.

        The red transition's order, for the same reason: the intent is on disk, with the reason the
        card is moving, before anything observable moves. Nothing comes after the move — the card waits.

        Nothing is re-pinned here. The park is exactly the window in which the reviewer's pane is
        gone while the merge gate must still refuse a checkout that moved off the reviewed
        candidate, and the round's bound context is what carries that candidate across it.
        """
        ref = task["ref"]
        record.worker_continuation.begin_park(
            "review", len(task.get("comments") or []), move_reason, verdict_outcome
        )
        records[ref] = record
        self.save_records(payload, records)
        return self._complete_park(record, records, payload, attempt_id, ref=ref)

    def _complete_park(
        self,
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        ref: str,
    ) -> dict[str, Any]:
        """Finish an open park from the board as it is now.

        Keyed on the baseline the intent was opened against, so the tick that already moved the card
        and the tick recovering from a crash before that move run the same call and it moves once.
        """
        continuation = record.worker_continuation
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="assessment",
            reason=continuation.move_reason,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                "review-assessment",
                ref,
                str(continuation.report_baseline),
            ),
        )
        continuation.confirm_park()
        record.state = "assessment"
        _reset_wait(record, "review")
        records[ref] = record
        self.save_records(payload, records)
        return {
            "status": "ok",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "to": "assessment",
            "verdict": continuation.verdict_outcome,
        }

    def _advance_assessment(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
    ) -> dict[str, Any]:
        """A parked card. Nothing here runs a head, reads a gate or merges anything."""
        ref = task["ref"]
        record = records.get(ref)
        if record is None:
            try:
                record = self._adopt(task, attempt_id)
            except HostError as exc:
                return self._block_unresumable(task, records, payload, attempt_id, "assessment", exc)
            records[ref] = record
        continuation = record.worker_continuation
        if continuation.red_transition_pending:
            # A rework decision whose move did not commit: finish it before any decision is read.
            return self._complete_red_transition(task, record, records, payload, attempt_id, ref=ref)
        if continuation.assessment_pending:
            # The move landed but the checkpoint did not; re-issuing is a no-op by request id.
            return self._complete_park(record, records, payload, attempt_id, ref=ref)
        if not continuation.parked:
            # A record lost while parked, or a card an operator parked by hand: the board is the
            # fact. A session this record cannot prove is held is not held, so it owns no worker.
            continuation.begin_park(
                "review", len(task.get("comments") or []), "adopted parked card", "unknown"
            )
            continuation.confirm_park()
            record.state = "assessment"
            records[ref] = record
            self.save_records(payload, records)
        decision, reason = self._recorded_decision(task)
        if not decision:
            return {
                "status": "ok",
                "step": "assessment",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "action": "waiting-observer-decision",
            }
        if decision == "rework":
            return self._rework_parked(task, record, records, payload, attempt_id, reason=reason)
        if decision == "reslice":
            return self._reslice_parked(task, record, records, payload, attempt_id, reason=reason)
        return self._release_parked(task, record, records, payload, attempt_id, reason=reason)

    def _recorded_decision(self, task: dict[str, Any]) -> tuple[str, str]:
        """The decision standing on this card since it entered Assessment, with its reason."""
        decision = standing_decision(self.audit.events(task["ref"]))
        if not decision:
            return "", ""
        return decision, _last_marker_body(task, f"decision:{decision}") or ""

    def _rework_parked(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """A rework decision releases the round the park was holding back."""
        ref = task["ref"]
        # A parked card should have no reviewer left; an adopted one may still name a pane nobody
        # stopped. Either way nothing is woken beside a head the host will not confirm gone.
        if record.owns_head("review"):
            unconfirmed = self._end_review_pane_confirmed(
                record,
                records,
                payload,
                ref,
                step="assessment",
                attempt_id=attempt_id,
                initiator=STOPPED_BY_REVIEW_VERDICT,
            )
            if unconfirmed is not None:
                return unconfirmed
        # The findings are not repeated in the move: the rework prompt reads the card's last red
        # verdict directly. The decision is what the round is for, so it is frozen with the round.
        return self._begin_red_transition(
            task,
            record,
            records,
            payload,
            attempt_id,
            phase="review",
            move_reason=f"Observer decision: rework. {reason}".strip(),
            verdict_outcome="red",
            decision="rework",
            decision_body=reason,
        )

    def _reslice_parked(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """A reslice decision ends the attempt and leaves the card for a fresh cut."""
        ref = task["ref"]
        unconfirmed = self._stop_worker_confirmed(record, ref, step="assessment", attempt_id=attempt_id)
        if unconfirmed is not None:
            records[ref] = record
            self.save_records(payload, records)
            return unconfirmed
        self.host.stop(record)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=f"Observer decision: reslice. {reason}".strip(),
            decision="reslice",
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "assessment-reslice", ref),
        )
        resume_workspaces = payload.setdefault("resume_workspaces", {})
        if isinstance(resume_workspaces, dict):
            resume_workspaces[ref] = record.attempt_id or attempt_id
        records.pop(ref, None)
        self.save_records(payload, records)
        return {
            "status": "ok",
            "step": "assessment",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "to": "blocked",
            "decision": "reslice",
        }

    def _block_merge_path(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        action: str,
        reason: str,
        step: str,
        outcome: str,
        decision: str = "",
    ) -> dict[str, Any]:
        """A merge path that cannot finish leaves the card Blocked with its heads down."""
        ref = task["ref"]
        self.host.stop(record)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=reason,
            decision=decision,
            request_id=_attempt_request_id(record.attempt_id or attempt_id, action, ref),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {"status": "blocked", "step": step, "pilot_ref": ref, "reason": outcome}

    def _block_red_review_ceiling(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        reds: int,
    ) -> dict[str, Any]:
        """The last red review a card with no observer gets: Blocked instead of another round.

        The verdict is still recorded against the heads that earned it; what does not happen is the red
        transition. The workspace's terminals are stopped rather than the workspace removed, so the
        checkout and the branch stay where the last round left them.
        """
        self._record_verdict_routing(task["ref"], record, "red")
        return self._block_merge_path(
            task,
            record,
            records,
            payload,
            attempt_id,
            action="red-review-ceiling",
            reason=(
                f"review:red. This card has now collected {reds} substantive red reviews and its "
                f"sprint has no observer to decide for it, so the no-observer ceiling of "
                f"{RED_REVIEW_CEILING} is reached: the card is Blocked instead of opening another "
                f"worker round. The workspace and the branch are kept as the last round left "
                f"them; unblock the card to continue."
            ),
            step="review",
            outcome="red review ceiling reached",
        )

    def _release_parked(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Perform a release decision: re-check the mechanical state, then merge."""
        ref = task["ref"]
        try:
            # A release decides that one reviewed pair may land, and the park is exactly the window
            # in which nothing else on the record still names it: the reviewer's pane is gone and
            # the release gate below attests today's checkout, whatever the verdict judged. So the
            # bound context is required here, before the gate is asked, and a round that lost it —
            # absent, damaged or belonging to another round — stops on the board with the reason
            # instead of merging on mechanical evidence that was never about it.
            context = require_review_context(record)
        except ReviewContextError as exc:
            return self.block_review_context(
                task,
                record,
                records,
                payload,
                attempt_id,
                step="assessment",
                reason=scrub_host_output(str(exc)),
            )
        kind, result, detail = self._merge_readiness(task, record, context)
        if kind == "transport":
            # A release that could not ask the gate is not a release that was refused.
            retry = self._gate_transport_retry(
                task,
                record,
                records,
                payload,
                attempt_id,
                GateTransportError(detail),
                step="assessment",
            )
            if retry is not None:
                return retry
            return self._block_gate_transport(
                task,
                record,
                records,
                payload,
                attempt_id,
                step="assessment",
                action="release-gate-transport-blocked",
                prefix="Observer decision: release. ",
            )
        if kind != "drift":
            # `drift` is decided before the gate is asked; only an answer clears the budget.
            self._gate_answered(ref, record, records, payload)
        if kind == "pending":
            if result is None:
                return self._block_merge_path(
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    action="release-gate-result-blocked",
                    reason="merge gate returned pending without a result payload",
                    step="assessment",
                    outcome="merge gate result unavailable",
                )
            return self._gate_pending(
                task,
                record,
                records,
                payload,
                attempt_id,
                result,
                step="assessment",
                action="merge-gate-pending",
            )
        if kind != "green":
            summary = {
                "drift": f"the release cannot land: {detail}",
                "failed": f"the merge gate could not be read: {detail}",
            }.get(kind, "the mechanical gate is no longer green for the checkout this release was decided on")
            return self._block_merge_path(
                task,
                record,
                records,
                payload,
                attempt_id,
                action=f"release-{kind}-blocked",
                reason=f"Observer decision: release. {summary}",
                step="assessment",
                outcome=f"release {kind}",
            )
        if result is None:
            return self._block_merge_path(
                task,
                record,
                records,
                payload,
                attempt_id,
                action="release-gate-result-blocked",
                reason="merge gate returned green without a result payload",
                step="assessment",
                outcome="merge gate result unavailable",
            )
        blocked = self._accept_green_gate(task, record, records, payload, attempt_id, result, stage="release")
        if blocked is not None:
            return blocked
        return self._release_effect(
            task,
            record,
            records,
            payload,
            attempt_id,
            step="assessment",
            move_reason=f"Observer decision: release. {reason}".strip(),
            decision="release",
        )

    def _release_effect(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        step: str,
        move_reason: str,
        decision: str = "",
    ) -> dict[str, Any]:
        """Merge the reviewed branch, tear the round down and move the card to Done."""
        ref = task["ref"]
        try:
            self.host.complete_green(task, record)
        except HostError as exc:
            # A rejected merge must land the card in Blocked rather than escape the tick: an
            # escaping error leaves the verdict standing and every later tick retries the merge.
            return self._block_merge_path(
                task,
                record,
                records,
                payload,
                attempt_id,
                action="merge-blocked",
                reason=f"merge failed: {scrub_host_output(str(exc))}",
                step=step,
                outcome="merge failed",
            )
        self.host.teardown(record)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="done",
            reason=move_reason,
            decision=decision,
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "review-green", ref),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {"status": "ok", "step": step, "pilot_ref": ref, "attempt_id": attempt_id, "to": "done"}

    def _review_drift(
        self, task: dict[str, Any], record: DispatcherRecord, context: ReviewRoundContext
    ) -> str:
        """Has the checkout moved off the candidate the reviewer was pointed at? A verdict describes one
        code state; merging a different one lands work nobody reviewed. Returns the operator message for
        the bounce, or "" when the states match, or when the checkout cannot be read — an unreadable
        workspace is the gate's failure to report, not a silent bounce.

        The candidate is the round's bound context, handed in by the readiness check that already
        required it: this compares two code states and never decides whether a round has an identity.
        """
        current = self.host.head_commit(record)
        if not current or current == context.candidate_sha:
            return ""
        if self.host.is_instance_publish_recovery(task, record, context.candidate_sha, current):
            return ""
        return (
            f"The review was given for commit `{context.candidate_sha[:12]}` while the working copy "
            f"is now on `{current[:12]}`: the verdict describes a different state of the code. The "
            f"card is back in In progress; rework it and report again."
        )

    def head_run_snapshot(
        self,
        task: dict[str, Any],
        *,
        role: str,
        head: str = "",
        workspace: str = "",
        failover: bool = False,
    ) -> dict[str, Any]:
        """The launch snapshot for a head the runtime has no launcher record of, or a marked
        minimal one when its profile can no longer be read.
        """
        try:
            return self.catalog.head_run(
                task, role=role, head=head, workspace=workspace, failover=failover
            ).to_json()
        except (HostError, AttributeError, KeyError, TypeError):
            return HeadRun(role=role, head=str(head), adapter="unknown", model_source=MODEL_UNKNOWN).to_json()

    def _journal_round(self, ref: str) -> int:
        """The last worker round the journal holds for this card. Survives a lost dispatcher record,
        a restore, and a card that went back to Ready and was claimed again."""
        history = _routing_attempts(self.audit.events(ref, kind="routing"))
        return history[-1].attempt if history else 0

    def open_worker_round(self, record: DispatcherRecord, *, round_number: int = 0) -> None:
        """Start the card's next worker round: stamp its number and drop the previous round's heads."""
        record.attempt_round = round_number or (record.attempt_round + 1)
        record.worker_run = {}
        record.review_run = {}

    def record_worker_routing(
        self, task: dict[str, Any], record: DispatcherRecord, run: dict[str, Any] | None = None
    ) -> None:
        """Record the worker head this bring-up just put up, as launched."""
        ref = task["ref"]
        if not record.attempt_round:
            record.attempt_round = self._journal_round(ref) + 1
        snapshot = run or self.head_run_snapshot(
            task,
            role="worker",
            head=record.head,
            workspace=record.workspace,
            failover=bool(record.preferred_head),
        )
        snapshot = _launched_head_run_snapshot(snapshot, lifecycle_run=record.worker_head_run)
        if record.worker_run and _run_key(record.worker_run) == _run_key(snapshot):
            snapshot = record.worker_run
        record.worker_run = snapshot
        self._record_routing(ref, record, phase="worker", heads=[record.worker_run])

    def record_review_routing(
        self, task: dict[str, Any], record: DispatcherRecord, run: dict[str, Any] | None = None
    ) -> None:
        """Record the reviewer head this bring-up just put up, as launched."""
        ref = task["ref"]
        if not record.attempt_round:
            record.attempt_round = self._journal_round(ref) + 1
        snapshot = run or self.head_run_snapshot(
            task,
            role="reviewer",
            head=record.review_head,
            workspace=record.workspace,
            failover=bool(record.preferred_review_head),
        )
        snapshot = _launched_head_run_snapshot(snapshot, lifecycle_run=record.review_head_run)
        if record.review_run and _run_key(record.review_run) == _run_key(snapshot):
            snapshot = record.review_run
        record.review_run = snapshot
        self._record_routing(ref, record, phase="review", heads=[record.review_run])

    def _record_routing(
        self,
        ref: str,
        record: DispatcherRecord,
        *,
        phase: str,
        heads: list[dict[str, Any]],
        outcome: str = "",
    ) -> None:
        heads = [head for head in heads if head]
        if not heads or not record.attempt_round:
            return
        # The request id carries the launched configurations, not just the round: the same head writes
        # the same id and commits once, a different configuration appends. Same for a verdict's pair.
        parts = [str(record.attempt_round)]
        if outcome:
            parts.append(outcome)
        parts.extend(_run_key(head) for head in heads)
        request_id = _attempt_request_id(record.attempt_id, f"routing-{phase}", ref, "-".join(parts))
        # A tick can die after the journal commit but before its launch snapshot reaches dispatcher
        # state. Recovery cannot rediscover that provider conversation from a live workspace, so it
        # must reuse the committed event's exact dynamic facts rather than retry the same request id
        # with a newly-derived null session or a rewritten prompt digest.
        existing = self.audit.committed_event(request_id)
        if existing is not None:
            payload = existing.get("payload") if isinstance(existing, dict) else None
            recorded_heads = payload.get("heads") if isinstance(payload, dict) else None
            if isinstance(recorded_heads, list):
                by_role = {
                    str(head.get("role") or ""): head for head in recorded_heads if isinstance(head, dict)
                }
                for head in heads:
                    recorded = by_role.get(str(head.get("role") or ""))
                    if recorded is not None:
                        head.clear()
                        head.update(recorded)
            return
        self.writer.routing(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            payload=_routing_payload(
                attempt=record.attempt_round,
                attempt_id=record.attempt_id,
                phase=phase,
                heads=heads,
                outcome=outcome,
            ),
            request_id=request_id,
        )

    def pending_attempt_usage(self) -> list[str]:
        """Card refs whose canonical usage occurrence still awaits export publication."""
        canon = self.writer.board_host.canon
        if canon is None:
            return []
        return [
            occurrence.event.ref for occurrence in canon.attempt_usage_occurrences() if occurrence.pending
        ]

    def publish_pending_attempt_usage(self) -> list[dict[str, Any]]:
        """Publish every staged `attempt.usage` occurrence the installation still owes.

        The single enforcement site of the durability order. A phase is accounted for as soon as its
        occurrence is staged, and the card is then free to advance — including into Blocked or Done,
        which no later step of the tick looks at again. So the obligation is finished from the
        pending set itself: no dispatcher record, no board lookup, no card state, nothing a terminal
        card has already given up. It runs before observer fencing, before the active cycle is read
        and before any phase boundary is, because every one of those reads a journal these records
        belong in.

        The canonical committed-plus-pending projection is also the source of recovery obligations;
        recovery does not interpret the pending directory independently. Publishing the exact staged
        record is the whole of it. A failure publishes nothing in its
        place: the record stays pending, stays exact, and is eligible again on every later permitted
        tick, whatever state its card has reached by then.
        """
        try:
            owed = self.pending_attempt_usage()
        except Exception as exc:  # noqa: BLE001 - an unreadable pending set is reported, not raised
            return [
                {
                    "status": "degraded",
                    "step": "attempt-usage-recovery",
                    "action": "attempt-usage-pending-unreadable",
                    "reason": (
                        "staged usage occurrences could not be read, so any obligation among them "
                        f"is still owed: {type(exc).__name__}: {exc}"
                    ),
                }
            ]
        if not owed:
            return []
        failure = ""
        try:
            self.writer.finish_attempt_usage(role="dispatcher")
        except Exception as exc:  # noqa: BLE001 - the obligation outlives its own recovery failing
            failure = f"{type(exc).__name__}: {exc}"
        try:
            remaining = self.pending_attempt_usage()
        except Exception as exc:  # noqa: BLE001 - unknown is owed, not settled
            remaining = list(owed)
            failure = failure or f"{type(exc).__name__}: {exc}"
        outcome: dict[str, Any] = {
            "status": "degraded" if remaining else "ok",
            "step": "attempt-usage-recovery",
            "action": "attempt-usage-still-pending" if remaining else "attempt-usage-published",
            "published": max(0, len(owed) - len(remaining)),
            "pending": len(remaining),
            "refs": sorted({ref for ref in owed if ref}),
        }
        if remaining:
            outcome["pending_refs"] = sorted({ref for ref in remaining if ref})
            outcome["reason"] = (
                f"{len(remaining)} staged usage occurrence(s) could not be published and stay owed"
                + (f": {failure}" if failure else "")
            )
        return [outcome]

    def record_attempt_usage(self, ref: str, record: DispatcherRecord, *, role: str, attempt_id: str) -> None:
        """Persist what the phase that just finished cost, before the card can advance past it.

        Called on the acceptance paths themselves — a terminal worker report, a reviewer verdict —
        because that is the last point at which the exact run that did the work is still on the
        record with its bound provider session.

        Two failures, and they are not the same failure. Reading the provider never decides
        anything: an unbound session, an unreadable journal and malformed records are named degraded
        outcomes inside the occurrence, and the report or verdict is accepted exactly as it would
        have been. Failing to make the occurrence durable is an audit failure, and this method
        refuses to swallow it: the control event and the transition may not outrun the account of
        the phase they close. A staged-but-unappended obligation is durable enough to advance past,
        because the canonical occurrence projection makes it authoritative immediately and the global
        publication reconciler later appends that exact record.
        """
        try:
            self._write_attempt_usage(ref, record, role=role, attempt_id=attempt_id)
        except TaskError as exc:
            if exc.code != "audit_pending":
                raise
            # The exact occurrence is staged in the append-only audit. The phase is accounted for;
            # only its publication is outstanding, and a later tick finishes it.
            return

    def _write_attempt_usage(self, ref: str, record: DispatcherRecord, *, role: str, attempt_id: str) -> None:
        phase = "worker" if role == WORKER_ROLE else "review"
        journal_role = WORKER if role == WORKER_ROLE else REVIEWER
        # A round is what binds the occurrence to a phase. Every accepted terminal report has one;
        # a record rebuilt without one still owes the phase an account, so the first round answers
        # for it rather than the occurrence being dropped.
        attempt = max(record.attempt_round or self._journal_round(ref), 1)
        generation = max(record.report_generation, 1)
        snapshot = dict(record.worker_run if role == WORKER_ROLE else record.review_run)
        lifecycle = dict(record.worker_head_run if role == WORKER_ROLE else record.review_head_run)
        if not snapshot:
            # A recovered record can hold the head's own run without the routing snapshot of its
            # configuration. The head's attested launch spec answers for the adapter; re-reading
            # today's `heads.toml` for a head launched hours ago would not.
            snapshot = _usage_fallback_snapshot(journal_role, record, lifecycle, role=role)
        try:
            snapshot = _launched_head_run_snapshot(snapshot, lifecycle_run=lifecycle)
        except ValueError:
            # An incomplete launch attestation is not a reason to drop the occurrence: the run
            # below still reports its own adapter, model and whatever session identity it holds.
            pass
        run = HeadRun.from_json(snapshot)
        # One order, for every provider and every lifecycle path. Projection integrity and causal
        # identity first, because neither depends on what a provider journal says and a phase slot
        # owned by another attempt may not be written whatever that journal would have said.
        try:
            occurrences = self.writer.board_host.canon.attempt_usage_occurrences(ref=ref)
            predecessor = _causal_predecessor(
                occurrences,
                adapter=run.adapter,
                session_id=run.session_id or "",
                attempt=attempt,
                attempt_id=record.attempt_id or attempt_id,
                report_generation=generation,
                phase=phase,
                role=journal_role,
            )
        except (OSError, TypeError, ValueError, TaskError) as exc:
            raise TaskError(
                "audit_unavailable", f"attempt usage projection is unreadable: {exc}", 4
            ) from None
        # The provider read second: a whole-session total, with no arithmetic of its own.
        collection = _collect_usage(
            adapter=run.adapter,
            source=_provider_usage_source(lifecycle, adapter=run.adapter),
        )
        # Attribution and cross-account validation third, in the one place that does either.
        try:
            collection = _attribute_phase(collection, predecessor)
        except (TypeError, ValueError) as exc:
            raise TaskError(
                "audit_unavailable", f"attempt usage projection is unreadable: {exc}", 4
            ) from None
        data = _attempt_usage_data(
            attempt=attempt,
            attempt_id=record.attempt_id or attempt_id,
            phase=phase,
            role=journal_role,
            report_generation=generation,
            head=run.head,
            adapter=run.adapter or "unknown",
            model=run.model,
            model_source=run.model_source,
            session_id=run.session_id,
            session_id_reason=(
                run.session_id_reason
                or ("" if run.session_id else "no provider session identity was recorded for this run")
            ),
            launch_id=run.launch_id,
            collection=collection,
        )
        self.writer.attempt_usage(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            data=data,
            reason=_attempt_usage_reason(data),
            # One occurrence per completed phase: the round it closed names it, so a re-entered
            # acceptance and a replayed request commit the same event rather than a second one.
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                f"attempt-usage-{phase}",
                ref,
                f"{attempt}-{generation}",
            ),
        )

    def _record_verdict_routing(self, ref: str, record: DispatcherRecord, outcome: str) -> None:
        """Tie the round's outcome to the heads that earned it, carrying both so worker-reviewer
        pairs group by outcome without a join against the launch records."""
        self._record_routing(
            ref,
            record,
            phase="verdict",
            heads=[record.worker_run, record.review_run],
            outcome=outcome,
        )

    def save_records(self, payload: dict[str, Any], records: dict[str, DispatcherRecord]) -> None:
        """Flush the dispatcher records into the production state."""
        self.production_state.put_records(payload, records)
        payload["last_tick_at"] = now_rfc3339()
        self.production_state.save(payload)

    def _adopt(self, task: dict[str, Any], attempt_id: str) -> DispatcherRecord:
        worker = task.get("claim", {}).get("worker") or _worker_id(task)
        review_baseline = _review_adoption_baseline(task)
        launched = self._review_launch_recorded(task, review_baseline)
        state = "review_starting" if launched else "adopted"
        if task.get("state") == "assessment":
            # A parked card has no head to recover: the reviewer was stopped when it parked, and a
            # worker still suspended in the checkout is not something this record can prove.
            state = "assessment"
        # The routing round of a lost record comes back from the journal, heads included:
        # re-reading the registry would report today's `heads.toml` for a head launched hours ago.
        resumed = _routing_attempts(self.audit.events(task["ref"], kind="routing"))
        round_record = resumed[-1] if resumed else None
        workspace = self.host.restore_workspace(task, worker)
        # The report generation is dispatcher state, lost on this path. The TASK.md names the round
        # the live worker is in; the board's reports are the floor with no readable document. Both
        # are lower bounds, so the larger one is taken: a generation may skip, never repeat.
        report_generation = max(_task_doc_report_generation(workspace), _spent_report_generations(task) + 1)
        # And the decision that round was opened on, from the same document. The card's newest
        # decision comment answers "what was decided since", which must not reach a running round.
        report_decision = _task_doc_decision(workspace)
        record = DispatcherRecord(
            worker=worker,
            workspace=workspace,
            handle="",
            head=self.catalog.claimed_worker_head(task),
            review_head=self.catalog.claimed_review_head(task),
            attempt_id=attempt_id,
            comment_baseline=_report_adoption_baseline(task),
            review_baseline=review_baseline,
            report_generation=report_generation,
            report_decision=report_decision,
            state=state,
            claimed_at=time.time(),
            # A reviewer launches only over a green gate, so a card in review inherits a passed gate.
            gate_state="green" if launched else "",
            attempt_round=round_record.attempt if round_record else 0,
            worker_run=round_record.worker.to_json() if round_record and round_record.worker else {},
            review_run=round_record.reviewer.to_json() if round_record and round_record.reviewer else {},
        )
        # A lost record may be recovered from the worker's own heartbeat, but only after its
        # self-described run, role and card binding are promoted into a HeadRun and checked again.
        # A legacy pid or another card's process stays unbound and is never signalled.
        pid_file = _launch_pid_file(WORKER_ROLE, task["ref"])
        heartbeat = _head_process_status(pid_file) if task.get("state") == "in_progress" else {}
        raw = heartbeat.get("record") if isinstance(heartbeat.get("record"), dict) else {}
        if (
            _heartbeat_is_live_match(heartbeat)
            and str(raw.get("role") or "") == WORKER_ROLE
            and str(raw.get("task") or "") == f"card:{task['ref']}"
            and str(raw.get("run_id") or "")
        ):
            recovered = head_ops.HeadRun(
                run_id=str(raw["run_id"]),
                spec=HeadSpec(
                    profile_id=record.head,
                    adapter=str(record.worker_run.get("adapter") or "unknown"),
                ),
                workspace=workspace,
                task_ref=head_ops.TaskRef.card(task["ref"]),
                leaf=str(raw.get("leaf") or ""),
                pid_file=pid_file,
            )
            verified = _head_run_process_status(
                pid_file,
                run=recovered,
                role=WORKER_ROLE,
                leaf=recovered.leaf,
            )
            if _heartbeat_is_live_match(verified):
                record.worker_head_run = recovered.to_json()
                record.worker_pid_file = pid_file
                record.worker_started_at = record.worker_progress_at = time.time()
        return record

    def _review_launch_recorded(self, task: dict[str, Any], review_baseline: int) -> bool:
        if task.get("state") != "validate":
            return False
        return self.audit.committed_event(_review_launch_request_id(task["ref"], review_baseline)) is not None


def runtime_from_args(
    instance: str, data_dir: str | None, *, host_mode: str, owner: str
) -> DispatcherRuntime:
    instance_path = Path(instance)
    data = Path(data_dir).expanduser() if data_dir else default_data_dir(instance_path)
    client = KanboardClient.for_instance(instance_path)
    catalog = InstanceCatalog(instance_path)
    return DispatcherRuntime(
        TaskReader(client),
        TaskWriter(client, data_dir=data),
        TaskAudit(data),
        data,
        catalog,
        CommandHostRuntime(catalog, data, mode=host_mode),
        owner=owner,
        checkpoint=CheckpointWriter(data, catalog.instance_dir),
        checkpoint_push=CheckpointPusher(catalog.instance_dir),
    )


def _review_launch_request_id(reference: str, review_baseline: int) -> str:
    return _attempt_request_id("review", "start-intent", reference, str(review_baseline))


def _retained_worker_busy_deferred(
    reference: str,
    record: DispatcherRecord,
    attempt_id: str,
    phase: str,
    *,
    delay: int | None = None,
) -> dict[str, Any]:
    """Report a retained continuation held by its own busy pane without changing ownership."""
    continuation = record.worker_continuation
    remaining = max(0, int(continuation.busy_next_at - time.time()))
    wait = delay if delay is not None else remaining
    return {
        "status": "degraded",
        "step": "review" if phase == "review" else "gate",
        "pilot_ref": reference,
        "attempt_id": record.attempt_id or attempt_id,
        "action": f"{phase}-red-worker-busy",
        "attempts": continuation.busy_attempts,
        "reason": (
            "the retained worker pane is busy before its continuation was delivered; its exact "
            f"HeadRun remains owned and the pending delivery retries in {wait}s"
        ),
    }


def _retained_worker_recovery_window(
    reference: str,
    record: DispatcherRecord,
    attempt_id: str,
    phase: str,
    *,
    remaining: int,
) -> dict[str, Any]:
    """Expose a persisted provider-safe recovery wait without pretending it is a busy retry."""
    liveness = record.worker_continuation_liveness
    return {
        "status": "degraded",
        "step": "review" if phase == "review" else "gate",
        "pilot_ref": reference,
        "attempt_id": record.attempt_id or attempt_id,
        "action": f"{phase}-red-worker-recovery-window",
        "attempts": liveness.busy_attempts,
        "reason": (
            "a provider/terminal-safe continuation recovery is awaiting its recorded response "
            f"window for the exact retained HeadRun ({max(0, remaining)}s remaining)"
        ),
    }


def _continuation_no_progress_evidence(
    record: DispatcherRecord,
    state: ContinuationLivenessState,
) -> str:
    """Classify unchanged provider evidence without retaining or interpreting pane text."""
    if state == ContinuationLivenessState.UNAVAILABLE:
        return "provider_unavailable"
    if state == ContinuationLivenessState.UNKNOWN:
        return "provider_or_identity_unknown"
    evidence = record.worker_delivery_evidence if isinstance(record.worker_delivery_evidence, dict) else {}
    composer_before = str(evidence.get("composer_before") or "")
    composer_after = str(evidence.get("composer_after") or "")
    cursor_before = str(evidence.get("cursor_before") or "")
    cursor_after = str(evidence.get("cursor_after") or "")
    if (
        composer_before
        and composer_before == composer_after
        and composer_before not in {COMPOSER_EMPTY, COMPOSER_UNKNOWN}
        and cursor_before
        and cursor_before == cursor_after
    ):
        return "completed_turn_residual_composer"
    return "active_or_unknown_turn"


def _wait_expectation(kind: str) -> str:
    return "review verdict" if kind == "review" else "worker report"
