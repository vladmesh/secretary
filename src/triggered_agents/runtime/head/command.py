"""The one place a head's shell command is built: profile plus prompt input, out comes a command.

What this module owns and what it deliberately does not:

  * **it owns the adapter shapes.** What a `claude`, a `codex` or a `hermes` invocation looks
    like, which efforts each accepts, and which of them carry their prompt on the command line;
  * **it owns the role-env wrapper**, because a head's command is not the adapter's argv — it is
    that argv under the role environment its launcher binds;
  * **it does not own the registry.** `[profiles.*]`, `heads.yaml`, `load_registry` and the
    fallback chains stay in `agents.pipeline.heads`; a profile arrives here as a mapping. This
    package is imported by the registry, never the reverse, which is what keeps a head operation
    runnable without the pipeline package;
  * **it does not open a pane.** A rendered command is a string; `spawn` is what runs it.

`prompt` is the whole of the launch-shape decision. A prompt given is a prompt on the command
line for the adapters that can carry one; `prompt=None` renders the interactive shape, where the
caller delivers the prompt into the live pane afterwards and `prompt_after_start` says so. A
Codex head has only the interactive shape, so it ignores a prompt either way.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .. import role_env
from ..codex_preflight import codex_home, codex_trust_paths

# Valid backend names; this renderer validates the profile's choice.
from ..head_runtimes import DEFAULT_HEAD_RUNTIME, HEAD_RUNTIMES
from ..launch_prefix import pythonpath_prefix

# Efforts each adapter accepts and their command-line spelling.
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

CLAUDE_EFFORTS = {"default", "low", "medium", "high", "xhigh", "max"}

# Interactive adapters receive prompts through their live pane.
PROMPT_AFTER_START_ADAPTERS = {"codex"}

# Codex heads are TUI sessions; unsupported modes are refused.
CODEX_TUI_MODE = "tui"
CODEX_LAUNCH_MODES = {CODEX_TUI_MODE}

PYTHON_SAFE_PATH_FLAG = "-P"

# The launcher chooses this binding: each entry point resolves a different PYTHONPATH.
SECRETARY_ROLE_ENV = "secretary.role_env"
RUNTIME_ROLE_ENV = "triggered_agents.runtime.role_env"
ROLE_ENV_ENTRY_POINTS = (SECRETARY_ROLE_ENV, RUNTIME_ROLE_ENV)


class HeadCommandError(RuntimeError):
    """A head whose command cannot be rendered: unknown adapter, unspellable effort, missing input."""


@dataclass(frozen=True)
class HeadCommand:
    """One head's launch command, whether its prompt still has to be delivered, and by what."""

    command: str
    prompt_after_start: bool = False
    adapter: str = ""


