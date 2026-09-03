"""Host I/O and catalog boundary for the production dispatcher."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from secretary import state_repo
from secretary._fsutil import write_text_atomic
from secretary.codex_provider_events import (
    CodexProviderEventIngress,
)
from secretary.config import validate_instance
from secretary.dispatch.head_vitality_episode import (
    VitalityVerdict as VitalityVerdict,
)
from secretary.dispatcher_gate import (
    GateResult,
)
from secretary.dispatcher_gate import (
    gate_check as _gate_check,
)
from secretary.dispatcher_gate import (
    rerun_failed_ci as _rerun_failed_ci,
)
from secretary.dispatcher_gate import (
    validation_ci as _validation_ci,
)
from secretary.dispatcher_gate_receipt import (
    accepted_receipt as _accepted_gate_receipt,
)
from secretary.dispatcher_gate_receipt import (
    is_exact_sha as _is_exact_sha,
)
from secretary.dispatcher_gate_receipt import (
    render_receipt,
)
from secretary.dispatcher_heartbeat import heartbeat_identity
from secretary.dispatcher_helpers import (
    _decision_record_line,
    _last_gate_red_body,
    _legacy_worker_branch,
    _round_record_line,
    _tail,
    scrub_host_output,
)
from secretary.dispatcher_helpers import (
    safe_one_line as _safe_one_line,
)
from secretary.dispatcher_launch import (
    CAUSE_BASE_BRANCH_CONTRACT,
    CAUSE_WORKSPACE_CONTRACT,
    REVIEW_ROLE,
    WORKER_ROLE,
)
from secretary.dispatcher_launch import (
    infrastructure_action as _infrastructure_action,
)
from secretary.dispatcher_launch import (
    pane_state_label as _pane_state_label,
)
from secretary.dispatcher_launcher import (
    HeadLaunchError,
)
from secretary.dispatcher_launcher import (
    claude_launch_model as _claude_launch_model,
)
from secretary.dispatcher_launcher import (
    ensure_claude_workspace_ready as _ensure_claude_workspace_ready,
)
from secretary.dispatcher_launcher import (
    ensure_codex_workspace_trusted as _ensure_codex_workspace_trusted,
)
from secretary.dispatcher_launcher import (
    role_launch_env as _role_launch_env,
)
from secretary.dispatcher_observer import (
    OBSERVER_HEAD_FALLBACK,
    OBSERVER_PROMPT_FILE,
    OBSERVER_ROLE,
    ObserverLaunchAborted,
)
from secretary.dispatcher_observer import (
    delivery_evidence_summary as _observer_delivery_evidence_summary,
)
from secretary.dispatcher_observer import (
    observer_launch_prompt as _observer_launch_prompt,
)
from secretary.dispatcher_observer import (
    observer_pid_file as _observer_pid_file,
)
from secretary.dispatcher_review import (
    command_terminal_status as _command_terminal_status,
)
from secretary.dispatcher_state import (
    DispatcherRecord,
)
from secretary.dispatcher_state import (
    attempt_request_id as _attempt_request_id,
)
from secretary.dispatcher_state import (
    request_token as _request_token,
)
from secretary.dispatcher_tui import (
    DELIVERY_ACCEPTED,
    READINESS_BUSY,
    READINESS_READY,
    TuiDeliveryError,
)
from secretary.dispatcher_tui import (
    bind_claude_provider_progress_source as _bind_claude_provider_progress_source,
)
from secretary.dispatcher_tui import deliver_tui_prompt as _deliver_tui_prompt
from secretary.dispatcher_tui import (
    delivery_readiness_state as _delivery_readiness_state,
)
from secretary.dispatcher_tui import (
    prepare_claude_provider_progress_source as _prepare_claude_provider_progress_source,
)
from secretary.dispatcher_tui import (
    provider_progress_for_run as _provider_progress_for_run,
)
from secretary.dispatcher_tui import (
    terminal_turn_started as _terminal_turn_started,
)
from secretary.dispatcher_types import (
    STOPPED_BY_DISPATCHER,
    STOPPED_BY_OPERATOR,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_RECONCILIATION,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_REVIEW_FREEZE,
    DispatcherError,
    HeadLaunchAborted,
    HeadPaneNotReady,
    HostError,
    ReviewLaunch,
    review_pane_label,
)
from secretary.dispatcher_watchdog import (
    HeadRunIdentityMismatch as _HeadRunIdentityMismatch,
)
from secretary.dispatcher_watchdog import (
    bind_head_heartbeat as _bind_head_heartbeat,
)
from secretary.dispatcher_watchdog import (
    clear_head_heartbeat as _clear_head_heartbeat,
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
    heartbeat_is_dead as _heartbeat_is_dead,
)
from secretary.dispatcher_watchdog import (
    heartbeat_is_live_match as _heartbeat_is_live_match,
)
from secretary.dispatcher_watchdog import (
    heartbeat_is_mismatch as _heartbeat_is_mismatch,
)
from secretary.dispatcher_watchdog import (
    pid_file_path as _pid_file_path,
)
from secretary.head_registry import HeadRegistryConfigError, installed_heads
from secretary.memory import access as memory_access
from secretary.observer_root import OBSERVER_REPO_NAME, observer_root_repo
from secretary.projects.contract import (
    ContractVerdict,
)
from secretary.projects.contract import decide as _decide_broad_check_contract
from secretary.projects.integration_base import (
    IntegrationBaseError,
    resolve_integration_base,
    seed_ref_refusal,
)
from secretary.projects.integration_base import (
    is_exact_sha as _is_exact_ref_sha,
)
from secretary.routing_journal import (
    HEAD_FROM_CARD,
    HEAD_FROM_FALLBACK,
    HEAD_FROM_RECORD,
    HEAD_FROM_ROLE_DEFAULT,
    MODEL_UNKNOWN,
    HeadRun,
    head_run_from_profile,
)
from secretary.tasks import (
    TaskAudit,
    durability_dirt,
    specification_revision,
)
from triggered_agents.agents.pipeline.heads import (
    HeadRegistryError,
)
from triggered_agents.agents.pipeline.heads import (
    resolve_head_id as _resolve_head_id,
)
from triggered_agents.runtime import head as head_ops
from triggered_agents.runtime.codex_preflight import (
    CodexFanoutPolicyError,
    preflight_codex_launch,
)
from triggered_agents.runtime.head import (
    CODEX_TUI_MODE,
    HeadCommand,
    HeadCommandError,
    HeadSpec,
    HeadSpecError,
)
from triggered_agents.runtime.head import (
    OBSERVE_PANE_DISCONNECTED as _OBSERVE_PANE_DISCONNECTED,
)
from triggered_agents.runtime.head import (
    OBSERVE_READINESS_UNKNOWN as _OBSERVE_READINESS_UNKNOWN,
)
from triggered_agents.runtime.head import (
    PYTHON_SAFE_PATH_FLAG as _PYTHON_SAFE_PATH_FLAG,
)
from triggered_agents.runtime.head import (
    render_head_command as _render_head_command,
)
from triggered_agents.runtime.head import (
    with_pid_heartbeat as _with_pid_heartbeat,
)
from triggered_agents.runtime.head_runtime_backends import (
    UnknownHeadRuntimeError,
    build_head_runtime,
    head_runtime_name,
)
from triggered_agents.runtime.head_runtimes import ORCA_LEGACY_RUNTIME
from triggered_agents.runtime.launch_prefix import pythonpath_prefix
from triggered_agents.runtime.pane_host import (
    OrcaSessionHost,
    Pane,
    PaneHostError,
    SessionHost,
)
from triggered_agents.runtime.pane_host import (
    safe_command_label as _safe_command_label,
)
from triggered_agents.runtime.prompt_document import (
    PromptDocumentError,
)
from triggered_agents.runtime.prompt_document import (
    nudge_for as _nudge_for,
)
from triggered_agents.runtime.prompt_document import (
    write_prompt_document as _write_prompt_document,
)

_PYTHONPATH_PREFIX = pythonpath_prefix()
_CONTROL_PLANE_TASK_COMMAND = f"{_PYTHONPATH_PREFIX} python3 {_PYTHON_SAFE_PATH_FLAG} -m secretary task"

OBSERVER_WORKSPACE_DIR = OBSERVER_REPO_NAME
OBSERVER_REPO_BRANCH = "observers"

# How long a confirmed stop waits for a head to leave after each signal, and how often it looks: a
# stop is never called unconfirmed over a process that is in the middle of leaving.
HEAD_STOP_GRACE_SECONDS = 5.0
HEAD_STOP_POLL_SECONDS = 0.1

DESTRUCTIVE_VERDICTS = frozenset(
    {
        VitalityVerdict.CONFIRMED_STALL,
        VitalityVerdict.DEAD,
    }
)

# Preflight identity excludes the mutable baseline and pane-derived binding facts.
_PREPARED_SOURCE_IDENTITY_KEYS = (
    "version",
    "kind",
    "run_id",
    "head_run_fingerprint",
    "workspace",
    "role",
    "task_ref",
    "root",
)


def _blocked_actions_and_their_infrastructure_twins(*actions: str) -> tuple[str, ...]:
    """Each blocked action and the token its infrastructure-classified form writes.

    A card blocked by a bring-up now names the outcome's class in the action it wrote, so the
    requeue that brings it back to Ready has to recognise both forms as its own block: recognising
    only the historical one would leave a re-run looking like a fresh claim over a live attempt.
    """
    seen: dict[str, None] = {}
    for action in actions:
        seen.setdefault(action, None)
        seen.setdefault(_infrastructure_action(action), None)
    return tuple(seen)


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
    leaf: str = ""
    delivery_evidence: dict[str, Any] = field(default_factory=dict)
    head_run: dict[str, Any] = field(default_factory=dict)
    fallback_reason: str = ""


class InstanceCatalog:
    instance_dir: Path | None = None

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
            # The installation's own snapshot, not the checkout this module was imported from:
            # judging the live registry by that tree makes an unmerged commit stop production ticks.
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

    def broad_check_verdict(self, project: str) -> ContractVerdict:
        """This project's broad-check contract as one of the three named states (secretary-1458).

        The rules are `projects.contract`'s, the same implementation the worker's own
        `secretary check broad --module` resolves through, so a card is never handed out on a
        contract the worker would then refuse. Reading the binding and the adapter beside it is
        all this costs: no workspace, no head, no process. There is no candidate workspace at this
        point, so `workspace=None`: a question that needs one comes back as `undecidable` with its
        name on it rather than as an approval this side is not entitled to give.
        """
        return _decide_broad_check_contract(
            self.binding(project), instance=self.instance_dir or Path("."), workspace=None
        )

    def project_default_branch(self, project: str) -> str:
        """The branch this project's binding declares as its own default. No card is consulted."""
        branch = self.binding(project).get("default_branch")
        return str(branch or "main")

    def integration_base(self, project: str, override: str | None) -> str:
        """Where this card's increment lands: its PR base, history range, receipt base and merge.

        A card may override it only with a branch the project declares it integrates into — its
        default branch, or one of the binding's `integration_bases`. Anything else, a predecessor's
        `pipeline/*` card branch above all, is refused here with the reason on it (secretary-1541):
        opening a release pull request into a branch the project's workflows do not trigger for
        produces zero check-runs, which is indistinguishable from checks that have not appeared yet.
        """
        binding = self.binding(project)
        declared = binding.get("integration_bases")
        try:
            return resolve_integration_base(
                default_branch=str(binding.get("default_branch") or "main"),
                declared=list(declared) if isinstance(declared, list) else None,
                override=override,
            )
        except IntegrationBaseError as exc:
            raise HostError(
                f"project {project!r}: {exc}", bring_up_cause=CAUSE_BASE_BRANCH_CONTRACT
            ) from None

    def workspace_seed(self, project: str, task: dict[str, Any]) -> str:
        """The git ref this card's checkout is cut from.

        A reslice successor inherits its predecessor's unreleased content, so it starts from that
        candidate — `workspace.seed_ref`, a card branch or the exact object id that was assessed.
        A card with no seed starts from its integration base, which is what every card did before
        this field existed and what every ordinary card still does.
        """
        workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
        seed = str((workspace or {}).get("seed_ref") or "").strip()
        if not seed:
            return self.integration_base(project, (workspace or {}).get("base_branch"))
        refusal = seed_ref_refusal(seed)
        if refusal:
            raise HostError(
                f"project {project!r}: {refusal}", bring_up_cause=CAUSE_BASE_BRANCH_CONTRACT
            ) from None
        return seed

    def worker_head(self, task: dict[str, Any]) -> str:
        requested = task.get("routing", {}).get("head_override")
        head = self._resolved_head(
            str(requested)
            if requested
            else str(self._heads.get("role_defaults", {}).get("new_card") or "codex")
        )
        self._head_profile(head)
        return head

    def review_head(self, task: dict[str, Any]) -> str:
        requested = task.get("routing", {}).get("review_head_override")
        head = self._resolved_head(
            str(requested)
            if requested
            else str(self._heads.get("role_defaults", {}).get("reviewer") or "codex-reviewer")
        )
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
        self,
        task: dict[str, Any],
        *,
        role: str,
        head: str = "",
        workspace: str = "",
        failover: bool = False,
    ) -> HeadRun:
        """The launch record for one head of `role`: the profile id plus the configuration it is
        launched with, read from the same snapshot the launcher renders its command from.
        """
        routing = task.get("routing") or {}
        if role == "worker":
            override = routing.get("head_override")
            asked = self._resolved_head(
                str(override)
                if override
                else str(self._heads.get("role_defaults", {}).get("new_card") or "codex")
            )
        else:
            override = routing.get("review_head_override")
            asked = self._resolved_head(
                str(override)
                if override
                else str(self._heads.get("role_defaults", {}).get("reviewer") or "codex-reviewer")
            )
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
    """How this dispatcher delivers to a head and closes its pane, against whatever host it is on."""

    runtime: CommandHostRuntime
    workspace: str = ""
    prompt_file: str = ""
    adapter: str = ""
    role: str = ""
    before_send: Callable[[], head_ops.HeadRun | None] | None = None
    # The caller's acknowledgement arrives somewhere other than this delivery. Only the observer
    # wake sets it: it holds both proofs a delivery can have and quotes the delivery id in the
    # resume written from the turn it starts, so the weaker of the two must not refuse it.
    ack_out_of_band: bool = False

    def deliver(
        self,
        run: head_ops.HeadRun,
        pointer: head_ops.NudgePointer,
        *,
        host: SessionHost,
        subject: str,
    ) -> head_ops.HeadDelivery:
        # The framing is the rendered command's fact, not the profile's: a registry edited since the
        # launch would frame a prompt for a head this launch never ran, so the launcher's view wins.
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
                ack_out_of_band=self.ack_out_of_band,
            )
        except Exception as exc:
            # A source may already have been durably bound when a later delivery stage refuses. The
            # abort path gets this exact run so its intent cannot write the old unbound copy back.
            exc.head_run = post_delivery
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


