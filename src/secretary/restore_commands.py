"""CLI handlers for bootstrap and restore."""

from __future__ import annotations

import argparse
from functools import wraps
from pathlib import Path

from secretary.cli_output import print_json
from secretary.config import validate_instance
from secretary.host import (
    LiveHostSource,
    build_expectations,
    build_plan,
    foreign_units,
    load_managed_manifest,
    plan_changes,
    plan_input_errors,
)
from secretary.host_apply import resolve_installed_packaged
from secretary.restore import (
    RestoreError,
    _target,
    bootstrap_empty,
    import_normalized_board,
    mark_reconcile_applied,
    plan_as_json,
    rebuild_memory_index,
    restore_backup,
    restore_state,
)


def add_restore_subcommands(subparsers) -> None:
    bootstrap = subparsers.add_parser(
        "bootstrap", help="bootstrap a host or create an empty secretary-data target"
    )
    bootstrap.add_argument("--empty", action="store_true", help="create an empty secretary-data target")
    bootstrap.add_argument("--instance", help="instance for --empty")
    bootstrap.add_argument("--instance-remote", help="private instance remote for host bootstrap")
    bootstrap.add_argument("--instance-dir", help="local instance checkout for host bootstrap")
    bootstrap.add_argument("--installation-user", help="dedicated OS account for host bootstrap")
    bootstrap.add_argument("--dry-run", action="store_true")
    bootstrap.set_defaults(handler=run_bootstrap)

    restore = subparsers.add_parser("restore", help="restore secretary-data from an archive")
    restore.add_argument("archive")
    restore.add_argument("--instance", required=True)
    restore.add_argument("--dry-run", action="store_true")
    restore.set_defaults(handler=run_restore)

    board = subparsers.add_parser("restore-board", help="import the normalized board into an empty backend")
    board.add_argument("--instance", required=True)
    board.set_defaults(handler=run_restore_board)

    live_board = subparsers.add_parser("board", help="repair the live Pipeline board schema")
    live_board_subcommands = live_board.add_subparsers(dest="board_command")
    migrate_assessment = live_board_subcommands.add_parser(
        "migrate-assessment",
        help="add the Assessment column to a Pipeline board that already holds cards",
    )
    migrate_assessment.add_argument("--instance", required=True)
    migrate_assessment.set_defaults(handler=run_board_migrate_assessment)
    live_board.set_defaults(handler=_board_subcommand_required)

    reconcile = subparsers.add_parser("restore-reconcile", help="verify live managed reconcile after restore")
    reconcile.add_argument("--instance", required=True)
    reconcile.set_defaults(handler=run_restore_reconcile)


def _restore_command(action: str):
    def decorate(command):
        @wraps(command)
        def run(args: argparse.Namespace) -> int:
            try:
                payload = command(args)
            except RestoreError as exc:
                _print_json({"ok": False, "action": action, "error": str(exc)})
                return 2
            if isinstance(payload, int):
                return payload
            _print_json(payload)
            return 0

        return run

    return decorate


@_restore_command("bootstrap")
def run_bootstrap(args: argparse.Namespace) -> int:
    if not args.empty:
        required = (args.instance_remote, args.instance_dir, args.installation_user)
        if not all(required):
            _print_json(
                {
                    "ok": False,
                    "action": "bootstrap",
                    "error": "host bootstrap requires --instance-remote, --instance-dir and --installation-user",
                }
            )
            return 2
        from secretary.bootstrap import bootstrap as bootstrap_host

        return bootstrap_host(args)
    if not args.instance:
        _print_json({"ok": False, "action": "bootstrap", "error": "--empty requires --instance"})
        return 2
    return plan_as_json(
        bootstrap_empty(Path(args.instance), dry_run=args.dry_run),
        action="bootstrap",
        dry_run=args.dry_run,
    )


