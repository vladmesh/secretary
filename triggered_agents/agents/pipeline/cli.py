"""pipeline agent — the task-pipeline board CLI (PO / dispatcher / worker / steward share one
binary).

Role is a global `--role` (or env BOARD_ROLE) checked before the command runs: create is
PO-, steward-, or worker-only (a worker's own create is further gated in ops — straight to Ready
only as a continuation of its own chain, see ops._check_worker_continuation, otherwise Идеи like
any other agent idea), claim is dispatcher-only, report/feedback are worker-only, move/ready defer
to the transition matrix for the role, comment is open to any role (the role becomes the marker).
update accepts any role at this layer but is PO-only in ops (GuardError otherwise), same as
move's per-role matrix. `move --reason` records a comment on the moved card. steward gets every po
transition (via move/ready) plus one more: Blocked -> Done, which additionally needs a non-empty
reason in the same call, see model.STEWARD_OVERRIDE and ops.move_card. steward escalations to
Blocked also need a non-empty reason. idea is reviewer- or retro-only (both file an Идеи-only card,
never move anything, model.TRANSITIONS leaves each an empty set).
setup/list/show/probe need no role. Guards live in model/ops; this layer only wires argv to them
and maps failures to exit codes.

pause/resume (triggered-agents-281) are po-/steward-only. `pause drain|freeze --reason ...`
toggles the persistent flag in `state/pipeline/pause.json` (see dispatcher.pause/resume, pause.py)
that dispatcher.tick checks before claiming or (in freeze's internal hard mode) before touching
any head at all, and runtime/dispatch.py checks before dispatching steward/curator/retro. Legacy
`soft`/`hard` aliases still parse for compatibility. pause-status needs no role, same as
list/show, it only reads the flag.

`probe --resource <id>` exits 0/1 for green/red (see health.run_builtin_probe), not the generic
KanboardError/GuardError table below — it is heads.toml's own probe command, run by
health.refresh, never touching the board at all.

Exit codes: 0 ok, 1 KanboardError, 2 usage/bad-args/role, 3 GuardError. Kanboard-touching
commands emit JSON on stdout; errors go to stderr prefixed `pipeline: `.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import pause as pause_flag
from .model import GuardError, ROLES


def _emit(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0


def _err(msg: str) -> None:
    print(f"pipeline: {msg}", file=sys.stderr)


def _text_arg(inline: str | None, path: str | None) -> str:
    """Resolve a --body / --description pair: file wins, `-` means stdin, else the inline value."""
    if path is not None:
        if path == "-":
            return sys.stdin.read()
        with open(path, encoding="utf-8") as f:
            return f.read()
    return inline or ""


def _need_role(role: str | None, allowed: tuple[str, ...]) -> bool:
    """True if `role` is permitted; else print a stderr reason. Caller returns 2 on False."""
    if role is None:
        _err(f"this command needs --role (one of {', '.join(allowed)}) or env BOARD_ROLE")
        return False
    if role not in allowed:
        _err(f"role {role!r} may not run this command (needs one of {', '.join(allowed)})")
        return False
    return True


def _pause_mode(args) -> str | None:
    mode_arg = getattr(args, "mode", None)
    mode_flag = getattr(args, "mode_flag", None)
    if mode_arg and mode_flag:
        _err("pause mode specified twice; use positional drain/freeze or legacy --mode, not both")
        return None
    mode = mode_arg or mode_flag
    if not mode:
        _err("pause needs a mode: drain or freeze (legacy aliases: soft, hard)")
        return None
    return mode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="triggered_agents pipeline", add_help=True)
    parser.add_argument("--role", choices=ROLES, help="acting role (or env BOARD_ROLE)")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("setup")
    sub.add_parser("tick")       # dispatcher: one deterministic tick (claim/advance)
    sub.add_parser("precheck")   # dispatcher: exit 0 if there is work, PRECHECK_SKIP (100) to skip

    p_pause = sub.add_parser("pause")     # po/steward: drain (claims off) or freeze (heads stopped)
    p_pause.add_argument(
        "mode", nargs="?", choices=(*pause_flag.PUBLIC_MODES, *pause_flag.MODES),
        help="drain stops new claims; freeze also stops live heads; soft/hard are legacy internal aliases")
    p_pause.add_argument(
        "--mode", dest="mode_flag", choices=(*pause_flag.PUBLIC_MODES, *pause_flag.MODES),
        help="legacy form; use positional drain/freeze for user-facing calls")
    p_pause.add_argument("--reason")
    p_pause.add_argument("--reason-file")
    p_pause.add_argument("--actor", help="who requested the pause; defaults to the acting role")
    p_pause.add_argument("--exclude-workspace", action="append", default=[],
                         help="hard pause: leave this worker workspace running")
    sub.add_parser("resume")              # po/steward: undo pause, idempotent if not paused
    sub.add_parser("pause-status")        # any role: current pause state, read-only

    p_probe = sub.add_parser("probe")   # heads.toml's own probe command for a resource
    p_probe.add_argument("--resource", required=True)

    p_create = sub.add_parser("create")
    p_create.add_argument("--project", required=True)
    p_create.add_argument("--type", required=True, dest="task_type")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--ref")
    p_create.add_argument("--column", default="Идеи")
    p_create.add_argument("--blocked-by", dest="blocked_by")
    p_create.add_argument("--head", dest="head")
    p_create.add_argument(
        "--review-head", dest="review_head",
        help="reviewer profile for Validate layer 3; reserved value 'none' skips only layer 3")
    p_create.add_argument("--slug")
    p_create.add_argument("--base-branch", dest="base_branch")
    p_create.add_argument("--description")
    p_create.add_argument("--description-file")
    p_create.add_argument("--own-ref", dest="own_ref")   # worker-only: its own card reference

    p_update = sub.add_parser("update")
    p_update.add_argument("--ref", required=True)
    p_update.add_argument("--blocked-by", dest="blocked_by")
    p_update.add_argument("--head", dest="head")
    p_update.add_argument(
        "--review-head", dest="review_head",
        help="reviewer profile, 'none' to skip layer 3, or empty string to restore the default")
    p_update.add_argument("--slug")
    p_update.add_argument("--base-branch", dest="base_branch")

    p_ready = sub.add_parser("ready")
    p_ready.add_argument("--ref", required=True)

    p_move = sub.add_parser("move")
    p_move.add_argument("--ref", required=True)
    p_move.add_argument("--to", required=True, dest="to_column")
    p_move.add_argument("--reason")           # comment body; required for steward break-glass moves
    p_move.add_argument("--reason-file")

    p_claim = sub.add_parser("claim")
    p_claim.add_argument("--ref", required=True)
    p_claim.add_argument("--worker", required=True)
    p_claim.add_argument("--cap", type=int, default=3)

    p_report = sub.add_parser("report")
    p_report.add_argument("--ref", required=True)
    p_report.add_argument("--kind", required=True, choices=("done", "blocked"))
    p_report.add_argument("--body")
    p_report.add_argument("--body-file")

    p_comment = sub.add_parser("comment")
    p_comment.add_argument("--ref", required=True)
    p_comment.add_argument("--body")
    p_comment.add_argument("--body-file")

    p_feedback = sub.add_parser("feedback")
    p_feedback.add_argument("--ref", required=True)
    p_feedback.add_argument("--body")
    p_feedback.add_argument("--body-file")

    p_verdict = sub.add_parser("verdict")     # reviewer: the layer-3 green/red verdict
    p_verdict.add_argument("--ref", required=True)
    p_verdict.add_argument("--kind", required=True, choices=("green", "red"))
    p_verdict.add_argument("--body")
    p_verdict.add_argument("--body-file")

    p_idea = sub.add_parser("idea")           # reviewer: file a finding as an Идеи card
    p_idea.add_argument("--project", required=True)
    p_idea.add_argument("--title", required=True)
    p_idea.add_argument("--type", default="code", dest="task_type")
    p_idea.add_argument("--ref")
    p_idea.add_argument("--head", dest="head")
    p_idea.add_argument("--slug")
    p_idea.add_argument("--description")
    p_idea.add_argument("--description-file")

    p_list = sub.add_parser("list")
    p_list.add_argument("--column")
    p_list.add_argument("--project")

    p_show = sub.add_parser("show")
    p_show.add_argument("--ref", required=True)

    return parser


def main(argv=None) -> int:
    from ...runtime.kanboard import KanboardError
    from . import ops

    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    role = args.role or os.environ.get("BOARD_ROLE") or None
    if role is not None and role not in ROLES:
        _err(f"unknown role {role!r} (roles: {', '.join(ROLES)})")
        return 2

    try:
        if args.cmd == "setup":
            return _emit(ops.ensure_structure())
        if args.cmd in ("tick", "precheck"):
            from . import dispatcher
            return dispatcher.tick() if args.cmd == "tick" else dispatcher.precheck()
        if args.cmd == "pause":
            if not _need_role(role, ("po", "steward")):
                return 2
            mode = _pause_mode(args)
            if mode is None:
                return 2
            reason = _text_arg(args.reason, args.reason_file).strip()
            if not reason:
                _err("pause needs a non-empty --reason or --reason-file")
                return 2
            actor = (args.actor or role or "").strip()
            from . import dispatcher
            return _emit(dispatcher.pause(mode, reason=reason, actor=actor,
                                          exclude_workspaces=args.exclude_workspace))
        if args.cmd == "resume":
            if not _need_role(role, ("po", "steward")):
                return 2
            from . import dispatcher
            return _emit(dispatcher.resume())
        if args.cmd == "pause-status":
            from . import dispatcher
            return _emit(dispatcher.pause_status())
        if args.cmd == "probe":
            from . import health
            try:
                result = health.run_builtin_probe_result(args.resource)
            except KeyError:
                _err(f"no builtin probe for resource {args.resource!r} "
                    f"(known: {', '.join(sorted(health.BUILTIN_PROBES))})")
                return 2
            if not result.ok:
                _err(health.format_probe_failure(args.resource, result))
            return 0 if result.ok else 1
        if args.cmd == "list":
            return _emit(ops.list_cards(column=args.column, project=args.project))
        if args.cmd == "show":
            return _emit(ops.show_card(args.ref))

        if args.cmd == "create":
            if not _need_role(role, ("po", "steward", "worker")):
                return 2
            desc = _text_arg(args.description, args.description_file)
            return _emit(ops.create_card(
                project=args.project, task_type=args.task_type, title=args.title,
                description=desc, ref=args.ref, column=args.column,
                blocked_by=args.blocked_by, head=args.head, review_head=args.review_head,
                slug=args.slug, base_branch=args.base_branch, role=role, own_ref=args.own_ref))
        if args.cmd == "update":
            if not _need_role(role, ROLES):
                return 2
            return _emit(ops.update_card(
                role, args.ref, slug=args.slug,
                head=args.head, blocked_by=args.blocked_by,
                base_branch=args.base_branch, review_head=args.review_head))
        if args.cmd == "ready":
            if not _need_role(role, ROLES):
                return 2
            return _emit(ops.move_card(role, args.ref, "Ready"))
        if args.cmd == "move":
            if not _need_role(role, ROLES):
                return 2
            reason = _text_arg(args.reason, args.reason_file)
            return _emit(ops.move_card(role, args.ref, args.to_column, reason=reason))
        if args.cmd == "claim":
            if not _need_role(role, ("dispatcher",)):
                return 2
            return _emit(ops.claim_card(args.ref, args.worker, cap=args.cap))
        if args.cmd == "report":
            if not _need_role(role, ("worker",)):
                return 2
            return _emit(ops.report(args.ref, args.kind, _text_arg(args.body, args.body_file)))
        if args.cmd == "feedback":
            if not _need_role(role, ("worker",)):
                return 2
            return _emit(ops.feedback(args.ref, _text_arg(args.body, args.body_file)))
        if args.cmd == "verdict":
            if not _need_role(role, ("reviewer",)):
                return 2
            return _emit(ops.verdict(args.ref, args.kind, _text_arg(args.body, args.body_file)))
        if args.cmd == "idea":
            if not _need_role(role, ("reviewer", "retro")):
                return 2
            # The reviewer's one code-creation exception: findings out of the card's scope go to
            # Идеи (never Ready) so they enter the queue only via a human, not the reviewer.
            # retro's only board write is the same shape: a fail-pattern proposal, Идеи-only.
            desc = _text_arg(args.description, args.description_file)
            fn = ops.reviewer_idea if role == "reviewer" else ops.retro_idea
            return _emit(fn(
                project=args.project, task_type=args.task_type, title=args.title,
                description=desc, ref=args.ref, head=args.head, slug=args.slug))
        if args.cmd == "comment":
            if not _need_role(role, ROLES):
                return 2
            return _emit(ops.add_comment(role, args.ref, _text_arg(args.body, args.body_file)))
    except GuardError as e:
        _err(str(e))
        return 3
    except KanboardError as e:
        _err(str(e))
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
