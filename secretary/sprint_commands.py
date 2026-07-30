"""CLI handlers for sprint entities."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Callable

from secretary.sprints import BUDGET_EVENT_TYPES, SprintReader, SprintWriter
from secretary.task_commands import _add_data_dir_args, _read_body, resolve_data_dir
from secretary.tasks import KanboardClient, TaskError
from secretary.dispatcher_observer import observer_snapshot
from secretary.config import ConfigError, load_config


def add_sprint_subcommands(subparsers) -> None:
    sprint = subparsers.add_parser("sprint", help="manage sprint entities on the dedicated Kanboard board")
    commands = sprint.add_subparsers(dest="sprint_command")
    listed = commands.add_parser("list")
    listed.add_argument("--status", action="append", choices=("open", "opening", "closed", "stopped"))
    listed.set_defaults(handler=run_list)
    shown = commands.add_parser("show")
    shown.add_argument("--ref", required=True)
    _add_data_dir_args(shown)
    shown.set_defaults(handler=run_show)
    status = commands.add_parser("status")
    status.add_argument("--ref", required=True)
    _add_data_dir_args(status)
    status.set_defaults(handler=run_status)
    created = commands.add_parser("create")
    created.add_argument("--role", required=True, choices=("po", "steward"))
    created.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(created)
    created.add_argument("--request-id")
    created.add_argument("--goal", required=True)
    created.add_argument("--definition-of-done", default="")
    created.add_argument("--dod-file")
    created.add_argument("--repository", action="append", default=[])
    created.add_argument("--product", required=True, help="product id the sprint belongs to")
    created.add_argument(
        "--issue", action="append", required=True,
        help="open issue of that product the sprint serves; repeat for more",
    )
    created.add_argument(
        "--project", action="append", required=True,
        help="registered project the sprint reserves; repeat for more",
    )
    created.add_argument("--ref", default="")
    created.set_defaults(handler=run_create)
    for name, handler, roles in (
        ("comment", run_comment, ("po", "dispatcher", "worker", "reviewer", "steward", "retro")),
        ("current-task", run_current_task, ("po", "dispatcher", "observer", "steward")),
        ("budget", run_budget, ("po", "dispatcher", "steward")),
        ("resume", run_resume, ("po", "dispatcher", "observer", "steward")),
        ("reopen", run_reopen, ("po",)),
        ("close", run_close, ("po",)),
    ):
        command = commands.add_parser(name)
        command.add_argument("--ref", required=True)
        command.add_argument("--role", required=True, choices=roles)
        command.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
        _add_data_dir_args(command)
        command.add_argument("--request-id")
        if name == "comment":
            command.add_argument("--body-file", required=True)
        elif name == "current-task":
            command.add_argument("--task", required=True)
        elif name == "budget":
            command.add_argument("--type", required=True, choices=BUDGET_EVENT_TYPES)
        elif name == "resume":
            command.add_argument("--body-file", required=True)
            command.add_argument("--delivery-id")
            command.add_argument("--through-event")
        command.set_defaults(handler=handler)
    sprint.set_defaults(handler=not_implemented)


def not_implemented(args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "sprint subcommand required"}}))
    return 2


def _read(
    operation: Callable[[SprintReader], object], *, data_dir: str | None = None,
    thresholds: dict | None = None,
) -> int:
    try:
        result = operation(SprintReader(KanboardClient(), data_dir=data_dir, thresholds=thresholds))
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _write(args: argparse.Namespace, operation: Callable[[SprintWriter], object]) -> int:
    try:
        result = operation(SprintWriter(
            KanboardClient(), data_dir=resolve_data_dir(args), thresholds=_thresholds(args),
            instance=getattr(args, "instance", None) or None,
        ))
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def run_list(args: argparse.Namespace) -> int:
    return _read(lambda reader: reader.list(statuses=set(args.status or ())))


def run_show(args: argparse.Namespace) -> int:
    return _read(
        lambda reader: reader.show(args.ref), data_dir=resolve_data_dir(args), thresholds=_thresholds(args),
    )


def run_status(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args)
    try:
        raw = json.loads((Path(data_dir) / "dispatcher" / "production-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raw = {}
    observer = next((row for row in observer_snapshot(raw) if row.get("sprint") == args.ref), None)
    return _read(
        lambda reader: reader.status(args.ref, observer=observer), data_dir=data_dir, thresholds=_thresholds(args),
    )


def _thresholds(args: argparse.Namespace) -> dict | None:
    raw_instance = getattr(args, "instance", "") or ""
    if not raw_instance:
        return None
    instance = Path(raw_instance)
    instance_file = instance / "instance.yaml" if instance.is_dir() else instance
    try:
        config = load_config(instance_file)
    except ConfigError:
        return None
    return config.get("sprint_budget") if isinstance(config, dict) else None


def run_create(args: argparse.Namespace) -> int:
    try:
        definition_of_done = _read_body(args.dod_file) if args.dod_file else args.definition_of_done
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    return _write(args, lambda writer: writer.create(
        role=args.role, actor=args.actor or args.role, goal=args.goal,
        definition_of_done=definition_of_done, repositories=args.repository,
        product=args.product, issues=args.issue, projects=args.project,
        reference=args.ref, request_id=args.request_id,
    ))


def run_comment(args: argparse.Namespace) -> int:
    return _write(args, lambda writer: writer.comment(role=args.role, actor=args.actor or args.role, reference=args.ref, body=_read_body(args.body_file), request_id=args.request_id))


def run_current_task(args: argparse.Namespace) -> int:
    return _write(args, lambda writer: writer.set_current_task(role=args.role, actor=args.actor or args.role, reference=args.ref, task_reference=args.task, request_id=args.request_id))


def run_budget(args: argparse.Namespace) -> int:
    return _write(args, lambda writer: writer.record_budget(role=args.role, actor=args.actor or args.role, reference=args.ref, event_type=args.type, request_id=args.request_id))


def run_resume(args: argparse.Namespace) -> int:
    try:
        entry = json.loads(_read_body(args.body_file))
    except (TaskError, ValueError):
        print(json.dumps({"error": {"code": "validation", "message": "resume file must contain JSON"}}), file=os.sys.stderr)
        return 2
    return _write(
        args,
        lambda writer: writer.resume(
            role=args.role,
            actor=args.actor or args.role,
            reference=args.ref,
            entry=entry,
            request_id=args.request_id,
            delivery_id=args.delivery_id or "",
            through_event=args.through_event or "",
        ),
    )


def run_reopen(args: argparse.Namespace) -> int:
    return _write(args, lambda writer: writer.reopen(role=args.role, actor=args.actor or args.role, reference=args.ref, request_id=args.request_id))


def run_close(args: argparse.Namespace) -> int:
    return _write(args, lambda writer: writer.close(role=args.role, actor=args.actor or args.role, reference=args.ref, request_id=args.request_id))