def validate_launch_shape(profile_id: str, profile: Mapping[str, Any]) -> None:
    """Whether one profile describes a launch shape this module can actually render.

    Both readers of a registry run through this one — `validate_registry` for the whole table and
    `HeadSpec.from_profile` for a single profile — so a head refused at load time and a head refused
    at bring-up are refused by the same rule. Only the launch shape: whether the resource a profile
    names exists, and whether its fallback chain points anywhere, stay with the registry.

    `runtime` is part of the launch shape and is checked here for the same reason: the name a
    profile gives its backend has to be refused when the table is read, not when the head is
    raised. It is checked independently of the adapter, because the two are orthogonal — any of
    `HEAD_RUNTIMES` may hold any of the adapters — and an absent one is `DEFAULT_HEAD_RUNTIME`.
    """
    adapter = _named(profile.get("adapter"), f"profile {profile_id!r} adapter")
    if adapter not in _ADAPTERS:
        raise HeadCommandError(
            f"profile {profile_id!r} has unknown adapter {adapter!r} (known: {', '.join(sorted(_ADAPTERS))})"
        )
    if adapter == "codex":
        effort = _named(profile.get("effort", "default"), f"profile {profile_id!r} effort")
        if effort not in CODEX_EFFORTS:
            known = ", ".join(sorted(CODEX_EFFORTS))
            raise HeadCommandError(
                f"profile {profile_id!r} has unknown codex effort {effort!r} (known: {known})"
            )
        mode = _named(profile.get("codex_mode", CODEX_TUI_MODE), f"profile {profile_id!r} codex launch mode")
        if mode not in CODEX_LAUNCH_MODES:
            known = ", ".join(sorted(CODEX_LAUNCH_MODES))
            raise HeadCommandError(
                f"profile {profile_id!r} has unknown codex launch mode {mode!r} (known: {known})"
            )
    if adapter == "claude":
        effort = _named(profile.get("effort", "default"), f"profile {profile_id!r} effort")
        if effort not in CLAUDE_EFFORTS:
            known = ", ".join(sorted(CLAUDE_EFFORTS))
            raise HeadCommandError(
                f"profile {profile_id!r} has unknown claude effort {effort!r} (known: {known})"
            )
    # Backend validity is independent of the CLI adapter.
    runtime = _named(profile.get("runtime", DEFAULT_HEAD_RUNTIME), f"profile {profile_id!r} runtime")
    if runtime not in HEAD_RUNTIMES:
        known = ", ".join(HEAD_RUNTIMES)
        raise HeadCommandError(f"profile {profile_id!r} has unknown runtime {runtime!r} (known: {known})")


def _named(value: object, what: str) -> str:
    """A profile field that has to be a plain name before anything can be looked up by it.

    Checked before the membership tests above rather than left to them: a list where a name belongs
    is unhashable, so `value not in table` would raise TypeError past every caller.
    """
    if not isinstance(value, str):
        raise HeadCommandError(f"{what} must be a name, got {type(value).__name__}")
    return value


def render_head_command(
    profile: Mapping[str, Any],
    *,
    prompt: str | None = None,
    workspace: str = "",
    role: str = "",
    identity: Mapping[str, str] | None = None,
    binding: str = SECRETARY_ROLE_ENV,
) -> HeadCommand:
    """The shell command that brings one head up, and how its prompt reaches it.

    `role` is what the command is wrapped for. An empty role renders the adapter command bare, for
    the one caller that is not launching a head into a pane at all: `secretary shell`. `workspace` is
    what a Codex head's directory-trust override names and is required for one. `identity` is a
    head's own binding and only the secretary entry point renders it.
    """
    adapter = str(profile.get("adapter") or "")
    render = _ADAPTERS.get(adapter)
    if render is None:
        known = ", ".join(sorted(_ADAPTERS))
        raise HeadCommandError(f"head has unknown adapter {adapter!r} (known: {known})")
    command = render(profile, prompt=prompt, workspace=workspace)
    if role:
        command = wrap_role_command(role, command, identity=identity, binding=binding)
    elif identity:
        raise HeadCommandError("an unwrapped head command carries no identity")
    return HeadCommand(
        command,
        prompt_after_start=prompt is None or adapter in PROMPT_AFTER_START_ADAPTERS,
        adapter=adapter,
    )


