from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .schema_checks import validate_instance_config


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INSTANCE = ROOT / "config" / "examples" / "instance.example.json"


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.exists() or path.is_absolute():
        return path
    return ROOT / path


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    if not args.empty:
        print("Phase 1 skeleton supports only bootstrap --empty.", file=sys.stderr)
        return 2
    print("bootstrap --empty dry placeholder: no host changes")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print("doctor apply mode is not implemented in the Phase 1 skeleton.", file=sys.stderr)
        return 2
    instance_path = _resolve_path(args.instance)
    data = _load_json(instance_path)
    errors = validate_instance_config(data)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"doctor dry-run ok: {instance_path}")
    print("host changes: none")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    mode = "dry-run" if args.dry_run else "apply"
    print(f"reconcile {mode} placeholder: no managed resources in Phase 1")
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    print(f"backup {args.backup_command} placeholder: data plane is not created in Phase 1")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    print(f"restore placeholder for {args.archive}: data plane is not created in Phase 1")
    return 0


def _cmd_project_add(args: argparse.Namespace) -> int:
    print(f"project add placeholder for {args.path_or_url}: onboarding starts after Phase 1")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="secretary")
    parser.add_argument("--version", action="version", version=f"secretary {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--empty", action="store_true")
    bootstrap.set_defaults(func=_cmd_bootstrap)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--dry-run", action="store_true")
    doctor.add_argument("--instance", default=str(DEFAULT_INSTANCE))
    doctor.set_defaults(func=_cmd_doctor)

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--dry-run", action="store_true")
    reconcile.set_defaults(func=_cmd_reconcile)

    backup = subparsers.add_parser("backup")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)
    backup_sub.add_parser("create").set_defaults(func=_cmd_backup)
    backup_sub.add_parser("verify").set_defaults(func=_cmd_backup)

    restore = subparsers.add_parser("restore")
    restore.add_argument("archive")
    restore.set_defaults(func=_cmd_restore)

    project = subparsers.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    project_add = project_sub.add_parser("add")
    project_add.add_argument("path_or_url")
    project_add.set_defaults(func=_cmd_project_add)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
