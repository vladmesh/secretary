"""Head command rendering for dispatcher-launched workers and reviewers."""

from __future__ import annotations

import json
import os
import shlex
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from secretary.role_env import RoleEnvError, runtime_env

CODEX_HOME_DEFAULT = "/home/dev/.config/orca/codex-runtime-home/home"
# The file codex itself reads trust from, inside whatever CODEX_HOME the head runs with.
CODEX_CONFIG_FILE = "config.toml"
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


def ensure_codex_workspace_trusted(
    profile: dict[str, Any],
    workspace: str,
    config: Path | None = None,
) -> None:
    """Answer the codex trust question for one workspace before a head starts in it.

    An interactive codex asks about trust before it takes a prompt, and it asks about the
    repository root of the directory it starts in rather than that directory: a worktree inherits
    the answer given to the repo it was cut from. The `-c projects...trust_level` overrides the
    launch command carries do not reach that check (codex 0.145 still shows the dialog with them
    in place), so a head whose root codex has never seen sits on the dialog, never goes idle, and
    never receives its prompt. The answer lives in `config.toml` of the CODEX_HOME the head runs
    with, which is where codex writes it when a human picks "Yes, continue", so the product writes
    it there instead of leaving the workspace waiting for that human.

    Both the workspace and its repository root are recorded: the root is what codex checks inside
    a git repo, the workspace itself is what it checks outside one, and neither is known to be the
    case from here. Trust already on file is left alone, and a path the file keeps at another
    trust level is somebody's decision, so it fails the bring-up with a readable reason instead of
    being overwritten.
    """
    config_path = config or Path(_codex_home(profile)) / CODEX_CONFIG_FILE
    text = _read_codex_config(config_path)
    projects = _codex_config_projects(text, config_path)
    additions: list[str] = []
    for target in _codex_trust_paths(workspace):
        entry = projects.get(target)
        if entry is None:
            additions.append(target)
            continue
        if not isinstance(entry, dict):
            raise HeadLaunchError(
                f"codex config {config_path} has a non-table project entry for {target}"
            )
        level = str(entry.get("trust_level") or "")
        if level == "trusted":
            continue
        raise HeadLaunchError(
            f"codex config {config_path} keeps {target} at trust_level {level or '(none)'!r}"
        )
    if not additions:
        return
    body = text if text.endswith("\n") or not text else f"{text}\n"
    for target in additions:
        body += f"\n[projects.{json.dumps(target)}]\ntrust_level = \"trusted\"\n"
    _save_codex_config(config_path, body)


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