def wrap_role_command(
    role: str,
    command: str,
    *,
    identity: Mapping[str, str] | None = None,
    binding: str = SECRETARY_ROLE_ENV,
) -> str:
    """Render one head's command under the role environment its launcher binds.

    The installation binding is written into the command itself because a head does not start as a
    child of its launcher: Orca creates the terminal, so nothing the launcher's unit exported is
    guaranteed to be in the environment `role_env exec` then runs in. Without it, a dispatcher
    rendered for a non-default instance launches heads that read the home default's `runtime.env`.

    `identity` is rendered beside that binding rather than left to `runtime.env`. Only names the
    role's allowlist knows are rendered; anything else is refused here instead of silently ignored.
    """
    if binding not in ROLE_ENV_ENTRY_POINTS:
        known = ", ".join(ROLE_ENV_ENTRY_POINTS)
        raise HeadCommandError(f"unknown role env entry point {binding!r} (known: {known})")
    if binding == RUNTIME_ROLE_ENV:
        if identity:
            raise HeadCommandError(
                f"the {RUNTIME_ROLE_ENV} entry point renders no identity for role {role!r}"
            )
        return role_env.wrap_shell_command(role, command)
    unknown = sorted(set(identity or {}) - set(role_env.ROLE_ALLOWLIST.get(role, ())))
    if unknown:
        raise HeadCommandError(f"role {role!r} carries no binding named {', '.join(unknown)}")
    rendered = [f"{name}={shlex.quote(value)}" for name, value in sorted((identity or {}).items())]
    prefix = " ".join([*role_env.launch_binding(), *rendered])
    command = role_env.role_shell_command(role, command)
    return (
        f"{prefix} {pythonpath_prefix(os.environ)} python3 {PYTHON_SAFE_PATH_FLAG} "
        f"-m {SECRETARY_ROLE_ENV} exec --role {shlex.quote(role)} -- /bin/sh -lc "
        f"{shlex.quote(command)}"
    )


def with_pid_heartbeat(command: str, pid_file: str, *, identity: Mapping[str, str] | None = None) -> str:
    """Prefix a head command with an atomic versioned launch-identity heartbeat.

    `$$` inside a shell always names that shell's own pid, and the trailing `exec` replaces the
    shell's process image with the head instead of forking it, so the pid written here stays the
    head's own for its whole life. The two statements before `;` force a real shell to run first,
    which is what makes `$$` mean anything. Orca keeps the pane's wrapping shell around once the head
    exits, but that shell is no longer this pid.

    A wrapped head command starts with a leading `NAME=value` assignment, and POSIX `exec` treats the
    word right after it as the program to run, so `exec PYTHONPATH=... python3` fails. Routing the
    whole command through `env` keeps `exec` a single-word invocation while `env` applies the leading
    assignments before it execs the real program in place, so the captured pid still belongs to the
    head.
    """
    # Keep the terminal process group for TTY semantics and safe group signalling.
    # Write the PID identity before exec and replace its record atomically.
    writer = """import json
import os
import sys
import tempfile
path, pid, identity = sys.argv[1:]
stat = open(f'/proc/{pid}/stat', encoding='utf-8').read()
close = stat.rfind(')')
fields = stat[close + 2:].split()
if close < 0 or len(fields) <= 19:
    raise RuntimeError('process stat has no start time')
record = json.loads(identity)
record.update({'version': 1, 'pid': int(pid),
               'boot_id': open('/proc/sys/kernel/random/boot_id', encoding='utf-8').read().strip(),
               'proc_starttime_ticks': fields[19]})
def bind_leaf(record):
    try:
        handoff = json.load(open(path + '.leaf', encoding='utf-8'))
        expected = handoff.get('expected')
        leaf = handoff.get('leaf')
        if (isinstance(expected, dict) and isinstance(leaf, str)
                and all(str(record.get(name) or '') == str(expected.get(name) or '')
                        and str(expected.get(name) or '')
                        for name in ('run_id', 'role', 'task'))):
            record['leaf'] = leaf
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
directory = os.path.dirname(path) or '.'
os.makedirs(directory, mode=0o700, exist_ok=True)
def publish(payload):
    fd, temporary = tempfile.mkstemp(prefix='.secretary-heartbeat-', dir=directory)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, sort_keys=True, separators=(',', ':'))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
bind_leaf(record)
publish(record)
# The terminal reply can race between the first handoff read and this base replace.  Check once
# more after publishing so the durable record eventually carries the returned leaf in either order.
before = record.get('leaf')
bind_leaf(record)
if record.get('leaf') != before:
    publish(record)"""
    encoded_identity = json.dumps(dict(identity or {}), sort_keys=True, separators=(",", ":"))
    return (
        f'python3 -P -c {shlex.quote(writer)} {shlex.quote(pid_file)} "$$" '
        f"{shlex.quote(encoded_identity)}; exec env {command}"
    )


