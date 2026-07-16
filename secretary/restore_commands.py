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
    plan_as_json,
    rebuild_memory_index,
    restore_backup,
)


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


def run_memory_reindex(args: argparse.Namespace) -> int:
    try:
        _, data_dir, _ = _target(Path(args.instance))
        count = rebuild_memory_index(data_dir)
    except RestoreError as exc:
        _print_json({"ok": False, "action": "memory-reindex", "error": str(exc)})
        return 2
    _print_json({"ok": True, "action": "memory-reindex", "facts": count})
    return 0


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
