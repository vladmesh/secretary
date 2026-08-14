"""Production dispatcher runtime."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import yaml

from secretary._fsutil import write_text_atomic
from secretary.checkpoint import CheckpointPusher, CheckpointWriter
from secretary.config import validate_instance
from secretary import state_repo
from secretary.dispatcher_launcher import (
    HeadLaunchError,
    claude_launch_model as _claude_launch_model,
    ensure_claude_workspace_ready as _ensure_claude_workspace_ready,
    ensure_codex_workspace_trusted as _ensure_codex_workspace_trusted,
    role_launch_env as _role_launch_env,
)
from secretary.dispatcher_helpers import (
    RED_REVIEW_CEILING,
    _decision_record_line,
    _gate_red_repeat_count,
    _last_gate_red_body,
    _last_marker,
    _round_record_line,
    _round_report_ids,
    _round_report_marker,
    _last_marker_body,
    _last_review_red_body,
    _legacy_worker_branch,
    _report_adoption_baseline,
    _review_adoption_baseline,
    _spent_report_generations,
    _task_doc_decision,
    _task_doc_report_generation,
    _tail,
    _worker_id,
    red_review_count as _red_review_count,
    safe_one_line as _safe_one_line,
    scrub_host_output,
)
from secretary.dispatcher_gate import (
    GATE_PENDING_STALL_SECONDS,
    GATE_TRANSPORT_MAX_ATTEMPTS,
    GateResult,
    _fingerprint as _gate_fingerprint,
    gate_check as _gate_check,
    validation_ci as _validation_ci,
)
from secretary.dispatcher_gate_receipt import (
    AcceptedGreenGate,
    accepted_receipt as _accepted_gate_receipt,
    is_exact_sha as _is_exact_sha,
    render_receipt,
)
from secretary.dispatcher_heartbeat import heartbeat_identity
from secretary.dispatcher_observer import (
    OBSERVER_HEAD_FALLBACK,
    OBSERVER_PROMPT_FILE,
    OBSERVER_ROLE,
    ObserverLaunchAborted,
    delivery_evidence_summary as _observer_delivery_evidence_summary,
    observer_launch_prompt as _observer_launch_prompt,
    observer_pid_file as _observer_pid_file,
)
from secretary.observer_root import OBSERVER_REPO_NAME, observer_root_repo
from secretary.dispatcher_launch import (
    REVIEW_ROLE,
    WORKER_ROLE,
    bring_up_blocked_reason as _bring_up_blocked_reason,
    clear_launch_intent as _clear_launch_intent,
    confirm_launch_intent as _confirm_launch_intent,
    forget_role_head as _forget_role_head,
    head_stop_unconfirmed as _head_stop_unconfirmed,
    keep_reserved_round as _keep_reserved_round,
    launch_aborted as _launch_aborted,
    launch_deferred as _launch_deferred,
    launch_intent as _launch_intent,
    launch_intent_unwritable as _launch_intent_unwritable,
    launch_left_a_head as _launch_left_a_head,
    launch_pid_file as _launch_pid_file,
    mark_launch_aborted as _mark_launch_aborted,
    merge_launch_head_run as _merge_launch_head_run,
    pane_state_label as _pane_state_label,
    reset_launch_attempts as _reset_launch_attempts,
    resolve_launch_intent as _resolve_launch_intent,
    write_launch_intent as _write_launch_intent,
)
from secretary.dispatcher_pause import ProductionPause
from secretary.dispatcher_pause_ops import (
    pause as _pause_pipeline,
    pause_status as _pause_status,
    resume as _resume_pipeline,
)
from secretary.dispatcher_production import (
    ProductionState,
    production_observe as _production_observe,
    production_probe as _production_probe,
    production_run as _production_run,
    production_tick as _production_tick,
)
from secretary.dispatcher_review import (
    command_terminal_status as _command_terminal_status,
    end_review_pane as _end_review_pane,
    recover_review_launch as _recover_review_launch,
    start_review as _start_review,
)
from secretary.dispatcher_watchdog import (
    bind_head_heartbeat as _bind_head_heartbeat,
    clear_head_heartbeat as _clear_head_heartbeat,
    guard_head_run_identity as _guard_head_run_identity,
    head_run_process_status as _head_run_process_status,
    HeadRunIdentityMismatch as _HeadRunIdentityMismatch,
    heartbeat_is_dead as _heartbeat_is_dead,
    heartbeat_is_live_match as _heartbeat_is_live_match,
    heartbeat_is_mismatch as _heartbeat_is_mismatch,
    head_process_status as _head_process_status,
    idle_stall_seconds as _idle_stall_seconds,
    idle_outcome as _idle_outcome,
    initial_output_stall_seconds as _initial_output_stall_seconds,
    pid_file_path as _pid_file_path,
    reset_wait as _reset_wait,
    reset_idle as _reset_idle,
    stall_seconds as _stall_seconds,
    wait_cycle_token as _wait_cycle_token,
    wait_outcome as _wait_outcome,
)
from secretary.dispatcher_state import (
    CLAIM_SKIP_FAILOVER_COLLAPSE,
    CLAIM_SKIP_RESOURCE_NOT_READY,
    DispatcherRecord,
    attempt_request_id as _attempt_request_id,
    claim_actual as _claim_actual,
    claim_mismatch as _claim_mismatch,
    new_attempt_id as _new_attempt_id,
    now_rfc3339,
    record_attempt as _record_attempt,
    record_divergence as _record_divergence,
    request_token as _request_token,
)
from secretary.dispatcher_tui import (
    COMPOSER_EMPTY,
    COMPOSER_UNKNOWN,
    DELIVERY_ACCEPTED,
    READINESS_BUSY,
    READINESS_READY,
    READINESS_UNKNOWN,
    TuiDeliveryError,
    close_terminal_strict as _close_tui_terminal_strict,
    deliver_interactive_prompt as _deliver_interactive_prompt,
    delivery_readiness_state as _delivery_readiness_state,
    terminal_readiness as _terminal_readiness,
    terminal_turn_started as _terminal_turn_started,
    prepare_claude_provider_progress_source as _prepare_claude_provider_progress_source,
    bind_claude_provider_progress_source as _bind_claude_provider_progress_source,
    provider_progress_for_run as _provider_progress_for_run,
)
from secretary.dispatcher_tui import deliver_tui_prompt as _deliver_tui_prompt
from secretary.dispatcher_worker_lifecycle import (
    BUSY_RETRY_INITIAL_SECONDS,
    CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS,
    ContinuationLivenessState,
    ContinuationProviderCondition,
    ContinuationRecoveryRung,
    WorkerContinuationLiveness,
)
from secretary.codex_provider_events import (
    CodexProviderEventIngress,
    CodexProviderSourceError,
)
from secretary.dispatcher_types import (
    # Who a stop is recorded as having been initiated by. Every stop the dispatcher performs has an
    # agent — the reviewer taking the checkout, a verdict ending the review round, a watchdog over
    # a head that stopped answering, a replacement opening, an operator pausing the pipeline,
    # reconciliation settling a record — and both heads' runs now say which.
    STOPPED_BY_DISPATCHER,
    STOPPED_BY_OPERATOR,
    STOPPED_BY_RECONCILIATION,
    STOPPED_BY_REPLACEMENT,
    STOPPED_BY_REVIEW_FREEZE,
    STOPPED_BY_REVIEW_VERDICT,
    STOPPED_BY_WATCHDOG,
    DispatcherError,
    GateTransportError,
    HeadLaunchAborted,
    HeadPaneNotReady,
    HostError,
    ReviewLaunch,
    review_pane_label,
)
from secretary.head_registry import HeadRegistryConfigError, installed_heads
from secretary.routing_journal import (
    HEAD_FROM_CARD,
    HEAD_FROM_FALLBACK,
    HEAD_FROM_RECORD,
    HEAD_FROM_ROLE_DEFAULT,
    MODEL_UNKNOWN,
    HeadRun,
    attempts as _routing_attempts,
    head_run_from_profile,
    routing_payload as _routing_payload,
    run_key as _run_key,
)
from secretary.head_health import HeadChoice, HeadHealth, HeadReadiness, resolve_head_chain
from secretary.sprints import SprintReader, budget_thresholds
from secretary.tasks import (
    KanboardClient,
    TaskAudit,
    TaskError,
    TaskReader,
    TaskWriter,
    durability_dirt,
    standing_decision,
)
from triggered_agents.agents.pipeline.heads import (
    HeadRegistryError,
    resolve_head_id as _resolve_head_id,
)
from triggered_agents.runtime.launch_prefix import pythonpath_prefix
from triggered_agents.runtime import head as head_ops
from triggered_agents.runtime.codex_preflight import (
    CodexFanoutPolicyError,
    CodexFanoutRecordingError,
    preflight_codex_launch,
)
from triggered_agents.runtime.head import (
    CODEX_TUI_MODE,
    PYTHON_SAFE_PATH_FLAG as _PYTHON_SAFE_PATH_FLAG,
    HeadCommand,
    HeadCommandError,
    HeadSpec,
    HeadSpecError,
    render_head_command as _render_head_command,
    with_pid_heartbeat as _with_pid_heartbeat,
)
from triggered_agents.runtime.pane_host import (
    OrcaSessionHost,
    Pane,
    PaneHostError,
    SessionHost,
    safe_command_label as _safe_command_label,
)
from triggered_agents.runtime.prompt_document import (
    PromptDocumentError,
    nudge_for as _nudge_for,
    write_prompt_document as _write_prompt_document,
)

# The prompts below are read and run by a head in its own shell, so the checkout fallback stays a
# shell expression rather than a path this process resolved.
_PYTHONPATH_PREFIX = pythonpath_prefix()
# A TASK.md runs from the candidate worktree, while task protocol mutations belong to the live
# dispatcher installation selected above.  ``-P`` keeps Python from prepending that worktree to
# sys.path and shadowing the explicitly selected control plane package.
_CONTROL_PLANE_TASK_COMMAND = f"{_PYTHONPATH_PREFIX} python3 {_PYTHON_SAFE_PATH_FLAG} -m secretary task"


def default_data_dir(instance_path: Path) -> Path:
    report = validate_instance(_instance_file(instance_path))
    if not report.ok:
        raise DispatcherError(
            "invalid_instance",
            "invalid instance: " + "; ".join(map(str, report.errors)),
            2,
        )
    data_dir = report.instance.get("data_dir")
    if not isinstance(data_dir, str):
        raise DispatcherError("invalid_instance", "instance data_dir is unavailable", 2)
    return Path(data_dir)


def _instance_file(path: Path) -> Path:
    return path / "instance.yaml" if path.is_dir() else path


# Observer workspaces live in this subdirectory of the workspaces root. Orca puts a worktree at
# <workspaces root>/<repo directory>/<worktree name>, so the observer repo's directory carries the
# same name and the path the dispatcher fixed in the launch intent is the path Orca hands back.
OBSERVER_WORKSPACE_DIR = OBSERVER_REPO_NAME
# The only branch of the observer repo, named at init so no bring-up has to ask git what a fresh
# repository calls its first branch.
OBSERVER_REPO_BRANCH = "observers"

# How long a confirmed stop waits for a head to leave after each signal, and how often it looks.
# A head that has been asked to close its pane exits within a moment; the wait exists so a stop is
# not called unconfirmed over a process that is in the middle of leaving.
HEAD_STOP_GRACE_SECONDS = 5.0
HEAD_STOP_POLL_SECONDS = 0.1


def _same_repo(first: Path, second: Path) -> bool:
    try:
        return first.expanduser().resolve() == second.expanduser().resolve()
    except OSError:
        return first.expanduser().absolute() == second.expanduser().absolute()


@dataclass(frozen=True)
class LaunchedHead:
    """One head bring-up as it happened: the pane it runs in and the configuration it runs with."""

    handle: str
    head: str = ""
    run: dict[str, Any] = field(default_factory=dict)
    # `terminal create` / `split` returns paneKey synchronously.  Its leaf survives Orca's handle
    # aliasing, so launch callers carry it instead of trying to recover it from an inventory keyed
    # by the already-unstable handle.
    leaf: str = ""
    # The completed launch prompt's bounded transport receipt.  This belongs beside the head
    # identity so caller recovery never has to infer a successful body/submit pair from the pane.
    delivery_evidence: dict[str, Any] = field(default_factory=dict)
    # The head's own run, as the three head operations keep it: identity that outlives a pane
    # handle, lifecycle, and later the initiator that ended it. Empty for a bring-up whose caller
    # keeps no lifecycle of its own (the in-process host seams, and noop mode).
    head_run: dict[str, Any] = field(default_factory=dict)


# The identity a session manager returns while creating one pane. It is `pane_host.Pane` now: the
# pane verbs moved to the host protocol, and a second local copy of "a handle and a leaf" would be
# a type the head operations and the dispatcher had to translate between for no reason.
PaneIdentity = Pane


class InstanceCatalog:
    def __init__(self, instance_path: Path) -> None:
        report = validate_instance(instance_path)
        if not report.ok:
            raise DispatcherError("invalid_instance", "instance config is invalid", 2)
        self.instance_path = report.instance_path
        self.instance_dir = report.instance_path.parent
        self.instance = report.instance
        self.bindings = {
            str(binding.get("id")): binding
            for binding in report.bindings
            if isinstance(binding, dict) and binding.get("enabled") is True
        }
        try:
            # The installation's own snapshot, not the checkout this module was imported from.
            # The dispatcher runs out of a working tree where development happens; comparing the
            # live registry against that tree made an unmerged commit stop production ticks.
            self._heads = installed_heads(self.instance_path)
        except HeadRegistryConfigError as exc:
            raise DispatcherError("invalid_heads", str(exc), 2) from None

    def binding(self, project: str) -> dict[str, Any]:
        binding = self.bindings.get(project) or self.bindings.get(project.replace("_", "-"))
        if not binding:
            raise HostError(f"project {project!r} is not enabled in the instance")
        repo = binding.get("repo")
        if not isinstance(repo, str) or not repo:
            raise HostError(f"project {project!r} has no repo path")
        return binding

    def adapter(self, project: str) -> dict[str, Any]:
        binding = self.binding(project)
        adapter = binding.get("adapter")
        if not isinstance(adapter, str) or not adapter:
            raise HostError(f"project {project!r} has no adapter")
        path = self.instance_dir / "adapters" / f"{adapter}.yaml"
        loaded = self._load_optional_yaml(path)
        if not loaded:
            raise HostError(f"adapter {adapter!r} is unavailable")
        return loaded

    def default_branch(self, project: str, override: str | None) -> str:
        if override:
            return override
        branch = self.binding(project).get("default_branch")
        return str(branch or "main")

    def worker_head(self, task: dict[str, Any]) -> str:
        requested = task.get("routing", {}).get("head_override")
        head = self._resolved_head(str(requested) if requested else str(
            self._heads.get("role_defaults", {}).get("new_card") or "codex"
        ))
        self._head_profile(head)
        return head

    def review_head(self, task: dict[str, Any]) -> str:
        requested = task.get("routing", {}).get("review_head_override")
        head = self._resolved_head(str(requested) if requested else str(
            self._heads.get("role_defaults", {}).get("reviewer") or "codex-reviewer"
        ))
        self._head_profile(head)
        return head

    def _resolved_head(self, head: str) -> str:
        """The profile in this snapshot that serves a head id somebody else wrote down.

        A declared old Codex id resolves to the equivalent interactive Codex profile; a Codex id with
        no interactive Codex profile at all is refused rather than resolved, because launching what
        that name points at now would move a claimed attempt onto another model family.
        """
        profiles = self._heads.get("profiles")
        try:
            return _resolve_head_id(head, profiles if isinstance(profiles, dict) else {})
        except HeadRegistryError as exc:
            raise HostError(f"head {head!r} is unavailable: {exc}") from None

    def head_fallback(self, head: str) -> list[str]:
        """The ordered fallback chain `head` names in the registry, empty when it names none."""
        profile = self._head_profile(head)
        chain = profile.get("fallback")
        if not isinstance(chain, list):
            return []
        return [str(entry) for entry in chain if isinstance(entry, str) and entry]

    def claimed_worker_head(self, task: dict[str, Any]) -> str:
        return self._claimed_head(task, "resolved_worker_head", self.worker_head)

    def claimed_review_head(self, task: dict[str, Any]) -> str:
        return self._claimed_head(task, "resolved_review_head", self.review_head)

    def _claimed_head(
        self,
        task: dict[str, Any],
        key: str,
        current: Callable[[dict[str, Any]], str],
    ) -> str:
        """The head the card was claimed with, for a card the dispatcher is picking back up.

        The head is decided once, at claim. Re-reading the override or the role default here would
        hand the rest of a running attempt to whatever the board says now, so a head that has since
        left `heads.yaml` stops the attempt instead of substituting today's default.
        """
        claimed = (task.get("routing") or {}).get(key)
        if not claimed:
            return current(task)
        head = self._resolved_head(str(claimed))
        try:
            self._head_profile(head)
        except HostError as exc:
            raise HostError(f"head {head!r} recorded at claim is unavailable: {exc}") from None
        return head

    def head_run(
        self, task: dict[str, Any], *, role: str, head: str = "", workspace: str = "",
        failover: bool = False,
    ) -> HeadRun:
        """The launch record for one head of `role`: the profile id plus the configuration it is
        launched with, read from the same snapshot the launcher renders its command from.
        """
        routing = task.get("routing") or {}
        if role == "worker":
            override = routing.get("head_override")
            asked = self._resolved_head(str(override) if override else str(
                self._heads.get("role_defaults", {}).get("new_card") or "codex"
            ))
        else:
            override = routing.get("review_head_override")
            asked = self._resolved_head(str(override) if override else str(
                self._heads.get("role_defaults", {}).get("reviewer") or "codex-reviewer"
            ))
        launched = str(head) if head else asked
        if launched != asked:
            head_source = HEAD_FROM_FALLBACK if failover else HEAD_FROM_RECORD
        else:
            head_source = HEAD_FROM_CARD if override else HEAD_FROM_ROLE_DEFAULT
        profile = self._head_profile(launched)
        resources = self._heads.get("resources")
        model: str | None = None
        model_source = ""
        if str(profile.get("adapter") or "") == "claude":
            # A claude profile need not pin a model (`claude-default` does not), and then the CLI
            # resolves one from its settings at startup. Read it here, at bring-up, so the record
            # names the model that ran instead of an empty field.
            model, model_source = _claude_launch_model(
                profile, workspace=workspace, env=_role_launch_env(role)
            )
        return head_run_from_profile(
            role=role,
            head=launched,
            head_source=head_source,
            profile=profile,
            resources=resources if isinstance(resources, dict) else {},
            model=model,
            model_source=model_source,
        )

    def observer_head(self) -> str:
        """The head profile a sprint observer is launched with."""
        head = str(self._heads.get("role_defaults", {}).get("observer") or OBSERVER_HEAD_FALLBACK)
        self._head_profile(head)
        return head

    def observer_profile(self, head: str) -> dict[str, Any]:
        """The registry entry for a head a sprint declares, or `HostError`."""
        return self._head_profile(head)

    def observer_run(self, head: str, *, workspace: str = "") -> HeadRun:
        """The launch record for an observer head, read from the same snapshot as its command."""
        profile = self._head_profile(head)
        resources = self._heads.get("resources")
        model: str | None = None
        model_source = ""
        if str(profile.get("adapter") or "") == "claude":
            model, model_source = _claude_launch_model(
                profile, workspace=workspace, env=_role_launch_env(OBSERVER_ROLE)
            )
        return head_run_from_profile(
            role=OBSERVER_ROLE,
            head=head,
            head_source=HEAD_FROM_ROLE_DEFAULT,
            profile=profile,
            resources=resources if isinstance(resources, dict) else {},
            model=model,
            model_source=model_source,
        )

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
    ) -> HeadCommand:
        """The command this workspace's pane will run, with its workspace made fit to run it in."""
        profile = self._head_profile(head)
        try:
            self.prepare_head_workspace(head, workspace, role=role)
            return _render_head_command(
                profile if isinstance(profile, dict) else {},
                prompt=None,
                workspace=workspace,
                role=role,
                identity=identity,
            )
        except (HeadLaunchError, HeadCommandError) as exc:
            raise HostError(str(exc)) from None

    def prepare_head_workspace(self, head: str, workspace: str, *, role: str = "") -> None:
        """Pre-answer the first-run questions a head's CLI would otherwise put to an operator.

        Every codex head runs as a TUI and is asked about directory trust before it will take a prompt,
        whatever its role; a head launched into an untrusted root sits on the dialog, never answers
        Orca's readiness probe and never receives its prompt. This runs before the pane is created:
        trust, then pane, then readiness, then delivery.
        """
        profile = self._head_profile(head)
        adapter = profile.get("adapter") if isinstance(profile, dict) else ""
        try:
            if adapter == "claude":
                _ensure_claude_workspace_ready(workspace)
            elif adapter == "codex":
                _ensure_codex_workspace_trusted(profile, workspace)
        except HeadLaunchError as exc:
            raise HostError(str(exc)) from None

    def _head_profile(self, head: str) -> dict[str, Any]:
        profiles = self._heads.get("profiles", {})
        profile = profiles.get(head) if isinstance(profiles, dict) else None
        if not isinstance(profile, dict):
            known = ", ".join(sorted(profiles)) if isinstance(profiles, dict) else ""
            raise HostError(f"unknown head {head!r} (known: {known or '(none)'})")
        return profile

    def head_profile(self, head: str) -> dict[str, Any]:
        return self._head_profile(head)

    def resource(self, resource: str) -> dict[str, Any]:
        resources = self._heads.get("resources", {})
        value = resources.get(resource) if isinstance(resources, dict) else None
        if not isinstance(value, dict):
            raise HostError(f"unknown head resource {resource!r}")
        return value

    @staticmethod
    def _load_optional_yaml(path: Path) -> dict[str, Any]:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return {}
        return loaded if isinstance(loaded, dict) else {}


@dataclass(frozen=True)
class DispatcherHeadTransport:
    """How this dispatcher delivers to a head and closes its pane, against whatever host it is on.
    """

    runtime: "CommandHostRuntime"
    workspace: str = ""
    prompt_file: str = ""
    adapter: str = ""
    role: str = ""
    before_send: Callable[[], head_ops.HeadRun | None] | None = None

    def deliver(
        self,
        run: head_ops.HeadRun,
        pointer: head_ops.NudgePointer,
        *,
        host: SessionHost,
        subject: str,
    ) -> head_ops.HeadDelivery:
        # The framing is the *rendered command's* fact, not the profile's: what is running in that
        # pane is what the launcher just started, and a registry edited since would frame a prompt
        # for a head this launch never ran. The run offers its own view of the adapter and this
        # deliberately keeps the launcher's when there is one.
        post_delivery = run

        def handoff_before_send() -> None:
            nonlocal post_delivery
            if self.before_send is None:
                return
            updated = self.before_send()
            if updated is not None:
                post_delivery = head_ops.post_delivery_run(run, updated)

        try:
            outcome = _deliver_tui_prompt(
                run.handle,
                self.workspace,
                self.prompt_file,
                host=host,
                adapter=self.adapter or run.spec.adapter,
                prompt_text=pointer.text or None,
                subject=subject,
                document_path=pointer.document,
                before_send=handoff_before_send if self.before_send is not None else None,
            )
        except Exception as exc:
            # A source may already have been durably bound when a later delivery stage refuses.
            # The abort path receives this exact run so its intent cannot write the old unbound
            # copy back over the source recovery has to read.
            setattr(exc, "head_run", post_delivery)
            raise
        return head_ops.HeadDelivery(run=post_delivery, outcome=outcome)

    def close(self, run: head_ops.HeadRun, *, host: SessionHost) -> None:
        self.runtime._close_head_pane(
            run.handle,
            run.pid_file,
            run=run,
            role=self.role,
            host=host,
        )


