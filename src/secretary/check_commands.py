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
import os
import subprocess
import sys
from pathlib import Path

from secretary.broad_check import (
    BroadCheckError,
    CheckSpec,
    receipt_path,
    recorded_result,
    run_broad_check,
    summarize,
    usable_receipt,
)
from secretary.config import ConfigError, load_config, validate
from secretary.onboarding import DEFAULT_INSTANCE


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
    parser.add_argument(
        "--instance",
        default=os.environ.get("SECRETARY_INSTANCE", DEFAULT_INSTANCE),
        help="registered project adapters (default: SECRETARY_INSTANCE or the default instance)",
    )
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


def _spec(args: argparse.Namespace) -> CheckSpec:
    if args.module:
        interpreter, import_package = _module_contract(Path(args.root), Path(args.instance))
        return CheckSpec.for_module(
            args.module,
            args.module_arg,
            interpreter=interpreter,
            import_package=import_package,
        )
    if args.module_arg:
        raise BroadCheckError("module_arg_without_module", "--module-arg needs --module")
    return CheckSpec.for_shell(args.command)


def _module_contract(root: Path, instance: Path) -> tuple[str, str]:
    """Return the registered project's explicit module-check contract, or the legacy default.

    A worker's checkout is normally a git worktree, not the registered checkout itself. Comparing
    git common directories identifies the registered repository without guessing from its files;
    an ordinary unregistered checkout keeps the long-standing Secretary default for direct use.
    """
    binding = _binding_for_workspace(root, instance)
    if binding is None:
        return sys.executable, "secretary"
    adapter_name = binding.get("adapter")
    if not isinstance(adapter_name, str) or not adapter_name:
        raise BroadCheckError("invalid_project_adapter", "registered project has no adapter")
    try:
        adapter = load_config(instance / "adapters" / f"{adapter_name}.yaml")
    except ConfigError as exc:
        raise BroadCheckError("invalid_project_adapter", f"adapter {adapter_name!r} is unavailable") from exc
    if not isinstance(adapter, dict) or validate(adapter, "adapter", f"{adapter_name}.yaml"):
        raise BroadCheckError("invalid_project_adapter", f"adapter {adapter_name!r} is invalid")
    configured = adapter.get("broad_check")
    if configured is None:
        return sys.executable, "secretary"
    if not isinstance(configured, dict):  # schema validation above normally catches this.
        raise BroadCheckError("invalid_project_adapter", f"adapter {adapter_name!r} has no broad-check contract")
    interpreter = str(configured.get("interpreter") or "").strip()
    import_package = str(configured.get("import_package") or "").strip()
    if not interpreter or not import_package:
        raise BroadCheckError("invalid_project_adapter", f"adapter {adapter_name!r} has no broad-check contract")
    interpreter_path = Path(interpreter)
    if not interpreter_path.is_absolute():
        # Keep a venv's symlink spelling. Resolving the final component would turn
        # `.venv/bin/python` into the base interpreter and discard that environment's site paths.
        interpreter = str(root.resolve() / interpreter_path)
    return interpreter, import_package


def _binding_for_workspace(root: Path, instance: Path) -> dict[str, object] | None:
    projects = instance / "projects"
    if not projects.is_dir():
        return None
    candidates: list[dict[str, object]] = []
    for path in sorted(projects.glob("*.yaml")):
        try:
            binding = load_config(path)
        except ConfigError:
            continue
        if not isinstance(binding, dict) or binding.get("enabled") is not True:
            continue
        repo = binding.get("repo")
        if isinstance(repo, str) and repo and _same_repository(root, Path(repo).expanduser()):
            candidates.append(binding)
    if len(candidates) > 1:
        raise BroadCheckError("ambiguous_project", "workspace matches more than one registered project")
    return candidates[0] if candidates else None


def _same_repository(first: Path, second: Path) -> bool:
    if first.resolve() == second.resolve():
        return True
    first_common = _git_common_dir(first)
    second_common = _git_common_dir(second)
    return first_common is not None and first_common == second_common


def _git_common_dir(root: Path) -> Path | None:
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        common = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    if top.returncode != 0 or common.returncode != 0:
        return None
    top_path = Path(top.stdout.strip())
    common_path = Path(common.stdout.strip())
    if not top_path.is_dir() or not str(common_path):
        return None
    return (common_path if common_path.is_absolute() else top_path / common_path).resolve()


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
                # A receipt that stands in for the run hands back the result that run had, taken
                # from the canonical model the load boundary reconstructed — never from a raw
                # field this command read for itself.
                return lookup.authorized_result().shell_status
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
    result = recorded_result(receipt)
    if result is None:  # unreachable: the writer records the model it just derived
        raise BroadCheckError("unrepresentable_result", "the check result could not be recorded")
    return result.shell_status


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
