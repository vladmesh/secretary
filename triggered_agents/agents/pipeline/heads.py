"""Head registry — turns a profile id from `heads.toml` into a launch command.

A worker/reviewer head is data (heads.toml: `[resources.*]` the accounts/limits heads draw from,
`[profiles.*]` the runtime+model+resource+fallback-chain combos), not a hardcoded `claude`
invocation. `render_command` picks a profile's adapter (ADAPTERS below) and builds the shell
command worker.py hands to `orca terminal create`. A new head is a new `[profiles.<id>]` entry
plus, only if its launch shape is genuinely new, one more `_render_*` function here — dispatcher.py
and worker.py never change.

Pure and I/O-light (`load_registry` caches its toml read per process — see its docstring): no
Kanboard, no orca, no subprocess.
"""
from __future__ import annotations

import json
import os
import shlex
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ...runtime import role_env

HEADS_TOML = Path(__file__).with_name("heads.toml")

# The CODEX_HOME shared by Orca-managed Codex sessions and pipeline heads. One physical home keeps
# auth refresh, MCP, skills, hooks, and quota probes on the same state. Pinned explicitly because
# the health probe is a plain subprocess rather than an Orca terminal. Env-overridable so tests can
# use a throwaway home.
CODEX_HOME = os.environ.get(
    "TA_CODEX_HOME", "/home/dev/.config/orca/codex-runtime-home/home"
)

# The profile a card gets when it names no head at all. New work defaults to Codex; legacy cards
# with explicit claude-* metadata keep using that exact profile until a PO updates them.
DEFAULT_PROFILE = "codex"

CODEX_EFFORTS = {
    "default": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "xhigh": "xhigh",
}

CODEX_LAUNCH_MODES = {"exec", "tui"}


@dataclass(frozen=True)
class LaunchSpec:
    """A terminal create command plus an optional prompt delivered after the TUI is ready."""

    command: str
    initial_prompt: str | None = None
    terminal_kind: str | None = None


class HeadRegistryError(RuntimeError):
    """heads.toml is missing/malformed, or a profile/resource/adapter/fallback it names is unknown."""


def _render_claude(profile: dict, *, prompt: str) -> str:
    model = profile.get("model")
    model_flag = f" --model {model}" if model else ""
    return f"claude --dangerously-skip-permissions{model_flag} {prompt!r}"


def _render_hermes(profile: dict, *, prompt: str) -> str:
    """Hermes' one-shot-seeded-session equivalent of `claude --dangerously-skip-permissions
    <prompt>`: `-z` seeds an autonomous session with the initial message (not `-q`/`chat`'s
    single-turn query mode), `--yolo` is Hermes' skip-permissions, `--cli` forces the plain REPL
    (no TUI) so it behaves in an Orca terminal the same way the classic `claude` invocation does."""
    parts = ["hermes", "-z", repr(prompt)]
    if profile.get("model"):
        parts += ["-m", profile["model"]]
    if profile.get("provider"):
        parts += ["--provider", profile["provider"]]
    parts += ["--yolo", "--cli"]
    return " ".join(parts)


def _render_codex(profile: dict, *, prompt: str) -> str:
    """Codex' non-interactive equivalent of `claude --dangerously-skip-permissions <prompt>`:
    `exec` runs one-shot (prints the agent turn, no TUI), `--dangerously-bypass-approvals-and-sandbox`
    is codex' skip-permissions (the Orca worktree is the external sandbox; it also lets the memory
    MCP tool run without an interactive approval prompt), `--skip-git-repo-check` keeps a
    not-yet-a-repo workspace from aborting. `CODEX_HOME` is pinned to the ChatGPT-authed home
    (see CODEX_HOME above) so the head finds its login, memory MCP, and global AGENTS.md regardless
    of the launching terminal's env. `codex_home` on the profile overrides it (e.g. for an e2e)."""
    home = profile.get("codex_home") or CODEX_HOME
    model = profile.get("model")
    model_flag = f" -m {model}" if model else ""
    effort = CODEX_EFFORTS.get(profile.get("effort", "default"))
    effort_flag = ""
    if effort:
        effort_flag = " -c " + shlex.quote(f'model_reasoning_effort="{effort}"')
    return (f"CODEX_HOME={home} codex exec --dangerously-bypass-approvals-and-sandbox "
            f"--skip-git-repo-check{model_flag}{effort_flag} {prompt!r}")


def _toml_basic_string(value: str) -> str:
    return json.dumps(value)


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
            line = dotgit.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    if not dotgit.is_file():
        return None
    if not line.startswith("gitdir:"):
        return None
    return _resolve_git_path(line.split(":", 1)[1].strip(), workspace_path)


