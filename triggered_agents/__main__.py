"""triggered-agents CLI — dispatch to a registered agent's deterministic helpers.

Usage: python3 -m triggered_agents <agent> <cmd> [args]

Each triggered-agent (cron/event-driven headless run) shares this runtime: watermark,
lock, precheck, redaction. The per-agent judgment lives in that agent's Orca skill; the
`<cmd>` helpers here are the deterministic parts the agent drives via Bash.

Agents are modules under `triggered_agents.agents.<name>` exposing `cli.main(argv)`.
"""
from __future__ import annotations

import sys
from importlib import import_module

AGENTS = ("curator", "retro", "pipeline", "steward")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        print("agents:", ", ".join(AGENTS))
        return 0
    if argv[0] == "health":  # cross-agent, not a per-agent cmd
        from .runtime import health
        return health.check(AGENTS)
    agent, rest = argv[0], argv[1:]
    if agent not in AGENTS:
        print(f"triggered_agents: unknown agent {agent!r} (known: {', '.join(AGENTS)})", file=sys.stderr)
        return 2
    if rest and rest[0] == "dispatch":
        # pipeline is the deterministic task dispatcher (no LLM head); everyone else uses the
        # generic singleton terminal driver that keeps one warm claude terminal per agent.
        dispatch_args = rest[1:]
        cleanup_only = "--cleanup-only" in dispatch_args
        finalize = "--finalize" in dispatch_args
        spawn_finalizer = "--spawn-finalizer" in dispatch_args
        generation = None
        if "--generation" in dispatch_args:
            gi = dispatch_args.index("--generation")
            if gi + 1 < len(dispatch_args):
                try:
                    generation = int(dispatch_args[gi + 1])
                except ValueError:
                    generation = None
        if agent == "pipeline":
            # ta-gate.sh (triggered-agents-445) now sends `--cleanup-only` to EVERY agent on a
            # precheck skip, pipeline included. The dispatcher has no terminal/PTY lifecycle at
            # all -- `--cleanup-only` here must be the exact no-op a plain skip always was, NOT a
            # full reconcile/advance/validate/claim tick (PR #95 review B1: this special case
            # ignored the flag entirely and ran dispatcher.tick() regardless). `--finalize` is
            # never appended to a pipeline launch command (dispatch.py only does that for an
            # ephemeral singleton-terminal agent, which pipeline isn't), but treat it the same
            # defensive way if it somehow arrives.
            if cleanup_only or finalize or spawn_finalizer:
                return 0
            from .agents.pipeline import dispatcher
            return dispatcher.tick()
        from .runtime import dispatch
        if spawn_finalizer:
            return dispatch.spawn_finalizer(agent, generation=generation)
        if finalize:
            # The head's trailer starts a detached helper with `--spawn-finalizer`; this is the
            # helper's cleanup entrypoint. It never dispatches a skill and needs its own lock
            # handling (see dispatch.finalize's docstring). `--generation` carries the terminal's
            # identity so it never stops a replacement a concurrent tick created.
            return dispatch.finalize(agent, generation=generation)
        # An optional variant name (e.g. the steward's "deep-sweep", triggered-agents-254)
        # selects a second, differently-scheduled mode of the same agent — see automation.toml's
        # [variants.<name>] table and dispatch.run's docstring. `--cleanup-only` (triggered-
        # agents-445) is ta-gate.sh's call on a precheck skip: no variant, no dispatch, just let
        # an ephemeral agent's finished/stuck terminal get torn down instead of waiting for a
        # tick that has real work.
        variant = next((a for a in dispatch_args if not a.startswith("--")), None)
        return dispatch.run(agent, variant, cleanup_only=cleanup_only)
    cli = import_module(f"triggered_agents.agents.{agent}.cli")
    return cli.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
