"""CLI handlers for bootstrap and restore."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from secretary.restore import (
    RestoreError,
    _target,
    bootstrap_empty,
    import_normalized_board,
    mark_reconcile_applied,
    plan_as_json,
    rebuild_memory_index,
    restore_backup,
)
from secretary.host import (
    LiveHostSource,
    build_expectations,
    build_plan,
    load_managed_manifest,
    plan_changes,
    plan_input_errors,
)
from secretary.config import validate_instance


def add_restore_subcommands(subparsers) -> None:
    bootstrap = subparsers.add_parser("bootstrap", help="create a new empty secretary-data target")
    bootstrap.add_argument("--empty", action="store_true", required=True)
    bootstrap.add_argument("--instance", required=True)
    bootstrap.add_argument("--dry-run", action="store_true")
    bootstrap.set_defaults(handler=run_bootstrap_empty)

    restore = subparsers.add_parser("restore", help="restore secretary-data from an encrypted archive")
    restore.add_argument("archive")
    restore.add_argument("--instance", required=True)
    restore.add_argument("--age-identity")
    restore.add_argument("--dry-run", action="store_true")
    restore.set_defaults(handler=run_restore)

    board = subparsers.add_parser("restore-board", help="import the normalized board into an empty backend")
    board.add_argument("--instance", required=True)
    board.set_defaults(handler=run_restore_board)

    reconcile = subparsers.add_parser("restore-reconcile", help="verify live managed reconcile after restore")
    reconcile.add_argument("--instance", required=True)
    reconcile.set_defaults(handler=run_restore_reconcile)

def run_bootstrap_empty(args: argparse.Namespace) -> int:
    try:
        plan = bootstrap_empty(Path(args.instance), dry_run=args.dry_run)
    except RestoreError as exc:
        _print_json({"ok": False, "action": "bootstrap", "error": str(exc)})
        return 2
    _print_json(plan_as_json(plan, action="bootstrap", dry_run=args.dry_run))
    return 0


def run_restore(args: argparse.Namespace) -> int:
    identity = args.age_identity or os.environ.get("SECRETARY_AGE_IDENTITY")
    try:
        plan = restore_backup(
            Path(args.archive),
            Path(args.instance),
            age_identity=Path(identity) if identity else None,
            dry_run=args.dry_run,
        )
    except RestoreError as exc:
        _print_json({"ok": False, "action": "restore", "error": str(exc)})
        return 2
    _print_json(plan_as_json(plan, action="restore", dry_run=args.dry_run))
    return 0


def run_restore_board(args: argparse.Namespace) -> int:
    try:
        _, data_dir, _ = _target(Path(args.instance))
        count = import_normalized_board(data_dir)
    except RestoreError as exc:
        _print_json({"ok": False, "action": "restore-board", "error": str(exc)})
        return 2
    _print_json({"ok": True, "action": "restore-board", "cards": count})
    return 0


def run_restore_reconcile(args: argparse.Namespace) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        _print_json({"ok": False, "action": "restore-reconcile", "error": "invalid instance config"})
        return 2
    if plan_input_errors(report.instance, report.bindings):
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
    changes = plan_changes(build_plan(report.instance, report.bindings), collected.inventory, load_managed_manifest(data_dir / "host-managed.json"), prefix)
    if not changes or any(change.action != "unchanged" for change in changes):
        _print_json({"ok": False, "action": "restore-reconcile", "error": "managed reconcile has not been applied"})
        return 1
    try:
        mark_reconcile_applied(data_dir)
    except RestoreError as exc:
        _print_json({"ok": False, "action": "restore-reconcile", "error": str(exc)})
        return 2
    _print_json({"ok": True, "action": "restore-reconcile"})
    return 0


def run_memory_reindex(args: argparse.Namespace) -> int:
    try:
        instance_path, data_dir, _ = _target(Path(args.instance))
        report = validate_instance(instance_path)
        host = report.host if isinstance(report.host, dict) else {}
        python = host.get("memory_reindex_python")
        script = host.get("memory_reindex_script")
        model = host.get("memory_model")
        dim = host.get("memory_dim")
        count = rebuild_memory_index(
            data_dir,
            python=Path(python) if isinstance(python, str) else None,
            script=Path(script) if isinstance(script, str) else None,
            model=model if isinstance(model, str) else None,
            dim=dim if isinstance(dim, int) else None,
        )
    except RestoreError as exc:
        _print_json({"ok": False, "action": "memory-reindex", "error": str(exc)})
        return 2
    _print_json({"ok": True, "action": "memory-reindex", "facts": count})
    return 0


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