def _render_claude(profile: Mapping[str, Any], *, prompt: str | None, workspace: str) -> str:
    del workspace
    args = ["claude", "--dangerously-skip-permissions"]
    model = profile.get("model")
    if model:
        args += ["--model", str(model)]
    effort = str(profile.get("effort") or "default")
    if effort not in CLAUDE_EFFORTS:
        known = ", ".join(sorted(CLAUDE_EFFORTS))
        raise HeadCommandError(f"claude profile has unknown effort {effort!r} (known: {known})")
    if effort != "default":
        args += ["--effort", effort]
    command = shlex.join(args)
    return command if prompt is None else f"{command} {prompt!r}"


def _render_hermes(profile: Mapping[str, Any], *, prompt: str | None, workspace: str) -> str:
    """Hermes' one-shot-seeded-session equivalent of `claude --dangerously-skip-permissions
    <prompt>`: `-z` seeds an autonomous session with the initial message (not `-q`/`chat`'s
    single-turn query mode), `--yolo` is Hermes' skip-permissions, `--cli` forces the plain REPL
    (no TUI) so it behaves in an Orca terminal the same way the classic `claude` invocation does.
    Without a prompt there is no session to seed, so the seed is simply absent and the REPL comes
    up empty — the shape `secretary shell` opens for an operator."""
    del workspace
    parts = ["hermes"]
    if prompt is not None:
        parts += ["-z", repr(prompt)]
    if profile.get("model"):
        parts += ["-m", str(profile["model"])]
    if profile.get("provider"):
        parts += ["--provider", str(profile["provider"])]
    parts += ["--yolo", "--cli"]
    return " ".join(parts)


def _render_codex_tui(profile: Mapping[str, Any], *, prompt: str | None, workspace: str) -> str:
    """The command that brings one Codex head up. There is one shape and it is interactive.

    Nothing selects it: no profile field, no card, no caller argument. `prompt` is accepted and never
    used — the caller delivers it into the live pane once Orca reports the TUI idle.

    `--skip-git-repo-check` is an `exec`-only flag in Codex 0.143; the top-level TUI rejects it, and
    pipeline workspaces are git worktrees already.

    The trust overrides state the intent on the command line, for the provisioned worktree and, for a
    linked worktree, the same repository root Codex derives from the common git dir. They do not on
    their own answer the dialog: Codex 0.145 still shows it with them in place, which is why the only
    thing that gets a pane past it is the `codex_preflight` write into the CODEX_HOME this command
    names, before the pane is created. The paths come from that same preflight.
    """
    del prompt
    if not workspace:
        raise HeadCommandError("codex TUI launch requires workspace for directory trust override")
    # Best-effort preference, never provider capability evidence.
    args = [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--enable",
        "multi_agent_v2",
        "-c",
        "features.multi_agent_v2.wait_agent_enabled=false",
        "-c",
        'mcp_servers.memory.bearer_token_env_var="SECRETARY_MEMORY_ACCESS_TOKEN"',
    ]
    model = profile.get("model")
    if model:
        args += ["-m", str(model)]
    effort_name = str(profile.get("effort") or "default")
    if effort_name not in CODEX_EFFORTS:
        known = ", ".join(sorted(CODEX_EFFORTS))
        raise HeadCommandError(f"codex profile has unknown effort {effort_name!r} (known: {known})")
    effort = CODEX_EFFORTS[effort_name]
    if effort:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    for path in codex_trust_paths(workspace):
        # TUI trust comes from preflight's config.toml, not these command overrides.
        args += ["-c", f'projects.{json.dumps(path)}.trust_level="trusted"']
    return f"CODEX_HOME={shlex.quote(codex_home(profile))} {shlex.join(args)}"


_ADAPTERS = {
    "claude": _render_claude,
    "hermes": _render_hermes,
    "codex": _render_codex_tui,
}
