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
        # Every dispatchable agent here is an LLM head driven by the generic singleton terminal
        # driver, which keeps one warm claude terminal per agent. Task dispatch itself is not one
        # of them: it lives in `secretary/dispatcher_production.py`, on its own timer.
        if agent == "pipeline":
            # `pipeline` is a board CLI, not a head: its automation.toml ships no skill, so there
            # is nothing to dispatch. Refuse instead of falling through into the terminal driver,
            # which would fail on the missing skill.
            print(
                "triggered_agents: pipeline has no dispatch — it is the board CLI only "
                "(task dispatch lives in secretary/dispatcher_production.py)",
                file=sys.stderr,
            )
            return 2
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