class CommandHostRuntime:
    def __init__(self, catalog: InstanceCatalog, data_dir: Path, *, mode: str = "real") -> None:
        self.catalog = catalog
        self.data_dir = data_dir
        self.mode = mode
        # Where a head run is flushed the moment an operation commits it, ahead of the tick's own
        # save (secretary-1412). Installed by the owner of the durable state for the span in which
        # it has that state loaded — `CommandHostRuntime` has a record in hand but not the file it
        # belongs to. Unset, a run reaches disk with the tick's records, which is all a caller
        # outside a tick can promise.
        self.commit_state: Callable[[], None] | None = None
        # The owner installs one entry before it asks this host to open a Codex pane.  It is keyed
        # by the HeadRun id written in that launch intent, rather than by workspace or pane, so a
        # same-workspace respawn cannot inherit a predecessor's provider event source.
        self._codex_provider_ingresses: dict[str, CodexProviderEventIngress] = {}

    def configure_codex_provider_ingress(
        self,
        run: head_ops.HeadRun,
        *,
        persist: Callable[[head_ops.HeadRun], None],
        stop: Callable[[head_ops.HeadRun, str], None],
        block: Callable[[dict[str, Any]], None],
    ) -> None:
        """Install the launch owner's exact-run provider-event ingress."""
        source = run.fanout_policy.get("provider_source")
        if run.spec.adapter != "codex" or not isinstance(source, dict):
            return
        self._codex_provider_ingresses[run.run_id] = CodexProviderEventIngress(
            run, persist, stop=stop, block=block,
        )

    def poll_codex_provider_ingress(self, run: head_ops.HeadRun) -> None:
        """Best-effort read new provider events through the run-bound launch ingress."""
        ingress = self._codex_provider_ingresses.get(run.run_id)
        if ingress is None:
            # The collector is process-local.  A dispatcher recovery without it must not turn
            # missing fan-out telemetry into a lifecycle decision about an otherwise valid head.
            return
        ingress.commit_run(run)
        ingress.poll()

    def _codex_provider_ingress(self, run: head_ops.HeadRun) -> CodexProviderEventIngress | None:
        ingress = self._codex_provider_ingresses.get(run.run_id)
        if ingress is not None:
            return ingress
        return None

    @contextlib.contextmanager
    def committing(self, flush: Callable[[], None]):
        """Lend this runtime a way to flush the durable state, for as long as the caller holds it.
        """
        previous = self.commit_state
        self.commit_state = flush
        try:
            yield
        finally:
            self.commit_state = previous

    def preflight_codex_run(
        self,
        head: str,
        *,
        role: str,
        workspace: str,
        task_ref: head_ops.TaskRef,
        pid_file: str,
        run_id: str,
    ) -> head_ops.HeadRun:
        """Create and attest the durable run that exists before this Codex pane does."""
        profile = self.catalog.head_profile(head)
        spec = self._head_spec(head, str(profile.get("adapter") or "unknown"))
        run = head_ops.HeadRun(
            run_id=run_id,
            spec=spec,
            workspace=workspace,
            task_ref=task_ref,
            role=role,
            pid_file=pid_file,
        )
        if spec.adapter == "claude":
            return _prepare_claude_provider_progress_source(run)
        if spec.adapter != "codex":
            return run
        return preflight_codex_launch(profile, workspace, run)

    def _prompt_adapter(self, run: Any, head: str) -> str:
        """The provider whose framing a prompt for this pane is delivered in."""
        if isinstance(run, dict):
            adapter = str(run.get("adapter") or "").lower()
            if adapter:
                return adapter
        try:
            profile = self.catalog.head_profile(head)
        except (AttributeError, HostError) as exc:
            raise HostError(
                f"cannot resolve the prompt adapter for head {head!r}: {exc or 'unknown head'}"
            ) from None
        try:
            return HeadSpec.from_profile(head, profile).adapter.lower()
        except HeadSpecError as exc:
            raise HostError(f"cannot resolve the prompt adapter for head {head!r}: {exc}") from None

    def prepare_worker(
        self,
        task: dict[str, Any],
        worker_id: str,
        head: str,
        *,
        attempt_id: str = "",
        require_existing_workspace: bool = False,
        generation: int = 0,
        failover: bool = False,
        heartbeat_run_id: str = "",
    ) -> dict[str, Any]:
        project = task["project"]
        base = self.catalog.default_branch(project, task.get("workspace", {}).get("base_branch"))
        workspace = self.restore_workspace(task, worker_id)
        reused = Path(workspace).exists()
        if reused:
            self._validate_resumable_workspace(task, workspace)
        else:
            if require_existing_workspace:
                raise HostError("resume workspace is missing")
            workspace = self._create_workspace(project, worker_id, base, expected=workspace)
            self._set_worker_branch(workspace, _legacy_worker_branch(task["ref"]))
            self._run_setup(project, workspace)
        # The caller's generation, not a constant: the first round of a claim is as much a report
        # round as a rework, and its number is the one already durable in the dispatcher record.
        self._clear_report_bodies(task["ref"])
        self._write_prompt(
            Path(workspace) / "TASK.md", self._worker_task_doc(task, base, attempt_id, generation)
        )
        launched = self._launch(
            workspace,
            f"{task['ref']} worker",
            head,
            "TASK.md",
            role="worker",
            env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
            launch_prompt=self._worker_launch_prompt(),
            prompt_document=str(Path(workspace) / "TASK.md"),
            task=task,
            failover=failover,
            heartbeat_run_id=heartbeat_run_id,
        )
        return {
            "workspace": workspace,
            "handle": launched.handle,
            "leaf": launched.leaf,
            "base_branch": base,
            # The launch configuration of the head that went up. The caller records this instead of
            # re-reading the registry, which a later edit would answer differently.
            "run": launched.run,
            "delivery_evidence": dict(launched.delivery_evidence),
            # The head's own run (secretary-1412): the identity every later nudge and the stop
            # address this worker by, and where the initiator of that stop is eventually written.
            "head_run": dict(launched.head_run),
        }

    def restart_worker(
        self, task: dict[str, Any], record: DispatcherRecord, *, heartbeat_run_id: str = ""
    ) -> LaunchedHead:
        """Launch rework in the existing workspace without recreating its branch."""
        workspace = Path(record.workspace)
        if self.mode == "noop":
            workspace.mkdir(parents=True, exist_ok=True)
        elif not workspace.is_dir():
            raise HostError("rework workspace is missing")
        base = self.catalog.default_branch(
            task["project"], task.get("workspace", {}).get("base_branch")
        )
        self._clear_report_bodies(task["ref"])
        self._write_prompt(
            workspace / "TASK.md",
            self._worker_task_doc(
                task, base, record.attempt_id, record.report_generation, record.report_decision
            ),
        )
        return self._launch(
            str(workspace),
            f"{task['ref']} worker rework",
            record.head,
            "TASK.md",
            role="worker",
            env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
            launch_prompt=self._worker_launch_prompt(),
            prompt_document=str(workspace / "TASK.md"),
            task=task,
            failover=bool(record.preferred_head),
            heartbeat_run_id=heartbeat_run_id,
        )

    def observer_workspace(self, reference: str) -> str:
        """Where one sprint observer runs. Its own directory, never a card workspace and never the
        interactive secretary session's checkout: the observer reads reports and slices cards, it
        owns no branch of the project it watches."""
        if self.mode == "noop":
            return str(
                self.data_dir / "dispatcher" / OBSERVER_WORKSPACE_DIR / _request_token(reference)
            )
        root = Path(
            os.environ.get(
                "SECRETARY_DISPATCHER_WORKSPACES_ROOT", str(Path.home() / "orca" / "workspaces")
            )
        )
        return str(root / OBSERVER_WORKSPACE_DIR / _request_token(reference))

    def _observer_repo(self) -> Path:
        """The repo observer workspaces are cut from: standalone, empty, without a remote.

        Orca only gives terminals to worktrees of repositories it has registered, so a directory made
        with `mkdir` gets no terminal at all. The observer needs that registration, not a checkout of
        the project it watches, hence a repo of its own created once and shared by every sprint.
        """
        repo = observer_root_repo(self.data_dir)
        if not (repo / ".git").is_dir():
            repo.mkdir(parents=True, exist_ok=True)
            self._run(
                [
                    "git", "-C", str(repo), "init", "--quiet",
                    "--initial-branch", OBSERVER_REPO_BRANCH,
                ],
                "observer repo init",
            )
            self._run(
                [
                    "git", "-C", str(repo),
                    "-c", "user.name=secretary-dispatcher",
                    "-c", "user.email=dispatcher@localhost",
                    "commit", "--quiet", "--allow-empty", "-m", "observer root",
                ],
                "observer repo commit",
            )
        # How Orca learns the path. It answers with the same repo when it already knows it, so this
        # stays a no-op on every bring-up after the first instead of a first-run special case.
        self._run_json(["orca", "repo", "add", "--path", str(repo), "--json"])
        return repo

    def _observer_workspace_registered(self, workspace: str) -> bool:
        """Whether Orca knows this path as a worktree of its own.

        Only `selector_not_found` reads as "not registered". Any other failure is Orca declining to
        answer, and an unanswered question must not pass for a free path.
        """
        try:
            self._run_json(
                ["orca", "worktree", "show", "--worktree", f"path:{workspace}", "--json"]
            )
        except HostError as exc:
            if "selector_not_found" in str(exc):
                return False
            raise
        return True

    def _create_observer_workspace(self, reference: str) -> Path:
        """The observer's workspace, registered with Orca and at the path the record already names.
        """
        workspace = Path(self.observer_workspace(reference))
        if self._observer_workspace_registered(str(workspace)):
            return workspace
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
        repo = self._observer_repo()
        result = self._run_json([
            "orca", "worktree", "create",
            "--repo", f"path:{repo}",
            "--name", workspace.name,
            "--base-branch", OBSERVER_REPO_BRANCH,
            "--setup", "skip",
            "--no-parent",
            "--json",
        ])
        worktree = result.get("worktree") if isinstance(result.get("worktree"), dict) else result
        path = worktree.get("path") if isinstance(worktree, dict) else None
        if not isinstance(path, str) or not path:
            raise HostError("orca did not return an observer workspace path")
        if Path(path) != workspace:
            # The lifecycle wrote `workspace` into the launch intent before this call, and a tick
            # that dies now can only find the head through it. A workspace somewhere else is a
            # deferred bring-up with a readable reason, not a head nothing points at.
            raise HostError(f"orca placed the observer workspace at {path}, not {workspace}")
        return workspace

    def observer_pid_file(self, reference: str) -> str:
        """Where this sprint's observer heartbeat writes its pid."""
        return _observer_pid_file(reference)

    def prepare_observer(
        self,
        sprint: dict[str, Any],
        head: str,
        *,
        prompt: str,
        identity: dict[str, str] | None = None,
        heartbeat_run_id: str = "",
    ) -> dict[str, Any]:
        """Bring one observer head up on its own workspace and terminal."""
        reference = str(sprint.get("ref") or "")
        if self.mode == "noop":
            workspace = Path(self.observer_workspace(reference))
            workspace.mkdir(parents=True, exist_ok=True)
        else:
            workspace = self._create_observer_workspace(reference)
        self._write_prompt(workspace / OBSERVER_PROMPT_FILE, prompt)
        pid_file = _observer_pid_file(reference)
        run = self._observer_run(head, str(workspace))
        lifecycle_run = head_ops.HeadRun(
            run_id=heartbeat_run_id or head_ops.new_run_id(),
            spec=self._head_spec(head, str(run.get("adapter") or "unknown")),
            workspace=str(workspace),
            task_ref=head_ops.TaskRef.sprint(reference),
            role=OBSERVER_ROLE,
            pid_file=pid_file,
        )
        if lifecycle_run.spec.adapter == "codex":
            try:
                lifecycle_run = self.preflight_codex_run(
                    head,
                    role=OBSERVER_ROLE,
                    workspace=str(workspace),
                    task_ref=lifecycle_run.task_ref,
                    pid_file=pid_file,
                    run_id=lifecycle_run.run_id,
                )
            except CodexFanoutPolicyError as exc:
                raise HostError(str(exc)) from None
        heartbeat = self._run_heartbeat_identity(lifecycle_run, OBSERVER_ROLE)
        if self.mode == "noop":
            return {
                "workspace": str(workspace),
                "handle": f"noop:{head}:{workspace.name}:{OBSERVER_PROMPT_FILE}",
                "leaf": "",
                # No pane exists in this mode, so no prompt was put in front of anything: the
                # lifecycle must count no launch delivery for it.
                "prompt_delivered": False,
                "delivery_evidence": {},
                "pid_file": pid_file,
                "run": run,
                "head_run": lifecycle_run.to_json(),
            }
        # Drop a predecessor's pid before the new head can be read as this launch's liveness.
        _clear_head_heartbeat(pid_file)
        launch = self.catalog.head_launch(
            head,
            OBSERVER_PROMPT_FILE,
            workspace=str(workspace),
            role=OBSERVER_ROLE,
            launch_prompt=_observer_launch_prompt(),
            identity=identity,
        )
        pane = self._create_terminal(
            str(workspace),
            f"{reference} observer",
            _with_pid_heartbeat(launch.command, pid_file, identity=heartbeat),
        )
        lifecycle_run = lifecycle_run.rebound(pane.handle, leaf=pane.leaf)
        ingress = self._codex_provider_ingress(lifecycle_run)
        if ingress is not None:
            # The returned pane leaf is part of the run identity that the event source is bound
            # to.  Persist it before the first provider prompt, not in the ordinary post-launch
            # observer save below.
            ingress.commit_run(lifecycle_run)
        _bind_head_heartbeat(pid_file, expected=heartbeat, leaf=pane.leaf)
        delivered = False
        delivery_evidence: dict[str, Any] = {}
        post_delivery_run = lifecycle_run
        if launch.prompt_after_start:
            try:
                def bind_before_send() -> head_ops.HeadRun:
                    nonlocal post_delivery_run
                    if ingress is None:
                        return post_delivery_run
                    post_delivery_run = head_ops.post_delivery_run(
                        lifecycle_run, ingress.bind_before_delivery(),
                    )
                    return post_delivery_run

                outcome = _deliver_tui_prompt(
                    pane.handle,
                    str(workspace),
                    OBSERVER_PROMPT_FILE,
                    run_json=self._run_json,
                    adapter=launch.adapter or "codex",
                    prompt_text=_observer_launch_prompt(),
                    subject="observer-launch",
                    before_send=bind_before_send if ingress is not None else None,
                )
                lifecycle_run = post_delivery_run
                delivered = True
                delivery_evidence = _delivery_evidence_json(outcome, "observer-launch")
            except (TuiDeliveryError, HostError) as exc:
                # Every prompt this bring-up put in front of a head is accounted for, whether or
                # not the launch was carrying an unacknowledged batch: the first launch of a
                # sprint delivers a prompt too, and a sprint that lost it must be able to say so.
                evidence = _delivery_evidence_json(exc, "observer-launch")
                try:
                    self._stop_observer_terminals(
                        str(workspace),
                        pid_file=pid_file,
                        run=lifecycle_run,
                        role=OBSERVER_ROLE,
                        task=f"sprint:{reference}",
                        leaf=pane.leaf,
                    )
                except Exception as stop_exc:
                    # The pane is still up. Its handle goes back with the failure, because this
                    # dict is the only pointer to it: reporting a plain bring-up failure would
                    # leave the sprint reading as headless and the next tick would open a second
                    # head beside a head that is already running.
                    raise ObserverLaunchAborted(
                        f"{exc}; observer terminal stop failed: {stop_exc}",
                        handle=pane.handle,
                        leaf=pane.leaf,
                        workspace=str(workspace),
                        pid_file=pid_file,
                        run=run,
                        evidence=evidence,
                    ) from None
                raise ObserverLaunchAborted(str(exc), evidence=evidence) from None
        return {
            "workspace": str(workspace),
            "handle": pane.handle,
            "leaf": pane.leaf,
            # Whether this bring-up put a prompt in front of the head, and what the delivery
            # boundary saw doing it. The lifecycle counts a launch delivery from this rather than
            # from whether the launch happened to carry a pending batch.
            "prompt_delivered": delivered,
            "delivery_evidence": delivery_evidence,
            "pid_file": pid_file,
            "run": run,
            # This mirrors `head_ops.spawn`: delivery owns the source handoff, while this
            # launcher adds only the pane facts it already proved.  Returning `lifecycle_run`
            # rather than its pre-send copy keeps observer adoption on the same run the ingress
            # persisted.
            "head_run": lifecycle_run.to_json(),
        }

    def stop_observer(self, record: Any) -> None:
        """End one observer head and give back what its bring-up took."""
        if self.mode == "noop":
            return
        observer_run = getattr(record, "head_run", {})
        observer_leaf = str(getattr(record, "leaf", "") or "")
        pid_file = str(getattr(record, "pid_file", "") or "")
        self._guard_head_run(
            observer_run,
            OBSERVER_ROLE,
            pid_file=pid_file,
            task=f"sprint:{getattr(record, 'sprint', '')}",
            leaf=observer_leaf,
        )
        workspace = str(getattr(record, "workspace", "") or "")
        if not workspace:
            # A record written before the launch intent named a workspace: the handle is the only
            # pointer left to that head.
            if record.handle:
                self._close_observer_pane(record.handle)
            return
        if not self._observer_workspace_registered(workspace):
            self._confirm_head_process_gone(
                pid_file,
                run=observer_run,
                role=OBSERVER_ROLE,
                task=f"sprint:{getattr(record, 'sprint', '')}",
                leaf=observer_leaf,
            )
            return
        self._stop_observer_terminals(
            workspace,
            pid_file=pid_file,
            run=observer_run,
            role=OBSERVER_ROLE,
            task=f"sprint:{getattr(record, 'sprint', '')}",
            leaf=observer_leaf,
        )
        # Heartbeat-wrapped heads have their own session, so terminal stop alone cannot prove the
        # observer died. Do not remove its worktree or forget its record until this confirms it.
        self._confirm_head_process_gone(
            pid_file,
            run=observer_run,
            role=OBSERVER_ROLE,
            task=f"sprint:{getattr(record, 'sprint', '')}",
            leaf=observer_leaf,
        )
        self._run_json([
            "orca", "worktree", "rm", "--worktree", f"path:{workspace}", "--force", "--json"
        ])

    def observer_status(self, record: Any) -> dict[str, Any]:
        """Read the observer pane's output clock and whether it is ready for a prompt."""
        if self.mode == "noop":
            return {}
        if not record.workspace or not (record.handle or record.leaf):
            raise HostError("observer record names no terminal to read")
        terminals = self._worktree_terminals(str(record.workspace))
        terminal = next(
            (pane for pane in terminals if record.leaf and pane.leaf == record.leaf),
            None,
        )
        if terminal is None and not record.leaf:
            terminal = next(
                (pane for pane in terminals if record.handle and pane.handle == record.handle),
                None,
            )
        if terminal is None:
            raise HostError("observer terminal is not in the inventory of its workspace")
        if not terminal.connected:
            raise HostError("observer terminal is not connected")
        readiness = _terminal_readiness(terminal.handle, run_json=self._run_json)
        if readiness == READINESS_UNKNOWN:
            # A probe that failed is not a working observer. Raising puts it on the lifecycle's
            # bounded failure path, where a busy pane would wait forever instead.
            raise HostError("observer terminal readiness could not be read")
        status: dict[str, Any] = {"idle": readiness == READINESS_READY}
        if terminal.last_output_at:
            status["last_activity"] = terminal.last_output_at
        # Only the idle-recovery path needs that clock, and it says so itself when it is missing.
        return status

    def observer_provider_progress(self, record: Any) -> dict[str, str]:
        """Read provider progress only from this observer's persisted HeadRun."""
        stored = getattr(record, "head_run", {})
        expected_workspace = str(getattr(record, "workspace", "") or "")
        expected_sprint = str(getattr(record, "sprint", "") or "")
        try:
            run = head_ops.HeadRun.from_json(stored)
        except (head_ops.HeadRunError, TypeError, ValueError):
            return {"state": "unavailable", "reason": "persisted observer HeadRun is unavailable"}
        if (
            run.workspace != expected_workspace
            or run.task_ref.kind != "sprint"
            or run.task_ref.ref != expected_sprint
            or (run.role and run.role != OBSERVER_ROLE)
        ):
            return {
                "state": "identity_mismatch",
                "reason": "persisted observer HeadRun binding mismatches observer record",
            }
        return _provider_progress_for_run(run)

    def nudge_observer(self, record: Any) -> str:
        """Give an idle observer one event-driven turn without replacing its head."""
        if self.mode == "noop":
            return DELIVERY_ACCEPTED
        workspace = str(getattr(record, "workspace", "") or "")
        handle = str(getattr(record, "handle", "") or "")
        leaf = str(getattr(record, "leaf", "") or "")
        if not workspace or not (handle or leaf):
            raise HostError("observer has no terminal handle for an event wake")
        terminals = self._worktree_terminals(workspace)
        terminal = next((pane for pane in terminals if leaf and pane.leaf == leaf), None)
        if terminal is None and not leaf:
            terminal = next((pane for pane in terminals if handle and pane.handle == handle), None)
        current = terminal.handle if terminal is not None else ""
        if not current:
            raise HostError("observer terminal is unavailable for an event wake")
        delivery = getattr(record, "delivery", None)
        delivery_id = str(getattr(delivery, "delivery_id", "") or "")
        through_event = str(getattr(delivery, "through_event", "") or "")
        message = (
            "A linked card changed. Read its worker report, reviewer verdict and any valid executed "
            "exact-SHA gate receipt first. Suppress a routine broad rerun only when that receipt exists; "
            "none/noop/missing evidence proves no broad suite, so run or request appropriate validation "
            "when the decision needs it. Take the next semantic step, then record resume."
        )
        if delivery_id and through_event:
            message += (
                " Acknowledge this delivery in that resume with --delivery-id "
                f"{delivery_id} --through-event {through_event}."
            )
        # The wakes this sprint has already lost travel with the wake that reaches the head, so the
        # resume or closeout written from this turn reports what actually happened rather than the
        # nothing a head can see of a prompt that never arrived.
        evidence_line = _observer_delivery_evidence_summary(delivery) if delivery is not None else ""
        if evidence_line:
            message += f" Sprint delivery evidence to carry into your closing resume: {evidence_line}."
        try:
            return _deliver_interactive_prompt(
                current,
                message,
                run_json=self._run_json,
                adapter=self._prompt_adapter(
                    getattr(record, "run", {}), str(getattr(record, "head", ""))
                ),
                ack_out_of_band=True,
                subject="observer-wake",
            )
        except TuiDeliveryError as exc:
            failure = HostError(f"observer wake was not delivered: {exc}")
            # The lifecycle stores this beside the sprint. It is the delivery boundary's own
            # evidence — terminal identity, payload size and hash, the stage the delivery reached,
            # the composer and output fingerprints around it — and carries no prompt text.
            failure.evidence = getattr(exc, "evidence", None)
            raise failure from None

    def _stop_observer_terminals(
        self,
        workspace: str,
        *,
        pid_file: str = "",
        run: Any = None,
        role: str = "",
        task: str = "",
        leaf: str = "",
    ) -> None:
        """Stop every pane of an observer workspace.

        One verb for the whole workspace rather than a close per handle: `close_pane` answers
        `tab_not_found` for a pane the runtime never gave a UI tab, so a per-handle close reports a
        stop that worked as a stop that failed.
        """
        if run is not None:
            self._guard_head_run(run, role, pid_file=pid_file, task=task, leaf=leaf)
        self.session.stop_workspace(workspace)

    def _close_observer_pane(self, handle: str) -> None:
        try:
            _close_tui_terminal_strict(handle, run_json=self._run_json)
        except HostError:
            raise
        except Exception as exc:
            raise HostError(f"observer terminal close failed: {exc}") from None

    def _observer_run(self, head: str, workspace: str) -> dict[str, Any]:
        try:
            return self.catalog.observer_run(head, workspace=workspace).to_json()
        except (HostError, AttributeError, KeyError, TypeError):
            return HeadRun(
                role=OBSERVER_ROLE, head=head, adapter="unknown", model_source=MODEL_UNKNOWN
            ).to_json()

    def provider_progress(
        self, task: dict[str, Any], record: DispatcherRecord, kind: str
    ) -> dict[str, str]:
        """Read an opaque provider cursor for this exact role's persisted HeadRun."""
        run = record.review_head_run if kind == "review" else record.worker_head_run
        expected_workspace = record.workspace
        try:
            lifecycle_run = head_ops.HeadRun.from_json(run)
        except (head_ops.HeadRunError, TypeError, ValueError):
            return {"state": "unavailable", "reason": "persisted HeadRun is unavailable"}
        expected_role = "reviewer" if kind == "review" else "worker"
        if (
            lifecycle_run.workspace != expected_workspace
            or lifecycle_run.task_ref.kind != "card"
            or lifecycle_run.task_ref.ref != str(task.get("ref") or "")
            or (lifecycle_run.role and lifecycle_run.role != expected_role)
        ):
            return {"state": "identity_mismatch", "reason": "persisted HeadRun binding mismatches role"}
        # Claude's launch contract records the pre-pane baseline on the HeadRun.  The provider
        # creates its transcript asynchronously, so the first read may be the first moment a
        # single new source exists.  Binding still uses only that durable launch baseline and is
        # persisted before the source can produce a liveness cursor; ambiguity remains typed
        # unavailable rather than a workspace selection.
        if lifecycle_run.spec.adapter == "claude":
            updated = _bind_claude_provider_progress_source(lifecycle_run)
            if updated != lifecycle_run:
                if kind == "review":
                    self._commit_review_run(record, updated)
                else:
                    self._commit_worker_run(record, updated)
                lifecycle_run = updated
        return _provider_progress_for_run(lifecycle_run)

    def safe_recover_worker_continuation(
        self,
        _task: dict[str, Any],
        _record: DispatcherRecord,
        _liveness: dict[str, Any],
    ) -> dict[str, str]:
        """Advertise that this host has no safe provider recovery primitive.

        An operator's raw interrupt can terminate a Codex provider session and a generic key chord
        cannot prove which composer it affected, so the only permitted answer is this typed absence.
        The dispatcher then takes its confirmed-stop fence before replacement.
        """
        return {
            "state": "unavailable",
            "reason": "no provider/terminal-safe continuation recovery capability is available",
        }

    def start_review(self, task: dict[str, Any], record: DispatcherRecord) -> ReviewLaunch:
        """Bring the reviewer up as a second pane inside the worker's own worktree.

        The pane is split off a live pane there rather than created as a new terminal: a plain
        `terminal create` on a headless serve lands as a background surface no client materialises.
        Once the reviewer has its pane the worker is shut down and its commit pinned, so the reviewer
        judges a checkout nothing else is still editing. What that pane is given is a nudge at a
        document written outside the checkout, not the review itself.
        """
        if not record.workspace:
            raise HostError("review workspace is unavailable")
        workspace = Path(record.workspace)
        if self.mode == "noop":
            workspace.mkdir(parents=True, exist_ok=True)
        elif not workspace.is_dir():
            raise HostError("review workspace is missing")
        self._clear_body_file("verdict", task["ref"], record.review_baseline)
        document, nudge = self._review_document(task, record)
        launched = self._launch(
            record.workspace,
            review_pane_label(task["ref"]),
            record.review_head,
            str(document),
            role="reviewer",
            env_name="SECRETARY_DISPATCHER_REVIEW_COMMAND",
            launch_prompt=nudge,
            prompt_document=str(document),
            split_from=self._split_anchor(record),
            task=task,
            failover=bool(record.preferred_review_head),
            heartbeat_run_id=str((record.launch_intent or {}).get("run_id") or ""),
        )
        try:
            if record.worker_continuation.retained and self.worker_retained_vanished(record):
                # The retained worker's process is provably gone: orca has no session for it and
                # its pid heartbeat resolves to a pid the OS no longer knows. A vanished session
                # cannot touch the checkout the reviewer judges, so there is no second writer to
                # freeze — the commit it left stands on its own and the reviewer takes it. A red
                # verdict on this round finds no session to resume and opens a replacement. Taking
                # the freeze path here instead would raise over a head that will never confirm
                # suspended and loop `review-launch-aborted` forever (issue:aa9a8ae4).
                pass
            elif record.worker_continuation.retained:
                # A retained worker is already SIGSTOPed: it cannot touch the checkout the reviewer
                # judges, and its conversation is what a red verdict continues. Confirm that
                # suspension rather than trusting the record; anything else takes the freeze path.
                self.confirm_worker_retained(record)
            else:
                self._freeze_worker(record)
        except HostError as exc:
            # The reviewer pane is up and the worker would not stop. Neither head can be reported
            # as absent, so the bring-up hands the reviewer's pane back with the failure and the
            # caller keeps its launch intent: dropping the record here would leave a live reviewer
            # with nothing pointing at it, and the freeze is retried on the recovery path.
            raise HeadLaunchAborted(
                f"worker freeze failed: {exc}",
                handle=launched.handle,
                leaf=launched.leaf,
                workspace=record.workspace,
                pid_file=_pid_file_path("review", task["ref"]),
                evidence=dict(launched.delivery_evidence),
                head_run=dict(launched.head_run),
            ) from None
        return ReviewLaunch(
            handle=launched.handle,
            leaf=launched.leaf,
            commit=self.head_commit(record),
            run=launched.run,
            head_run=dict(launched.head_run),
            delivery_evidence=dict(launched.delivery_evidence),
        )

    def nudge_review_delivery(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        """Retry the document nudge for the exact reviewer a busy launch intent retained."""
        if not record.workspace:
            raise HostError("review workspace is unavailable for a retained launch")
        stored_run = intent.get("head_run")
        if not isinstance(stored_run, dict) or not stored_run.get("run_id"):
            raise HostError("retained reviewer launch has no durable head run")
        try:
            run = head_ops.HeadRun.from_json(stored_run)
        except (head_ops.HeadRunError, head_ops.TaskRefError) as exc:
            raise HostError(f"retained reviewer launch has an unreadable head run: {exc}") from None
        workspace = str(intent.get("workspace") or record.workspace)
        handle = str(intent.get("handle") or run.handle)
        leaf = str(intent.get("leaf") or run.leaf)
        pid_file = str(intent.get("pid_file") or run.pid_file)
        run = replace(run, workspace=workspace, handle=handle, leaf=leaf, pid_file=pid_file)
        ingress = self._codex_provider_ingress(run)
        if ingress is not None:
            ingress.commit_run(run)
            # A recovered, already-bound source is consumed before another delivery can act.  An
            # unbound source belongs to a prior busy pre-send attempt and is bound by the same
            # before-send boundary as the original launch.
            if ingress.source.get("state") == "bound":
                ingress.poll()
        document, nudge = self._review_document(task, record)
        try:
            outcome = head_ops.nudge(
                run,
                head_ops.NudgePointer(text=nudge, document=str(document)),
                host=self.session,
                transport=self._head_transport(
                    workspace,
                    str(document),
                    self._prompt_adapter(intent.get("run"), record.review_head),
                    "reviewer",
                    before_send=ingress.bind_before_delivery if ingress is not None else None,
                ),
                subject="reviewer-launch",
            )
        except (head_ops.HeadOperationError, TuiDeliveryError, HostError) as exc:
            failure = HostError(f"retained reviewer document nudge was not delivered: {exc}")
            failure.evidence = _delivery_evidence_json(exc, "reviewer-launch")
            raise failure from None
        return {
            "handle": outcome.run.handle,
            "leaf": outcome.run.leaf,
            "head_run": outcome.run.to_json(),
            "delivery_evidence": _delivery_evidence_json(outcome.delivery, "reviewer-launch"),
        }

    def worker_status(self, task: dict[str, Any], record: DispatcherRecord) -> dict[str, Any]:
        return _command_terminal_status(self, task, record, kind="worker")

    def review_status(self, task: dict[str, Any], record: DispatcherRecord) -> dict[str, Any]:
        return _command_terminal_status(self, task, record, kind="review")

    def stop_review(
        self, record: DispatcherRecord, initiator: str = STOPPED_BY_DISPATCHER
    ) -> None:
        """End the reviewer's lifecycle alone. `stop` would take the whole worktree down with it,
        which on a red verdict means killing the checkout's terminals the worker is about to get
        back. Closing the reviewer's own split leaf removes that pane and leaves the rest alone.
        """
        if self.mode == "noop" or not (
            record.review_handle or record.review_leaf or record.review_pid_file
        ):
            return
        self.stop_head(record, "review", initiator)

    def head_commit(self, record: DispatcherRecord) -> str:
        """Commit the workspace checkout currently sits on, or "" when it cannot be read. Pinned
        at review start and re-read at merge time so a verdict can be tied to a code state."""
        if self.mode == "noop" or not record.workspace:
            return ""
        try:
            completed = self._run(
                ["git", "-C", record.workspace, "rev-parse", "HEAD"], "review head sha"
            )
        except HostError:
            return ""
        return completed.stdout.strip()

    def is_instance_publish_recovery(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        reviewed_commit: str,
        current_commit: str,
    ) -> bool:
        if self.mode == "noop" or not record.workspace:
            return False
        try:
            repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        except (KeyError, HostError):
            return False
        if not _same_repo(repo, Path(getattr(self.catalog, "instance_dir"))):
            return False
        base = self.catalog.default_branch(task["project"], task.get("workspace", {}).get("base_branch"))
        try:
            self._run(["git", "-C", record.workspace, "fetch", "origin", base], "review recovery fetch")
            remote_head = self._run(
                ["git", "-C", record.workspace, "rev-parse", f"origin/{base}"],
                "review recovery remote head",
            ).stdout.strip()
            parents = self._run(
                ["git", "-C", record.workspace, "rev-list", "--parents", "-n", "1", current_commit],
                "review recovery parents",
            ).stdout.split()
            local_head = self._run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                "review recovery local head",
            ).stdout.strip()
            self._run(
                ["git", "-C", record.workspace, "merge-base", "--is-ancestor", reviewed_commit, current_commit],
                "review recovery ancestry",
            )
        except HostError:
            return False
        if not (
            remote_head
            and remote_head == current_commit
            and reviewed_commit in parents[1:]
            and len(parents) > 2
        ):
            return False
        for parent in parents[1:]:
            if parent == reviewed_commit:
                continue
            if not self._commit_is_ancestor(str(repo), parent, local_head):
                return False
        return True

    def gate_check(self, task: dict[str, Any], record: DispatcherRecord) -> GateResult:
        return _gate_check(self, task, record)

    def verify_worker_result(self, task: dict[str, Any], record: DispatcherRecord) -> None:
        if self.mode == "noop":
            return
        workspace = Path(record.workspace)
        if not workspace.is_dir():
            raise HostError("worker workspace is missing")
        completed = self._run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            "git status",
        )
        if durability_dirt(completed.stdout):
            raise HostError("worker reported done with uncommitted changes")

    def restore_workspace(self, task: dict[str, Any], worker: str) -> str:
        """Where this card's worker checkout lives, new or already cut.

        The namespace under the workspaces root is Orca's: <root>/<repo registration name>/<worktree
        name>, where the registration name is the binding's `orca_binding` and not the Secretary
        project id — the two spellings differ and the id is not what decides where the checkout goes.
        """
        if self.mode == "noop":
            return str(self.data_dir / "dispatcher" / "workspaces" / worker)
        root = Path(os.environ.get("SECRETARY_DISPATCHER_WORKSPACES_ROOT", str(Path.home() / "orca" / "workspaces")))
        return str(root / self._orca_binding_name(str(task.get("project") or "")) / worker)

    def _orca_binding_name(self, project: str) -> str:
        """The Orca repo registration name this project's workspaces are namespaced by."""
        binding = self.catalog.binding(project)
        name = binding.get("orca_binding")
        if isinstance(name, str) and name:
            return name
        return str(self._orca_repo(project).get("displayName") or "")

    def _orca_repo(self, project: str) -> dict[str, Any]:
        """This project's Orca repo registration, resolved from the configured repo path."""
        repo = Path(str(self.catalog.binding(project)["repo"])).expanduser()
        listing = self._run_json(["orca", "repo", "list", "--json"])
        repos = listing.get("repos") if isinstance(listing, dict) else None
        for entry in repos if isinstance(repos, list) else []:
            path = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(path, str) and path and _same_repo(Path(path), repo):
                if not isinstance(entry.get("id"), str) or not entry["id"]:
                    raise HostError(f"orca registered {repo} without an id")
                return entry
        raise HostError(f"project {project!r} repo {repo} is not registered with orca")

    def complete_green(self, task: dict[str, Any], record: DispatcherRecord) -> None:
        if self.mode == "noop" or not record.workspace:
            return
        if os.environ.get("SECRETARY_DISPATCHER_AUTOMERGE", "on").strip().lower() == "off":
            return
        branch = _legacy_worker_branch(task["ref"])
        base = self.catalog.default_branch(task["project"], task.get("workspace", {}).get("base_branch"))
        if _validation_ci(self, task) == "github":
            self._merge_github_pr(task, record, branch, base)
            return
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        if _same_repo(repo, Path(getattr(self.catalog, "instance_dir"))):
            self._complete_green_instance_repo(record, branch, base, repo)
            return
        # Publish the reviewed branch as main (a non-fast-forward push is rejected, never
        # force-landed), then fast-forward the project's own checkout. The dispatcher runs
        # from the secretary checkout, so this is how a merged self-modification reaches the
        # next oneshot tick; other projects just stay current for the next worktree base.
        self._run(["git", "-C", record.workspace, "push", "origin", f"{branch}:main"], "merge push")
        self._run(["git", "-C", str(repo), "fetch", "origin", "main"], "post-merge fetch")
        self._run(["git", "-C", str(repo), "merge", "--ff-only", "origin/main"], "post-merge fast-forward")

    def _complete_green_instance_repo(
        self,
        record: DispatcherRecord,
        branch: str,
        base: str,
        repo: Path,
    ) -> None:
        """Publish an instance-repo card without racing checkpoint commits."""
        with state_repo.state_repo_lock(repo):
            self._run(["git", "-C", record.workspace, "fetch", "origin", base], "merge preflight fetch")
            branch_head = self._run(
                ["git", "-C", record.workspace, "rev-parse", branch],
                "merge preflight branch head",
            ).stdout.strip()
            local_head = self._run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                "merge preflight local head",
            ).stdout.strip()
            remote_head = self._run(
                ["git", "-C", record.workspace, "rev-parse", f"origin/{base}"],
                "merge preflight remote head",
            ).stdout.strip()
            if not self._commit_is_ancestor(record.workspace, remote_head, branch_head):
                if not self._commit_is_ancestor(str(repo), remote_head, local_head):
                    raise HostError(
                        f"merge preflight failed: origin/{base} contains unreviewed remote history"
                    )
                self._run(
                    ["git", "-C", record.workspace, "fetch", str(repo), "HEAD"],
                    "merge preflight fetch local checkpoint",
                )
                self._run(
                    [
                        "git",
                        *state_repo.commit_identity(Path(record.workspace)),
                        "-C",
                        record.workspace,
                        "merge",
                        "--no-edit",
                        "FETCH_HEAD",
                    ],
                    "merge preflight checkpoint sync",
                )
            self._run(["git", "-C", record.workspace, "push", "origin", f"{branch}:{base}"], "merge push")
            self._run(["git", "-C", str(repo), "fetch", "origin", base], "post-merge fetch")
            self._run(
                [
                    "git",
                    *state_repo.commit_identity(repo),
                    "-C",
                    str(repo),
                    "merge",
                    "--no-edit",
                    f"origin/{base}",
                ],
                "post-merge reconcile",
            )

    def _commit_is_ancestor(self, repo: str, ancestor: str, descendant: str) -> bool:
        try:
            self._run(
                ["git", "-C", repo, "merge-base", "--is-ancestor", ancestor, descendant],
                "merge ancestry check",
            )
        except HostError:
            return False
        return True

    def _merge_github_pr(self, task: dict[str, Any], record: DispatcherRecord, branch: str, base: str) -> None:
        """Land a github-CI project through its PR, then fast-forward the project's own checkout so the
        next worktree bases on the merged tree.

        gh honours branch protection and refuses to merge while required checks are unsatisfied. The
        checkout tracks the project's default branch, not the card's base, and the refresh stays
        best-effort: the card is already merged by then, so a failed refresh is not the card's failure.
        """
        self._run(["gh", "pr", "merge", branch, "--merge"], "merge pr", cwd=Path(record.workspace))
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        default_branch = self.catalog.default_branch(task["project"], None)
        # `gh pr merge` is the irreversible delivery boundary.  Refreshing this checkout afterwards
        # is only a convenience for future worktree bases; a user's preserved local commit or dirty
        # branch may legitimately make ff-only impossible.  Never report the already-merged card as
        # failed because that best-effort cache refresh could not be applied.
        try:
            refresh_branch = base if base == default_branch else default_branch
            self._run(
                ["git", "-C", str(repo), "fetch", "origin", refresh_branch],
                "post-merge fetch",
            )
            self._run(
                ["git", "-C", str(repo), "merge", "--ff-only", f"origin/{refresh_branch}"],
                "post-merge fast-forward",
            )
        except HostError:
            pass

    def stop(self, record: DispatcherRecord) -> None:
        if self.mode == "noop" or not record.workspace:
            return
        try:
            self.stop_workspace(record)
        except HostError:
            pass

    def stop_workspace(self, record: DispatcherRecord) -> None:
        """Stop every head of this workspace and let a refusal reach the caller.

        The confirmed twin of `stop`: a refused workspace stop is not evidence the head is gone, so a
        path that opens a replacement afterwards must use this one. `selector_not_found` is the single
        exception — Orca has no worktree there at all, which is not a refused stop.
        """
        if self.mode == "noop" or not record.workspace:
            return
        heartbeats: list[tuple[str, Any, str, str]] = []
        for kind in ("worker", "review"):
            field = "worker_head_run" if kind == "worker" else "review_head_run"
            pid_file = record.worker_pid_file if kind == "worker" else record.review_pid_file
            leaf = record.worker_leaf if kind == "worker" else record.review_leaf
            heartbeats.append((pid_file, getattr(record, field, {}), kind, leaf))
        # A workspace stop kills every pane it contains.  Fence every recorded head before the
        # first destructive call, not afterwards when an unrelated live process may already have
        # lost its pane.
        for pid_file, run, kind, leaf in heartbeats:
            self._guard_head_run(run, kind, pid_file=pid_file, leaf=leaf)
        try:
            self.session.stop_workspace(record.workspace)
        except HostError as exc:
            if "selector_not_found" not in str(exc):
                raise
        for pid_file, run, kind, leaf in heartbeats:
            self._confirm_head_process_gone(
                pid_file, run=run, role=kind, leaf=leaf,
            )

    def stop_head(
        self, record: DispatcherRecord, kind: str, initiator: str = STOPPED_BY_DISPATCHER
    ) -> None:
        """Stop one role's head through the head operation, recording who ended it."""
        if self.mode == "noop":
            return
        if kind == "review":
            self.stop_review_head(record, initiator)
            return
        self.stop_worker_head(record, initiator)

    def stop_review_head(
        self, record: DispatcherRecord, initiator: str = STOPPED_BY_DISPATCHER
    ) -> None:
        """End this card's reviewer through the head operation, recording who ended it."""
        run = self.review_lifecycle_run(record)
        try:
            outcome = head_ops.stop(
                run,
                head_ops.StopInitiator(actor=initiator),
                host=self.session,
                transport=self._head_transport(record.workspace, role="reviewer"),
                commit=lambda finishing: self._commit_review_run(record, finishing),
                preflight=lambda current: self._guard_head_run(current, "reviewer"),
                confirm_gone=lambda path: self._confirm_head_process_gone(
                    path, run=run, role="reviewer",
                ),
            )
        except head_ops.HeadStopFailed as exc:
            # `head_ops.stop` itself commits a proved run before close or signalling. A preflight
            # mismatch is intentionally uncommitted: that foreign process was never this run.
            raise HostError(str(exc)) from None
        self._commit_review_run(record, outcome.run)

    def _commit_review_run(self, record: DispatcherRecord, run: head_ops.HeadRun) -> None:
        """Write this reviewer's run onto the record, flushing it when the caller lent us the state.
        """
        record.review_head_run = run.to_json()
        if self.commit_state is not None:
            self.commit_state()

    def review_lifecycle_run(self, record: DispatcherRecord) -> head_ops.HeadRun:
        """This card's reviewer as the head operations see it."""
        stored = record.review_head_run if isinstance(record.review_head_run, dict) else {}
        run: head_ops.HeadRun | None = None
        if stored.get("run_id"):
            try:
                run = head_ops.HeadRun.from_json(stored)
            except (head_ops.HeadRunError, head_ops.TaskRefError):
                run = None
            if run is not None and run.settled and record.owns_head(REVIEW_ROLE):
                run = None
        if run is None:
            run = head_ops.HeadRun(
                run_id=head_ops.new_run_id(),
                spec=HeadSpec(
                    profile_id=record.review_head,
                    adapter=self._prompt_adapter(record.review_run, record.review_head),
                ),
                workspace=record.workspace,
                # The reviewer's own worker id is the card's, as the claim built it: `<ref>-<slug>`.
                # A pointer that names the head being reconstructed is the truthful one; inventing
                # a card reference this call never received would not be.
                task_ref=head_ops.TaskRef.card(
                    record.worker or record.review_head or "unknown-reviewer"
                ),
            )
        return replace(
            run,
            workspace=record.workspace or run.workspace,
            handle=record.review_handle,
            leaf=record.review_leaf,
            pid_file=record.review_pid_file,
        )

    def stop_worker_head(
        self, record: DispatcherRecord, initiator: str = STOPPED_BY_DISPATCHER
    ) -> None:
        """End this card's worker through the head operation, recording who ended it."""
        run = self.worker_lifecycle_run(record)
        try:
            outcome = head_ops.stop(
                run,
                head_ops.StopInitiator(actor=initiator),
                host=self.session,
                transport=self._head_transport(record.workspace, role="worker"),
                commit=lambda finishing: self._commit_worker_run(record, finishing),
                preflight=lambda current: self._guard_head_run(current, "worker"),
                confirm_gone=lambda path: self._confirm_head_process_gone(
                    path, run=run, role="worker",
                ),
            )
        except head_ops.HeadStopFailed as exc:
            # A post-attribution stop failure was already committed by `head_ops.stop`; a failed
            # preflight must leave the persisted HeadRun untouched.
            raise HostError(str(exc)) from None
        self._commit_worker_run(record, outcome.run)

    def _commit_worker_run(self, record: DispatcherRecord, run: head_ops.HeadRun) -> None:
        """Write this worker's run onto the record, and flush it if the caller gave us the state.
        """
        record.worker_head_run = run.to_json()
        if self.commit_state is not None:
            self.commit_state()

    def commit_gate_pr_authorship(self, record: DispatcherRecord, entry: dict[str, Any]) -> None:
        """Write down that the github gate wrote a known text on a known pull request."""
        record.gate_pr_authorship = dict(entry)
        if self.commit_state is not None:
            self.commit_state()

    def worker_lifecycle_run(self, record: DispatcherRecord) -> head_ops.HeadRun:
        """This card's worker as the head operations see it."""
        stored = record.worker_head_run if isinstance(record.worker_head_run, dict) else {}
        run: head_ops.HeadRun | None = None
        if stored.get("run_id"):
            try:
                run = head_ops.HeadRun.from_json(stored)
            except (head_ops.HeadRunError, head_ops.TaskRefError):
                run = None
            if run is not None and run.settled and record.owns_head(WORKER_ROLE):
                # The record still names a pane or a heartbeat while the run says that head was
                # confirmed gone: whatever is there is not the run that ended, so it is not stopped
                # as that run either. A fresh identity below is what keeps a stop from quietly
                # skipping a head because a previous one was confirmed.
                run = None
        if run is None:
            run = head_ops.HeadRun(
                run_id=head_ops.new_run_id(),
                spec=HeadSpec(
                    profile_id=record.head,
                    adapter=self._prompt_adapter(record.worker_run, record.head),
                ),
                workspace=record.workspace,
                # No card reference reaches this call, and the worker id it does have carries one:
                # `<ref>-<slug>` is what the claim built it from. A pointer that names the worker
                # is the truthful reconstruction; inventing a card reference would not be.
                task_ref=head_ops.TaskRef.card(record.worker or record.head or "unknown-worker"),
            )
        return replace(
            run,
            workspace=record.workspace or run.workspace,
            handle=record.handle,
            leaf=record.worker_leaf,
            pid_file=record.worker_pid_file,
        )

    @staticmethod
    def _head_status(
        pid_file: str,
        *,
        run: Any = None,
        role: str = "",
        task: str = "",
        leaf: str = "",
        expected: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Classify a head through its persisted run whenever one is available."""
        if run is not None:
            return _head_run_process_status(
                pid_file, run=run, role=role, task=task, leaf=leaf,
            )
        return _head_process_status(pid_file, expected=expected)

    def _guard_head_run(
        self,
        run: Any,
        role: str,
        *,
        pid_file: str = "",
        task: str = "",
        leaf: str = "",
    ) -> dict[str, Any]:
        """Fence a live foreign process before any lifecycle attribution or destructive call."""
        if not pid_file:
            pid_file = str(getattr(run, "pid_file", "") or "")
            if isinstance(run, dict):
                pid_file = str(run.get("pid_file") or pid_file)
        if not pid_file:
            return {"known": False, "reason": "missing-pid-file"}
        try:
            return _guard_head_run_identity(
                pid_file, run=run, role=role, task=task, leaf=leaf,
            )
        except _HeadRunIdentityMismatch:
            raise HostError(f"head heartbeat from {pid_file} has a mismatching launch identity") from None

    def _confirm_head_process_gone(
        self,
        pid_file: str,
        *,
        run: Any = None,
        role: str = "",
        task: str = "",
        leaf: str = "",
        expected: dict[str, str] | None = None,
    ) -> None:
        """Make sure the process behind a heartbeat is not running, escalating if it is."""
        if not pid_file:
            return
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            status = self._head_status(
                pid_file, run=run, role=role, task=task, leaf=leaf, expected=expected,
            )
            if _heartbeat_is_mismatch(status):
                raise HostError(f"head heartbeat from {pid_file} has a mismatching launch identity")
            if not status.get("known") or not status.get("alive"):
                if status.get("known"):
                    _clear_head_heartbeat(pid_file)
                return
            # SIGTERM and SIGHUP remain pending for a SIGSTOPed retained worker.  Wake its group
            # before the graceful signal so green handoff does not wait out the whole grace period
            # and then kill the worker unconditionally.
            if signal_number == signal.SIGTERM:
                self._signal_head(
                    pid_file, signal.SIGCONT,
                    run=run, role=role, task=task, leaf=leaf, expected=expected,
                )
            self._signal_head(
                pid_file, signal_number,
                run=run, role=role, task=task, leaf=leaf, expected=expected,
            )
            self._await_head_exit(
                pid_file, run=run, role=role, task=task, leaf=leaf, expected=expected,
            )
        status = self._head_status(
            pid_file, run=run, role=role, task=task, leaf=leaf, expected=expected,
        )
        if _heartbeat_is_mismatch(status):
            raise HostError(f"head heartbeat from {pid_file} has a mismatching launch identity")
        if status.get("known") and status.get("alive"):
            raise HostError(f"head process from {pid_file} is still running after stop")
        if status.get("known"):
            _clear_head_heartbeat(pid_file)

    def _signal_head(
        self,
        pid_file: str,
        signal_number: int,
        *,
        run: Any = None,
        role: str = "",
        task: str = "",
        leaf: str = "",
        expected: dict[str, str] | None = None,
    ) -> None:
        status = self._head_status(
            pid_file, run=run, role=role, task=task, leaf=leaf, expected=expected,
        )
        if not _heartbeat_is_live_match(status):
            return
        pid = int(status["pid"])
        try:
            # The terminal gives an interactive head its own foreground process group, so this
            # reaches helpers as well as the provider process without detaching the head from its
            # controlling terminal. Old launches and focused tests can share our group; do not
            # turn those compatibility paths into a signal to the dispatcher itself.
            group = os.getpgid(pid)
            if group != os.getpgrp():
                os.killpg(group, signal_number)
            else:
                os.kill(pid, signal_number)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise HostError(f"head process {pid} could not be signalled: {exc}") from None

    def _await_head_exit(
        self,
        pid_file: str,
        *,
        run: Any = None,
        role: str = "",
        task: str = "",
        leaf: str = "",
        expected: dict[str, str] | None = None,
    ) -> None:
        deadline = time.monotonic() + HEAD_STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            status = self._head_status(
                pid_file, run=run, role=role, task=task, leaf=leaf, expected=expected,
            )
            if _heartbeat_is_mismatch(status):
                return
            if not status.get("known") or not status.get("alive"):
                return
            time.sleep(HEAD_STOP_POLL_SECONDS)

    def freeze_worker(self, record: DispatcherRecord) -> None:
        """Shut this card's worker head down and confirm it. Raises when it cannot be confirmed."""
        self._freeze_worker(record)

    def teardown(self, record: DispatcherRecord) -> None:
        """Done-path cleanup after a green merge: stop the worktree's terminals (killing
        the worker and reviewer heads plus their child shells and subagents via the PTY
        tree) and remove the worktree from Orca and git. Never used on rework, which
        reuses the workspace."""
        if self.mode == "noop" or not record.workspace:
            return
        self.stop(record)
        try:
            self._run_json(["orca", "worktree", "rm", "--worktree", f"path:{record.workspace}", "--force", "--json"])
        except HostError:
            pass

    def _create_workspace(
        self, project: str, worker_id: str, base: str, *, expected: str = ""
    ) -> str:
        """Cut the card's worktree and accept it only as this card's workspace of this repo.

        What Orca returns is checked against what Orca itself registered — the repo registration this
        project's binding resolves to, and this card's worker id as the worktree name — and against the
        `expected` path the launch intent already wrote to disk, so a workspace that landed anywhere
        else is a deferred bring-up rather than a checkout no later stop or teardown can find. A create
        that succeeded and then failed any of this is removed before the failure is raised.
        """
        if self.mode == "noop":
            workspace = self.data_dir / "dispatcher" / "workspaces" / worker_id
            workspace.mkdir(parents=True, exist_ok=True)
            return str(workspace)
        binding = self.catalog.binding(project)
        repo = Path(str(binding["repo"])).expanduser()
        if not repo.is_absolute() or not repo.is_dir():
            raise HostError(f"project repo for {project!r} is unavailable")
        registration = self._orca_repo(project)
        self._run(["git", "-C", str(repo), "fetch", "origin", base], "git fetch")
        result = self._run_json([
            "orca", "worktree", "create",
            "--repo", f"path:{repo}",
            "--name", worker_id,
            "--base-branch", f"origin/{base}",
            "--setup", "skip",
            "--no-parent",
            "--activate",
            "--json",
        ])
        worktree = result.get("worktree") if isinstance(result.get("worktree"), dict) else result
        path = worktree.get("path") if isinstance(worktree, dict) else None
        if not isinstance(path, str) or not path:
            raise HostError("orca did not return a workspace path")
        reason = self._workspace_rejection(path, str(registration["id"]), worker_id, expected)
        if reason:
            raise HostError(f"{reason}{self._discard_workspace(path)}")
        return path

    def _workspace_rejection(
        self, path: str, repo_id: str, worker_id: str, expected: str
    ) -> str:
        """Why this returned worktree may not be adopted, or "" when it may.

        A worktree Orca will not describe is a rejection rather than a pass.
        """
        try:
            shown = self._run_json(
                ["orca", "worktree", "show", "--worktree", f"path:{path}", "--json"]
            )
        except HostError as exc:
            return f"orca will not describe the worker workspace at {path}: {exc}"
        worktree = shown.get("worktree") if isinstance(shown.get("worktree"), dict) else shown
        if not isinstance(worktree, dict):
            return f"orca did not describe the worker workspace at {path}"
        if worktree.get("repoId") != repo_id:
            return (
                f"orca registered the worker workspace at {path} under repo "
                f"{worktree.get('repoId')!r}, not this project's repo {repo_id!r}"
            )
        if worktree.get("displayName") != worker_id:
            return (
                f"orca registered the worker workspace at {path} as "
                f"{worktree.get('displayName')!r}, not this card's workspace {worker_id!r}"
            )
        if expected and not _same_repo(Path(path), Path(expected)):
            return f"orca placed the worker workspace at {path}, not {expected}"
        return ""

    def _discard_workspace(self, path: str) -> str:
        """Remove a worktree that was created but must not be adopted, and say what is left."""
        try:
            self._run_json(
                ["orca", "worktree", "rm", "--worktree", f"path:{path}", "--force", "--json"]
            )
        except HostError as exc:
            return f"; the rejected worktree at {path} could not be removed either: {exc}"
        return ""

    def _validate_resumable_workspace(self, task: dict[str, Any], workspace: str) -> None:
        """Accept only the registered project worktree on this card's worker branch."""
        if self.mode == "noop":
            return
        path = Path(workspace)
        if not path.is_dir():
            raise HostError("resume workspace is not a directory")
        try:
            top_level = self._run(
                ["git", "-C", workspace, "rev-parse", "--show-toplevel"], "resume workspace git check"
            ).stdout.strip()
        except HostError as exc:
            raise HostError("resume workspace is not a git worktree") from exc
        if not _same_repo(Path(top_level), path):
            raise HostError("resume workspace git root does not match its expected path")
        branch = self._run(
            ["git", "-C", workspace, "branch", "--show-current"], "resume workspace branch check"
        ).stdout.strip()
        expected_branch = _legacy_worker_branch(task["ref"])
        if branch != expected_branch:
            raise HostError(
                f"resume workspace is on branch {branch or '(detached)'}, expected {expected_branch}"
            )
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        if not repo.is_dir():
            raise HostError("resume project repo is unavailable")
        listing = self._run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"], "resume workspace ownership check"
        ).stdout
        registered = {
            line.removeprefix("worktree ").strip()
            for line in listing.splitlines()
            if line.startswith("worktree ")
        }
        if not any(_same_repo(Path(candidate), path) for candidate in registered):
            raise HostError("resume workspace is not a registered worktree of the project repo")

    def _run_setup(self, project: str, workspace: str) -> None:
        if self.mode == "noop":
            return
        adapter = self.catalog.adapter(project)
        for command in adapter.get("setup", {}).get("commands", []):
            self._run_shell(str(command), Path(workspace), "setup command")
        smoke = adapter.get("smoke", {}).get("command")
        if smoke:
            self._run_shell(str(smoke), Path(workspace), "smoke command")

    def _launch(
        self,
        workspace: str,
        title: str,
        head: str,
        prompt_file: str,
        *,
        role: str,
        env_name: str,
        launch_prompt: str | None = None,
        prompt_document: str = "",
        split_from: str = "",
        task: dict[str, Any] | None = None,
        failover: bool = False,
        heartbeat_run_id: str = "",
    ) -> LaunchedHead:
        """Bring one head up and hand back the pane together with the configuration it started with.
        """
        pid_file = _pid_file_path(_watchdog_kind(role), task["ref"]) if task else ""
        task_ref = self._task_ref(task, role, prompt_document)
        run_id = heartbeat_run_id or head_ops.new_run_id()
        # ``preflight_codex_run`` is deliberately reached even by noop.  A fake/noop transport is
        # not an exemption from the policy boundary: it is how tests prove a refused attestation
        # opens no pane and clears no predecessor state.
        if self.mode == "noop":
            try:
                preflight_run = self.preflight_codex_run(
                    head,
                    role=role,
                    workspace=workspace,
                    task_ref=task_ref,
                    pid_file=pid_file,
                    run_id=run_id,
                )
            except CodexFanoutPolicyError as exc:
                raise HostError(str(exc)) from None
            return self._launched(
                f"noop:{head}:{Path(workspace).name}:{Path(prompt_file).name}", head, task, role,
                workspace, failover, head_run=preflight_run.to_json(),
            )
        heartbeat = heartbeat_identity(
            run_id=run_id, role=role, task_ref=task_ref.to_json(),
        )
        if pid_file:
            # Drop any pid a previous launch in this same workspace left behind, so a respawn
            # cannot read a dead predecessor's pid as this launch's liveness signal before the new
            # head has had a chance to overwrite it (secretary-751).
            _clear_head_heartbeat(pid_file)
        command = os.environ.get(env_name)
        launch = HeadCommand(command) if command else None
        if command:
            # A raw command override bypasses the catalog launcher entirely, so it never gets the
            # pid heartbeat wrapper below. That is deliberate: this path exists for tests and
            # manual overrides, not the runtimes the watchdog needs to trust, and it keeps the long
            # inactivity ceiling as its fallback (documented in docs/OPERATIONS.md).
            self.catalog.prepare_head_workspace(head, workspace, role=role)
        else:
            launch = self.catalog.head_launch(
                head,
                prompt_file,
                workspace=workspace,
                role=role,
                launch_prompt=launch_prompt,
            )
            command = launch.command
            if pid_file:
                command = _with_pid_heartbeat(command, pid_file, identity=heartbeat)
        adapter = (getattr(launch, "adapter", "") or "codex") if launch else "codex"
        try:
            preflight_run = self.preflight_codex_run(
                head,
                role=role,
                workspace=workspace,
                task_ref=task_ref,
                pid_file=pid_file,
                run_id=run_id,
            )
        except CodexFanoutPolicyError as exc:
            raise HostError(str(exc)) from None
        ingress = self._codex_provider_ingress(preflight_run)
        subject = f"{role or 'head'}-launch"
        pointer = None
        if launch and launch.prompt_after_start:
            # Which of the two prompt shapes this head is in is decided by the rendered command,
            # not by the profile: a raw command override runs a provider in a shape no profile
            # describes. A caller that has already written its task passes the document, and the
            # pointer is then the bounded line naming it.
            # An empty pointer text is the legacy shape and not an empty prompt: a caller that
            # passes no launch prompt is one whose head is sent the prompt file's own contents,
            # which the delivery below reads. A caller that has written a task document passes the
            # bounded line naming it, and that line is the whole payload.
            pointer = head_ops.NudgePointer(text=launch_prompt or "", document=prompt_document)
        try:
            outcome = head_ops.spawn(
                self._head_spec(head, adapter),
                workspace,
                task_ref,
                host=self.session,
                command=command,
                title=title,
                pointer=pointer,
                pid_file=pid_file,
                split_from=split_from,
                transport=self._head_transport(
                    workspace, prompt_file, adapter, role,
                    before_send=ingress.bind_before_delivery if ingress is not None else None,
                ),
                subject=subject,
                run_id=run_id,
                role=role,
                run=preflight_run,
                commit=ingress.commit_run if ingress is not None else None,
            )
        except head_ops.HeadOperationError as exc:
            failed_run = getattr(exc, "run", None)
            if pid_file and failed_run is not None and failed_run.leaf:
                # Delivery can refuse with a live pane before ``spawn`` returns normally.  Bind
                # that pane to the already-written exact heartbeat before persisting the failed
                # launch intent, so recovery does not mistake our own head for a foreign one.
                _bind_head_heartbeat(pid_file, expected=heartbeat, leaf=failed_run.leaf)
            raise self._launch_failure(exc, workspace, pid_file, subject) from None
        if pid_file:
            # Pane create gives us the leaf after the head has written its base identity.  A best
            # effort bind is enough for a head still starting; the reader still requires the run,
            # role and task binding and treats a subsequently declared wrong leaf as a mismatch.
            _bind_head_heartbeat(pid_file, expected=heartbeat, leaf=outcome.run.leaf)
        delivery = outcome.delivery
        return self._launched(
            outcome.run.handle,
            head,
            task,
            role,
            workspace,
            failover,
            leaf=outcome.run.leaf,
            delivery_evidence=(
                _delivery_evidence_json(delivery, subject) if delivery is not None else {}
            ),
            head_run=outcome.run.to_json(),
        )

    def _head_spec(self, head: str, adapter: str) -> HeadSpec:
        """The launch shape the run is recorded with, degrading rather than failing a live bring-up.
        """
        try:
            return HeadSpec.from_profile(head, self.catalog.head_profile(head))
        except (HeadSpecError, HostError, AttributeError, KeyError, TypeError):
            return HeadSpec(profile_id=head, adapter=adapter or "unknown")

    @staticmethod
    def _task_ref(task: dict[str, Any] | None, role: str, document: str) -> head_ops.TaskRef:
        """What this head is being pointed at."""
        pointer = document if document and os.path.isabs(document) else ""
        if task and task.get("ref"):
            return head_ops.TaskRef.card(str(task["ref"]), document=pointer)
        return head_ops.TaskRef.standing(role or "head", document=pointer)

    def _head_transport(
        self,
        workspace: str,
        prompt_file: str = "",
        adapter: str = "",
        role: str = "",
        before_send: Callable[[], head_ops.HeadRun | None] | None = None,
    ) -> "DispatcherHeadTransport":
        """This product's delivery and close semantics, for the operation to perform through
        the host it is running on.
        """
        return DispatcherHeadTransport(self, workspace, prompt_file, adapter, role, before_send)

    @staticmethod
    def _run_heartbeat_identity(run: head_ops.HeadRun, role: str) -> dict[str, str]:
        return heartbeat_identity(
            run_id=run.run_id, role=role, task_ref=run.task_ref.to_json(), leaf=run.leaf,
        )

    def _record_heartbeat_status(self, record: DispatcherRecord, kind: str) -> dict[str, Any]:
        field = "review_head_run" if kind == "review" else "worker_head_run"
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        return self._head_status(
            pid_file, run=getattr(record, field, {}), role=kind, leaf=leaf,
        )

    def _launch_failure(
        self, exc: head_ops.HeadOperationError, workspace: str, pid_file: str, subject: str
    ) -> Exception:
        """Translate one operation's refusal into the failure the dispatcher's callers already read.
        """
        evidence = _delivery_evidence_json(exc, subject)
        if isinstance(exc, head_ops.HeadSpawnAborted):
            return HeadLaunchAborted(
                str(exc),
                handle=exc.run.handle,
                leaf=exc.run.leaf,
                workspace=workspace or exc.run.workspace,
                pid_file=pid_file or exc.run.pid_file,
                evidence=evidence,
                head_run=exc.run.to_json(),
            )
        if isinstance(exc, head_ops.HeadPaneBusy):
            return HeadPaneNotReady(
                f"the head pane was {_pane_state_label(exc.readiness)} and never took its launch "
                f"prompt: {exc}",
                readiness=exc.readiness,
                pane=exc.pane,
                evidence=evidence,
            )
        failure = HostError(str(exc))
        failure.evidence = evidence
        return failure

    def _close_head_pane(
        self,
        handle: str,
        pid_file: str,
        *,
        run: Any = None,
        role: str = "",
        task: str = "",
        leaf: str = "",
        expected: dict[str, str] | None = None,
        host: SessionHost,
    ) -> None:
        """Close a head's pane through the session host and confirm nothing of it survived.

        The heartbeat decides, not the close: Orca answers `tab_not_found` for a pane it never gave a
        UI tab, which is every pane a dispatcher-launched head gets on a headless serve. Only when
        there is no heartbeat to read is a refused close taken at face value.
        """
        if run is not None:
            self._guard_head_run(run, role, pid_file=pid_file, task=task, leaf=leaf)
        elif pid_file:
            status = self._head_status(pid_file, expected=expected)
            if _heartbeat_is_mismatch(status):
                raise HostError(f"head heartbeat from {pid_file} has a mismatching launch identity")
        try:
            host.close_pane(handle)
        except Exception as exc:  # noqa: BLE001 — any refusal, whatever the transport called it
            status = (
                self._head_status(
                    pid_file, run=run, role=role, task=task, leaf=leaf, expected=expected,
                ) if pid_file else {"known": False}
            )
            if not status.get("known"):
                raise HostError(f"head terminal close failed: {exc}") from None
            if _heartbeat_is_mismatch(status):
                raise HostError("head terminal close found a mismatching launch identity") from None
        self._confirm_head_process_gone(
            pid_file, run=run, role=role, task=task, leaf=leaf, expected=expected,
        )

    def _launched(
        self, handle: str, head: str, task: dict[str, Any] | None, role: str, workspace: str = "",
        failover: bool = False, leaf: str = "", delivery_evidence: dict[str, Any] | None = None,
        head_run: dict[str, Any] | None = None,
    ) -> LaunchedHead:
        """Pair the pane with the launch snapshot of the head running in it."""
        if task is None:
            return LaunchedHead(
                handle=handle, head=head, leaf=leaf,
                delivery_evidence=dict(delivery_evidence or {}),
                head_run=dict(head_run or {}),
            )
        try:
            run = self.catalog.head_run(
                task, role=role, head=head, workspace=workspace, failover=failover
            ).to_json()
        except (HostError, AttributeError, KeyError, TypeError):
            run = HeadRun(
                role=role, head=head, adapter="unknown", model_source=MODEL_UNKNOWN
            ).to_json()
        return LaunchedHead(
            handle=handle,
            head=head,
            run=run,
            leaf=leaf,
            delivery_evidence=dict(delivery_evidence or {}),
            head_run=dict(head_run or {}),
        )

    @property
    def session(self) -> OrcaSessionHost:
        """The session manager this runtime opens, addresses and closes panes through."""
        return OrcaSessionHost(self._run_json)

    def _create_terminal(self, workspace: str, title: str, command: str) -> PaneIdentity:
        try:
            return self.session.open_pane(workspace, title, command)
        except PaneHostError as exc:
            raise HostError(str(exc)) from None

    def _split_anchor(self, record: DispatcherRecord) -> str:
        """Pane to split the reviewer off. The worker's own pane when it is still connected, so
        both heads of a card end up in one tab; otherwise any live pane in the same worktree.
        Empty when the worktree has no live pane left — the caller then falls back to creating a
        terminal, which is less visible but still gets the card reviewed."""
        connected = [pane for pane in self._worktree_terminals(record.workspace) if pane.connected]
        if record.worker_leaf:
            for pane in connected:
                if pane.leaf == record.worker_leaf:
                    return pane.handle
        elif record.handle:
            for pane in connected:
                if pane.handle == record.handle:
                    return record.handle
        return connected[0].handle if connected else ""

    def _worktree_terminals(self, workspace: str) -> list[Pane]:
        """Pane inventory for a worktree, or [] when it cannot be read. Callers use it to pick
        a pane, never to decide a head is dead, so an unreadable inventory degrades into a weaker
        choice rather than a failed tick."""
        if self.mode == "noop" or not workspace:
            return []
        try:
            return self._worktree_terminals_or_raise(workspace)
        except HostError:
            return []

    def _worktree_terminals_or_raise(self, workspace: str) -> list[Pane]:
        """Read a worktree inventory when its absence would make a lifecycle decision unsafe."""
        if self.mode == "noop":
            return []
        try:
            return list(self.session.panes(workspace))
        except PaneHostError as exc:
            raise HostError(str(exc)) from None

    def _freeze_worker(self, record: DispatcherRecord) -> None:
        """Shut the worker head down now that the reviewer is up, leaving the workspace untouched.

        Nothing else stops the worker from editing the checkout mid-review. A worker adopted from a
        launch intent has no pane handle, so the stop goes by its pid heartbeat instead.
        """
        if self.mode == "noop" or not (record.handle or record.worker_leaf or record.worker_pid_file):
            return
        self.stop_head(record, "worker", STOPPED_BY_REVIEW_FREEZE)

    def retain_worker(self, record: DispatcherRecord) -> None:
        """Suspend a completed worker without throwing its provider conversation away.

        A missing or dead pid heartbeat is not safe to retain, and a head with no pane handle is not
        retained at all: the caller falls back through the confirmed-stop and durable replacement path
        rather than guessing that a pane is idle.
        """
        if self.mode == "noop":
            raise HostError("noop runtime cannot retain a worker session")
        if not record.handle:
            raise HostError("worker session has no addressable pane to retain")
        status = self._head_status(
            record.worker_pid_file,
            run=record.worker_head_run,
            role="worker",
            leaf=record.worker_leaf,
        )
        if not _heartbeat_is_live_match(status):
            raise HostError("worker session is unavailable for retention")
        try:
            self._signal_head(
                record.worker_pid_file,
                signal.SIGSTOP,
                run=record.worker_head_run,
                role="worker",
                leaf=record.worker_leaf,
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise HostError(f"worker session could not be suspended: {exc}") from None
        deadline = time.monotonic() + HEAD_STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            retained = self._head_status(
                record.worker_pid_file,
                run=record.worker_head_run,
                role="worker",
                leaf=record.worker_leaf,
            )
            if _heartbeat_is_live_match(retained) and retained.get("stopped"):
                return
            if not retained.get("alive"):
                break
            time.sleep(HEAD_STOP_POLL_SECONDS)
        raise HostError("worker session could not be confirmed suspended")

    def _continuation_addressable(self, record: DispatcherRecord) -> bool:
        """Whether this worker head is a live provider conversation a prompt can be sent into."""
        run = record.worker_run
        adapter = run.get("adapter")
        return bool(record.handle) and (
            adapter == "claude"
            or (adapter == "codex" and str(run.get("codex_mode") or CODEX_TUI_MODE) == CODEX_TUI_MODE)
        )

    def worker_retained_alive(self, record: DispatcherRecord) -> bool:
        """Whether this card's worker session is confirmably alive and still suspended."""
        if self.mode == "noop" or not record.worker_continuation.retained:
            return False
        status = self._record_heartbeat_status(record, "worker")
        return bool(_heartbeat_is_live_match(status) and status.get("stopped"))

    def confirm_worker_retained(self, record: DispatcherRecord) -> None:
        """Assert the retained worker is still suspended, or raise for the caller's stop path."""
        if self.mode == "noop":
            return
        if not self.worker_retained_alive(record):
            raise HostError("retained worker session is no longer confirmably suspended")

    def worker_retained_vanished(self, record: DispatcherRecord) -> bool:
        """Whether this card's retained worker is provably gone, so nothing is left to freeze.

        Only the pid heartbeat's definitive death signal (`known and not alive`) counts; the ambiguous
        `known: False` of a heartbeat that was never written stays on the confirm-or-freeze path.
        """
        if self.mode == "noop" or not record.worker_continuation.retained:
            return False
        status = self._record_heartbeat_status(record, "worker")
        return _heartbeat_is_dead(status)

    def worker_addressable(self, record: DispatcherRecord) -> bool:
        """Whether this card's worker is a live conversation a prompt could be typed into."""
        if self.mode == "noop":
            return False
        return self._continuation_addressable(record)

    def prompt_worker_report(self, task: dict[str, Any], record: DispatcherRecord) -> None:
        """Ask a live worker to run the open round's ordinary report command. Nothing else."""
        status = self._head_status(
            record.worker_pid_file,
            run=record.worker_head_run,
            role="worker",
            leaf=record.worker_leaf,
        )
        if not _heartbeat_is_live_match(status):
            raise HostError("worker session exited")
        if status.get("stopped"):
            raise HostError("worker session is suspended and cannot take a report prompt")
        if not self._continuation_addressable(record):
            raise HostError("worker session cannot accept a report prompt")
        workspace = Path(record.workspace)
        if not workspace.is_dir():
            raise HostError("worker workspace is missing")
        prompt = _report_nudge_prompt(record.report_generation, task["ref"])
        self._nudge_worker(
            record,
            head_ops.NudgePointer.line(prompt),
            "worker report prompt",
            subject="worker-report",
        )

    def resume_worker(self, task: dict[str, Any], record: DispatcherRecord) -> None:
        """Resume an addressable retained worker and deliver its updated rework task."""
        status = self._head_status(
            record.worker_pid_file,
            run=record.worker_head_run,
            role="worker",
            leaf=record.worker_leaf,
        )
        if not _heartbeat_is_live_match(status):
            raise HostError("retained worker session exited")
        if not self._continuation_addressable(record):
            raise HostError("retained worker session cannot accept a continuation")
        adapter = self._prompt_adapter(record.worker_run, record.head)
        workspace = Path(record.workspace)
        if not workspace.is_dir():
            raise HostError("retained worker workspace is missing")
        continuation = record.worker_continuation
        if continuation.delivery_confirmed:
            if status.get("stopped"):
                raise HostError("confirmed retained continuation is no longer running")
            return
        if not status.get("stopped") and _terminal_turn_started(
            record.handle,
            run_json=self._run_json,
            workspace=str(workspace),
            since=continuation.sent_at,
            adapter=adapter,
        ):
            # The dispatcher may have died after the provider started but before it recorded
            # that confirmation. Returning lets recovery checkpoint it before finishing, without
            # touching a TASK.md or report body the resumed worker may already be using.
            return
        base = self.catalog.default_branch(task["project"], task.get("workspace", {}).get("base_branch"))
        # One generation and one decision, read once: the document the worker is sent back to and
        # the prompt that sends it there name the same round and the same adjudication because they
        # are built from the same values, not because two call sites happen to agree.
        generation = record.report_generation
        decision = record.report_decision
        self._clear_report_bodies(task["ref"])
        self._write_prompt(
            workspace / "TASK.md",
            self._worker_task_doc(task, base, record.attempt_id, generation, decision),
        )
        # The continuation travels as a pointer at the document just written, not as the round
        # typed into the composer: that is the delivery shape the product has never lost a prompt
        # on, and the one this path was still missing (secretary-1413). The pointer is built before
        # the wake-up, because a nudge that will not fit is a continuation this path cannot make,
        # and finding that out after SIGCONT leaves a woken head with nothing to read.
        try:
            pointer = head_ops.NudgePointer.at_document(
                str(workspace / "TASK.md"), _continuation_note(generation, decision)
            )
        except PromptDocumentError as exc:
            raise HostError(f"the continuation pointer could not be built: {exc}") from None
        activate = None
        if status.get("stopped"):
            # The delivery transport waits for `tui-idle` before this callback.  In particular a
            # readiness timeout that says the pane is busy leaves the retained HeadRun frozen:
            # it does not become a SIGCONT followed by a replacement recovery.
            activate = lambda: self._signal_head(
                record.worker_pid_file,
                signal.SIGCONT,
                run=record.worker_head_run,
                role="worker",
                leaf=record.worker_leaf,
            )
        self._nudge_worker(
            record,
            pointer,
            "retained worker continuation",
            subject="worker-continuation",
            before_send=activate,
        )

    def _nudge_worker(
        self,
        record: DispatcherRecord,
        pointer: head_ops.NudgePointer,
        what: str,
        *,
        subject: str,
        before_send: Callable[[], None] | None = None,
    ) -> None:
        """Point this card's live worker at one thing, through the head operation (secretary-1412).
        """
        run = self.worker_lifecycle_run(record)
        try:
            outcome = head_ops.nudge(
                run,
                pointer,
                host=self.session,
                transport=self._head_transport(
                    record.workspace, "TASK.md",
                    self._prompt_adapter(record.worker_run, record.head),
                    before_send=before_send,
                ),
                subject=subject,
            )
        except head_ops.HeadOperationError as exc:
            failure = HostError(f"{what} was not delivered: {exc}")
            failure.evidence = getattr(exc, "evidence", None)
            raise failure from None
        except (TuiDeliveryError, HostError) as exc:
            failure = HostError(f"{what} was not delivered: {exc}")
            failure.evidence = getattr(exc, "evidence", None)
            raise failure from None
        record.worker_head_run = outcome.run.to_json()
        _record_worker_delivery_evidence(record, outcome.delivery)

    def _set_worker_branch(self, workspace: str, branch: str) -> None:
        if self.mode == "noop":
            return
        # A fresh worktree may start on the base branch, but the target name must never be
        # force-updated. In particular, a preserved checkout elsewhere can already own it.
        self._run(["git", "-C", workspace, "branch", "-m", branch], "git branch")

    def _write_prompt(self, path: Path, body: str) -> None:
        write_text_atomic(path, body)

    def _review_document(
        self, task: dict[str, Any], record: DispatcherRecord
    ) -> tuple[Path, str]:
        """This round's review task, on disk, and the one line that points a reviewer at it.

        The text never travels through the pane; only a bounded pointer does. A retry re-renders the
        same document at the same path, and a document that cannot be written stops the bring-up before
        a pane is opened. Nothing here writes to or removes anything from the candidate checkout.
        """
        document = self._prompt_document_path(REVIEW_ROLE, task["ref"], record.review_baseline)
        prompt = self._review_prompt(
            task, record.attempt_id, record.review_baseline, record=record
        )
        try:
            _write_prompt_document(document, prompt, outside=Path(record.workspace))
            nudge = _nudge_for(document)
        except PromptDocumentError as exc:
            raise HostError(f"the reviewer task document could not be prepared: {exc}") from None
        return document, nudge

    def _prompt_document_path(self, role: str, reference: str, round_number: int) -> Path:
        """Where a head's task document lives: outside every worktree, with the run's artifacts."""
        root = os.environ.get("SECRETARY_DISPATCHER_PROMPT_DIR")
        base = Path(root).expanduser() if root else self.data_dir / "artifacts" / "prompts"
        name = f"{_request_token(role)}-{_request_token(str(round_number))}.md"
        return (base / _request_token(reference) / name).resolve()

    def _clear_body_file(self, kind: str, reference: str, review_round: int) -> None:
        """Drop the body file before launching the head that is supposed to write it.

        The path is keyed on ref+round and heads are told to leave the file in place, so a respawned
        head would inherit its predecessor's body; nothing downstream rejects a stale one. A missing
        file at least fails loudly. Reports go through `_clear_report_bodies`.
        """
        try:
            Path(_body_file_path(kind, reference, review_round)).unlink(missing_ok=True)
        except OSError:
            pass

    def _clear_report_bodies(self, reference: str) -> None:
        """Drop every report body file this card has, the round about to start included.

        The rounds already over are cleared too: the task protocol answers an identical retry from its
        committed event, and a leftover body file is how a retained conversation produces one.
        """
        sample = _body_file_path("report", reference, 0)
        directory, name = os.path.split(sample)
        prefix = name[: -len("0.md")]
        try:
            entries = os.listdir(directory)
        except OSError:
            return
        for entry in entries:
            if not entry.startswith(prefix) or not entry.endswith(".md"):
                continue
            if not entry[len(prefix):-len(".md")].isdigit():
                continue
            try:
                os.unlink(os.path.join(directory, entry))
            except OSError:
                pass

    def _worker_launch_prompt(self) -> str:
        """Short pointer delivered to the worker head at launch. The full spec lives in TASK.md
        (written next to the workspace root); duplicating it into the launch prompt would ship
        the whole task twice. The head opens TASK.md itself and reports with the command there.
        """
        return (
            "The full task is in TASK.md at the workspace root. Read it first and follow it. "
            "Do not spawn, create, delegate to, or manage subagents; perform the work in this "
            "head only. Report done or blocked with the command given in TASK.md. Do not commit "
            "TASK.md."
        )

    def _worker_task_doc(
        self, task: dict[str, Any], base: str, attempt_id: str, generation: int = 0,
        decision: str = "",
    ) -> str:
        branch = _legacy_worker_branch(task["ref"])
        # The generation keeps the report request-id distinct per report round: a rework reuses the
        # same attempt_id, so without it the second done-report collides with the first and is
        # idempotently deduped, leaving the dispatcher waiting forever.
        request = _attempt_request_id(attempt_id, "worker-report-done", task["ref"], str(generation))
        # One id per classification. A worker that restates a block under the other classification
        # is filing a different report, and since secretary-1060 a request id claims its payload:
        # one shared id would answer the second call with `validation` / exit 2 instead of
        # recording it.
        blocked_requests = {
            classification: _attempt_request_id(
                attempt_id, f"worker-report-blocked-{classification}", task["ref"], str(generation)
            )
            for classification in ("external_fact", "wrong_task_definition")
        }
        body_file = _body_file_path("report", task["ref"], generation)
        sections = [
            f"# Task {task['ref']}",
            "",
            task.get("description") or "(empty task description)",
            "",
            "## No subagents",
            "",
            "Perform this task in this head only. Do not spawn, create, delegate to, or manage",
            "subagents or child agents. Use ordinary tools directly when needed.",
            "",
        ]
        decision = (decision or "").strip()
        if decision:
            # The decision is rendered above the findings it was made on, and named as the thing to
            # follow. A round opened by an observer decision that only carried the reviewer's
            # findings had workers repairing findings the observer had rejected and skipping the
            # change it asked for (secretary-1064).
            sections += [
                "## Observer rework decision to follow",
                "",
                "The observer read the review that sent this card back and decided what this round",
                "owes. That decision is the authoritative instruction for this round: follow it.",
                "Where it and the reviewer findings below disagree, the decision wins. It may",
                "accept some findings and reject others: do not change what it rejects, and do not",
                "argue the findings it accepts. If it asks for something no reviewer raised, that",
                "is part of this round too.",
                "",
                decision,
                "",
            ]
        review_red = _last_review_red_body(task)
        if review_red and decision:
            sections += [
                "## Reviewer findings, as supporting context (previous submission was RED)",
                "",
                "These are the findings the decision above was made on. They are context for it,",
                "not the instruction: a finding the decision rejects or narrows is settled by the",
                "decision, not by the wording here. Do NOT re-report the same commit unchanged:",
                "",
                review_red,
                "",
            ]
        elif review_red:
            sections += [
                "## Reviewer verdict to address (previous submission was RED)",
                "",
                "Your last commit was reviewed and rejected. Fix these findings before reporting",
                "done again — do NOT re-report the same commit unchanged:",
                "",
                review_red,
                "",
            ]
        gate_red = _last_gate_red_body(task)
        if gate_red:
            sections += [
                "## Mechanical gate failure to address (CI/local validation was RED)",
                "",
                "The mechanical gate bounced your last submission before it reached review. Fix",
                "the actual cause named below before reporting done again — do NOT re-report the",
                "same commit unchanged:",
                "",
                gate_red,
                "",
            ]
        sections += [
            "## Check-cost contract",
            "",
            "During development, run the smallest relevant checks first. Run at most one local broad",
            "suite for this report generation and unchanged SHA when it is actually useful; name any",
            "additional broad rerun and its reason in the report. A later executed local/GitHub gate is",
            "reusable downstream only if it produces a valid exact-SHA receipt. A none/noop gate or a",
            "missing receipt attests no broad suite; do not call it authoritative, and run the appropriate",
            "validation before reporting when this card's acceptance criteria require it.",
            "",
            # A worker cannot recover a summary from a scrolled pane, so the cheapest repair used
            # to be another full suite over unchanged code (secretary-1406).
            "Run that broad suite through the receipt wrapper, so its result outlives the pane:",
            "",
            "    python3 -m secretary check broad --module <this project's broad suite module>",
            "",
            "It streams the combined output while the suite runs and returns the check's own exit",
            "status, and it writes `state/checks/broad-<digest>.json` in this workspace: command and",
            "digest, cwd and imported project, start/end/duration, exit code, parsed verdict and",
            "counts where the runner prints them, and a bounded diagnostic tail. Read it back with",
            "`python3 -m secretary check show --module <the same module>` and quote its summary",
            "in the report. While that receipt is usable, you already have the answer: rerunning the",
            "broad suite only because the TUI scrolled its output away is prohibited. A changed SHA,",
            "an edit to the working tree, or a concrete red result you are fixing opens a new",
            "justified run — name which one in the report. The receipt is workspace-local and",
            "ignored by git; never commit it, and never present it as the mechanical gate's",
            "exact-SHA attestation.",
            "",
            "A receipt stands in for a run only while it describes this content and the check",
            "process imported the project from this workspace; an import resolved elsewhere is",
            "recorded truthfully and still refused. `check show` and `--reuse` answer that with",
            "one predicate, so they cannot disagree.",
            "",
            "A check that needs a shell runs as `--command '<shell>'` instead, and buys that",
            "generality by attesting less: a shell may change directory or import environment",
            "before any interpreter starts, so its receipt records no import provenance and is",
            "never reused in place of a run. Prefer `--module` for the suite you report on.",
            "",
            "## Scope of a rework",
            "",
            "Address a reviewer finding when its repair is local to this card. Use `report:blocked`",
            "instead only for an obvious wrong cut: the requested fix contradicts this card, crosses",
            "its explicit Out of scope, or requires a new durable protocol, product contract, or trust",
            "boundary. Difficulty or size alone is not a reason to stop. In a blocked report, name the",
            "conflict and the observer decision needed. Do not silently expand the supported boundary.",
            "",
            "A blocked report has to say which kind of blocker it is, and the two are repaired",
            "differently: `--classification external_fact` when the blocker is a fact outside this",
            "card that somebody has to change first, for example missing access, a broken dependency",
            "or an upstream defect; `--classification wrong_task_definition` when the card itself is",
            "wrong, for example a contradiction, a wrong cut, or scope the card cannot carry. Pick",
            "the one that matches and run that command line below; a blocked report without one is",
            "refused.",
            "",
            # A worker once ran a live sync from its workspace and published unmerged skills into
            # the homes the running agents read, silently reverting a merged safeguard.
            "Your checks read the live installation; they never write to it. Do not run a command",
            "that deploys, syncs, provisions or reconciles live state from this workspace: it would",
            "publish unmerged work into the homes the running agents read. Where a check has a",
            "candidate-scoped form, use it (for example `--product-root .`). Where it has none, say",
            "in your report what you could not verify rather than running the live-writing form.",
            "",
            "Do not change or weaken an existing test to make a failure go away. If a test really",
            "does encode behaviour this card changes, say so in the report: name the test, what it",
            "asserted, and why the new contract is the right one. A silently rewritten assertion is",
            "treated as a defect of this round.",
            "",
            "Run every check in the foreground and wait for it there. Do not put work in the",
            "background and do not write a loop that waits for it: you have nothing to wait with.",
            "A background job reports back only at the start of your next turn, so receiving it",
            "means ending this one, while any tool call keeps the turn open — including a no-op",
            "call made to pass the time. A head that spent 23 minutes alternating between `true`",
            "and announcing it would stop polling, then waited on a loop whose condition could",
            "never hold, is why this paragraph exists (secretary-1161, 2026-08-06). The full suite",
            "takes about 95 seconds in the foreground; that is cheaper than any way of waiting.",
            "",
            # In sprint:1300 two commits with AI co-author trailers were published before anything
            # looked at them. The instruction not to write them lived only in one model family's
            # home file, so it never reached a head of another family; it belongs in the packet
            # every worker gets, whatever runtime is behind it (secretary-1401).
            "Do not add AI co-authorship to your commits. No `Co-Authored-By:` trailer naming",
            "Claude, Codex, an assistant or any model or vendor, and no generated-by attribution",
            "line: the dispatcher checks every commit message on your branch before it publishes",
            "anything, and a violation bounces the card back to you as a red gate. Human",
            "co-authors are fine. If the gate does bounce one, repair the message in this checkout",
            "with `git commit --amend` or `git rebase -i` and report done again; nothing is",
            "rewritten or force-pushed for you.",
            "",
            "Before reporting done, stage AND commit everything on the worker branch: run",
            "`git add -A && git commit`, then confirm `git status --porcelain` prints nothing.",
            "The dispatcher rejects a done report while the workspace has any uncommitted changes,",
            "so a partial `git add` that misses your fix files will bounce the card.",
            "",
            "Report through the secretary task protocol only:",
            f"This document is report generation {generation}. Every request id below ends in "
            f"-{generation}, and so does the body file. A command carrying any other number belongs",
            "to a round that is over: that id already names that round's report, and its body file",
            "has been removed. Running it does not report this round. It either fails on the missing",
            "body or answers from the old round's record without writing anything to the card, and",
            "either way this round is left waiting. Copy the command from here, never from an",
            "earlier turn of this conversation.",
            *_body_file_instructions(body_file),
            f'{_CONTROL_PLANE_TASK_COMMAND} report --ref {task["ref"]} --role worker --kind done --request-id {request} --body-file {body_file}',
            f'{_CONTROL_PLANE_TASK_COMMAND} report --ref {task["ref"]} --role worker --kind blocked --classification external_fact --request-id {blocked_requests["external_fact"]} --body-file {body_file}',
            f'{_CONTROL_PLANE_TASK_COMMAND} report --ref {task["ref"]} --role worker --kind blocked --classification wrong_task_definition --request-id {blocked_requests["wrong_task_definition"]} --body-file {body_file}',
            "",
            f"Base branch: {base}",
            f"Worker branch: {branch}",
            "",
            # Last, after everything the card description or the decision itself can write into, so
            # the recovery in `_task_doc_decision` reads the dispatcher's own record and not a
            # decision-shaped string that arrived in somebody's prose. Written on every document,
            # empty body included: a round with no decision has to read back as none.
            _decision_record_line(generation, decision),
            # And the round's own ids, on the same terms and for the same reason: the report
            # commands above are prose in a document that also renders the card description, so
            # they cannot be the authority on which ids this round issued. This line can.
            _round_record_line(generation, [request, *blocked_requests.values()]),
            "",
        ]
        return "\n".join(sections)

    def _review_prompt(
        self, task: dict[str, Any], attempt_id: str, review_round: int,
        *, record: DispatcherRecord | None = None,
    ) -> str:
        # The round belongs in the key for the same reason it does in the worker report id: a card
        # that goes red twice within one attempt reuses attempt_id, so a round-less id makes the
        # second verdict a replay of the first. TaskWriter then skips the mutation, the CLI still
        # answers "verdict recorded", and the reviewer exits leaving the card waiting (secretary-654).
        green_request = _attempt_request_id(attempt_id, "review-green", task["ref"], str(review_round))
        red_request = _attempt_request_id(attempt_id, "review-red", task["ref"], str(review_round))
        body_file = _body_file_path("verdict", task["ref"], review_round)
        current_sha = self.head_commit(record) if record else ""
        attestation = _gate_attestation_for_prompt(record, current_sha)
        sections = [
            f"# Review {task['ref']}",
            "",
            task.get("description") or "(empty task description)",
            "",
            "## No subagents",
            "",
            "Perform this review in this head only. Do not spawn, create, delegate to, or manage",
            "subagents or child agents. Use ordinary tools directly when needed.",
            "",
            # One verdict carries every blocker the reviewer has. Holding some back for a later
            # round ratchets the card through extra worker attempts, and each of those costs the
            # sprint a budget event.
            "A red verdict must list every blocker you have found in this round. Prefix each with a",
            "stable `BLOCKER-<short-slug>` id so a re-review can close it without rediscovering it.",
            "Do not hold blockers back for a later round and do not widen the scope on the next one.",
            "",
            "For every RED blocker, state the concrete reachable scenario, the violated acceptance",
            "criterion or operational invariant, material assumptions, whether this branch introduced",
            "the defect or it was pre-existing, and whether the repair appears local or would change",
            "architecture, a compatibility promise, a product contract, or a trust boundary. Report",
            "evidence; do not silently widen the supported boundary or decide sprint scope.",
            "",
            # Defense in depth behind the gate's own deterministic preflight (secretary-1401): the
            # gate reads the commit messages before it publishes, and the reviewer reads them again
            # on the checkout, because a check that only ever ran in one place is a check with no
            # second opinion.
            "Read the commit messages on this branch, not only the diff. AI co-authorship is",
            "forbidden: a `Co-Authored-By:` trailer naming a model or vendor, or a generated-by",
            "attribution line, is a RED blocker. Ordinary human co-authors are not. Say what you",
            "found; do not rewrite history yourself.",
            "",
            # Two live-breaking defects once shipped under a full green suite because the fixtures
            # encoded the same wrong assumption about the backend as the code did.
            "When a change depends on how an external backend behaves, a passing fixture is not",
            "evidence: it can encode the same wrong assumption as the code under review. Say which",
            "real behaviour you verified and how. If no end-to-end check against the real backend",
            "was possible, write plainly that it was not done and which assumption stays unverified.",
            "",
            "Post exactly one review verdict through the secretary task protocol:",
            *_body_file_instructions(body_file),
            f'{_CONTROL_PLANE_TASK_COMMAND} verdict --ref {task["ref"]} --role reviewer --kind green --request-id {green_request} --body-file {body_file}',
            f'{_CONTROL_PLANE_TASK_COMMAND} verdict --ref {task["ref"]} --role reviewer --kind red --request-id {red_request} --body-file {body_file}',
            "",
        ]
        if attestation:
            sections[4:4] = [
                "## Mechanical gate attestation",
                "",
                render_receipt(attestation),
                "",
                "Independently inspect the diff, acceptance criteria and invariants. The attested broad",
                "check above already passed on this exact SHA: do not rerun that broad command or suite on",
                "the same SHA unless you record a concrete `rerun_reason`. A focused reproduction is allowed",
                "for a new blocker, an uncovered external behaviour, or a security/data-loss high-risk need.",
                "Mandatory CI and the exact-SHA pre-merge gate remain machinery-owned and are not waived.",
                "",
            ]
        else:
            sections[4:4] = [
                "## Mechanical gate evidence",
                "",
                "No valid SHA-bound mechanical-gate receipt is available. Independently inspect the diff,",
                "acceptance criteria and invariants; mandatory CI and exact-SHA pre-merge checks remain",
                "machinery-owned. Do not claim that a broad suite was attested. This includes none/noop",
                "gates: run appropriate focused or broad validation when the review needs that evidence.",
                "",
            ]
        if record and record.preferred_head:
            sections[4:4] = [
                "## Head failover",
                "",
                f"This branch was written by `{_safe_one_line(record.head)}`, not by the head this "
                f"card asks for (`{_safe_one_line(record.preferred_head)}`): that head's resource "
                "was red or spent when the card was claimed, so the claim walked the registry's "
                "fallback chain onto another family.",
                "Review the work on its merits. This is here because who wrote it is a fact you are",
                "entitled to have, not an invitation to grade the head.",
                "",
            ]
        if record and record.previous_reviewed_sha:
            sections[4:4] = [
                "## Re-review packet",
                "",
                f"previous_reviewed_sha: {_safe_one_line(record.previous_reviewed_sha)}",
                f"current_sha: {_safe_one_line(current_sha) or '(unavailable)'}",
                "Changed paths / delta from the prior review:",
                self._review_delta(record, record.previous_reviewed_sha, current_sha),
                "Previous blockers (close or explicitly retain these stable IDs):",
                _safe_one_line(record.previous_blockers, limit=2000)
                or "(legacy verdict had no structured blocker IDs)",
                "Review this delta, the closure of prior blockers and collateral impact; do not restart",
                "from the original base unless a concrete suspicion requires the historical diff.",
                "",
            ]
        return "\n".join(sections)

    def _review_delta(self, record: DispatcherRecord, previous: str, current: str) -> str:
        """A small re-review packet; failure to read it is evidence, never a broad test fallback."""
        if (
            self.mode == "noop"
            or not record.workspace
            or not _is_exact_sha(previous)
            or not _is_exact_sha(current)
        ):
            return "(delta unavailable; inspect only the necessary history)"
        try:
            paths = self.run_capture(
                ["git", "-C", record.workspace, "diff", "--name-only", f"{previous}..{current}"],
                "review delta paths",
            )
            stat = self.run_capture(
                ["git", "-C", record.workspace, "diff", "--stat", f"{previous}..{current}"],
                "review delta stat",
            )
        except (HostError, OSError, subprocess.TimeoutExpired):
            return "(delta unavailable; inspect only the necessary history)"
        if paths.returncode or stat.returncode:
            return "(delta unavailable; inspect only the necessary history)"
        names = (paths.stdout or "").strip()
        summary = (stat.stdout or "").strip()
        return "\n".join(_safe_one_line(part, limit=4000) for part in (names, summary) if part) or "(no changed paths)"

    def _run_shell(self, command: str, cwd: Path, label: str) -> None:
        self._run(["bash", "-lc", command], label, cwd=cwd)

    def _run_json(self, args: list[str]) -> dict[str, Any]:
        completed = self._run(args, _safe_command_label(args))
        try:
            loaded = json.loads(completed.stdout or "{}")
        except ValueError:
            raise HostError(f"{args[0]} returned invalid JSON") from None
        return loaded.get("result", loaded) if isinstance(loaded, dict) else {}

    def _run(self, args: list[str], label: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError(f"{label} failed: {exc}") from None
        if completed.returncode != 0:
            text = (completed.stderr or completed.stdout or "").strip()
            raise HostError(f"{label} failed: {_tail(text)}")
        return completed

    def run_capture(self, args: list[str], label: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Like _run but returns the CompletedProcess regardless of exit status (the gate reads a
        non-zero code as a red verdict, not a host failure). Still raises HostError when the process
        can't run at all."""
        try:
            return subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError(f"{label} failed: {exc}") from None


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
        # Sprint entities live on their own board, so the observer pass reads them through their
        # own reader rather than the card reader.
        instance = getattr(catalog, "instance", {})
        limits = budget_thresholds(instance if isinstance(instance, dict) else None)
        self.sprints = sprints if sprints is not None else SprintReader(
            reader.client, data_dir=Path(audit.board_dir).parent, thresholds=limits
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
        stored = (
            record.worker_head_run if role == WORKER_ROLE else record.review_head_run
        )
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
            # The head operation re-reads the heartbeat identity before it commits ``finishing``
            # or signals anything.  A mismatch is intentionally swallowed here: the block still
            # records the unknown source, while no foreign process is signalled.
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
        # A launch intent can outlive its tick.  Re-establish the exact provider source before
        # launch adoption/recovery reads a heartbeat or delivers another prompt, not afterwards
        # when an unbound/mismatched session might already have been attributed to this card.
        record = records.get(ref)
        if record is not None:
            fanout = self.poll_codex_provider_ingress(record, records, payload, reference=ref)
            if fanout is not None:
                return fanout
        # A record carrying a launch intent is a bring-up whose tick did not live to record its
        # outcome. It is settled before anything else: until it is, neither "this card has a head"
        # nor "this card is headless" is known, and acting on the wrong answer is how a card ends
        # up with two heads on one workspace.
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

    def _failover_collapse(
        self, worker: HeadChoice, review: HeadChoice
    ) -> dict[str, Any] | None:
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
        # the card's preference: a dead resource sends the walk down the canon's chain onto another
        # family. Nothing launchable at the end of either walk is a claim-skip — the card stays in
        # Ready and the outcome below names the dead resource.
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
        # The reviewer is resolved at claim too, because the claim is what writes its head onto the
        # card. A reviewer chain that is entirely dead does not stop the work: the preferred head
        # stays recorded and the reviewer's own preflight waits for its resource, exactly as before.
        review_choice = self.resolve_head(self.catalog.review_head(task))
        collapse = self._failover_collapse(worker_choice, review_choice)
        if collapse is not None:
            return dict(collapse, pilot_ref=ref)
        head = worker_choice.head
        review_head = review_choice.head or review_choice.preferred
        # A card the dispatcher still holds a record for, back in Ready with its claim already
        # committed under the current attempt, is a re-run: an operator-approved retry after
        # Blocked, or a plain preempt/requeue out of in_progress or validate. An attempt id
        # otherwise lives as long as the record does, so the claim would replay idempotently,
        # return the old event and leave the card Ready. Give every re-run a fresh identity before
        # claiming, so it claims the card for real, its worker report command cannot collide with
        # the old report, and the journal gets a second attempt instead of nothing. A committed
        # claim without a record is a genuine board divergence and still fails closed below.
        active = records.get(ref)
        requeued = active is not None
        retry_after_block = resume_workspace or any(
            self.audit.committed_event(_attempt_request_id(attempt_id, action, ref)) is not None
            for action in (
                "bringup-blocked",
                "worker-result-blocked",
                "worker-blocked",
                "worker-respawn-blocked",
                "worker-wait-stall",
                "rework-blocked",
                "gate-blocked",
                "gate-red-blocked",
                "gate-pending-stall",
                "merge-gate-blocked",
                "merge-gate-red-blocked",
                "merge-blocked",
                # The release the pre-merge re-check refused: a card blocked from Assessment
                # comes back to Ready the same way, and a re-run that kept the old attempt id
                # would replay its claim idempotently and leave the card sitting in Ready.
                "release-drift-blocked",
                "release-failed-blocked",
                "review-blocked",
                "review-freeze-red-blocked",
                "review-inventory-blocked",
                "review-wait-stall",
            )
        )
        if requeued and active is not None:
            # The preempted head can still be sitting in the workspace the next round claims. The
            # workspace is what it is stopped through, not the handle: a head adopted from a launch
            # intent is running with no handle on record, and skipping it here would leave it in
            # the checkout the new round is about to hand a second head.
            if active.owns_head("review"):
                # A preempt out of Validate leaves the worker pane already closed by
                # `start_review` but the reviewer still up. Left alone it keeps reading the same
                # checkout the new worker gets, and its verdict would land on the new attempt.
                unconfirmed = self._end_review_pane_confirmed(
                    active, records, payload, ref, step="claim", attempt_id=attempt_id,
                    initiator=STOPPED_BY_REPLACEMENT,
                )
                if unconfirmed is not None:
                    return unconfirmed
            if active.needs_settling():
                unconfirmed = self._stop_worker_confirmed(
                    active, ref, step="claim", attempt_id=attempt_id
                )
                if unconfirmed is not None:
                    return unconfirmed
        if retry_after_block or requeued:
            attempt_id = _new_attempt_id()
            _record_attempt(payload, attempt_id, ref, self.owner, self.owner)
            payload["attempt_id"] = attempt_id
        claim_request_id = _attempt_request_id(attempt_id, "claim", ref)
        worker_id = _worker_id(task)
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
        # Before the card is read back, so the comment is inside the baseline the record takes: a
        # head chosen by failover is written onto the card itself, where the reviewer and the
        # observer read it, rather than living only in this tick's stdout.
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
            # The claim opens the attempt's first report round. It is durable in the record below
            # before `prepare_worker` writes the TASK.md that names it.
            report_generation=1,
            state="claim_verified",
            claimed_at=time.time(),
            # Empty unless the walk had to leave the card's preference behind. The pair is what
            # lets the review document name the head that actually did the work without
            # re-resolving a role default that may have moved since the claim.
            preferred_head=worker_choice.preferred if worker_choice.substituted else "",
            preferred_review_head=(
                review_choice.preferred if review_choice.substituted else ""
            ),
        )
        # A re-claimed card continues its own round numbering: the journal, not the board, knows
        # how many rounds it has had, so a return to Ready adds a round instead of resetting.
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
            # This record belongs to the claim being opened now, so it has no HeadRun that can
            # prove the pre-existing file belongs to it.  A versioned heartbeat may name a real
            # process from an earlier or foreign launch; signalling its workspace here would turn
            # an absence of ownership into permission to stop it.  Keep the claim fixed and make
            # the ambiguity visible until an operator resolves the unbound process.
            return {
                "status": "degraded",
                "step": "claim",
                "action": "orphan-worker-heartbeat-unbound",
                "pilot_ref": ref,
                "attempt_id": record.attempt_id,
                "reason": "a live worker heartbeat has no durable HeadRun binding for this claim",
            }
        # The workspace is asked of the host rather than taken from its answer: `prepare_worker`
        # resolves the same path itself, and the answer is exactly what a tick that dies mid-launch
        # never sees. With it and the pid file in the record, the next tick can read the head's
        # liveness and close its terminal without ever having held its handle.
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
                # No terminal was created.  This is policy evidence, not a transient state-plane
                # failure worth retrying around: a later tick with the same schema would be the
                # same prohibited launch.  The board gets a typed, operator-readable block before
                # this record is released for a future, independently attested retry.
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
            # A launch nobody can record is exactly how a card ends up with two heads, so the host
            # is not touched at all. The card keeps its claim and the next tick launches again.
            return _launch_intent_unwritable(
                step="claim", ref=ref, attempt_id=record.attempt_id, role=WORKER_ROLE, reason=failure
            )
        # The launch intent already contains the exact preflight HeadRun.  Bind its provider
        # source before ``prepare_worker`` can create a pane; the transport will establish the
        # new session identity and cursor before it delivers TASK.md.
        self.bind_codex_provider_ingress(
            record, records, payload, role=WORKER_ROLE, reference=ref,
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
                # The head did not come up, and nothing of it is running. The card keeps its claim
                # and its record: `claim_verified` is what makes the next tick launch it again,
                # which is the whole of the retry.
                records[ref] = record
                self.save_records(payload, records)
                return deferred
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="blocked",
                reason=_bring_up_blocked_reason(
                    "dispatcher bring-up failed", exc, record, WORKER_ROLE
                ),
                request_id=_attempt_request_id(record.attempt_id, "bringup-blocked", ref),
            )
            records.pop(ref, None)
            self.save_records(payload, records)
            return {"status": "blocked", "step": "claim", "pilot_ref": ref, "reason": "host bring-up failed"}
        record.workspace = prepared["workspace"]
        _record_worker_delivery_evidence(record, prepared.get("delivery_evidence"))
        # The intent carries the pane, the launch snapshot and this head's own run before the record
        # is told anything else about it: from here on every failure is one over a worker that is
        # already running, and one that survives a tick dying carries the run it launched with.
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
        # The worker is up: record the head running it, from the launcher's own snapshot. An adopted
        # card whose claim predates this telemetry has no round yet, so record_worker_routing opens
        # one from the journal rather than leaving the round unrecorded. The intent is spent only
        # once that has landed: a journal that refuses here leaves the head adoptable, and the
        # adoption writes the routing event this round would otherwise never get.
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
            # The tick says a head was substituted, in the same line that says the card was
            # claimed. An operator reading the tick must not have to open the card to find out
            # that the work is running somewhere other than where the card asked for it.
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
        """End the reviewer before a replacement head opens. Returns the tick's outcome on refusal.
        """
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
                # workspace is still known. Sweep that workspace before opening a replacement;
                # an unnamed writer is ambiguity, never evidence that nothing is running.
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
        # The session is gone; a red transition already opened over it is not. Dropping that here
        # would leave the card In progress with nothing durable owing it a replacement.
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
        """The aborted-launch outcome when this failure may have left a worker running, else None.
        """
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

    def _settle_worker_pane(
        self, ref: str, record: DispatcherRecord, handle: str, leaf: str
    ) -> None:
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
        blocked_reason: str,
        blocked_request_id: str,
    ) -> tuple[LaunchedHead | None, dict[str, Any] | None]:
        """Relaunch this card's worker in its own workspace, under the intent already on disk."""
        ref = task["ref"]
        try:
            self._require_head_ready(record.head)
            self.bind_codex_provider_ingress(
                record, records, payload, role=WORKER_ROLE, reference=ref,
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
                # or not its head lived. Carrying it onto the record is what the intent's own
                # resolution does when a launch leaves nothing running, and the deferred relaunch
                # then belongs to the round the rework opened rather than the one it replaced.
                _keep_reserved_round(self, record, intent)
                # Nothing of this launch is running and the record still names no head, so the next
                # tick reads the card as one whose worker pane is missing and brings it up again.
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
                reason=blocked_reason,
                request_id=blocked_request_id,
                error=exc,
            )
        # The head is up. Its pane, its launch configuration and its own run go into the intent
        # before anything else is attempted with them, so an adoption gets the run that actually
        # launched rather than a fresh identity for the same process.
        _confirm_launch_intent(
            self, payload, records, ref, record,
            handle=launched.handle, leaf=launched.leaf, run=launched.run,
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
                # A lost dispatcher record can recover the live worker only from that worker's
                # own launch identity.  It is already bound and re-checked by `_adopt`, so this
                # is a continuation of its HeadRun, not a fresh claim over an unknown process.
                # Continue into the ordinary report path: a report written before the dispatcher
                # lost its record still belongs to this run and must advance the card on this
                # reconciliation rather than wait for another tick.
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
            # An open red transition outranks everything else this card could be doing. The board
            # move may or may not have committed before the tick that opened it died, so the
            # transition is finished against the board as it is now, ahead of any report marker.
            return self._complete_red_transition(record, records, payload, attempt_id, ref=ref)
        if record.state == "claim_verified":
            return self._launch_worker_after_claim(task, record, records, payload)
        # The round the dispatcher is holding, not merely the last report marker on the card: a
        # marker is attributed to a round through the request id its command carried, which is what
        # the audit keeps (secretary-1063).
        marker = _round_report_marker(
            self.audit,
            ref,
            _round_report_ids(record.workspace, record.attempt_id or attempt_id, ref, record.report_generation),
        )
        continuation = record.worker_continuation
        if continuation.delivery_pending:
            if marker in {"report:done", "report:blocked"}:
                # A report after the resume phase opened proves the continuation reached the
                # retained conversation. Do not rewrite TASK.md or replay the prompt over a
                # completed turn after recovering from a crash before its delivery checkpoint.
                continuation.confirm_delivery()
                records[ref] = record
                self.save_records(payload, records)
                return self._finish_retained_worker_resume(
                    task, record, records, payload, attempt_id,
                    phase=continuation.phase or "gate",
                )
            # Progress is sampled on every retained-continuation retry before its persisted
            # readiness backoff is interpreted.  A new provider cursor is stronger than a busy
            # pane and resets only that no-progress ladder, never this HeadRun or its claim.
            now = time.time()
            provider_observation = self._observe_retained_continuation_progress(
                task, record, now=now
            )
            blocked = self._block_unadmitted_continuation_liveness(
                task, record, records, payload, attempt_id,
                phase=continuation.phase or "gate", observation=provider_observation,
            )
            if blocked is not None:
                return blocked
            fresh_provider_progress = provider_observation == "progressed"
            pending = self._continuation_recovery_window(
                task, record, records, payload, attempt_id,
                phase=continuation.phase or "gate",
                fresh_provider_progress=fresh_provider_progress, now=now,
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
                    ref, record, attempt_id, continuation.phase or "gate",
                    delay=BUSY_RETRY_INITIAL_SECONDS,
                )
            if not continuation.busy_retry_due(time.time()):
                return _retained_worker_busy_deferred(
                    ref, record, attempt_id, continuation.phase or "gate"
                )
            # Nothing is woken from here. The suspension this delivery was opened over is a fact
            # of the tick that died: terminal recovery or an operator may have resumed that head
            # since, and re-entering the transition is what asks the heartbeat again before the
            # boundary is reopened.
            return self._deliver_red_continuation(
                task, record, records, payload, attempt_id, phase=continuation.phase or "gate"
            )
        if continuation.delivery_confirmed:
            # The delivery was checkpointed and the tick died before the round it opened was
            # recorded. Finishing it again is what makes that checkpoint worth writing: the rework
            # otherwise runs attributed to the round the verdict closed, with no reuse on the card.
            return self._finish_retained_worker_resume(
                task, record, records, payload, attempt_id, phase=continuation.phase or "gate"
            )
        if marker == "report:done":
            if continuation.validation_move_pending:
                # The process was frozen and recorded before a tick died between retention and
                # the board move. Replaying this idempotent move never wakes the worker.
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
                return {"status": "ok", "step": "advance", "pilot_ref": ref, "attempt_id": attempt_id, "to": "validate"}
            try:
                self.host.verify_worker_result(task, record)
            except HostError as exc:
                unconfirmed = self._stop_worker_confirmed(
                    record, ref, step="advance", attempt_id=attempt_id
                )
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
            if current_sha and current_sha == record.rejected_sha:
                return self._reject_stale_done(task, record, records, payload, attempt_id, current_sha)
            record.rejected_done_reports = 0
            record.review_baseline = len(task.get("comments") or [])
            # Freeze before moving the board. The persisted state is the crash boundary: a later
            # tick may finish the idempotent move, but it never leaves a completed worker writing
            # while CI or a reviewer owns this checkout.
            try:
                self.host.retain_worker(record)
                continuation.begin_retention(time.time())
            except HostError:
                # A worker with no reusable conversation is still made safe. A red gate will use
                # the normal replacement path, and an unconfirmed stop keeps this tick inert.
                unconfirmed = self._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
                if unconfirmed is not None:
                    return unconfirmed
            # Fresh code state: the mechanical gate must re-run before this report reaches review.
            record.gate_state = ""
            record.gate_pending_since = 0.0
            record.gate_transport_failures = 0
            record.gate_transport_error = ""
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
                # crash between retention and the move carry one id, whatever the card's comment
                # count has done since.
                request_id=_attempt_request_id(
                    record.attempt_id or attempt_id, "worker-done", ref, str(record.report_generation)
                ),
            )
            if continuation.validation_move_pending:
                continuation.confirm_validation_move()
            record.state = "validate"
            self.save_records(payload, records)
            return {"status": "ok", "step": "advance", "pilot_ref": ref, "attempt_id": attempt_id, "to": "validate"}
        if marker == "report:blocked":
            unconfirmed = self._stop_worker_confirmed(
                record, ref, step="advance", attempt_id=attempt_id
            )
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
            return {"status": "ok", "step": "advance", "pilot_ref": ref, "attempt_id": attempt_id, "to": "blocked"}
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
            unconfirmed = self._stop_worker_confirmed(
                record, ref, step="advance", attempt_id=attempt_id
            )
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
                    record.attempt_id or attempt_id, "stale-done-blocked", ref, str(record.rejected_done_reports)
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

        # The rework worker opens in this same checkout, so the head that reported the stale done
        # has to be confirmed gone first. A refusal ends the tick before the comment and before the
        # relaunch: the next tick retries the stop on an unchanged record.
        unconfirmed = self._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
        if unconfirmed is not None:
            return unconfirmed
        # Counted only once the bounce actually happens: a tick that stopped at the refusal above
        # rejected nothing, and charging it would block the card on the retry of the same report.
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
        record.review_baseline = record.comment_baseline
        # The bounce restarts this same attempt with a new TASK.md, so it is a new report round.
        # Without a new generation the next legitimate done report would be deduped against the
        # stale one just rejected. The attempt's routing round does not move here, because the card
        # never left the worker, which is why this generation cannot be `attempt_round`.
        record.report_generation += 1
        # Nobody adjudicated this round: it was opened by the bounce, not by an observer. The
        # decision that opened the previous one goes with it, or the document would hand a worker
        # an instruction about a review this bounce has nothing to do with (secretary-1064).
        record.report_decision = ""
        _reset_wait(record, "worker")
        _reset_wait(record, "review")
        moved = self.reader.show(ref)
        failure = self._worker_relaunch_intent(
            payload, records, ref, record, action="stale-done-rework"
        )
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
            blocked_reason="stale-result rework bring-up failed",
            blocked_request_id=_attempt_request_id(
                record.attempt_id or attempt_id, "stale-done-rework-blocked", ref
            ),
        )
        if launched is None:
            assert failed is not None
            return failed
        _record_worker_delivery_evidence(record, launched.delivery_evidence)
        record.state = "claimed"
        # A rejected done report earns no verdict, so this stays the same round: the relaunch is
        # recorded and dedupes unless the registry moved under it.
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
            # The park's board move did not commit, or its tick died before the checkpoint. The
            # card is still in Validate with the verdict already recorded; finishing the park
            # comes before the gate is read again and before any review marker, for the same
            # reason the red transition does: a verdict this card already owes is not re-decided.
            return self._complete_park(record, records, payload, attempt_id, ref=ref)
        if record.worker_continuation.red_transition_pending:
            # A red transition whose board move did not commit leaves the card in Validate with the
            # verdict already recorded. Finishing it comes before the gate is read again, before any
            # review marker and before a reviewer is started: a rollup that has turned green since
            # cannot retract a red round this card is already owed.
            return self._complete_red_transition(record, records, payload, attempt_id, ref=ref)
        marker = _last_marker(task, record.review_baseline, {"review:green", "review:red"})
        if marker == "review:green":
            return self._park_green_verdict(task, record, records, payload, attempt_id)
        if marker == "review:red":
            # Only the reviewer's lifecycle ends here. A full `stop` would take the worktree's
            # terminals down wholesale, and the same checkout is about to be parked for the
            # observer, and it is never re-created from base. A stop the host will not confirm ends
            # the tick before the card is moved: nothing else may run in this checkout while a
            # reviewer may still be alive in it, parked or not.
            # Read before the pane goes: ending the reviewer forgets the commit it judged, and
            # the park has to keep it. A parked card is still the round's code, and what a later
            # release may land is that commit and nothing else.
            reviewed = record.review_commit or self.host.head_commit(record)
            unconfirmed = self._end_review_pane_confirmed(
                record, records, payload, ref, step="review", attempt_id=attempt_id,
                initiator=STOPPED_BY_REVIEW_VERDICT,
            )
            if unconfirmed is not None:
                return unconfirmed
            record.rejected_sha = reviewed
            record.rejected_done_reports = 0
            # This is the only point at which both the last review body and the SHA it judged are
            # still available. Keep them for the next review packet, rather than asking the next
            # reviewer to reconstruct the whole card from its original base.
            record.previous_reviewed_sha = reviewed
            record.previous_blockers = _safe_one_line(_last_review_red_body(task) or "", limit=2000)
            if not self._parks_for_decision(task):
                # No observer to release it: the verdict acts on its own tick, as it did before
                # Assessment existed. The worker of this round stayed suspended through the gate
                # and the review, so the verdict goes to the conversation that wrote the code.
                # Except at the ceiling: a card nobody watches has to stop asking for another
                # round at some point, and this verdict is the one that decides which it is.
                reds = _red_review_count(task)
                if reds >= RED_REVIEW_CEILING:
                    return self._block_red_review_ceiling(
                        task, record, records, payload, attempt_id, reds=reds
                    )
                return self._begin_red_transition(
                    task, record, records, payload, attempt_id, phase="review",
                    move_reason="review:red", verdict_outcome="red",
                )
            # The worker of this round stays suspended through the park: the observer may send
            # the findings back to the conversation that wrote the code, and that conversation is
            # only worth keeping if nothing else touches the checkout while the card waits.
            self._record_verdict_routing(ref, record, "red")
            return self._begin_park(
                task, record, records, payload, attempt_id, verdict_outcome="red",
                reviewed_commit=reviewed,
                move_reason=(
                    "review:red. The card is parked in Assessment: the reviewer is stopped and "
                    "the worker of this round is held, waiting for a release, rework or reslice "
                    "decision."
                ),
            )
        # Mechanical gate (secretary-633): a fresh report clears the cheap CI/local gate before the
        # expensive reviewer is spawned. A review already in flight (state review_starting/reviewing)
        # cleared the gate when it launched, so re-running it here would be wasted host I/O.
        if record.state not in ("review_starting", "reviewing") and record.gate_state != "green":
            gated = self._run_gate(task, record, records, payload, attempt_id)
            if gated is not None:
                return gated
        if record.state == "review_starting":
            return _recover_review_launch(self, task, records, record, attempt_id, payload=payload)
        if record.state != "reviewing":
            if record.worker_continuation.retained and not self.host.worker_retained_alive(record):
                # The record remembers a suspended worker the host cannot confirm is still frozen.
                # Ambiguous liveness is never permission to leave it beside the reviewer, so a
                # confirmed stop runs before the reviewer launch intent is written and the round
                # loses its continuation.
                unconfirmed = self._stop_worker_confirmed(
                    record, ref, step="review", attempt_id=attempt_id
                )
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
            return {"status": "ok", "step": "review" if kind == "review" else "advance", "pilot_ref": task["ref"], "attempt_id": attempt_id, "action": f"{kind}-paused"}
        runtime_reason = ""
        try:
            status = self.host.review_status(task, record) if kind == "review" else self.host.worker_status(task, record)
        except Exception as exc:
            # Orca may be down or between reconnects.  It is not evidence that this particular
            # head died, so do not restart it merely for that.  It also cannot prove progress,
            # so retain the ordinary wait ceiling as the fallback.
            status = {"known": False, "live": True, "reason": "runtime-unavailable"}
            runtime_reason = scrub_host_output(str(exc))

        def unavailable() -> dict[str, Any]:
            return {
                "status": "degraded", "step": "review" if kind == "review" else "advance",
                "pilot_ref": task["ref"], "attempt_id": attempt_id,
                "action": f"{kind}-runtime-unavailable", "reason": runtime_reason,
            }
        if status.get("identity_mismatch"):
            return {
                "status": "degraded",
                "step": "review" if kind == "review" else "advance",
                "pilot_ref": task["ref"],
                "attempt_id": attempt_id,
                "action": f"{kind}-heartbeat-identity-mismatch",
                "reason": "the heartbeat names a live process with a mismatching launch identity",
            }
        if not status.get("live"):
            return self._trigger_wait_watchdog(
                task, record, records, payload, attempt_id, kind=kind,
                trigger=f"terminal {status.get('reason') or 'missing'}",
            )
        activity = status.get("last_activity")
        progress_at = float(getattr(record, f"{kind}_progress_at") or 0.0)
        if activity:
            updated = max(progress_at, float(activity))
            if updated != progress_at:
                progress_at = updated
                setattr(record, f"{kind}_progress_at", progress_at)
                self.save_records(payload, records)
        now = time.time()
        pid_confirmed = bool(status.get("pid_confirmed"))
        if pid_confirmed and "idle" in status:
            # The pid heartbeat proves this exact head process is still running, and the pane says
            # whether it is doing anything. Between them they answer better than any clock, so the
            # timing ceilings below do not apply here: a head that is working waits as long as it
            # needs, and one that has stopped without delivering ends the wait now rather than at a
            # ceiling.
            idle, fence_moved = _idle_outcome(record, status, kind=kind, now=now)
            if fence_moved:
                self.save_records(payload, records)
            if idle != "wait":
                expectation = _wait_expectation(kind)
                if kind == "worker":
                    expectation = f"{expectation} for generation {record.report_generation}"
                state = "held in a dialog" if status.get("idle_reason") == "dialog" else "idle"
                idle_since = float(getattr(record, f"{kind}_idle_since") or now)
                idle_trigger = (
                    f"the head has been {state} for {int(now - idle_since)}s with no {expectation}"
                )
                # Stopping a live pane needs evidence separated in time.  Do not issue a second
                # status probe inside this tick: a turn can start between two microsecond-adjacent
                # probes, while the next ordinary dispatcher tick observes that transition and
                # clears the episode before anything destructive happens.
                if idle == "pending":
                    return {
                        "status": "degraded", "step": "review" if kind == "review" else "advance",
                        "pilot_ref": task["ref"], "attempt_id": attempt_id,
                        "action": f"{kind}-idle-unconfirmed", "reason": idle_trigger,
                    }
                if kind == "worker":
                    # The confirmed-idle boundary, and the last point before something destructive
                    # happens to a head that may simply have finished without reporting. The round
                    # spends one prompt here; everything below is what it has always been, and is
                    # what this returns to the moment that prompt is spent, refused or impossible.
                    # A prompt that was attempted and failed travels on in the trigger, so the
                    # respawn or Blocked that follows says so rather than reporting only silence.
                    prompted, idle_trigger = self._prompt_worker_report(
                        task, record, records, payload, attempt_id, trigger=idle_trigger
                    )
                    if prompted is not None:
                        return prompted
                # Degraded, not ok. A head that stopped without delivering is the pipeline failing
                # to make progress on a card, and the operator learns about it from the tick's own
                # health: an `ok` bounce would write healthy telemetry over the one signal that
                # says this card needs looking at before it reaches Blocked.
                return self._trigger_wait_watchdog(
                    task, record, records, payload, attempt_id, kind=kind,
                    trigger=idle_trigger, stall=_idle_stall_seconds(), degraded=True,
                )
            return unavailable() if runtime_reason else None
        # Either no heartbeat, or a heartbeat with nothing that can say what the head is doing: an
        # adopted head whose pane identity was never persisted, a pane binding Orca has lost, a
        # probe it refuses. A live pid is not on its own a reason to wait forever, so those fall
        # back to the ordinary ceilings, the same fallback a runtime with no signals at all gets.
        stall = _stall_seconds(kind)
        waiting_since = float(getattr(record, f"{kind}_waiting_since") or 0.0)
        started_at = float(getattr(record, f"{kind}_started_at") or 0.0)
        # Except the short first-output window, which stays off for a confirmed pid: silence right
        # after launch is exactly what that heartbeat exists to answer (secretary-751).
        if not pid_confirmed and activity and started_at and float(activity) <= started_at and now - started_at > _initial_output_stall_seconds():
            return self._trigger_wait_watchdog(
                task, record, records, payload, attempt_id, kind=kind,
                trigger=f"no terminal output since launch for {int(now - started_at)}s",
            )
        if progress_at and now - progress_at > stall:
            return self._trigger_wait_watchdog(
                task, record, records, payload, attempt_id, kind=kind,
                trigger=f"no terminal output for {int(now - progress_at)}s",
            )
        # A known fresh activity signal is progress, not merely liveness.  It starts a new wait
        # window so a long-running head cannot hit the old total-duration fallback ceiling.
        if activity and float(activity) >= waiting_since:
            setattr(record, f"{kind}_waiting_since", now)
            self.save_records(payload, records)
            return None
        if not waiting_since:
            setattr(record, f"{kind}_waiting_since", now)
            self.save_records(payload, records)
            return unavailable() if runtime_reason else None
        outcome = _wait_outcome(
            waiting_since=waiting_since,
            now=now,
            stall_seconds=stall,
            respawns=int(getattr(record, f"{kind}_respawns") or 0),
        )
        if outcome == "wait":
            return unavailable() if runtime_reason else None
        if outcome == "respawn":
            return self._respawn_wait(task, record, records, payload, attempt_id, kind=kind, now=now, trigger=f"no {_wait_expectation(kind)} within {stall}s")
        return self._escalate_wait(task, record, records, payload, attempt_id, kind=kind, stall=stall, trigger=f"no {_wait_expectation(kind)}")

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
        # The prompted head owns a fresh idle window: it has just been given something to do, and
        # charging it with the episode that produced the prompt would escalate on the next tick
        # before the worker could possibly have answered.
        _reset_idle(record, "worker")
        records[ref] = record
        self.save_records(payload, records)
        # Persisted before the comment, for the reason the respawn is: an exception out of the
        # writer must not leave the prompt delivered and the record saying it was not.
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
            # Degraded for the same reason the respawn below is: a card whose worker had to be
            # reminded is a card the pipeline is not moving on its own, and an `ok` here would write
            # healthy telemetry over the one signal an operator has before this reaches Blocked.
            "status": "degraded",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "worker-report-prompted",
            "reason": trigger,
        }, trigger

    def _trigger_wait_watchdog(
        self, task, record, records, payload, attempt_id, *, kind: str, trigger: str,
        stall: int | None = None, degraded: bool = False,
    ):
        if int(getattr(record, f"{kind}_respawns") or 0) < 1:
            return self._respawn_wait(
                task, record, records, payload, attempt_id, kind=kind, now=time.time(),
                trigger=trigger, degraded=degraded,
            )
        return self._escalate_wait(
            task, record, records, payload, attempt_id, kind=kind,
            stall=_stall_seconds(kind) if stall is None else stall, trigger=trigger,
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
            # Only the reviewer is stalled; its pane goes and the workspace stays. A stall is not
            # a death: the head this respawn replaces may well still be running, so a stop the host
            # will not confirm ends the tick here rather than putting a second reviewer beside it.
            unconfirmed = self._end_review_pane_confirmed(
                record, records, payload, ref, step=step, attempt_id=attempt_id,
                initiator=STOPPED_BY_WATCHDOG,
            )
            if unconfirmed is not None:
                return unconfirmed
            # One bring-up path for the reviewer: the same helper the normal launch and the
            # recovery path use, so its error handling can't drift from theirs.
            outcome = _start_review(
                self, task, records, record, attempt_id, action="review-respawned", payload=payload
            )
            if outcome.get("status") != "ok":
                self.save_records(payload, records)
                return outcome
        else:
            # Same reasoning as the reviewer above: a silent worker is not a dead one, and this
            # path is about to open a replacement in the same workspace.
            unconfirmed = self._stop_worker_confirmed(
                record, ref, step=step, attempt_id=attempt_id
            )
            if unconfirmed is not None:
                return unconfirmed
            failure = self._worker_relaunch_intent(
                payload, records, ref, record, action="worker-respawn"
            )
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
                blocked_reason="worker respawn failed",
                blocked_request_id=_attempt_request_id(
                    record.attempt_id or attempt_id,
                    "worker-respawn-blocked",
                    ref,
                    _wait_cycle_token(record),
                ),
            )
            if launched is None:
                assert failed is not None
                return failed
            record.state = "claimed"
            # A respawn is a real bring-up: a repinned profile lands a different configuration, and
            # the round then belongs to the head that is actually running.
            self.record_worker_routing(task, record, launched.run)
            _clear_launch_intent(record)
            record.worker_started_at = record.worker_progress_at = now
        if kind == "review":
            record.review_started_at = record.review_progress_at = now
        # Persist the restart before commenting. The tick has no try/except around this, so
        # a writer.comment that raises would otherwise escape with the head already respawned and
        # respawns still 0: the next tick respawns again and the escalation never arrives.
        setattr(record, f"{kind}_waiting_since", now)
        # The replacement head owns its own readiness: whatever the one it replaces was doing when
        # the watchdog fired is not charged against it.
        _reset_idle(record, kind)
        respawns = int(getattr(record, f"{kind}_respawns") or 0) + 1
        setattr(record, f"{kind}_respawns", respawns)
        records[ref] = record
        self.save_records(payload, records)
        # Leave a trace: without it the operator sees only the Blocked hours later and has no
        # way to tell a first stall from a card whose head was already restarted once.
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
                    if kind == "worker" else ""
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
            # A stall the timing ceilings caught is the watchdog working as designed on a head that
            # went quiet. A head that is alive, idle and has delivered nothing is the pipeline
            # failing to move a card, and the tick says so: `degraded` is what puts it in the
            # production telemetry an operator and the steward read (secretary-1063).
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
            # The reviewer may still hold the checkout when its second stall escalates.  End it
            # through the same confirmed boundary that protects a reviewer respawn; a refused
            # stop leaves the record untouched for this exact retry.
            unconfirmed = self._end_review_pane_confirmed(
                record, records, payload, ref, step=step, attempt_id=attempt_id,
                initiator=STOPPED_BY_WATCHDOG,
            )
            if unconfirmed is not None:
                return unconfirmed
            # Review starts over a retained worker.  It cannot outlive a terminal Blocked move
            # either, so settle that role before dropping the only durable record for the checkout.
            unconfirmed = self._stop_worker_confirmed(
                record, ref, step=step, attempt_id=attempt_id
            )
        else:
            unconfirmed = self._stop_worker_confirmed(
                record, ref, step=step, attempt_id=attempt_id
            )
        if unconfirmed is not None:
            return unconfirmed
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=(
                f"wait watchdog: {trigger} after respawn "
                f"(ceiling {stall}s), blocked for the operator"
            ),
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
            # The board move committed but the dispatcher died before checkpointing it. Close
            # that boundary before a red gate can move the card back to the worker.
            record.worker_continuation.confirm_validation_move()
            records[ref] = record
            self.save_records(payload, records)
        try:
            result = self.host.gate_check(task, record)
        except GateTransportError as exc:
            retry = self._gate_transport_retry(
                task, record, records, payload, attempt_id, exc, step="gate",
            )
            if retry is not None:
                return retry
            return self._block_gate_transport(
                task, record, records, payload, attempt_id, step="gate",
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
                task, record, records, payload, attempt_id,
                action=f"{stage}-gate-receipt-blocked",
                reason=f"{stage} gate reported green without a valid exact-SHA receipt",
                step=step, outcome=f"{stage} gate receipt unavailable",
            )
        record.gate_state = "green"
        record.gate_pending_since = 0.0
        record.gate_attestation = accepted.persisted_payload()
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
                    record.attempt_id or attempt_id, f"gate-attestation-{stage}", ref,
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
        record.rejected_sha = self.host.head_commit(record)
        record.rejected_done_reports = 0
        detail = scrub_host_output(result.summary)
        log = scrub_host_output(result.log).strip()
        # A caller that skips the sha-aware summary (GateResult built without `fingerprint`, e.g.
        # the review-freeze drift check) still gets a usable, SHA-independent identity here rather
        # than losing repeat detection outright.
        fingerprint = result.fingerprint or _gate_fingerprint("fallback", log or detail)
        repeat = _gate_red_repeat_count(task, fingerprint)
        prefix = (f"Repeat return (round {repeat + 1}, the reason has not changed). "
                  if repeat else "")
        body = (f"{prefix}The mechanical validation gate is red: {detail}. The card is back in "
                f"In progress for rework.")
        if log:
            body += f"\nTail:\n```\n{log}\n```"
        body += f"\n<!-- gate-fingerprint: {fingerprint} -->"
        # A reviewer can only exist for the later merge/review gates. It must be gone before a
        # retained worker resumes, but the worker itself stays suspended until the continuation
        # has either been delivered or conclusively fallen back to a replacement.
        unconfirmed = self._end_review_pane_confirmed(
            record, records, payload, ref, step="gate", attempt_id=attempt_id,
            initiator=STOPPED_BY_REPLACEMENT,
        )
        if unconfirmed is not None:
            return unconfirmed
        # The round ends here without a reviewer verdict: the outcome names the gate so a later
        # reading does not attribute the bounce to whoever reviewed the round.
        return self._begin_red_transition(
            task, record, records, payload, attempt_id, phase=phase,
            move_reason=body, verdict_outcome=f"{phase}_red",
        )

    def _begin_red_transition(
        self, task: dict[str, Any], record: DispatcherRecord, records: dict[str, DispatcherRecord],
        payload: dict[str, Any], attempt_id: str, *, phase: str, move_reason: str,
        verdict_outcome: str, decision: str = "", decision_body: str = "",
    ) -> dict[str, Any]:
        """The only way a card goes back to In progress for rework.

        The order lives here and nowhere else: the intent is on disk, with its phase, the report
        baseline it was opened against and the reason the card is moving, before anything observable
        moves; the board moves; and only then is it decided whether the round's own session takes the
        continuation or a replacement does. Holding a session is deliberately not a precondition.
        """
        ref = task["ref"]
        baseline = len(task.get("comments") or [])
        # The round this transition opens is reserved here, with the intent and before the move.
        # Completion is idempotent only if the generation is a value it reads rather than one it
        # computes: a recovery that re-entered the completion would otherwise advance a second time
        # and hand one rework round two generations. The observer's instruction is frozen in the
        # same write, for the same reason and against a second one: what the round is for cannot be
        # re-read later from a card whose newest decision comment may have moved on.
        record.worker_continuation.begin_red_transition(
            phase, baseline, move_reason, verdict_outcome, decision,
            reserved_generation=record.report_generation + 1,
            decision_body=decision_body,
        )
        records[ref] = record
        self.save_records(payload, records)
        return self._complete_red_transition(record, records, payload, attempt_id, ref=ref)

    def _complete_red_transition(
        self, record: DispatcherRecord, records: dict[str, DispatcherRecord],
        payload: dict[str, Any], attempt_id: str, *, ref: str,
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
            # already recorded when the card parked. Recording it again would overwrite the
            # round's outcome with the name of the decision that acted on it.
            self._record_verdict_routing(ref, record, continuation.verdict_outcome)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="in_progress",
            reason=continuation.move_reason,
            # A rework decision carries itself into the move; the board refuses to take a card
            # out of Assessment on anything less. A red mechanical gate moving out of Validate
            # carries nothing, and is refused nothing.
            decision=continuation.decision,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id, f"{phase}-red", ref, str(baseline)
            ),
        )
        moved = self.reader.show(ref)
        # The report that closed the previous round is behind this baseline, so no later tick can
        # read it as a completion of the round this transition opens.
        record.comment_baseline = max(len(moved.get("comments") or []), baseline)
        # Where the next review verdict is scanned from, so the verdict this transition acted on
        # cannot be read again as the new round's.
        record.review_baseline = record.comment_baseline
        # The rework is a new report round, and its generation is the one this transition reserved
        # before the move. Assigned, not advanced: every tick that finishes this transition writes
        # the same number. A transition written before the reservation existed carries none, and
        # falls back to the advance it was written with.
        record.report_generation = (
            continuation.reserved_generation or record.report_generation + 1
        )
        # And the instruction that round is being opened on, taken from the same transition. Always
        # assigned, never merged: a round opened by a red gate carries no decision, and inheriting
        # the one that opened the round before it would hand a worker an adjudication of a review
        # its own code has already answered.
        record.report_decision = continuation.decision_body
        record.gate_state = ""
        record.gate_pending_since = 0.0
        record.gate_attestation = {}
        record.gate_transport_failures = 0
        record.gate_transport_error = ""
        # The round the verdict judged is over here. A park keeps the reviewed commit while the
        # card waits; the rework this opens is new code, and a stale pin would refuse its merge.
        record.review_commit = ""
        _reset_wait(record, "review")
        _reset_wait(record, "worker")
        records[ref] = record
        self.save_records(payload, records)
        return self._deliver_red_continuation(
            moved, record, records, payload, attempt_id, phase=phase
        )

    def _deliver_red_continuation(
        self, task: dict[str, Any], record: DispatcherRecord, records: dict[str, DispatcherRecord],
        payload: dict[str, Any], attempt_id: str, *, phase: str,
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
            provider_observation = self._observe_retained_continuation_progress(
                task, record, now=now
            )
            blocked = self._block_unadmitted_continuation_liveness(
                task, record, records, payload, attempt_id, phase=phase,
                observation=provider_observation,
            )
            if blocked is not None:
                return blocked
            fresh_provider_progress = provider_observation == "progressed"
            pending = self._continuation_recovery_window(
                task, record, records, payload, attempt_id, phase=phase,
                fresh_provider_progress=fresh_provider_progress, now=now,
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
                        task, record, records, payload, attempt_id,
                        continuation_reason="safe recovery resume was already spent", phase=phase,
                    )
                # This is a once-only capability result.  Persist spending it before normal
                # delivery touches the pane, so a crash cannot return through this branch twice.
                records[ref] = record
                self.save_records(payload, records)
        if continuation.retained:
            try:
                # The suspension was confirmed when the round was handed to the gate or the
                # reviewer, but that answer is a memory of a past tick: a SIGCONT from terminal
                # recovery or an operator since then makes this a second writer, not a session to
                # reuse. The heartbeat is asked again here, where the boundary actually opens.
                self.host.confirm_worker_retained(record)
            except HostError as exc:
                reason = scrub_host_output(str(exc))
                unconfirmed = self._stop_worker_confirmed(
                    record, ref, step=step, attempt_id=attempt_id
                )
                if unconfirmed is not None:
                    return unconfirmed
                return self._restart_red_worker(
                    task, record, records, payload, attempt_id,
                    continuation_reason=reason, phase=phase, worker_stopped=True,
                )
            if opening_delivery:
                # Persist the delivery boundary before waking the worker. If this tick dies after
                # delivery, replay stays on this branch instead of treating the old done marker as
                # a completion from the new rework round.
                continuation.begin_delivery(phase, time.time())
                record.worker_continuation_liveness = WorkerContinuationLiveness.begin(
                    record.worker_head_run
                )
                # Establish the provider cursor before SIGCONT/readiness.  The first observation
                # is a baseline, not proof that a historical rollout belongs to this continuation.
                provider_observation = self._observe_retained_continuation_progress(
                    task, record, now=time.time()
                )
                blocked = self._block_unadmitted_continuation_liveness(
                    task, record, records, payload, attempt_id, phase=phase,
                    observation=provider_observation,
                )
                if blocked is not None:
                    return blocked
                fresh_provider_progress = provider_observation == "progressed"
                records[ref] = record
                self.save_records(payload, records)
                pending = self._continuation_recovery_window(
                    task, record, records, payload, attempt_id, phase=phase,
                    fresh_provider_progress=fresh_provider_progress, now=time.time(),
                )
                if pending is not None:
                    return pending
            else:
                # This is an already-open delivery boundary.  Recreating the liveness value here
                # would turn every busy retry into a new baseline and make no-progress unbounded.
                records[ref] = record
                self.save_records(payload, records)
            try:
                self.host.resume_worker(task, record)
            except HostError as exc:
                if _delivery_readiness_state(exc) == READINESS_BUSY:
                    # The shared delivery boundary observed an owned pane working before it sent
                    # anything. That is neither acknowledgement nor a dead-head vote: preserve
                    # the exact continuation and try its durable, bounded schedule later.
                    _record_worker_delivery_evidence(record, exc)
                    liveness = record.worker_continuation_liveness
                    # The pre-read provider cursor is the precedence rule: a fresh rollout keeps
                    # exactly this HeadRun and merely restarts its no-progress ladder.  `tui-idle`
                    # saying busy neither nudges nor replaces it.
                    if fresh_provider_progress:
                        continuation.busy_attempts = liveness.busy_attempts
                        continuation.busy_next_at = time.time() + BUSY_RETRY_INITIAL_SECONDS
                        records[ref] = record
                        self.save_records(payload, records)
                        return _retained_worker_busy_deferred(
                            ref, record, attempt_id, phase,
                            delay=BUSY_RETRY_INITIAL_SECONDS,
                        )
                    if liveness.state != ContinuationLivenessState.STALLED:
                        # The first exact-source cursor is a persisted v1 baseline, not evidence
                        # that this retained turn is stalled.  Keep the existing head and retry
                        # schedule without spending any no-progress attempt.
                        continuation.busy_next_at = time.time() + BUSY_RETRY_INITIAL_SECONDS
                        records[ref] = record
                        self.save_records(payload, records)
                        return _retained_worker_busy_deferred(
                            ref, record, attempt_id, phase, delay=BUSY_RETRY_INITIAL_SECONDS,
                        )
                    liveness.no_progress_evidence = _continuation_no_progress_evidence(
                        record, liveness.state
                    )
                    liveness.note_busy(time.time())
                    continuation.busy_attempts = max(0, liveness.busy_attempts - 1)
                    delay = continuation.defer_busy(time.time())
                    # `defer_busy` owns the old persisted retry deadline; liveness owns the
                    # bounded episode count.  Keep them in sync without letting the former grow
                    # beyond this exact HeadRun's provider evidence.
                    continuation.busy_attempts = liveness.busy_attempts
                    records[ref] = record
                    self.save_records(payload, records)
                    bounded = self._advance_no_progress_continuation(
                        task, record, records, payload, attempt_id, phase=phase,
                    )
                    if bounded is not None:
                        return bounded
                    return _retained_worker_busy_deferred(
                        ref, record, attempt_id, phase, delay=delay
                    )
                _record_worker_delivery_evidence(record, exc, failure=True)
                records[ref] = record
                self.save_records(payload, records)
                return self._restart_red_worker(
                    task, record, records, payload, attempt_id,
                    continuation_reason=scrub_host_output(str(exc)), phase=phase,
                )
            continuation.confirm_delivery()
            records[ref] = record
            self.save_records(payload, records)
            return self._finish_retained_worker_resume(
                task, record, records, payload, attempt_id, phase=phase
            )
        # Same reservation as the retained branch: the round the rework head belongs to is fixed on
        # disk with the intent, so an adoption resumes it instead of the round the verdict closed.
        return self._restart_red_worker(
            task, record, records, payload, attempt_id,
            continuation_reason="no retained worker session was available", phase=phase,
        )

    def _observe_retained_continuation_progress(
        self, task: dict[str, Any], record: DispatcherRecord, *, now: float,
    ) -> str:
        """Persist provider progress before a continuation interprets `tui-idle`."""
        try:
            evidence = getattr(
                self.host, "provider_progress", lambda _task, _record, _kind: {
                    "state": "unavailable", "reason": "host has no provider-progress probe",
                }
            )(task, record, "worker")
        except Exception as exc:
            evidence = {
                "state": "unavailable",
                "reason": f"provider-progress probe failed: {scrub_host_output(str(exc))}",
            }
        liveness = record.worker_continuation_liveness
        if not liveness.bound and record.worker_continuation.busy_attempts:
            # This explains incident-era retry state without ever admitting it into the v1
            # ladder.  An old count is audit data, not an unchanged exact-source observation.
            liveness.legacy_busy_attempts = max(
                liveness.legacy_busy_attempts, record.worker_continuation.busy_attempts,
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
        if observation in {"baseline", "stalled", "progressed"} and record.worker_continuation_liveness.admitted:
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
                "continuation-liveness-unavailable", ref, phase,
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
            # The existing stop path is the only component allowed to resolve this.  It will
            # either confirm the old HeadRun stopped and launch one replacement, or return its
            # own identity-fenced refusal; neither outcome reaches for a foreign pane.
            return self._restart_red_worker(
                task, record, records, payload, attempt_id,
                continuation_reason="continuation liveness HeadRun identity is fenced",
                phase=phase,
            )
        if liveness.recovery_rung != ContinuationRecoveryRung.SAFE_RECOVERY_RESPONSE_WINDOW:
            return None
        if now < liveness.recovery_response_deadline:
            records[ref] = record
            self.save_records(payload, records)
            return _retained_worker_recovery_window(
                ref, record, attempt_id, phase,
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
            task, record, records, payload, attempt_id,
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
        if (
            not liveness.admitted
            or liveness.state != ContinuationLivenessState.STALLED
        ):
            return self._block_unadmitted_continuation_liveness(
                task, record, records, payload, attempt_id, phase=phase,
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
            # The intent was durable before the capability was called.  After a crash in that
            # boundary we cannot tell whether the provider acted, so retrying would be a second
            # recovery action against a live conversation.  Spend the safe rung conservatively.
            liveness.terminalize(
                "replacement", "safe recovery response was unconfirmed after dispatcher recovery"
            )
            records[task["ref"]] = record
            self.save_records(payload, records)
        if not liveness.terminal:
            # Intent first: a dispatcher death before or inside a provider capability invocation
            # cannot make the next process invoke it twice.
            liveness.begin_safe_recovery(time.time())
            records[task["ref"]] = record
            self.save_records(payload, records)
            try:
                result = getattr(
                    self.host, "safe_recover_worker_continuation", lambda *_args: {
                        "state": "unavailable",
                        "reason": "host has no provider/terminal-safe recovery capability",
                    }
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
                # This is the only extension point for a future provider API.  Its response is
                # recorded before waiting, and it has no way to tunnel a raw interrupt through a
                # generic terminal command.
                liveness.safe_recovery_response_window(time.time(), 30.0)
                records[task["ref"]] = record
                self.save_records(payload, records)
                return _retained_worker_recovery_window(
                    task["ref"], record, attempt_id, phase, remaining=30,
                )
            reason = (
                str(result.get("reason") or "safe recovery capability is unavailable")
                if isinstance(result, dict) else "safe recovery capability returned an invalid shape"
            )
            liveness.recovery_rung = ContinuationRecoveryRung.SAFE_RECOVERY_UNAVAILABLE
            liveness.terminalize("replacement", f"safe recovery unavailable: {reason}")
            records[task["ref"]] = record
            self.save_records(payload, records)
        return self._restart_red_worker(
            task, record, records, payload, attempt_id,
            continuation_reason=(
                "provider progress remained absent after bounded continuation recovery: "
                f"{record.worker_continuation_liveness.reason}"
            ),
            phase=phase,
        )

    def _finish_retained_worker_resume(
        self, task: dict[str, Any], record: DispatcherRecord, records: dict[str, DispatcherRecord],
        payload: dict[str, Any], attempt_id: str, *, phase: str,
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
            "status": "ok", "step": step, "pilot_ref": ref, "attempt_id": attempt_id,
            "action": f"{phase}-red-reused-worker",
        }

    def _restart_red_worker(
        self, task: dict[str, Any], record: DispatcherRecord, records: dict[str, DispatcherRecord],
        payload: dict[str, Any], attempt_id: str, *, continuation_reason: str, phase: str,
        worker_stopped: bool = False,
    ) -> dict[str, Any]:
        """Launch the red-verdict fallback only after its worker was conclusively stopped."""
        ref = task["ref"]
        review = phase == "review"
        step = "review" if review else "gate"
        blocked_kind = "rework-blocked" if review else f"{phase}-red-blocked"
        action = "rework-started" if review else f"{phase}-red-rework"
        continuation = "replacement"
        if (
            record.worker_continuation_liveness.bound
            and not record.worker_continuation_liveness.terminal
        ):
            record.worker_continuation_liveness.terminalize(
                "replacement", continuation_reason
            )
        # This is intentionally unconditional.  A record written by an older dispatcher, or one
        # adopted after a crash, may not carry the retained timestamp even while its old worker is
        # still alive.  Lack of that field is ambiguous, never permission for a second writer.
        if not worker_stopped:
            unconfirmed = self._stop_worker_confirmed(
                record, ref, step=step, attempt_id=attempt_id
            )
            if unconfirmed is not None:
                return unconfirmed
        rework_round = record.attempt_round + 1
        # The launch intent takes the transition over from here: it is durable, it reserves the
        # rework round, and recovery adopts or relaunches exactly one head from it. Handing the
        # record over in the same write is what keeps the two from both owing this card a worker.
        # The handover is only real if it reached the disk. `write_launch_intent` restores the
        # intent fields it touched, but the transition it was meant to take over lives here, and a
        # tick that returns still saves this record: dropping it on a failed write would leave the
        # card In progress with nothing durable owing it a worker.
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
            blocked_reason="rework bring-up failed",
            blocked_request_id=_attempt_request_id(
                record.attempt_id or attempt_id, blocked_kind, ref
            ),
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
        reason: str,
        request_id: str,
        error: Exception,
    ) -> dict[str, Any]:
        """Block a failed rework launch while retaining the workspace's resume provenance."""
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=_bring_up_blocked_reason(reason, error, record, WORKER_ROLE),
            request_id=request_id,
        )
        resume_workspaces = payload.setdefault("resume_workspaces", {})
        if isinstance(resume_workspaces, dict):
            resume_workspaces[ref] = record.attempt_id or attempt_id
        records.pop(ref, None)
        self.save_records(payload, records)
        return {"status": "blocked", "step": step, "pilot_ref": ref, "reason": reason}

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
            task, record, records, payload, attempt_id,
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
    ) -> dict[str, Any]:
        """CI is non-terminal (a check still running, or none posted yet). Wait, tracking how long the
        rollup has sat non-terminal; past GATE_PENDING_STALL_SECONDS escalate once to Blocked so a
        required check nothing ever posts does not leave the card unwatched forever.
        """
        ref = task["ref"]
        now = time.time()
        if not record.gate_pending_since:
            record.gate_pending_since = now
            self.save_records(payload, records)
            return {"status": "ok", "step": "gate", "pilot_ref": ref, "attempt_id": attempt_id, "action": "gate-pending"}
        if now - record.gate_pending_since <= GATE_PENDING_STALL_SECONDS:
            return {"status": "ok", "step": "gate", "pilot_ref": ref, "attempt_id": attempt_id, "action": "gate-pending"}
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
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "gate-pending-stall", ref),
        )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {"status": "ok", "step": "gate", "pilot_ref": ref, "attempt_id": attempt_id, "to": "blocked"}

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
    ) -> dict[str, Any]:
        """A green review verdict parks the card; it does not merge it."""
        ref = task["ref"]
        # The verdict is recorded before the gate runs: it is a fact about the head pair of this
        # round and stays true even when the mechanical re-check bounces the card back afterwards.
        self._record_verdict_routing(ref, record, "green")
        kind, result, detail = self._merge_readiness(task, record)
        if kind == "transport":
            retry = self._gate_transport_retry(
                task, record, records, payload, attempt_id,
                GateTransportError(detail), step="review",
            )
            if retry is not None:
                return retry
            return self._block_gate_transport(
                task, record, records, payload, attempt_id, step="review",
                action="merge-gate-transport-blocked",
            )
        if kind == "drift":
            # The gate was never asked here, so nothing about the transport budget is known: the
            # bounce clears the record's gate state on its own way to In progress.
            return self._gate_red_to_worker(
                task, record, records, payload, attempt_id, GateResult("red", detail), phase="review-freeze"
            )
        self._gate_answered(ref, record, records, payload)
        if kind == "failed":
            return self._block_merge_path(
                task, record, records, payload, attempt_id,
                action="merge-gate-blocked", reason=f"merge gate failed: {detail}",
                step="review", outcome="merge gate failed",
            )
        if kind == "pending":
            return {"status": "ok", "step": "review", "pilot_ref": ref, "attempt_id": attempt_id, "action": "merge-gate-pending"}
        if kind != "green":
            if result is None:
                return self._block_merge_path(
                    task, record, records, payload, attempt_id,
                    action="merge-gate-result-blocked",
                    reason="merge gate returned a non-green state without a result payload",
                    step="review", outcome="merge gate result unavailable",
                )
            return self._gate_red_to_worker(task, record, records, payload, attempt_id, result, phase="merge-gate")
        if result is None:
            return self._block_merge_path(
                task, record, records, payload, attempt_id,
                action="merge-gate-result-blocked",
                reason="merge gate returned green without a result payload",
                step="review", outcome="merge gate result unavailable",
            )
        parks = self._parks_for_decision(task)
        blocked = self._accept_green_gate(
            task, record, records, payload, attempt_id, result,
            stage="assessment" if parks else "release",
        )
        if blocked is not None:
            return blocked
        if not parks:
            # No observer to release it: the green verdict merges on its own tick, as it did
            # before Assessment existed.
            return self._release_effect(
                task, record, records, payload, attempt_id, step="review",
                move_reason="review:green",
            )
        # The reviewer's round is over whichever way the decision goes, and the checkout must be
        # quiet while the card waits: a reviewer left alive in it would keep reading a workspace
        # nobody is watching, for as long as the park lasts. Its commit outlives its pane.
        reviewed = record.review_commit or self.host.head_commit(record)
        unconfirmed = self._end_review_pane_confirmed(
            record, records, payload, ref, step="review", attempt_id=attempt_id,
            initiator=STOPPED_BY_REVIEW_VERDICT,
        )
        if unconfirmed is not None:
            return unconfirmed
        return self._begin_park(
            task, record, records, payload, attempt_id, verdict_outcome="green",
            reviewed_commit=reviewed,
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
        reviewed_commit: str = "",
    ) -> dict[str, Any]:
        """The only way a substantive verdict leaves Validate.

        The red transition's order, for the same reason: the intent is on disk, with the reason the
        card is moving, before anything observable moves. Nothing comes after the move — the card waits.
        """
        ref = task["ref"]
        # Re-pinned after the reviewer's pane was forgotten: the merge gate refuses a release for
        # a checkout that moved off the commit the verdict was given for, and between the park and
        # the decision is exactly the window in which it can move.
        record.review_commit = reviewed_commit or record.review_commit
        record.worker_continuation.begin_park(
            "review", len(task.get("comments") or []), move_reason, verdict_outcome
        )
        records[ref] = record
        self.save_records(payload, records)
        return self._complete_park(record, records, payload, attempt_id, ref=ref)

    def _complete_park(
        self, record: DispatcherRecord, records: dict[str, DispatcherRecord],
        payload: dict[str, Any], attempt_id: str, *, ref: str,
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
                record.attempt_id or attempt_id, "review-assessment", ref,
                str(continuation.report_baseline),
            ),
        )
        continuation.confirm_park()
        record.state = "assessment"
        _reset_wait(record, "review")
        records[ref] = record
        self.save_records(payload, records)
        return {
            "status": "ok", "step": "review", "pilot_ref": ref, "attempt_id": attempt_id,
            "to": "assessment", "verdict": continuation.verdict_outcome,
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
            # A rework decision whose board move did not commit. Same rule as Validate: the
            # transition the card is already owed is finished before any decision is read again.
            return self._complete_red_transition(record, records, payload, attempt_id, ref=ref)
        if continuation.assessment_pending:
            # The park's move landed and the tick died before the checkpoint. Re-issuing it is a
            # no-op through the request id, and it is what turns the record into a parked one.
            return self._complete_park(record, records, payload, attempt_id, ref=ref)
        if not continuation.parked:
            # A card whose dispatcher record was lost while parked, or one an operator parked by
            # hand. The board is the fact; the record is brought to it without a move. A session
            # this record cannot prove is held is not held, so an adopted park owns no worker and
            # a rework decision on it opens a replacement through the ordinary confirmed stop.
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
                "status": "ok", "step": "assessment", "pilot_ref": ref, "attempt_id": attempt_id,
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
        # A parked card should have no reviewer left; an adopted one may still carry the
        # identity of a pane nobody stopped. Either way nothing is woken beside a head the host
        # will not confirm gone.
        if record.owns_head("review"):
            unconfirmed = self._end_review_pane_confirmed(
                record, records, payload, ref, step="assessment", attempt_id=attempt_id,
                initiator=STOPPED_BY_REVIEW_VERDICT,
            )
            if unconfirmed is not None:
                return unconfirmed
        # The findings themselves are not repeated here: the rework prompt reads the card's last
        # red verdict directly, and a second copy on the move would drift from it. The decision is
        # different: it is what the round is for, so it is frozen with the round rather than looked
        # up again when the document is built.
        return self._begin_red_transition(
            task, record, records, payload, attempt_id, phase="review",
            move_reason=f"Observer decision: rework. {reason}".strip(),
            verdict_outcome="red", decision="rework", decision_body=reason,
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
            "status": "ok", "step": "assessment", "pilot_ref": ref, "attempt_id": attempt_id,
            "to": "blocked", "decision": "reslice",
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
            task, record, records, payload, attempt_id,
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
        kind, result, detail = self._merge_readiness(task, record)
        if kind == "transport":
            # The decision stands and the card stays parked: a release that could not ask the gate
            # is not a release that was refused.
            retry = self._gate_transport_retry(
                task, record, records, payload, attempt_id,
                GateTransportError(detail), step="assessment",
            )
            if retry is not None:
                return retry
            return self._block_gate_transport(
                task, record, records, payload, attempt_id, step="assessment",
                action="release-gate-transport-blocked",
                prefix="Observer decision: release. ",
            )
        if kind != "drift":
            # `drift` is decided before the gate is asked; only an answer clears the budget.
            self._gate_answered(ref, record, records, payload)
        if kind == "pending":
            return {"status": "ok", "step": "assessment", "pilot_ref": ref, "attempt_id": attempt_id, "action": "merge-gate-pending"}
        if kind != "green":
            summary = {
                "drift": f"the release cannot land: {detail}",
                "failed": f"the merge gate could not be read: {detail}",
            }.get(kind, "the mechanical gate is no longer green for the checkout this release was decided on")
            return self._block_merge_path(
                task, record, records, payload, attempt_id,
                action=f"release-{kind}-blocked", reason=f"Observer decision: release. {summary}",
                step="assessment", outcome=f"release {kind}",
            )
        if result is None:
            return self._block_merge_path(
                task, record, records, payload, attempt_id,
                action="release-gate-result-blocked",
                reason="merge gate returned green without a result payload",
                step="assessment", outcome="merge gate result unavailable",
            )
        blocked = self._accept_green_gate(
            task, record, records, payload, attempt_id, result, stage="release"
        )
        if blocked is not None:
            return blocked
        return self._release_effect(
            task, record, records, payload, attempt_id, step="assessment",
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
            # A rejected merge (non-fast-forward push, gh refusing on branch protection) must
            # land the card in Blocked rather than escape the tick: an escaping error leaves the
            # card where it is with a verdict or a decision already standing, so the next tick
            # retries the same doomed merge forever while the terminals stay up.
            return self._block_merge_path(
                task, record, records, payload, attempt_id,
                action="merge-blocked", reason=f"merge failed: {scrub_host_output(str(exc))}",
                step=step, outcome="merge failed",
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
        self, task: dict[str, Any], *, role: str, head: str = "", workspace: str = "",
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
            return HeadRun(
                role=role, head=str(head), adapter="unknown", model_source=MODEL_UNKNOWN
            ).to_json()

    def _journal_round(self, ref: str) -> int:
        """The last worker round the journal holds for this card. Survives a lost dispatcher record,
        a restore, and a card that went back to Ready and was claimed again."""
        history = _routing_attempts(self.audit.events(ref, kind="routing"))
        return history[-1].attempt if history else 0

    def open_worker_round(self, record: DispatcherRecord, *, round_number: int = 0) -> None:
        """Start the card's next worker round: stamp its number and drop the previous round's heads.
        """
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
        record.worker_run = run or self.head_run_snapshot(
            task, role="worker", head=record.head, workspace=record.workspace,
            failover=bool(record.preferred_head),
        )
        self._record_routing(ref, record, phase="worker", heads=[record.worker_run])

    def record_review_routing(
        self, task: dict[str, Any], record: DispatcherRecord, run: dict[str, Any] | None = None
    ) -> None:
        """Record the reviewer head this bring-up just put up, as launched."""
        ref = task["ref"]
        if not record.attempt_round:
            record.attempt_round = self._journal_round(ref) + 1
        record.review_run = run or self.head_run_snapshot(
            task, role="reviewer", head=record.review_head, workspace=record.workspace,
            failover=bool(record.preferred_review_head),
        )
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
        # The request id carries the launched configurations, not just the round: a repeated
        # bring-up of the same head writes the same id and commits once, while a bring-up on a
        # different configuration is a different id and appends. Same for a verdict: it is keyed by
        # the pair that produced it, so a verdict issued by a relaunched reviewer is not swallowed
        # by the first reviewer's record.
        parts = [str(record.attempt_round)]
        if outcome:
            parts.append(outcome)
        parts.extend(_run_key(head) for head in heads)
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
            request_id=_attempt_request_id(
                record.attempt_id, f"routing-{phase}", ref, "-".join(parts)
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
            # A parked card has no head to recover: the reviewer was stopped when it parked and
            # the worker, if one is still suspended in the checkout, is not something this record
            # can prove. It is adopted as parked and the decision path stops whatever it finds.
            state = "assessment"
        # The routing round of a card whose dispatcher record was lost comes back from the journal,
        # heads included: re-reading the registry would report today's `heads.toml` for a head
        # launched hours ago. A card claimed before this telemetry existed has no round; it opens
        # one on its next bring-up rather than inventing history for the round already running.
        resumed = _routing_attempts(self.audit.events(task["ref"], kind="routing"))
        round_record = resumed[-1] if resumed else None
        workspace = self.host.restore_workspace(task, worker)
        # The report generation is dispatcher state, and this is the path where that state was
        # lost. The TASK.md in the checkout names the round the live worker is actually in; the
        # reports already on the board are the floor when there is no readable document. Both are
        # lower bounds, so the larger one is taken: a generation may skip, never repeat.
        report_generation = max(
            _task_doc_report_generation(workspace), _spent_report_generations(task) + 1
        )
        # And the decision that round was opened on, from the same document. The card's newest
        # decision comment is deliberately not consulted here: it answers "what has been decided
        # since", which is the question that must not reach a running round.
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
            # A reviewer only launches once the gate is green, so an adopted card already in review
            # inherits a passed gate rather than re-running it before the recovery path.
            gate_state="green" if launched else "",
            attempt_round=round_record.attempt if round_record else 0,
            worker_run=round_record.worker.to_json() if round_record and round_record.worker else {},
            review_run=round_record.reviewer.to_json() if round_record and round_record.reviewer else {},
        )
        # A lost record may be recovered from the worker's own heartbeat, but only after its
        # self-described run, role and card binding have been promoted into a HeadRun and checked
        # again.  A legacy pid or a process for another card has no such proof and remains
        # unbound; the normal claim path then refuses to signal it.
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
                pid_file, run=recovered, role=WORKER_ROLE, leaf=recovered.leaf,
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


def runtime_from_args(instance: str, data_dir: str | None, *, host_mode: str, owner: str) -> DispatcherRuntime:
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


def _gate_attestation_for_prompt(
    record: DispatcherRecord | None,
    current_sha: str = "",
    candidate: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return a complete persisted receipt, never a guessed substitute.

    A record carrying only the old boolean ``gate_state`` is unavailable evidence rather than an
    invented SHA: the exact binding is the safety property.
    """
    source = candidate if isinstance(candidate, dict) else getattr(record, "gate_attestation", {})
    return _accepted_gate_receipt(source, current_sha)


def _continuation_note(generation: int = 0, decision: str = "") -> str:
    """The discriminating tail of the continuation pointer: what a pointer cannot delegate.

    The generation, because the retained conversation still holds the previous round's report
    command in its own scrollback and a number is what makes a replayed one visibly wrong; and the
    standing of the decision, because a pointer that only names a document leaves the conversation
    to rank that document's sections itself. This is a tail, not a line: `NudgePointer.at_document`
    builds it into the nudge with the document's absolute path and checks the ceiling over both.
    """
    note = f"Generation {generation}: use its report command, not an earlier turn's."
    if decision.strip():
        note += " Its observer decision outranks the findings below it."
    return note


def _delivery_evidence_json(carrier: Any, subject: str) -> dict[str, Any]:
    """The bounded evidence a delivery verdict or failure carries, ready to persist."""
    evidence = getattr(carrier, "evidence", None)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    if not isinstance(evidence, dict) or not evidence:
        if isinstance(carrier, BaseException):
            return {"subject": subject, "reason": scrub_host_output(str(carrier))[:400]}
        return {}
    evidence = dict(evidence)
    evidence["subject"] = subject
    return evidence


def _record_worker_delivery_evidence(
    record: DispatcherRecord, carrier: Any, *, failure: bool = False
) -> None:
    """Persist a worker prompt receipt before recovery can replace its conversation."""
    evidence = (
        dict(carrier)
        if isinstance(carrier, dict) and carrier
        else _delivery_evidence_json(carrier, "worker-prompt")
    )
    if not evidence:
        return
    record.worker_delivery_evidence = evidence
    if failure and _delivery_readiness_state(evidence) != READINESS_BUSY:
        record.worker_delivery_failures += 1


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
    record: DispatcherRecord, state: ContinuationLivenessState,
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


def _report_nudge_prompt(generation: int, reference: str) -> str:
    """What a worker that stopped working without reporting is told, once per round.

    It opens no round and carries no instruction about the work. The generation is spelled out
    because the conversation's own scrollback holds earlier rounds' commands. The last sentence is
    the point: a commit, a push or a green test run is not a report.
    """
    card = f" for {reference}" if reference else ""
    return (
        f"The dispatcher is still waiting for the worker report of generation {generation}{card}, "
        "and this head is sitting at its prompt with nothing delivered for that round. If the work "
        "is done, report it now with the report command in TASK.md at the workspace root: its "
        f"--request-id and its body file both end in {generation}. If it is not done, carry on — "
        "this changes nothing about the task, the round or what the round owes. Committing, "
        "pushing, a green test run or an earlier round's report command is not a report of this "
        "round: the card moves only when that command runs."
    )


def _wait_expectation(kind: str) -> str:
    return "review verdict" if kind == "review" else "worker report"


def _watchdog_kind(role: str) -> str:
    """`_launch`'s `role` ("worker"/"reviewer") to the `kind` the wait watchdog and
    `command_terminal_status` key their pid-heartbeat file on ("worker"/"review")."""
    return "review" if role == "reviewer" else "worker"


def _body_file_path(kind: str, reference: str, review_round: int) -> str:
    """Where a head writes its report/verdict body. Outside the workspace on purpose: a stray file in
    the worktree makes `git status` dirty, and the done-report check rejects that. The round is in
    the name because heads are told to leave the file behind: without it round 2 starts on top of
    round 1's body and a head that skips the write posts a stale verdict.
    """
    root = os.environ.get("SECRETARY_DISPATCHER_BODY_DIR", "/tmp").rstrip("/") or "/tmp"
    return f"{root}/secretary-{kind}-{_request_token(reference)}-{_request_token(str(review_round))}.md"


def _body_file_instructions(body_file: str) -> list[str]:
    """Spell out the delivery path: file first with a normal editing tool, then one plain command.
    The codex runtime refuses an inline mktemp/rm assembly.
    """
    return [
        f"Write the body to {body_file} with your file-writing tool,",
        "then run the command below verbatim. Do not assemble the body inside the shell command",
        "(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses",
        "rm-style commands, and quotes or backticks in the body break the call. Leave the file in",
        "place afterwards; the dispatcher does not read it.",
    ]
