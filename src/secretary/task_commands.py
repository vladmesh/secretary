"""CLI handlers for the public task protocol."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path

from secretary.cli_output import print_json
from secretary.config import ConfigError, DataDirError, instance_data_dir, load_config
from secretary.onboarding import DEFAULT_INSTANCE
from secretary.tasks import (
    _BLOCK_CLASSIFICATIONS,
    KanboardClient,
    TaskAudit,
    TaskError,
    TaskReader,
    TaskWriter,
)
from triggered_agents.runtime.head import CODEX_LAUNCH_MODES


def _add_instance_arg(parser) -> None:
    """Every task command names the installation it talks to, reads included.

    Reads used to skip this and fall through `_instance` to `DEFAULT_INSTANCE`, which is
    ``Path.home()/secretary-instance`` resolved at import: neither ``--instance`` nor
    ``SECRETARY_INSTANCE`` could move them, so a process bound to one installation still read the
    home one. On the appliance host that is the production board, reached from a cleared
    environment — the accident class of secretary-1026, and the reason a unit-suite `task show`
    could answer with live cards.
    """
    parser.add_argument(
        "--instance",
        default=os.environ.get("SECRETARY_INSTANCE", DEFAULT_INSTANCE),
        help=f"instance directory (default: SECRETARY_INSTANCE or {DEFAULT_INSTANCE})",
    )


def _add_data_dir_args(parser) -> None:
    """Data dir is pinned to the installation, not to the process CWD.

    A worker runs the task protocol from its own project workspace; a CWD-relative
    default would drop the audit trail into that workspace and leave it dirty.
    """
    parser.add_argument("--data-dir", default=os.environ.get("SECRETARY_DATA_DIR"))
    _add_instance_arg(parser)


def resolve_data_dir(args: argparse.Namespace) -> str:
    explicit = getattr(args, "data_dir", None)
    if explicit:
        return str(Path(explicit).expanduser())
    instance = Path(getattr(args, "instance", None) or DEFAULT_INSTANCE).expanduser()
    try:
        return str(instance_data_dir(instance))
    except DataDirError as exc:
        instance_file = instance / "instance.yaml" if instance.is_dir() else instance
        raise TaskError(
            "usage", f"cannot resolve data dir from {instance_file}: {exc}; pass --data-dir", 2
        ) from None


def _instance(args: argparse.Namespace) -> str:
    """One explicit board-routing source for every task command."""
    return str(getattr(args, "instance", None) or DEFAULT_INSTANCE)


def add_task_subcommands(subparsers) -> None:
    task = subparsers.add_parser("task", help="read normalized cards from the Pipeline board")
    task_subcommands = task.add_subparsers(dest="task_command")
    task_list = task_subcommands.add_parser("list")
    task_list.add_argument(
        "--state",
        action="append",
        choices=("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done"),
    )
    task_list.add_argument("--project")
    task_list.add_argument("--sprint")
    _add_instance_arg(task_list)
    task_list.set_defaults(handler=run_task_list)
    task_show = task_subcommands.add_parser("show")
    task_show.add_argument("--ref", required=True)
    _add_instance_arg(task_show)
    task_show.set_defaults(handler=run_task_show)
    task_create = task_subcommands.add_parser("create")
    task_create.add_argument(
        "--role", required=True, choices=("po", "worker", "reviewer", "steward", "retro", "observer")
    )
    task_create.add_argument("--actor", default=os.environ.get("BOARD_ACTOR"))
    _add_data_dir_args(task_create)
    task_create.add_argument("--request-id")
    task_create.add_argument("--project", required=True)
    task_create.add_argument("--type", required=True, choices=("code", "research"))
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--description", default="")
    task_create.add_argument("--body-file")
    task_create.add_argument("--ref", default="")
    task_create.add_argument("--state", choices=("issues", "ready"), default="ready")
    task_create.add_argument("--blocked-by", default="")
    task_create.add_argument("--head", default="")
    task_create.add_argument("--review-head", default="")
    task_create.add_argument("--slug", default="")
    task_create.add_argument(
        "--base-branch",
        default="",
        help="the branch this card integrates into; only a branch the project declares",
    )
    task_create.add_argument(
        "--seed-ref",
        default="",
        help="git ref or object id the card's checkout starts from (a reslice successor's predecessor candidate)",
    )
    task_create.add_argument(
        "--supersedes", default="", help="reference of the predecessor card a --seed-ref inherits from"
    )
    task_create.add_argument(
        "--complexity", choices=("cheap", "standard", "hard", "frontier"), default="standard"
    )
    task_create.add_argument("--family-preference", choices=("auto", "claude", "codex"), default="auto")
    # No `choices`: `--codex-mode exec` names a launch shape the product removed, and it is
    # answered with that sentence in `_validate_codex_mode_for_create` rather than with argparse's
    # "invalid choice" over a flag whose only remaining value is the default anyway.
    task_create.add_argument("--codex-mode", "--codex-launch-mode", dest="codex_mode", default="")
    task_create.add_argument("--sprint", default="", help="link the card to an open sprint reference")
    task_create.add_argument("--priority", default="", help="rejected: tasks do not carry product priority")
    task_create.add_argument(
        "--budget-event",
        choices=("recreated_task", "hotfix"),
        default="",
        help="charge a sprint recreation or hotfix event",
    )
    _add_sprint_override_args(task_create)
    task_create.set_defaults(handler=run_task_create)
    for name, handler in (
        ("comment", run_task_comment),
        ("report", run_task_report),
        ("verdict", run_task_verdict),
        ("decide", run_task_decide),
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
            # Required with `--kind blocked`, refused with `--kind done`; the writer holds both
            # rules so the protocol is the same from a script as from the CLI.
            command.add_argument("--classification", default="", choices=("", *_BLOCK_CLASSIFICATIONS))
        if name == "verdict":
            command.add_argument("--kind", required=True, choices=("green", "red"))
        if name == "decide":
            command.add_argument("--kind", required=True, choices=("release", "rework", "reslice"))
            command.add_argument("--reason-file")
            command.add_argument(
                "--protocol-prerequisite",
                action="append",
                default=[],
                help="registry artifact required by a rework worker; repeat for multiple prerequisites",
            )
        if name == "move":
            # `--target` is the spelling the restore commands use for the same idea, and the one
            # operators reach for. Both names write the same dest, so neither is a second contract.
            command.add_argument(
                "--to",
                "--target",
                dest="to",
                required=True,
                choices=("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done"),
            )
            command.add_argument("--reason-file")
            # A card leaves Assessment on a decision somebody recorded with `task decide`, and
            # the move has to name it: the writer checks it against the card's audit.
            command.add_argument("--decision", default="", choices=("", "release", "rework", "reslice"))
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
    parser.add_argument(
        "--sprint-override", action="store_true", help="PO only: bypass an open sprint's single-writer guard"
    )
    parser.add_argument("--sprint-override-reason-file", help="required PO override reason file")


def not_implemented_task(args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "task subcommand required"}}))
    return 2


def run_task_list(args: argparse.Namespace) -> int:
    return _run_task_read(
        args,
        lambda reader: reader.list(states=set(args.state or ()), project=args.project, sprint=args.sprint),
    )


def run_task_show(args: argparse.Namespace) -> int:
    return _run_task_read(args, lambda reader: reader.show(args.ref))


def _run_task_read(args: argparse.Namespace, operation: Callable[[TaskReader], object]) -> int:
    return run_task_command(lambda: operation(TaskReader(KanboardClient.for_instance(_instance(args)))))


def run_task_command(
    operation: Callable[[], object], *, exit_code: Callable[[object], int] | None = None
) -> int:
    try:
        result = operation()
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print_json(result, compact=True)
    return exit_code(result) if exit_code is not None else 0


def _read_body(path: str | None) -> str:
    if path is None:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise TaskError("usage", f"cannot read body file: {exc}", 2) from None


def _run_task_write(args: argparse.Namespace, operation: Callable[[TaskWriter, str, str], object]) -> int:
    def command() -> object:
        body = _read_body(getattr(args, "body_file", None) or getattr(args, "reason_file", None))
        writer = TaskWriter(KanboardClient.for_instance(_instance(args)), data_dir=resolve_data_dir(args))
        return operation(writer, body, args.actor or args.role)

    return run_task_command(command)


def run_task_comment(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.comment(
            role=args.role, actor=actor, reference=args.ref, body=body, request_id=args.request_id
        ),
    )


def run_task_create(args: argparse.Namespace) -> int:
    def command() -> object:
        _validate_codex_mode_for_create(args)
        description = _read_body(args.body_file) if args.body_file else args.description
        writer = TaskWriter(KanboardClient.for_instance(_instance(args)), data_dir=resolve_data_dir(args))
        return writer.create(
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
            seed_ref=args.seed_ref,
            supersedes=args.supersedes,
            complexity=args.complexity,
            family_preference=args.family_preference,
            codex_launch_mode=args.codex_mode,
            sprint=args.sprint,
            priority=args.priority,
            budget_event=args.budget_event,
            sprint_override=args.sprint_override,
            sprint_override_reason=_read_body(args.sprint_override_reason_file),
            request_id=args.request_id,
        )

    return run_task_command(command)


def run_task_edit(args: argparse.Namespace) -> int:
    def command() -> object:
        description = _read_body(args.body_file) if args.body_file else args.description
        writer = TaskWriter(KanboardClient.for_instance(_instance(args)), data_dir=resolve_data_dir(args))
        return writer.edit(
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

    return run_task_command(command)


def run_task_report(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.report(
            role=args.role,
            actor=actor,
            reference=args.ref,
            kind=args.kind,
            body=body,
            classification=args.classification,
            request_id=args.request_id,
        ),
    )


def run_task_verdict(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.verdict(
            role=args.role,
            actor=actor,
            reference=args.ref,
            kind=args.kind,
            body=body,
            protocol_prerequisites=args.protocol_prerequisite,
            request_id=args.request_id,
        ),
    )


def run_task_decide(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.decide(
            role=args.role,
            actor=actor,
            reference=args.ref,
            kind=args.kind,
            body=body,
            request_id=args.request_id,
        ),
    )


def run_task_move(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.move(
            role=args.role,
            actor=actor,
            reference=args.ref,
            target=args.to,
            reason=body,
            decision=args.decision,
            sprint_override=args.sprint_override,
            sprint_override_reason=_read_body(args.sprint_override_reason_file),
            request_id=args.request_id,
        ),
    )


def run_task_archive(args: argparse.Namespace) -> int:
    return _run_task_write(
        args,
        lambda writer, body, actor: writer.archive(
            role=args.role, actor=actor, reference=args.ref, reason=body, request_id=args.request_id
        ),
    )


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
    def command() -> object:
        repaired, unresolved = TaskWriter(
            KanboardClient.for_instance(_instance(args)),
            data_dir=resolve_data_dir(args),
        ).reconcile()
        return {"repaired": repaired, "unresolved": unresolved}

    return run_task_command(command, exit_code=lambda result: 0 if result["unresolved"] == 0 else 1)


def run_task_verify_audit(args: argparse.Namespace) -> int:
    return run_task_command(
        lambda: TaskAudit(resolve_data_dir(args)).status(),
        exit_code=lambda result: 0 if result["ok"] else 1,
    )


def _validate_codex_mode_for_create(args: argparse.Namespace) -> None:
    if not args.codex_mode:
        return
    mode = str(args.codex_mode).strip()
    if mode not in CODEX_LAUNCH_MODES:
        # Refused before the registry is even read, and long before the board is touched: there is
        # one Codex launch shape and it is interactive, so `exec` is not a mode this rejects for
        # being unavailable here — it is a mode that no longer exists anywhere.
        known = ", ".join(sorted(CODEX_LAUNCH_MODES))
        raise TaskError(
            "validation",
            f"--codex-mode {mode!r} is not a Codex launch mode; Codex heads launch through the "
            f"interactive TUI only (known: {known})",
            2,
        )
    heads = _load_heads(Path(args.instance))
    head = args.head or str(heads.get("role_defaults", {}).get("new_card") or "codex")
    profiles = heads.get("profiles", {})
    profile = profiles.get(head) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        raise TaskError(
            "validation", f"--codex-mode requires a known Codex worker head; {head!r} is not defined", 2
        )
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
        raise TaskError(
            "validation", "cannot validate --codex-mode: heads config has an unsupported shape", 2
        )
    return loaded
