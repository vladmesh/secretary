"""Interactive secretary launch — the trusted operator entry point.

`secretary shell` is the human operator's own tool, not a pipeline role. It boots a chosen head
(claude, codex, hermes or any heads.toml profile) with the *full* installation runtime env, so
board access and every other credential are present regardless of which head runs. Automated
worker/reviewer heads stay narrowly scoped through role_env; the operator deliberately does not.

The env is injected at the launch boundary, not by the head, so switching heads never changes
whether the credentials are there.
"""
from __future__ import annotations

import argparse
import os
import sys

from secretary.role_env import RUNTIME_ENV, load_env_file
from triggered_agents.agents.pipeline import heads as head_registry

# Board credentials the operator must have to touch the Pipeline board. The operator gets the whole
# runtime env, but a missing board token is a launch-time error worth catching loudly rather than a
# confusing `secretary task` failure once inside the head.
BOARD_ENV = ("KANBOARD_URL", "KANBOARD_API_USER", "KANBOARD_API_TOKEN")

# The operator names a head the way a human thinks about it ("claude", "codex", "hermes"). Map a
# bare adapter name to a concrete default profile. Any real heads.toml profile id is also accepted
# verbatim, so `--head claude-opus` or `--head codex-high` work too.
ADAPTER_DEFAULT_PROFILE = {
    "claude": "claude-default",
    "codex": "codex",
    "hermes": "hermes",
}
DEFAULT_HEAD = "claude-default"


class SessionError(RuntimeError):
    pass


def operator_env(
    env_file: str | os.PathLike[str] | None = None,
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Full runtime env for the operator: base process env overlaid with the entire runtime.env.

    No allowlist and no sensitive-name scrubbing — that is the point. Fails closed if the board
    credentials are absent.
    """
    base = dict(os.environ if base_env is None else base_env)
    source = load_env_file(env_file)
    env = {**base, **source}
    env["SECRETARY_ROLE"] = "operator"
    missing = [key for key in BOARD_ENV if not env.get(key)]
    if missing:
        where = env_file or RUNTIME_ENV
        raise SessionError(f"runtime env missing {', '.join(missing)} (checked {where})")
    return env


def resolve_profile_id(head: str | None, *, registry: head_registry.Registry | None = None) -> str:
    """Resolve a user-supplied head name to a real heads.toml profile id."""
    reg = registry or head_registry.load_registry()
    name = head or DEFAULT_HEAD
    candidate = ADAPTER_DEFAULT_PROFILE.get(name, name)
    reg.profile(candidate)  # raises HeadRegistryError listing known ids if unknown
    return candidate


def render_interactive(
    profile_id: str,
    *,
    workspace: str | None = None,
    registry: head_registry.Registry | None = None,
) -> str:
    """The interactive (no seeded prompt) launch command for a profile's adapter."""
    reg = registry or head_registry.load_registry()
    profile = reg.profile(profile_id)
    adapter = profile.get("adapter")
    if adapter == "claude":
        model = profile.get("model")
        model_flag = f" --model {model}" if model else ""
        return f"claude --dangerously-skip-permissions{model_flag}"
    if adapter == "codex":
        # Reuse the pipeline's TUI render: it carries CODEX_HOME, model/effort and the
        # directory-trust flags for the workspace so no trust dialog blocks the pinned launch.
        return head_registry._render_codex_tui(profile, workspace=workspace or os.getcwd())
    if adapter == "hermes":
        parts = ["hermes"]
        if profile.get("model"):
            parts += ["-m", profile["model"]]
        if profile.get("provider"):
            parts += ["--provider", profile["provider"]]
        parts += ["--yolo", "--cli"]
        return " ".join(parts)
    raise SessionError(f"adapter {adapter!r} has no interactive launch shape")


def run_shell(args: argparse.Namespace) -> int:
    try:
        profile_id = resolve_profile_id(args.head)
        command = render_interactive(profile_id, workspace=args.workspace)
        env = operator_env(args.env_file)
    except (SessionError, head_registry.HeadRegistryError) as exc:
        print(f"secretary shell: {exc}", file=sys.stderr)
        return 2
    if args.print_command:
        print(command)
        return 0
    argv = ["/bin/sh", "-c", command]
    try:
        os.execvpe(argv[0], argv, env)
    except OSError as exc:
        print(f"secretary shell: exec {command!r} failed: {exc}", file=sys.stderr)
        return 126
    return 0  # unreachable after a successful execvpe
