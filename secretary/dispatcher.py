"""Production dispatcher runtime."""

from __future__ import annotations

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
    HeadLaunch,
    HeadLaunchError,
    claude_launch_model as _claude_launch_model,
    ensure_claude_workspace_ready as _ensure_claude_workspace_ready,
    ensure_codex_workspace_trusted as _ensure_codex_workspace_trusted,
    render_claude_command as _render_claude_command,
    render_codex_command as _render_codex_command,
    render_codex_launch as _render_codex_launch,
    PYTHON_SAFE_PATH_FLAG as _PYTHON_SAFE_PATH_FLAG,
    role_launch_env as _role_launch_env,
    with_pid_heartbeat as _with_pid_heartbeat,
    wrap_role_shell_command as _wrap_role_shell_command,
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
from secretary.dispatcher_observer import (
    OBSERVER_HEAD_FALLBACK,
    OBSERVER_PROMPT_FILE,
    OBSERVER_ROLE,
    ObserverLaunchAborted,
    observer_launch_prompt as _observer_launch_prompt,
    observer_pid_file as _observer_pid_file,
)
from secretary.observer_root import OBSERVER_REPO_NAME, observer_root_repo
from secretary.dispatcher_launch import (
    WORKER_ROLE,
    clear_launch_intent as _clear_launch_intent,
    confirm_launch_intent as _confirm_launch_intent,
    forget_role_head as _forget_role_head,
    head_stop_unconfirmed as _head_stop_unconfirmed,
    launch_aborted as _launch_aborted,
    launch_intent_unwritable as _launch_intent_unwritable,
    launch_left_a_head as _launch_left_a_head,
    launch_pid_file as _launch_pid_file,
    mark_launch_aborted as _mark_launch_aborted,
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
    command_review_running as _command_review_running,
    end_review_pane as _end_review_pane,
    recover_review_launch as _recover_review_launch,
    start_review as _start_review,
)
from secretary.dispatcher_watchdog import (
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
    DELIVERY_ACCEPTED,
    DELIVERY_CONFIRMED,
    READINESS_READY,
    READINESS_UNKNOWN,
    TuiDeliveryError,
    close_terminal as _close_tui_terminal,
    close_terminal_strict as _close_tui_terminal_strict,
    deliver_interactive_prompt as _deliver_interactive_prompt,
    terminal_readiness as _terminal_readiness,
    terminal_turn_started as _terminal_turn_started,
    turn_started_confirm as _turn_started_confirm,
)
from secretary.dispatcher_tui import deliver_tui_prompt as _deliver_tui_prompt
from secretary.dispatcher_types import (
    DispatcherError,
    HeadLaunchAborted,
    HostError,
    ReviewLaunch,
    review_pane_label,
)
from secretary.head_registry import HeadRegistryConfigError, installed_heads
from secretary.routing_journal import (
    HEAD_FROM_CARD,
    HEAD_FROM_RECORD,
    HEAD_FROM_ROLE_DEFAULT,
    MODEL_UNKNOWN,
    HeadRun,
    attempts as _routing_attempts,
    head_run_from_profile,
    routing_payload as _routing_payload,
    run_key as _run_key,
)
from secretary.head_health import HeadHealth, HeadReadiness
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
from triggered_agents.agents.pipeline.task_protocol import pythonpath_prefix

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
        head = str(requested) if requested else str(
            self._heads.get("role_defaults", {}).get("new_card") or "codex"
        )
        self._head_profile(head)
        return head

    def review_head(self, task: dict[str, Any]) -> str:
        requested = task.get("routing", {}).get("review_head_override")
        head = str(requested) if requested else str(
            self._heads.get("role_defaults", {}).get("reviewer") or "codex-reviewer"
        )
        self._head_profile(head)
        return head

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

        The head is decided once, at claim, and the claim writes it onto the card. Re-reading the
        override or the role default here would hand the rest of a running attempt to whatever the
        board says now, so a role default edited mid-attempt would silently move the reviewer. If
        that head has since left `heads.yaml` there is nothing to resume it on: the attempt stops
        and a human decides, because substituting today's role default would launch a head the
        claim never picked and file it under the running attempt. A card claimed before the claim
        recorded a head has no decision to keep, so it takes the current one.
        """
        claimed = (task.get("routing") or {}).get(key)
        if not claimed:
            return current(task)
        head = str(claimed)
        try:
            self._head_profile(head)
        except HostError as exc:
            raise HostError(f"head {head!r} recorded at claim is unavailable: {exc}") from None
        return head

    def head_run(
        self, task: dict[str, Any], *, role: str, head: str = "", workspace: str = ""
    ) -> HeadRun:
        """The launch record for one head of `role`: the profile id plus the configuration it is
        launched with, read from the same snapshot the launcher renders its command from.

        `head` is the profile the bring-up handed to the launcher. There is no substitution between
        the routing decision and the launch: the head is decided once, at claim, from the card's
        override or the role default, so it normally equals what the card asks for. Passing it
        explicitly keeps the record describing the process that runs even when the card's metadata
        is edited afterwards.
        """
        routing = task.get("routing") or {}
        if role == "worker":
            override = routing.get("head_override")
            asked = str(override) if override else str(
                self._heads.get("role_defaults", {}).get("new_card") or "codex"
            )
            codex_mode = str(routing.get("codex_launch_mode") or "")
        else:
            override = routing.get("review_head_override")
            asked = str(override) if override else str(
                self._heads.get("role_defaults", {}).get("reviewer") or "codex-reviewer"
            )
            codex_mode = ""
        launched = str(head) if head else asked
        if launched != asked:
            head_source = HEAD_FROM_RECORD
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
            codex_mode=codex_mode,
            model=model,
            model_source=model_source,
        )

    def observer_head(self) -> str:
        """The head profile a sprint observer is launched with.

        Its own `role_defaults` key, never the worker's: an observer is a different role with a
        different job, and repointing the worker default must not move it. The fallback is a named
        profile rather than a silent reuse of another role's default, and it is validated here, so
        a registry that has neither the key nor the fallback profile fails loudly at bring-up.
        """
        head = str(self._heads.get("role_defaults", {}).get("observer") or OBSERVER_HEAD_FALLBACK)
        self._head_profile(head)
        return head

    def observer_profile(self, head: str) -> dict[str, Any]:
        """The registry entry for a head a sprint declares, or `HostError`.

        The declaration is durable and the registry is not: a sprint can name a profile that was
        removed from `heads.yaml` after it was declared. That is the sprint being unrunnable, not
        an invitation to substitute the role default, so the resolution is one lookup with no
        fallback and the fence answers for the failure.
        """
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

    def head_command(self, head: str, prompt_file: str, *, workspace: str, role: str) -> str:
        return self.head_launch(head, prompt_file, workspace=workspace, role=role).command

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        codex_mode: str | None = None,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
    ) -> HeadLaunch:
        profile = self._head_profile(head)
        adapter = profile.get("adapter") if isinstance(profile, dict) else ""
        try:
            self.prepare_head_workspace(head, workspace, role=role)
            if adapter == "claude":
                launch = HeadLaunch(_render_claude_command(profile, prompt_file, launch_prompt=launch_prompt))
            else:
                launch = _render_codex_launch(
                    profile, prompt_file, workspace=workspace, mode=codex_mode, launch_prompt=launch_prompt
                )
        except HeadLaunchError as exc:
            raise HostError(str(exc)) from None
        try:
            wrapped = _wrap_role_shell_command(role, launch.command, identity=identity)
        except HeadLaunchError as exc:
            raise HostError(str(exc)) from None
        return HeadLaunch(wrapped, prompt_after_start=launch.prompt_after_start)

    def prepare_head_workspace(self, head: str, workspace: str, *, role: str = "") -> None:
        """Pre-answer the first-run questions a head's CLI would otherwise put to an operator.

        The codex branch is observer-only on purpose. Worker and reviewer workspaces are worktrees
        of repositories the codex runtime already trusts, so they never see the dialog, and the
        role that does see it is the only one whose bring-up may touch the runtime's own
        `config.toml`.
        """
        profile = self._head_profile(head)
        adapter = profile.get("adapter") if isinstance(profile, dict) else ""
        try:
            if adapter == "claude":
                _ensure_claude_workspace_ready(workspace)
            elif adapter == "codex" and role == OBSERVER_ROLE:
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


class CommandHostRuntime:
    def __init__(self, catalog: InstanceCatalog, data_dir: Path, *, mode: str = "real") -> None:
        self.catalog = catalog
        self.data_dir = data_dir
        self.mode = mode

    def prepare_worker(
        self,
        task: dict[str, Any],
        worker_id: str,
        head: str,
        *,
        attempt_id: str = "",
        require_existing_workspace: bool = False,
        generation: int = 0,
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
            codex_mode=task.get("routing", {}).get("codex_launch_mode"),
            launch_prompt=self._worker_launch_prompt(),
            task=task,
        )
        return {
            "workspace": workspace,
            "handle": launched.handle,
            "base_branch": base,
            # The launch configuration of the head that went up. The caller records this instead of
            # re-reading the registry, which a later edit would answer differently.
            "run": launched.run,
        }

    def restart_worker(self, task: dict[str, Any], record: DispatcherRecord) -> LaunchedHead:
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
            codex_mode=task.get("routing", {}).get("codex_launch_mode"),
            launch_prompt=self._worker_launch_prompt(),
            task=task,
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

        Orca only gives terminals to worktrees it knows, and it only registers git repositories, so
        a directory made with `mkdir` gets no terminal at all. What the observer needs is that
        registration, not a checkout of the project it watches: it reads the board and the sprint
        entity and writes no code, so a workspace it could commit the project from would be wrong
        as well as unnecessary. Hence a repo of its own, created once and shared by every sprint.
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
        answer, and an unanswered question must not pass for a free path: the caller clears an
        unregistered directory out of the way, and the stop path removes what it finds.
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

        A workspace Orca already knows is reused: a relaunch after a dead pid keeps the sprint's
        directory. An unregistered directory at that path is left over from a bring-up that never
        reached Orca and holds nothing but the prompt file this launch rewrites, so it is cleared
        rather than worked around: `worktree create` would otherwise sidestep it and put the
        workspace at a path no record points at.
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
        """Where this sprint's observer heartbeat writes its pid.

        Path arithmetic over the reference, like `observer_workspace`: the lifecycle records both
        before it calls `prepare_observer`, so a tick that dies mid-launch still leaves behind
        enough to read the head's liveness and find its terminal.
        """
        return _observer_pid_file(reference)

    def prepare_observer(
        self,
        sprint: dict[str, Any],
        head: str,
        *,
        prompt: str,
        identity: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Bring one observer head up on its own workspace and terminal.

        Same rendering and role environment as any other dispatcher-launched head: the command
        comes from the catalog launcher, the process runs through `role_env exec --role observer`,
        and the pid heartbeat wrapper writes the head's own pid where the tick reads it back.

        `identity` is the sprint binding of the record this bring-up is for, rendered into the
        head's command line. It comes from the lifecycle rather than from the sprint document
        passed here, because the record is what the launch is about; a head that reaches the board
        without it can prove nothing about which sprint it observes and is refused there.

        The workspace is registered with Orca first, the way the worker path registers its own:
        `terminal create` takes a worktree selector, and a plain directory is not one.
        """
        reference = str(sprint.get("ref") or "")
        if self.mode == "noop":
            workspace = Path(self.observer_workspace(reference))
            workspace.mkdir(parents=True, exist_ok=True)
        else:
            workspace = self._create_observer_workspace(reference)
        self._write_prompt(workspace / OBSERVER_PROMPT_FILE, prompt)
        pid_file = _observer_pid_file(reference)
        run = self._observer_run(head, str(workspace))
        if self.mode == "noop":
            return {
                "workspace": str(workspace),
                "handle": f"noop:{head}:{workspace.name}:{OBSERVER_PROMPT_FILE}",
                "pid_file": pid_file,
                "run": run,
            }
        # Drop a predecessor's pid before the new head can be read as this launch's liveness.
        Path(pid_file).unlink(missing_ok=True)
        launch = self.catalog.head_launch(
            head,
            OBSERVER_PROMPT_FILE,
            workspace=str(workspace),
            role=OBSERVER_ROLE,
            launch_prompt=_observer_launch_prompt(),
            identity=identity,
        )
        handle = self._create_terminal(
            str(workspace), f"{reference} observer", _with_pid_heartbeat(launch.command, pid_file)
        )
        if launch.prompt_after_start:
            try:
                _deliver_tui_prompt(
                    handle,
                    str(workspace),
                    OBSERVER_PROMPT_FILE,
                    run_json=self._run_json,
                    prompt_text=_observer_launch_prompt(),
                )
            except (TuiDeliveryError, HostError) as exc:
                try:
                    self._stop_observer_terminals(str(workspace))
                except Exception as stop_exc:
                    # The pane is still up. Its handle goes back with the failure, because this
                    # dict is the only pointer to it: reporting a plain bring-up failure would
                    # leave the sprint reading as headless and the next tick would open a second
                    # head beside a head that is already running.
                    raise ObserverLaunchAborted(
                        f"{exc}; observer terminal stop failed: {stop_exc}",
                        handle=handle,
                        workspace=str(workspace),
                        pid_file=pid_file,
                        run=run,
                    ) from None
                raise ObserverLaunchAborted(str(exc)) from None
        return {"workspace": str(workspace), "handle": handle, "pid_file": pid_file, "run": run}

    def stop_observer(self, record: Any) -> None:
        """End one observer head and give back what its bring-up took.

        The stop goes through the workspace, not pane by pane. That workspace belongs to one sprint
        and nothing else runs there, so its terminals are that head: the pane the record names, the
        pane a head adopted from a launch intent has lost the handle of, and the shell Orca opens
        beside a worktree it has just created. The worktree registration goes with them, so a
        stopped sprint leaves Orca neither a terminal of its observer nor a workspace for it.

        A workspace Orca does not know is a head that is already gone, which is what makes the
        retry of a half-finished stop terminate. Anything else Orca refuses — an answer it will not
        give, a stop it will not perform, a worktree it will not remove — is a failed stop: it
        raises, and the lifecycle keeps the record as `stop-pending` and comes back to it. Dropping
        the record instead would leave a live head with nothing pointing at it, and the next tick
        would put a second head on the same sprint.
        """
        if self.mode == "noop":
            return
        workspace = str(getattr(record, "workspace", "") or "")
        if not workspace:
            # A record written before the launch intent named a workspace: the handle is the only
            # pointer left to that head.
            if record.handle:
                self._close_observer_pane(record.handle)
            return
        if not self._observer_workspace_registered(workspace):
            self._confirm_head_process_gone(str(getattr(record, "pid_file", "") or ""))
            return
        self._stop_observer_terminals(workspace)
        # Heartbeat-wrapped heads have their own session, so terminal stop alone cannot prove the
        # observer died. Do not remove its worktree or forget its record until this confirms it.
        self._confirm_head_process_gone(str(getattr(record, "pid_file", "") or ""))
        self._run_json([
            "orca", "worktree", "rm", "--worktree", f"path:{workspace}", "--force", "--json"
        ])

    def observer_status(self, record: Any) -> dict[str, Any]:
        """Read the observer pane's output clock and whether it is ready for a prompt.

        Readiness is Orca's `tui-idle`, the same signal the delivery path waits on before it sends
        to any head. Nothing here reads the screen, so the answer does not depend on which provider
        the observer profile happens to name.

        This is advisory work liveness, not process liveness. The lifecycle still owns the pid
        heartbeat. A pane nothing can be sent to, though, is not a busy observer: a record whose
        handle died with the tick that launched it, a terminal Orca no longer lists and a
        disconnected one all refuse here, so the caller's bounded failure path replaces that head
        instead of waiting for a readiness that can never arrive.
        """
        if self.mode == "noop":
            return {}
        if not record.workspace or not (record.handle or record.leaf):
            raise HostError("observer record names no terminal to read")
        terminals = self._worktree_terminals(str(record.workspace))
        terminal = next(
            (
                item for item in terminals
                if (record.handle and item.get("handle") == record.handle)
                or (record.leaf and item.get("leafId") == record.leaf)
            ),
            None,
        )
        if terminal is None:
            raise HostError("observer terminal is not in the inventory of its workspace")
        if terminal.get("connected") is False:
            raise HostError("observer terminal is not connected")
        readiness = _terminal_readiness(
            str(terminal.get("handle") or ""), run_json=self._run_json
        )
        if readiness == READINESS_UNKNOWN:
            # A probe that failed is not a working observer. Raising puts it on the lifecycle's
            # bounded failure path, where a busy pane would wait forever instead.
            raise HostError("observer terminal readiness could not be read")
        status: dict[str, Any] = {"idle": readiness == READINESS_READY}
        try:
            status["last_activity"] = float(terminal.get("lastOutputAt")) / 1000.0
        except (TypeError, ValueError):
            # Only the idle-recovery path needs the clock, and it says so itself when it is missing.
            pass
        return status

    def nudge_observer(self, record: Any) -> str:
        """Give an idle observer one event-driven turn without replacing its head.

        The prompt goes through the same delivery path as a worker or reviewer continuation: wait
        for the pane, send, re-enter a swallowed prompt, and refuse upwards when the retries run
        out. This reports terminal acceptance only. The observer's later durable resume, consumed
        by the next lifecycle reconciliation, is the sole acknowledgement of the batch.
        """
        if self.mode == "noop":
            return DELIVERY_ACCEPTED
        workspace = str(getattr(record, "workspace", "") or "")
        handle = str(getattr(record, "handle", "") or "")
        leaf = str(getattr(record, "leaf", "") or "")
        if not workspace or not (handle or leaf):
            raise HostError("observer has no terminal handle for an event wake")
        terminals = self._worktree_terminals(workspace)
        terminal = next(
            (
                item for item in terminals
                if (handle and item.get("handle") == handle) or (leaf and item.get("leafId") == leaf)
            ),
            None,
        )
        current = str(terminal.get("handle") or "") if isinstance(terminal, dict) else ""
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
        try:
            return _deliver_interactive_prompt(
                current,
                message,
                run_json=self._run_json,
                ack_out_of_band=True,
            )
        except TuiDeliveryError as exc:
            raise HostError(f"observer wake was not delivered: {exc}") from None

    def _stop_observer_terminals(self, workspace: str) -> None:
        """Stop every pane of an observer workspace.

        One call for the whole workspace rather than a close per handle: `terminal close` answers
        `tab_not_found` for a pane the runtime never gave a UI tab, and that is every pane a
        dispatcher-launched head gets on a headless serve, so a per-handle close reports a stop
        that worked as a stop that failed. This is the same call the worker teardown makes.
        """
        self._run_json(["orca", "terminal", "stop", "--worktree", f"path:{workspace}", "--json"])

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

    def pane_leaf(self, workspace: str, handle: str) -> str:
        return self._pane_leaf(workspace, handle)

    def codex_tui_activity(
        self, task: dict[str, Any], record: DispatcherRecord, kind: str
    ) -> float | None:
        """Return Codex TUI rollout activity without reading the session contents."""
        if not isinstance(getattr(self.catalog, "_heads", None), dict):
            return None
        head = record.review_head if kind == "review" else record.head
        try:
            profile = self.catalog._head_profile(head)
        except HostError:
            # Head snapshots can change while a card is waiting.  That makes the optional TUI
            # supplement unavailable, not the terminal inventory itself unavailable.
            return None
        mode = (
            task.get("routing", {}).get("codex_launch_mode")
            or profile.get("codex_mode", "exec")
        )
        if profile.get("adapter") != "codex" or mode != "tui" or not record.workspace:
            return None
        from triggered_agents.agents.pipeline.codex_sessions import latest_activity_for
        return latest_activity_for(record.workspace)

    def start_review(self, task: dict[str, Any], record: DispatcherRecord) -> ReviewLaunch:
        """Bring the reviewer up as a second pane inside the worker's own worktree.

        The pane is split off a live pane there rather than created as a new terminal: a plain
        `terminal create` on a headless serve lands as a background surface a client that already
        has the worktree open never materialises, so the operator would have no reviewer to watch.
        Once the reviewer has its own pane the worker head is shut down and the commit it left is
        pinned, so the reviewer judges a checkout nothing else is still editing."""
        if not record.workspace:
            raise HostError("review workspace is unavailable")
        workspace = Path(record.workspace)
        if self.mode == "noop":
            workspace.mkdir(parents=True, exist_ok=True)
        elif not workspace.is_dir():
            raise HostError("review workspace is missing")
        review_file = Path(record.workspace) / "REVIEW.md"
        self._clear_body_file("verdict", task["ref"], record.review_baseline)
        self._write_prompt(
            review_file,
            self._review_prompt(task, record.attempt_id, record.review_baseline, record=record),
        )
        launched = self._launch(
            record.workspace,
            review_pane_label(task["ref"]),
            record.review_head,
            "REVIEW.md",
            role="reviewer",
            env_name="SECRETARY_DISPATCHER_REVIEW_COMMAND",
            split_from=self._split_anchor(record),
            task=task,
        )
        try:
            if record.worker_continuation.retained:
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
                workspace=record.workspace,
                pid_file=_pid_file_path("review", task["ref"]),
            ) from None
        try:
            leaf = self._pane_leaf(record.workspace, launched.handle)
        except Exception as exc:  # noqa: BLE001 — a failure over a reviewer that is already up
            # Both heads are settled by now and only the pane's leaf id is missing, so this is not
            # a bring-up that left no reviewer: it goes back with the pane it opened, and the caller
            # keeps the launch intent instead of blocking the card over a live head.
            raise HeadLaunchAborted(
                f"review pane identity could not be read: {exc}",
                handle=launched.handle,
                workspace=record.workspace,
                pid_file=_pid_file_path("review", task["ref"]),
            ) from None
        return ReviewLaunch(
            handle=launched.handle,
            leaf=leaf,
            commit=self.head_commit(record),
            run=launched.run,
        )

    def review_running(self, task: dict[str, Any], record: DispatcherRecord) -> bool:
        return _command_review_running(self, task, record)

    def worker_status(self, task: dict[str, Any], record: DispatcherRecord) -> dict[str, Any]:
        return _command_terminal_status(self, task, record, kind="worker")

    def review_status(self, task: dict[str, Any], record: DispatcherRecord) -> dict[str, Any]:
        return _command_terminal_status(self, task, record, kind="review")

    def stop_review(self, record: DispatcherRecord) -> None:
        """End the reviewer's lifecycle alone. `stop` would take the whole worktree down with it,
        which on a red verdict means killing the checkout's terminals the worker is about to get
        back. Closing the reviewer's own split leaf removes that pane and leaves the rest alone.

        A reviewer adopted from a launch intent is stopped by its pid heartbeat, for the same
        reason the worker freeze is: without it the red-verdict bounce, the reviewer respawn and
        the pipeline freeze would all leave a live reviewer behind and start a head beside it."""
        if self.mode == "noop" or not (record.review_handle or record.review_pid_file):
            return
        self.stop_head(record, "review")

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

    def is_ancestor(self, record: DispatcherRecord, ancestor: str, descendant: str) -> bool:
        if self.mode == "noop" or not record.workspace:
            return False
        try:
            self._run(
                ["git", "-C", record.workspace, "merge-base", "--is-ancestor", ancestor, descendant],
                "review head ancestry",
            )
        except HostError:
            return False
        return True

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

        The namespace under the workspaces root is Orca's, not the Secretary's: Orca puts a worktree
        at <root>/<repo registration name>/<worktree name>, and that registration name is the
        binding's `orca_binding`. Reconstructing it from the Secretary project id was the defect:
        `codegen-orchestrator` and `codegen_orchestrator` are the same project spelled by two
        different authorities, and the id is not the one that decides where the checkout goes.
        """
        if self.mode == "noop":
            return str(self.data_dir / "dispatcher" / "workspaces" / worker)
        root = Path(os.environ.get("SECRETARY_DISPATCHER_WORKSPACES_ROOT", str(Path.home() / "orca" / "workspaces")))
        return str(root / self._orca_binding_name(str(task.get("project") or "")) / worker)

    def _orca_binding_name(self, project: str) -> str:
        """The Orca repo registration name this project's workspaces are namespaced by.

        Configured `orca_binding` first: an enabled binding is required to carry it, and it is what
        the host plan registers the repo under, so it answers without a live call. A binding without
        one is resolved from Orca itself by repo path.
        """
        binding = self.catalog.binding(project)
        name = binding.get("orca_binding")
        if isinstance(name, str) and name:
            return name
        return str(self._orca_repo(project).get("displayName") or "")

    def _orca_repo(self, project: str) -> dict[str, Any]:
        """This project's Orca repo registration, resolved from the configured repo path.

        The repo path is the one fact both authorities agree on, so it is the join key. The `id` it
        answers with is what a returned worktree is checked against; no name or path is compared as
        a string.
        """
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
        """Publish an instance-repo card without racing checkpoint commits.

        Checkpoints are normal local commits in the instance repo. If one was
        already pushed, fold only the local checkout's known checkpoint history
        into the reviewed branch. A remote tip that is neither in the reviewed
        branch nor in the local checkpoint history is true foreign divergence.
        """
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
        """Land a github-CI project through its PR. gh honours branch protection and refuses to
        merge while required checks are unsatisfied, so a non-green CI never lands even though the
        dispatcher has already re-run the gate on this same tick. Then fast-forward the project's
        own checkout (from the worker workspace's origin) so the next worktree bases on the merged
        tree, matching the local-merge path.

        The checkout tracks the project's default branch, not the card's base: a stacked card bases
        on another `pipeline/<ref>` branch, and `_create_workspace` fetches that base itself. So the
        refresh follows the default branch even when the PR landed on a stacked base, where
        `origin/<base>` is a sibling of the checkout's branch and any unrelated card that merged
        meanwhile makes it unmergeable. The card is already merged at this point, so the refresh
        failing there is not the card's failure either; it stays best-effort and the deliverable
        does not get reported as a merge that did not happen."""
        self._run(["gh", "pr", "merge", branch, "--merge"], "merge pr", cwd=Path(record.workspace))
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        default_branch = self.catalog.default_branch(task["project"], None)
        if base == default_branch:
            self._run(["git", "-C", str(repo), "fetch", "origin", base], "post-merge fetch")
            self._run(["git", "-C", str(repo), "merge", "--ff-only", f"origin/{base}"], "post-merge fast-forward")
            return
        try:
            self._run(
                ["git", "-C", str(repo), "fetch", "origin", default_branch],
                "post-merge fetch",
            )
            self._run(
                ["git", "-C", str(repo), "merge", "--ff-only", f"origin/{default_branch}"],
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

        The confirmed twin of `stop`. A path that opens a replacement head afterwards cannot use
        the suppressing one: `orca terminal stop` refusing is not evidence the head is gone, and
        the replacement would then be the second process on the same checkout.

        `selector_not_found` is the one exception: as in `_observer_workspace_registered`, it means
        Orca has no worktree left at this path at all, which a stop cannot be refused on since
        there is nothing there to refuse stopping. A workspace already removed out from under the
        dispatcher (a manual cleanup, a PO taking a card back out of the cycle) must not read as a
        stop that failed.
        """
        if self.mode == "noop" or not record.workspace:
            return
        try:
            self._run_json(
                ["orca", "terminal", "stop", "--worktree", f"path:{record.workspace}", "--json"]
            )
        except HostError as exc:
            if "selector_not_found" not in str(exc):
                raise
        for pid_file in (record.worker_pid_file, record.review_pid_file):
            self._confirm_head_process_gone(pid_file)

    def stop_head(self, record: DispatcherRecord, kind: str) -> None:
        """Stop one role's head and confirm it is gone, or raise.

        Identity is the pane handle when the record has one and the pid heartbeat when it does not:
        a head adopted from a launch intent lost its handle with the tick that opened it, and
        without the heartbeat every stop for it would be a silent no-op — the worker left editing
        the checkout under review, the reviewer left running beside its replacement.

        The pane close stays best-effort because Orca answers `tab_not_found` for a pane it never
        gave a UI tab, which is every pane a dispatcher-launched head gets on a headless serve. The
        heartbeat is what actually decides: a head still answering it after the close is a stop
        that did not happen, and it raises rather than letting a replacement start.
        """
        handle = record.review_handle if kind == "review" else record.handle
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        if self.mode == "noop":
            return
        if handle:
            _close_tui_terminal(handle, run_json=self._run_json)
        elif not pid_file:
            # Neither identity: nothing here can name that head, so nothing here can promise it is
            # gone. The caller answers by not launching a replacement.
            raise HostError(f"{kind} head has neither a pane handle nor a pid heartbeat")
        self._confirm_head_process_gone(pid_file)

    def _confirm_head_process_gone(self, pid_file: str) -> None:
        """Make sure the process behind a heartbeat is not running, escalating if it is.

        A pid file that was never written (a raw `SECRETARY_DISPATCHER_*_COMMAND` override skips
        the heartbeat wrapper) says nothing either way, and the close that came before is then all
        the evidence there is.
        """
        if not pid_file:
            return
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            status = _head_process_status(pid_file)
            if not status.get("known") or not status.get("alive"):
                if status.get("known"):
                    Path(pid_file).unlink(missing_ok=True)
                return
            # SIGTERM and SIGHUP remain pending for a SIGSTOPed retained worker.  Wake its group
            # before the graceful signal so green handoff does not wait out the whole grace period
            # and then kill the worker unconditionally.
            if signal_number == signal.SIGTERM:
                self._signal_head(pid_file, signal.SIGCONT)
            self._signal_head(pid_file, signal_number)
            self._await_head_exit(pid_file)
        status = _head_process_status(pid_file)
        if status.get("known") and status.get("alive"):
            raise HostError(f"head process from {pid_file} is still running after stop")
        if status.get("known"):
            Path(pid_file).unlink(missing_ok=True)

    def _signal_head(self, pid_file: str, signal_number: int) -> None:
        try:
            pid = int(Path(pid_file).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return
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

    def _await_head_exit(self, pid_file: str) -> None:
        deadline = time.monotonic() + HEAD_STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            status = _head_process_status(pid_file)
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

        What Orca returns is checked against what Orca itself registered: the worktree has to belong
        to the repo registration this project's binding resolves to, and to carry this card's worker
        id as its own name. That is an identity check on Orca's records, so a path from another
        repo, another card, or nowhere at all fails closed without any path being reconstructed.

        `expected` is `restore_workspace`'s answer, which the launch intent wrote to disk before
        this call. A tick that dies right after this call can only find the head through the intent,
        and an intent naming the wrong directory would send every later review, stop, respawn and
        teardown to a checkout with nothing in it, so a workspace that landed anywhere else is a
        deferred bring-up with a readable reason. Same invariant the observer workspace holds.

        A create that succeeded and then failed any of this is removed before the failure is raised:
        the path is known, so this is the deterministic case, and leaving it registered would leave
        an orphan behind.
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

        Orca's own record answers it. A worktree it will not describe is not a worktree this card
        may run in either, so an unreadable answer is a rejection rather than a pass.
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
        """Remove a worktree that was created but must not be adopted, and say what is left.

        Returns "" when Orca confirmed the removal, and otherwise a note naming the registration
        that survived, so the failure the caller raises says whether an orphan remains. Only ever
        called over a path Orca has just answered with, never over an adopted workspace.
        """
        try:
            self._run_json(
                ["orca", "worktree", "rm", "--worktree", f"path:{path}", "--force", "--json"]
            )
        except HostError as exc:
            return f"; the rejected worktree at {path} could not be removed either: {exc}"
        return ""

    def _validate_resumable_workspace(self, task: dict[str, Any], workspace: str) -> None:
        """Accept only the registered project worktree on this card's worker branch.

        A Blocked card can be returned to Ready after an infrastructure outage. Its old checkout is
        valuable state, including commits and WIP, but a path merely happening to exist must never
        be adopted as a dispatcher workspace.
        """
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
        codex_mode: str | None = None,
        launch_prompt: str | None = None,
        split_from: str = "",
        task: dict[str, Any] | None = None,
    ) -> LaunchedHead:
        """Bring one head up and hand back the pane together with the configuration it started with.

        The snapshot is taken here, on the bring-up path itself, so every route out of this call
        (the real launcher, the `SECRETARY_DISPATCHER_*_COMMAND` override, noop mode) reports the
        same thing, and no caller has to re-read the registry afterwards.
        """
        if self.mode == "noop":
            return self._launched(
                f"noop:{head}:{Path(workspace).name}:{prompt_file}", head, task, role, workspace
            )
        pid_file = _pid_file_path(_watchdog_kind(role), task["ref"]) if task else ""
        if pid_file:
            # Drop any pid a previous launch in this same workspace left behind, so a respawn
            # cannot read a dead predecessor's pid as this launch's liveness signal before the new
            # head has had a chance to overwrite it (secretary-751).
            Path(pid_file).unlink(missing_ok=True)
        command = os.environ.get(env_name)
        launch = HeadLaunch(command) if command else None
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
                codex_mode=codex_mode,
                launch_prompt=launch_prompt,
            )
            command = launch.command
            if pid_file:
                command = _with_pid_heartbeat(command, pid_file)
        if split_from:
            handle = self._split_pane(
                split_from, title, command, workspace=workspace, pid_file=pid_file
            )
        else:
            handle = self._create_terminal(workspace, title, command)
        if launch and launch.prompt_after_start:
            try:
                _deliver_tui_prompt(
                    handle, workspace, prompt_file, run_json=self._run_json, prompt_text=launch_prompt
                )
            except (TuiDeliveryError, HostError) as exc:
                # The terminal is already up, so a failure here is not proof that no head exists.
                # The close decides which: confirmed, nothing of this bring-up is left and the
                # caller may treat it as a launch that did not happen; refused, the pane goes back
                # with the failure so the caller keeps its launch intent and the next tick settles
                # the head instead of opening a second one beside it.
                try:
                    self._close_launched_pane(handle, pid_file)
                except HostError as stop_exc:
                    raise HeadLaunchAborted(
                        f"{exc}; head terminal stop failed: {stop_exc}",
                        handle=handle,
                        workspace=workspace,
                        pid_file=pid_file,
                    ) from None
                raise HostError(str(exc)) from None
        return self._launched(handle, head, task, role, workspace)

    def _close_launched_pane(self, handle: str, pid_file: str) -> None:
        """Close a pane this bring-up opened and confirm nothing of its head survived.

        The close is asked strictly, but its refusal is not the answer on its own: Orca reports
        `tab_not_found` for a pane it never gave a UI tab, which is every pane a dispatcher-launched
        head gets on a headless serve. The heartbeat decides. Only when there is no heartbeat to
        read is a refused close taken at face value, because then nothing else can say whether the
        head is still there.
        """
        try:
            _close_tui_terminal_strict(handle, run_json=self._run_json)
        except Exception as exc:  # noqa: BLE001 — any refusal, whatever the transport called it
            status = _head_process_status(pid_file) if pid_file else {"known": False}
            if not status.get("known"):
                raise HostError(f"head terminal close failed: {exc}") from None
        self._confirm_head_process_gone(pid_file)

    def _launched(
        self, handle: str, head: str, task: dict[str, Any] | None, role: str, workspace: str = ""
    ) -> LaunchedHead:
        """Pair the pane with the launch snapshot of the head running in it.

        A registry that no longer describes the launched head still has to yield a usable record:
        the point of the journal is that it survives edits to `heads.toml`. So an unreadable profile
        degrades to the head id under an `unknown` adapter instead of failing a bring-up that
        already succeeded.
        """
        if task is None:
            return LaunchedHead(handle=handle, head=head)
        try:
            run = self.catalog.head_run(task, role=role, head=head, workspace=workspace).to_json()
        except (HostError, AttributeError, KeyError, TypeError):
            run = HeadRun(
                role=role, head=head, adapter="unknown", model_source=MODEL_UNKNOWN
            ).to_json()
        return LaunchedHead(handle=handle, head=head, run=run)

    def _create_terminal(self, workspace: str, title: str, command: str) -> str:
        result = self._run_json([
            "orca", "terminal", "create",
            "--worktree", f"path:{workspace}",
            "--title", title,
            "--command", command,
            "--json",
        ])
        terminal = result.get("terminal") if isinstance(result.get("terminal"), dict) else result
        handle = terminal.get("handle") or terminal.get("id") if isinstance(terminal, dict) else None
        if not isinstance(handle, str) or not handle:
            raise HostError("orca did not return a terminal handle")
        return handle

    def _split_pane(
        self,
        split_from: str,
        title: str,
        command: str,
        *,
        workspace: str = "",
        pid_file: str = "",
    ) -> str:
        """Run `command` in a new pane beside an existing one. `terminal split` takes no --title,
        so the label goes on afterwards; a label that will not stick is a failed bring-up rather
        than an unlabelled pane, because the operator has no other way to tell the panes apart.

        The head is already running by then, so the cleanup decides what kind of failure this is:
        a confirmed close leaves nothing of the bring-up and the rename failure travels as the
        ordinary error it is, while a close that cannot promise the head is gone hands the pane
        back as `HeadLaunchAborted` and the caller keeps its launch intent."""
        result = self._run_json([
            "orca", "terminal", "split",
            "--terminal", split_from,
            "--direction", "vertical",
            "--command", command,
            "--json",
        ])
        split = result.get("split") if isinstance(result.get("split"), dict) else result
        handle = split.get("handle") if isinstance(split, dict) else None
        if not isinstance(handle, str) or not handle:
            raise HostError("orca did not return a split terminal handle")
        try:
            self._run_json([
                "orca", "terminal", "rename",
                "--terminal", handle,
                "--title", title,
                "--json",
            ])
        except HostError as exc:
            try:
                self._close_launched_pane(handle, pid_file)
            except HostError as stop_exc:
                raise HeadLaunchAborted(
                    f"{exc}; head terminal stop failed: {stop_exc}",
                    handle=handle,
                    workspace=workspace,
                    pid_file=pid_file,
                ) from None
            raise
        return handle

    def _split_anchor(self, record: DispatcherRecord) -> str:
        """Pane to split the reviewer off. The worker's own pane when it is still connected, so
        both heads of a card end up in one tab; otherwise any live pane in the same worktree.
        Empty when the worktree has no live pane left — the caller then falls back to creating a
        terminal, which is less visible but still gets the card reviewed."""
        terminals = self._worktree_terminals(record.workspace)
        connected = [
            terminal for terminal in terminals if terminal.get("connected") is not False
        ]
        for terminal in connected:
            if record.handle and terminal.get("handle") == record.handle:
                return record.handle
        return str(connected[0].get("handle") or "") if connected else ""

    def _pane_leaf(self, workspace: str, handle: str) -> str:
        for terminal in self._worktree_terminals(workspace):
            if terminal.get("handle") == handle:
                return str(terminal.get("leafId") or "")
        return ""

    def _worktree_terminals(self, workspace: str) -> list[dict[str, Any]]:
        """Terminal inventory for a worktree, or [] when it cannot be read. Callers use it to pick
        a pane, never to decide a head is dead, so an unreadable inventory degrades into a weaker
        choice rather than a failed tick."""
        if self.mode == "noop" or not workspace:
            return []
        try:
            data = self._run_json([
                "orca", "terminal", "list", "--worktree", f"path:{workspace}", "--json"
            ])
        except HostError:
            return []
        payload = data.get("result") if isinstance(data.get("result"), dict) else data
        terminals = payload.get("terminals") if isinstance(payload, dict) else []
        return [terminal for terminal in terminals if isinstance(terminal, dict)] if isinstance(terminals, list) else []

    def _freeze_worker(self, record: DispatcherRecord) -> None:
        """Shut the worker head down now that the reviewer is up. Nothing else stops the worker
        from editing the checkout mid-review, which would leave the verdict describing a tree that
        no longer exists. The workspace itself is untouched: a red verdict hands it straight back.

        A worker adopted from a launch intent has no pane handle, so the stop goes by its pid
        heartbeat instead: a freeze that quietly did nothing for want of a handle would leave that
        worker editing the tree the reviewer is judging."""
        if self.mode == "noop" or not (record.handle or record.worker_pid_file):
            return
        self.stop_head(record, "worker")

    def retain_worker(self, record: DispatcherRecord) -> None:
        """Suspend a completed worker without throwing its provider conversation away.

        The pid heartbeat names the actual head rather than its surrounding terminal shell.  A
        missing or dead heartbeat is not safe to retain: the caller falls back through the
        confirmed-stop and durable replacement path instead of guessing that a pane is idle.

        A head with no pane handle is not retained at all: nothing can address it, so there is no
        conversation to keep and the caller stops it before the reviewer takes the checkout.
        Whether the provider behind that pane will actually take a continuation is decided at
        delivery, where a refusal falls back to a confirmed stop and a replacement.
        """
        if self.mode == "noop":
            raise HostError("noop runtime cannot retain a worker session")
        if not record.handle:
            raise HostError("worker session has no addressable pane to retain")
        status = _head_process_status(record.worker_pid_file)
        if not status.get("known") or not status.get("alive"):
            raise HostError("worker session is unavailable for retention")
        try:
            self._signal_head(record.worker_pid_file, signal.SIGSTOP)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise HostError(f"worker session could not be suspended: {exc}") from None
        deadline = time.monotonic() + HEAD_STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            retained = _head_process_status(record.worker_pid_file)
            if retained.get("known") and retained.get("alive") and retained.get("stopped"):
                return
            if not retained.get("alive"):
                break
            time.sleep(HEAD_STOP_POLL_SECONDS)
        raise HostError("worker session could not be confirmed suspended")

    def _continuation_addressable(self, record: DispatcherRecord) -> bool:
        """Whether this worker head is a live provider conversation a prompt can be sent into.

        Codex TUI and Claude's interactive terminal are; a one-shot Codex exec worker has already
        spent its turn, and a head with no pane handle cannot be typed into at all.
        """
        run = record.worker_run
        adapter = run.get("adapter")
        return bool(record.handle) and (
            adapter == "claude" or (adapter == "codex" and run.get("codex_mode") == "tui")
        )

    def worker_retained_alive(self, record: DispatcherRecord) -> bool:
        """Whether this card's worker session is confirmably alive and still suspended.

        The record saying `retained` is a memory of a past tick. Only the heartbeat says whether
        that head is still frozen now, and an ambiguous answer is never permission to leave it
        beside a reviewer.
        """
        if self.mode == "noop" or not record.worker_continuation.retained:
            return False
        status = _head_process_status(record.worker_pid_file)
        return bool(status.get("known") and status.get("alive") and status.get("stopped"))

    def confirm_worker_retained(self, record: DispatcherRecord) -> None:
        """Assert the retained worker is still suspended, or raise for the caller's stop path."""
        if self.mode == "noop":
            return
        if not self.worker_retained_alive(record):
            raise HostError("retained worker session is no longer confirmably suspended")

    def resume_worker(self, task: dict[str, Any], record: DispatcherRecord) -> None:
        """Resume an addressable retained worker and deliver its updated rework task.

        Codex TUI and Claude's interactive terminal are both live provider conversations.  A
        Codex exec worker has already spent its turn, so it deliberately follows the replacement
        path. A running process after a crash is only known to have received the prompt when its
        provider turn is visibly underway; otherwise replay resumes at the SIGCONT/send boundary.
        """
        status = _head_process_status(record.worker_pid_file)
        if not status.get("known") or not status.get("alive"):
            raise HostError("retained worker session exited")
        if not self._continuation_addressable(record):
            raise HostError("retained worker session cannot accept a continuation")
        adapter = record.worker_run.get("adapter")
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
            adapter=str(adapter or ""),
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
        try:
            if status.get("stopped"):
                self._signal_head(record.worker_pid_file, signal.SIGCONT)
            prompt = _continuation_prompt(continuation.phase, generation, task["ref"], decision)
            if adapter == "codex":
                _deliver_tui_prompt(
                    record.handle, str(workspace), "TASK.md", run_json=self._run_json,
                    prompt_text=prompt,
                )
            else:
                _deliver_interactive_prompt(
                    record.handle,
                    prompt,
                    run_json=self._run_json,
                    confirm=_turn_started_confirm(
                        record.handle, str(workspace), str(adapter or ""),
                        run_json=self._run_json,
                    ),
                )
        except (TuiDeliveryError, HostError) as exc:
            raise HostError(f"retained worker continuation was not delivered: {exc}") from None

    def _set_worker_branch(self, workspace: str, branch: str) -> None:
        if self.mode == "noop":
            return
        # A fresh worktree may start on the base branch, but the target name must never be
        # force-updated. In particular, a preserved checkout elsewhere can already own it.
        self._run(["git", "-C", workspace, "branch", "-m", branch], "git branch")

    def _write_prompt(self, path: Path, body: str) -> None:
        write_text_atomic(path, body)

    def _clear_body_file(self, kind: str, reference: str, review_round: int) -> None:
        """Drop the body file before launching the head that is supposed to write it.

        Heads are told to leave the file in place, and the path is keyed on ref+round, so a
        respawned head inherits whatever its half-dead predecessor left there. Nothing downstream
        catches a stale body: `_read_body` only rejects a missing file, and `report done` /
        `verdict green` accept an empty one, so a truncated body would land on the board as a real
        report. A missing file at least fails loudly.

        This is the reviewer's path. A report goes through `_clear_report_bodies`, which clears the
        rounds already over as well."""
        try:
            Path(_body_file_path(kind, reference, review_round)).unlink(missing_ok=True)
        except OSError:
            pass

    def _clear_report_bodies(self, reference: str) -> None:
        """Drop every report body file this card has, the round about to start included.

        The new round's path is cleared for the reason above. The rounds already over are cleared
        because their file is the last thing that makes a command from an earlier round look like a
        report of this one: the task protocol answers an identical retry from its committed event,
        and an unchanged body file left in place is exactly how a retained conversation produces
        one. With the file gone the stale command fails on a missing body instead of telling a
        worker that a report the board never received was recorded. A worker that types the round's
        body again into the old path is still a retry the protocol may answer; only authorising the
        open generation inside the report protocol closes that, which is a durable protocol change.
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
        the whole task twice. The head opens TASK.md itself and reports with the command there."""
        return (
            "The full task is in TASK.md at the workspace root. Read it first and follow it. "
            "Report done or blocked with the command given in TASK.md. Do not commit TASK.md."
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
        completed = self._run(args, " ".join(args))
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

    def _require_head_ready(self, head: str) -> None:
        readiness = self.head_readiness(head)
        if not readiness.launch_allowed:
            raise HostError(f"head resource {readiness.resource} is {readiness.status}: {readiness.reason}")

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
        head = self.catalog.worker_head(task)
        readiness = self.head_readiness(head)
        if not readiness.launch_allowed:
            return {
                "status": "skipped",
                "step": "head-preflight",
                "action": "resource-not-ready",
                "pilot_ref": ref,
                "head": head,
                "readiness": readiness.to_json(),
                "reason": readiness.reason,
            }
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
                    active, records, payload, ref, step="claim", attempt_id=attempt_id
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
        review_head = self.catalog.review_head(task)
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
            record.workspace = self.host.restore_workspace(claimed, record.worker)
            record.worker_pid_file = _launch_pid_file(WORKER_ROLE, ref)
            records[ref] = record
            try:
                self.host.stop_workspace(record)
            except HostError as exc:
                self.writer.move(
                    role="dispatcher",
                    actor=self.owner,
                    reference=ref,
                    target="blocked",
                    reason=(
                        "dispatcher found an unowned live worker and could not confirm its stop: "
                        f"{scrub_host_output(str(exc))}"
                    ),
                    request_id=_attempt_request_id(
                        record.attempt_id, "orphan-worker-stop-blocked", ref
                    ),
                )
                self.save_records(payload, records)
                return {
                    "status": "blocked",
                    "step": "claim",
                    "action": "orphan-worker-stop-unconfirmed",
                    "pilot_ref": ref,
                    "attempt_id": record.attempt_id,
                    "reason": "an unowned live worker could not be stopped",
                }
            _forget_role_head(record, WORKER_ROLE)
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
            # A launch nobody can record is exactly how a card ends up with two heads, so the host
            # is not touched at all. The card keeps its claim and the next tick launches again.
            return _launch_intent_unwritable(
                step="claim", ref=ref, attempt_id=record.attempt_id, role=WORKER_ROLE, reason=failure
            )
        try:
            prepared = self.host.prepare_worker(
                claimed,
                record.worker,
                record.head,
                attempt_id=record.attempt_id,
                require_existing_workspace=require_existing_workspace,
                generation=record.report_generation,
            )
        except (HeadLaunchAborted, HostError) as exc:
            aborted = self._worker_launch_failure(
                payload, records, ref, record, exc, step="claim", attempt_id=record.attempt_id
            )
            if aborted is not None:
                return aborted
            _clear_launch_intent(record)
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="blocked",
                reason=f"dispatcher bring-up failed: {scrub_host_output(str(exc))}",
                request_id=_attempt_request_id(record.attempt_id, "bringup-blocked", ref),
            )
            records.pop(ref, None)
            self.save_records(payload, records)
            return {"status": "blocked", "step": "claim", "pilot_ref": ref, "reason": "host bring-up failed"}
        record.workspace = prepared["workspace"]
        # The intent carries the pane and the launch snapshot before the record does: from here on
        # every failure is one over a worker that is already running.
        _confirm_launch_intent(
            self,
            payload,
            records,
            ref,
            record,
            handle=str(prepared.get("handle") or ""),
            run=prepared.get("run"),
        )
        try:
            self._settle_worker_pane(ref, record, prepared["handle"])
        except HeadLaunchAborted as exc:
            return self._worker_launch_aborted(
                payload, records, ref, record, exc, step="claim", attempt_id=record.attempt_id
            )
        record.worker_started_at = record.worker_progress_at = time.time()
        record.state = "claimed"
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
        return {
            "status": "ok",
            "step": "claim",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "worker": record.worker,
            "workspace": prepared["workspace"],
        }

    def _end_review_pane_confirmed(
        self,
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        ref: str,
        *,
        step: str,
        attempt_id: str,
    ) -> dict[str, Any] | None:
        """End the reviewer before a replacement head opens. Returns the tick's outcome on refusal.

        A refusal leaves the record pointing at that reviewer exactly as it was, so the next tick
        retries the same stop. Nothing after this call runs on this tick: whatever it was going to
        open — the rework worker, the replacement reviewer — would be the second process in a
        checkout the previous head may still be holding.
        """
        try:
            _end_review_pane(self.host, record)
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
            if record.handle or record.worker_pid_file:
                self.host.stop_head(record, "worker")
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
        """A worker bring-up that failed with its terminal already open.

        Nothing is blocked and nothing is dropped: the host could not say the head is gone, so the
        launch intent stays on disk carrying whatever identity the failure knew. The next tick
        reads the heartbeat and either adopts that head or stops it. Moving the card to Blocked and
        removing its record here — which is what an ordinary bring-up failure does, because there
        it is true that no head exists — would leave a live worker on a checkout nothing points at.
        """
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

        `HeadLaunchAborted` says so outright. An ordinary failure claims the opposite, and is taken
        at its word only while the heartbeat agrees: a bring-up that got far enough to write one
        left a process behind whatever the error said, and blocking the card and dropping its record
        over that process is exactly the head the next requeue would open a second one beside.
        """
        if not isinstance(exc, HeadLaunchAborted):
            if not _launch_left_a_head(record):
                return None
            exc = HeadLaunchAborted(
                str(exc),
                workspace=record.workspace,
                pid_file=_launch_pid_file(WORKER_ROLE, ref),
            )
        return self._worker_launch_aborted(
            payload, records, ref, record, exc, step=step, attempt_id=attempt_id
        )

    def _settle_worker_pane(self, ref: str, record: DispatcherRecord, handle: str) -> None:
        """Put the pane identity of a worker head that is already up onto its record.

        Everything from here runs against a live process, so a failure is ambiguous rather than a
        launch that did not happen: it comes back as `HeadLaunchAborted` carrying that pane, the
        caller keeps the launch intent, and the next tick settles the head instead of the record
        being dropped out from under it.
        """
        record.handle = handle
        try:
            record.worker_leaf = self.host.pane_leaf(record.workspace, handle)
        except Exception as exc:  # noqa: BLE001 — any failure over a head that already exists
            raise HeadLaunchAborted(
                f"worker pane identity could not be read: {scrub_host_output(str(exc))}",
                handle=handle,
                workspace=record.workspace,
                pid_file=_launch_pid_file(WORKER_ROLE, ref),
            ) from None

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
        """Relaunch this card's worker in its own workspace, under the intent already on disk.

        Returns the launched head, or the tick's outcome when nothing usable came up. Every step
        after `restart_worker` runs over a process that exists, so a failure there is ambiguous and
        the record must not be dropped for it: only a launch that left no head at all clears the
        intent and blocks the card. The caller keeps the intent until it has written the head's
        routing event, which is the last thing the round owes a bring-up.
        """
        ref = task["ref"]
        try:
            self._require_head_ready(record.head)
            launched = self.host.restart_worker(task, record)
        except Exception as exc:  # noqa: BLE001 — classified by what it left running, not by type
            aborted = self._worker_launch_failure(
                payload, records, ref, record, exc, step=step, attempt_id=attempt_id
            )
            if aborted is not None:
                return None, aborted
            _clear_launch_intent(record)
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
        # The head is up. Its pane and its launch configuration go into the intent before anything
        # else is attempted with them, so an adoption gets the run that actually launched.
        _confirm_launch_intent(
            self, payload, records, ref, record, handle=launched.handle, run=launched.run
        )
        try:
            self._settle_worker_pane(ref, record, launched.handle)
        except HeadLaunchAborted as exc:
            return None, self._worker_launch_aborted(
                payload, records, ref, record, exc, step=step, attempt_id=attempt_id
            )
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
        """Fix a rework or respawn bring-up on disk before `restart_worker` is called.

        Every relaunch reuses the workspace the record already names, so the intent has the head's
        identity from the record itself. A rework passes the round it has reserved for the head it
        is about to start, so an adoption resumes that round and not the one being left behind.
        Returns the failure, or None when the launch may proceed.
        """
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
                self.host.stop(record)
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
            self.host.stop(record)
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
            record.rejected_done_reports = rejected
            self.host.stop(record)
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
                record, records, payload, ref, step="review", attempt_id=attempt_id
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
            idle = _idle_outcome(record, status, kind=kind, now=now)
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
                record, records, payload, ref, step=step, attempt_id=attempt_id
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
        self.host.stop(record)
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
        """Validate and persist every green gate through one exact-SHA policy boundary.

        ``initial`` supplies review evidence, ``assessment`` publishes the fresh post-review
        receipt used to park, and ``release`` publishes the fresh final pre-merge receipt.  A
        local/GitHub gate can never pass this boundary without evidence; none/noop remains an
        explicit absence of evidence and therefore emits no attestation audit.
        """
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

        This is deliberately separate from ``ci:none``: the latter is an explicit no-mechanical-
        validation mode, while local/github promised to execute a check and therefore fail closed
        if the SHA/base/check receipt cannot be materialized.
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
            record, records, payload, ref, step="gate", attempt_id=attempt_id
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

        A red gate and a red review differ in the comment they leave and in the identities they
        dedupe on, not in the order they owe the card. That order lives here and nowhere else: the
        intent is on disk, with its phase, the report baseline it was opened against and the reason
        the card is moving, before anything observable moves; the board moves; and only then is it
        decided whether the round's own session takes the continuation or a replacement does.

        Whether a session is held is deliberately not a precondition of this call. The round with
        nothing to reuse is the one whose replacement a crash would otherwise lose, leaving the
        report that closed the round to be read as a fresh completion.
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

        Every tick that finds one comes here, whichever phase opened it and whether or not a session
        is held, before it reads a gate result, a report marker or a review verdict. The move is
        keyed on the baseline the intent was opened against, so the tick that already moved the card
        and the tick recovering from a crash before that move run the same call and the card moves
        once. Nothing here re-reads the verdict: the transition carries its own reason, so a gate
        that has since turned green cannot talk this card out of the red round it already owes.
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

        Both red verdicts and the recovery of an interrupted red transition come through here, so
        the order is the same for the gate and for the review: the suspension is re-confirmed at the
        moment of use, the delivery boundary is durable before the worker is woken, and every way
        out that cannot reuse the session goes through a confirmed stop first.
        """
        ref = task["ref"]
        continuation = record.worker_continuation
        step = "review" if phase == "review" else "gate"
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
            # Persist the delivery boundary before waking the worker. If this tick dies after
            # delivery, replay stays on this branch instead of treating the old done marker as a
            # completion from the new rework round.
            continuation.begin_delivery(phase, time.time())
            records[ref] = record
            self.save_records(payload, records)
            try:
                self.host.resume_worker(task, record)
            except HostError as exc:
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

    def _finish_retained_worker_resume(
        self, task: dict[str, Any], record: DispatcherRecord, records: dict[str, DispatcherRecord],
        payload: dict[str, Any], attempt_id: str, *, phase: str,
    ) -> dict[str, Any]:
        ref = task["ref"]
        step = "review" if phase == "review" else "gate"
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
        """Launch the red-verdict fallback only after its worker was conclusively stopped.

        Both red verdicts land here when the round's own session cannot take the continuation: a
        red mechanical gate and a red review differ in the request identities they dedupe on and in
        the step they report, not in what they have to guarantee before a second head opens.
        """
        ref = task["ref"]
        review = phase == "review"
        step = "review" if review else "gate"
        blocked_kind = "rework-blocked" if review else f"{phase}-red-blocked"
        action = "rework-started" if review else f"{phase}-red-rework"
        continuation = "replacement"
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
        """A claimed card the dispatcher cannot pick back up on the head it was claimed with.

        Nothing is launched and nothing is recorded: the routing journal keeps the attempt as the
        last bring-up left it rather than gaining a head that never ran. A human re-points the card
        or restores the profile.
        """
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
            reason=f"{reason}: {scrub_host_output(str(error))}",
            request_id=request_id,
        )
        resume_workspaces = payload.setdefault("resume_workspaces", {})
        if isinstance(resume_workspaces, dict):
            resume_workspaces[ref] = record.attempt_id or attempt_id
        records.pop(ref, None)
        self.save_records(payload, records)
        return {"status": "blocked", "step": step, "pilot_ref": ref, "reason": reason}

    def _gate_pending(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        result: GateResult,
    ) -> dict[str, Any]:
        """CI is non-terminal (a check still running, or none posted yet). Wait, tracking how long
        the rollup has sat non-terminal; past GATE_PENDING_STALL_SECONDS escalate once to Blocked so
        a required check nothing ever posts does not leave the card unwatched forever."""
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
        """Whether a substantive verdict on this card waits for a decision, or acts at once.

        A card parks only where a decision can come from: its sprint is open and declares a
        concrete observer head. A card with no sprint, a card whose sprint declares `none`, and a
        card whose sprint is closed keep the immediate behaviour, because parking one of those is
        parking it forever. The same is true of a sprint board that cannot be read: an unreadable
        answer moves nothing into a wait nobody can end.
        """
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

        Returns one of "drift", "failed", "pending", "red" or "green"; the gate result where
        there is one, and the operator-facing detail where there is not. Both sides of the seam
        ask it: Validate asks before parking a green verdict, so a card only ever parks with its
        mechanical state green, and the release asks again immediately before the merge itself.
        """
        drift = self._review_drift(task, record)
        if drift:
            return "drift", None, drift
        try:
            result = self.host.gate_check(task, record)
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
        """A green review verdict parks the card; it does not merge it.

        The mechanical re-checks stay on this side of the seam deliberately. A drifted checkout
        and a red or pending gate resolve in Validate exactly as they did before Assessment
        existed, so nothing mechanical ever reaches the observer: a parked card has passed
        everything a machine is going to decide, and the only question left is the decision.
        """
        ref = task["ref"]
        # The verdict is recorded before the gate runs: it is a fact about the head pair of this
        # round and stays true even when the mechanical re-check bounces the card back afterwards.
        self._record_verdict_routing(ref, record, "green")
        kind, result, detail = self._merge_readiness(task, record)
        if kind == "drift":
            return self._gate_red_to_worker(
                task, record, records, payload, attempt_id, GateResult("red", detail), phase="review-freeze"
            )
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
            record, records, payload, ref, step="review", attempt_id=attempt_id
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

        The order is the red transition's order, and for the same reason: the intent is on disk,
        with the reason the card is moving, before anything observable moves. What differs is
        what comes after the move: nothing. No merge, no worker, no reviewer. The card waits.
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

        The move is keyed on the baseline the intent was opened against, so the tick that already
        moved the card and the tick recovering from a crash before that move run the same call and
        the card moves once. Nothing here re-reads the verdict: the park carries its own reason.
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
        """A parked card. Nothing here runs a head, reads a gate or merges anything.

        The one input is the decision on the card, and until there is one the tick's whole job is
        to leave the card alone: the worker of the round stays suspended and its workspace stays
        owned, so a rework decision has something to go back to and a release has the reviewed
        checkout to merge.
        """
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
        """The decision standing on this card since it entered Assessment, with its reason.

        Read from the audit rather than from a comment baseline on the record: the audit is what
        the board writer itself checks when it refuses a decision-less move, and it is still
        there for a card whose dispatcher record was lost while it was parked.
        """
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
        """A rework decision releases the round the park was holding back.

        From here it is the ordinary red transition, same intent, same move, same ownership
        decision about the retained session, with the decision carried into the board move,
        which is what makes the audit say who sent the card back and why.
        """
        ref = task["ref"]
        # A parked card should have no reviewer left; an adopted one may still carry the
        # identity of a pane nobody stopped. Either way nothing is woken beside a head the host
        # will not confirm gone.
        if record.owns_head("review"):
            unconfirmed = self._end_review_pane_confirmed(
                record, records, payload, ref, step="assessment", attempt_id=attempt_id
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
        """A reslice decision ends the attempt and leaves the card for a fresh cut.

        The heads go down confirmed before the card is blocked, because a card nobody is watching
        is exactly the one that must not keep a writer in its checkout, while the workspace and the
        branch stay, because the recut is expected to start from the work that is already there.
        """
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
        """A merge path that cannot finish leaves the card Blocked with its heads down.

        An escaping error would leave the card where it is with a verdict or a decision already
        standing, so the next tick retries the same doomed merge forever while the worker's
        terminals stay up.
        """
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

        The verdict is still recorded against the heads that earned it. It happened, and the
        round's outcome is the same red whether or not it opened a rework. What does not happen
        is the red transition: no board move to In progress, no continuation delivered, no
        replacement head. `_block_merge_path` takes the heads down and moves the card, and it
        stops the terminals of the workspace rather than removing it, so the checkout and the
        branch are exactly where the third round left them. Coming back is one transition someone
        makes on purpose, which is the whole point of the ceiling.
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
        """Perform a release decision: re-check the mechanical state, then merge.

        The gate is asked again here rather than trusted from the park: a card can sit in
        Assessment for as long as the decision takes, and the reviewed checkout is the only thing
        that may land. A release that cannot be carried out goes to Blocked with the reason, the
        same as any other merge path that cannot finish: leaving it parked would mean taking the
        decision back down, and that machinery is a card of its own.
        """
        ref = task["ref"]
        kind, result, detail = self._merge_readiness(task, record)
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
        """Merge the reviewed branch, tear the round down and move the card to Done.

        One linear release, whether a decision opened it or a card nobody watches merged on its
        own verdict. A merge that cannot be carried out ends in Blocked with its reason.
        Recovering a release that failed part-way through is deliberately not attempted here, so
        no retry can publish a second time.
        """
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
        """Has the checkout moved off the commit the reviewer was pointed at? A verdict describes
        one code state; merging a different one lands work nobody reviewed. Returns the operator
        message for the bounce, or "" when the states match (or when neither can be read — an
        unreadable workspace is the gate's failure to report, not a silent bounce)."""
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
        self, task: dict[str, Any], *, role: str, head: str = "", workspace: str = ""
    ) -> dict[str, Any]:
        """The launch snapshot for a head the runtime has no launcher record of, or a marked
        minimal one when its profile can no longer be read.

        Only an adopted card takes this path: its bring-up happened in a previous dispatcher life,
        so the configuration is re-read now. A registry edited since must still leave a usable
        attempt record rather than take the tick down: the point of the journal is that it keeps
        working when `heads.toml` moves.
        """
        try:
            return self.catalog.head_run(task, role=role, head=head, workspace=workspace).to_json()
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

        A round is one worker bring-up plus the review it earns. Claim opens round 1, each rework
        bounce opens the next; a respawn inside a round continues that round. Nothing is snapshotted
        here: the heads are recorded by the bring-ups themselves, once they are actually up.
        """
        record.attempt_round = round_number or (record.attempt_round + 1)
        record.worker_run = {}
        record.review_run = {}

    def record_worker_routing(
        self, task: dict[str, Any], record: DispatcherRecord, run: dict[str, Any] | None = None
    ) -> None:
        """Record the worker head this bring-up just put up, as launched.

        Called after every worker launch (claim, rework, respawn) and never before: the record must
        name the process that is running, and `run` is the snapshot the launcher handed back for it.
        Only an adopted card, whose launch happened in a previous dispatcher life, has no such
        snapshot and falls back to reading the registry now. A relaunch onto the same configuration
        is the same record and dedupes on its request id; one onto a different configuration appends
        its own event and replaces the round's active head, so the verdict below reports the head
        that actually earned it.
        """
        ref = task["ref"]
        if not record.attempt_round:
            record.attempt_round = self._journal_round(ref) + 1
        record.worker_run = run or self.head_run_snapshot(
            task, role="worker", head=record.head, workspace=record.workspace
        )
        self._record_routing(ref, record, phase="worker", heads=[record.worker_run])

    def record_review_routing(
        self, task: dict[str, Any], record: DispatcherRecord, run: dict[str, Any] | None = None
    ) -> None:
        """Record the reviewer head this bring-up just put up, as launched.

        Same rule as the worker: `run` comes from the reviewer's own bring-up. A restart onto an
        unchanged configuration is the same record; one onto a repinned profile is a second reviewer
        for the round and says so.
        """
        ref = task["ref"]
        if not record.attempt_round:
            record.attempt_round = self._journal_round(ref) + 1
        record.review_run = run or self.head_run_snapshot(
            task, role="reviewer", head=record.review_head, workspace=record.workspace
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
        """Flush the dispatcher records into the production state.

        Public because the launch-intent contour (`dispatcher_launch`) persists through it: an
        intent that is not on disk before the host call is not an intent at all.
        """
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
        return DispatcherRecord(
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

    A FakeHost or a legacy record may have only the old boolean ``gate_state``.  Treat that as
    unavailable evidence instead of inventing a SHA: the exact binding is the safety property.
    ``current_sha`` is deliberately only a consistency check for callers that already hold one.
    """
    source = candidate if isinstance(candidate, dict) else getattr(record, "gate_attestation", {})
    return _accepted_gate_receipt(source, current_sha)


