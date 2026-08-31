"""Secretary-owned composition root for the board-owning standing agents.

The live steward and retro gate paths enter here so every board read/write uses
Secretary's canonical task adapters.  The generic triggered-agent runtime
remains independent: it receives only its narrow structural ports.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from secretary.board.done_retention import DoneRetentionBoard
from secretary.board.steward_reports import StewardReportBoard, StewardSignalBoard
from secretary.config import instance_data_dir
from secretary.tasks import KanboardClient, TaskError, TaskReader, TaskWriter
from triggered_agents import __main__ as triggered_main
from triggered_agents.agents.retro import cli as retro_cli
from triggered_agents.agents.steward import cli as steward_cli
from triggered_agents.runtime import dispatch
from triggered_agents.runtime.kanboard import KanboardUnreachable
from triggered_agents.runtime.paths import default_instance_path

_SIGNAL_COMMANDS = frozenset({"scan", "precheck", "advance"})


def _instance_path() -> Path:
    """Resolve the same instance route as the public task commands."""
    return Path(os.environ.get("SECRETARY_INSTANCE") or default_instance_path()).expanduser()


def _data_dir(instance: Path) -> Path:
    """Use an explicit process override or the instance-owned canonical path."""
    configured = os.environ.get("SECRETARY_DATA_DIR")
    return Path(configured).expanduser() if configured else instance_data_dir(instance)


def _canonical_reader() -> TaskReader:
    """Build a reader only when a signal command actually reads the board."""
    try:
        return TaskReader(KanboardClient.for_instance(_instance_path()))
    except TaskError as exc:
        if exc.code == "backend_unavailable":
            # Precheck already distinguishes this historical "board is not
            # available yet" result from a broken deterministic helper.
            raise KanboardUnreachable(exc.message) from None
        raise


def _map_signal_error(error: TaskError) -> None:
    """Keep the steward precheck's historical deferred-board classification."""
    if error.code == "backend_unavailable":
        raise KanboardUnreachable(error.message) from None


def _signal_board() -> StewardSignalBoard:
    return StewardSignalBoard(reader_factory=_canonical_reader, error_mapper=_map_signal_error)


def _report_board() -> StewardReportBoard:
    def build() -> tuple[TaskReader, TaskWriter]:
        instance = _instance_path()
        client = KanboardClient.for_instance(instance)
        return TaskReader(client), TaskWriter(client, data_dir=_data_dir(instance))

    return StewardReportBoard(board_factory=build)


def _done_retention_board() -> DoneRetentionBoard:
    """Construct no config/client/audit state until retro actually cleans Done."""

    def build() -> tuple[TaskReader, TaskWriter]:
        instance = _instance_path()
        client = KanboardClient.for_instance(instance)
        return TaskReader(client), TaskWriter(client, data_dir=_data_dir(instance))

    return DoneRetentionBoard(board_factory=build, error_mapper=_map_signal_error)


def _steward(argv: list[str]) -> int:
    command = argv[0] if argv else "help"
    if command != "dispatch":
        reader = _signal_board() if command in _SIGNAL_COMMANDS else None
        return steward_cli.main(argv, reader=reader)

    parsed = triggered_main.parse_dispatch_arguments(argv[1:])
    # These are the legacy dispatch terminal-finalizer paths.  They must remain
    # free of config, board and audit construction; notably cleanup-only for a
    # non-ephemeral steward is a zero-side-effect early return in dispatch.run.
    if parsed.spawn_finalizer or parsed.finalize:
        return triggered_main.main(["steward", *argv])
    if parsed.cleanup_only:
        return dispatch.run("steward", parsed.variant, cleanup_only=True)
    return dispatch.run("steward", parsed.variant, report_board=_report_board())


def _retro(argv: list[str]) -> int:
    command = argv[0] if argv else "help"
    if command in {"precheck", "harvest"}:
        return retro_cli.main(argv, retention=_done_retention_board())
    return triggered_main.main(["retro", *argv])


def main(argv: list[str] | None = None) -> int:
    """Run ``<agent> <cmd> [args]`` through the alternative composition root."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return triggered_main.main(argv)
    if argv[0] == "retro":
        return _retro(argv[1:])
    if argv[0] != "steward":
        return triggered_main.main(argv)
    return _steward(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
