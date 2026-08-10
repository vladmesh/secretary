"""Head command rendering for dispatcher-launched workers and reviewers."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from secretary.role_env import (
    OBSERVER_GENERATION_ENV,
    OBSERVER_SPRINT_ENV,
    ROLE_ALLOWLIST,
    RUNTIME_ENV_FILE_ENVS,
    UNIT_BOUND_ENV,
    RoleEnvError,
    runtime_env,
)
from triggered_agents.agents.pipeline.heads import (
    CLAUDE_EFFORTS,
    CODEX_EFFORTS,
)
from triggered_agents.agents.pipeline.task_protocol import pythonpath_prefix
from triggered_agents.runtime.codex_preflight import (
    CODEX_HOME_DEFAULT,
    CodexPreflightError,
    codex_home as _codex_home,
    codex_trust_paths as _codex_trust_paths,
    reject_symlinked_config as _reject_symlinked_config,
)
from triggered_agents.runtime.codex_preflight import (
    ensure_codex_workspace_trusted as _preflight_codex_workspace,
)

# What a launched head has to be told about the installation it belongs to. The env file name
# first: everything else about the head's runtime comes out of that file.
#
# The observer identity is bound the same way but not taken from here: it names one head rather
# than the installation, so it comes from the record being launched and is passed per call. The
# dispatcher's own environment must never answer for it.
LAUNCH_BOUND_ENV = tuple(
    name for name in (*RUNTIME_ENV_FILE_ENVS, *UNIT_BOUND_ENV)
    if name not in {OBSERVER_SPRINT_ENV, OBSERVER_GENERATION_ENV}
)

PYTHON_SAFE_PATH_FLAG = "-P"
CLAUDE_JSON_DEFAULT = str(Path.home() / ".claude.json")
CLAUDE_THEME_DEFAULT = "dark"
# Where the `claude` CLI itself takes a model from when the head profile pins none.
CLAUDE_MANAGED_SETTINGS_DEFAULT = "/etc/claude-code/managed-settings.json"
CLAUDE_MANAGED_SETTINGS_ENV = "CLAUDE_MANAGED_SETTINGS"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
CLAUDE_MODEL_ENV = "ANTHROPIC_MODEL"


class HeadLaunchError(RuntimeError):
    pass


@dataclass(frozen=True)
class HeadLaunch:
    command: str
    prompt_after_start: bool = False


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
    """The dispatcher's way in to the shared Codex interactive preflight.

    The preflight itself lives in `triggered_agents.runtime.codex_preflight`, beside the delivery
    primitive and for the same reason: the triggered-agents service launcher brings up interactive
    Codex heads too and cannot import `secretary`. One implementation of "make this workspace fit
    for a Codex pane" therefore has to sit where both callers can reach it, and this is the seam
    that turns its failure into the `HeadLaunchError` every other bring-up step here raises — so
    a dispatcher caller still has one exception type to catch for a head that cannot be launched.
    """
    try:
        _preflight_codex_workspace(profile, workspace, config)
    except CodexPreflightError as exc:
        raise HeadLaunchError(str(exc)) from None


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
    effort = str(profile.get("effort") or "default")
    if effort not in CLAUDE_EFFORTS:
        known = ", ".join(sorted(CLAUDE_EFFORTS))
        raise HeadLaunchError(f"claude profile has unknown effort {effort!r} (known: {known})")
    if effort != "default":
        args += ["--effort", effort]
    return f"{shlex.join(args)} {_delivered_prompt(prompt_file, launch_prompt)}"


def render_codex_command(
    profile: dict[str, Any],
    prompt_file: str,
    *,
    workspace: str,
    launch_prompt: str | None = None,
) -> str:
    return render_codex_launch(
        profile, prompt_file, workspace=workspace, launch_prompt=launch_prompt
    ).command


def render_codex_launch(
    profile: dict[str, Any],
    prompt_file: str,
    *,
    workspace: str,
    launch_prompt: str | None = None,
) -> HeadLaunch:
    """The command that brings one Codex head up. There is one shape and it is interactive.

    Nothing selects it: no profile field, no card, no caller argument. The one-shot `codex exec`
    head is gone (secretary-1173), so a launch mode carried by routing data or by a registry that
    predates that has nothing left to select and is not consulted here at all.

    The TUI carries no prompt on its command line, which is what `prompt_after_start` says: the
    caller delivers `launch_prompt` (or the prompt_file contents) through `orca terminal send`
    once the pane is ready. `prompt_file` and `launch_prompt` are still named on the signature
    because they are the caller's own prompt inputs, and the caller resolves them the same way for
    every adapter.
    """
    return HeadLaunch(
        _render_codex_tui_command(profile, workspace=workspace), prompt_after_start=True
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


def _codex_effort(profile: dict[str, Any]) -> str | None:
    effort_name = str(profile.get("effort") or "default")
    if effort_name not in CODEX_EFFORTS:
        known = ", ".join(sorted(CODEX_EFFORTS))
        raise HeadLaunchError(f"codex profile has unknown effort {effort_name!r} (known: {known})")
    return CODEX_EFFORTS[effort_name]


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
    """The same guard the codex preflight writes its config behind, answered in this module's own
    failure type so a Claude bring-up still raises what its callers catch."""
    try:
        _reject_symlinked_config(config, "Claude")
    except CodexPreflightError as exc:
        raise HeadLaunchError(str(exc)) from None


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


def launch_binding() -> list[str]:
    """The installation binding a launched head has to carry in its own command line.

    A head does not start as a child of the dispatcher: Orca creates the terminal, so nothing the
    dispatcher's unit exported is guaranteed to be in the environment `role_env exec` then runs
    in. Without these, a dispatcher rendered for a non-default instance launches heads that read
    the home default's `runtime.env` and route off that installation's heads. Rendering
    the names into the command is what binds them; the `UNIT_BOUND_ENV` rule inside `runtime_env`
    then keeps the file from taking the instance back.

    Only names the launcher was actually given are rendered. Writing out the fallback path when
    nothing selected an installation would state a choice nobody made, and would override whatever
    the environment the head starts in has to say about it.
    """
    return [
        f"{name}={shlex.quote(value)}"
        for name in LAUNCH_BOUND_ENV
        if (value := os.environ.get(name))
    ]


def wrap_role_shell_command(role: str, command: str, *, identity: dict[str, str] | None = None) -> str:
    """Render one head's command with the installation binding and, for a head that has one, its
    identity.

    `identity` is rendered beside the installation binding rather than left to `runtime.env`, so
    the same `UNIT_BOUND_ENV` rule that keeps the file from moving the installation keeps it from
    renaming the caller. Only names the role's allowlist knows are rendered; anything else would
    be dropped by `runtime_env` on the way in and is refused here instead of silently ignored.
    """
    unknown = sorted(set(identity or {}) - set(ROLE_ALLOWLIST.get(role, ())))
    if unknown:
        raise HeadLaunchError(f"role {role!r} carries no binding named {', '.join(unknown)}")
    rendered = [f"{name}={shlex.quote(value)}" for name, value in sorted((identity or {}).items())]
    binding = " ".join([*launch_binding(), *rendered])
    return (
        f"{binding} {pythonpath_prefix(os.environ)} python3 {PYTHON_SAFE_PATH_FLAG} -m secretary.role_env exec "
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
    assignment, e.g. `PYTHONPATH=... python3 -P -m secretary.role_env exec ...`. POSIX `exec` treats
    the word right after it as the program to run, not an assignment, so `exec PYTHONPATH=... python3`
    fails to find a program named `PYTHONPATH=...`. Routing the whole command through `env` instead
    keeps `exec` a single-word invocation while `env` itself parses and applies any leading
    assignments before it execs the real program in place, so the pid captured above still ends up
    belonging to the head once `env` hands off to it.
    """
    # The terminal already puts its foreground head in a process group. Keeping that terminal
    # session matters for interactive heads: they need /dev/tty, resize signals and normal pane
    # teardown. CommandHostRuntime signals that existing group when it is safe to do so.
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
