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


def _add_data_dir_args(parser) -> None:
    """Data dir is pinned to the installation, not to the process CWD.

    A worker runs the task protocol from its own project workspace; a CWD-relative
    default would drop the audit trail into that workspace and leave it dirty.
    """
    parser.add_argument("--data-dir", default=os.environ.get("SECRETARY_DATA_DIR"))
    parser.add_argument(
        "--instance",
        default=os.environ.get("SECRETARY_INSTANCE", DEFAULT_INSTANCE),
        help=f"instance directory (default: SECRETARY_INSTANCE or {DEFAULT_INSTANCE})",
    )


def resolve_data_dir(args: argparse.Namespace) -> str:
    explicit = getattr(args, "data_dir", None)
    if explicit:
        return str(Path(explicit).expanduser())
    instance = Path(getattr(args, "instance", None) or DEFAULT_INSTANCE).expanduser()
    instance_file = instance / "instance.yaml" if instance.is_dir() else instance
    try:
        loaded = load_config(instance_file)
    except ConfigError as exc:
        raise TaskError("usage", f"cannot resolve data dir from {instance_file}: {exc}; pass --data-dir", 2) from None
    data_dir = loaded.get("data_dir") if isinstance(loaded, dict) else None
    if not isinstance(data_dir, str) or not data_dir:
        raise TaskError("usage", f"{instance_file} has no usable data_dir; pass --data-dir", 2)
    resolved = Path(data_dir).expanduser()
    return str(resolved if resolved.is_absolute() else instance_file.parent / resolved)


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
    task_list.add_argument("--sprint")
    task_list.set_defaults(handler=run_task_list)
    task_show = task_subcommands.add_parser("show")
    task_show.add_argument("--ref", required=True)
    task_show.set_defaults(handler=run_task_show)
    task_create = task_subcommands.add_parser("create")
    task_create.add_argument("--role", required=True, choices=("po", "worker", "reviewer", "steward", "retro", "observer"))
    task_create.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(task_create)
    task_create.add_argument("--request-id")
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
    task_create.add_argument("--sprint", default="", help="link the card to an open sprint reference")
    task_create.add_argument("--budget-event", choices=("recreated_task", "hotfix"), default="", help="charge a sprint recreation or hotfix event")
    _add_sprint_override_args(task_create)
    task_create.set_defaults(handler=run_task_create)
    for name, handler in (
        ("comment", run_task_comment),
        ("report", run_task_report),
        ("verdict", run_task_verdict),
        ("move", run_task_move),
        ("archive", run_task_archive),
    ):
        command = task_subcommands.add_parser(name)
        command.add_argument("--ref", required=True)
        command.add_argument(
            "--role",
            required=True,
            choices=("po", "dispatcher", "worker", "reviewer", "steward", "retro", "observer"),
        )
        command.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
        _add_data_dir_args(command)
        command.add_argument("--request-id")
        command.add_argument("--body-file")
        if name == "report":
            command.add_argument("--kind", required=True, choices=("done", "blocked"))
        if name == "verdict":
            command.add_argument("--kind", required=True, choices=("green", "red"))
        if name == "move":
            command.add_argument("--to", required=True, choices=("ideas", "ready", "in_progress", "validate", "blocked", "done"))
            command.add_argument("--reason-file")
            _add_sprint_override_args(command)
        if name == "archive":
            command.add_argument("--reason-file")
        command.set_defaults(handler=handler)
    task_edit = task_subcommands.add_parser("edit")
    task_edit.add_argument("--ref", required=True)
    task_edit.add_argument("--role", required=True, choices=("po", "dispatcher", "observer"))
    task_edit.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(task_edit)
    task_edit.add_argument("--request-id")
    task_edit.add_argument("--title")
    task_edit.add_argument("--description")
    task_edit.add_argument("--body-file", help="file with the full replacement description")
    task_edit.add_argument("--head")
    task_edit.add_argument("--review-head")
    _add_sprint_override_args(task_edit)
    task_edit.set_defaults(handler=run_task_edit)
    task_claim = task_subcommands.add_parser("claim")
    task_claim.add_argument("--ref", required=True)
    task_claim.add_argument("--role", required=True, choices=("dispatcher",))
    task_claim.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(task_claim)
    task_claim.add_argument("--request-id")
    task_claim.add_argument("--worker", required=True)
    task_claim.add_argument("--resolved-head", default="")
    task_claim.add_argument("--resolved-review-head", default="")
    task_claim.add_argument("--slug", default="")
    task_claim.add_argument("--base-branch", default="")
    task_claim.add_argument("--cap", type=int, default=3)
    task_claim.set_defaults(handler=run_task_claim)
    reconcile_audit = task_subcommands.add_parser("reconcile-audit")
    _add_data_dir_args(reconcile_audit)
    reconcile_audit.set_defaults(handler=run_task_reconcile_audit)
    verify_audit = task_subcommands.add_parser("verify-audit")
    _add_data_dir_args(verify_audit)
    verify_audit.set_defaults(handler=run_task_verify_audit)
    task.set_defaults(handler=not_implemented_task)


def _add_sprint_override_args(parser) -> None:
    parser.add_argument("--sprint-override", action="store_true", help="PO only: bypass an open sprint's single-writer guard")
    parser.add_argument("--sprint-override-reason-file", help="required PO override reason file")


def not_implemented_task(args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "task subcommand required"}}))
    return 2


def run_task_list(args: argparse.Namespace) -> int:
    return _run_task_read(lambda reader: reader.list(states=set(args.state or ()), project=args.project, sprint=args.sprint))


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
        writer = TaskWriter(KanboardClient(), data_dir=resolve_data_dir(args))
        result = operation(writer, body, args.actor or args.role)
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
        writer = TaskWriter(KanboardClient(), data_dir=resolve_data_dir(args))
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
            sprint=args.sprint,
            budget_event=args.budget_event,
            sprint_override=args.sprint_override,
            sprint_override_reason=_read_body(args.sprint_override_reason_file),
            request_id=args.request_id,
        )
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def run_task_edit(args: argparse.Namespace) -> int:
    try:
        description = _read_body(args.body_file) if args.body_file else args.description
        writer = TaskWriter(KanboardClient(), data_dir=resolve_data_dir(args))
        result = writer.edit(
            role=args.role,
            actor=args.actor or args.role,
            reference=args.ref,
            title=args.title,
            description=description,
            head=args.head,
            review_head=args.review_head,
            sprint_override=args.sprint_override,
            sprint_override_reason=_read_body(args.sprint_override_reason_file),
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
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.move(
            role=args.role, actor=actor, reference=args.ref, target=args.to, reason=body,
            sprint_override=args.sprint_override,
            sprint_override_reason=_read_body(args.sprint_override_reason_file),
            request_id=args.request_id,
        ),
    )


def run_task_archive(args: argparse.Namespace) -> int:
    return _run_task_write(args, lambda writer, body, actor: writer.archive(role=args.role, actor=actor, reference=args.ref, reason=body, request_id=args.request_id))


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
        repaired, unresolved = TaskWriter(KanboardClient(), data_dir=resolve_data_dir(args)).reconcile()
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print(json.dumps({"repaired": repaired, "unresolved": unresolved}, sort_keys=True, separators=(",", ":")))
    return 0 if unresolved == 0 else 1


def run_task_verify_audit(args: argparse.Namespace) -> int:
    try:
        status = TaskAudit(resolve_data_dir(args)).status()
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
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
