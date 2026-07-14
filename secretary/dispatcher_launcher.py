"""Head command rendering for dispatcher-launched workers and reviewers."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

CODEX_HOME_DEFAULT = "/home/dev/.config/orca/codex-runtime-home/home"
CODEX_EFFORTS = {
    "default": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "xhigh": "xhigh",
}


class HeadLaunchError(RuntimeError):
    pass


def render_claude_command(profile: dict[str, Any], prompt_file: str) -> str:
    args = ["claude", "--dangerously-skip-permissions"]
    model = profile.get("model")
    if model:
        args += ["--model", str(model)]
    return f"{shlex.join(args)} {_prompt_substitution(prompt_file)}"


def render_codex_command(profile: dict[str, Any], prompt_file: str, *, workspace: str) -> str:
    home = str(profile.get("codex_home") or os.environ.get("TA_CODEX_HOME") or CODEX_HOME_DEFAULT)
    args = [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
    ]
    model = profile.get("model")
    if model:
        args += ["-m", str(model)]
    effort_name = str(profile.get("effort") or "default")
    if effort_name not in CODEX_EFFORTS:
        known = ", ".join(sorted(CODEX_EFFORTS))
        raise HeadLaunchError(f"codex profile has unknown effort {effort_name!r} (known: {known})")
    effort = CODEX_EFFORTS[effort_name]
    if effort:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    for path in _codex_trust_paths(workspace):
        args += ["-c", f"projects.{json.dumps(path)}.trust_level=\"trusted\""]
    return f"CODEX_HOME={shlex.quote(home)} {shlex.join(args)} {_prompt_substitution(prompt_file)}"


def wrap_role_shell_command(role: str, command: str) -> str:
    py_path = "\"${TA_SECRETARY_REPO:-/home/dev/secretary}${PYTHONPATH:+:$PYTHONPATH}\""
    return (
        f"PYTHONPATH={py_path} python3 -m secretary.role_env exec "
        f"--role {shlex.quote(role)} -- /bin/sh -lc {shlex.quote(command)}"
    )


def _prompt_substitution(prompt_file: str) -> str:
    return f"\"$(cat {shlex.quote(prompt_file)})\""


def _resolve_git_path(value: str, base: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _workspace_git_dir(workspace_path: Path) -> Path | None:
    dotgit = workspace_path / ".git"
    try:
        if dotgit.is_dir():
            return dotgit.resolve(strict=False)
        if dotgit.is_file():
            first = dotgit.read_text(encoding="utf-8").splitlines()[0].strip()
        else:
            return None
    except (OSError, IndexError, UnicodeError):
        return None
    if not first.startswith("gitdir:"):
        return None
    return _resolve_git_path(first.split(":", 1)[1].strip(), workspace_path)


def _git_common_dir(git_dir: Path) -> Path:
    common = git_dir / "commondir"
    try:
        if common.is_file():
            value = common.read_text(encoding="utf-8").splitlines()[0].strip()
            if value:
                return _resolve_git_path(value, git_dir)
    except (OSError, IndexError, UnicodeError):
        pass
    return git_dir.resolve(strict=False)


def _codex_repository_trust_root(workspace_path: Path) -> Path | None:
    git_dir = _workspace_git_dir(workspace_path)
    if git_dir is None:
        return None
    common_dir = _git_common_dir(git_dir)
    if common_dir.name != ".git":
        return None
    try:
        if git_dir != common_dir and not git_dir.is_relative_to(common_dir / "worktrees"):
            return None
    except ValueError:
        return None
    return common_dir.parent.resolve(strict=False)


def _codex_trust_paths(workspace: str) -> list[str]:
    workspace_path = Path(workspace).resolve(strict=False)
    paths = [workspace_path]
    repo_root = _codex_repository_trust_root(workspace_path)
    if repo_root is not None:
        paths.append(repo_root)
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
