"""Head command rendering for dispatcher-launched workers and reviewers."""

from __future__ import annotations

import json
import os
import shlex
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CODEX_HOME_DEFAULT = "/home/dev/.config/orca/codex-runtime-home/home"
CLAUDE_JSON_DEFAULT = str(Path.home() / ".claude.json")
CLAUDE_THEME_DEFAULT = "dark"
# Where the `claude` CLI itself takes a model from when the head profile pins none.
CLAUDE_MANAGED_SETTINGS_DEFAULT = "/etc/claude-code/managed-settings.json"
CLAUDE_MANAGED_SETTINGS_ENV = "CLAUDE_MANAGED_SETTINGS"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
CLAUDE_MODEL_ENV = "ANTHROPIC_MODEL"
CODEX_EFFORTS = {
    "default": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "ultra",
}
CODEX_LAUNCH_MODES = {"exec", "tui"}


class HeadLaunchError(RuntimeError):
    pass


@dataclass(frozen=True)
class HeadLaunch:
    command: str
    prompt_after_start: bool = False


def ensure_claude_workspace_trusted(workspace: str, config: Path | None = None) -> None:
    """Mark one Claude Code workspace trusted before a headless launch."""
    config_path = config or Path(os.environ.get("TA_CLAUDE_JSON", CLAUDE_JSON_DEFAULT))
    data = _load_claude_config(config_path)
    if _mark_claude_workspace_trusted(data, str(workspace), config_path):
        _save_claude_config(config_path, data)


def ensure_claude_workspace_ready(
    workspace: str,
    config: Path | None = None,
    *,
    default_theme: str = CLAUDE_THEME_DEFAULT,
) -> None:
    """Pre-answer Claude Code first-run prompts for one headless workspace."""
    config_path = config or Path(os.environ.get("TA_CLAUDE_JSON", CLAUDE_JSON_DEFAULT))
    data = _load_claude_config(config_path)
    changed = _mark_claude_workspace_trusted(data, str(workspace), config_path)
    changed = _ensure_claude_theme(data, default_theme) or changed
    if changed:
        _save_claude_config(config_path, data)


def render_claude_command(
    profile: dict[str, Any],
    prompt_file: str,
    *,
    launch_prompt: str | None = None,
) -> str:
    args = ["claude", "--dangerously-skip-permissions"]
    model = profile.get("model")
    if model:
        args += ["--model", str(model)]
    return f"{shlex.join(args)} {_delivered_prompt(prompt_file, launch_prompt)}"


def render_codex_command(
    profile: dict[str, Any],
    prompt_file: str,
    *,
    workspace: str,
    mode: str | None = None,
    launch_prompt: str | None = None,
) -> str:
    return render_codex_launch(
        profile, prompt_file, workspace=workspace, mode=mode, launch_prompt=launch_prompt
    ).command


def render_codex_launch(
    profile: dict[str, Any],
    prompt_file: str,
    *,
    workspace: str,
    mode: str | None = None,
    launch_prompt: str | None = None,
) -> HeadLaunch:
    launch_mode = _codex_launch_mode(profile, mode)
    if launch_mode == "tui":
        # The TUI carries no prompt on its command line; the caller delivers launch_prompt
        # (or the prompt_file contents) through `orca terminal send` once the TUI is idle.
        return HeadLaunch(_render_codex_tui_command(profile, workspace=workspace), prompt_after_start=True)
    return HeadLaunch(
        _render_codex_exec_command(profile, prompt_file, workspace=workspace, launch_prompt=launch_prompt)
    )


def claude_launch_model(profile: dict[str, Any], *, workspace: str = "") -> tuple[str, str]:
    """The model a `claude` bring-up will run under, and where that value came from.

    A profile without `model` (`claude-default`) renders a command without `--model`, so the CLI
    picks the model itself. The routing journal has to name that model as of the bring-up rather
    than record an empty field, so this reads the same sources the CLI reads, in the CLI's own
    precedence: enterprise policy over the command line, the command line over the environment,
    then the workspace's settings, then the user's. When nothing pins a model anywhere the value
    stays empty under a `cli_default` source, which says the CLI's built-in default applied instead
    of guessing which model that is.
    """
    managed = _settings_model(Path(os.environ.get(CLAUDE_MANAGED_SETTINGS_ENV, CLAUDE_MANAGED_SETTINGS_DEFAULT)))
    if managed:
        return managed, "managed_settings"
    pinned = str(profile.get("model") or "")
    if pinned:
        return pinned, "profile"
    from_env = str(os.environ.get(CLAUDE_MODEL_ENV) or "").strip()
    if from_env:
        return from_env, f"env:{CLAUDE_MODEL_ENV}"
    if workspace:
        for name, source in (("settings.local.json", "project_settings_local"), ("settings.json", "project_settings")):
            model = _settings_model(Path(workspace) / ".claude" / name)
            if model:
                return model, source
    user = _settings_model(_claude_config_dir() / "settings.json")
    if user:
        return user, "user_settings"
    return "", "cli_default"


def _claude_config_dir() -> Path:
    override = os.environ.get(CLAUDE_CONFIG_DIR_ENV)
    return Path(override) if override else Path.home() / ".claude"