def _git_common_dir(git_dir: Path) -> Path:
    common = git_dir / "commondir"
    try:
        if common.is_file():
            value = common.read_text(encoding="utf-8").splitlines()[0].strip()
            if value:
                return _resolve_git_path(value, git_dir)
    except (OSError, IndexError):
        pass
    return git_dir.resolve(strict=False)


def _codex_repository_trust_root(workspace_path: Path) -> Path | None:
    """Codex' TUI trust check keys linked worktrees by the common git dir's repo root."""
    git_dir = _workspace_git_dir(workspace_path)
    if git_dir is None:
        return None
    common_dir = _git_common_dir(git_dir)
    if common_dir.name != ".git":
        return None
    if git_dir != common_dir and not git_dir.is_relative_to(common_dir / "worktrees"):
        return None
    return common_dir.parent.resolve(strict=False)


def _codex_tui_trust_paths(workspace: str) -> list[str]:
    workspace_path = Path(workspace).resolve(strict=False)
    paths = [workspace_path]
    repo_root = _codex_repository_trust_root(workspace_path)
    if repo_root is not None:
        paths.append(repo_root)
    seen = set()
    out = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _codex_trust_flag(path: str) -> str:
    key = f"projects.{_toml_basic_string(path)}.trust_level=\"trusted\""
    return " -c " + shlex.quote(key)


def _render_codex_tui(profile: dict, *, workspace: str | None = None) -> str:
    """Interactive Codex TUI command. The prompt is sent after Orca reports `tui-idle`.

    `--skip-git-repo-check` is an `exec`-only flag in Codex 0.143; the top-level TUI rejects it.
    Pipeline worker/reviewer workspaces are git worktrees already, so the TUI path does not need
    it. Trust overrides only skip Codex' directory-trust dialog for the provisioned worktree and,
    for linked worktrees, the same repository root Codex derives from the common git dir. The head
    already runs with `--dangerously-bypass-approvals-and-sandbox`, so this does not grant extra
    rights.
    """
    home = profile.get("codex_home") or CODEX_HOME
    model = profile.get("model")
    model_flag = f" -m {model}" if model else ""
    effort = CODEX_EFFORTS.get(profile.get("effort", "default"))
    effort_flag = ""
    if effort:
        effort_flag = " -c " + shlex.quote(f'model_reasoning_effort="{effort}"')
    if not workspace:
        raise HeadRegistryError("codex TUI launch requires workspace for directory trust override")
    trust_flags = "".join(_codex_trust_flag(path) for path in _codex_tui_trust_paths(workspace))
    return (f"CODEX_HOME={home} codex --dangerously-bypass-approvals-and-sandbox"
            f"{model_flag}{effort_flag}{trust_flags}")


def _env_codex_mode() -> str | None:
    mode = os.environ.get("TA_CODEX_MODE")
    if mode:
        mode = mode.strip().lower()
        if mode not in CODEX_LAUNCH_MODES:
            known = ", ".join(sorted(CODEX_LAUNCH_MODES))
            raise HeadRegistryError(f"unknown TA_CODEX_MODE {mode!r} (known: {known})")
        return mode
    flag = os.environ.get("TA_CODEX_TUI")
    if flag is None:
        return None
    flag = flag.strip().lower()
    if flag in {"", "0", "false", "no", "off"}:
        return "exec"
    return "tui"


def _codex_launch_mode(profile: dict) -> str:
    return _env_codex_mode() or profile.get("codex_mode", "exec")


ADAPTERS = {
    "claude": _render_claude,
    "hermes": _render_hermes,
    "codex": _render_codex,
}


class Registry:
    def __init__(self, resources: dict, profiles: dict):
        self.resources = resources
        self.profiles = profiles

    def profile(self, profile_id: str) -> dict:
        """The profile dict for `profile_id`, or HeadRegistryError with the known ids — the text
        a claim guard or a create/update validation surfaces verbatim to whoever reads it."""
        prof = self.profiles.get(profile_id)
        if prof is None:
            known = ", ".join(sorted(self.profiles)) or "(none)"
            raise HeadRegistryError(f"unknown head {profile_id!r} (known: {known})")
        return prof

    def known(self) -> list[str]:
        return sorted(self.profiles)


