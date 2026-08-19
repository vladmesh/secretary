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

from secretary.board_transport import (
    BoardTransportError,
    resolve_for_environ,
)
from secretary.role_env import load_env_file
from triggered_agents.agents.pipeline import heads as head_registry
from triggered_agents.runtime.head import HeadCommandError, render_head_command

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

    No allowlist and no sensitive-name scrubbing — that is the point. Board transport is checked
    from local configuration, not injected as credentials.
    """
    base = dict(os.environ if base_env is None else base_env)
    source = load_env_file(env_file)
    env = {**base, **source}
    env["SECRETARY_ROLE"] = "operator"
    try:
        resolve_for_environ(env)
    except BoardTransportError as exc:
        raise SessionError(f"board transport configuration is unavailable: {exc}") from None
    return env


def resolve_profile_id(head: str | None, *, registry: head_registry.Registry | None = None) -> str:
    """Resolve a user-supplied head name to a real heads.toml profile id."""
    reg = registry or head_registry.load_registry()
    name = head or DEFAULT_HEAD
    # `resolve` keeps a Codex id from before the TUI-only rule pointing at the interactive Codex
    # profile the installation publishes now, and refuses the name outright when it has none left
    # rather than opening an operator session on whatever other family now answers to it. Anything
    # it does not recognise is handed to the lookup unchanged and still fails by name.
    candidate = reg.resolve(ADAPTER_DEFAULT_PROFILE.get(name, name))
    reg.profile(candidate)  # raises HeadRegistryError listing known ids if unknown
    return candidate


def render_interactive(
    profile_id: str,
    *,
    workspace: str | None = None,
    registry: head_registry.Registry | None = None,
) -> str:
    """The interactive (no seeded prompt) launch command for a profile's adapter.

    The same renderer every launched head goes through, asked for the same shape a dispatcher- or
    tick-launched head gets: `prompt=None` is the command that carries no prompt, and an operator
    types into the session once it is up. Two things differ, and both are this caller: the command
    is not wrapped for a role, because it execs in a terminal the operator already owns with the
    environment they already have; and a Codex head is not preflighted through `codex_preflight`
    beforehand, because the trust dialog the flags do not always answer is being put to somebody
    who is sitting in front of it. So this writes nothing to the runtime's own config.
    """
    reg = registry or head_registry.load_registry()
    profile = reg.profile(profile_id)
    try:
        return render_head_command(
            profile, prompt=None, workspace=workspace or os.getcwd()
        ).command
    except HeadCommandError as exc:
        raise SessionError(str(exc)) from None


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
