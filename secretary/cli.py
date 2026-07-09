from __future__ import annotations

import argparse
from pathlib import Path

from secretary.config import validate, validate_instance
from secretary.config import load_config
from secretary.data import KANBOARD_DATA_PATH, init_layout, raw_kanboard_dump
from secretary.host import (
    FixtureHostSource,
    KindDiff,
    LiveHostSource,
    build_expectations,
    inventory,
)


NOT_IMPLEMENTED = "not implemented in Phase 1 skeleton"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="secretary")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor", help="inspect an instance without changing the host")
    doctor.add_argument("--dry-run", action="store_true", help="required for the Phase 1 doctor")
    doctor.add_argument(
        "--instance",
        required=True,
        help="path to an instance dir or instance.yaml",
    )
    doctor.add_argument(
        "--host",
        action="store_true",
        help="also compare the instance against the live host (read-only inventory)",
    )
    doctor.add_argument(
        "--host-fixture",
        metavar="DIR",
        help="compare against a fixture host dir instead of the live host (implies --host)",
    )
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="treat migration warnings as findings",
    )
    doctor.set_defaults(handler=run_doctor)

    data = subparsers.add_parser("data", help="manage the secretary-data layout")
    data_subcommands = data.add_subparsers(dest="data_command")

    data_init = data_subcommands.add_parser("init", help="create secretary-data and its manifest")
    data_init.add_argument(
        "--instance",
        required=True,
        help="path to an instance dir or instance.yaml",
    )
    data_init.add_argument(
        "--data-dir",
        help="override instance.yaml data_dir",
    )
    data_init.set_defaults(handler=run_data_init)

    raw_dump = data_subcommands.add_parser(
        "raw-kanboard-dump",
        help="copy the live Kanboard storage into secretary-data/board",
    )
    raw_dump.add_argument(
        "--instance",
        required=True,
        help="path to an instance dir or instance.yaml",
    )
    raw_dump.add_argument(
        "--data-dir",
        help="override instance.yaml data_dir",
    )
    raw_dump.add_argument("--container", default="cp-kanboard")
    raw_dump.add_argument("--source-path", default=KANBOARD_DATA_PATH)
    raw_dump.set_defaults(handler=run_raw_kanboard_dump)
    data.set_defaults(handler=not_implemented("data"))

    for name in ("reconcile", "backup", "restore"):
        command = subparsers.add_parser(name)
        command.add_argument("args", nargs="*")
        command.set_defaults(handler=not_implemented(name))

    project = subparsers.add_parser("project")
    project_subcommands = project.add_subparsers(dest="project_command")
    project_add = project_subcommands.add_parser("add")
    project_add.add_argument("path_or_url", nargs="?")
    project_add.set_defaults(handler=not_implemented("project add"))
    project.set_defaults(handler=not_implemented("project"))

    for name in ("task", "memory"):
        command = subparsers.add_parser(name)
        command.add_argument("args", nargs="*")
        command.set_defaults(handler=not_implemented(name))

    return parser


def run_doctor(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print("secretary doctor requires --dry-run in the Phase 1 skeleton")
        return 2

    report = validate_instance(Path(args.instance))

    if not report.ok:
        print(f"secretary doctor: {len(report.errors)} config problem(s):")
        for error in report.errors:
            print(f"  {error}")
        return 1

    print("Secretary doctor report")
    print("mode: dry-run")
    print(f"instance: {report.instance_path}")
    print(f"name: {report.name or 'unnamed'}")
    print(f"projects: {report.projects}")
    print(f"adapters: {report.adapters}")
    print(f"data manifest: {'present' if report.has_manifest else 'absent'}")
    if report.manifest_path:
        print(f"data manifest path: {report.manifest_path}")
    if report.warnings:
        print(f"warnings: {len(report.warnings)}")
        for warning in report.warnings:
            print(f"  {warning}")

    host_incomplete = False
    if args.host or args.host_fixture:
        host_incomplete = print_host_inventory(report, args)

    print("host changes: none")
    if host_incomplete:
        # A kind could not be inspected, so this is not a clean "all matched".
        print("status: host inventory incomplete")
        return 1
    if report.warnings and args.strict:
        print("status: warnings")
        return 1
    print("status: ok")
    return 0


def run_data_init(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=False)
    if data_dir is None:
        return 1

    layout = init_layout(data_dir)
    manifest = load_config(layout.manifest_path)
    errors = validate(manifest, "data-manifest", layout.manifest_path.name)
    if errors:
        print(f"secretary data init: generated invalid data manifest at {layout.manifest_path}")
        for error in errors:
            print(f"  {error}")
        return 1

    print(f"secretary-data: {layout.data_dir}")
    print(f"manifest: {layout.manifest_path}")
    print(f"created directories: {_join([str(p.relative_to(layout.data_dir)) for p in layout.created_dirs])}")
    print("status: ok")
    return 0


def run_raw_kanboard_dump(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1

    try:
        dump = raw_kanboard_dump(
            data_dir,
            container=args.container,
            source_path=args.source_path,
        )
    except RuntimeError as exc:
        print(f"secretary data raw-kanboard-dump: {exc}")
        return 1

    print(f"kanboard raw dump: {dump.dump_dir}")
    print(f"source: {dump.source}")
    print("status: ok")
    return 0


def print_host_inventory(report, args: argparse.Namespace) -> bool:
    """Print the read-only host inventory: matched / missing / unmanaged per kind.

    Returns True if any kind could not be inspected (reported as unavailable).
    """
    if args.host_fixture:
        source = FixtureHostSource(Path(args.host_fixture))
    else:
        source = LiveHostSource()

    expected = build_expectations(report.bindings, report.host)
    collected = source.collect(expected)
    diffs = inventory(expected, collected.inventory)

    print("")
    print("host inventory: read-only")
    for kind in ("projects", "units", "orca repos"):
        reason = collected.errors.get(kind)
        if reason:
            print(f"{kind}:")
            print(f"  unavailable: {reason}")
        else:
            _print_kind(kind, diffs[kind])
    return bool(collected.errors)


def _print_kind(kind: str, diff: KindDiff) -> None:
    print(f"{kind}:")
    print(f"  matched: {_join(diff.matched)}")
    print(f"  missing-on-host: {_join(diff.missing_on_host)}")
    print(f"  unmanaged-on-host: {_join(diff.unmanaged_on_host)}")


def _join(names: list[str]) -> str:
    return ", ".join(names) if names else "(none)"


def _instance_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path / "instance.yaml" if path.is_dir() else path


def _data_dir_from_args(args: argparse.Namespace, *, validate_tree: bool) -> Path | None:
    if args.data_dir:
        return Path(args.data_dir).expanduser()

    if validate_tree:
        report = validate_instance(_instance_path(args.instance))
        if report.errors:
            print(f"secretary data: {len(report.errors)} config problem(s):")
            for error in report.errors:
                print(f"  {error}")
            return None

    data_dir = _load_data_dir(_instance_path(args.instance))
    if data_dir is None:
        print("secretary data: instance.yaml has no usable data_dir")
        return None
    return data_dir


def _load_data_dir(instance_path: Path) -> Path | None:
    from secretary.config import ConfigError

    try:
        instance = load_config(instance_path)
    except ConfigError:
        return None
    if not isinstance(instance, dict):
        return None
    data_dir = instance.get("data_dir")
    if not isinstance(data_dir, str) or not data_dir:
        return None
    return Path(data_dir).expanduser()


def not_implemented(command: str):
    def handler(_args: argparse.Namespace) -> int:
        print(f"secretary {command}: {NOT_IMPLEMENTED}")
        return 1

    return handler
