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

Neither flag is required. Since issue:8b39e60e4df361c6138e a registered project's adapter names its
own broad suite, so ``check broad --reuse`` and ``check show`` with no shape flag run exactly the
suite that project declared. That is what makes the worker task packet able to print a real command
rather than the placeholder it used to. An explicit ``--module`` still wins, and a project that
declares no module and is given none is refused by name (``no_broad_check_module``) instead of
falling back to repository-wide discovery.

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
from secretary.config import ConfigError, load_config
from secretary.onboarding import DEFAULT_INSTANCE
from secretary.projects.contract import (
    LEGACY_IMPORT_PACKAGE,
    ContractUnusable,
    ModuleContract,
    module_contract,
)

_GIT_TIMEOUT = 60


class ResolvedCheck:
    """The executable check and the contract selection the caller should be able to see."""

    def __init__(self, spec: CheckSpec, module_contract: dict[str, str] | None = None) -> None:
        self.spec = spec
        self.module_contract = module_contract


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
        "--timeout-seconds",
        type=float,
        default=0.0,
        help="kill the check after this long; the receipt then records an incomplete run",
    )
    broad.add_argument(
        "--reuse",
        action="store_true",
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
    # Not `required=True` any more. Since issue:8b39e60e4df361c6138e the registered project's
    # adapter can name its own broad suite, and when it does, the whole point is that a worker (or
    # the prompt that tells a worker what to run) does not have to know the module name to run the
    # project's broad check. The group stays mutually exclusive — `--module` and `--command` are
    # still two different promises about import provenance — and neither flag falls back silently:
    # a project that declares no module and is given none fails with `no_broad_check_module`.
    shape = parser.add_mutually_exclusive_group()
    shape.add_argument(
        "--module",
        help="run `python -m MODULE` in this workspace; the standard shape, which attests the "
        "project the check process imported. Omitted, the registered project's declared broad "
        "suite is used",
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


def _spec(args: argparse.Namespace) -> ResolvedCheck:
    """The check this invocation names, from the flags and the registered project's contract.

    Three shapes reach here, and the order matters. `--command` is asked first because it resolves
    no contract at all: it attests nothing about imports, so there is nothing for an adapter to say
    about it. Otherwise the registered project's contract is resolved once, and an explicit
    `--module` overrides the suite it names while still taking its interpreter and import package —
    a worker debugging one module must not thereby run on a different runtime than the broad check
    does. With no `--module`, the suite comes from the adapter (issue:8b39e60e4df361c6138e), which
    is what lets the worker prompt print a real command instead of a placeholder.

    There is no fourth, silent shape. A project whose adapter names no module and an invocation
    that names none either is a usage error with a name on it, not a fallback to whatever
    repository-wide discovery happens to find.
    """
    if args.command:
        if args.module_arg:
            raise BroadCheckError("module_arg_without_module", "--module-arg needs --module")
        return ResolvedCheck(CheckSpec.for_shell(args.command))
    contract = _module_contract(Path(args.root), Path(args.instance))
    if args.module:
        module, module_args = args.module, list(args.module_arg)
    elif contract.module:
        if args.module_arg:
            raise BroadCheckError("module_arg_without_module", "--module-arg needs --module")
        module, module_args = contract.module, list(contract.args)
    else:
        raise BroadCheckError(
            "no_broad_check_module",
            "no --module or --command was given and this project's adapter declares no broad-check "
            "module; name the suite with --module, or declare `broad_check.module` in the adapter",
        )
    return ResolvedCheck(
        CheckSpec.for_module(
            module,
            module_args,
            interpreter=contract.interpreter,
            import_package=contract.import_package,
        ),
        contract.as_dict(),
    )


def _module_contract(root: Path, instance: Path) -> ModuleContract:
    """Return the registered project's usable module-check contract, or the legacy default.

    A worker's checkout is normally a git worktree, not the registered checkout itself. Comparing
    git common directories identifies the registered repository without guessing from its files;
    an ordinary unregistered checkout keeps the long-standing Secretary default for direct use.

    A registered project's contract is judged by `projects.contract`, the one implementation of
    those rules, and the dispatcher's preflight asks it the same question before a card is ever
    given to a worker (secretary-1458). This side maps its refusal onto the CLI error contract and
    never re-decides what a usable contract is.
    """
    binding, fallback_reason = _binding_for_workspace(root, instance)
    if binding is None:
        return ModuleContract(sys.executable, LEGACY_IMPORT_PACKAGE, fallback_reason)
    try:
        return module_contract(binding, instance=instance, project_root=root)
    except ContractUnusable as exc:
        raise BroadCheckError(exc.code, exc.message) from exc


def _binding_for_workspace(root: Path, instance: Path) -> tuple[dict[str, object] | None, str]:
    projects = instance / "projects"
    if not projects.is_dir():
        return None, "no_project_binding"
    enabled: list[dict[str, object]] = []
    disabled_match = False
    root_common = _git_common_dir(root)
    for path in sorted(projects.glob("*.yaml")):
        try:
            binding = load_config(path)
        except ConfigError:
            continue
        if not isinstance(binding, dict):
            continue
        repo = binding.get("repo")
        if not isinstance(repo, str) or not repo:
            continue
        if not _same_repository(
            root, Path(repo).expanduser(), first_common=root_common, first_common_known=True
        ):
            continue
        if binding.get("enabled") is True:
            enabled.append(binding)
        else:
            disabled_match = True
    if len(enabled) > 1:
        raise BroadCheckError("ambiguous_project", "workspace matches more than one registered project")
    if enabled:
        return enabled[0], ""
    return None, "project_binding_disabled" if disabled_match else "no_project_binding"


def _same_repository(
    first: Path,
    second: Path,
    *,
    first_common: Path | None = None,
    first_common_known: bool = False,
) -> bool:
    try:
        if first.resolve() == second.resolve():
            return True
    except OSError:
        return False
    if not first_common_known:
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
            timeout=_GIT_TIMEOUT,
        )
        common = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
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
        resolved = _spec(args)
        spec = resolved.spec
        if args.reuse:
            # The one authorization question, asked the one way `check show` asks it.
            lookup = usable_receipt(root, spec)
            authorized = lookup.authorized()
            if authorized is not None:
                payload = {
                    "reused": True,
                    "path": str(lookup.path),
                    "receipt": authorized,
                    "summary": summarize(authorized),
                }
                if resolved.module_contract is not None:
                    payload["module_contract"] = resolved.module_contract
                print(json.dumps(payload, sort_keys=True, indent=2))
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
    payload = {
        "reused": False,
        "path": str(receipt_path(root, spec)),
        "receipt": receipt,
        "summary": summarize(receipt),
    }
    if resolved.module_contract is not None:
        payload["module_contract"] = resolved.module_contract
    print(json.dumps(payload, sort_keys=True, indent=2))
    result = recorded_result(receipt)
    if result is None:  # unreachable: the writer records the model it just derived
        raise BroadCheckError("unrepresentable_result", "the check result could not be recorded")
    return result.shell_status


def run_check_show(args: argparse.Namespace) -> int:
    try:
        resolved = _spec(args)
        lookup = usable_receipt(Path(args.root), resolved.spec)
    except BroadCheckError as exc:
        return _fail(exc)
    payload = lookup.as_dict()
    if resolved.module_contract is not None:
        payload["module_contract"] = resolved.module_contract
    if lookup.receipt is not None:
        payload["summary"] = summarize(lookup.receipt)
    print(json.dumps(payload, sort_keys=True, indent=2))
    # `show` answers whether a run may be skipped, not what the check decided: 0 when a receipt is
    # authorized, 1 when it is not. The check's own status is in the receipt it prints.
    return 0 if lookup.authorized() is not None else 1
