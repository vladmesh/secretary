"""Pilot dispatcher runtime for the Phase 7 cutover."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from secretary._fsutil import file_lock, write_json, write_text_atomic
from secretary.config import ConfigError, load_config, validate_instance
from secretary.dispatcher_launcher import (
    HeadLaunchError,
    ensure_claude_workspace_ready as _ensure_claude_workspace_ready,
    render_claude_command as _render_claude_command,
    render_codex_command as _render_codex_command,
    wrap_role_shell_command as _wrap_role_shell_command,
)
from secretary.dispatcher_pause import FileLegacyPauseProbe, LegacyPauseSnapshot
from secretary.dispatcher_state import (
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
)
from secretary.tasks import KanboardClient, TaskAudit, TaskError, TaskReader, TaskWriter


class DispatcherError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


class HostError(Exception):
    pass


_ASSIGN_RE = re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)\s*=\s*\S+")
_BLOB_RE = re.compile(r"\b[A-Za-z0-9+=_-]{40,}\b")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True)
class PilotSelector:
    reference: str

    @classmethod
    def exact(cls, reference: str | None) -> "PilotSelector":
        value = (reference or "").strip()
        if not value:
            raise DispatcherError("pilot_selector_required", "dispatcher requires an exact pilot ref")
        return cls(value)

    def accepts(self, task: dict[str, Any]) -> bool:
        return task.get("ref") == self.reference


def default_data_dir(instance_path: Path) -> Path:
    try:
        loaded = load_config(_instance_file(instance_path))
    except ConfigError as exc:
        raise DispatcherError("invalid_instance", str(exc), 2) from None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("data_dir"), str):
        raise DispatcherError("invalid_instance", "instance data_dir is unavailable", 2)
    return Path(loaded["data_dir"])


def _instance_file(path: Path) -> Path:
    return path / "instance.yaml" if path.is_dir() else path


class CutoverState:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "dispatcher"
        self.path = self.root / "pilot-state.json"
        self.tick_lock = self.root / "pilot-tick.lock"

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "phase": "new"}
        except (OSError, ValueError, UnicodeError):
            raise DispatcherError("state_unavailable", "dispatcher state is unreadable", 2) from None
        if not isinstance(payload, dict):
            raise DispatcherError("state_unavailable", "dispatcher state has an unsupported shape", 2)
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        write_json(self.path, payload)

    def records(self, payload: dict[str, Any]) -> dict[str, DispatcherRecord]:
        raw = payload.get("records") or {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(ref): DispatcherRecord.from_json(record)
            for ref, record in raw.items()
            if isinstance(record, dict)
        }

    def put_records(self, payload: dict[str, Any], records: dict[str, DispatcherRecord]) -> None:
        payload["records"] = {ref: record.to_json() for ref, record in sorted(records.items())}


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
        binding = self.bindings.get(project)
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
        profile = self._head_profile(head)
        adapter = profile.get("adapter") if isinstance(profile, dict) else ""
        try:
            self.prepare_head_workspace(head, workspace)
            if adapter == "claude":
                command = _render_claude_command(profile, prompt_file)
            else:
                command = _render_codex_command(profile, prompt_file, workspace=workspace)
        except HeadLaunchError as exc:
            raise HostError(str(exc)) from None
        return _wrap_role_shell_command(role, command)

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
        self._write_prompt(Path(workspace) / "TASK.md", self._worker_prompt(task, base, attempt_id))
        handle = self._launch(
            workspace,
            f"{task['ref']} worker",
            head,
            "TASK.md",
            role="worker",
            env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
        )
        return {"workspace": workspace, "handle": handle, "base_branch": base}

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

    def restore_workspace(self, task: dict[str, Any], worker: str) -> str:
        if self.mode == "noop":
            return str(self.data_dir / "dispatcher" / "workspaces" / worker)
        root = Path(os.environ.get("SECRETARY_DISPATCHER_WORKSPACES_ROOT", str(Path.home() / "orca" / "workspaces")))
        return str(root / str(task.get("project") or "") / worker)

    def complete_green(self, task: dict[str, Any], record: DispatcherRecord) -> None:
        command = os.environ.get("SECRETARY_DISPATCHER_MERGE_COMMAND", "").strip()
        if not command or self.mode == "noop":
            return
        rendered = command.format(
            ref=shlex.quote(task["ref"]),
            workspace=shlex.quote(record.workspace),
        )
        self._run_shell(rendered, Path(record.workspace), "merge command")

    def stop(self, record: DispatcherRecord) -> None:
        if self.mode == "noop" or not record.workspace:
            return
        try:
            self._run_json(["orca", "terminal", "stop", "--worktree", f"path:{record.workspace}", "--json"])
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
    ) -> str:
        if self.mode == "noop":
            return f"noop:{head}:{Path(workspace).name}:{prompt_file}"
        command = os.environ.get(env_name)
        if command:
            self.catalog.prepare_head_workspace(head, workspace)
        else:
            command = self.catalog.head_command(
                head,
                prompt_file,
                workspace=workspace,
                role=role,
            )
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

    def _set_worker_branch(self, workspace: str, branch: str) -> None:
        if self.mode == "noop":
            return
        self._run(["git", "-C", workspace, "branch", "-M", branch], "git branch")

    def _write_prompt(self, path: Path, body: str) -> None:
        write_text_atomic(path, body)

    def _worker_prompt(self, task: dict[str, Any], base: str, attempt_id: str) -> str:
        branch = _legacy_worker_branch(task["ref"])
        request = _attempt_request_id(attempt_id, "worker-report-done", task["ref"])
        return "\n".join([
            f"# Task {task['ref']}",
            "",
            task.get("description") or "(empty task description)",
            "",
            "Report through the secretary task protocol only:",
            f'PYTHONPATH="${{TA_SECRETARY_REPO:-/home/dev/secretary}}${{PYTHONPATH:+:$PYTHONPATH}}" python3 -m secretary task report --ref {task["ref"]} --role worker --kind done --request-id {request} --body-file <file>',
            "",
            f"Base branch: {base}",
            f"Worker branch: {branch}",
            "",
        ])

    def _review_prompt(self, task: dict[str, Any], attempt_id: str) -> str:
        green_request = _attempt_request_id(attempt_id, "review-green", task["ref"])
        red_request = _attempt_request_id(attempt_id, "review-red", task["ref"])
        return "\n".join([
            f"# Review {task['ref']}",
            "",
            task.get("description") or "(empty task description)",
            "",
            "Post exactly one review verdict through the secretary task protocol:",
            f'PYTHONPATH="${{TA_SECRETARY_REPO:-/home/dev/secretary}}${{PYTHONPATH:+:$PYTHONPATH}}" python3 -m secretary task verdict --ref {task["ref"]} --role reviewer --kind green --request-id {green_request} --body-file <file>',
            f'PYTHONPATH="${{TA_SECRETARY_REPO:-/home/dev/secretary}}${{PYTHONPATH:+:$PYTHONPATH}}" python3 -m secretary task verdict --ref {task["ref"]} --role reviewer --kind red --request-id {red_request} --body-file <file>',
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
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.audit = audit
        self.state = state
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
            "task": None if task is None else {
                "state": task["state"],
                "claim": task["claim"],
                "comments": len(task.get("comments") or []),
            },
            "records": list((payload.get("records") or {}).keys()),
            "divergences": list((payload.get("controlled_divergences") or [])),
        }

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
            return self._advance_review(task, records, attempt_id)
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
                f"Pilot dispatcher claimed {ref}, attempt {record.attempt_id}, "
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
            record.review_baseline = len(task.get("comments") or [])
            record.state = "validate"
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
        attempt_id: str,
    ) -> dict[str, Any]:
        ref = task["ref"]
        record = records.get(ref)
        if record is None:
            record = self._adopt(task, attempt_id)
            records[ref] = record
        marker = _last_marker(task, record.review_baseline, {"review:green", "review:red"})
        if marker == "review:green":
            self.host.complete_green(task, record)
            self.host.stop(record)
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
        if marker == "review:red":
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
            record.state = "claimed"
            return {"status": "ok", "step": "review", "pilot_ref": ref, "attempt_id": attempt_id, "to": "in_progress"}
        if record.state != "reviewing":
            try:
                record.handle = self.host.start_review(task, record)
            except HostError as exc:
                self.writer.move(
                    role="dispatcher",
                    actor=self.owner,
                    reference=ref,
                    target="blocked",
                    reason=f"review bring-up failed: {scrub_host_output(str(exc))}",
                    request_id=_attempt_request_id(record.attempt_id or attempt_id, "review-blocked", ref),
                )
                records.pop(ref, None)
                return {"status": "blocked", "step": "review", "pilot_ref": ref, "reason": "host review failed"}
            record.review_baseline = len(task.get("comments") or [])
            record.state = "reviewing"
            return {
                "status": "ok",
                "step": "review",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "action": "review-started",
            }
        return {
            "status": "ok",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "waiting-review-verdict",
        }

    def _save_records(self, payload: dict[str, Any], records: dict[str, DispatcherRecord]) -> None:
        self.state.put_records(payload, records)
        payload["last_tick_at"] = now_rfc3339()
        self.state.save(payload)

    def _adopt(self, task: dict[str, Any], attempt_id: str) -> DispatcherRecord:
        worker = task.get("claim", {}).get("worker") or _worker_id(task)
        return DispatcherRecord(
            worker=worker,
            workspace=self.host.restore_workspace(task, worker),
            handle="",
            head=self.catalog.worker_head(task),
            review_head=self.catalog.review_head(task),
            attempt_id=attempt_id,
            comment_baseline=len(task.get("comments") or []),
            review_baseline=_review_adoption_baseline(task),
            state="adopted",
            claimed_at=time.time(),
        )


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


def _worker_id(task: dict[str, Any]) -> str:
    slug = task.get("workspace", {}).get("slug") or _slug(task.get("title") or task["ref"])
    return f"{task['ref']}-{slug}"[:80].strip("-")


def _legacy_worker_branch(reference: str) -> str:
    return f"pipeline/{reference}"


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:30] or "task"


def _last_marker(task: dict[str, Any], baseline: int, markers: set[str]) -> str | None:
    result = None
    for comment in (task.get("comments") or [])[baseline:]:
        marker = comment.get("marker")
        if marker in markers:
            result = marker
    return result


def _review_adoption_baseline(task: dict[str, Any]) -> int:
    baseline = len(task.get("comments") or [])
    for index, comment in enumerate(task.get("comments") or []):
        if comment.get("marker") == "report:done":
            baseline = index + 1
    return baseline


def scrub_host_output(text: str) -> str:
    text = _ASSIGN_RE.sub(r"\1=<redacted>", text)
    return _BLOB_RE.sub(lambda match: match.group(0) if _HEX_RE.match(match.group(0)) else "<redacted>", text)


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])
