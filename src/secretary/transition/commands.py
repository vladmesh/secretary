"""`transition from-secretary --instance <dir> (--plan | --apply | --rollback)`.

Only the parser lives here; the transition itself is imported when the command runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .names import OLD


def add_transition_subcommands(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    group = subparsers.add_parser(
        "transition", help="one-shot installation transitions (docs/RENAME.md §T3)"
    )
    verbs = group.add_subparsers(dest="transition_command")
    command = verbs.add_parser(
        f"from-{OLD.package}",
        help=f"move this installation from {OLD.package} to its new name; --plan first",
    )
    command.add_argument("--instance", required=True, help="the instance repository (it keeps its name)")
    mode = command.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="print every step and precondition; write nothing")
    mode.add_argument("--apply", action="store_true", help="run, resuming at the first unfinished step")
    mode.add_argument("--rollback", action="store_true", help="undo what the journal says was done")
    command.add_argument("--through", default="", help="stop after this step (the bootstrap stops after 'move')")
    command.add_argument("--sprint", default="", help="the sprint the observer prepared (default: found by marker)")
    command.add_argument(
        "--allow-extra-merge",
        action="append",
        default=[],
        metavar="SHA",
        help="a merge between the checkout and origin/main besides the rename's, checked by hand",
    )
    command.add_argument("--home", default="", help=argparse.SUPPRESS)
    command.set_defaults(handler=run_transition)
    group.set_defaults(handler=lambda args: (group.print_help(), 2)[1])


def run_transition(args: argparse.Namespace) -> int:
    from . import engine, steps
    from .context import Context, Journal, Layout, Runner, TransitionError

    instance = Path(args.instance).expanduser()
    if instance.name == "instance.yaml":
        instance = instance.parent
    layout = Layout(home=Path(args.home).expanduser() if args.home else Path.home(), instance=instance.resolve())
    try:
        ctx = Context(
            layout=layout,
            runner=Runner(),
            journal=Journal.load(layout.journal_path),
            sprint=args.sprint,
            allow_extra_merges=tuple(args.allow_extra_merge),
            board=steps.BoardOps(),
        )
        if args.plan:
            return engine.plan(ctx)
        if args.rollback:
            return engine.run_rollback(ctx)
        return engine.apply(ctx, through=args.through)
    except TransitionError as exc:
        print(json.dumps({"error": {"code": "transition_refused", "message": str(exc)}}), file=sys.stderr)
        return 3


__all__ = ["add_transition_subcommands", "run_transition"]
