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
from pathlib import Path

from secretary.memory import access as memory_access
from secretary.runtime import heads as head_registry
from secretary.runtime.codex_home import bound_data_dir
from secretary.runtime.head import (
    HeadCommandError,
    HeadRun,
    HeadSpec,
    TaskRef,
    new_run_id,
    render_head_command,
    with_pid_heartbeat,
)
from secretary.runtime.role_env import load_env_file

# The operator names a head the way a human thinks about it ("claude", "codex", "hermes"): a bare
# adapter name is an adapter choice, never a profile id, and picks that adapter's head from the
# installation's registry. Any real heads.toml profile id is also accepted verbatim, so
# `--head claude-opus-high` or `--head codex-sol-medium` work too. With no `--head`, the shell opens
# on the registry's `role_defaults.new_card`.
SHELL_ADAPTERS = ("claude", "codex", "hermes")
DEFAULT_HEAD_ROLE = "new_card"


class SessionError(RuntimeError):
    pass


def operator_env(
    env_file: str | os.PathLike[str] | None = None,
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Full runtime env for the operator: base process env overlaid with the entire runtime.env.

    No allowlist and no sensitive-name scrubbing — that is the point.
    """
    base = dict(os.environ if base_env is None else base_env)
    source = load_env_file(env_file)
    env = {**base, **source}
    env["SECRETARY_ROLE"] = "operator"
    return env


def resolve_profile_id(head: str | None, *, registry: head_registry.Registry | None = None) -> str:
    """Resolve a user-supplied head name to a real heads.toml profile id."""
    reg = registry or head_registry.load_registry()
    if not head:
        head = head_registry.required_role_default(reg.role_defaults, DEFAULT_HEAD_ROLE)
    elif head in SHELL_ADAPTERS:
        return adapter_profile(head, reg)
    # An id the registry does not define — one the installation has retired included — fails by
    # name with the known ids rather than being routed to a look-alike.
    return reg.resolve(head)


def adapter_profile(adapter: str, registry: head_registry.Registry) -> str:
    """The profile a bare adapter name opens: the first role default on that adapter, else the
    first profile on it in id order."""
    routed = [str(head) for head in registry.role_defaults.values() if isinstance(head, str)]
    candidates = [*routed, *registry.known()]
    for pid in candidates:
        profile = registry.profiles.get(pid)
        if isinstance(profile, dict) and profile.get("adapter") == adapter:
            return pid
    raise head_registry.HeadRegistryError(
        f"no {adapter} head in the registry (known: {', '.join(registry.known()) or '(none)'})"
    )


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
        return render_head_command(profile, prompt=None, workspace=workspace or os.getcwd()).command
    except HeadCommandError as exc:
        raise SessionError(str(exc)) from None


def run_shell(args: argparse.Namespace) -> int:
    # A Codex shell renders its CODEX_HOME against the selected installation's data dir.
    with bound_data_dir():
        return _run_shell(args)


def _run_shell(args: argparse.Namespace) -> int:
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
    try:
        registry = head_registry.load_registry()
        run_id = new_run_id()
        data_dir = _memory_data_dir(args.env_file, env)
        pid_dir = memory_access.bindings_dir(data_dir) / "heads"
        pid_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        run = HeadRun(
            run_id=run_id,
            spec=HeadSpec.from_profile(profile_id, registry.profile(profile_id)),
            workspace=args.workspace or os.getcwd(),
            task_ref=TaskRef.standing("interactive"),
            role="po",
            pid_file=str(pid_dir / f"{run_id}.pid"),
        )
        grant = memory_access.issue_grant(run, memory_access.interactive_po_subject(), data_dir=data_dir)
        env[memory_access.MEMORY_ACCESS_TOKEN_ENV] = grant.token
        command = with_pid_heartbeat(
            command,
            run.pid_file,
            identity={"run_id": run.run_id, "role": run.role, "task": "standing:interactive"},
        )
    except (MemoryError, OSError, ValueError, memory_access.MemoryAccessError) as exc:
        print(f"secretary shell: memory access binding could not be issued: {exc}", file=sys.stderr)
        return 2
    argv = ["/bin/sh", "-c", command]
    try:
        os.execvpe(argv[0], argv, env)
    except OSError as exc:
        print(f"secretary shell: exec {command!r} failed: {exc}", file=sys.stderr)
        return 126
    return 0  # unreachable after a successful execvpe


def _memory_data_dir(env_file: str | os.PathLike[str] | None, env: dict[str, str]) -> Path | None:
    """Use the selected installation's data plane without making runtime.env authority."""
    configured = env.get("SECRETARY_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    instance = env.get("SECRETARY_INSTANCE")
    if instance:
        from secretary.config import instance_data_dir

        return instance_data_dir(Path(instance))
    if env_file:
        from secretary.config import instance_data_dir

        return instance_data_dir(Path(env_file).expanduser().parent)
    return None