def _continuation_prompt(
    phase: str, generation: int = 0, reference: str = "", decision: str = ""
) -> str:
    """What the resumed conversation is told. The updated TASK.md carries the detail; this names
    which of the two red verdicts sent the card back, which report round it opens, and, when an
    observer decision opened the round, that the decision outranks the findings.

    The generation is spelled out because the retained conversation still has the previous round's
    report command in its own scrollback. "Read the updated TASK.md" is exactly the instruction the
    incident worker did not follow; a number both the agent and a human reading the pane can
    compare makes a replayed command visibly the wrong one.

    The decision is named for the same reason: a prompt that only points at a document leaves the
    retained conversation to rank the document's sections itself, and a worker that read the
    reviewer's findings as the instruction reworked findings the observer had rejected
    (secretary-1064).
    """
    if phase == "review":
        cause, work = "The review verdict is red.", "address the findings"
    else:
        cause, work = "The mechanical validation gate returned red.", "fix the failure"
    if decision.strip():
        work = (
            "follow the observer decision under \"Observer rework decision to follow\", which is "
            "the authoritative instruction for this round and outranks the reviewer findings kept "
            "below it as context"
        )
    card = f" for {reference}" if reference else ""
    return (
        f"{cause} This opens report generation {generation}{card}. TASK.md at the workspace root "
        f"has been rewritten for it: read it again, {work}, then report with the command in that "
        f"file. Its --request-id and its body file both end in {generation}. A report command from "
        "an earlier turn of this conversation ends in a different number and belongs to a round "
        "that is over: that id already names that round's report and its body file is gone, so "
        "running it reports nothing here, whether it fails or answers from the old round's record."
    )


