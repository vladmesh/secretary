"""Pilot dispatcher runtime for the Phase 7 cutover."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from secretary._fsutil import file_lock, write_text_atomic
from secretary.config import validate_instance
from secretary.dispatcher_launcher import (
    HeadLaunch,
    HeadLaunchError,
    ensure_claude_workspace_ready as _ensure_claude_workspace_ready,
    render_claude_command as _render_claude_command,
    render_codex_command as _render_codex_command,
    render_codex_launch as _render_codex_launch,
    wrap_role_shell_command as _wrap_role_shell_command,
)
from secretary.dispatcher_helpers import (
    _last_marker,
    _last_review_red_body,
    _legacy_worker_branch,
    _review_adoption_baseline,
    _tail,
    _worker_id,
    scrub_host_output,
)
from secretary.dispatcher_gate import (
    GATE_PENDING_STALL_SECONDS,
    GateResult,
    gate_check as _gate_check,
    validation_ci as _validation_ci,
)
from secretary.dispatcher_pause import FileLegacyPauseProbe, LegacyPauseSnapshot
from secretary.dispatcher_production import (
    ProductionState,
    production_observe as _production_observe,
    production_probe as _production_probe,
    production_run as _production_run,
    production_tick as _production_tick,
)
from secretary.dispatcher_review import (
    command_review_running as _command_review_running,
    recover_review_launch as _recover_review_launch,
    start_review as _start_review,
)
from secretary.dispatcher_watchdog import (
    REVIEW_VERDICT_STALL_SECONDS,
    WORKER_REPORT_STALL_SECONDS,
    reset_wait as _reset_wait,
    wait_outcome as _wait_outcome,
)
from secretary.dispatcher_state import (
    CutoverState,
    DispatcherRecord,
    attempt_request_id as _attempt_request_id,
    claim_actual as _claim_actual,
    claim_mismatch as _claim_mismatch,
    ensure_attempt as _ensure_attempt,
    mark_attempt_rolled_back as _mark_attempt_rolled_back,
    new_attempt_id as _new_attempt_id,
    now_rfc3339,
    record_attempt as _record_attempt,
    record_divergence as _record_divergence,
    request_token as _request_token,
)
from secretary.dispatcher_tui import TuiDeliveryError, close_terminal as _close_tui_terminal
from secretary.dispatcher_tui import deliver_tui_prompt as _deliver_tui_prompt
from secretary.dispatcher_types import DispatcherError, HostError, PilotSelector
from secretary.tasks import KanboardClient, TaskAudit, TaskError, TaskReader, TaskWriter


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
        self._heads = self._load_optional_yaml(self.instance_dir / "heads" / "heads.yaml")

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
        if requested:
            return str(requested)
        return str(self._heads.get("role_defaults", {}).get("new_card") or "codex")

    def review_head(self, task: dict[str, Any]) -> str:
        requested = task.get("routing", {}).get("review_head_override")
        if requested:
            return str(requested)
        return str(self._heads.get("role_defaults", {}).get("reviewer") or "codex-reviewer")

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
    ) -> HeadLaunch:
        profile = self._head_profile(head)
        adapter = profile.get("adapter") if isinstance(profile, dict) else ""
        try:
            self.prepare_head_workspace(head, workspace)
            if adapter == "claude":
                launch = HeadLaunch(_render_claude_command(profile, prompt_file, launch_prompt=launch_prompt))
            else:
                launch = _render_codex_launch(
                    profile, prompt_file, workspace=workspace, mode=codex_mode, launch_prompt=launch_prompt
                )
        except HeadLaunchError as exc:
            raise HostError(str(exc)) from None
        return HeadLaunch(
            _wrap_role_shell_command(role, launch.command),
            prompt_after_start=launch.prompt_after_start,
        )

    def prepare_head_workspace(self, head: str, workspace: str) -> None:
        profile = self._head_profile(head)
        adapter = profile.get("adapter") if isinstance(profile, dict) else ""
        if adapter != "claude":
            return
        try:
            _ensure_claude_workspace_ready(workspace)
        except HeadLaunchError as exc:
            raise HostError(str(exc)) from None

    def _head_profile(self, head: str) -> dict[str, Any]:
        profile = self._heads.get("profiles", {}).get(head, {})
        return profile if isinstance(profile, dict) else {}

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
    ) -> dict[str, str]:
        project = task["project"]
        base = self.catalog.default_branch(project, task.get("workspace", {}).get("base_branch"))
        workspace = self._create_workspace(project, worker_id, base)
        self._set_worker_branch(workspace, _legacy_worker_branch(task["ref"]))
        self._run_setup(project, workspace)
        self._write_prompt(Path(workspace) / "TASK.md", self._worker_task_doc(task, base, attempt_id))
        handle = self._launch(
            workspace,
            f"{task['ref']} worker",
            head,
            "TASK.md",
            role="worker",
            env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
            codex_mode=task.get("routing", {}).get("codex_launch_mode"),
            launch_prompt=self._worker_launch_prompt(),
        )
        return {"workspace": workspace, "handle": handle, "base_branch": base}

    def restart_worker(self, task: dict[str, Any], record: DispatcherRecord) -> str:
        """Launch rework in the existing workspace without recreating its branch."""
        workspace = Path(record.workspace)
        if self.mode == "noop":
            workspace.mkdir(parents=True, exist_ok=True)
        elif not workspace.is_dir():
            raise HostError("rework workspace is missing")
        base = self.catalog.default_branch(
            task["project"], task.get("workspace", {}).get("base_branch")
        )
        self._write_prompt(workspace / "TASK.md", self._worker_task_doc(task, base, record.attempt_id, record.review_baseline))
        return self._launch(
            str(workspace),
            f"{task['ref']} worker rework",
            record.head,
            "TASK.md",
            role="worker",
            env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
            codex_mode=task.get("routing", {}).get("codex_launch_mode"),
            launch_prompt=self._worker_launch_prompt(),
        )

    def start_review(self, task: dict[str, Any], record: DispatcherRecord) -> str:
        if not record.workspace:
            raise HostError("review workspace is unavailable")
        workspace = Path(record.workspace)
        if self.mode == "noop":
            workspace.mkdir(parents=True, exist_ok=True)
        elif not workspace.is_dir():
            raise HostError("review workspace is missing")
        review_file = Path(record.workspace) / "REVIEW.md"
        self._write_prompt(review_file, self._review_prompt(task, record.attempt_id))
        return self._launch(
            record.workspace,
            f"{task['ref']} review",
            record.review_head,
            "REVIEW.md",
            role="reviewer",
            env_name="SECRETARY_DISPATCHER_REVIEW_COMMAND",
        )

    def review_running(self, task: dict[str, Any], record: DispatcherRecord) -> bool:
        return _command_review_running(self, task, record)

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
        if completed.stdout.strip():
            raise HostError("worker reported done with uncommitted changes")

    def restore_workspace(self, task: dict[str, Any], worker: str) -> str:
        if self.mode == "noop":
            return str(self.data_dir / "dispatcher" / "workspaces" / worker)
        root = Path(os.environ.get("SECRETARY_DISPATCHER_WORKSPACES_ROOT", str(Path.home() / "orca" / "workspaces")))
        return str(root / str(task.get("project") or "") / worker)

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
        # Publish the reviewed branch as main (a non-fast-forward push is rejected, never
        # force-landed), then fast-forward the project's own checkout. The dispatcher runs
        # from the secretary checkout, so this is how a merged self-modification reaches the
        # next oneshot tick; other projects just stay current for the next worktree base.
        self._run(["git", "-C", record.workspace, "push", "origin", f"{branch}:main"], "merge push")
        self._run(["git", "-C", str(repo), "fetch", "origin", "main"], "post-merge fetch")
        self._run(["git", "-C", str(repo), "merge", "--ff-only", "origin/main"], "post-merge fast-forward")

    def _merge_github_pr(self, task: dict[str, Any], record: DispatcherRecord, branch: str, base: str) -> None:
        """Land a github-CI project through its PR. gh honours branch protection and refuses to
        merge while required checks are unsatisfied, so a non-green CI never lands even though the
        dispatcher has already re-run the gate on this same tick. Then fast-forward the project's
        own checkout (from the worker workspace's origin) so the next worktree bases on the merged
        tree, matching the local-merge path."""
        self._run(["gh", "pr", "merge", branch, "--merge"], "merge pr", cwd=Path(record.workspace))
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        self._run(["git", "-C", str(repo), "fetch", "origin", base], "post-merge fetch")
        self._run(["git", "-C", str(repo), "merge", "--ff-only", f"origin/{base}"], "post-merge fast-forward")

    def stop(self, record: DispatcherRecord) -> None:
        if self.mode == "noop" or not record.workspace:
            return
        try:
            self._run_json(["orca", "terminal", "stop", "--worktree", f"path:{record.workspace}", "--json"])
        except HostError:
            pass

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

    def _create_workspace(self, project: str, worker_id: str, base: str) -> str:
        if self.mode == "noop":
            workspace = self.data_dir / "dispatcher" / "workspaces" / worker_id
            workspace.mkdir(parents=True, exist_ok=True)
            return str(workspace)
        binding = self.catalog.binding(project)
        repo = Path(str(binding["repo"])).expanduser()
        if not repo.is_absolute() or not repo.is_dir():
            raise HostError(f"project repo for {project!r} is unavailable")
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
        return path

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
    ) -> str:
        if self.mode == "noop":
            return f"noop:{head}:{Path(workspace).name}:{prompt_file}"
        command = os.environ.get(env_name)
        launch = HeadLaunch(command) if command else None
        if command:
            self.catalog.prepare_head_workspace(head, workspace)
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
        if launch and launch.prompt_after_start:
            try:
                _deliver_tui_prompt(
                    handle, workspace, prompt_file, run_json=self._run_json, prompt_text=launch_prompt
                )
            except TuiDeliveryError as exc:
                _close_tui_terminal(handle, run_json=self._run_json)
                raise HostError(str(exc)) from None
            except HostError:
                _close_tui_terminal(handle, run_json=self._run_json)
                raise
        return handle

    def _set_worker_branch(self, workspace: str, branch: str) -> None:
        if self.mode == "noop":
            return
        self._run(["git", "-C", workspace, "branch", "-M", branch], "git branch")

    def _write_prompt(self, path: Path, body: str) -> None:
        write_text_atomic(path, body)

    def _worker_launch_prompt(self) -> str:
        """Short pointer delivered to the worker head at launch. The full spec lives in TASK.md
        (written next to the workspace root); duplicating it into the launch prompt would ship
        the whole task twice. The head opens TASK.md itself and reports with the command there."""
        return (
            "The full task is in TASK.md at the workspace root. Read it first and follow it. "
            "Report done or blocked with the command given in TASK.md. Do not commit TASK.md."
        )

    def _worker_task_doc(self, task: dict[str, Any], base: str, attempt_id: str, review_round: int = 0) -> str:
        branch = _legacy_worker_branch(task["ref"])
        # review_round keeps the report request-id distinct per rework round: a rework
        # reuses the same attempt_id, so without it the second done-report collides with
        # the first and is idempotently deduped, leaving the dispatcher waiting forever.
        request = _attempt_request_id(attempt_id, "worker-report-done", task["ref"], str(review_round))
        body_file = _body_file_path("report", task["ref"])
        sections = [
            f"# Task {task['ref']}",
            "",
            task.get("description") or "(empty task description)",
            "",
        ]
        review_red = _last_review_red_body(task)
        if review_red:
            sections += [
                "## Reviewer verdict to address (previous submission was RED)",
                "",
                "Your last commit was reviewed and rejected. Fix these findings before reporting",
                "done again — do NOT re-report the same commit unchanged:",
                "",
                review_red,
                "",
            ]
        sections += [
            "Before reporting done, stage AND commit everything on the worker branch: run",
            "`git add -A && git commit`, then confirm `git status --porcelain` prints nothing.",
            "The dispatcher rejects a done report while the workspace has any uncommitted changes,",
            "so a partial `git add` that misses your fix files will bounce the card.",
            "",
            "Report through the secretary task protocol only:",
            *_body_file_instructions(body_file),
            f'PYTHONPATH="${{TA_SECRETARY_REPO:-/home/dev/secretary}}${{PYTHONPATH:+:$PYTHONPATH}}" python3 -m secretary task report --ref {task["ref"]} --role worker --kind done --request-id {request} --body-file {body_file}',
            "",
            f"Base branch: {base}",
            f"Worker branch: {branch}",
            "",
        ]
        return "\n".join(sections)

    def _review_prompt(self, task: dict[str, Any], attempt_id: str) -> str:
        green_request = _attempt_request_id(attempt_id, "review-green", task["ref"])
        red_request = _attempt_request_id(attempt_id, "review-red", task["ref"])
        body_file = _body_file_path("verdict", task["ref"])
        return "\n".join([
            f"# Review {task['ref']}",
            "",
            task.get("description") or "(empty task description)",
            "",
            "Post exactly one review verdict through the secretary task protocol:",
            *_body_file_instructions(body_file),
            f'PYTHONPATH="${{TA_SECRETARY_REPO:-/home/dev/secretary}}${{PYTHONPATH:+:$PYTHONPATH}}" python3 -m secretary task verdict --ref {task["ref"]} --role reviewer --kind green --request-id {green_request} --body-file {body_file}',
            f'PYTHONPATH="${{TA_SECRETARY_REPO:-/home/dev/secretary}}${{PYTHONPATH:+:$PYTHONPATH}}" python3 -m secretary task verdict --ref {task["ref"]} --role reviewer --kind red --request-id {red_request} --body-file {body_file}',
            "",
        ])

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
        state: CutoverState,
        catalog: InstanceCatalog,
        host: CommandHostRuntime,
        *,
        owner: str = "secretary-dispatcher",
        legacy_pause: FileLegacyPauseProbe | None = None,
        production_state: ProductionState | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.audit = audit
        self.state = state
        self.production_state = production_state or ProductionState(state.root.parent)
        self.catalog = catalog
        self.host = host
        self.owner = owner
        self.legacy_pause = legacy_pause or FileLegacyPauseProbe()

    def preflight(self, selector: PilotSelector) -> dict[str, Any]:
        audit = self.audit.status()
        payload = self.state.load()
        legacy_pause = self.legacy_pause.snapshot()
        status = "ok"
        reasons: list[str] = []
        if not audit["ok"]:
            status = "blocked"
            reasons.append("task audit has unresolved pending events")
        if payload.get("pilot_ref") not in (None, selector.reference):
            status = "blocked"
            reasons.append("dispatcher state is bound to another pilot ref")
        if not payload.get("old_owner_paused"):
            status = "blocked"
            reasons.append("old dispatcher pause evidence is missing")
        if not legacy_pause.sufficient:
            status = "blocked"
            reasons.append(legacy_pause.reason)
        return {
            "status": status,
            "step": "preflight",
            "pilot_ref": selector.reference,
            "attempt_id": payload.get("attempt_id"),
            "audit": audit,
            "reasons": reasons,
            "phase": payload.get("phase", "new"),
            "legacy_pause": legacy_pause.to_json(),
        }

    def pause_old(self, selector: PilotSelector, *, actor: str, evidence: str) -> dict[str, Any]:
        with file_lock(self.state.tick_lock):
            legacy_pause = self.legacy_pause.snapshot()
            if not legacy_pause.sufficient:
                return {
                    "status": "blocked",
                    "step": "pause-old",
                    "reason": legacy_pause.reason,
                    "pilot_ref": selector.reference,
                    "legacy_pause": legacy_pause.to_json(),
                }
            payload = self.state.load()
            if payload.get("phase") == "new_pilot" and payload.get("pilot_ref") != selector.reference:
                raise DispatcherError("pilot_conflict", "another pilot is already active", 3)
            payload.update({
                "version": 1,
                "phase": "old_paused",
                "pilot_ref": selector.reference,
                "old_owner_paused": True,
                "old_owner_evidence": evidence,
                "old_owner_pause_snapshot": legacy_pause.to_json(),
                "old_owner_paused_at": now_rfc3339(),
                "old_owner_paused_by": actor,
            })
            payload.setdefault("records", {})
            self.state.save(payload)
        return {"status": "ok", "step": "pause-old", "pilot_ref": selector.reference, "phase": "old_paused"}

    def start_new_pilot(self, selector: PilotSelector, *, actor: str) -> dict[str, Any]:
        with file_lock(self.state.tick_lock):
            payload = self.state.load()
            if payload.get("pilot_ref") not in (None, selector.reference):
                raise DispatcherError("pilot_conflict", "dispatcher state is bound to another pilot ref", 3)
            if not payload.get("old_owner_paused"):
                return {"status": "blocked", "step": "start-new-pilot", "reason": "old dispatcher pause evidence is missing", "pilot_ref": selector.reference}
            legacy_pause = self.legacy_pause.snapshot()
            if not legacy_pause.sufficient:
                return {
                    "status": "blocked",
                    "step": "start-new-pilot",
                    "reason": legacy_pause.reason,
                    "pilot_ref": selector.reference,
                    "legacy_pause": legacy_pause.to_json(),
                }
            active_attempt = (
                str(payload.get("attempt_id") or "")
                if payload.get("phase") == "new_pilot" and payload.get("pilot_ref") == selector.reference
                else ""
            )
            attempt_id = active_attempt or _new_attempt_id()
            if not active_attempt:
                _record_attempt(payload, attempt_id, selector.reference, actor, self.owner)
                if payload.get("phase") != "new_pilot":
                    payload["records"] = {}
            payload.update({
                "version": 1,
                "phase": "new_pilot",
                "pilot_ref": selector.reference,
                "attempt_id": attempt_id,
                "new_owner": self.owner,
                "new_owner_started_at": now_rfc3339(),
                "new_owner_started_by": actor,
                "new_owner_legacy_pause_snapshot": legacy_pause.to_json(),
            })
            payload.setdefault("records", {})
            self.state.save(payload)
        return {
            "status": "ok",
            "step": "start-new-pilot",
            "pilot_ref": selector.reference,
            "attempt_id": attempt_id,
            "phase": "new_pilot",
        }

    def tick(self, selector: PilotSelector) -> dict[str, Any]:
        with file_lock(self.state.tick_lock):
            payload = self.state.load()
            guard = self._mutation_guard(payload, selector)
            if guard is not None:
                return guard
            attempt_id = _ensure_attempt(payload, selector.reference, self.owner, self.owner)
            records = self.state.records(payload)
            task = self.reader.show(selector.reference)
            if not selector.accepts(task):
                return {"status": "skipped", "step": "tick", "reason": "pilot selector rejected task"}
            outcome = self._tick_task(task, records, payload, attempt_id)
            self.state.put_records(payload, records)
            payload["last_tick_at"] = now_rfc3339()
            self.state.save(payload)
            return outcome

    def observe(self, selector: PilotSelector) -> dict[str, Any]:
        payload = self.state.load()
        task: dict[str, Any] | None
        try:
            task = self.reader.show(selector.reference)
        except TaskError:
            task = None
        return {
            "status": "ok",
            "step": "observe",
            "pilot_ref": selector.reference,
            "attempt_id": payload.get("attempt_id"),
            "phase": payload.get("phase", "new"),
            "new_owner": payload.get("new_owner"),
            "old_owner_paused": bool(payload.get("old_owner_paused")),
            "legacy_decommissioned": bool(payload.get("legacy_decommissioned")),
            "task": None if task is None else {
                "state": task["state"],
                "claim": task["claim"],
                "comments": len(task.get("comments") or []),
            },
            "records": list((payload.get("records") or {}).keys()),
            "divergences": list((payload.get("controlled_divergences") or [])),
        }

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

    def commit_cutover(self, selector: PilotSelector, *, actor: str) -> dict[str, Any]:
        with file_lock(self.state.tick_lock):
            payload = self.state.load()
            guard = self._pilot_guard(payload, selector)
            if guard is not None:
                return guard
            legacy_guard = self._legacy_pause_guard("commit-cutover")
            if legacy_guard is not None:
                return legacy_guard
            payload["phase"] = "cutover_committed"
            payload["cutover_committed_at"] = now_rfc3339()
            payload["cutover_committed_by"] = actor
            self.state.save(payload)
        return {"status": "ok", "step": "commit-cutover", "pilot_ref": selector.reference, "phase": "cutover_committed"}

    def decommission_old(self, selector: PilotSelector, *, actor: str) -> dict[str, Any]:
        with file_lock(self.state.tick_lock):
            payload = self.state.load()
            guard = self._pilot_guard(payload, selector)
            if guard is not None:
                return guard
            if payload.get("phase") != "cutover_committed":
                return {"status": "blocked", "step": "decommission-old", "reason": "cutover is not committed"}
            production = self.production_state.load()
            if production.get("phase") != "production" or not production.get("owner"):
                return {"status": "blocked", "step": "decommission-old", "reason": "production owner is not active"}
            payload["legacy_decommissioned"] = True
            payload["legacy_decommissioned_at"] = now_rfc3339()
            payload["legacy_decommissioned_by"] = actor
            self.state.save(payload)
        return {
            "status": "ok",
            "step": "decommission-old",
            "pilot_ref": selector.reference,
            "phase": "cutover_committed",
            "legacy_decommissioned": True,
        }

    def rollback(self, selector: PilotSelector, *, actor: str, reason: str) -> dict[str, Any]:
        with file_lock(self.state.tick_lock):
            payload = self.state.load()
            guard = self._pilot_guard(payload, selector)
            if guard is not None:
                return guard
            records = self.state.records(payload)
            stopped = []
            for record in records.values():
                self.host.stop(record)
                stopped.append(record.worker)
            payload["phase"] = "rolled_back"
            payload["new_owner"] = ""
            payload["old_owner_paused"] = False
            payload["rollback"] = {
                "actor": actor,
                "at": now_rfc3339(),
                "reason": reason,
                "stopped_workers": stopped,
            }
            _mark_attempt_rolled_back(payload, actor, reason)
            payload["records"] = {}
            self.state.save(payload)
        return {"status": "ok", "step": "rollback", "pilot_ref": selector.reference, "phase": "rolled_back", "stopped_workers": stopped}

    def _mutation_guard(self, payload: dict[str, Any], selector: PilotSelector) -> dict[str, Any] | None:
        if payload.get("phase") != "new_pilot":
            return {"status": "blocked", "step": "tick", "reason": "new pilot is not started"}
        if not payload.get("old_owner_paused"):
            return {"status": "blocked", "step": "tick", "reason": "old dispatcher is not paused"}
        legacy_guard = self._legacy_pause_guard("tick")
        if legacy_guard is not None:
            return legacy_guard
        return self._pilot_guard(payload, selector)

    def _pilot_guard(self, payload: dict[str, Any], selector: PilotSelector) -> dict[str, Any] | None:
        if payload.get("pilot_ref") != selector.reference:
            return {"status": "blocked", "step": "guard", "reason": "pilot selector does not match active state"}
        return None

    def _legacy_pause_guard(self, step: str) -> dict[str, Any] | None:
        legacy_pause = self.legacy_pause.snapshot()
        if legacy_pause.sufficient:
            return None
        return {
            "status": "blocked",
            "step": step,
            "reason": legacy_pause.reason,
            "legacy_pause": legacy_pause.to_json(),
        }

    def _tick_task(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
    ) -> dict[str, Any]:
        ref = task["ref"]
        if task["state"] == "ready":
            return self._claim(task, records, payload, attempt_id)
        if task["state"] == "in_progress":
            return self._advance_worker(task, records, payload, attempt_id)
        if task["state"] == "validate":
            return self._advance_review(task, records, payload, attempt_id)
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
    ) -> dict[str, Any]:
        ref = task["ref"]
        head = self.catalog.worker_head(task)
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
            request_id=_attempt_request_id(attempt_id, "claim", ref),
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
            state="claim_verified",
            claimed_at=time.time(),
        )
        records[ref] = record
        self._save_records(payload, records)
        return self._launch_worker_after_claim(claimed, record, records, payload)

    def _launch_worker_after_claim(
        self,
        claimed: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
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
        try:
            prepared = self.host.prepare_worker(
                claimed,
                record.worker,
                record.head,
                attempt_id=record.attempt_id,
            )
        except HostError as exc:
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="blocked",
                reason=f"dispatcher bring-up failed: {scrub_host_output(str(exc))}",
                request_id=_attempt_request_id(record.attempt_id, "bringup-blocked", ref),
            )
            records.pop(ref, None)
            self._save_records(payload, records)
            return {"status": "blocked", "step": "claim", "pilot_ref": ref, "reason": "host bring-up failed"}
        record.workspace = prepared["workspace"]
        record.handle = prepared["handle"]
        record.state = "claimed"
        records[ref] = record
        self._save_records(payload, records)
        self.writer.comment(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            body=(
                f"{_dispatcher_label(payload)} claimed {ref}, attempt {record.attempt_id}, "
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
            record = self._adopt(task, attempt_id)
            records[ref] = record
            current_claim = _attempt_request_id(attempt_id, "claim", ref)
            if self.audit.committed_event(current_claim) is not None:
                mismatch = _claim_mismatch(task, record.worker, record.head, record.review_head)
                if not mismatch:
                    record.state = "claim_verified"
                    self._save_records(payload, records)
                    return self._launch_worker_after_claim(task, record, records, payload)
        if record.state == "claim_verified":
            return self._launch_worker_after_claim(task, record, records, payload)
        marker = _last_marker(task, record.comment_baseline, {"report:done", "report:blocked"})
        if marker == "report:done":
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
            record.review_baseline = len(task.get("comments") or [])
            record.state = "validate"
            # Fresh code state: the mechanical gate must re-run before this report reaches review.
            record.gate_state = ""
            record.gate_pending_since = 0.0
            _reset_wait(record, "worker")
            _reset_wait(record, "review")
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="validate",
                reason="worker report:done",
                request_id=_attempt_request_id(record.attempt_id or attempt_id, "worker-done", ref, str(record.review_baseline)),
            )
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
            record = self._adopt(task, attempt_id)
            records[ref] = record
        marker = _last_marker(task, record.review_baseline, {"review:green", "review:red"})
        if marker == "review:green":
            return self._finish_green(task, record, records, payload, attempt_id)
        if marker == "review:red":
            self.host.stop(record)
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="in_progress",
                reason="review:red",
                request_id=_attempt_request_id(
                    record.attempt_id or attempt_id,
                    "review-red",
                    ref,
                    str(len(task.get("comments") or [])),
                ),
            )
            record.comment_baseline = len(task.get("comments") or [])
            record.gate_state = ""
            record.gate_pending_since = 0.0
            _reset_wait(record, "review")
            _reset_wait(record, "worker")
            moved = self.reader.show(ref)
            try:
                record.handle = self.host.restart_worker(moved, record)
            except HostError as exc:
                self.writer.move(
                    role="dispatcher",
                    actor=self.owner,
                    reference=ref,
                    target="blocked",
                    reason=f"dispatcher rework bring-up failed: {scrub_host_output(str(exc))}",
                    request_id=_attempt_request_id(
                        record.attempt_id or attempt_id, "rework-blocked", ref
                    ),
                )
                records.pop(ref, None)
                self._save_records(payload, records)
                return {
                    "status": "blocked",
                    "step": "review",
                    "pilot_ref": ref,
                    "reason": "rework bring-up failed",
                }
            record.state = "claimed"
            records[ref] = record
            self._save_records(payload, records)
            return {
                "status": "ok",
                "step": "review",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "action": "rework-started",
            }
        # Mechanical gate (secretary-633): a fresh report clears the cheap CI/local gate before the
        # expensive reviewer is spawned. A review already in flight (state review_starting/reviewing)
        # cleared the gate when it launched, so re-running it here would be wasted host I/O.
        if record.state not in ("review_starting", "reviewing") and record.gate_state != "green":
            gated = self._run_gate(task, record, records, payload, attempt_id)
            if gated is not None:
                return gated
        if record.state == "review_starting":
            return _recover_review_launch(self, task, records, record, attempt_id)
        if record.state != "reviewing":
            launch_request = _review_launch_request_id(ref, record.review_baseline)
            if self.audit.committed_event(launch_request) is not None:
                record.state = "review_starting"
                return _recover_review_launch(self, task, records, record, attempt_id)
            self.writer.comment(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                body=f"Dispatcher review launch requested for {ref}, review baseline {record.review_baseline}.",
                request_id=launch_request,
            )
            record.state = "review_starting"
            return _start_review(self, task, records, record, attempt_id, action="review-started")
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
        """Watch an open-ended wait (kind "worker" or "review"). Returns None to keep waiting,
        or a tick outcome once the wait blew its ceiling: one respawn, then Blocked. Without
        this a head that died before posting parks the card forever. The ceiling is the only
        input on purpose; see dispatcher_watchdog for why no liveness probe is trustworthy."""
        stall = REVIEW_VERDICT_STALL_SECONDS if kind == "review" else WORKER_REPORT_STALL_SECONDS
        waiting_since = float(getattr(record, f"{kind}_waiting_since") or 0.0)
        now = time.time()
        if not waiting_since:
            setattr(record, f"{kind}_waiting_since", now)
            self._save_records(payload, records)
            return None
        outcome = _wait_outcome(
            waiting_since=waiting_since,
            now=now,
            stall_seconds=stall,
            respawns=int(getattr(record, f"{kind}_respawns") or 0),
        )
        if outcome == "wait":
            return None
        if outcome == "respawn":
            return self._respawn_wait(task, record, records, payload, attempt_id, kind=kind, now=now)
        return self._escalate_wait(task, record, records, payload, attempt_id, kind=kind, stall=stall)

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
    ) -> dict[str, Any]:
        ref = task["ref"]
        step = "review" if kind == "review" else "advance"
        self.host.stop(record)
        try:
            if kind == "review":
                record.handle = self.host.start_review(task, record)
                record.state = "reviewing"
            else:
                record.handle = self.host.restart_worker(task, record)
                record.state = "claimed"
        except Exception as exc:
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="blocked",
                reason=f"{kind} respawn failed: {scrub_host_output(str(exc))}",
                request_id=_attempt_request_id(
                    record.attempt_id or attempt_id, f"{kind}-respawn-blocked", ref
                ),
            )
            records.pop(ref, None)
            self._save_records(payload, records)
            return {"status": "blocked", "step": step, "pilot_ref": ref, "reason": f"{kind} respawn failed"}
        setattr(record, f"{kind}_waiting_since", now)
        setattr(record, f"{kind}_respawns", int(getattr(record, f"{kind}_respawns") or 0) + 1)
        records[ref] = record
        self._save_records(payload, records)
        return {
            "status": "ok",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{kind}-respawned",
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
    ) -> dict[str, Any]:
        ref = task["ref"]
        step = "review" if kind == "review" else "advance"
        expected = "вердикт ревьюера" if kind == "review" else "отчёт воркера"
        self.host.stop(record)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="blocked",
            reason=(
                f"Вотчдог ожидания: {expected} не пришёл после respawn "
                f"(порог {stall}s). Карточка в Blocked до vladmesh."
            ),
            request_id=_attempt_request_id(record.attempt_id or attempt_id, f"{kind}-wait-stall", ref),
        )
        records.pop(ref, None)
        self._save_records(payload, records)
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
            self._save_records(payload, records)
            return {"status": "blocked", "step": "gate", "pilot_ref": ref, "reason": "validation gate failed"}
        if result.status == "green":
            record.gate_state = "green"
            record.gate_pending_since = 0.0
            self._save_records(payload, records)
            return None
        if result.status == "pending":
            return self._gate_pending(task, record, records, payload, attempt_id, result)
        return self._gate_red_to_worker(task, record, records, payload, attempt_id, result, phase="gate")

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
        detail = scrub_host_output(result.summary)
        log = scrub_host_output(result.log).strip()
        body = f"Механический гейт валидации красный: {detail}. Карточка возвращена в In progress на доработку."
        if log:
            body += f"\nХвост:\n```\n{log}\n```"
        self.host.stop(record)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="in_progress",
            reason=body,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id, f"{phase}-red", ref, str(len(task.get("comments") or []))
            ),
        )
        record.comment_baseline = len(self.reader.show(ref)["comments"])
        record.gate_state = ""
        record.gate_pending_since = 0.0
        _reset_wait(record, "review")
        _reset_wait(record, "worker")
        moved = self.reader.show(ref)
        try:
            record.handle = self.host.restart_worker(moved, record)
        except HostError as exc:
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="blocked",
                reason=f"dispatcher rework bring-up failed: {scrub_host_output(str(exc))}",
                request_id=_attempt_request_id(record.attempt_id or attempt_id, f"{phase}-red-blocked", ref),
            )
            records.pop(ref, None)
            self._save_records(payload, records)
            return {"status": "blocked", "step": "gate", "pilot_ref": ref, "reason": "rework bring-up failed"}
        record.state = "claimed"
        records[ref] = record
        self._save_records(payload, records)
        return {"status": "ok", "step": "gate", "pilot_ref": ref, "attempt_id": attempt_id, "action": f"{phase}-red-rework"}

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
            self._save_records(payload, records)
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
                f"Механический гейт: {scrub_host_output(result.summary)} — CI висит без "
                f"терминального результата дольше порога ({GATE_PENDING_STALL_SECONDS}s). "
                f"Карточка в Blocked до vladmesh."
            ),
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "gate-pending-stall", ref),
        )
        records.pop(ref, None)
        self._save_records(payload, records)
        return {"status": "ok", "step": "gate", "pilot_ref": ref, "attempt_id": attempt_id, "to": "blocked"}

    def _finish_green(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
    ) -> dict[str, Any]:
        """Green review verdict. Re-run the mechanical gate right before merging so a non-green
        CI/local gate never lands: green merges, red bounces back to the worker, pending waits."""
        ref = task["ref"]
        try:
            result = self.host.gate_check(task, record)
        except HostError as exc:
            self.host.stop(record)
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="blocked",
                reason=f"merge gate failed: {scrub_host_output(str(exc))}",
                request_id=_attempt_request_id(record.attempt_id or attempt_id, "merge-gate-blocked", ref),
            )
            records.pop(ref, None)
            self._save_records(payload, records)
            return {"status": "blocked", "step": "review", "pilot_ref": ref, "reason": "merge gate failed"}
        if result.status == "pending":
            return {"status": "ok", "step": "review", "pilot_ref": ref, "attempt_id": attempt_id, "action": "merge-gate-pending"}
        if result.status != "green":
            return self._gate_red_to_worker(task, record, records, payload, attempt_id, result, phase="merge-gate")
        try:
            self.host.complete_green(task, record)
        except HostError as exc:
            # A rejected merge (non-fast-forward push, gh refusing on branch protection) must land
            # the card in Blocked rather than escape the tick: an escaping error leaves the card in
            # validate with a green verdict, so the next tick retries the same doomed merge forever
            # while the worker's terminals stay up.
            self.host.stop(record)
            self.writer.move(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                target="blocked",
                reason=f"merge failed: {scrub_host_output(str(exc))}",
                request_id=_attempt_request_id(record.attempt_id or attempt_id, "merge-blocked", ref),
            )
            records.pop(ref, None)
            self._save_records(payload, records)
            return {"status": "blocked", "step": "review", "pilot_ref": ref, "reason": "merge failed"}
        self.host.teardown(record)
        self.writer.move(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            target="done",
            reason="review:green",
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "review-green", ref),
        )
        records.pop(ref, None)
        return {"status": "ok", "step": "review", "pilot_ref": ref, "attempt_id": attempt_id, "to": "done"}

    def _save_records(self, payload: dict[str, Any], records: dict[str, DispatcherRecord]) -> None:
        state = self.production_state if payload.get("mode") == "production" else self.state
        state.put_records(payload, records)
        payload["last_tick_at"] = now_rfc3339()
        state.save(payload)

    def _adopt(self, task: dict[str, Any], attempt_id: str) -> DispatcherRecord:
        worker = task.get("claim", {}).get("worker") or _worker_id(task)
        review_baseline = _review_adoption_baseline(task)
        launched = self._review_launch_recorded(task, review_baseline)
        state = "review_starting" if launched else "adopted"
        return DispatcherRecord(
            worker=worker,
            workspace=self.host.restore_workspace(task, worker),
            handle="",
            head=self.catalog.worker_head(task),
            review_head=self.catalog.review_head(task),
            attempt_id=attempt_id,
            comment_baseline=len(task.get("comments") or []),
            review_baseline=review_baseline,
            state=state,
            claimed_at=time.time(),
            # A reviewer only launches once the gate is green, so an adopted card already in review
            # inherits a passed gate rather than re-running it before the recovery path.
            gate_state="green" if launched else "",
        )

    def _review_launch_recorded(self, task: dict[str, Any], review_baseline: int) -> bool:
        if task.get("state") != "validate":
            return False
        return self.audit.committed_event(_review_launch_request_id(task["ref"], review_baseline)) is not None


def runtime_from_args(instance: str, data_dir: str | None, *, host_mode: str, owner: str) -> DispatcherRuntime:
    instance_path = Path(instance)
    data = Path(data_dir) if data_dir else default_data_dir(instance_path)
    client = KanboardClient()
    catalog = InstanceCatalog(instance_path)
    return DispatcherRuntime(
        TaskReader(client),
        TaskWriter(client, data_dir=data),
        TaskAudit(data),
        CutoverState(data),
        catalog,
        CommandHostRuntime(catalog, data, mode=host_mode),
        owner=owner,
    )


def _dispatcher_label(payload: dict[str, Any]) -> str:
    return "Production dispatcher" if payload.get("mode") == "production" else "Pilot dispatcher"


def _review_launch_request_id(reference: str, review_baseline: int) -> str:
    return _attempt_request_id("review", "start-intent", reference, str(review_baseline))


def _body_file_path(kind: str, reference: str) -> str:
    """Where a head writes its report/verdict body. Outside the workspace on purpose: a stray
    file in the worktree makes `git status` dirty, and the done-report check rejects that."""
    root = os.environ.get("SECRETARY_DISPATCHER_BODY_DIR", "/tmp").rstrip("/") or "/tmp"
    return f"{root}/secretary-{kind}-{_request_token(reference)}.md"


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
