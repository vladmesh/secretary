"""triggered-agents CLI — dispatch to a registered agent's deterministic helpers.

Usage: python3 -m triggered_agents <agent> <cmd> [args]

Each triggered-agent (cron/event-driven headless run) shares this runtime: watermark,
lock, precheck, redaction. The per-agent judgment lives in that agent's Orca skill; the
`<cmd>` helpers here are the deterministic parts the agent drives via Bash.

Agents are modules under `triggered_agents.agents.<name>` exposing `cli.main(argv)`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import import_module

AGENTS = ("curator", "retro", "steward")
# Pipeline is a scheduled production-dispatcher component, rather than an
# agent command.  It remains in the cross-component health output even though
# its retired board CLI is no longer a dispatchable/public agent.
HEALTH_COMPONENTS = ("curator", "retro", "pipeline", "steward")


@dataclass(frozen=True)
class DispatchArguments:
    """The dispatch flags interpreted by both composition roots.

    This deliberately preserves the legacy loose argv interpretation: a value
    following ``--generation`` is still eligible to be the variant, because
    that is what the original one-pass selector did.
    """

    cleanup_only: bool
    finalize: bool
    spawn_finalizer: bool
    generation: int | None
    variant: str | None


def parse_dispatch_arguments(argv: list[str]) -> DispatchArguments:
    """Return the legacy dispatch interpretation without performing dispatch."""
    cleanup_only = "--cleanup-only" in argv
    finalize = "--finalize" in argv
    spawn_finalizer = "--spawn-finalizer" in argv
    generation = None
    if "--generation" in argv:
        index = argv.index("--generation")
        if index + 1 < len(argv):
            try:
                generation = int(argv[index + 1])
            except ValueError:
                generation = None
    return DispatchArguments(
        cleanup_only=cleanup_only,
        finalize=finalize,
        spawn_finalizer=spawn_finalizer,
        generation=generation,
        variant=next((arg for arg in argv if not arg.startswith("--")), None),
    )


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        print("agents:", ", ".join(AGENTS))
        return 0
    if argv[0] == "health":  # cross-agent, not a per-agent cmd
        from .runtime import health

        return health.check(HEALTH_COMPONENTS)
    agent, rest = argv[0], argv[1:]
    if agent not in AGENTS:
        print(f"triggered_agents: unknown agent {agent!r} (known: {', '.join(AGENTS)})", file=sys.stderr)
        return 2
    if rest and rest[0] == "dispatch":
        # Every dispatchable agent here is an LLM head driven by the generic singleton terminal
        # driver, which keeps one warm claude terminal per agent. Task dispatch itself is not one
        # of them: it lives in `secretary/dispatcher_production.py`, on its own timer.
        dispatch_args = rest[1:]
        parsed = parse_dispatch_arguments(dispatch_args)
        from .runtime import dispatch

        if parsed.spawn_finalizer:
            return dispatch.spawn_finalizer(agent, generation=parsed.generation)
        if parsed.finalize:
            # The head's trailer starts a detached helper with `--spawn-finalizer`; this is the
            # helper's cleanup entrypoint. It never dispatches a skill and needs its own lock
            # handling (see dispatch.finalize's docstring). `--generation` carries the terminal's
            # identity so it never stops a replacement a concurrent tick created.
            return dispatch.finalize(agent, generation=parsed.generation)
        # An optional variant name (e.g. the steward's "deep-sweep", triggered-agents-254)
        # selects a second, differently-scheduled mode of the same agent — see automation.toml's
        # [variants.<name>] table and dispatch.run's docstring. `--cleanup-only` (triggered-
        # agents-445) is ta-gate.sh's call on a precheck skip: no variant, no dispatch, just let
        # an ephemeral agent's finished/stuck terminal get torn down instead of waiting for a
        # tick that has real work.
        return dispatch.run(agent, parsed.variant, cleanup_only=parsed.cleanup_only)
    cli = import_module(f"triggered_agents.agents.{agent}.cli")
    return cli.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
