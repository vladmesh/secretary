"""Public CLI for the Product and Issue board records."""
from __future__ import annotations

import argparse
import json
import os

from secretary.onboarding import DEFAULT_INSTANCE
from secretary.product_issues import ProductIssueStore
from secretary.task_commands import resolve_data_dir
from secretary.tasks import KanboardClient, TaskError


def _common(parser: argparse.ArgumentParser, *, write: bool = False) -> None:
    parser.add_argument("--instance", default=os.environ.get("SECRETARY_INSTANCE", DEFAULT_INSTANCE))
    parser.add_argument("--data-dir", default=os.environ.get("SECRETARY_DATA_DIR"))
    if write:
        parser.add_argument("--role", required=True, choices=("po",))
        parser.add_argument("--actor", default=os.environ.get("BOARD_ACTOR", "po"))
        parser.add_argument("--request-id")


def add_product_issue_subcommands(subparsers) -> None:
    product = subparsers.add_parser("product", help="manage durable Product records")
    product_sub = product.add_subparsers(dest="product_command")
    create = product_sub.add_parser("create")
    _common(create, write=True)
    create.add_argument("--id", required=True)
    create.add_argument("--project", action="append", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--description", default="")
    create.set_defaults(handler=run_product_create)
    listing = product_sub.add_parser("list")
    _common(listing)
    listing.set_defaults(handler=run_product_list)
    show = product_sub.add_parser("show")
    _common(show)
    show.add_argument("--id", required=True)
    show.set_defaults(handler=run_product_show)
    product.set_defaults(handler=_missing("product subcommand required"))

    issue = subparsers.add_parser("issue", help="manage durable Product issues")
    issue_sub = issue.add_subparsers(dest="issue_command")
    create = issue_sub.add_parser("create")
    _common(create, write=True)
    create.add_argument("--product", required=True)
    create.add_argument("--kind", required=True, choices=("bug", "feature", "question", "improvement"))
    create.add_argument("--priority", required=True, choices=("P0", "P1", "P2", "P3"))
    create.add_argument("--title", required=True)
    create.add_argument("--description", default="")
    create.set_defaults(handler=run_issue_create)
    listing = issue_sub.add_parser("list")
    _common(listing)
    listing.add_argument("--product")
    listing.add_argument("--closed", action="store_true")
    listing.set_defaults(handler=run_issue_list)
    show = issue_sub.add_parser("show")
    _common(show)
    show.add_argument("--ref", required=True)
    show.set_defaults(handler=run_issue_show)
    priority = issue_sub.add_parser("update-priority")
    _common(priority, write=True)
    priority.add_argument("--ref", required=True)
    priority.add_argument("--priority", required=True, choices=("P0", "P1", "P2", "P3"))
    priority.add_argument("--reason", required=True)
    priority.set_defaults(handler=run_issue_priority)
    close = issue_sub.add_parser("close")
    _common(close, write=True)
    close.add_argument("--ref", required=True)
    close.add_argument("--reason", required=True, choices=("resolved", "invalid", "duplicate", "wont_do"))
    close.set_defaults(handler=run_issue_close)
    issue.set_defaults(handler=_missing("issue subcommand required"))


def _missing(message: str):
    def handler(_args: argparse.Namespace) -> int:
        print(json.dumps({"error": {"code": "usage", "message": message}}))
        return 2
    return handler


def _store(args: argparse.Namespace) -> ProductIssueStore:
    return ProductIssueStore(KanboardClient(), data_dir=resolve_data_dir(args), instance=args.instance)


def _run(args: argparse.Namespace, callback) -> int:
    try:
        output = callback(_store(args))
    except TaskError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=os.sys.stderr)
        return exc.exit_code
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


def run_product_create(args):
    return _run(args, lambda store: store.create_product(product_id=args.id, projects=args.project, title=args.title, description=args.description, actor=args.actor, request_id=args.request_id))
def run_product_list(args): return _run(args, lambda store: store.list_products())
def run_product_show(args): return _run(args, lambda store: store.show_product(args.id))
def run_issue_create(args):
    return _run(args, lambda store: store.create_issue(product=args.product, issue_kind=args.kind, priority=args.priority, title=args.title, description=args.description, actor=args.actor, request_id=args.request_id))
def run_issue_list(args): return _run(args, lambda store: store.list_issues(product=args.product, include_closed=args.closed))
def run_issue_show(args): return _run(args, lambda store: store.show_issue(args.ref))
def run_issue_priority(args):
    return _run(args, lambda store: store.update_priority(reference=args.ref, priority=args.priority, reason=args.reason, actor=args.actor, request_id=args.request_id))
def run_issue_close(args):
    return _run(args, lambda store: store.close_issue(reference=args.ref, reason=args.reason, actor=args.actor, request_id=args.request_id))