#: Which backend a run, a spec or a name is held by. Shared with the mechanical-role driver rather
#: than kept here: one reader of the key, as there is one mapping from its value to a backend.
_head_runtime_name = head_runtime_name


def _durable_head_run(subject: Any) -> head_ops.HeadRun | None:
    """The run a record names at a workspace-scoped stop, or `None` when it names none.

    Workspace cleanup reads the run itself rather than a lifecycle run rebuilt around the record:
    the per-role builders invent a fresh identity for a field that holds nothing, and a stop has to
    act on the head that was actually raised or on nothing at all. So a field that is empty,
    unparseable, or carries no run id is `None` here — that is a workspace with no head of that
    role, not a head to go looking for.
    """
    if subject is None:
        return None
    if isinstance(subject, head_ops.HeadRun):
        return subject if subject.run_id else None
    if not isinstance(subject, dict) or not subject.get("run_id"):
        return None
    try:
        run = head_ops.HeadRun.from_json(subject)
    except (head_ops.HeadRunError, head_ops.TaskRefError, TypeError, ValueError):
        return None
    return run if run.run_id else None


class CommandHostRuntime:
    def __init__(self, catalog: InstanceCatalog, data_dir: Path, *, mode: str = "real") -> None:
        self.catalog = catalog
        self.data_dir = data_dir
        # TASK.md is a durable projection, so its feedback selector reads the same audit journal
        # as the dispatcher rather than depending on a live record or wall-clock ordering.
        self.audit = TaskAudit(data_dir)
        self.mode = mode
        # Where a head run is flushed the moment an operation commits it, ahead of the tick's own
        # save. Its durable-state owner installs this only while it holds the record's file: this
        # host has a record, not that file. Unset, a run reaches disk with the tick's records.
        self.commit_state: Callable[[], None] | None = None
        # The first preflight fixes a provider source's baseline before its pane exists. A launch
        # may attest the same run again immediately before opening that pane, but that second
        # read must not replace the durable baseline with a listing taken later in bring-up.
        # Codex also has a live ingress below; Claude has no ingress because its source is bound
        # by the retained HeadRun's read-only progress probe.
        self._prepared_provider_runs: dict[str, head_ops.HeadRun] = {}
        # The owner installs one entry before it asks this host to open a Codex pane, keyed by the
        # HeadRun id in that intent so a same-workspace respawn cannot inherit a predecessor's source.
        self._codex_provider_ingresses: dict[str, CodexProviderEventIngress] = {}
        # The boundaries a head's life is lived through, one per backend a profile can name. Held
        # rather than rebuilt per access, because the turn leases and the activity epoch are what
        # they hold: a runtime rebuilt on every access would have no memory of the turns it handed
        # out. Built lazily and cached by name, so a dispatcher whose registry names only
        # `orca-legacy` never constructs the other one — which is the whole of what "no profile is
        # on local-pty" costs at runtime.
        self._head_runtimes: dict[str, Any] = {}

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
            run,
            persist,
            stop=stop,
            block=block,
        )

    def poll_codex_provider_ingress(self, run: head_ops.HeadRun) -> None:
        """Best-effort read new provider events through the run-bound launch ingress."""
        ingress = self._codex_provider_ingresses.get(run.run_id)
        if ingress is None:
            # The collector is process-local: missing fan-out telemetry is not a lifecycle decision.
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
        """Lend this runtime a way to flush the durable state, for as long as the caller holds it."""
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
            prepared = _prepare_claude_provider_progress_source(run)
            self._prepared_provider_runs.setdefault(run_id, prepared)
            return prepared
        if spec.adapter != "codex":
            return run
        return preflight_codex_launch(profile, workspace, run)

    def _preflight_launch_run(
        self,
        head: str,
        *,
        role: str,
        workspace: str,
        task_ref: head_ops.TaskRef,
        pid_file: str,
        run_id: str,
    ) -> head_ops.HeadRun:
        """Attest this bring-up's run, then hand it the source its launch was prepared with.

        The worker and reviewer launch path only.  The observer bring-up has its own delivery
        contour and keeps calling `preflight_codex_run` directly, so this repair cannot change what
        an observer launch hands its handoff.
        """
        return self._retain_prepared_provider_source(
            self.preflight_codex_run(
                head,
                role=role,
                workspace=workspace,
                task_ref=task_ref,
                pid_file=pid_file,
                run_id=run_id,
            )
        )

    def _retain_prepared_provider_source(self, attested: head_ops.HeadRun) -> head_ops.HeadRun:
        """Keep the pre-pane provider source this exact run was already prepared with.

        One launch preflights twice: once to fix the intent on disk, and once here, inside the host
        call that opens the pane.  Both attestations must run — workspace trust is a hard pre-pane
        requirement and is rechecked by the second — but only the first descriptor is durable. It is
        the one the ingress binds its session against and the one a recovery reads, while the second
        enumerates the session root again and legitimately sees whatever journals other heads opened
        in between.  Handing that second baseline back as this run's source is what made a rework
        relaunch fail its own handoff with two conflicting unbound descriptors for one HeadRun.

        Retention is fenced on the exact run: the prepared descriptor is kept only when it names this
        run id, spec, workspace, role, task ref and pid file, and describes the same source identity
        and root.  Anything else stays the fresh attestation and is refused downstream as the
        identity conflict it is, and a source already bound to a session is preserved rather than
        replaced by a new unbound one.
        """
        ingress = self._codex_provider_ingresses.get(attested.run_id)
        prepared = ingress.run if ingress is not None else self._prepared_provider_runs.get(attested.run_id)
        if prepared is None:
            return attested
        if (
            not prepared.same_run(attested)
            or prepared.spec != attested.spec
            or prepared.workspace != attested.workspace
            or prepared.task_ref != attested.task_ref
            or prepared.role != attested.role
            or prepared.pid_file != attested.pid_file
        ):
            return attested
        source_key = "provider_source" if attested.spec.adapter == "codex" else "provider_progress_source"
        prepared_source = prepared.fanout_policy.get(source_key)
        fresh_source = attested.fanout_policy.get(source_key)
        if not isinstance(prepared_source, dict) or not isinstance(fresh_source, dict):
            # A fresh attestation that established no source of its own has nothing to hand over,
            # and never erases the durable one; the handoff merge keeps what is already on disk.
            return attested
        if any(prepared_source.get(key) != fresh_source.get(key) for key in _PREPARED_SOURCE_IDENTITY_KEYS):
            return attested
        policy = dict(attested.fanout_policy)
        policy[source_key] = dict(prepared_source)
        return attested.with_fanout_policy(policy)

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
        base = self.catalog.integration_base(project, task.get("workspace", {}).get("base_branch"))
        seed = self.catalog.workspace_seed(project, task)
        workspace = self.restore_workspace(task, worker_id)
        reused = Path(workspace).exists()
        if reused:
            self._validate_resumable_workspace(task, workspace)
        else:
            if require_existing_workspace:
                # The card was requeued onto the checkout its own last attempt preserved, and that
                # checkout is gone. No host repairs it and no later tick finds it: this is the one
                # bring-up family that is about the card rather than the host.
                raise HostError("resume workspace is missing", bring_up_cause=CAUSE_WORKSPACE_CONTRACT)
            workspace = self._create_workspace(project, worker_id, seed, expected=workspace)
            self._set_worker_branch(workspace, _legacy_worker_branch(task["ref"]))
            self._run_setup(project, workspace)
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
            # Recorded instead of re-reading the registry, which a later edit would answer differently.
            "run": launched.run,
            "delivery_evidence": dict(launched.delivery_evidence),
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
            # Same family as the missing resume workspace above: the checkout this card's rework
            # continues in is not there, which is this card's own bring-up contract, not the host's.
            raise HostError("rework workspace is missing", bring_up_cause=CAUSE_WORKSPACE_CONTRACT)
        base = self.catalog.integration_base(task["project"], task.get("workspace", {}).get("base_branch"))
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
            return str(self.data_dir / "dispatcher" / OBSERVER_WORKSPACE_DIR / _request_token(reference))
        root = Path(
            os.environ.get("SECRETARY_DISPATCHER_WORKSPACES_ROOT", str(Path.home() / "orca" / "workspaces"))
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
                    "git",
                    "-C",
                    str(repo),
                    "init",
                    "--quiet",
                    "--initial-branch",
                    OBSERVER_REPO_BRANCH,
                ],
                "observer repo init",
            )
            self._run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=secretary-dispatcher",
                    "-c",
                    "user.email=dispatcher@localhost",
                    "commit",
                    "--quiet",
                    "--allow-empty",
                    "-m",
                    "observer root",
                ],
                "observer repo commit",
            )
        self._run_json(["orca", "repo", "add", "--path", str(repo), "--json"])
        return repo

    def _observer_workspace_registered(self, workspace: str) -> bool:
        """Whether Orca knows this path as a worktree of its own.

        Only `selector_not_found` reads as "not registered". Any other failure is Orca declining to
        answer, and an unanswered question must not pass for a free path.
        """
        try:
            self._run_json(["orca", "worktree", "show", "--worktree", f"path:{workspace}", "--json"])
        except HostError as exc:
            if "selector_not_found" in str(exc):
                return False
            raise
        return True

    def _create_observer_workspace(self, reference: str) -> Path:
        """The observer's workspace, registered with Orca and at the path the record already names."""
        workspace = Path(self.observer_workspace(reference))
        if self._observer_workspace_registered(str(workspace)):
            return workspace
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
        repo = self._observer_repo()
        result = self._run_json(
            [
                "orca",
                "worktree",
                "create",
                "--repo",
                f"path:{repo}",
                "--name",
                workspace.name,
                "--base-branch",
                OBSERVER_REPO_BRANCH,
                "--setup",
                "skip",
                "--no-parent",
                "--json",
            ]
        )
        worktree = result.get("worktree") if isinstance(result.get("worktree"), dict) else result
        path = worktree.get("path") if isinstance(worktree, dict) else None
        if not isinstance(path, str) or not path:
            raise HostError("orca did not return an observer workspace path")
        if Path(path) != workspace:
            # The launch intent already names `workspace`, and a tick that dies now can only find the
            # head through it: a workspace elsewhere is a deferred bring-up, not a head nothing names.
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
        if lifecycle_run.spec.adapter in {"codex", "claude"}:
            try:
                attested = self.preflight_codex_run(
                    head,
                    role=OBSERVER_ROLE,
                    workspace=str(workspace),
                    task_ref=lifecycle_run.task_ref,
                    pid_file=pid_file,
                    run_id=lifecycle_run.run_id,
                )
                # Codex keeps its existing fan-out ingress handoff. Claude has no ingress: its
                # pre-pane descriptor is retained here, then bound by the exact-run cursor probe.
                lifecycle_run = (
                    self._retain_prepared_provider_source(attested)
                    if lifecycle_run.spec.adapter == "claude"
                    else attested
                )
            except CodexFanoutPolicyError as exc:
                raise HostError(str(exc)) from None
        heartbeat = self._run_heartbeat_identity(lifecycle_run, OBSERVER_ROLE)
        if self.mode == "noop":
            return {
                "workspace": str(workspace),
                "handle": f"noop:{head}:{workspace.name}:{OBSERVER_PROMPT_FILE}",
                "leaf": "",
                "prompt_delivered": False,
                "delivery_evidence": {},
                "pid_file": pid_file,
                "run": run,
                "head_run": lifecycle_run.to_json(),
            }
        # Drop a predecessor's pid before the new head can be read as this launch's liveness.
        _clear_head_heartbeat(pid_file)
        try:
            grant = memory_access.issue_grant(
                lifecycle_run,
                memory_access.sprint_subject(reference, list(sprint.get("reservations") or [])),
                data_dir=self.data_dir,
            )
        except memory_access.MemoryAccessError as exc:
            raise HostError(f"memory access binding could not be issued: {exc}") from None
        launch_identity = {**(identity or {}), **grant.launch_identity}
        launch = self.catalog.head_launch(
            head,
            OBSERVER_PROMPT_FILE,
            workspace=str(workspace),
            role=OBSERVER_ROLE,
            launch_prompt=_observer_launch_prompt(),
            identity=launch_identity,
        )
        lifecycle_run = self._open_head_pane(
            lifecycle_run,
            f"{reference} observer",
            _with_pid_heartbeat(launch.command, pid_file, identity=heartbeat),
        )
        ingress = self._codex_provider_ingress(lifecycle_run)
        if ingress is not None:
            # The returned pane leaf is part of the run identity the event source is bound to:
            # persist it before the first provider prompt, not in the post-launch save below.
            ingress.commit_run(lifecycle_run)
        _bind_head_heartbeat(pid_file, expected=heartbeat, leaf=lifecycle_run.leaf)
        delivered = False
        delivery_evidence: dict[str, Any] = {}
        if launch.prompt_after_start:
            # What this bring-up persists is the run its ingress bound, not the lifecycle the
            # delivery moved: the observer's watchdog adopts exactly the record the source binding
            # was made durable under, and a launcher returning a later value would hand adoption a
            # run the crash-era intent never saw.
            bound_run = lifecycle_run

            def bind_before_send() -> head_ops.HeadRun:
                nonlocal bound_run
                if ingress is not None:
                    bound_run = head_ops.post_delivery_run(
                        lifecycle_run,
                        ingress.bind_before_delivery(),
                    )
                return bound_run

            failure: Exception | None = None
            try:
                receipt = self.head_runtime_for(lifecycle_run).deliver(
                    lifecycle_run,
                    head_ops.NudgePointer(text=_observer_launch_prompt()),
                    transport=self._head_transport(
                        str(workspace),
                        OBSERVER_PROMPT_FILE,
                        launch.adapter or "codex",
                        OBSERVER_ROLE,
                        before_send=bind_before_send if ingress is not None else None,
                    ),
                    subject="observer-launch",
                )
                if not receipt.ok:
                    # A non-ok receipt does not have to carry a `failure`: `HEAD_DRAINING` is a
                    # refusal with no operation error behind it, and reading success off
                    # `failure is None` would have counted it as a delivered launch prompt.
                    failure = receipt.failure or HostError(
                        f"the observer launch prompt was refused: {receipt.reason}"
                    )
            except (TuiDeliveryError, HostError) as exc:
                failure = exc
            if failure is None:
                lifecycle_run = bound_run
                delivered = True
                delivery_evidence = _delivery_evidence_json(receipt.delivery, "observer-launch")
            else:
                exc = failure
                evidence = _delivery_evidence_json(exc, "observer-launch")
                try:
                    self._stop_observer_terminals(
                        str(workspace),
                        pid_file=pid_file,
                        run=lifecycle_run,
                        role=OBSERVER_ROLE,
                        task=f"sprint:{reference}",
                        leaf=lifecycle_run.leaf,
                    )
                except Exception as stop_exc:
                    # The pane is still up and this dict is the only pointer to it: reporting a plain
                    # bring-up failure would leave the sprint headless and open a second head beside it.
                    raise ObserverLaunchAborted(
                        f"{exc}; observer terminal stop failed: {stop_exc}",
                        handle=lifecycle_run.handle,
                        leaf=lifecycle_run.leaf,
                        workspace=str(workspace),
                        pid_file=pid_file,
                        run=run,
                        evidence=evidence,
                    ) from None
                raise ObserverLaunchAborted(str(exc), evidence=evidence) from None
        return {
            "workspace": str(workspace),
            "handle": lifecycle_run.handle,
            "leaf": lifecycle_run.leaf,
            "prompt_delivered": delivered,
            "delivery_evidence": delivery_evidence,
            "pid_file": pid_file,
            "run": run,
            # Delivery owns the source handoff; this launcher adds only the pane facts it proved, and
            # returning `lifecycle_run` keeps observer adoption on the run the ingress persisted.
            "head_run": lifecycle_run.to_json(),
        }

    def stop_observer(self, record: Any) -> None:
        """End one observer head and give back what its bring-up took.

        Unconditional: a freeze, a closed sprint, a policy refusal and an emergency replacement all
        mean "end this now", and none of them may be refused because the head happens to be busy.
        What it owes the head runtime afterwards is the forgetting that the `stop` verb does for
        itself — this teardown is Orca's worktree removal, not that verb, and a runtime that lives
        as long as the production loop would otherwise keep one epoch, one output mark and one
        admission entry per head ever launched.
        """
        if self.mode == "noop":
            return
        self._stop_observer_head(record)
        observer_run = self._observer_lifecycle_run(record)
        self.head_runtime_for(observer_run).forget_head(observer_run.run_id)

    def _stop_observer_head(self, record: Any) -> None:
        """Give Orca back the pane, the process and the worktree one observer bring-up took."""
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
            if record.handle:
                self._close_observer_pane(record, record.handle)
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
        # Terminal stop alone cannot prove a heartbeat-wrapped head died: confirm before removing it.
        self._confirm_head_process_gone(
            pid_file,
            run=observer_run,
            role=OBSERVER_ROLE,
            task=f"sprint:{getattr(record, 'sprint', '')}",
            leaf=observer_leaf,
        )
        self._run_json(["orca", "worktree", "rm", "--worktree", f"path:{workspace}", "--force", "--json"])

    def observer_activity_epoch(self, record: Any) -> int:
        """This observer head's activity epoch, to hand back to a stop that must only run if quiet.

        Per head, never the runtime's own counter: another sprint's observer doing something must
        not make this one look busy.

        Asked of the backend rather than of its memory where the backend can answer that way. A
        runtime object lives for one tick, so `activity.epoch` alone reads an epoch of zero for
        every head this process did not start; a backend whose head has a durable witness offers
        `activity_epoch`, which recovers the real one before answering (secretary-1479). The
        legacy backend has no such witness and no such method, and there it is `activity.epoch`
        exactly as before — the fallback is the whole of what a backend without one can say.
        """
        if self.mode == "noop":
            return 0
        observer_run = self._observer_lifecycle_run(record)
        runtime = self.head_runtime_for(observer_run)
        durable = getattr(runtime, "activity_epoch", None)
        if callable(durable):
            return int(durable(observer_run))
        return runtime.activity.epoch(observer_run.run_id)

    def stop_observer_if_quiescent(
        self, record: Any, expected_activity_epoch: int, head_process_alive: bool
    ) -> bool:
        """End this observer only while it is still quiet. False means it was not, and nothing ran.

        The check and the teardown are one critical section inside the head runtime: this head's
        epoch still where the caller saw it, the turn settled, admission closed, and only then the
        teardown — so a delivery cannot land between deciding the head is finished and taking its
        pane away. The teardown itself is `stop_observer` unchanged, because an observer's stop is
        Orca's whole-worktree teardown plus its pid confirmation, not a pane close; it runs inside
        that same section rather than after it.

        Both facts come from the caller and neither is re-read here. `head_process_alive` is the
        pid-heartbeat answer the caller already had — this host cannot produce it, because what it
        can ask Orca about is a pane, and a pane says `busy` for a dead head's leftover shell just
        as loudly as for a working one.
        """
        if self.mode == "noop":
            return True
        observer_run = self._observer_lifecycle_run(record)
        receipt = self.head_runtime_for(observer_run).stop_if_quiescent(
            observer_run,
            head_ops.StopInitiator(actor=STOPPED_BY_DISPATCHER),
            expected_activity_epoch=expected_activity_epoch,
            head_process_alive=head_process_alive,
            teardown=lambda: self.stop_observer(record),
        )
        return receipt.ok

    def observer_status(self, record: Any) -> dict[str, Any]:
        """Read the observer pane's output clock and whether it is ready for a prompt."""
        if self.mode == "noop":
            return {}
        if not record.workspace or not (record.handle or record.leaf):
            raise HostError("observer record names no terminal to read")
        observer_run = self._observer_lifecycle_run(record)
        seen = self.head_runtime_for(observer_run).observe(observer_run)
        if seen.reason == _OBSERVE_PANE_DISCONNECTED:
            raise HostError("observer terminal is not connected")
        if seen.reason == _OBSERVE_READINESS_UNKNOWN:
            # A probe that failed is not a working observer; raising puts it on the bounded failure path.
            raise HostError("observer terminal readiness could not be read")
        if not seen.ok:
            # What is left once the pane's own answers are handled is the pane not being in the
            # inventory at all. An inventory that could not be read is no longer folded in here:
            # the session manager's refusal travels out of the observation as itself, which is what
            # this path did before the boundary existed and what its callers already classify.
            raise HostError("observer terminal is not in the inventory of its workspace")
        status: dict[str, Any] = {"idle": seen.readiness == READINESS_READY}
        if seen.last_output_at:
            status["last_activity"] = seen.last_output_at
        return status

    def _observer_lifecycle_run(self, record: Any) -> head_ops.HeadRun:
        """This observer as the head runtime addresses it: its own run, at the pane it is in.

        The persisted run is preferred, because the turn leases and the activity epoch the runtime
        keeps are per head and a fresh identity every tick would keep losing them. A record written
        before that run existed still has a workspace and a pane, which is all an observation needs.
        """
        workspace = str(getattr(record, "workspace", "") or "")
        handle = str(getattr(record, "handle", "") or "")
        leaf = str(getattr(record, "leaf", "") or "")
        stored = getattr(record, "head_run", {})
        run: head_ops.HeadRun | None = None
        if isinstance(stored, dict) and stored.get("run_id"):
            try:
                run = head_ops.HeadRun.from_json(stored)
            except (head_ops.HeadRunError, head_ops.TaskRefError, TypeError, ValueError):
                run = None
        if run is None:
            # Deliberately not resolved through the catalog: an observation must not fail because
            # a profile was renamed since this observer started, and nothing an observation reads
            # depends on the spec. A delivery that does need the adapter is told it explicitly.
            run = head_ops.HeadRun(
                run_id=head_ops.new_run_id(),
                spec=HeadSpec(
                    profile_id=str(getattr(record, "head", "") or "unknown-observer"),
                    adapter="unknown",
                ),
                workspace=workspace,
                task_ref=head_ops.TaskRef.sprint(str(getattr(record, "sprint", "") or "unknown-sprint")),
                role=OBSERVER_ROLE,
                pid_file=str(getattr(record, "pid_file", "") or ""),
            )
        return replace(run, workspace=workspace or run.workspace, handle=handle, leaf=leaf)

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
        # Claude journals appear asynchronously after the pane starts. Bind only the one source
        # selected from this exact run's persisted pre-pane baseline, then retain that result on
        # the observer record before its wake reducer evaluates the cursor.
        if run.spec.adapter == "claude":
            updated = _bind_claude_provider_progress_source(run)
            if updated != run:
                record.head_run = updated.to_json()
                if self.commit_state is not None:
                    self.commit_state()
                run = updated
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
            "dispatcher-owned exact-SHA gate receipt first. Suppress a routine broad rerun only when "
            "that receipt exists; "
            "none/noop/missing evidence proves no broad suite, so run or request appropriate validation "
            "when the decision needs it. Keep a worker-local broad receipt with the worker. It does "
            "not suppress that rerun. Take the next semantic step, then record resume."
        )
        if delivery_id and through_event:
            message += (
                " Acknowledge this delivery in that resume with --delivery-id "
                f"{delivery_id} --through-event {through_event}."
            )
        # The wakes this sprint has already lost travel with the wake that reaches the head, so the
        # resume written from this turn reports what happened rather than what a head could see.
        evidence_line = _observer_delivery_evidence_summary(delivery) if delivery is not None else ""
        if evidence_line:
            message += f" Sprint delivery evidence to carry into your closing resume: {evidence_line}."
        try:
            adapter = self._prompt_adapter(getattr(record, "run", {}), str(getattr(record, "head", "")))
            # A wake carries both proofs a delivery can have, and either one confirms it. The head
            # is live and working, so the screen evidence this used to rely on alone is the weaker
            # of them: Orca reports a working Codex as idle, and the pane the wake is delivered
            # into is precisely the pane that is printing. The provider's own record of the turn
            # is what a launch has always been confirmed by, and it says the same thing about a
            # wake. What stays out of band is the causal acknowledgement, not the delivery: the
            # observer still quotes the delivery id in the resume it writes from this turn.
            waking = replace(self._observer_lifecycle_run(record), handle=current)
            receipt = self.head_runtime_for(waking).deliver(
                waking,
                head_ops.NudgePointer(text=message),
                transport=self._head_transport(
                    workspace,
                    adapter=adapter,
                    role=OBSERVER_ROLE,
                    ack_out_of_band=True,
                ),
                subject="observer-wake",
            )
        except TuiDeliveryError as exc:
            failure = HostError(f"observer wake was not delivered: {exc}")
            # The lifecycle stores this beside the sprint: the delivery boundary's own evidence
            # (terminal identity, payload size and hash, stage, fingerprints) and no prompt text.
            failure.evidence = getattr(exc, "evidence", None)
            raise failure from None
        if not receipt.ok:
            failure = HostError(f"observer wake was not delivered: {receipt.reason}")
            failure.evidence = receipt.evidence
            raise failure from None
        return receipt.delivery

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
        """End the observer this workspace holds, through the backend that observer is held by.

        For a legacy observer this is still one verb for the whole workspace rather than a close
        per handle: `close_pane` answers `tab_not_found` for a pane the runtime never gave a UI
        tab, so a per-handle close reports a stop that worked as a stop that failed. An observer
        raised on a supervised backend owns no pane at all, and is ended by its own run's stop —
        which is why the choice is made from the persisted run rather than from a default.
        """
        if run is not None:
            self._guard_head_run(run, role, pid_file=pid_file, task=task, leaf=leaf)
        self._stop_recorded_heads(workspace, [(run, role or OBSERVER_ROLE)])

    def _close_observer_pane(self, record: Any, handle: str) -> None:
        """End an observer that is nothing but a pane handle, through the head runtime.

        The record predates the intent naming a workspace, so there is no worktree to take down and
        no leaf to re-find the pty by: the handle is the whole of what is left. A run already
        confirmed gone gets a fresh identity, because the stop must not skip a pane the record still
        names.
        """
        run = self._observer_lifecycle_run(record)
        if run.settled:
            run = replace(
                run,
                run_id=head_ops.new_run_id(),
                lifecycle=head_ops.SPAWNED,
                stopped_by=None,
            )
        receipt = self.head_runtime_for(run).stop(
            replace(run, handle=handle, leaf=""),
            head_ops.StopInitiator(actor=STOPPED_BY_DISPATCHER),
        )
        if not receipt.ok:
            raise HostError(f"observer terminal close failed: {receipt.reason}")

    def _observer_run(self, head: str, workspace: str) -> dict[str, Any]:
        try:
            return self.catalog.observer_run(head, workspace=workspace).to_json()
        except (HostError, AttributeError, KeyError, TypeError):
            return HeadRun(
                role=OBSERVER_ROLE, head=head, adapter="unknown", model_source=MODEL_UNKNOWN
            ).to_json()

    def provider_progress(self, task: dict[str, Any], record: DispatcherRecord, kind: str) -> dict[str, str]:
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
        # The provider creates its transcript asynchronously, so binding uses only the durable
        # pre-pane baseline on the HeadRun; ambiguity stays typed unavailable, never a guess.
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
                # The retained worker is provably gone (no orca session, pid heartbeat resolves to a
                # dead pid), so there is no second writer to freeze. The freeze path here would loop
                # `review-launch-aborted` over a head that can never confirm suspended.
                pass
            elif record.worker_continuation.retained:
                # A retained worker is already SIGSTOPed and its conversation is what a red verdict
                # continues. Confirm that suspension rather than trusting the record.
                self.confirm_worker_retained(record)
            else:
                self._freeze_worker(record)
        except HostError as exc:
            # The reviewer pane is up and the worker would not stop: neither head can be reported as
            # absent, so the pane goes back with the failure and the caller keeps its launch intent.
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
            fallback_reason=launched.fallback_reason,
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
            # A recovered, already-bound source is consumed before another delivery can act. An
            # unbound source belongs to a prior busy pre-send and is bound by the same boundary.
            if ingress.source.get("state") == "bound":
                ingress.poll()
        document, nudge = self._review_document(task, record)
        try:
            receipt = self.head_runtime_for(run).deliver(
                run,
                head_ops.NudgePointer(text=nudge, document=str(document)),
                transport=self._head_transport(
                    workspace,
                    str(document),
                    self._prompt_adapter(intent.get("run"), record.review_head),
                    "reviewer",
                    before_send=ingress.bind_before_delivery if ingress is not None else None,
                ),
                subject="reviewer-launch",
            )
        except (TuiDeliveryError, HostError) as exc:
            failure = HostError(f"retained reviewer document nudge was not delivered: {exc}")
            failure.evidence = _delivery_evidence_json(exc, "reviewer-launch")
            raise failure from None
        if not receipt.ok:
            failure = HostError(f"retained reviewer document nudge was not delivered: {receipt.reason}")
            failure.evidence = _delivery_evidence_json(receipt.failure, "reviewer-launch")
            raise failure from None
        return {
            "handle": receipt.run.handle,
            "leaf": receipt.run.leaf,
            "head_run": receipt.run.to_json(),
            "delivery_evidence": _delivery_evidence_json(receipt.delivery, "reviewer-launch"),
        }

    def worker_status(self, task: dict[str, Any], record: DispatcherRecord) -> dict[str, Any]:
        return _command_terminal_status(self, task, record, kind="worker")

    def review_status(self, task: dict[str, Any], record: DispatcherRecord) -> dict[str, Any]:
        return _command_terminal_status(self, task, record, kind="review")

    def stop_review(self, record: DispatcherRecord, initiator: str = STOPPED_BY_DISPATCHER) -> None:
        """End the reviewer's lifecycle alone. `stop` would take the whole worktree down with it,
        which on a red verdict means killing the checkout's terminals the worker is about to get
        back. Closing the reviewer's own split leaf removes that pane and leaves the rest alone.
        """
        if self.mode == "noop" or not (record.review_handle or record.review_leaf or record.review_pid_file):
            return
        self.stop_head(record, "review", initiator)

    def head_commit(self, record: DispatcherRecord) -> str:
        """Commit the workspace checkout currently sits on, or "" when it cannot be read. Pinned
        at review start and re-read at merge time so a verdict can be tied to a code state."""
        if self.mode == "noop" or not record.workspace:
            return ""
        try:
            completed = self._run(["git", "-C", record.workspace, "rev-parse", "HEAD"], "review head sha")
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
        if not _same_repo(repo, Path(self.catalog.instance_dir)):
            return False
        base = self.catalog.integration_base(task["project"], task.get("workspace", {}).get("base_branch"))
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
                [
                    "git",
                    "-C",
                    record.workspace,
                    "merge-base",
                    "--is-ancestor",
                    reviewed_commit,
                    current_commit,
                ],
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

    def rerun_failed_ci(self, task: dict[str, Any], record: DispatcherRecord, result: GateResult) -> None:
        """The gate owns the GitHub Actions write needed to retry its classified failed run."""
        _rerun_failed_ci(self, result)

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

    def retained_workspace_state(self, task: dict[str, Any], record: DispatcherRecord) -> dict[str, Any]:
        """Describe the retained worker checkout of a card recovered without a live head.

        Read-only by construction: nothing here creates, re-seeds, resets or moves a branch. A
        checkout that cannot be bound to this card is reported unbound with a typed reason, so the
        caller refuses rather than recreating the work from base somewhere else (secretary-1544).
        The four facts the recovery is allowed to rest on are the ones returned: the path, the
        branch, whether the tree is dirty, and the exact candidate commit.
        """
        expected_branch = _legacy_worker_branch(task["ref"])
        workspace = record.workspace or self.restore_workspace(task, record.worker)
        state: dict[str, Any] = {
            "workspace": workspace,
            "expected_branch": expected_branch,
            "branch": "",
            "dirty": None,
            "sha": "",
            "bound": False,
            "reason": "",
            "detail": "",
        }
        if self.mode == "noop":
            state.update(bound=True, branch=expected_branch, dirty=False)
            return state
        if not workspace or not Path(workspace).is_dir():
            state["reason"] = "workspace_missing"
            return state
        try:
            self._validate_resumable_workspace(task, workspace)
        except HostError as exc:
            state["reason"] = "workspace_unbindable"
            state["detail"] = scrub_host_output(str(exc))
            return state
        state["branch"] = expected_branch
        try:
            tree = self._run(
                ["git", "-C", workspace, "status", "--porcelain"], "retained workspace tree"
            ).stdout
            sha = self._run(
                ["git", "-C", workspace, "rev-parse", "HEAD"], "retained workspace candidate"
            ).stdout.strip()
        except HostError as exc:
            state["reason"] = "workspace_unreadable"
            state["detail"] = scrub_host_output(str(exc))
            return state
        state["dirty"] = bool(durability_dirt(tree))
        state["sha"] = sha
        if not sha:
            state["reason"] = "candidate_unknown"
            return state
        state["bound"] = True
        return state

    def restore_workspace(self, task: dict[str, Any], worker: str) -> str:
        """Where this card's worker checkout lives, new or already cut.

        The namespace under the workspaces root is Orca's: <root>/<repo registration name>/<worktree
        name>, where the registration name is the binding's `orca_binding` and not the Secretary
        project id — the two spellings differ and the id is not what decides where the checkout goes.
        """
        if self.mode == "noop":
            return str(self.data_dir / "dispatcher" / "workspaces" / worker)
        root = Path(
            os.environ.get("SECRETARY_DISPATCHER_WORKSPACES_ROOT", str(Path.home() / "orca" / "workspaces"))
        )
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
        base = self.catalog.integration_base(task["project"], task.get("workspace", {}).get("base_branch"))
        if _validation_ci(self, task) == "github":
            if self._no_diff_research_delivery_is_complete(task, record):
                return
            self._merge_github_pr(task, record, branch, base)
            return
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        if _same_repo(repo, Path(self.catalog.instance_dir)):
            self._complete_green_instance_repo(record, branch, base, repo)
            return
        # Publish onto the card's integration base (a non-fast-forward push is rejected, never
        # force-landed), then fast-forward the checkout: that is how a merged self-modification
        # reaches the next oneshot tick. The base is read from the card rather than hard-coded to
        # `main`: `integration_bases` makes a non-default base sanctioned for the first time
        # (secretary-1541), and pushing a card that declared one onto `main` anyway would land an
        # increment on a branch it was never validated against.
        self._run(["git", "-C", record.workspace, "push", "origin", f"{branch}:{base}"], "merge push")
        self._run(["git", "-C", str(repo), "fetch", "origin", base], "post-merge fetch")
        self._run(
            ["git", "-C", str(repo), "merge", "--ff-only", f"origin/{base}"], "post-merge fast-forward"
        )

    def _no_diff_research_delivery_is_complete(self, task: dict[str, Any], record: DispatcherRecord) -> bool:
        """Whether a dispatcher-dispatched research candidate has no delivery effect left.

        A workflow-dispatch entry is written only by the no-diff research gate, and the release
        gate has just accepted its exact-SHA receipt.  When that receipt names the same base and
        candidate commit, GitHub has no PR to merge because there is literally nothing to land.
        A different candidate SHA still matters even if its tree is identical: its own commit
        cannot be delivered through a no-PR path, so make that unsupported case explicit instead
        of pretending a GitHub PR merge can land it.
        """
        if task.get("type") != "research":
            return False
        dispatch = getattr(record, "gate_workflow_dispatch", {})
        receipt = getattr(record, "gate_attestation", {})
        if not isinstance(dispatch, dict) or not isinstance(receipt, dict):
            return False
        dispatched_sha = str(dispatch.get("sha") or "")
        candidate_sha = str(receipt.get("validated_sha") or "")
        base_sha = str(receipt.get("base_sha") or "")
        if (
            not _is_exact_sha(dispatched_sha)
            or not _is_exact_sha(candidate_sha)
            or not _is_exact_sha(base_sha)
            or dispatched_sha != candidate_sha
        ):
            return False
        if candidate_sha == base_sha:
            return True
        raise HostError(
            "base-identical research candidate owns commits and cannot complete without a pull request"
        )

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

    def _require_pr_base(self, record: DispatcherRecord, branch: str, base: str) -> None:
        """Refuse the merge unless the open pull request for `branch` targets `base`.

        Unreadable is refused too: the delivery boundary is irreversible, and "the backend would not
        say where this lands" is not evidence that it lands on the integration base.
        """
        completed = self._run(
            ["gh", "pr", "view", branch, "--json", "baseRefName", "-q", ".baseRefName"],
            "merge pr base",
            cwd=Path(record.workspace),
        )
        observed = (completed.stdout or "").strip()
        if observed != base:
            raise HostError(
                f"pull request for {branch!r} targets {observed or '(unreadable)'!r}, not the "
                f"integration base {base!r}; nothing was merged"
            )

    def _merge_github_pr(
        self, task: dict[str, Any], record: DispatcherRecord, branch: str, base: str
    ) -> None:
        """Land a github-CI project through its PR, then fast-forward the project's own checkout so the
        next worktree bases on the merged tree.

        gh honours branch protection and refuses to merge while required checks are unsatisfied. The
        checkout tracks the project's default branch, not the card's base, and the refresh stays
        best-effort: the card is already merged by then, so a failed refresh is not the card's failure.

        `gh pr merge` lands the pull request in *its own* base, whatever that is, so the base is read
        back and required to be this card's integration base before the irreversible call is made
        (secretary-1541). A pull request still pointing somewhere else — a card branch a stale PR was
        opened against — is refused here rather than merged into a branch nothing releases from.
        """
        self._require_pr_base(record, branch, base)
        self._run(["gh", "pr", "merge", branch, "--merge"], "merge pr", cwd=Path(record.workspace))
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        default_branch = self.catalog.project_default_branch(task["project"])
        # `gh pr merge` is the irreversible delivery boundary. Refreshing this checkout afterwards is
        # only a convenience for future worktree bases, and a preserved local commit can make
        # ff-only impossible: never report an already-merged card as failed because it did not apply.
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
        # A workspace stop kills every pane it contains. Fence every recorded head before the first
        # destructive call, not afterwards when a live process may already have lost its pane.
        for pid_file, run, kind, leaf in heartbeats:
            self._guard_head_run(run, kind, pid_file=pid_file, leaf=leaf)
        self._stop_recorded_heads(record.workspace, [(run, kind) for _, run, kind, _ in heartbeats])
        for pid_file, run, kind, leaf in heartbeats:
            self._confirm_head_process_gone(
                pid_file,
                run=run,
                role=kind,
                leaf=leaf,
            )

    def _stop_recorded_heads(self, workspace: str, runs: Sequence[tuple[Any, str]]) -> None:
        """Take one workspace's heads down, each through the backend that head is held by.

        The one place workspace-scoped cleanup chooses a backend, and it chooses from the durable
        run the record names rather than from the profile a later registry would resolve or from
        the product default. That is the same rule the per-head verbs follow: a head raised on one
        backend has to be stopped through that backend, and a registry repointed while it ran must
        not send its stop somewhere it never lived.

        A supervised head owns no pane, so nothing about a worktree can end it — it is ended by its
        own run's `stop`, and a refusal is raised rather than absorbed, because the callers of this
        are exactly the ones that go on to remove the worktree or open a replacement head.

        Orca's own by-worktree teardown is left for what it is for: a workspace whose heads are
        legacy ones, which is what that call ends, and a workspace that names no run at all — a
        bring-up that never got as far as a durable run, or a record written before heads were
        recorded — where the panes Orca knows about are the only thing there can be to stop. A
        workspace whose every recorded head is supervised has no pane to give back, and asking
        Orca to tear it down would be a call about a container this product did not put anything in.
        """
        named = [(_durable_head_run(run), role) for run, role in runs]
        live = [(run, role) for run, role in named if run is not None]
        for run, role in live:
            if _head_runtime_name(run) == ORCA_LEGACY_RUNTIME:
                continue
            if run.settled:
                # Its own stop already ran and was committed, so its run directory may be gone;
                # asking a supervisor that no longer exists would only wait out the confirmation
                # bound to learn what the record already says.
                continue
            receipt = self.head_runtime_for(run).stop(
                run,
                head_ops.StopInitiator(actor=STOPPED_BY_DISPATCHER),
            )
            if not receipt.ok:
                raise HostError(f"the {role} head of {workspace} was not stopped: {receipt.reason}")
        if live and all(_head_runtime_name(run) != ORCA_LEGACY_RUNTIME for run, _ in live):
            return
        try:
            self.head_runtime_for(ORCA_LEGACY_RUNTIME).stop_workspace(workspace)
        except HostError as exc:
            if "selector_not_found" not in str(exc):
                raise

    def stop_head(self, record: DispatcherRecord, kind: str, initiator: str = STOPPED_BY_DISPATCHER) -> None:
        """Stop one role's head through the head operation, recording who ended it."""
        if self.mode == "noop":
            return
        if kind == "review":
            self.stop_review_head(record, initiator)
            return
        self.stop_worker_head(record, initiator)

    def stop_review_head(self, record: DispatcherRecord, initiator: str = STOPPED_BY_DISPATCHER) -> None:
        """End this card's reviewer through the head operation, recording who ended it."""
        run = self.review_lifecycle_run(record)
        receipt = self.head_runtime_for(run).stop(
            run,
            head_ops.StopInitiator(actor=initiator),
            transport=self._head_transport(record.workspace, role="reviewer"),
            commit=lambda finishing: self._commit_review_run(record, finishing),
            preflight=lambda current: self._guard_head_run(current, "reviewer"),
            confirm_gone=lambda path: self._confirm_head_process_gone(
                path,
                run=run,
                role="reviewer",
            ),
        )
        if not receipt.ok:
            # A preflight mismatch is intentionally uncommitted: that foreign process was never this run.
            raise HostError(receipt.reason)
        self._commit_review_run(record, receipt.run)

    def _commit_review_run(self, record: DispatcherRecord, run: head_ops.HeadRun) -> None:
        """Write this reviewer's run onto the record, flushing it when the caller lent us the state."""
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
                # Inventing a card reference this call never received would not be truthful.
                task_ref=head_ops.TaskRef.card(record.worker or record.review_head or "unknown-reviewer"),
            )
        return replace(
            run,
            workspace=record.workspace or run.workspace,
            handle=record.review_handle,
            leaf=record.review_leaf,
            pid_file=record.review_pid_file,
        )

    def stop_worker_head(self, record: DispatcherRecord, initiator: str = STOPPED_BY_DISPATCHER) -> None:
        """End this card's worker through the head operation, recording who ended it."""
        run = self.worker_lifecycle_run(record)
        receipt = self.head_runtime_for(run).stop(
            run,
            head_ops.StopInitiator(actor=initiator),
            transport=self._head_transport(record.workspace, role="worker"),
            commit=lambda finishing: self._commit_worker_run(record, finishing),
            preflight=lambda current: self._guard_head_run(current, "worker"),
            confirm_gone=lambda path: self._confirm_head_process_gone(
                path,
                run=run,
                role="worker",
            ),
        )
        if not receipt.ok:
            # A failed preflight must leave the persisted HeadRun untouched.
            raise HostError(receipt.reason)
        self._commit_worker_run(record, receipt.run)

    def _commit_worker_run(self, record: DispatcherRecord, run: head_ops.HeadRun) -> None:
        """Write this worker's run onto the record, and flush it if the caller gave us the state."""
        record.worker_head_run = run.to_json()
        if self.commit_state is not None:
            self.commit_state()

    def commit_gate_pr_authorship(self, record: DispatcherRecord, entry: dict[str, Any]) -> None:
        """Write down that the github gate wrote a known text on a known pull request."""
        record.gate_pr_authorship = dict(entry)
        if self.commit_state is not None:
            self.commit_state()

    def commit_gate_workflow_dispatch(self, record: DispatcherRecord, entry: dict[str, Any]) -> None:
        """Persist a no-diff research workflow request before a later tick can repeat it."""
        record.gate_workflow_dispatch = dict(entry)
        if self.commit_state is not None:
            self.commit_state()

    def commit_gate_published_ref(self, record: DispatcherRecord, entry: dict[str, Any]) -> None:
        """Persist the branch and object id the gate just published, as the next push's lease."""
        record.gate_published_ref = dict(entry)
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
                # The record still names a pane while the run says that head was confirmed gone: a
                # fresh identity below keeps the stop from skipping a head already confirmed once.
                run = None
        if run is None:
            run = head_ops.HeadRun(
                run_id=head_ops.new_run_id(),
                spec=HeadSpec(
                    profile_id=record.head,
                    adapter=self._prompt_adapter(record.worker_run, record.head),
                ),
                workspace=record.workspace,
                # No card reference reaches this call, but the worker id carries one: `<ref>-<slug>`
                # is what the claim built it from. Inventing a card reference would not be truthful.
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
                pid_file,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
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
                pid_file,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
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
                pid_file,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
                expected=expected,
            )
            if _heartbeat_is_mismatch(status):
                raise HostError(f"head heartbeat from {pid_file} has a mismatching launch identity")
            if not status.get("known") or not status.get("alive"):
                if status.get("known"):
                    _clear_head_heartbeat(pid_file)
                return
            # SIGTERM and SIGHUP stay pending for a SIGSTOPed retained worker: wake its group before
            # the graceful signal, or the green handoff waits out the grace period and then kills it.
            if signal_number == signal.SIGTERM:
                self._signal_head(
                    pid_file,
                    signal.SIGCONT,
                    run=run,
                    role=role,
                    task=task,
                    leaf=leaf,
                    expected=expected,
                )
            self._signal_head(
                pid_file,
                signal_number,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
                expected=expected,
            )
            self._await_head_exit(
                pid_file,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
                expected=expected,
            )
        status = self._head_status(
            pid_file,
            run=run,
            role=role,
            task=task,
            leaf=leaf,
            expected=expected,
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
            pid_file,
            run=run,
            role=role,
            task=task,
            leaf=leaf,
            expected=expected,
        )
        if not _heartbeat_is_live_match(status):
            return
        pid = int(status["pid"])
        try:
            # The terminal gives an interactive head its own foreground process group, so this
            # reaches helpers without detaching the head from its controlling terminal. Old launches
            # and focused tests can share our group: never turn that into a signal to the dispatcher.
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
                pid_file,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
                expected=expected,
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
        """Done-path cleanup after a green merge: stop the worktree's heads (killing the
        worker and reviewer plus their child shells and subagents) and remove the worktree
        from Orca and git. Never used on rework, which reuses the workspace.

        The stop is the confirmed twin, not the best-effort one, because this is the path that
        removes the worktree next: `stop` absorbs a refusal, and a removal made on the strength of
        an absorbed refusal takes the checkout out from under a head that is still running — after
        which the card's next attempt raises a second head beside the first. The teardown itself
        still does not escape, so a green card reaches Done either way; what a refusal costs is the
        removal, and the worktree is left standing for whoever looks at the head that would not go.
        """
        if self.mode == "noop" or not record.workspace:
            return
        try:
            self.stop_workspace(record)
        except HostError:
            return
        try:
            self._run_json(
                ["orca", "worktree", "rm", "--worktree", f"path:{record.workspace}", "--force", "--json"]
            )
        except HostError:
            pass

    def _fetch_seed(self, repo: Path, seed: str) -> str:
        """Bring `seed` into the project checkout and return the start point a worktree is cut at.

        A branch seed is fetched by name and cut at its remote-tracking ref, which is what every
        card did before seeds existed. An exact object id — the predecessor candidate a reslice
        successor inherits — is not a ref the remote will serve by name, so the whole remote is
        fetched and the object is then required to be present: a seed that is not there is this
        card's own contract failing, not a checkout to invent.
        """
        if _is_exact_ref_sha(seed):
            self._run(["git", "-C", str(repo), "fetch", "origin"], "git fetch")
            try:
                self._run(["git", "-C", str(repo), "cat-file", "-e", f"{seed}^{{commit}}"], "git seed probe")
            except HostError:
                raise HostError(
                    f"seed commit {seed[:12]} is not on the project remote; the predecessor "
                    "candidate this card inherits was never published or has been removed",
                    bring_up_cause=CAUSE_BASE_BRANCH_CONTRACT,
                ) from None
            return seed
        self._run(["git", "-C", str(repo), "fetch", "origin", seed], "git fetch")
        return f"origin/{seed}"

    def _create_workspace(self, project: str, worker_id: str, seed: str, *, expected: str = "") -> str:
        """Cut the card's worktree from `seed` and accept it only as this card's workspace of this repo.

        `seed` is where the checkout starts, not where the card integrates: an ordinary card seeds
        from its integration base, and a reslice successor from the predecessor candidate its
        `workspace.seed_ref` names (secretary-1541).

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
        start = self._fetch_seed(repo, seed)
        result = self._run_json(
            [
                "orca",
                "worktree",
                "create",
                "--repo",
                f"path:{repo}",
                "--name",
                worker_id,
                "--base-branch",
                start,
                "--setup",
                "skip",
                "--no-parent",
                "--activate",
                "--json",
            ]
        )
        worktree = result.get("worktree") if isinstance(result.get("worktree"), dict) else result
        path = worktree.get("path") if isinstance(worktree, dict) else None
        if not isinstance(path, str) or not path:
            raise HostError("orca did not return a workspace path")
        reason = self._workspace_rejection(path, str(registration["id"]), worker_id, expected)
        if reason:
            raise HostError(f"{reason}{self._discard_workspace(path)}")
        return path

    def _workspace_rejection(self, path: str, repo_id: str, worker_id: str, expected: str) -> str:
        """Why this returned worktree may not be adopted, or "" when it may.

        A worktree Orca will not describe is a rejection rather than a pass.
        """
        try:
            shown = self._run_json(["orca", "worktree", "show", "--worktree", f"path:{path}", "--json"])
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
            self._run_json(["orca", "worktree", "rm", "--worktree", f"path:{path}", "--force", "--json"])
        except HostError as exc:
            return f"; the rejected worktree at {path} could not be removed either: {exc}"
        return ""

    def _validate_resumable_workspace(self, task: dict[str, Any], workspace: str) -> None:
        """Accept only the registered project worktree on this card's worker branch."""
        if self.mode == "noop":
            return
        path = Path(workspace)
        if not path.is_dir():
            raise HostError("resume workspace is not a directory", bring_up_cause=CAUSE_WORKSPACE_CONTRACT)
        try:
            top_level = self._run(
                ["git", "-C", workspace, "rev-parse", "--show-toplevel"], "resume workspace git check"
            ).stdout.strip()
        except HostError as exc:
            raise HostError(
                "resume workspace is not a git worktree", bring_up_cause=CAUSE_WORKSPACE_CONTRACT
            ) from exc
        if not _same_repo(Path(top_level), path):
            raise HostError(
                "resume workspace git root does not match its expected path",
                bring_up_cause=CAUSE_WORKSPACE_CONTRACT,
            )
        branch = self._run(
            ["git", "-C", workspace, "branch", "--show-current"], "resume workspace branch check"
        ).stdout.strip()
        expected_branch = _legacy_worker_branch(task["ref"])
        if branch != expected_branch:
            raise HostError(
                f"resume workspace is on branch {branch or '(detached)'}, expected {expected_branch}",
                bring_up_cause=CAUSE_WORKSPACE_CONTRACT,
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
            raise HostError(
                "resume workspace is not a registered worktree of the project repo",
                bring_up_cause=CAUSE_WORKSPACE_CONTRACT,
            )

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
        """Bring one head up and hand back the pane together with the configuration it started with."""
        pid_file = _pid_file_path(_watchdog_kind(role), task["ref"]) if task else ""
        task_ref = self._task_ref(task, role, prompt_document)
        run_id = heartbeat_run_id or head_ops.new_run_id()
        # `preflight_codex_run` is deliberately reached even by noop: a fake transport is not an
        # exemption from the policy boundary. A refused attestation opens no pane and clears no
        # predecessor state.
        if self.mode == "noop":
            try:
                preflight_run = self._preflight_launch_run(
                    head,
                    role=role,
                    workspace=workspace,
                    task_ref=task_ref,
                    pid_file=pid_file,
                    run_id=run_id,
                )
            except CodexFanoutPolicyError as exc:
                raise HostError(str(exc)) from None
            preflight_run = self._capture_launch_prompt_identity(
                preflight_run, role=role, document=prompt_document
            )
            return self._launched(
                f"noop:{head}:{Path(workspace).name}:{Path(prompt_file).name}",
                head,
                task,
                role,
                workspace,
                failover,
                head_run=preflight_run.to_json(),
            )
        heartbeat = heartbeat_identity(
            run_id=run_id,
            role=role,
            task_ref=task_ref.to_json(),
        )
        try:
            preflight_run = self._preflight_launch_run(
                head,
                role=role,
                workspace=workspace,
                task_ref=task_ref,
                pid_file=pid_file,
                run_id=run_id,
            )
        except CodexFanoutPolicyError as exc:
            raise HostError(str(exc)) from None
        preflight_run = self._capture_launch_prompt_identity(
            preflight_run, role=role, document=prompt_document
        )
        memory_identity: dict[str, str] | None = None
        project = str((task or {}).get("project") or "")
        if task is not None and role in {"worker", "reviewer"} and project:
            try:
                grant = memory_access.issue_grant(
                    preflight_run,
                    memory_access.card_subject(str(task.get("ref") or ""), project),
                    data_dir=self.data_dir,
                )
            except memory_access.MemoryAccessError as exc:
                raise HostError(f"memory access binding could not be issued: {exc}") from None
            memory_identity = grant.launch_identity
        if pid_file:
            # Drop any pid a previous launch in this workspace left behind, so a respawn cannot read
            # a dead predecessor's pid as this launch's liveness before the new head overwrites it.
            _clear_head_heartbeat(pid_file)
        command = os.environ.get(env_name)
        launch = HeadCommand(command) if command else None
        if command:
            # A raw command override bypasses the catalog launcher and its pid heartbeat wrapper:
            # deliberate for tests and manual overrides, with the inactivity ceiling as the fallback.
            self.catalog.prepare_head_workspace(head, workspace, role=role)
        else:
            launch = self.catalog.head_launch(
                head,
                prompt_file,
                workspace=workspace,
                role=role,
                launch_prompt=launch_prompt,
                identity=memory_identity,
            )
            command = launch.command
            if pid_file:
                command = _with_pid_heartbeat(command, pid_file, identity=heartbeat)
        adapter = (getattr(launch, "adapter", "") or "codex") if launch else "codex"
        ingress = self._codex_provider_ingress(preflight_run)
        subject = f"{role or 'head'}-launch"
        pointer = None
        if launch and launch.prompt_after_start:
            # Which of the two prompt shapes this head is in is decided by the rendered command, not
            # the profile: a raw command override runs a provider no profile describes. An empty
            # pointer text is the legacy shape, not an empty prompt — that head is sent the prompt
            # file's own contents; a caller with a task document passes the bounded line naming it.
            pointer = head_ops.NudgePointer(text=launch_prompt or "", document=prompt_document)
        spec = self._head_spec(head, adapter)
        receipt = self.head_runtime_for(spec).start(
            spec,
            workspace,
            task_ref,
            command=command,
            title=title,
            pointer=pointer,
            pid_file=pid_file,
            split_from=split_from,
            transport=self._head_transport(
                workspace,
                prompt_file,
                adapter,
                role,
                before_send=ingress.bind_before_delivery if ingress is not None else None,
            ),
            subject=subject,
            run_id=run_id,
            role=role,
            run=preflight_run,
            commit=ingress.commit_run if ingress is not None else None,
        )
        if not receipt.ok:
            failed_run = receipt.run
            if pid_file and failed_run is not None and failed_run.leaf:
                # Delivery can refuse with a live pane before the bring-up returns normally. Bind
                # that pane to the written heartbeat before persisting the failed launch intent.
                _bind_head_heartbeat(pid_file, expected=heartbeat, leaf=failed_run.leaf)
            if receipt.failure is None:
                # A refusal the boundary made on its own terms, before any operation ran: a
                # bring-up over a turn this runtime is still holding is the one that exists. It
                # carries no `HeadOperationError` to translate, so the reason is the message —
                # `_launch_failure(None, ...)` would have raised `HostError("None")`.
                raise HostError(receipt.reason) from None
            raise self._launch_failure(receipt.failure, workspace, pid_file, subject) from None
        if pid_file:
            # Pane create gives the leaf after the head wrote its base identity. A best-effort bind
            # is enough: the reader still requires the run, role and task binding to match.
            _bind_head_heartbeat(pid_file, expected=heartbeat, leaf=receipt.run.leaf)
        lifecycle_run = receipt.run
        if lifecycle_run.spec.adapter == "claude":
            # Claude creates its jsonl after its pane starts.  Capture the one transcript that the
            # pre-pane baseline identifies before routing records this bring-up, so the journal has
            # the provider's session id rather than asking analytics to reconstruct it from cwd.
            lifecycle_run = _bind_claude_provider_progress_source(lifecycle_run)
        delivery = receipt.delivery
        return self._launched(
            receipt.run.handle,
            head,
            task,
            role,
            workspace,
            failover,
            leaf=receipt.run.leaf,
            delivery_evidence=(_delivery_evidence_json(delivery, subject) if delivery is not None else {}),
            head_run=lifecycle_run.to_json(),
            fallback_reason=receipt.fallback_reason,
        )

    def _head_spec(self, head: str, adapter: str) -> HeadSpec:
        """The launch shape the run is recorded with, degrading rather than failing a live bring-up."""
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

    def _capture_launch_prompt_identity(
        self,
        run: head_ops.HeadRun, *, role: str, document: str
    ) -> head_ops.HeadRun:
        """Attach the exact worker/reviewer document before a pane can observe it.

        TASK.md is a mutable workspace projection, so routing may not read it after delivery.
        The document digest is a required launch fact: a read failure aborts the bring-up before a
        head can receive an instruction whose durable identity the dispatcher cannot record.
        """
        if role not in {WORKER_ROLE, REVIEW_ROLE, "reviewer"} or not document:
            return run
        path = Path(document)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise HostError(f"launch prompt {path} could not be captured: {exc}") from None
        policy = dict(run.fanout_policy)
        policy["prompt_identity"] = {
            "path": str(path.resolve(strict=False)),
            "version": f"sha256:{hashlib.sha256(content).hexdigest()}",
        }
        captured = run.with_fanout_policy(policy)
        # The Codex ingress owns the exact run it hands back immediately before delivery. Keep
        # that handoff aligned with the launch-time prompt fact, or its later provider binding
        # would otherwise return the pre-capture record and erase this identity.
        ingress = self._codex_provider_ingresses.get(run.run_id)
        if ingress is not None:
            ingress.run = captured
        if self._prepared_provider_runs.get(run.run_id) is not None:
            self._prepared_provider_runs[run.run_id] = captured
        return captured

    def _head_transport(
        self,
        workspace: str,
        prompt_file: str = "",
        adapter: str = "",
        role: str = "",
        before_send: Callable[[], head_ops.HeadRun | None] | None = None,
        ack_out_of_band: bool = False,
    ) -> DispatcherHeadTransport:
        """This product's delivery and close semantics, for the operation to perform through
        the host it is running on.
        """
        return DispatcherHeadTransport(
            self,
            workspace,
            prompt_file,
            adapter,
            role,
            before_send,
            ack_out_of_band,
        )

    @staticmethod
    def _run_heartbeat_identity(run: head_ops.HeadRun, role: str) -> dict[str, str]:
        return heartbeat_identity(
            run_id=run.run_id,
            role=role,
            task_ref=run.task_ref.to_json(),
            leaf=run.leaf,
        )

    def _record_heartbeat_status(self, record: DispatcherRecord, kind: str) -> dict[str, Any]:
        field = "review_head_run" if kind == "review" else "worker_head_run"
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        return self._head_status(
            pid_file,
            run=getattr(record, field, {}),
            role=kind,
            leaf=leaf,
        )

    def _launch_failure(
        self, exc: head_ops.HeadOperationError, workspace: str, pid_file: str, subject: str
    ) -> Exception:
        """Translate one operation's refusal into the failure the dispatcher's callers already read."""
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
                    pid_file,
                    run=run,
                    role=role,
                    task=task,
                    leaf=leaf,
                    expected=expected,
                )
                if pid_file
                else {"known": False}
            )
            if not status.get("known"):
                raise HostError(f"head terminal close failed: {exc}") from None
            if _heartbeat_is_mismatch(status):
                raise HostError("head terminal close found a mismatching launch identity") from None
        self._confirm_head_process_gone(
            pid_file,
            run=run,
            role=role,
            task=task,
            leaf=leaf,
            expected=expected,
        )

    def _launched(
        self,
        handle: str,
        head: str,
        task: dict[str, Any] | None,
        role: str,
        workspace: str = "",
        failover: bool = False,
        leaf: str = "",
        delivery_evidence: dict[str, Any] | None = None,
        head_run: dict[str, Any] | None = None,
        fallback_reason: str = "",
    ) -> LaunchedHead:
        """Pair the pane with the launch snapshot of the head running in it."""
        if task is None:
            return LaunchedHead(
                handle=handle,
                head=head,
                leaf=leaf,
                delivery_evidence=dict(delivery_evidence or {}),
                head_run=dict(head_run or {}),
                fallback_reason=fallback_reason,
            )
        try:
            run = self.catalog.head_run(
                task, role=role, head=head, workspace=workspace, failover=failover
            ).to_json()
        except (HostError, AttributeError, KeyError, TypeError):
            run = HeadRun(role=role, head=head, adapter="unknown", model_source=MODEL_UNKNOWN).to_json()
        return LaunchedHead(
            handle=handle,
            head=head,
            run=run,
            leaf=leaf,
            delivery_evidence=dict(delivery_evidence or {}),
            head_run=dict(head_run or {}),
            fallback_reason=fallback_reason,
        )

    @property
    def head_runtime(self) -> Any:
        """The product's default backend, for a caller that has no head to name.

        Every operation of this dispatcher that acts on a head — including the workspace-scoped
        cleanups, which choose from the durable run the record names — goes through
        `head_runtime_for` instead. What is left on this property is the reading a caller outside
        the lifecycle does when it wants the default backend as an object and has nothing to
        resolve it from.
        """
        return self.head_runtime_for(None)

    def head_runtime_for(self, subject: Any = None) -> Any:
        """The backend this head is held by — the one place a `runtime` value becomes an object.

        One resolver rather than a branch at each caller: every lifecycle site hands over the head
        it is acting on (a `HeadRun`, a `HeadSpec`, or the name itself) and is handed back the
        backend that head's profile named, so no caller has to know that there is more than one.
        `None` is the operation that names no head at all and gets the product default.

        The value is read off the head rather than re-resolved from the registry, because the
        registry can be repointed while a head is running: a head raised on one backend has to go
        on being observed and stopped through that backend until it ends.
        """
        return self._head_runtime_named(_head_runtime_name(subject))

    def _head_runtime_named(self, name: str) -> Any:
        """Build — or hand back — the one instance of the backend called `name`.

        An unknown name cannot arrive from a validated registry (`validate_launch_shape` refuses it
        when the table loads), so reaching this refusal means a record or a caller invented one,
        and it fails closed by name rather than falling back to a backend the head is not on.
        """
        held = self._head_runtimes.get(name)
        if held is not None:
            return held
        try:
            built = build_head_runtime(
                name,
                session=lambda: self.session,
                local_pty_root=self._local_pty_root,
                head_process_status=_head_process_status,
            )
        except UnknownHeadRuntimeError as exc:
            raise HostError(str(exc)) from exc
        self._head_runtimes[name] = built
        return built

    def _local_pty_root(self) -> Path:
        """Where this dispatcher's supervised heads keep their run directories.

        Deliberately short and directly under the data directory: a run directory holds a Unix
        socket, whose address the kernel bounds at about a hundred bytes, and the substrate refuses
        an address it cannot fit rather than failing opaquely later.
        """
        return self.data_dir / "heads"

    @property
    def session(self) -> OrcaSessionHost:
        """The session manager this runtime reads a workspace's pane inventory through.

        Not a lifecycle seam any more: starting, delivering to, observing, draining, stopping and
        attaching to a head all go through `head_runtime`. What is left is the workspace inventory —
        which panes exist, which are connected — read to choose a split anchor and to answer
        diagnostics. A read that names no head is not a head's lifecycle.
        """
        return OrcaSessionHost(self._run_json)

    def _open_head_pane(self, run: head_ops.HeadRun, title: str, command: str) -> head_ops.HeadRun:
        """Bring a head up in a pane of its own, with no prompt delivered by the bring-up.

        The observer is the one head whose delivery contour is its own: it opens the pane, then puts
        its launch prompt in front of it through the same boundary, so that the two halves can be
        accounted for separately. `start` with no pointer is exactly that pane and nothing else.
        """
        try:
            receipt = self.head_runtime_for(run).start(
                run.spec,
                run.workspace,
                run.task_ref,
                command=command,
                title=title,
                pid_file=run.pid_file,
                run_id=run.run_id,
                role=run.role,
                run=run,
            )
        except PaneHostError as exc:
            raise HostError(str(exc)) from None
        if not receipt.ok:
            raise HostError(receipt.reason)
        return receipt.run

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
            # The dispatcher may have died after the provider started but before it recorded that
            # confirmation. Returning lets recovery checkpoint it without touching TASK.md.
            return
        base = self.catalog.integration_base(task["project"], task.get("workspace", {}).get("base_branch"))
        # One generation and one decision, read once: the document the worker is sent back to and
        # the prompt that sends it there name the same round and adjudication because they share
        # these values, not because separate call sites happen to agree.
        generation = record.report_generation
        decision = record.report_decision
        self._clear_report_bodies(task["ref"])
        self._write_prompt(
            workspace / "TASK.md",
            self._worker_task_doc(task, base, record.attempt_id, generation, decision),
        )
        # The continuation travels as a pointer at the document just written, not as the round typed
        # into the composer: that is the delivery shape that has never lost a prompt. It is built
        # before the wake-up, because finding out after SIGCONT leaves a woken head nothing to read.
        try:
            pointer = head_ops.NudgePointer.at_document(
                str(workspace / "TASK.md"), _continuation_note(generation, decision)
            )
        except PromptDocumentError as exc:
            raise HostError(f"the continuation pointer could not be built: {exc}") from None
        activate = None
        if status.get("stopped"):
            # The delivery transport waits for `tui-idle` before this callback: a readiness timeout
            # saying the pane is busy leaves the retained HeadRun frozen, never SIGCONT'd.
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
        """Point this card's live worker at one thing, through the head operation (secretary-1412)."""
        run = self.worker_lifecycle_run(record)
        try:
            receipt = self.head_runtime_for(run).deliver(
                run,
                pointer,
                transport=self._head_transport(
                    record.workspace,
                    "TASK.md",
                    self._prompt_adapter(record.worker_run, record.head),
                    before_send=before_send,
                ),
                subject=subject,
            )
        except (TuiDeliveryError, HostError) as exc:
            failure = HostError(f"{what} was not delivered: {exc}")
            failure.evidence = getattr(exc, "evidence", None)
            raise failure from None
        if not receipt.ok:
            failure = HostError(f"{what} was not delivered: {receipt.reason}")
            failure.evidence = receipt.evidence
            raise failure from None
        record.worker_head_run = receipt.run.to_json()
        _record_worker_delivery_evidence(record, receipt.delivery)

    def _set_worker_branch(self, workspace: str, branch: str) -> None:
        if self.mode == "noop":
            return
        # Never force-update the target name: a preserved checkout elsewhere can already own it.
        self._run(["git", "-C", workspace, "branch", "-m", branch], "git branch")

    def _write_prompt(self, path: Path, body: str) -> None:
        write_text_atomic(path, body)

    def _review_document(self, task: dict[str, Any], record: DispatcherRecord) -> tuple[Path, str]:
        """This round's review task, on disk, and the one line that points a reviewer at it.

        The text never travels through the pane; only a bounded pointer does. A retry re-renders the
        same document at the same path, and a document that cannot be written stops the bring-up before
        a pane is opened. Nothing here writes to or removes anything from the candidate checkout.
        """
        document = self._prompt_document_path(REVIEW_ROLE, task["ref"], record.review_baseline)
        prompt = self._review_prompt(task, record.attempt_id, record.review_baseline, record=record)
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
            if not entry[len(prefix) : -len(".md")].isdigit():
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

    def _broad_check_invocation(self, project: str) -> tuple[str, str]:
        """The exact `check broad`/`check show` commands this project's contract resolves to.

        Until issue:8b39e60e4df361c6138e there was nowhere for a project to say which suite its
        broad check runs, so this packet printed the literal placeholder
        `<this project's broad suite module>` and left the worker to guess. What it guessed, because
        that is what every document said, was bare `python3 -m unittest`: repository-wide discovery,
        every CI suite in one process, ~402s for Secretary. A packet that names a command a worker
        cannot run without inventing part of it is a packet that teaches the expensive habit.

        The contract is read through the same `projects.contract` rules the preflight and the
        worker's own resolution use, so the command printed here is the command that workspace will
        actually accept. Two empty strings mean the project declares no suite: the caller then says
        so in words rather than printing a command that does not exist.
        """
        if not project:
            return "", ""
        try:
            verdict = self.catalog.broad_check_verdict(project)
        except (HostError, DispatcherError):
            # Rendering a task document never fails over a registry question. A project whose
            # binding or adapter cannot be read reaches the worker with the honest wording below,
            # and the refusal itself is the preflight's to report, not this packet's.
            return "", ""
        contract = verdict.contract if verdict.fit else None
        if contract is None or not contract.module:
            return "", ""
        arguments = "".join(f" --module-arg {shlex.quote(argument)}" for argument in contract.args)
        return (
            f"python3 -m secretary check broad --reuse --module {contract.module}{arguments}",
            f"python3 -m secretary check show --module {contract.module}{arguments}",
        )

    def _worker_task_doc(
        self,
        task: dict[str, Any],
        base: str,
        attempt_id: str,
        generation: int = 0,
        decision: str = "",
    ) -> str:
        branch = _legacy_worker_branch(task["ref"])
        # The generation keeps the report request-id distinct per round: a rework reuses the same
        # attempt_id, so without it the second done-report is deduped and the dispatcher waits on.
        request = _attempt_request_id(attempt_id, "worker-report-done", task["ref"], str(generation))
        # One id per classification: a worker restating a block under the other classification is
        # filing a different report, and a shared id would answer the second call with `validation`.
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
        decision, review_red = self._select_revision_bound_worker_feedback(task, decision)
        if decision:
            # Rendered above the findings it was made on, and named as the thing to follow.
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
        broad_command, show_command = self._broad_check_invocation(str(task.get("project") or ""))
        if broad_command:
            broad_invocation = [f"    {broad_command}", ""]
            show_invocation = f"`{show_command}` and quote its summary"
        else:
            broad_invocation = [
                "This project's adapter declares no broad suite, so there is no exact command to",
                "print here. Work out which suite this card's acceptance criteria require, run that",
                "one through the wrapper by naming it yourself:",
                "",
                "    python3 -m secretary check broad --reuse --module <the suite module you chose>",
                "",
                "and say in your report which module you ran and why that is the right suite. Do not",
                "reach for repository-wide test discovery because it is the easiest thing to type.",
                "",
            ]
            show_invocation = (
                "`python3 -m secretary check show --module <the same module>` and quote its summary"
            )
        sections += [
            "## Check-cost contract",
            "",
            "During development, run the smallest relevant checks first. Run at most one local broad",
            "suite for this report generation and unchanged content when it is actually useful; name any",
            "additional broad rerun and its reason in the report. A later executed local/GitHub gate is",
            "reusable downstream only if it produces a valid dispatcher-owned exact-SHA gate receipt. A",
            "none/noop gate or a missing dispatcher-owned exact-SHA gate receipt attests no broad suite;",
            "do not call it authoritative, and run the appropriate validation before reporting when this",
            "card's acceptance criteria require it.",
            "",
            "Run that broad suite through the receipt wrapper, so its worker-local broad receipt outlives",
            "the pane:",
            "",
            *broad_invocation,
            "`--reuse` is the default way to invoke it: with a usable worker-local broad receipt it prints",
            "that worker-local broad receipt",
            "and returns the result the run had, and otherwise it runs the suite. So the answer to a",
            "pane that scrolled away is this same command, not a rerun; asking for a rerun over content the",
            "worker-local broad receipt already covers is prohibited. Drop `--reuse` only to force a fresh run you can",
            "name a reason for.",
            "",
            "It streams the combined output while the suite runs and returns the check's own exit",
            "status, and it writes `state/checks/broad-<digest>.json` in this workspace: command and",
            "digest, cwd and imported project, start/end/duration, exit code, parsed verdict and",
            "counts where the runner prints them, and a bounded diagnostic tail. Read it back with",
            show_invocation,
            "in the report. While that worker-local broad receipt is usable, you already have the answer. An edit to the",
            "content, or a concrete red result you are fixing, opens a new justified run — name which",
            "one in the report. Committing content a receipt already covers is not one of them: the",
            "identity is the tree, so a commit that changes no byte reuses the worker-local broad receipt.",
            "The worker-local broad receipt is workspace-local and ignored by git; never commit it.",
            "It is never presented as a dispatcher-owned exact-SHA gate receipt.",
            "",
            "A worker-local broad receipt stands in for a run only while it describes this content and the check",
            "process imported the project from this workspace; an import resolved elsewhere is",
            "recorded truthfully and still refused. `check show` and `--reuse` answer that with",
            "one predicate, so they cannot disagree.",
            "",
            "A check that needs a shell runs as `--command '<shell>'` instead, and buys that",
            "generality by attesting less: a shell may change directory or import environment",
            "before any interpreter starts, so its receipt records no import provenance and is",
            "never reused in place of a run. Prefer `--module` for the suite you report on.",
            "",
        ]
        sections += [
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
            "never hold, is why this paragraph exists (secretary-1161, 2026-08-06). Run only the",
            "checks the contract above permits, and run each of them in the foreground.",
            "",
            # In the packet every worker gets: a home file reaches only one model family.
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
            f"{_CONTROL_PLANE_TASK_COMMAND} report --ref {task['ref']} --role worker --kind done --request-id {request} --body-file {body_file}",
            f"{_CONTROL_PLANE_TASK_COMMAND} report --ref {task['ref']} --role worker --kind blocked --classification external_fact --request-id {blocked_requests['external_fact']} --body-file {body_file}",
            f"{_CONTROL_PLANE_TASK_COMMAND} report --ref {task['ref']} --role worker --kind blocked --classification wrong_task_definition --request-id {blocked_requests['wrong_task_definition']} --body-file {body_file}",
            "",
            f"Base branch: {base}",
            f"Worker branch: {branch}",
            "",
            # Last, after anything the card description or decision can write into, so
            # `_task_doc_decision` reads the dispatcher's own record. Written on every document,
            # empty body included: a round with no decision has to read back as none.
            _decision_record_line(generation, decision),
            # And the round's own ids, on the same terms: the report commands above are prose in a
            # document that also renders the card description, so they cannot be the authority.
            _round_record_line(generation, [request, *blocked_requests.values()]),
            "",
        ]
        return "\n".join(sections)

    def _select_revision_bound_worker_feedback(
        self, task: dict[str, Any], decision: str
    ) -> tuple[str, str | None]:
        """Select only review/decision instructions bound to this description revision.

        The board keeps comments forever, while `TASK.md` must only carry instructions for the
        specification it renders. Missing, malformed, or non-unique bindings intentionally
        produce no historical instruction; the current card description remains the work item.
        """
        events = self.audit.events(str(task.get("ref") or ""))
        description = str(task.get("description") or "")
        revision = specification_revision(events, description)
        if not revision:
            return "", None
        digest = hashlib.sha256(description.encode("utf-8")).hexdigest()
        decision = (decision or "").strip()
        if decision:
            if not self._bound_marker_body(task, events, "decision:rework", revision, digest, decision):
                return "", None
            review = self._bound_marker_body(task, events, "review:red", revision, digest)
            return decision, review
        return "", self._bound_marker_body(task, events, "review:red", revision, digest)

    @staticmethod
    def _bound_marker_body(
        task: dict[str, Any],
        events: list[dict[str, Any]],
        marker: str,
        revision: str,
        description_digest: str,
        required_body: str = "",
    ) -> str | None:
        """Return the latest uniquely located marker body with an exact spec binding."""
        comments = task.get("comments") or []
        for event in reversed(events):
            data = event.get("data") if isinstance(event.get("data"), dict) else event.get("payload")
            if not isinstance(data, dict) or data.get("marker") != marker:
                continue
            if (
                data.get("specification_revision") != revision
                or data.get("description_sha256") != description_digest
            ):
                continue
            body = data.get("body")
            occurrence = data.get("marker_occurrence")
            if (
                not isinstance(body, str)
                or not body.strip()
                or not isinstance(occurrence, int)
                or occurrence < 1
            ):
                return None
            if required_body and body.strip() != required_body:
                continue
            rendered = f"[{marker}]\n{body}"
            matches = [
                comment
                for comment in comments
                if comment.get("marker") == marker and comment.get("body") == rendered
            ]
            if len(matches) < occurrence:
                return None
            return body.strip()
        return None

    def _review_prompt(
        self,
        task: dict[str, Any],
        attempt_id: str,
        review_round: int,
        *,
        record: DispatcherRecord | None = None,
    ) -> str:
        # The round belongs in the key like it does in the worker report id: a card that goes red
        # twice in one attempt reuses attempt_id, and a round-less id replays the first verdict.
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
            # Deliberately duplicates the gate's own deterministic preflight: a check that only
            # ever runs in one place has no second opinion.
            "Read the commit messages on this branch, not only the diff. AI co-authorship is",
            "forbidden: a `Co-Authored-By:` trailer naming a model or vendor, or a generated-by",
            "attribution line, is a RED blocker. Ordinary human co-authors are not. Say what you",
            "found; do not rewrite history yourself.",
            "",
            "When a change depends on how an external backend behaves, a passing fixture is not",
            "evidence: it can encode the same wrong assumption as the code under review. Say which",
            "real behaviour you verified and how. If no end-to-end check against the real backend",
            "was possible, write plainly that it was not done and which assumption stays unverified.",
            "",
            "Post exactly one review verdict through the secretary task protocol:",
            *_body_file_instructions(body_file),
            f"{_CONTROL_PLANE_TASK_COMMAND} verdict --ref {task['ref']} --role reviewer --kind green --request-id {green_request} --body-file {body_file}",
            f"{_CONTROL_PLANE_TASK_COMMAND} verdict --ref {task['ref']} --role reviewer --kind red --request-id {red_request} --body-file {body_file}",
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
        return (
            "\n".join(_safe_one_line(part, limit=4000) for part in (names, summary) if part)
            or "(no changed paths)"
        )

    def _run_shell(self, command: str, cwd: Path, label: str) -> None:
        self._run(["bash", "-lc", command], label, cwd=cwd)

    def _run_json(self, args: list[str]) -> dict[str, Any]:
        completed = self._run(args, _safe_command_label(args))
        try:
            loaded = json.loads(completed.stdout or "{}")
        except ValueError:
            raise HostError(f"{args[0]} returned invalid JSON") from None
        return loaded.get("result", loaded) if isinstance(loaded, dict) else {}

    def _run(
        self, args: list[str], label: str, *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError(f"{label} failed: {exc}") from None
        if completed.returncode != 0:
            text = (completed.stderr or completed.stdout or "").strip()
            raise HostError(f"{label} failed: {_tail(text)}")
        return completed

    def run_capture(
        self, args: list[str], label: str, *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Like _run but returns the CompletedProcess regardless of exit status (the gate reads a
        non-zero code as a red verdict, not a host failure). Still raises HostError when the process
        can't run at all."""
        try:
            return subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError(f"{label} failed: {exc}") from None


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
