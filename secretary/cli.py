from __future__ import annotations

import argparse
from pathlib import Path

from secretary.config import validate_instance
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
    doctor.set_defaults(handler=run_doctor)

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

    host_incomplete = False
    if args.host or args.host_fixture:
        host_incomplete = print_host_inventory(report, args)

    print("host changes: none")
    if host_incomplete:
        # A kind could not be inspected, so this is not a clean "all matched".
        print("status: host inventory incomplete")
        return 1
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


def not_implemented(command: str):
    def handler(_args: argparse.Namespace) -> int:
        print(f"secretary {command}: {NOT_IMPLEMENTED}")
        return 1

    return handler
