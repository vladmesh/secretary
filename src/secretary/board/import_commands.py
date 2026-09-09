"""``secretary board import``: the CLI face of :mod:`secretary.board.import_board`.

Two things this command deliberately does not do.  It never creates
``<instance>/board-store.env`` and never stands a PostgreSQL up: a real run is given the store it
should write to with ``--dsn``, which is what the throwaway-container proof uses, and an
installation that has been configured resolves through ``board_store`` like everything else.  And
its default is ``--dry-run``: reading a whole board and reporting on it is the safe half, so it is
the half you get without asking.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from secretary.board.import_board import BoardImportError, render, run
from secretary.onboarding import DEFAULT_INSTANCE


def add_board_store_subcommands(board_subcommands) -> None:
    """Add the store's commands to the existing ``secretary board`` group."""
    importer = board_subcommands.add_parser(
        "import", help="import today's Kanboard board into the board store's schema"
    )
    importer.add_argument(
        "--instance",
        default=DEFAULT_INSTANCE,
        help="instance directory; its board transport and project registry are read",
    )
    importer.add_argument(
        "--data-dir",
        help="secretary-data; strictly read the complete audit journal, budget timestamps and transactions",
    )
    importer.add_argument(
        "--dsn",
        help="SQLAlchemy URL of an already-migrated board store; omit for --dry-run",
    )
    importer.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="read everything, write nothing, and produce the same report (the default)",
    )
    importer.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="write the plan into the store named by --dsn",
    )
    importer.add_argument(
        "--report",
        help="write the machine-readable report here, and its human summary beside it as .txt",
    )
    importer.add_argument("--json", action="store_true", help="print the report as JSON")
    importer.set_defaults(handler=run_board_import)


def run_board_import(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.dsn:
        print("secretary board import: --apply needs a --dsn naming a migrated board store")
        return 2
    try:
        report = run(
            Path(args.instance).expanduser(),
            data_dir=args.data_dir,
            dsn=args.dsn,
            dry_run=args.dry_run,
            report_path=args.report,
        )
    except BoardImportError as exc:
        print(f"secretary board import: {exc}")
        return 1
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True) if args.json else render(report), end="")
    return 0 if report.parity.get("ok") else 1


__all__ = ["add_board_store_subcommands", "run_board_import"]