@_restore_command("restore")
def run_restore(args: argparse.Namespace) -> int:
    return plan_as_json(
        restore_backup(Path(args.archive), Path(args.instance), dry_run=args.dry_run),
        action="restore",
        dry_run=args.dry_run,
    )


@_restore_command("restore-board")
def run_restore_board(args: argparse.Namespace) -> int:
    instance_path, data_dir, _ = _target(Path(args.instance))
    count = import_normalized_board(data_dir, instance=instance_path.parent)
    sprints = restore_state(data_dir).get("sprint_count", 0)
    return {"ok": True, "action": "restore-board", "cards": count, "sprints": sprints}


def _board_subcommand_required(args: argparse.Namespace) -> int:
    _print_json({"ok": False, "action": "board", "error": "board subcommand required"})
    return 2


@_restore_command("board migrate-assessment")
def run_board_migrate_assessment(args: argparse.Namespace) -> int:
    """Add the Assessment column to the board bound to ``--instance``."""
    from secretary.bootstrap import BootstrapError, migrate_assessment_column

    try:
        return migrate_assessment_column(Path(args.instance).expanduser())
    except BootstrapError as exc:
        raise RestoreError(str(exc)) from None


def run_restore_reconcile(args: argparse.Namespace) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        _print_json({"ok": False, "action": "restore-reconcile", "error": "invalid instance config"})
        return 2
    assert report.data_dir is not None
    packaged = resolve_installed_packaged(
        report.instance,
        instance_path=report.instance_path.parent,
        data_dir=report.data_dir,
    )
    if plan_input_errors(report.instance, report.bindings, packaged=packaged):
        _print_json({"ok": False, "action": "restore-reconcile", "error": "invalid desired state"})
        return 2
    expected = build_expectations(report.bindings, report.host)
    collected = LiveHostSource().collect(expected)
    if collected.errors:
        _print_json({"ok": False, "action": "restore-reconcile", "error": "host inventory unavailable"})
        return 2
    try:
        _, data_dir, _ = _target(Path(args.instance))
    except RestoreError as exc:
        _print_json({"ok": False, "action": "restore-reconcile", "error": str(exc)})
        return 2
    prefix = report.host.get("unit_prefix", "") if isinstance(report.host, dict) else ""
    managed, error = load_managed_manifest(data_dir / "host-managed.json")
    if error:
        _print_json({"ok": False, "action": "restore-reconcile", "error": error})
        return 2
    changes = plan_changes(
        build_plan(report.instance, report.bindings, packaged=packaged),
        collected.inventory,
        managed,
        prefix,
        foreign_units(report.host),
    )
    if not changes or any(change.action != "unchanged" for change in changes):
        _print_json(
            {"ok": False, "action": "restore-reconcile", "error": "managed reconcile has not been applied"}
        )
        return 1
    try:
        mark_reconcile_applied(data_dir)
    except RestoreError as exc:
        _print_json({"ok": False, "action": "restore-reconcile", "error": str(exc)})
        return 2
    _print_json({"ok": True, "action": "restore-reconcile"})
    return 0


@_restore_command("memory-reindex")
def run_memory_reindex(args: argparse.Namespace) -> int:
    instance_path, data_dir, _ = _target(Path(args.instance))
    report = validate_instance(instance_path)
    host = report.host if isinstance(report.host, dict) else {}
    python = host.get("memory_reindex_python")
    script = host.get("memory_reindex_script")
    model = host.get("memory_model", "intfloat/multilingual-e5-large")
    dim = host.get("memory_dim", 1024)
    threads = host.get("memory_threads", 1)
    count = rebuild_memory_index(
        data_dir,
        instance_path.parent,
        python=Path(python) if isinstance(python, str) else None,
        script=Path(script) if isinstance(script, str) else None,
        model=model if isinstance(model, str) else None,
        dim=dim if isinstance(dim, int) else None,
        threads=threads if isinstance(threads, int) else None,
    )
    return {"ok": True, "action": "memory-reindex", "facts": count}


def _print_json(payload: dict) -> None:
    print_json(payload)
