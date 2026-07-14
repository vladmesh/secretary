"""CLI handlers for the public task protocol."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Callable

from secretary.config import ConfigError, load_config
from secretary.onboarding import DEFAULT_INSTANCE
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
    task_create = task_subcommands.add_parser("create")
    task_create.add_argument("--role", required=True, choices=("po", "worker", "reviewer", "steward", "retro"))
    task_create.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    task_create.add_argument("--data-dir", default=os.environ.get("SECRETARY_DATA_DIR", "secretary-data"))
    task_create.add_argument("--request-id")
    task_create.add_argument(
        "--instance",
        default=os.environ.get("SECRETARY_INSTANCE", DEFAULT_INSTANCE),
        help="instance directory (default: SECRETARY_INSTANCE or /home/dev/secretary-instance)",
    )
    task_create.add_argument("--project", required=True)
    task_create.add_argument("--type", required=True, choices=("code", "research"))
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--description", default="")
    task_create.add_argument("--body-file")
    task_create.add_argument("--ref", default="")
    task_create.add_argument("--state", choices=("ideas", "ready"), default="ideas")
    task_create.add_argument("--blocked-by", default="")
    task_create.add_argument("--head", default="")
    task_create.add_argument("--review-head", default="")
    task_create.add_argument("--slug", default="")
    task_create.add_argument("--base-branch", default="")
    task_create.add_argument("--complexity", choices=("cheap", "standard", "hard", "frontier"), default="standard")
    task_create.add_argument("--family-preference", choices=("auto", "claude", "codex"), default="auto")
    task_create.add_argument("--codex-mode", "--codex-launch-mode", dest="codex_mode", choices=("exec", "tui"), default="")
    task_create.set_defaults(handler=run_task_create)
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


def run_task_create(args: argparse.Namespace) -> int:
    try:
        _validate_codex_mode_for_create(args)
        description = _read_body(args.body_file) if args.body_file else args.description
        writer = TaskWriter(KanboardClient(), data_dir=args.data_dir)
        result = writer.create(
            role=args.role,
            actor=args.actor or args.role,
            project=args.project,
            task_type=args.type,
            title=args.title,
            description=description,
            target=args.state,
            reference=args.ref,
            blocked_by=args.blocked_by,
            head=args.head,
            review_head=args.review_head,
            slug=args.slug,
            base_branch=args.base_branch,
            complexity=args.complexity,
            family_preference=args.family_preference,
            codex_launch_mode=args.codex_mode,
            request_id=args.request_id,
        )
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


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


def _validate_codex_mode_for_create(args: argparse.Namespace) -> None:
    if not args.codex_mode:
        return
    heads = _load_heads(Path(args.instance))
    head = args.head or str(heads.get("role_defaults", {}).get("new_card") or "codex")
    profiles = heads.get("profiles", {})
    profile = profiles.get(head) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        raise TaskError("validation", f"--codex-mode requires a known Codex worker head; {head!r} is not defined", 2)
    adapter = str(profile.get("adapter") or "")
    if adapter != "codex":
        detail = adapter or "unknown"
        raise TaskError("validation", f"--codex-mode requires a Codex worker head; {head!r} uses {detail}", 2)


def _load_heads(instance: Path) -> dict:
    instance_file = instance / "instance.yaml" if instance.is_dir() else instance
    heads_file = instance_file.parent / "heads" / "heads.yaml"
    try:
        loaded = load_config(heads_file)
    except ConfigError as exc:
        raise TaskError("validation", f"cannot validate --codex-mode: {exc}", 2) from None
    if not isinstance(loaded, dict):
        raise TaskError("validation", "cannot validate --codex-mode: heads config has an unsupported shape", 2)
    return loaded
