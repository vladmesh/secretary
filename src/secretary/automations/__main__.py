"""Background-agents CLI — dispatch to a registered agent's deterministic helpers.

Usage: python3 -P -m secretary automations <agent> <cmd> [args]

Each triggered-agent (cron/event-driven headless run) shares this runtime: watermark,
lock, precheck, redaction. The per-agent judgment lives in that agent's Orca skill; the
`<cmd>` helpers here are the deterministic parts the agent drives via Bash.

Agents are modules under `secretary.automations.agents.<name>` exposing `cli.main(argv)`.
`secretary automations` enters through `secretary.automations.composition`, which injects the
board ports steward and retro need from `secretary` and hands everything else to `main` here.
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
    """The dispatch flags interpreted by both this runner and the composition root.

    The variant is the first argument that is not a flag.
    """

    cleanup_only: bool
    variant: str | None


def parse_dispatch_arguments(argv: list[str]) -> DispatchArguments:
    """Return the legacy dispatch interpretation without performing dispatch."""
    return DispatchArguments(
        cleanup_only="--cleanup-only" in argv,
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
        print(f"secretary automations: unknown agent {agent!r} (known: {', '.join(AGENTS)})", file=sys.stderr)
        return 2
    if rest and rest[0] == "dispatch":
        # Every dispatchable agent here is an LLM head driven by the generic singleton head
        # driver, which raises one supervised head per tick. Task dispatch itself is not one of
        # them: it lives in `secretary/dispatch/production.py`, on its own timer.
        parsed = parse_dispatch_arguments(rest[1:])
        from .runtime import dispatch

        # An optional variant name (e.g. the steward's "deep-sweep", triggered-agents-254)
        # selects a second, differently-scheduled mode of the same agent — see automation.toml's
        # [variants.<name>] table and dispatch.run's docstring. `--cleanup-only` is the gate's call
        # on a precheck skip, and a no-op exit 0 (see dispatch.run).
        return dispatch.run(agent, parsed.variant, cleanup_only=parsed.cleanup_only)
    cli = import_module(f"secretary.automations.agents.{agent}.cli")
    return cli.main(rest)


if __name__ == "__main__":
    from secretary.automations.composition import main as composed_main

    raise SystemExit(composed_main())
