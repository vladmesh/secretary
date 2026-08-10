"""The documented worker command for a broad check and its receipt.

``secretary check broad`` is the one form a worker is asked to use, so that a broad run always
leaves evidence behind; ``secretary check show`` answers, without running anything, whether that
evidence still describes the code in the checkout.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from secretary.broad_check import (
    BroadCheckError,
    receipt_path,
    run_broad_check,
    summarize,
    usable_receipt,
)


def add_check_subcommands(subparsers) -> None:
    check = subparsers.add_parser(
        "check", help="run a broad check with a workspace-local receipt, or read that receipt"
    )
    check_sub = check.add_subparsers(dest="check_command")

    broad = check_sub.add_parser(
        "broad", help="run one broad check, streaming its combined output and writing a receipt"
    )
    _common(broad)
    broad.add_argument("--command", required=True, help="the broad command, run through bash -lc")
    broad.add_argument(
        "--timeout-seconds", type=float, default=0.0,
        help="kill the command after this long; the receipt then records an incomplete run",
    )
    broad.add_argument(
        "--reuse", action="store_true",
        help="skip the run when an intact receipt already describes this exact content",
    )
    broad.set_defaults(handler=run_check_broad)

    show = check_sub.add_parser("show", help="report whether a receipt covers the current content")
    _common(show)
    show.add_argument("--command", required=True)
    show.set_defaults(handler=run_check_show)

    check.set_defaults(handler=_missing("check subcommand required"))


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="workspace root; the receipt lives under it")


def _missing(message: str):
    def handler(_args: argparse.Namespace) -> int:
        print(json.dumps({"error": {"code": "usage", "message": message}}))
        return 2
    return handler


def _fail(exc: BroadCheckError) -> int:
    print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=sys.stderr)
    return 2


def run_check_broad(args: argparse.Namespace) -> int:
    """Run the check and hand back its own exit status, never a status of our own invention."""
    root = Path(args.root)
    try:
        if args.reuse:
            lookup = usable_receipt(root, args.command)
            if lookup.usable and lookup.receipt is not None:
                print(json.dumps(
                    {"reused": True, "path": str(lookup.path), "receipt": lookup.receipt,
                     "summary": summarize(lookup.receipt)},
                    sort_keys=True, indent=2,
                ))
                return 0 if lookup.receipt.get("verdict") == "passed" else 1
        # The command's combined output goes to stderr so it stays visible live while stdout
        # keeps carrying exactly one JSON document, as every other command here does.
        exit_code, receipt = run_broad_check(
            args.command,
            root=root,
            stream=sys.stderr,
            timeout_seconds=args.timeout_seconds or None,
        )
    except BroadCheckError as exc:
        return _fail(exc)
    print(json.dumps(
        {"reused": False, "path": str(receipt_path(root, args.command)), "receipt": receipt,
         "summary": summarize(receipt)},
        sort_keys=True, indent=2,
    ))
    # A signal-killed command has no portable exit status of its own; the shell convention keeps
    # it distinguishable from any ordinary failure instead of flattening it to 1.
    return exit_code if exit_code >= 0 else 128 - exit_code


def run_check_show(args: argparse.Namespace) -> int:
    lookup = usable_receipt(Path(args.root), args.command)
    payload = lookup.as_dict()
    if lookup.usable and lookup.receipt is not None:
        payload["summary"] = summarize(lookup.receipt)
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0 if lookup.usable else 1