def _wait_expectation(kind: str) -> str:
    return "review verdict" if kind == "review" else "worker report"


def _watchdog_kind(role: str) -> str:
    """`_launch`'s `role` ("worker"/"reviewer") to the `kind` the wait watchdog and
    `command_terminal_status` key their pid-heartbeat file on ("worker"/"review")."""
    return "review" if role == "reviewer" else "worker"


def _body_file_path(kind: str, reference: str, review_round: int) -> str:
    """Where a head writes its report/verdict body. Outside the workspace on purpose: a stray
    file in the worktree makes `git status` dirty, and the done-report check rejects that. The
    round is in the name because heads are told to leave the file behind: without it round 2
    starts on top of round 1's body and a head that skips the write posts a stale verdict."""
    root = os.environ.get("SECRETARY_DISPATCHER_BODY_DIR", "/tmp").rstrip("/") or "/tmp"
    return f"{root}/secretary-{kind}-{_request_token(reference)}-{_request_token(str(review_round))}.md"


def _body_file_instructions(body_file: str) -> list[str]:
    """Spell out the delivery path (secretary-637: a reviewer assembled the body inline with
    mktemp/rm, the codex runtime refused the rm, and the card sat in validate for four hours
    with no verdict). File first with a normal editing tool, then one plain command."""
    return [
        f"Write the body to {body_file} with your file-writing tool,",
        "then run the command below verbatim. Do not assemble the body inside the shell command",
        "(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses",
        "rm-style commands, and quotes or backticks in the body break the call. Leave the file in",
        "place afterwards; the dispatcher does not read it.",
    ]
