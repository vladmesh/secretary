"""CLI handlers for the pilot dispatcher cutover."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from secretary.dispatcher import (
    DispatcherError,
    HostError,
    PilotSelector,
    runtime_from_args,
)
from secretary.dispatcher_state import DispatcherStateError
from secretary.tasks import TaskError


def add_dispatcher_subcommands(subparsers) -> None:
    dispatcher = subparsers.add_parser("dispatcher", help="run the Phase 7 pilot dispatcher")
    commands = dispatcher.add_subparsers(dest="dispatcher_command")

    for name, handler in (
        ("preflight", run_dispatcher_preflight),
        ("pause-old", run_dispatcher_pause_old),
        ("start-new-pilot", run_dispatcher_start_new_pilot),
        ("tick", run_dispatcher_tick),
        ("observe", run_dispatcher_observe),
        ("commit-cutover", run_dispatcher_commit_cutover),
        ("rollback", run_dispatcher_rollback),
    ):
        command = commands.add_parser(name)
        add_pilot_common(command)
        command.set_defaults(handler=handler)
        if name == "pause-old":
            command.add_argument("--evidence-file")
        if name == "rollback":
            command.add_argument("--reason-file")

    for name, handler in (
        ("production-tick", run_dispatcher_production_tick),
        ("production-observe", run_dispatcher_production_observe),
        ("production-run", run_dispatcher_production_run),
    ):
        command = commands.add_parser(name)
        add_production_common(command)
        command.set_defaults(handler=handler)
        if name == "production-run":
            command.add_argument("--interval-seconds", type=float, default=60.0)
            command.add_argument("--max-interval-seconds", type=float, default=300.0)
            command.add_argument("--max-ticks", type=int)

    dispatcher.set_defaults(handler=not_implemented_dispatcher)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance", required=True)
    parser.add_argument("--data-dir")
    parser.add_argument("--owner", default=os.environ.get("SECRETARY_DISPATCHER_OWNER", "secretary-dispatcher"))
    parser.add_argument("--actor", default=os.environ.get("BOARD_ACTOR", "operator"))
    parser.add_argument(
        "--host-mode",
        choices=("real", "noop"),
        default=os.environ.get("SECRETARY_DISPATCHER_HOST_MODE", "real"),
    )


def add_pilot_common(parser: argparse.ArgumentParser) -> None:
    add_common(parser)
    parser.add_argument("--pilot-ref", required=True)


def add_production_common(parser: argparse.ArgumentParser) -> None:
    add_common(parser)


def not_implemented_dispatcher(args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "dispatcher subcommand required"}}))
    return 2


def run_dispatcher_preflight(args: argparse.Namespace) -> int:
    return _run(args, lambda runtime, selector: runtime.preflight(selector))


def run_dispatcher_pause_old(args: argparse.Namespace) -> int:
    evidence = _read_optional(args.evidence_file) or "operator confirmed old dispatcher pause"
    return _run(args, lambda runtime, selector: runtime.pause_old(selector, actor=args.actor, evidence=evidence))


def run_dispatcher_start_new_pilot(args: argparse.Namespace) -> int:
    return _run(args, lambda runtime, selector: runtime.start_new_pilot(selector, actor=args.actor))


def run_dispatcher_tick(args: argparse.Namespace) -> int:
    return _run(args, lambda runtime, selector: runtime.tick(selector))


def run_dispatcher_observe(args: argparse.Namespace) -> int:
    return _run(args, lambda runtime, selector: runtime.observe(selector))


def run_dispatcher_commit_cutover(args: argparse.Namespace) -> int:
    return _run(args, lambda runtime, selector: runtime.commit_cutover(selector, actor=args.actor))


def run_dispatcher_rollback(args: argparse.Namespace) -> int:
    def operation(runtime, selector):
        reason = _read_optional(args.reason_file)
        if not reason.strip():
            raise DispatcherError("validation", "rollback requires a non-empty reason file", 2)
        return runtime.rollback(selector, actor=args.actor, reason=reason)

    return _run(args, operation)


def run_dispatcher_production_tick(args: argparse.Namespace) -> int:
    return _run_production(args, lambda runtime: runtime.production_tick())


def run_dispatcher_production_observe(args: argparse.Namespace) -> int:
    return _run_production(args, lambda runtime: runtime.production_observe())


def run_dispatcher_production_run(args: argparse.Namespace) -> int:
    return _run_production(
        args,
        lambda runtime: runtime.production_run(
            interval_seconds=args.interval_seconds,
            max_interval_seconds=args.max_interval_seconds,
            max_ticks=args.max_ticks,
        ),
    )


def _run(args: argparse.Namespace, operation) -> int:
    try:
        selector = PilotSelector.exact(args.pilot_ref)
        runtime = runtime_from_args(args.instance, args.data_dir, host_mode=args.host_mode, owner=args.owner)
        result = operation(runtime, selector)
    except (DispatcherError, DispatcherStateError, TaskError) as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, sort_keys=True, separators=(",", ":")))
        return exc.exit_code
    except HostError as exc:
        print(json.dumps({"error": {"code": "host_error", "message": str(exc)}}, sort_keys=True, separators=(",", ":")))
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status") in {"ok", "skipped"} else 3


def _run_production(args: argparse.Namespace, operation) -> int:
    try:
        runtime = runtime_from_args(args.instance, args.data_dir, host_mode=args.host_mode, owner=args.owner)
        result = operation(runtime)
    except (DispatcherError, DispatcherStateError, TaskError) as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, sort_keys=True, separators=(",", ":")))
        return exc.exit_code
    except HostError as exc:
        print(json.dumps({"error": {"code": "host_error", "message": str(exc)}}, sort_keys=True, separators=(",", ":")))
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status") in {"ok", "skipped"} else 3


def _read_optional(path: str | None) -> str:
    if path is None:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DispatcherError("usage", f"cannot read file: {exc}", 2) from None