def _settings_model(path: Path) -> str:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(loaded, dict):
        return ""
    return str(loaded.get("model") or "").strip()


def _render_codex_exec_command(
    profile: dict[str, Any],
    prompt_file: str,
    *,
    workspace: str,
    launch_prompt: str | None = None,
) -> str:
    args = _codex_base_args(profile)
    args.insert(1, "exec")
    args.append("--skip-git-repo-check")
    for path in _codex_trust_paths(workspace):
        args += ["-c", f"projects.{json.dumps(path)}.trust_level=\"trusted\""]
    return f"CODEX_HOME={shlex.quote(_codex_home(profile))} {shlex.join(args)} {_delivered_prompt(prompt_file, launch_prompt)}"


def _render_codex_tui_command(profile: dict[str, Any], *, workspace: str) -> str:
    args = _codex_base_args(profile)
    for path in _codex_trust_paths(workspace):
        args += ["-c", f"projects.{json.dumps(path)}.trust_level=\"trusted\""]
    return f"CODEX_HOME={shlex.quote(_codex_home(profile))} {shlex.join(args)}"


def _codex_base_args(profile: dict[str, Any]) -> list[str]:
    args = [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    model = profile.get("model")
    if model:
        args += ["-m", str(model)]
    effort = _codex_effort(profile)
    if effort:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    return args


def _codex_home(profile: dict[str, Any]) -> str:
    return str(profile.get("codex_home") or os.environ.get("TA_CODEX_HOME") or CODEX_HOME_DEFAULT)


def _codex_effort(profile: dict[str, Any]) -> str | None:
    effort_name = str(profile.get("effort") or "default")
    if effort_name not in CODEX_EFFORTS:
        known = ", ".join(sorted(CODEX_EFFORTS))
        raise HeadLaunchError(f"codex profile has unknown effort {effort_name!r} (known: {known})")
    return CODEX_EFFORTS[effort_name]


def _codex_launch_mode(profile: dict[str, Any], override: str | None) -> str:
    mode = (override or str(profile.get("codex_mode") or "exec")).strip()
    if mode not in CODEX_LAUNCH_MODES:
        known = ", ".join(sorted(CODEX_LAUNCH_MODES))
        raise HeadLaunchError(f"codex profile has unknown launch mode {mode!r} (known: {known})")
    return mode


def _load_claude_config(config: Path) -> dict[str, Any]:
    _reject_symlinked_claude_config(config)
    try:
        if not config.exists():
            return {}
        loaded = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HeadLaunchError(f"cannot read Claude config {config}: {exc}") from None
    if not isinstance(loaded, dict):
        raise HeadLaunchError(f"Claude config {config} has an unsupported shape")
    return loaded


def _mark_claude_workspace_trusted(data: dict[str, Any], workspace: str, config: Path) -> bool:
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise HeadLaunchError(f"Claude config {config} has non-object projects")
    entry = projects.setdefault(workspace, {})
    if not isinstance(entry, dict):
        raise HeadLaunchError(f"Claude config {config} has non-object project entry for {workspace}")
    if entry.get("hasTrustDialogAccepted") is True:
        return False
    entry["hasTrustDialogAccepted"] = True
    return True


def _ensure_claude_theme(data: dict[str, Any], default_theme: str) -> bool:
    if data.get("theme"):
        return False
    data["theme"] = default_theme
    return True


def _save_claude_config(config: Path, data: dict[str, Any]) -> None:
    temp_path: Path | None = None
    try:
        _reject_symlinked_claude_config(config)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{config.name}.",
            suffix=".tmp",
            dir=config.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _reject_symlinked_claude_config(config)
        os.replace(temp_path, config)
    except OSError as exc:
        raise HeadLaunchError(f"cannot update Claude config {config}: {exc}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _reject_symlinked_claude_config(config: Path) -> None:
    try:
        mode = config.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise HeadLaunchError(f"cannot inspect Claude config {config}: {exc}") from None
    if stat.S_ISLNK(mode):
        raise HeadLaunchError(f"refusing symlinked Claude config {config}")
    if not stat.S_ISREG(mode):
        raise HeadLaunchError(f"Claude config {config} is not a regular file")


def wrap_role_shell_command(role: str, command: str) -> str:
    py_path = "\"${TA_SECRETARY_REPO:-/home/dev/secretary}${PYTHONPATH:+:$PYTHONPATH}\""
    return (
        f"PYTHONPATH={py_path} python3 -m secretary.role_env exec "
        f"--role {shlex.quote(role)} -- /bin/sh -lc {shlex.quote(command)}"
    )


def _delivered_prompt(prompt_file: str, launch_prompt: str | None) -> str:
    """The prompt argument handed to a head on its command line. A launch_prompt is a short
    literal pointer (task body lives in prompt_file, which the head opens itself); without it
    the whole prompt_file is piped in as the prompt, the reviewer's REVIEW.md path."""
    if launch_prompt is not None:
        return shlex.quote(launch_prompt)
    return _prompt_substitution(prompt_file)


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