def claude_launch_model(
    profile: dict[str, Any],
    *,
    workspace: str = "",
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """The model a `claude` bring-up will run under, and where that value came from.

    A profile without `model` (`claude-default`) renders a command without `--model`, so the CLI
    picks the model itself. The routing journal has to name that model as of the bring-up rather
    than record an empty field, so this reads the same sources the CLI reads, in the CLI's own
    precedence: enterprise policy over the command line, the command line over the environment,
    then the workspace's settings, then the user's. When nothing pins a model anywhere the value
    stays empty under a `cli_default` source, which says the CLI's built-in default applied instead
    of guessing which model that is.

    `env` is the environment the head itself will run with, which is not the dispatcher's own:
    heads are executed through `wrap_role_shell_command`, and `role_env.runtime_env` drops every
    `runtime.env` variable that is not role-allowlisted. Reading `os.environ` here would journal an
    `ANTHROPIC_MODEL` or a `CLAUDE_CONFIG_DIR` that the CLI never receives. Callers that are not
    launching a head can leave it unset and get the current process environment.
    """
    environ = os.environ if env is None else env
    managed = _settings_model(
        Path(environ.get(CLAUDE_MANAGED_SETTINGS_ENV) or CLAUDE_MANAGED_SETTINGS_DEFAULT)
    )
    if managed:
        return managed, "managed_settings"
    pinned = str(profile.get("model") or "")
    if pinned:
        return pinned, "profile"
    from_env = str(environ.get(CLAUDE_MODEL_ENV) or "").strip()
    if from_env:
        return from_env, f"env:{CLAUDE_MODEL_ENV}"
    if workspace:
        for name, source in (("settings.local.json", "project_settings_local"), ("settings.json", "project_settings")):
            model = _settings_model(Path(workspace) / ".claude" / name)
            if model:
                return model, source
    user = _settings_model(_claude_config_dir(environ) / "settings.json")
    if user:
        return user, "user_settings"
    return "", "cli_default"


def _claude_config_dir(env: Mapping[str, str]) -> Path:
    override = env.get(CLAUDE_CONFIG_DIR_ENV)
    if override:
        return Path(override)
    home = env.get("HOME")
    return (Path(home) if home else Path.home()) / ".claude"


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
    # The `projects` overrides below state the intent on the command line; what the TUI actually
    # checks before it asks about trust is `config.toml`, written by `ensure_codex_workspace_trusted`.
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


def _read_codex_config(config: Path) -> str:
    _reject_symlinked_config(config, "codex")
    try:
        return config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise HeadLaunchError(f"cannot read codex config {config}: {exc}") from None


def _codex_config_projects(text: str, config: Path) -> dict[str, Any]:
    try:
        loaded = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HeadLaunchError(f"cannot read codex config {config}: {exc}") from None
    projects = loaded.get("projects", {})
    if not isinstance(projects, dict):
        raise HeadLaunchError(f"codex config {config} has a non-table projects value")
    return projects


def _save_codex_config(config: Path, text: str) -> None:
    """Replace the codex config with `text`, but only once it parses as the TOML codex will read.

    The new trust tables are appended to the file as it stands rather than re-rendered from a
    parse, because this file is the installation's own: comments, ordering and everything the
    dispatcher has no opinion about survive a bring-up untouched. Appending can only produce
    invalid TOML if the file already declared `projects` in a form a table header cannot extend,
    so the result is parsed back before it replaces anything: a codex config the dispatcher cannot
    write safely leaves the bring-up deferred with a reason, not a codex that no longer starts.
    """
    _codex_config_projects(text, config)
    temp_path: Path | None = None
    try:
        config.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlinked_config(config, "codex")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{config.name}.", suffix=".tmp", dir=config.parent, text=True
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _reject_symlinked_config(config, "codex")
        os.replace(temp_path, config)
    except OSError as exc:
        raise HeadLaunchError(f"cannot update codex config {config}: {exc}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _reject_symlinked_claude_config(config: Path) -> None:
    _reject_symlinked_config(config, "Claude")


def _reject_symlinked_config(config: Path, kind: str) -> None:
    try:
        mode = config.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise HeadLaunchError(f"cannot inspect {kind} config {config}: {exc}") from None
    if stat.S_ISLNK(mode):
        raise HeadLaunchError(f"refusing symlinked {kind} config {config}")
    if not stat.S_ISREG(mode):
        raise HeadLaunchError(f"{kind} config {config} is not a regular file")


def role_launch_env(role: str) -> dict[str, str]:
    """The environment `wrap_role_shell_command` will actually hand a head of `role`.

    Same call the wrapper makes, so a snapshot taken here sees what the head sees rather than what
    the dispatcher happens to carry. A role the allowlist does not know is not launchable through
    the wrapper at all; the dispatcher's own environment is the closest honest answer there.
    """
    try:
        return runtime_env(role)
    except RoleEnvError:
        return dict(os.environ)


def wrap_role_shell_command(role: str, command: str) -> str:
    py_path = "\"${TA_SECRETARY_REPO:-/home/dev/secretary}${PYTHONPATH:+:$PYTHONPATH}\""
    return (
        f"PYTHONPATH={py_path} python3 -m secretary.role_env exec "
        f"--role {shlex.quote(role)} -- /bin/sh -lc {shlex.quote(command)}"
    )


def with_pid_heartbeat(command: str, pid_file: str) -> str:
    """Prefix a head's launch command so its own pid lands in `pid_file` right before it execs.

    `$$` inside a shell always names that shell's own pid, and the trailing `exec` replaces the
    shell's process image with the head instead of forking it as a child, so the pid written here
    stays the head's own pid for its whole life. That holds regardless of whether the local `sh`
    would otherwise have folded a single trailing command into an exec on its own: the two
    statements before `;` force a real shell to run first, which is what makes `$$` mean anything.
    Orca still keeps the pane's own wrapping shell around once the head exits, but that shell is no
    longer this pid, so the watchdog can tell the two apart.

    Catalog-launched commands (`head_launch`) start with a leading `NAME=value` environment
    assignment, e.g. `PYTHONPATH=... python3 -m secretary.role_env exec ...`. POSIX `exec` treats
    the word right after it as the program to run, not an assignment, so `exec PYTHONPATH=... python3`
    fails to find a program named `PYTHONPATH=...`. Routing the whole command through `env` instead
    keeps `exec` a single-word invocation while `env` itself parses and applies any leading
    assignments before it execs the real program in place, so the pid captured above still ends up
    belonging to the head once `env` hands off to it.
    """
    return f'echo "$$" > {shlex.quote(pid_file)}; exec env {command}'


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
