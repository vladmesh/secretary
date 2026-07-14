"""CLI handlers for the public task protocol."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Callable

from secretary.tasks import KanboardClient, TaskAudit, TaskError, TaskReader, TaskWriter


def add_task_subcommands(subparsers) -> None:
    task = subparsers.add_parser("task", help="read normalized cards from the Pipeline board")
    task_subcommands = task.add_subparsers(dest="task_command")
    task_list = task_subcommands.add_parser("list")
    task_list.add_argument(
        "--state",
        action="append",
        choices=("ideas", "ready", "in_progress", "validate", "blocked", "done"),
    )
    task_list.add_argument("--project")
    task_list.set_defaults(handler=run_task_list)
    task_show = task_subcommands.add_parser("show")
    task_show.add_argument("--ref", required=True)
    task_show.set_defaults(handler=run_task_show)
    for name, handler in (
        ("comment", run_task_comment),
        ("report", run_task_report),
        ("verdict", run_task_verdict),
        ("move", run_task_move),
    ):
        command = task_subcommands.add_parser(name)
        command.add_argument("--ref", required=True)
        command.add_argument(
            "--role",
            required=True,
            choices=("po", "dispatcher", "worker", "reviewer", "steward", "retro"),
        )
        command.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
        command.add_argument("--data-dir", default=os.environ.get("SECRETARY_DATA_DIR", "secretary-data"))
        command.add_argument("--request-id")
        command.add_argument("--body-file")
        if name == "report":
            command.add_argument("--kind", required=True, choices=("done", "blocked"))
        if name == "verdict":
            command.add_argument("--kind", required=True, choices=("green", "red"))
        if name == "move":
            command.add_argument("--to", required=True, choices=("ideas", "ready", "in_progress", "validate", "blocked", "done"))
            command.add_argument("--reason-file")
        command.set_defaults(handler=handler)
    task_claim = task_subcommands.add_parser("claim")
    task_claim.add_argument("--ref", required=True)
    task_claim.add_argument("--role", required=True, choices=("dispatcher",))
    task_claim.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    task_claim.add_argument("--data-dir", default=os.environ.get("SECRETARY_DATA_DIR", "secretary-data"))
    task_claim.add_argument("--request-id")
    task_claim.add_argument("--worker", required=True)
    task_claim.add_argument("--resolved-head", default="")
    task_claim.add_argument("--resolved-review-head", default="")
    task_claim.add_argument("--slug", default="")
    task_claim.add_argument("--base-branch", default="")
    task_claim.add_argument("--cap", type=int, default=3)
    task_claim.set_defaults(handler=run_task_claim)
    reconcile_audit = task_subcommands.add_parser("reconcile-audit")
    reconcile_audit.add_argument("--data-dir", default=os.environ.get("SECRETARY_DATA_DIR", "secretary-data"))
    reconcile_audit.set_defaults(handler=run_task_reconcile_audit)
    verify_audit = task_subcommands.add_parser("verify-audit")
    verify_audit.add_argument("--data-dir", default=os.environ.get("SECRETARY_DATA_DIR", "secretary-data"))
    verify_audit.set_defaults(handler=run_task_verify_audit)
    task.set_defaults(handler=not_implemented_task)


def not_implemented_task(args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "task subcommand required"}}))
    return 2


def run_task_list(args: argparse.Namespace) -> int:
    return _run_task_read(lambda reader: reader.list(states=set(args.state or ()), project=args.project))


def run_task_show(args: argparse.Namespace) -> int:
    return _run_task_read(lambda reader: reader.show(args.ref))


def _run_task_read(operation: Callable[[TaskReader], object]) -> int:
    try:
        reader = TaskReader(KanboardClient())
        result = operation(reader)
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _read_body(path: str | None) -> str:
    if path is None:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise TaskError("usage", f"cannot read body file: {exc}", 2) from None


def _run_task_write(args: argparse.Namespace, operation: Callable[[TaskWriter, str, str], object]) -> int:
    try:
        body = _read_body(getattr(args, "body_file", None) or getattr(args, "reason_file", None))
        result = operation(TaskWriter(KanboardClient(), data_dir=args.data_dir), body, args.actor or args.role)
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def run_task_comment(args: argparse.Namespace) -> int:
    return _run_task_write(args, lambda writer, body, actor: writer.comment(role=args.role, actor=actor, reference=args.ref, body=body, request_id=args.request_id))


def run_task_report(args: argparse.Namespace) -> int:
    return _run_task_write(args, lambda writer, body, actor: writer.report(role=args.role, actor=actor, reference=args.ref, kind=args.kind, body=body, request_id=args.request_id))


def run_task_verdict(args: argparse.Namespace) -> int:
    return _run_task_write(args, lambda writer, body, actor: writer.verdict(role=args.role, actor=actor, reference=args.ref, kind=args.kind, body=body, request_id=args.request_id))


def run_task_move(args: argparse.Namespace) -> int:
    return _run_task_write(args, lambda writer, body, actor: writer.move(role=args.role, actor=actor, reference=args.ref, target=args.to, reason=body, request_id=args.request_id))


def run_task_claim(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.claim(
            role=args.role,
            actor=actor,
            reference=args.ref,
            worker=args.worker,
            resolved_head=args.resolved_head,
            resolved_review_head=args.resolved_review_head,
            slug=args.slug,
            base_branch=args.base_branch,
            cap=args.cap,
            request_id=args.request_id,
        ),
    )


def run_task_reconcile_audit(args: argparse.Namespace) -> int:
    try:
        repaired, unresolved = TaskWriter(KanboardClient(), data_dir=args.data_dir).reconcile()
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print(json.dumps({"repaired": repaired, "unresolved": unresolved}, sort_keys=True, separators=(",", ":")))
    return 0 if unresolved == 0 else 1


def run_task_verify_audit(args: argparse.Namespace) -> int:
    status = TaskAudit(args.data_dir).status()
    print(json.dumps(status, sort_keys=True, separators=(",", ":")))
    return 0 if status["ok"] else 1
