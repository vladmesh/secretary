"""Production dispatcher runtime."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from secretary.board.completion_evidence import (
    RESEARCH_REPORT_DIR,
    has_candidate,
    missing_completion_evidence,
    render_research_completion_link,
    research_report_path,
    research_report_refusal,
    review_required,
)
from secretary.board.outcome_round_context import OutcomeRoundContext, OutcomeRoundPhase
from secretary.board.protocol_artifacts import ArtifactOwnershipViolation, validate_rework_prerequisites
from secretary.board.terminal_taxonomy import (
    TerminalTaxonomy,
    TerminalTaxonomyValidationError,
    normalize_terminal_taxonomy,
)
from secretary.checkpoint import CheckpointPusher, CheckpointWriter
from secretary.codex_provider_events import (
    CodexProviderSourceError,
)
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
from secretary.dispatch.claim import (
    SPRINT_RESERVATION_BLOCKED_ACTION,  # noqa: F401  # Compatibility re-export.
    SPRINT_RESERVATION_RESERVED,  # noqa: F401  # Compatibility re-export.
    SPRINT_RESERVATION_UNVERIFIABLE,  # noqa: F401  # Compatibility re-export.
)
from secretary.dispatch.claim import (
    claim_ready_task as _claim_ready_task,
)
from secretary.dispatch.claim import (
    resolve_head as _resolve_claim_head,
)
from secretary.dispatch.gate import (
    GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS,
    GATE_PENDING_STALL_SECONDS,
    GATE_TRANSPORT_MAX_ATTEMPTS,
    GateResult,
)
from secretary.dispatch.gate import (
    _fingerprint as _gate_fingerprint,
)
from secretary.dispatch.gate import (
    validation_ci as _validation_ci,
)
from secretary.dispatch.gate_receipt import (
    AcceptedGreenGate,
)
from secretary.dispatch.head_vitality_episode import (
    VitalityVerdict as VitalityVerdict,
)
from secretary.dispatch.helpers import (
    RED_REVIEW_CEILING,
    _gate_red_repeat_count,
    _last_marker,
    _last_marker_body,
    _last_review_red_body,
    _report_adoption_baseline,
    _review_adoption_baseline,
    _round_report_ids,
    _spent_report_generations,
    _task_doc_decision,
    _task_doc_protocol_prerequisites,
    _task_doc_report_generation,
    _worker_id,
    scrub_host_output,
)
from secretary.dispatch.helpers import (
    red_review_count as _red_review_count,
)
from secretary.dispatch.helpers import (
    safe_one_line as _safe_one_line,
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
    _body_file_instructions,
    _body_file_path,
    _continuation_note,
    _durable_head_run,
    _gate_attestation_for_prompt,
    _head_runtime_name,
    _legacy_worker_branch,
    _record_worker_delivery_evidence,
    _report_nudge_prompt,
    _same_repo,
    _watchdog_kind,
)
from secretary.dispatch.host import (
    LaunchedHead as LaunchedHead,  # Compatibility re-export.
)
from secretary.dispatch.launch import (
    REVIEW_ROLE,
    WORKER_ROLE,
)
from secretary.dispatch.launch import (
    bring_up_blocked_action as _bring_up_blocked_action,
)
from secretary.dispatch.launch import (
    bring_up_blocked_reason as _bring_up_blocked_reason,
)
from secretary.dispatch.launch import (
    bring_up_terminal_reason as _bring_up_terminal_reason,
)
from secretary.dispatch.launch import (
    classify_bring_up_failure as _classify_bring_up_failure,
)
from secretary.dispatch.launch import (
    forget_role_head as _forget_role_head,
)
from secretary.dispatch.launch import (
    head_stop_unconfirmed as _head_stop_unconfirmed,
)
from secretary.dispatch.launch import (
    launch_pid_file as _launch_pid_file,
)
from secretary.dispatch.launch import (
    merge_launch_head_run as _merge_launch_head_run,
)
from secretary.dispatch.launch import (
    resolve_launch_intent as _resolve_launch_intent,
)
from secretary.dispatch.pause import ProductionPause
from secretary.dispatch.pause_ops import (
    pause as _pause_pipeline,
)
from secretary.dispatch.pause_ops import (
    pause_status as _pause_status,
)
from secretary.dispatch.pause_ops import (
    resume as _resume_pipeline,
)
from secretary.dispatch.production import (
    ProductionState,
)
from secretary.dispatch.production import (
    production_observe as _production_observe,
)
from secretary.dispatch.production import (
    production_probe as _production_probe,
)
from secretary.dispatch.production import (
    production_run as _production_run,
)
from secretary.dispatch.production import (
    production_tick as _production_tick,
)
from secretary.dispatch.review import (
    end_review_pane as _end_review_pane,
)
from secretary.dispatch.review import (
    recover_review_launch as _recover_review_launch,
)
from secretary.dispatch.review import (
    start_review as _start_review,
)
from secretary.dispatch.state import (
    REVIEW_REJECTION_REASON,
    DispatcherRecord,
    OutcomeTerminalPath,
    now_rfc3339,
)
from secretary.dispatch.state import (
    attempt_request_id as _attempt_request_id,
)
from secretary.dispatch.state import (
    claim_mismatch as _claim_mismatch,
)
from secretary.dispatch.state import (
    outcome_terminal_path as _outcome_terminal_path,
)
from secretary.dispatch.state import (
    request_token as _request_token,
)
from secretary.dispatch.types import (
    STOPPED_BY_DISPATCHER,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_OPERATOR,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_RECONCILIATION,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_REPLACEMENT,
    STOPPED_BY_REVIEW_FREEZE,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_REVIEW_VERDICT,
    STOPPED_BY_WATCHDOG,  # noqa: F401  # Public compatibility re-export.
    GateTransportError,
    HostError,
    ProjectGitAccessError,
)
from secretary.dispatch.types import DispatcherError as DispatcherError
from secretary.dispatch.watchdog import (
    head_process_status as _head_process_status,
)
from secretary.dispatch.watchdog import (
    head_run_process_status as _head_run_process_status,
)
from secretary.dispatch.watchdog import (
    heartbeat_is_live_match as _heartbeat_is_live_match,
)
from secretary.dispatch.watchdog import (
    reset_wait as _reset_wait,
)
from secretary.dispatch.worker_continuation import (
    begin_red_transition as _begin_red_transition,
)
from secretary.dispatch.worker_continuation import (
    complete_red_transition as _complete_red_transition,
)
from secretary.dispatch.worker_continuation import (
    recover_worker_continuation as _recover_worker_continuation,
)
from secretary.dispatch.wait_vitality import (
    execute_recovery_intent as _execute_recovery_intent,
)
from secretary.dispatch.wait_vitality import (
    recovery_policy_outcome as _recovery_policy_outcome,
)
from secretary.dispatch.wait_vitality import (
    reduce_and_store_vitality_episode as _reduce_and_store_vitality_episode,
)
from secretary.dispatch.wait_vitality import wait_watchdog as _wait_watchdog
from secretary.dispatch.worker_launch import (
    launch_worker_after_claim as _launch_worker_after_claim,
)
from secretary.dispatch.worker_launch import (
    resolve_headless_worker as _resolve_headless_worker,
)
from secretary.dispatch.worker_report import (
    handle_worker_report as _handle_worker_report,
)
from secretary.dispatch.worker_report import (
    worker_report_marker as _worker_report_marker,
)
from secretary.head_health import (
    HeadChoice,
    HeadHealth,
    HeadReadiness,
)
from secretary.knowledge_write import KnowledgeError, KnowledgeValidationError, write_knowledge_directory
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
    routing_head_snapshot_from_launch as _routing_head_snapshot_from_launch,
)
from secretary.routing_journal import (
    routing_payload as _routing_payload,
)
from secretary.routing_journal import (
    run_key as _run_key,
)
from secretary.sprints import SprintReader, budget_thresholds
from secretary.state_repo import StateRepoError
from secretary.tasks import (
    TaskAudit,
    TaskError,
    TaskReader,
    TaskWriter,
    _event_payload,
    assessment_resolution,
    specification_revision,
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
        self.data_dir = Path(data_dir)
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
            else SprintReader(reader.client, data_dir=self.data_dir, thresholds=limits)
        )

    def head_readiness(self, head: str) -> HeadReadiness:
        return self.head_health.check(head)

    def resolve_head(self, preferred: str) -> HeadChoice:
        """Compatibility entry point; claim-owned resolution lives in dispatch.claim."""
        return _resolve_claim_head(self, preferred)

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
            self.terminal_effect(
                {"ref": reference},
                record,
                target="blocked",
                reason=(
                    "Codex provider fan-out policy blocked this head: "
                    f"{evidence.get('state') or 'unknown'}; {evidence.get('reason') or 'provider event observed'}"
                ),
                request_id=_attempt_request_id(
                    record.attempt_id, "codex-provider-event-blocked", reference, role, run.run_id
                ),
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="provider",
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
            return _claim_ready_task(
                self,
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

    def _claim(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        resume_workspace: bool = False,
    ) -> dict[str, Any]:
        """Compatibility entry point; production claim ownership lives in dispatch.claim."""
        return _claim_ready_task(
            self,
            task,
            records,
            payload,
            attempt_id,
            resume_workspace=resume_workspace,
        )

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
                        return _launch_worker_after_claim(self, task, record, records, payload)
        if record.worker_continuation.red_transition_pending:
            # An open red transition outranks everything else. The board move may or may not have
            # committed before its tick died, so it is finished against the board as it is now.
            return _complete_red_transition(self, task, record, records, payload, attempt_id, ref=ref)
        if record.state == "claim_verified":
            return _launch_worker_after_claim(self, task, record, records, payload)
        marker = _worker_report_marker(self, task, record, records, payload, attempt_id)
        recovered = _recover_worker_continuation(
            self, task, record, records, payload, attempt_id, marker=marker
        )
        if recovered is not None:
            return recovered
        reported = _handle_worker_report(
            self, task, record, records, payload, attempt_id, marker=marker
        )
        if reported is not None:
            return reported
        # Before any wait: a card cannot wait for a report from a worker no record can name. The
        # watchdog below observes a head; this decides whether there is one to observe at all.
        headless = _resolve_headless_worker(self, task, record, records, payload, attempt_id)
        if headless is not None:
            return headless
        watchdog = _wait_watchdog(self, task, record, records, payload, attempt_id, kind="worker")
        if watchdog is not None:
            return watchdog
        return {
            "status": "ok",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "waiting-worker-report",
        }

    def _transfer_research_report(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        step: str,
    ) -> dict[str, Any] | None:
        """Move a research card's report directory into knowledge and link it; None when done.

        `<workspace>/.secretary-report/` replaces `state/knowledge/reports/<ref>/` through the knowledge
        directory writer, then one `[completion:research]` comment keyed on the report generation is
        written, so a replayed tick commits nothing new and writes no second link. A refused or failed
        transfer Blocks the card with the cause named, keeps the workspace and writes no link. Any
        other kind answers None at once.
        """
        if task.get("type") != "research":
            return None
        ref = task["ref"]
        generation = str(record.report_generation)
        source = Path(record.workspace) / RESEARCH_REPORT_DIR
        refusal, message = "", ""
        if research_report_refusal(Path(record.workspace)):
            refusal = "report_missing"
            message = f"the workspace holds no non-empty {RESEARCH_REPORT_DIR}/report.md"
        else:
            try:
                write_knowledge_directory(
                    Path(self.catalog.instance_dir),
                    directory=research_report_path(ref),
                    actor="dispatcher",
                    source_dir=source,
                    message=(
                        f"knowledge: research report of {ref}, report generation {generation}\n\n"
                        f"Principal: dispatcher\nDocument: {research_report_path(ref)}\n"
                    ),
                )
            except KnowledgeValidationError as exc:
                refusal, message = exc.reason or "refused", str(exc)
            except (KnowledgeError, StateRepoError, OSError) as exc:
                refusal, message = "write_failed", str(exc)
        if refusal:
            outcome = self._block_merge_path(
                task,
                record,
                records,
                payload,
                attempt_id,
                action="research-report-transfer-refused",
                reason=(
                    f"research report transfer refused ({refusal}): {scrub_host_output(message)}. "
                    f"Nothing was linked and the card cannot be Done; the workspace and its "
                    f'`{RESEARCH_REPORT_DIR}/` are kept. See docs/PROTOCOLS.md, "Card kinds, live impact '
                    'and the review choice".'
                ),
                step=step,
                outcome="research report transfer refused",
            )
            outcome["transfer_refusal"] = refusal
            return outcome
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            body=render_research_completion_link(ref),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id, "completion-research", ref, generation
            ),
        )
        return None

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
            return _complete_red_transition(self, task, record, records, payload, attempt_id, ref=ref)
        marker = _last_marker(task, record.review_baseline, {"review:green", "review:red"})
        if marker == "review:green":
            self._capture_outcome_source(task, record, phase="verdict", kind="card.verdict", marker=marker)
            return self._park_green_verdict(task, record, records, payload, attempt_id)
        if marker == "review:red":
            self._capture_outcome_source(task, record, phase="verdict", kind="card.verdict", marker=marker)
            # Only the reviewer's lifecycle ends here: a full `stop` would take the worktree's
            # terminals down, and this checkout is about to be parked and is never re-created from
            # base. An unconfirmed stop ends the tick before the card moves. The commit is read
            # first: ending the reviewer forgets the commit it judged and the park has to keep it.
            reviewed = record.review_commit or self.host.head_commit(record)
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
            record.rejected_failure_reason = REVIEW_REJECTION_REASON
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
                return _begin_red_transition(self, 
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
                reviewed_commit=reviewed,
                move_reason=(
                    "review:red. The card is parked in Assessment: the reviewer is stopped and "
                    "the worker of this round is held, waiting for a release, rework or reslice "
                    "decision."
                ),
            )
        # Mechanical gate: a fresh report clears the cheap CI/local gate before the expensive
        # reviewer is spawned. A review already in flight cleared the gate when it launched.
        # A research/infra card has no candidate, so it has no mechanical gate at all.
        if (
            has_candidate(task)
            and record.state not in ("review_starting", "reviewing")
            and record.gate_state != "green"
        ):
            gated = self._run_gate(task, record, records, payload, attempt_id)
            if gated is not None:
                return gated
        if not review_required(task) and record.state not in ("review_starting", "reviewing"):
            # `review: skipped`: no reviewer for any kind. The accepted report takes the path a green
            # verdict takes, which for a code card still re-reads the gate and merges on release.
            return self._park_green_verdict(task, record, records, payload, attempt_id, reviewed=False)
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
            self._persist_outcome_round_context(task, record, phase="review")
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
        watchdog = _wait_watchdog(self, task, record, records, payload, attempt_id, kind="review")
        if watchdog is not None:
            return watchdog
        return {
            "status": "ok",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "waiting-review-verdict",
        }

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
            self.terminal_effect(
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
            self.save_records(payload, records)
            outcome = {
                "status": "blocked",
                "step": "gate",
                "pilot_ref": ref,
                "reason": "validation gate failed",
            }
            if isinstance(exc, ProjectGitAccessError):
                # A refused credential is its own determinate class, never the transport retry.
                outcome["git_access_refusal"] = {"project": exc.project, "code": exc.code}
            return outcome
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
        record.gate_attestation = accepted.receipt if accepted.receipt is not None else {}
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
        self.terminal_effect(
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
        if result.failure_class == "topology":
            # The candidate was never offered to CI, and no round of rework could change that: the
            # pull request's base or the project's triggers are what is wrong (secretary-1541).
            # Sending the worker back over its own code would spend a round on the wrong file, so
            # this goes to a human with the cause named instead.
            return self._block_merge_path(
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
            return self._retry_infrastructure_gate(
                task,
                record,
                records,
                payload,
                attempt_id,
                result,
                phase=phase,
            )
        record.rejected_sha = self.host.head_commit(record)
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
        return _begin_red_transition(self, 
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
        self.terminal_effect(
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
        self.terminal_effect(
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
        self.save_records(payload, records)
        return {
            "status": "blocked",
            "step": phase,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "gate-infrastructure-rerun-blocked",
            "reason": result.failure_reason,
        }


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
        record = records.get(ref)
        if record is not None and record.attempt_id == attempt_id:
            self.terminal_effect(
                task,
                record,
                target="blocked",
                reason=f"claimed head is unavailable: {scrub_host_output(str(error))}",
                request_id=_attempt_request_id(attempt_id, "adopt-head-blocked", ref),
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="other",
            )
        else:
            # A lost record that cannot be adopted has no durable round context.
            # The lifecycle effect still wins, but v1 cannot manufacture its key.
            self.terminal_effect(
                task,
                DispatcherRecord(
                    worker="",
                    workspace="",
                    handle="",
                    head="",
                    review_head="",
                    attempt_id="",
                    comment_baseline=0,
                    review_baseline=0,
                    state="",
                    claimed_at=0.0,
                ),
                target="blocked",
                reason=f"claimed head is unavailable: {scrub_host_output(str(error))}",
                request_id=_attempt_request_id(attempt_id, "adopt-head-blocked", ref),
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="other",
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
        self.terminal_effect(
            {"ref": ref},
            record,
            target="blocked",
            reason=blocked_reason,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                _bring_up_blocked_action(action, failure),
                ref,
                request_suffix,
            ),
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason=_bring_up_terminal_reason(failure),
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
                return _execute_recovery_intent(self, 
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
                outcome = _recovery_policy_outcome(self, 
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
        self.terminal_effect(
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
            return _reduce_and_store_vitality_episode(self, 
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
        self, task: dict[str, Any], record: DispatcherRecord
    ) -> tuple[str, GateResult | None, str]:
        """Everything that must hold before this checkout may be merged, read once.

        Returns one of "drift", "transport", "failed", "pending", "red" or "green". Both sides of the
        seam ask it: Validate before parking a green verdict, and the release again immediately before
        the merge. "transport" is deliberately not "failed" — a backend that could not be reached says
        nothing about the checkout, so the caller retries rather than deciding the card on silence.
        """
        drift = self._review_drift(task, record)
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
        *,
        reviewed: bool = True,
    ) -> dict[str, Any]:
        """A green review verdict, or an accepted report with review skipped, parks the card.

        It does not merge it. A card without a candidate has no gate to re-read and nothing to merge,
        so it goes straight to the park or, with nobody to decide, to the release.
        """
        ref = task["ref"]
        if reviewed:
            # Recorded before the gate: this round's head pair is a fact a red re-check cannot undo.
            self._record_verdict_routing(ref, record, "green")
            self.record_attempt_usage(ref, record, role=REVIEW_ROLE, attempt_id=attempt_id)
        if has_candidate(task):
            gated = self._merge_ready_for_park(task, record, records, payload, attempt_id)
            if gated is not None:
                return gated
        else:
            # Before the park or the release: the observer decides with the report in knowledge.
            refused = self._transfer_research_report(
                task, record, records, payload, attempt_id, step="review"
            )
            if refused is not None:
                return refused
        parks = self._parks_for_decision(task)
        if not parks:
            # No observer to release it, so the green verdict merges on its own tick.
            return self._release_effect(
                task,
                record,
                records,
                payload,
                attempt_id,
                step="review",
                move_reason="review:green" if reviewed else "report:done, review skipped",
                verdict="green" if reviewed else "missing",
            )
        # The checkout must be quiet while the card waits, so the reviewer's pane goes here — but
        # its commit is read first, because ending the reviewer forgets the commit it judged.
        pinned = (record.review_commit or self.host.head_commit(record)) if has_candidate(task) else ""
        if reviewed:
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
        if not has_candidate(task):
            waits = "there is no candidate to merge, and Done waits"
        elif reviewed:
            waits = "the mechanical gate is green and the merge waits"
        else:
            waits = "the mechanical gate is green, no reviewer runs, and the merge waits"
        return self._begin_park(
            task,
            record,
            records,
            payload,
            attempt_id,
            verdict_outcome="green" if reviewed else "missing",
            reviewed_commit=pinned,
            move_reason=(
                f"{'review:green' if reviewed else 'report:done, review skipped'}. The card is parked "
                f"in Assessment: {waits} for a release, rework or reslice decision."
            ),
        )

    def _merge_ready_for_park(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
    ) -> dict[str, Any] | None:
        """Re-read the merge gate before a candidate is parked or released; None when it is green."""
        ref = task["ref"]
        kind, result, detail = self._merge_readiness(task, record)
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
        return self._accept_green_gate(
            task,
            record,
            records,
            payload,
            attempt_id,
            result,
            stage="assessment" if self._parks_for_decision(task) else "release",
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
        reviewed_commit: str = "",
    ) -> dict[str, Any]:
        """The only way a substantive verdict leaves Validate.

        The red transition's order, for the same reason: the intent is on disk, with the reason the
        card is moving, before anything observable moves. Nothing comes after the move — the card waits.
        """
        ref = task["ref"]
        # Re-pinned after the reviewer's pane was forgotten: the merge gate refuses a release for
        # a checkout that moved off the reviewed commit, and the park is exactly that window.
        record.review_commit = reviewed_commit or record.review_commit
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
            return _complete_red_transition(self, task, record, records, payload, attempt_id, ref=ref)
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
        decision, reason, prerequisites = self._recorded_decision(task)
        if not decision:
            return {
                "status": "ok",
                "step": "assessment",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "action": "waiting-observer-decision",
            }
        visit, recorded = assessment_resolution(self.audit.events(ref))
        decision_request = str(recorded.get("request_id") or "") if isinstance(recorded, dict) else ""
        decision_event_id = str(recorded.get("event_id") or "") if isinstance(recorded, dict) else ""
        if visit and decision_request and decision_event_id:
            try:
                self._persist_outcome_round_context(
                    task,
                    record,
                    phase="decision",
                    assessment_visit=visit,
                    request_ids={decision_request},
                    source_event_id=decision_event_id,
                    marker=f"decision:{decision}",
                )
            except (OSError, TaskError, ValueError):
                # The decision has already committed.  Do not turn a journal
                # outage into a new lifecycle authority; terminal projection
                # will retain an explicit missing-decision diagnostic instead.
                pass
        if decision == "rework":
            return self._rework_parked(
                task,
                record,
                records,
                payload,
                attempt_id,
                reason=reason,
                protocol_prerequisites=prerequisites,
            )
        if decision == "reslice":
            return self._reslice_parked(task, record, records, payload, attempt_id, reason=reason)
        return self._release_parked(task, record, records, payload, attempt_id, reason=reason)

    def _recorded_decision(self, task: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
        """The current Assessment decision and its registry-validated prerequisite declaration."""
        events = self.audit.events(task["ref"])
        _visit, event = assessment_resolution(events)
        data = _event_payload(event) if isinstance(event, dict) else {}
        decision = str(data.get("decision") or "")
        body = data.get("body")
        if not isinstance(body, str) or not body.strip():
            body = _last_marker_body(task, f"decision:{decision}") or ""
        if decision not in {"release", "rework", "reslice"} or not isinstance(body, str) or not body.strip():
            return "", "", ()
        # A missing field is the released empty declaration; a present malformed value is never
        # allowed to turn into an authoritative worker instruction.
        declared = data.get("protocol_prerequisites", [])
        if not isinstance(declared, list):
            return "", "", ()
        if decision != "rework":
            return decision, body, ()
        try:
            prerequisites = validate_rework_prerequisites(
                declared,
                specification_revision=specification_revision(events, str(task.get("description") or ""))
                or None,
            )
        except (ValueError, ArtifactOwnershipViolation):
            # An invalid declaration is never a worker instruction. The writer rejects it before
            # commit; this is the recovery fence for a malformed historical audit record.
            return "", "", ()
        return decision, body, tuple(artifact.name for artifact in prerequisites)

    def _rework_parked(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        reason: str,
        protocol_prerequisites: tuple[str, ...],
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
        return _begin_red_transition(self, 
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
            decision_protocol_prerequisites=protocol_prerequisites,
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
        self.terminal_effect(
            task,
            record,
            target="blocked",
            reason=f"Observer decision: reslice. {reason}".strip(),
            decision="reslice",
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "assessment-reslice", ref),
            terminal_state="blocked",
            disposition="reslice",
            verdict=record.worker_continuation.verdict_outcome
            if record.worker_continuation.verdict_outcome in {"green", "red", "blocked"}
            else "missing",
            blocked_reason=None,
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
        self.terminal_effect(
            task,
            record,
            target="blocked",
            reason=reason,
            decision=decision,
            request_id=_attempt_request_id(record.attempt_id or attempt_id, action, ref),
            terminal_state="blocked",
            disposition="blocked",
            verdict=record.worker_continuation.verdict_outcome
            if record.worker_continuation.verdict_outcome in {"green", "red", "blocked"}
            else "missing",
            blocked_reason=_merge_terminal_reason(action),
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
        if not has_candidate(task):
            # Nothing to re-check or merge: the release goes to the completion evidence check. A
            # research card parked by a red verdict reaches here without a transfer, and one parked
            # green has already made it, which this repeats as a no-op.
            refused = self._transfer_research_report(
                task, record, records, payload, attempt_id, step="assessment"
            )
            if refused is not None:
                return refused
            return self._release_effect(
                task,
                record,
                records,
                payload,
                attempt_id,
                step="assessment",
                move_reason=f"Observer decision: release. {reason}".strip(),
                decision="release",
                verdict=_released_verdict(record),
            )
        kind, result, detail = self._merge_readiness(task, record)
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
            verdict=_released_verdict(record),
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
        verdict: str = "green",
    ) -> dict[str, Any]:
        """Merge the reviewed branch, tear the round down and move the card to Done.

        This is the only way a card reaches Done, so the completion evidence check sits here: every
        release, automatic or decided, first taken or replayed after a lost tick, goes through it.
        """
        ref = task["ref"]
        if has_candidate(task):
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
        blocked = self._require_completion_evidence(task, record, records, payload, attempt_id, step=step)
        if blocked is not None:
            return blocked
        try:
            self.host.teardown(record)
        except HostError as exc:
            # Cleanup is a provenance boundary, not best effort. A mismatch keeps the checkout and
            # prevents Done so the next tick cannot repeatedly run an already-failed release path.
            return self._block_merge_path(
                task,
                record,
                records,
                payload,
                attempt_id,
                action="cleanup-provenance-blocked",
                reason=f"release cleanup refused: {scrub_host_output(str(exc))}",
                step=step,
                outcome="release cleanup refused",
                decision=decision,
            )
        self.terminal_effect(
            task,
            record,
            target="done",
            reason=move_reason,
            decision=decision,
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "review-green", ref),
            terminal_state="done",
            disposition="release",
            verdict=verdict,
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {"status": "ok", "step": step, "pilot_ref": ref, "attempt_id": attempt_id, "to": "done"}

    def _require_completion_evidence(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        step: str,
    ) -> dict[str, Any] | None:
        """Completion evidence for kind: the one check between a release and Done.

        A code card's evidence is the merge `complete_green` has just made. A research or infra card
        is read fresh from the board, because its evidence is a marked comment written since the
        tick's snapshot; without it the card is Blocked, naming the missing marker, and not torn down.
        """
        if has_candidate(task):
            return None
        missing = missing_completion_evidence(self.reader.show(task["ref"]))
        if not missing:
            return None
        outcome = self._block_merge_path(
            task,
            record,
            records,
            payload,
            attempt_id,
            action="completion-evidence-missing",
            reason=(
                f"completion evidence missing: this {task.get('type')} card has no `[{missing}]` "
                "record, so it cannot be Done. The workspace is kept; see docs/PROTOCOLS.md, "
                '"Card kinds, live impact and the review choice".'
            ),
            step=step,
            outcome="completion evidence missing",
        )
        outcome["missing_evidence"] = missing
        return outcome

    def _review_drift(self, task: dict[str, Any], record: DispatcherRecord) -> str:
        """Has the checkout moved off the commit the reviewer was pointed at? A verdict describes one code
        state; merging a different one lands work nobody reviewed. Returns the operator message for the
        bounce, or "" when the states match, or when neither can be read — an unreadable workspace is
        the gate's failure to report, not a silent bounce.
        """
        if not record.review_commit:
            return ""
        current = self.host.head_commit(record)
        if not current or current == record.review_commit:
            return ""
        if self.host.is_instance_publish_recovery(task, record, record.review_commit, current):
            return ""
        return (
            f"The review was given for commit `{record.review_commit[:12]}` while the working copy "
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
    ) -> HeadRun:
        """The typed launch snapshot for a head with no launcher record, or a marked minimal one."""
        try:
            return self.catalog.head_run(
                task, role=role, head=head, workspace=workspace, failover=failover
            )
        except (HostError, AttributeError, KeyError, TypeError):
            return HeadRun(role=role, head=str(head), adapter="unknown", model_source=MODEL_UNKNOWN)

    def _journal_round(self, ref: str) -> int:
        """The last worker round the journal holds for this card. Survives a lost dispatcher record,
        a restore, and a card that went back to Ready and was claimed again."""
        history = _routing_attempts(self.audit.events(ref, kind="routing"))
        return history[-1].attempt if history else 0

    def open_worker_round(self, record: DispatcherRecord, *, round_number: int = 0) -> None:
        """Start the card's next worker round: stamp its number and drop the previous round's heads."""
        record.attempt_round = round_number or (record.attempt_round + 1)
        record.outcome_terminal_path = OutcomeTerminalPath.NO_ACCEPTED_REPORT
        record.worker_run = {}
        record.review_run = {}

    def record_worker_routing(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        run: HeadRun | dict[str, Any] | None = None,
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
        snapshot = _routing_head_snapshot_from_launch(snapshot, lifecycle_run=record.worker_head_run)
        if record.worker_run and _run_key(record.worker_run) == _run_key(snapshot):
            snapshot = record.worker_run.snapshot or snapshot
        record.worker_run = snapshot
        self._record_routing(ref, record, phase="worker", heads=[record.worker_run])

    def record_review_routing(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        run: HeadRun | dict[str, Any] | None = None,
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
        snapshot = _routing_head_snapshot_from_launch(snapshot, lifecycle_run=record.review_head_run)
        if record.review_run and _run_key(record.review_run) == _run_key(snapshot):
            snapshot = record.review_run.snapshot or snapshot
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
                    role = str(head.get("role") or "")
                    recorded = by_role.get(role)
                    if recorded is None:
                        continue
                    if role == WORKER:
                        record.worker_run = recorded
                    elif role == REVIEWER:
                        record.review_run = recorded
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

    def _attempt_outcome_obligation(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        *,
        terminal_state: str,
        disposition: str,
        verdict: str = "missing",
        decision: str = "",
        terminal_path: OutcomeTerminalPath,
        taxonomy: TerminalTaxonomy,
    ) -> dict[str, Any] | None:
        """Freeze the forward lineage before its lifecycle effect is issued.

        The finisher and recovery path consume this exact object only.  They
        never reopen a card, walk comments, or search the journal for a newer
        source after the effect has happened.
        """
        reference = str(task.get("ref") or "")
        if not record.attempt_id or record.attempt_round < 1:
            return None
        context = self._outcome_round_context(reference, record)
        worker_context = context.get("worker")
        attempt_id = (worker_context.attempt_id if worker_context is not None else record.attempt_id) or ""
        attempt = worker_context.attempt if worker_context is not None else record.attempt_round
        generation = (
            worker_context.report_generation if worker_context is not None else record.report_generation
        )
        if not attempt_id or attempt < 1 or generation < 1:
            return None
        reviewed = "review" in context or bool(record.review_run)
        report_context = context.get("report")
        revision = (
            report_context.specification_revision
            if report_context is not None
            else worker_context.specification_revision
            if worker_context is not None
            else None
        )
        # Select requiredness before source lookup. The dispatcher persists
        # this typed path when it accepts the report; it never consults the
        # handoff being validated, so a missing handoff remains incomplete.
        report_relevant = (
            terminal_path is OutcomeTerminalPath.FOLLOWS_ACCEPTED_REPORT
            or verdict in {"green", "red", "blocked"}
            or bool(decision)
        )
        verdict_relevant = reviewed and verdict in {"green", "red"}
        decision_relevant = bool(decision)
        source, diagnostic = self._outcome_lineage_sources(
            reference,
            revision=revision,
            context=context,
            report_required=report_relevant,
            verdict_required=verdict_relevant,
            decision_required=decision_relevant,
        )
        completeness: dict[str, str] = {}
        for role, ledger_role in (("worker", "worker"), ("review", "reviewer")):
            usage = self._outcome_usage_source(reference, attempt_id, attempt, generation, ledger_role)
            if usage is None:
                completeness[role] = "missing"
                continue
            source[f"{role}_usage"] = usage.event_id
            completeness[role] = "collected" if usage.data.get("outcome") == "collected" else "degraded"
        # Requiredness is selected from the path before source resolution. A
        # null source is evidence of incomplete lineage, never permission to
        # redefine the path as one that did not consume it. Verdict and
        # decision paths are necessarily report-derived.
        required = {
            "specification_revision": report_relevant,
            "report": report_relevant,
            "verdict": verdict_relevant,
            "decision": decision_relevant,
            "effect": True,
            "worker_usage": completeness["worker"] in {"collected", "degraded"},
            "review_usage": completeness["review"] in {"collected", "degraded"},
        }
        return {
            "version": 2,
            "attempt_id": attempt_id,
            "attempt": attempt,
            "report_generation": generation,
            "sprint_ref": task.get("sprint") or None,
            "specification_revision": revision or None,
            "terminal_state": terminal_state,
            "verdict": verdict,
            "disposition": taxonomy.disposition,
            "blocked_reason": taxonomy.blocked_reason,
            "source_event_ids": source,
            "usage_completeness": completeness,
            "lineage_required": required,
            **({"lineage_diagnostic": diagnostic} if diagnostic else {}),
        }

    def _outcome_lineage_sources(
        self,
        reference: str,
        *,
        revision: str | None,
        context: dict[str, OutcomeRoundContext],
        report_required: bool,
        verdict_required: bool,
        decision_required: bool,
    ) -> tuple[dict[str, str | None], str]:
        """Read only the already-durable exact source handoff.

        Source handlers recorded the event ids before they selected a terminal
        effect.  This method deliberately does not search marker history or
        reconstruct a request id, so it remains valid after dispatcher adoption.
        """
        source: dict[str, str | None] = {
            "report": None,
            "verdict": None,
            "decision": None,
            "effect": None,
            "worker_usage": None,
            "review_usage": None,
        }
        canon = self.writer.board_host.canon
        events = {event.event_id: event for event in canon.events(ref=reference)} if canon is not None else {}

        def one(name: str, phase: str, kind: str, marker: str) -> str:
            if canon is None:
                return f"attempt_outcome_lineage_missing_{name}"
            handoff = context.get(phase)
            if handoff is None or not handoff.source_event_id:
                return f"attempt_outcome_lineage_missing_{name}"
            event = events.get(handoff.source_event_id)
            if event is None:
                return f"attempt_outcome_lineage_dangling_{name}"
            data = event.data
            if event.kind.value != kind or event.ref != reference or data.get("marker") != marker:
                return f"attempt_outcome_lineage_incompatible_{name}"
            if "specification_revision" not in data:
                return f"attempt_outcome_lineage_legacy_{name}"
            if data.get("specification_revision") != revision:
                return f"attempt_outcome_lineage_incompatible_{name}"
            if phase == "decision" and data.get("assessment_visit") != handoff.assessment_visit:
                return f"attempt_outcome_lineage_incompatible_{name}"
            source[name] = event.event_id
            return ""

        report_context = context.get("report")
        verdict_context = context.get("verdict")
        decision_context = context.get("decision")
        diagnostics = [
            one(
                "report", "report", "card.reported",
                report_context.marker if report_context is not None else "",
            )
            if report_required else "",
            one(
                "verdict", "verdict", "card.verdict",
                verdict_context.marker if verdict_context is not None else "",
            )
            if verdict_required else "",
            one(
                "decision", "decision", "card.decided",
                decision_context.marker if decision_context is not None else "",
            )
            if decision_required else "",
        ]
        return source, next((diagnostic for diagnostic in diagnostics if diagnostic), "")

    def _outcome_round_context_request_id(self, record: DispatcherRecord, reference: str, phase: str) -> str:
        return _attempt_request_id(
            record.attempt_id, f"outcome-round-context-{phase}", reference, str(record.report_generation)
        )

    def _persist_outcome_round_context(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        *,
        phase: str,
        assessment_visit: str = "",
        request_ids: set[str] | None = None,
        source_event_id: str = "",
        marker: str = "",
        source_revision: str | None = None,
        freeze_source_revision: bool = False,
    ) -> None:
        """Write a stable forward round handoff at a launch or source boundary.

        This method is intentionally never called from ``terminal_effect``.
        The worker launch creates the stable identity; review and source records
        carry that identity forward without consulting mutable dispatcher state.
        """
        reference = str(task.get("ref") or "")
        if not reference or not record.attempt_id or record.attempt_round < 1 or record.report_generation < 1:
            return
        existing_context = self._outcome_round_context(reference, record)
        worker = existing_context.get("worker")
        round_id = worker.round_id if worker is not None else ""
        if phase == "worker":
            context_request = self._outcome_round_context_request_id(record, reference, phase)
            round_id = context_request
        elif not round_id:
            # A pre-v2 or unavailable launch handoff cannot be repaired from a
            # terminal path.  Its terminal outcome is honestly incomplete.
            return
        else:
            context_request = _attempt_request_id(round_id, f"outcome-round-context-{phase}", reference)
        if self.audit.committed_event(context_request) is not None:
            return
        if request_ids is None:
            if phase == "worker":
                request_ids = _round_report_ids(
                    record.workspace, record.attempt_id, reference, record.report_generation
                )
            elif phase == "review":
                request_ids = {
                    _attempt_request_id(
                        record.attempt_id, f"review-{kind}", reference, str(record.review_baseline)
                    )
                    for kind in ("green", "red")
                }
            else:
                return
        revision = source_revision if phase != "worker" else None
        if phase != "worker" and not freeze_source_revision and source_revision is None:
            revision = worker.specification_revision if worker is not None else None
        if phase == "worker":
            revision = (
                specification_revision(self.audit.events(reference), str(task.get("description") or ""))
                or None
            )
        try:
            context = OutcomeRoundContext(
                version=2,
                phase=OutcomeRoundPhase(phase),
                round_id=round_id,
                attempt_id=(
                    worker.attempt_id
                    if phase != "worker" and worker is not None
                    else record.attempt_id
                ),
                attempt=(
                    worker.attempt
                    if phase != "worker" and worker is not None
                    else record.attempt_round
                ),
                report_generation=(
                    worker.report_generation
                    if phase != "worker" and worker is not None
                    else record.report_generation
                ),
                request_ids=tuple(sorted(request_ids)),
                assessment_visit=assessment_visit,
                source_event_id=source_event_id,
                specification_revision=revision,
                marker=marker,
            )
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        self.writer.outcome_round_context(
            role="dispatcher", actor=self.owner, reference=reference,
            request_id=context_request, data=context,
        )

    def _capture_outcome_source(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        *,
        phase: str,
        kind: str,
        marker: str,
        assessment_visit: str = "",
    ) -> None:
        """Attach the exact marker id as soon as this dispatcher consumes it.

        A journal outage here must not veto the later lifecycle effect.  In
        that case there is no invented identity: the effect carries the named
        missing-source diagnostic and projection marks the required lineage
        incomplete.
        """
        reference = str(task.get("ref") or "")
        context = self._outcome_round_context(reference, record)
        owner = context.get("worker" if phase == "report" else "review")
        if owner is None:
            return
        request_ids = owner.request_ids
        canon = self.writer.board_host.canon
        if canon is None:
            return
        matches = [
            event
            for request_id in request_ids
            if (event := canon.committed(str(request_id))) is not None
            and event.kind.value == kind
            and event.ref == reference
            and event.data.get("marker") == marker
        ]
        if len(matches) != 1:
            return
        try:
            self._persist_outcome_round_context(
                task,
                record,
                phase=phase,
                assessment_visit=assessment_visit,
                request_ids={str(matches[0].event_id)},
                source_event_id=matches[0].event_id,
                marker=marker,
                source_revision=matches[0].data.get("specification_revision"),
                freeze_source_revision=True,
            )
        except (OSError, TaskError, ValueError):
            return

    def _outcome_round_context(
        self, reference: str, record: DispatcherRecord
    ) -> dict[str, OutcomeRoundContext]:
        """Find one unsettled durable handoff without re-estimating its identity.

        The fast path keeps ordinary dispatch cheap. Adoption can lose the
        process-local attempt id and report generation, so its fallback uses
        only durable handoffs and excludes rounds already sealed by a lifecycle
        effect. It never uses card comments, workspace text, event order or
        request-id grammar to choose a source.
        """
        payloads: list[OutcomeRoundContext] = []
        for event in self.audit.events(reference, kind="outcome_round_context"):
            payload = (
                event.get("data")
                if event.get("record_type") == "board.protocol_event"
                else event.get("payload")
            )
            if not isinstance(payload, dict) or payload.get("version") != 2:
                continue
            try:
                payloads.append(OutcomeRoundContext.from_data(payload))
            except ValueError:
                continue
        workers = [payload for payload in payloads if payload.phase is OutcomeRoundPhase.WORKER]
        exact = [
            payload for payload in workers
            if payload.attempt_id == record.attempt_id
            and payload.attempt == record.attempt_round
            and payload.report_generation == record.report_generation
        ]
        if len(exact) == 1:
            worker = exact[0]
        else:
            sealed = {
                (data.get("attempt_id"), data.get("attempt"), data.get("report_generation"))
                for event in self.writer.board_host.canon.events(ref=reference)
                if isinstance((data := event.data.get("attempt_outcome_owed")), dict)
            }
            unsettled = [
                payload for payload in workers
                if (payload.attempt_id, payload.attempt, payload.report_generation) not in sealed
            ]
            if len(unsettled) != 1:
                return {}
            worker = unsettled[0]
        context = {"worker": worker}
        for payload in payloads:
            if payload.phase is not OutcomeRoundPhase.WORKER and payload.round_id == worker.round_id:
                context[payload.phase.value] = payload
        return context

    def _outcome_usage_source(
        self, reference: str, attempt_id: str, attempt: int, generation: int, role: str
    ) -> Any | None:
        canon = self.writer.board_host.canon
        if canon is None:
            return None
        matches = [
            occurrence.event
            for occurrence in canon.attempt_usage_occurrences(ref=reference)
            if occurrence.event.data.get("attempt_id") == attempt_id
            and occurrence.event.data.get("attempt") == attempt
            and occurrence.event.data.get("report_generation") == generation
            and occurrence.event.data.get("role") == role
        ]
        return matches[0] if len(matches) == 1 else None

    def _finish_attempt_outcome(self, obligation: dict[str, Any], effect_event_id: str) -> dict[str, Any]:
        """Stage/append one owed row, reporting degradation without lifecycle work."""
        try:
            canon = self.writer.board_host.canon
            if canon is None:
                raise RuntimeError("board event canon is unavailable")
            reference = str(obligation.get("card_ref") or "")
            # Transition obligations inherit their Card subject; immediate
            # callers add it below so recovery never consults live card state.
            if not reference:
                raise ValueError("attempt outcome obligation has no card ref")
            source = dict(obligation["source_event_ids"])
            source["effect"] = effect_event_id
            required = obligation.get("lineage_required")
            if not isinstance(required, dict):
                raise ValueError(  # noqa: TRY004 - corrupt persisted state, not caller type input.
                    "attempt_outcome_lineage_requiredness_missing"
                )
            data = {
                **{
                    key: value
                    for key, value in obligation.items()
                    if key not in {"card_ref", "lineage_diagnostic", "source_event_ids"}
                },
                "source_event_ids": source,
            }
            return self.writer.attempt_outcome(
                role="dispatcher",
                actor=self.owner,
                reference=reference,
                data=data,
                reason="confirmed terminal lifecycle effect",
                request_id=_attempt_request_id(
                    str(obligation["attempt_id"]),
                    "attempt-outcome",
                    reference,
                    str(obligation["report_generation"]),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - analytics never gates a lifecycle effect
            return {
                "status": "degraded",
                "step": "attempt-outcome",
                "action": "attempt-outcome-owed",
                "reason": f"outcome remains owed: {type(exc).__name__}: {exc}",
            }

    def terminal_effect(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        *,
        target: str,
        reason: str,
        request_id: str,
        terminal_state: str,
        disposition: str,
        verdict: str = "missing",
        blocked_reason: str | None = None,
        decision: str = "",
    ) -> dict[str, Any]:
        """The lifecycle-owned terminal effect and its non-blocking finisher."""
        taxonomy: TerminalTaxonomy | None = None
        try:
            taxonomy = normalize_terminal_taxonomy(disposition=disposition, blocked_reason=blocked_reason)
        except TerminalTaxonomyValidationError:
            # The effect is authoritative. A malformed observational value is
            # rejected at its typed boundary but cannot delay or retract it.
            pass
        obligation = (
            self._attempt_outcome_obligation(
                task,
                record,
                terminal_state=terminal_state,
                disposition=disposition,
                verdict=verdict,
                decision=decision,
                terminal_path=record.outcome_terminal_path,
                taxonomy=taxonomy,
            )
            if taxonomy is not None
            else None
        )
        if obligation is not None:
            obligation = {"card_ref": task["ref"], **obligation}
        effect = self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=task["ref"],
            target=target,
            reason=reason,
            decision=decision,
            request_id=request_id,
            outcome_owed=obligation,
            terminal_taxonomy=taxonomy.to_record() if taxonomy is not None else None,
        )
        effect_obligation = effect.get("outcome_owed") if isinstance(effect, dict) else None
        if isinstance(effect_obligation, dict):
            self._finish_attempt_outcome(effect_obligation, str(effect["event_id"]))
        elif obligation is not None:
            self._finish_attempt_outcome(obligation, str(effect["event_id"]))
        return effect

    def publish_pending_attempt_outcomes(self) -> list[dict[str, Any]]:
        """Recover committed terminal-effect obligations, then exact staged rows.

        This is journal-only and fail-open: malformed analytics history is a
        diagnostic, never a reason to abort the production tick.
        """
        try:
            canon = self.writer.board_host.canon
            if canon is None:
                return []
            outcomes: list[dict[str, Any]] = []
            for effect in canon.attempt_outcome_effects():
                obligation = effect.data.get("attempt_outcome_owed")
                if not isinstance(obligation, dict):
                    continue
                outcome = self._finish_attempt_outcome(obligation, effect.event_id)
                if outcome.get("status") == "degraded":
                    outcomes.append(outcome)
            self.writer.finish_attempt_outcomes(role="dispatcher")
            return outcomes
        except Exception as exc:  # noqa: BLE001 - no analytics reader may gate a tick
            return [
                {
                    "status": "degraded",
                    "step": "attempt-outcome-recovery",
                    "action": "attempt-outcome-pending-unreadable",
                    "reason": f"outcome recovery remains owed: {type(exc).__name__}: {exc}",
                }
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
            run = _routing_head_snapshot_from_launch(snapshot, lifecycle_run=lifecycle)
        except ValueError:
            # An incomplete launch attestation is not a reason to drop the occurrence: the routing
            # snapshot still reports its adapter, model and whatever session identity it already held.
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
        report_protocol_prerequisites = _task_doc_protocol_prerequisites(workspace)
        # The lost state file took the round's terminal path with it. The card's own lifecycle
        # state is the same dispatcher-owned fact and outlives that file: a card cannot stand in
        # Validate or Assessment without an accepted report. The reconstructed record state is
        # read the same way, and neither reads back a handoff or a marker.
        adopted_path = _outcome_terminal_path(None, state=str(task.get("state") or ""))
        if adopted_path is OutcomeTerminalPath.NO_ACCEPTED_REPORT:
            adopted_path = _outcome_terminal_path(None, state=state)
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
            report_protocol_prerequisites=report_protocol_prerequisites,
            state=state,
            claimed_at=time.time(),
            # A reviewer launches only over a green gate, so a card in review inherits a passed gate.
            gate_state="green" if launched else "",
            attempt_round=round_record.attempt if round_record else 0,
            worker_run=round_record.worker.to_json() if round_record and round_record.worker else {},
            review_run=round_record.reviewer.to_json() if round_record and round_record.reviewer else {},
            outcome_terminal_path=adopted_path,
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


def _review_launch_request_id(reference: str, review_baseline: int) -> str:
    return _attempt_request_id("review", "start-intent", reference, str(review_baseline))


def _released_verdict(record: DispatcherRecord) -> str:
    """The verdict a decided release carries: the parked one, or `missing` when no reviewer ran."""
    return "missing" if record.worker_continuation.verdict_outcome == "missing" else "green"


def _merge_terminal_reason(action: str) -> str:
    """Classify the terminal cause a merge path actually reached.

    A failed release/merge and a gate that cannot supply a usable result still
    charge as their own terminal work.  Only a classified head bring-up is the
    distinct uncharged infrastructure family.
    """
    if action == "red-review-ceiling":
        return "review"
    if "gate" in action:
        return "gate"
    return "implementation"

