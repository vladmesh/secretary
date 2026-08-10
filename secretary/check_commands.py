"""The documented worker command for a broad check and its receipt.

``secretary check broad`` is the one form a worker is asked to use, so that a broad run always
leaves evidence behind; ``secretary check show`` answers, without running anything, whether that
evidence still describes the code in the checkout.

Two check shapes are accepted, and they differ in exactly one promise. ``--module`` is the
documented standard shape: this command builds the argv itself, so the suite runs in a process that
records its own import provenance and the receipt can be reused while the checkout is unchanged.
``--command`` accepts any shell a project needs and attests nothing about imports, because the
shell may change directory or import environment before an interpreter starts; its receipt is a
summary to read, never a substitute for running the check again.

Reuse is authorized in exactly one place — ``usable_receipt``, read through
``ReceiptLookup.authorized()``. Both commands here ask that one question, so ``check show`` cannot
report "not usable" while ``--reuse`` quietly skips the run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from secretary.broad_check import (
    BroadCheckError,
    CheckSpec,
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
    broad.add_argument(
        "--timeout-seconds", type=float, default=0.0,
        help="kill the check after this long; the receipt then records an incomplete run",
    )
    broad.add_argument(
        "--reuse", action="store_true",
        help="skip the run when an intact receipt already describes this exact content",
    )
    broad.set_defaults(handler=run_check_broad)

    show = check_sub.add_parser("show", help="report whether a receipt covers the current content")
    _common(show)
    show.set_defaults(handler=run_check_show)

    check.set_defaults(handler=_missing("check subcommand required"))


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="workspace root; the receipt lives under it")
    shape = parser.add_mutually_exclusive_group(required=True)
    shape.add_argument(
        "--module",
        help="run `python -m MODULE` in this workspace; the standard shape, which attests the "
             "project the check process imported",
    )
    shape.add_argument(
        "--command",
        help="run an arbitrary command through bash -lc; its receipt attests no import provenance "
             "and is never reused in place of a run",
    )
    parser.add_argument(
        "--module-arg", action="append", default=[], help="argument passed to --module, repeatable"
    )


def _missing(message: str):
    def handler(_args: argparse.Namespace) -> int:
        print(json.dumps({"error": {"code": "usage", "message": message}}))
        return 2
    return handler


def _fail(exc: BroadCheckError) -> int:
    print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=sys.stderr)
    return 2


def _shell_status(exit_code: int) -> int:
    """Turn one recorded process result into this command's exit status.

    A usable receipt substitutes for rerunning the exact check, so it has to answer the question
    the run answered — including `2` for a usage failure or `3` for a tool-specific one.  Flattening
    every non-passing outcome to `1` loses precisely the fact the caller came for (secretary-1406
    review).  A signal has no portable exit status of its own, so it becomes the shell's `128+N`,
    and it does so here whether the result was just observed or read back out of a receipt.
    """
    return exit_code if exit_code >= 0 else 128 - exit_code


def _spec(args: argparse.Namespace) -> CheckSpec:
    if args.module:
        return CheckSpec.for_module(args.module, args.module_arg)
    if args.module_arg:
        raise BroadCheckError("module_arg_without_module", "--module-arg needs --module")
    return CheckSpec.for_shell(args.command)


def run_check_broad(args: argparse.Namespace) -> int:
    """Run the check and hand back its own exit status, never a status of our own invention."""
    root = Path(args.root)
    try:
        spec = _spec(args)
        if args.reuse:
            # The one authorization question, asked the one way `check show` asks it.
            lookup = usable_receipt(root, spec)
            authorized = lookup.authorized()
            if authorized is not None:
                print(json.dumps(
                    {"reused": True, "path": str(lookup.path), "receipt": authorized,
                     "summary": summarize(authorized)},
                    sort_keys=True, indent=2,
                ))
                # A receipt that stands in for the run hands back the result that run had.
                return _shell_status(int(authorized["exit_code"]))
        # The check's combined output goes to stderr so it stays visible live while stdout keeps
        # carrying exactly one JSON document, as every other command here does.
        exit_code, receipt = run_broad_check(
            spec,
            root=root,
            stream=sys.stderr,
            timeout_seconds=args.timeout_seconds or None,
        )
    except BroadCheckError as exc:
        return _fail(exc)
    print(json.dumps(
        {"reused": False, "path": str(receipt_path(root, spec)), "receipt": receipt,
         "summary": summarize(receipt)},
        sort_keys=True, indent=2,
    ))
    return _shell_status(exit_code)


def run_check_show(args: argparse.Namespace) -> int:
    try:
        lookup = usable_receipt(Path(args.root), _spec(args))
    except BroadCheckError as exc:
        return _fail(exc)
    payload = lookup.as_dict()
    if lookup.receipt is not None:
        payload["summary"] = summarize(lookup.receipt)
    print(json.dumps(payload, sort_keys=True, indent=2))
    # `show` answers whether a run may be skipped, not what the check decided: 0 when a receipt is
    # authorized, 1 when it is not. The check's own status is in the receipt it prints.
    return 0 if lookup.authorized() is not None else 1
