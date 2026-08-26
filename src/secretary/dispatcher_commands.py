"""CLI handlers for the production dispatcher."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from secretary.dispatcher import (
    DispatcherError,
    HostError,
    runtime_from_args,
)
from secretary.dispatch.head_status import head_status
from secretary.dispatcher_pause import PAUSE_MODES
from secretary.tasks import TaskError


def add_dispatcher_subcommands(subparsers) -> None:
    dispatcher = subparsers.add_parser("dispatcher", help="run the task and sprint dispatcher")
    commands = dispatcher.add_subparsers(dest="dispatcher_command")

    for name, handler in (
        ("production-tick", run_dispatcher_production_tick),
        ("production-observe", run_dispatcher_production_observe),
        ("resource-health", run_dispatcher_resource_health),
        ("production-run", run_dispatcher_production_run),
    ):
        command = commands.add_parser(name)
        add_common(command)
        command.set_defaults(handler=handler)
        if name == "production-tick":
            command.add_argument(
                "--probe",
                action="store_true",
                help="run the tick with every write aborted, as a health gate",
            )
        if name == "production-run":
            command.add_argument("--interval-seconds", type=float, default=60.0)
            command.add_argument("--max-interval-seconds", type=float, default=300.0)
            command.add_argument("--max-ticks", type=int)

    dispatcher.set_defaults(handler=not_implemented_dispatcher)


def add_pause_commands(subparsers) -> None:
    """The one door to the pipeline-wide pause.

    Top level rather than under `dispatcher`, because pausing is an operator action on the pipeline
    and not one of the dispatcher's own steps. The legacy `pipeline pause` entry refuses and points
    here, so the two implementations cannot drift apart in silence.
    """
    pause = subparsers.add_parser(
        "pause",
        help="stop the pipeline: drain (no new claims) or freeze (heads stopped too)",
    )
    add_common(pause)
    pause.add_argument("mode", choices=(*PAUSE_MODES, "soft", "hard"))
    pause.add_argument("--reason", help="why the pipeline is paused; required")
    pause.add_argument("--reason-file")
    pause.add_argument(
        "--exclude-workspace",
        action="append",
        default=[],
        help="freeze: leave the head in this workspace running (initiator exception)",
    )
    pause.set_defaults(handler=run_pause)

    resume = subparsers.add_parser("resume", help="clear the pause and put back what a freeze stopped")
    add_common(resume)
    resume.set_defaults(handler=run_resume)

    status = subparsers.add_parser("pause-status", help="read the production dispatcher's pause state")
    add_common(status)
    status.set_defaults(handler=run_pause_status)


def add_head_status_command(subparsers) -> None:
    """The operator's read-only answer to "is there a head in this workspace?".

    Top level, beside `pause-status`, for the same reason that one is: this is a question an
    operator asks about the pipeline, not a step of the dispatcher's tick, and the person asking it
    is usually standing in front of a workspace that looks empty. It lives with the dispatcher's
    commands rather than with `reconcile plan/apply/adopt` because the heads it reports on are
    dispatcher state -- the records naming which head serves which card in which workspace -- while
    `host_commands` is about host resources the dispatcher does not own.
    """
    command = subparsers.add_parser(
        "head-status",
        help="read whether the dispatcher's heads in a workspace are alive, and separately "
        "whether their runtime panes are visible",
    )
    add_common(command)
    command.add_argument(
        "--workspace",
        required=True,
        help="the live workspace to look at; every head the dispatcher holds there is reported",
    )
    command.set_defaults(handler=run_head_status)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance", required=True)
    # Same pair, same order, as `secretary task` (task_commands._add_data_dir_args) and as the
    # telemetry reader in triggered_agents/runtime/production_telemetry.py: an installation that
    # points its data plane elsewhere through the environment must move the dispatcher's writes
    # and its readers together, or health and steward scan would report a file nobody writes
    # (secretary-833 review, round 3).
    parser.add_argument("--data-dir", default=os.environ.get("SECRETARY_DATA_DIR"))
    parser.add_argument(
        "--owner", default=os.environ.get("SECRETARY_DISPATCHER_OWNER", "secretary-dispatcher")
    )
    parser.add_argument("--actor", default=os.environ.get("BOARD_ACTOR", "operator"))
    parser.add_argument(
        "--host-mode",
        choices=("real", "noop"),
        default=os.environ.get("SECRETARY_DISPATCHER_HOST_MODE", "real"),
    )


def not_implemented_dispatcher(args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "dispatcher subcommand required"}}))
    return 2


def run_dispatcher_production_tick(args: argparse.Namespace) -> int:
    if getattr(args, "probe", False):
        return _run_production(args, lambda runtime: runtime.production_probe())
    return _run_production(args, lambda runtime: runtime.production_tick())


def run_dispatcher_production_observe(args: argparse.Namespace) -> int:
    return _run_production(args, lambda runtime: runtime.production_observe())


def run_dispatcher_resource_health(args: argparse.Namespace) -> int:
    return _run_production(
        args,
        lambda runtime: {
            "status": "ok",
            "step": "resource-health",
            "resources": runtime.head_health.snapshot(),
        },
    )


def run_dispatcher_production_run(args: argparse.Namespace) -> int:
    return _run_production(
        args,
        lambda runtime: runtime.production_run(
            interval_seconds=args.interval_seconds,
            max_interval_seconds=args.max_interval_seconds,
            max_ticks=args.max_ticks,
        ),
    )


def run_pause(args: argparse.Namespace) -> int:
    reason = (args.reason or "").strip() or _read_optional(args.reason_file).strip()
    if not reason:
        print(json.dumps({"error": {"code": "usage", "message": "pause requires --reason or --reason-file"}}))
        return 2
    return _run_production(
        args,
        lambda runtime: runtime.pause_pipeline(
            mode=args.mode,
            actor=args.actor,
            reason=reason,
            exclude_workspaces=list(args.exclude_workspace or []),
        ),
    )


def run_resume(args: argparse.Namespace) -> int:
    return _run_production(args, lambda runtime: runtime.resume_pipeline(actor=args.actor))


def run_pause_status(args: argparse.Namespace) -> int:
    return _run_production(args, lambda runtime: runtime.pause_status())


def run_head_status(args: argparse.Namespace) -> int:
    return _run_production(args, lambda runtime: head_status(runtime, workspace=args.workspace))


def _run_production(args: argparse.Namespace, operation) -> int:
    try:
        runtime = runtime_from_args(args.instance, args.data_dir, host_mode=args.host_mode, owner=args.owner)
        result = operation(runtime)
    except (DispatcherError, TaskError) as exc:
        print(
            json.dumps(
                {"error": {"code": exc.code, "message": exc.message}}, sort_keys=True, separators=(",", ":")
            )
        )
        return exc.exit_code
    except HostError as exc:
        print(
            json.dumps(
                {"error": {"code": "host_error", "message": str(exc)}}, sort_keys=True, separators=(",", ":")
            )
        )
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