def profile_info(profile_id: str, registry: Registry | None = None) -> dict:
    """Display-facing profile facts. Unknown profiles return a marked record instead of raising.

    `effort` is Codex-specific in the current registry. Non-Codex adapters deliberately show
    `n/a` rather than an empty string, so board/list consumers never have to special-case a blank
    label.
    """
    reg = registry or load_registry()
    try:
        prof = reg.profile(profile_id)
    except HeadRegistryError:
        return {
            "profile": profile_id,
            "known": False,
            "adapter": "unknown",
            "model": "unknown",
            "effort": "unknown",
        }
    adapter = prof.get("adapter") or "unknown"
    effort = prof.get("effort", "default") if adapter == "codex" else "n/a"
    return {
        "profile": profile_id,
        "known": True,
        "adapter": adapter,
        "model": prof.get("model") or "default",
        "effort": effort,
    }


def _validate(resources: dict, profiles: dict) -> None:
    for pid, prof in profiles.items():
        resource = prof.get("resource")
        if resource not in resources:
            raise HeadRegistryError(f"profile {pid!r} references unknown resource {resource!r}")
        adapter = prof.get("adapter")
        if adapter not in ADAPTERS:
            raise HeadRegistryError(f"profile {pid!r} has unknown adapter {adapter!r} "
                                    f"(known: {', '.join(sorted(ADAPTERS))})")
        if adapter == "codex":
            effort = prof.get("effort", "default")
            if effort not in CODEX_EFFORTS:
                known = ", ".join(sorted(CODEX_EFFORTS))
                raise HeadRegistryError(f"profile {pid!r} has unknown codex effort {effort!r} "
                                        f"(known: {known})")
            mode = prof.get("codex_mode", "exec")
            if mode not in CODEX_LAUNCH_MODES:
                known = ", ".join(sorted(CODEX_LAUNCH_MODES))
                raise HeadRegistryError(f"profile {pid!r} has unknown codex launch mode {mode!r} "
                                        f"(known: {known})")
        for fb in prof.get("fallback") or []:
            if fb not in profiles:
                raise HeadRegistryError(f"profile {pid!r} fallback references unknown profile {fb!r}")


@lru_cache(maxsize=None)
def load_registry(path: Path = HEADS_TOML) -> Registry:
    """heads.toml, parsed and validated. Cached per (process, path) — every dispatcher tick is a
    fresh `python3 -m triggered_agents pipeline tick` process, so this only dedupes the 2+ reads
    a single tick already does (claim's `_check_head`, then bring-up's `render_command`), never a
    long-lived process going stale against an edited file on disk. A raised HeadRegistryError is
    not cached — the next call re-reads, so a fixed-then-retried registry recovers without a
    process restart."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise HeadRegistryError(f"head registry missing: {path}") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise HeadRegistryError(f"head registry {path} is not valid TOML: {e}") from e
    resources = data.get("resources") or {}
    profiles = data.get("profiles") or {}
    _validate(resources, profiles)
    return Registry(resources=resources, profiles=profiles)


def render_command(profile_id: str, *, role: str, prompt: str, registry: Registry | None = None) -> str:
    """The full shell command for `orca terminal create --command`.

    The role env wrapper sets BOARD_ROLE and the minimal role-specific runtime env before the
    adapter command execs. Secret values stay out of the command string Orca stores.
    """
    reg = registry or load_registry()
    profile = reg.profile(profile_id)
    render = ADAPTERS[profile["adapter"]]
    return role_env.wrap_shell_command(role, render(profile, prompt=prompt))


def render_launch(profile_id: str, *, role: str, prompt: str, workspace: str | None = None,
                  registry: Registry | None = None) -> LaunchSpec:
    """Launch contract for worker/reviewer terminals.

    Most adapters are single-command batch launches. Codex can opt into TUI mode through
    `codex_mode = "tui"` or `TA_CODEX_MODE=tui`: the terminal starts plain `codex`, waits for
    `tui-idle`, then receives `prompt` through `orca terminal send`.
    """
    reg = registry or load_registry()
    profile = reg.profile(profile_id)
    if profile["adapter"] == "codex" and _codex_launch_mode(profile) == "tui":
        return LaunchSpec(
            command=role_env.wrap_shell_command(role, _render_codex_tui(profile, workspace=workspace)),
            initial_prompt=prompt,
            terminal_kind="codex-tui",
        )
    return LaunchSpec(command=render_command(profile_id, role=role, prompt=prompt, registry=reg))


def terminal_kind(profile_id: str, *, registry: Registry | None = None) -> str | None:
    """The tracked Orca terminal kind this profile expects, or None for legacy batch sessions."""
    reg = registry or load_registry()
    profile = reg.profile(profile_id)
    if profile["adapter"] == "codex" and _codex_launch_mode(profile) == "tui":
        return "codex-tui"
    return None
