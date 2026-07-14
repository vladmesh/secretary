"""Clean-worktree onboarding gate and the sole binding enable transition."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from secretary._fsutil import file_lock, publish_state_atomic
from secretary.config import ConfigError, load_config, validate
from secretary.onboarding import scan_repo
from secretary.provision import _instance_dir, _load_inputs, _project_lock_path, _run_id

_SECRET = re.compile(r"(?i)(gh[pousr]_[A-Za-z0-9_]+|(?:token|password|secret|api[_-]?key)\s*[=:]\s*\S+)")
_TAIL = 4000


def run_gate(instance_value: str, project_id: str) -> tuple[int, dict[str, Any]]:
    instance = _instance_dir(instance_value)
    with file_lock(_project_lock_path(instance, project_id)):
        return _run_gate_locked(instance, project_id)


def _run_gate_locked(instance: Path, project_id: str) -> tuple[int, dict[str, Any]]:
    binding_path = instance / "projects" / f"{project_id}.yaml"
    draft_path = instance / "adapter-drafts" / f"{project_id}.yaml"
    try:
        existing_binding = load_config(binding_path)
        existing_draft = load_config(draft_path)
    except ConfigError:
        existing_binding = existing_draft = None
    if isinstance(existing_binding, dict) and existing_binding.get("enabled") is True:
        results = sorted((instance / "gate-runs" / project_id).glob("*/result.json"))
        if isinstance(existing_draft, dict) and existing_draft.get("gate", {}).get("status") == "passed" and results:
            previous = load_config(results[-1])
            if isinstance(previous, dict) and previous.get("status") == "passed":
                adapter_path = instance / "adapters" / f"{existing_binding['adapter']}.yaml"
                try:
                    current_head = scan_repo(Path(existing_binding["repo"]), existing_binding["default_branch"])["repo"]["head"]
                    current_digest = "sha256:" + hashlib.sha256(adapter_path.read_bytes()).hexdigest()
                except (OSError, KeyError):
                    current_head = current_digest = "unavailable"
                if (current_head == previous["input_revision"]["scanner_head"] and
                        current_digest == previous["adapter_digest"]):
                    return 0, previous
                return _disable_stale_enabled(instance, project_id, existing_binding, existing_draft, previous)
        return 1, {"status": "conflict", "finding": "enabled binding has no matching passed gate result"}
    loaded = _load_inputs(instance, project_id)
    if loaded["status"] != "ok":
        return 1, loaded
    draft, binding = loaded["draft"], loaded["binding"]
    if draft["provision"]["status"] != "drafted":
        return 1, {"status": "conflict", "finding": "provision is not drafted"}
    adapter_path = instance / "adapters" / f"{draft['identity']['adapter']}.yaml"
    try:
        adapter_bytes = adapter_path.read_bytes()
        adapter = load_config(adapter_path)
    except (OSError, ConfigError) as exc:
        return 1, {"status": "conflict", "finding": _redact(str(exc))}
    errors = validate(adapter, "adapter", adapter_path.name)
    if errors:
        return 1, {"status": "conflict", "finding": "canonical adapter is invalid"}
    digest = "sha256:" + hashlib.sha256(adapter_bytes).hexdigest()
    provision_run = _run_id(draft)
    run_id = "gate-" + hashlib.sha256(
        f"{project_id}\0{draft['scanner']['repo']['head']}\0{provision_run}\0{digest}".encode()
    ).hexdigest()[:20]
    result_path = instance / "gate-runs" / project_id / run_id / "result.json"
    if result_path.exists():
        previous = load_config(result_path)
        if previous.get("status") == "passed" and binding.get("enabled") is True:
            return 0, previous
        if previous.get("status") == "passed":
            return 1, _conflict_result(draft, run_id, provision_run, digest, "passed result is superseded")

    result = _base_result(draft, run_id, provision_run, digest)
    repo = Path(draft["identity"]["repo"])
    head = draft["scanner"]["repo"]["head"]
    try:
        current_head = scan_repo(repo, draft["identity"]["default_branch"])["repo"]["head"]
    except (OSError, KeyError):
        current_head = "unavailable"
    if current_head != head:
        return _publish_stale(result_path, result, "scanner HEAD changed")
    with tempfile.TemporaryDirectory(prefix="secretary-gate-") as temp:
        worktree = Path(temp) / "worktree"
        add = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), head], capture_output=True, text=True)
        if add.returncode:
            return _publish_failure(result_path, result, "clean_worktree", add)
        try:
            result["checks"]["clean_worktree"] = {"status": "passed"}
            for command in adapter["setup"]["commands"]:
                outcome = _command(command, worktree)
                if outcome.returncode:
                    return _publish_failure(result_path, result, "setup", outcome)
            result["checks"]["setup"] = {"status": "passed"}
            outcome = _command(adapter["smoke"]["command"], worktree)
            if outcome.returncode:
                return _publish_failure(result_path, result, "smoke", outcome)
            result["checks"]["smoke"] = {"status": "passed"}
            validation = adapter["validation"]
            if validation["ci"] == "none":
                result["checks"]["validation"] = {"status": "declared-missing"}
                result["missing_coverage"] = list(validation["missing"])
            else:
                command = validation.get("command", "git diff --check HEAD")
                outcome = _command(command, worktree)
                if outcome.returncode:
                    return _publish_failure(result_path, result, "validation", outcome)
                result["checks"]["validation"] = {"status": "passed"}
            result["checks"]["artifact_policy"] = {"status": "passed"}
        finally:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], capture_output=True)

    latest = _load_inputs(instance, project_id)
    if latest["status"] != "ok" or latest["draft"]["scanner"]["repo"]["head"] != head:
        return _publish_stale(result_path, result, "scanner or provision state changed")
    try:
        current_digest = "sha256:" + hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    except OSError:
        current_digest = "unavailable"
    if current_digest != digest:
        return _publish_stale(result_path, result, "canonical adapter changed")

    updated = copy.deepcopy(draft)
    updated["gate"] = {
        "owner": "onboarding-gate", "status": "passed",
        "checks": {key: value["status"] for key, value in result["checks"].items()},
        "binding": {"enabled": True}, "findings": [],
    }
    enabled = copy.deepcopy(binding)
    enabled["enabled"] = True
    result["status"] = "passed"
    compatibility = _compatibility_manifest(enabled, adapter)
    writes = [
        (result_path, json.dumps(result, indent=2, sort_keys=True) + "\n"),
        (instance / "adapter-drafts" / f"{project_id}.yaml", yaml.safe_dump(updated, sort_keys=False)),
        (instance / "projects" / f"{project_id}.yaml", yaml.safe_dump(enabled, sort_keys=False)),
        (instance / "compatibility-manifests" / f"{project_id}.toml", compatibility),
    ]
    try:
        publish_state_atomic(writes)
    except OSError as exc:
        result["status"] = "failed"
        result["findings"] = [{"code": "publication.failed", "message": _redact(exc.strerror or "I/O error")}]
        return 1, result
    return 0, result


def _base_result(draft: dict[str, Any], run_id: str, provision_run: str, digest: str) -> dict[str, Any]:
    return {"version": 1, "run_id": run_id, "identity": copy.deepcopy(draft["identity"]),
            "input_revision": {"scanner_head": draft["scanner"]["repo"]["head"], "provision_run_id": provision_run},
            "adapter_digest": digest, "status": "failed",
            "checks": {name: {"status": "not-run"} for name in ("clean_worktree", "setup", "smoke", "validation", "artifact_policy")},
            "findings": []}


def _command(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, shell=True, capture_output=True, text=True)


def _publish_failure(path: Path, result: dict[str, Any], stage: str, outcome: subprocess.CompletedProcess[str]) -> tuple[int, dict[str, Any]]:
    result["checks"][stage] = {"status": "failed"}
    result["findings"] = [{"code": f"{stage}.failed", "message": f"{stage} command failed", "log_tail": _redact((outcome.stdout + outcome.stderr)[-_TAIL:])}]
    publish_state_atomic([(path, json.dumps(result, indent=2, sort_keys=True) + "\n")])
    return 1, result


def _publish_stale(path: Path, result: dict[str, Any], message: str) -> tuple[int, dict[str, Any]]:
    result["status"] = "stale"
    result["findings"] = [{"code": "stale.input", "message": message}]
    publish_state_atomic([(path, json.dumps(result, indent=2, sort_keys=True) + "\n")])
    return 1, result


def _conflict_result(draft: dict[str, Any], run_id: str, provision_run: str, digest: str, message: str) -> dict[str, Any]:
    result = _base_result(draft, run_id, provision_run, digest)
    result["status"] = "conflict"
    result["findings"] = [{"code": "result.conflict", "message": message}]
    return result


def _redact(value: str) -> str:
    return _SECRET.sub("[REDACTED]", value)


def _compatibility_manifest(binding: dict[str, Any], adapter: dict[str, Any]) -> str:
    lines = ["# Generated from enabled binding and canonical adapter.", "[workspace]", f"project = {json.dumps(binding['id'])}", f"base_branch = {json.dumps(binding['default_branch'])}", "", "[setup]", "commands = ["]
    lines.extend(f"  {json.dumps(command)}," for command in adapter["setup"]["commands"])
    lines += ["]", "", "[smoke]", f"command = {json.dumps(adapter['smoke']['command'])}", "", "[validate]", f"ci = {json.dumps(adapter['validation']['ci'])}"]
    if "command" in adapter["validation"]:
        lines.append(f"command = {json.dumps(adapter['validation']['command'])}")
    return "\n".join(lines) + "\n"


def _disable_stale_enabled(
    instance: Path,
    project_id: str,
    binding: dict[str, Any],
    draft: dict[str, Any],
    previous: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    disabled = copy.deepcopy(binding)
    disabled["enabled"] = False
    updated = copy.deepcopy(draft)
    updated["gate"] = {
        "owner": "onboarding-gate", "status": "failed",
        "checks": {name: "not-run" for name in ("clean_worktree", "setup", "smoke", "validation", "artifact_policy")},
        "binding": {"enabled": False},
        "findings": [{"code": "stale.input", "severity": "error", "message": "enabled gate inputs changed"}],
    }
    stale = copy.deepcopy(previous)
    stale["status"] = "stale"
    stale["findings"] = [{"code": "stale.input", "message": "enabled gate inputs changed"}]
    try:
        publish_state_atomic(
            [(instance / "projects" / f"{project_id}.yaml", yaml.safe_dump(disabled, sort_keys=False)),
             (instance / "adapter-drafts" / f"{project_id}.yaml", yaml.safe_dump(updated, sort_keys=False))],
            removes=[instance / "compatibility-manifests" / f"{project_id}.toml"],
        )
    except OSError as exc:
        return 1, {"status": "publication_failed", "finding": _redact(exc.strerror or "I/O error")}
    return 1, stale
