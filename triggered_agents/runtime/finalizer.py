"""Detached completion cleanup for ephemeral agent terminals.

The dispatcher owns terminal creation and the regular tick lifecycle. This module owns the
completion path started by an ephemeral terminal's launcher trailer, so that path can stay small
and independently readable without giving it a second lifecycle owner.
"""
from __future__ import annotations

import subprocess
import sys
import time

from . import role_env
from .state import AgentState


def _dispatch_module():
    # Import lazily: dispatch imports this module to expose the public compatibility entrypoints,
    # while cleanup needs dispatch's workspace and Orca primitives only when a helper actually runs.
    from . import dispatch
    return dispatch


def with_finalizer(agent: str, launch: str, generation: int) -> str:
    """Append a detached-cleanup trailer to an ephemeral terminal launch command.

    Plain `;` makes the helper run after either a successful or failed head exit. The generation
    identifies this terminal, so a late helper cannot stop a newer replacement.
    """
    finalizer = role_env.wrap_shell_command(
        agent, f"python3 -m triggered_agents {agent} dispatch --spawn-finalizer --generation {generation}")
    return f"{launch}; {finalizer}"


def spawn_finalizer(agent: str, generation: int | None = None) -> int:
    """Start completion cleanup outside the terminal that requested it."""
    dispatch = _dispatch_module()
    if not dispatch._is_ephemeral(agent):
        return 0
    state = AgentState(agent)
    command = [sys.executable, "-m", "triggered_agents", agent, "dispatch", "--finalize"]
    if generation is not None:
        command += ["--generation", str(generation)]
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        state.log_run("finalize", action="self-teardown-helper-failed", generation=generation,
                      error=str(exc))
        print(f"dispatch[{agent}]: could not start detached finalizer ({exc}); next tick will retry")
        return 1
    state.log_run("finalize", action="self-teardown-helper-started", generation=generation)
    return 0


def _finalize_locked(agent: str, ws: str, state: AgentState, generation: int | None) -> int:
    """Tear down a completed terminal while holding its lifecycle lock.

    A helper for an older generation only reaps its ghost tab. It must never issue the
    workspace-wide stop that would kill the newer generation.
    """
    dispatch = _dispatch_module()
    live_generation = state.load_terminal_generation()
    if generation is not None and live_generation is not None and live_generation != generation:
        reaped, ok = dispatch._reap_ghosts(ws)
        action = "self-teardown-superseded" if ok else "self-teardown-superseded-tab-failed"
        state.log_run("finalize", action=action, generation=generation)
        tail = "" if ok else " but a ghost tab would not close; next tick re-reaps"
        print(f"dispatch[{agent}]: finalize — terminal already superseded by a newer run"
              f"{f'; reaped {reaped} ghost(s)' if reaped else ''}{tail}")
        return 0
    if dispatch._stop_and_confirm_workspace_empty(ws):
        _, ok = dispatch._reap_ghosts(ws)
        state.save_terminal_handle(None)
        if ok:
            state.log_run("finalize", action="self-teardown", generation=generation)
            print(f"dispatch[{agent}]: finalize — self-torn-down after head exit")
        else:
            state.log_run("finalize", action="self-teardown-tab-failed", generation=generation)
            print(f"dispatch[{agent}]: finalize — stopped the pty but a ghost tab would not close; "
                  "next tick re-reaps")
    else:
        state.log_run("finalize", action="self-teardown-failed", generation=generation)
        print(f"dispatch[{agent}]: finalize — could not confirm self-teardown; next tick will retry")
    return 0


def finalize(agent: str, generation: int | None = None) -> int:
    """Run an ephemeral terminal's completion cleanup in a detached helper process.

    Lock contention gets a bounded retry. The generation guard in `_finalize_locked` makes a
    late helper safe after a newer terminal has taken over the workspace.
    """
    dispatch = _dispatch_module()
    if not dispatch._is_ephemeral(agent):
        return 0
    ws = dispatch._workspace(agent)
    state = AgentState(agent)
    for attempt in range(dispatch.FINALIZE_LOCK_ATTEMPTS):
        try:
            with state.lock():
                return _finalize_locked(agent, ws, state, generation)
        except SystemExit:
            if attempt + 1 >= dispatch.FINALIZE_LOCK_ATTEMPTS:
                state.log_run("finalize", action="self-teardown-deferred", generation=generation)
                print(f"dispatch[{agent}]: finalize deferred — a live tick holds the lock; it or "
                      "the next tick will tear this terminal down")
                return 0
            time.sleep(dispatch.FINALIZE_LOCK_RETRY_S)
    return 0
