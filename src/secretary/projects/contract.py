"""Whether a registered project's broad-check contract can attest that project.

A card's broad check is the one evidence a worker's round produces about its own code, and the
contract behind it — which suite it runs, which package the run must import and, where it matters,
which interpreter runs it — belongs to the registered project's adapter. Until secretary-1458 that contract was resolved lazily, inside
the workspace, by the worker itself: an adapter that could not name a usable one was discovered
after a workspace had been created, a head brought up and the card put in work, and the round was
already spent by the time anybody could read the refusal.

The rules are here, once, so both sides ask the same question of the same registry:

* the worker's own resolution (``secretary check broad --module``, in ``check_commands``), and
* the dispatcher's preflight before a card is given to a worker at all.

`decide` is the single decision point, and it answers with **three** named states, never with two
and a silence:

* ``fit`` — the contract is declared usably, and here it is;
* ``refused(shape)`` — one of the enumerated refusal shapes, which are enumerated once, below,
  and neither side may add a sixth on its own;
* ``undecidable(question)`` — the question cannot be answered without something the asker does not
  have. This is a state with a name, an evidence record and tests, not the absence of an answer.

The third one exists because leaving it unnamed is what made this card cost three rounds. The
adapter schema resolves a *relative* interpreter against the candidate workspace
(``schemas/adapter.schema.json``), and at preflight there is no candidate workspace. Round one had
the dispatcher test that interpreter in the registered checkout — an assertion about a different
directory, which approved contracts the worker then refused. Round two had the dispatcher say
nothing about it — and a silence is not a decision, so nobody could see or test what the caller
then did with it. Now the state is returned, and every caller must branch on it by name.

Which suite the check runs is part of that contract too, since issue:8b39e60e4df361c6138e. Only the
project can say what its broad suite is; without a place to say it, the worker task packet printed a
placeholder and the documentation answered it with repository-wide discovery, which runs every CI
suite in one process. A declared ``broad_check`` therefore names a ``module`` (and may name the exact
``args`` vector), and omitting it is `BROAD_CHECK_INCOMPLETE` rather than a quiet fallback.

What each state buys a card is settled here and not re-decided by a caller:

* ``refused`` always wins, and always before the card is issued. Its guarantee is the card's:
  no workspace, no head, no round spent.
* ``undecidable`` resolves in favour of **compatibility, not of saving the round**: the card goes
  to work, and the side that holds the tree answers the question there. A relative interpreter is
  the documented and recommended spelling; breaking that published promise to make an internal
  check convenient would move the product contract the wrong way, and absolute paths are
  machine-specific. The live cost of this today is zero: no adapter on the installation declares
  ``broad_check``, so no project reaches ``undecidable`` at all.
* ``fit`` goes to work, as it always did.

The other half of usability is asked relative to a project. An adapter that declares no
``broad_check`` falls back to the long-standing Secretary default — this interpreter, importing
``secretary`` — and for the Secretary project itself that default is a true contract: the checkout
being attested is exactly the sources that get imported. For any other project the same default
attests an installed copy of somebody else's package, which is a check that cannot fail for the
right reason and cannot pass for one either. So the question is: can this contract attest THIS
checkout, rather than does the adapter happen to spell the key.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.config import ConfigError, load_config, validate

# Enumerated refusal shapes shared by both sides.
ADAPTER_UNAVAILABLE = "adapter_unavailable"
ADAPTER_INVALID = "adapter_invalid"
BROAD_CHECK_INCOMPLETE = "broad_check_incomplete"
INTERPRETER_UNAVAILABLE = "interpreter_unavailable"
CANNOT_ATTEST_PROJECT = "cannot_attest_project"
CONTRACT_REFUSALS = (
    ADAPTER_UNAVAILABLE,
    ADAPTER_INVALID,
    BROAD_CHECK_INCOMPLETE,
    INTERPRETER_UNAVAILABLE,
    CANNOT_ATTEST_PROJECT,
)

# Enumerated answer states.
CONTRACT_FIT = "fit"
CONTRACT_REFUSED = "refused"
CONTRACT_UNDECIDABLE = "undecidable"
CONTRACT_STATES = (CONTRACT_FIT, CONTRACT_REFUSED, CONTRACT_UNDECIDABLE)

# Enumerated open questions: the registry cannot resolve this interpreter yet.
UNDECIDABLE_RELATIVE_INTERPRETER = "relative_interpreter"
# No registered project means no adapter contract to evaluate.
UNDECIDABLE_NO_REGISTERED_PROJECT = "no_registered_project"
# Lookup failure makes no claim about the project binding.
UNDECIDABLE_PROJECT_UNAVAILABLE = "project_unavailable"
UNDECIDABLE_QUESTIONS = (
    UNDECIDABLE_RELATIVE_INTERPRETER,
    UNDECIDABLE_NO_REGISTERED_PROJECT,
    UNDECIDABLE_PROJECT_UNAVAILABLE,
)

# Legacy default and its public diagnosis.
LEGACY_IMPORT_PACKAGE = "secretary"
LEGACY_REASON_MISSING_BROAD_CHECK = "adapter_missing_broad_check"

# CLI names for refusals; preflight preserves the existing missing-interpreter code.
WORKER_ERROR_CODES = {
    ADAPTER_UNAVAILABLE: "invalid_project_adapter",
    ADAPTER_INVALID: "invalid_project_adapter",
    BROAD_CHECK_INCOMPLETE: "invalid_project_adapter",
    INTERPRETER_UNAVAILABLE: "interpreter_start_failed",
    CANNOT_ATTEST_PROJECT: "invalid_project_adapter",
}


@dataclass(frozen=True)
class ModuleContract:
    """The adapter-owned module-check runtime and suite, plus any legacy-default diagnosis.

    `module` and `args` are the half added for issue:8b39e60e4df361c6138e. Before them the contract
    could say which interpreter runs a broad check and which package it must import, but not WHICH
    SUITE it runs — so the worker task packet printed the literal placeholder
    ``<this project's broad suite module>`` and the documentation answered it with repository-wide
    discovery, a run that costs every CI suite in one process. Only the project can name its own
    broad suite, so the project's adapter is where it is named.

    `module` is empty exactly when the contract comes from the legacy default, which names no suite:
    a declared ``broad_check`` that omits it is `BROAD_CHECK_INCOMPLETE`.
    """

    interpreter: str
    import_package: str
    reason: str = ""
    module: str = ""
    args: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, str]:
        if self.reason:
            return {"source": "legacy_default", "reason": self.reason}
        return {"source": "adapter"}


class ContractUnusable(Exception):
    """One registered project's broad-check contract, and why it cannot attest that project."""

    def __init__(self, shape: str, adapter: str, message: str) -> None:
        super().__init__(message)
        self.shape = shape
        self.adapter = adapter
        self.message = message

    @property
    def code(self) -> str:
        """The worker CLI's error code for this shape."""
        return WORKER_ERROR_CODES[self.shape]

    def evidence(self) -> dict[str, str]:
        """What is missing, in the words an outcome carries: the adapter and the exact shape."""
        return {"shape": self.shape, "adapter": self.adapter, "detail": self.message}

    def detail(self) -> str:
        return f"{self.message} [broad-check contract refusal: {self.shape}]"


class ContractStateError(Exception):
    """A verdict reached a caller that cannot act on it. Never a default allow, never a silence."""


@dataclass(frozen=True)
class ContractVerdict:
    """The answer `decide` gives: one of three named states, each carrying its own evidence.

    Built only through the three constructors below, so a state can never arrive without the thing
    that makes it readable — a refusal without its shape, or an open question without its name.
    """

    state: str
    adapter: str = ""
    contract: ModuleContract | None = None
    refusal: ContractUnusable | None = None
    question: str = ""
    detail: str = ""

    @classmethod
    def as_fit(cls, contract: ModuleContract, adapter: str) -> ContractVerdict:
        return cls(state=CONTRACT_FIT, adapter=adapter, contract=contract)

    @classmethod
    def as_refused(cls, shape: str, adapter: str, message: str) -> ContractVerdict:
        if shape not in CONTRACT_REFUSALS:
            raise ContractStateError(f"unknown refusal shape {shape!r}")
        return cls(
            state=CONTRACT_REFUSED,
            adapter=adapter,
            refusal=ContractUnusable(shape, adapter, message),
            detail=message,
        )

    @classmethod
    def as_undecidable(cls, question: str, adapter: str, detail: str) -> ContractVerdict:
        if question not in UNDECIDABLE_QUESTIONS:
            raise ContractStateError(f"unknown open question {question!r}")
        return cls(state=CONTRACT_UNDECIDABLE, adapter=adapter, question=question, detail=detail)

    @property
    def fit(self) -> bool:
        return self.state == CONTRACT_FIT

    @property
    def refused(self) -> bool:
        return self.state == CONTRACT_REFUSED

    @property
    def undecidable(self) -> bool:
        return self.state == CONTRACT_UNDECIDABLE

    def evidence(self) -> dict[str, str]:
        """What this verdict says, in the words an outcome or a log carries."""
        if self.refused and self.refusal is not None:
            return self.refusal.evidence()
        record = {"state": self.state, "adapter": self.adapter, "detail": self.detail}
        if self.undecidable:
            record["question"] = self.question
        return record


def decide(
    binding: dict[str, Any],
    *,
    instance: Path,
    project_root: Path,
    workspace: Path | None,
) -> ContractVerdict:
    """The one decision point about one registered project's broad-check contract.

    `workspace` is the tree the check will actually run in, or ``None`` when no candidate workspace
    exists yet. The workspace-independent questions are asked first and a refusal among them always
    wins; only once they are all answered can what is left be `undecidable`.

    Cheap and offline by construction: the binding it was handed, the adapter beside it, and the
    project's own checkout. Nothing here starts a process, creates a workspace or brings up a head,
    which is what lets the dispatcher ask it before a claim.
    """
    adapter_name = binding.get("adapter")
    if not isinstance(adapter_name, str) or not adapter_name:
        return ContractVerdict.as_refused(ADAPTER_UNAVAILABLE, "", "registered project has no adapter")
    try:
        adapter = load_config(Path(instance) / "adapters" / f"{adapter_name}.yaml")
    except ConfigError:
        return ContractVerdict.as_refused(
            ADAPTER_UNAVAILABLE, adapter_name, f"adapter {adapter_name!r} is unavailable"
        )
    if not isinstance(adapter, dict) or validate(adapter, "adapter", f"{adapter_name}.yaml"):
        return ContractVerdict.as_refused(
            ADAPTER_INVALID, adapter_name, f"adapter {adapter_name!r} is invalid"
        )
    configured = adapter.get("broad_check")
    if configured is None:
        return _legacy_default(adapter_name, Path(project_root))
    return _declared_contract(configured, adapter_name, workspace)


def _legacy_default(adapter_name: str, project_root: Path) -> ContractVerdict:
    """The contract an adapter that declares none falls back to, judged against this checkout."""
    contract = ModuleContract(sys.executable, LEGACY_IMPORT_PACKAGE, LEGACY_REASON_MISSING_BROAD_CHECK)
    # The default names the running interpreter and is always answerable.
    if not _executable(contract.interpreter):
        return ContractVerdict.as_refused(
            INTERPRETER_UNAVAILABLE,
            adapter_name,
            f"could not start configured interpreter {contract.interpreter!r}: it is not an executable file",
        )
    if not _attests(project_root, contract.import_package):
        # Only the legacy default uses checkout layout; declared contracts use receipt provenance.
        return ContractVerdict.as_refused(
            CANNOT_ATTEST_PROJECT,
            adapter_name,
            f"adapter {adapter_name!r} declares no broad-check contract, so the legacy default "
            f"attests package {contract.import_package!r}, which is not part of this project's "
            f"checkout {project_root}",
        )
    return ContractVerdict.as_fit(contract, adapter_name)


def _declared_contract(
    configured: Any,
    adapter_name: str,
    workspace: Path | None,
) -> ContractVerdict:
    """The contract an adapter declares for itself: complete, runnable, or an open question."""
    if not isinstance(configured, dict):  # schema validation above normally catches this.
        return ContractVerdict.as_refused(
            BROAD_CHECK_INCOMPLETE,
            adapter_name,
            f"adapter {adapter_name!r} has no broad-check contract",
        )
    import_package = str(configured.get("import_package") or "").strip()
    module = str(configured.get("module") or "").strip()
    if not import_package or not module:
        return ContractVerdict.as_refused(
            BROAD_CHECK_INCOMPLETE,
            adapter_name,
            f"adapter {adapter_name!r} has no broad-check contract",
        )
    raw_args = configured.get("args")
    if raw_args is None:
        args: tuple[str, ...] = ()
    elif isinstance(raw_args, list) and all(isinstance(entry, str) for entry in raw_args):
        args = tuple(raw_args)
    else:  # schema validation above normally catches this.
        return ContractVerdict.as_refused(
            BROAD_CHECK_INCOMPLETE,
            adapter_name,
            f"adapter {adapter_name!r} declares broad-check arguments that are not a list of strings",
        )
    # An omitted interpreter is not an incomplete contract, it is the common case. PR #329 made the
    # check subprocess prepend the candidate workspace's own import roots to `sys.path` before it
    # imports the project, so the interpreter that runs the wrapper imports the CANDIDATE and not
    # whatever a shared editable installation points at. That is what lets a project whose worktrees
    # have no venv of their own declare a supported contract at all: requiring `workspace/.venv`
    # here would demand a directory the Secretary worktrees do not have and are not getting
    # (issue:8b39e60e4df361c6138e). A declared interpreter still means exactly what it always did.
    if "interpreter" not in configured:
        return ContractVerdict.as_fit(
            ModuleContract(sys.executable, import_package, module=module, args=args), adapter_name
        )
    interpreter = str(configured.get("interpreter") or "").strip()
    if not interpreter:
        # Present but blank is a typo, not an omission: it names nothing runnable, and answering it
        # with the wrapper's interpreter would silently run a contract nobody wrote down.
        return ContractVerdict.as_refused(
            BROAD_CHECK_INCOMPLETE,
            adapter_name,
            f"adapter {adapter_name!r} has no broad-check contract",
        )
    interpreter_path = Path(interpreter)
    if not interpreter_path.is_absolute():
        if workspace is None:
            return ContractVerdict.as_undecidable(
                UNDECIDABLE_RELATIVE_INTERPRETER,
                adapter_name,
                f"adapter {adapter_name!r} names interpreter {interpreter!r}, which the adapter "
                "schema resolves from the candidate workspace; no candidate workspace exists yet, "
                "so the tree that will run the check is the only side that can answer this",
            )
        # Preserve a venv symlink: resolving it loses its site paths.
        interpreter = str(Path(workspace).resolve() / interpreter_path)
    if not _executable(interpreter):
        return ContractVerdict.as_refused(
            INTERPRETER_UNAVAILABLE,
            adapter_name,
            f"could not start configured interpreter {interpreter!r}: it is not an executable file",
        )
    return ContractVerdict.as_fit(
        ModuleContract(interpreter, import_package, module=module, args=args), adapter_name
    )


def contract_of(verdict: ContractVerdict) -> ModuleContract:
    """The contract to run, for the side that holds the tree. Exhaustive over the three states.

    There is no branch here that turns an unanswered question into permission: a verdict this side
    cannot act on raises rather than falling through. `decide` leaves no question open once it is
    given a workspace, so `undecidable` reaching here is a programming error and is reported as one.
    """
    if verdict.refused and verdict.refusal is not None:
        raise verdict.refusal
    if verdict.fit and verdict.contract is not None:
        return verdict.contract
    if verdict.undecidable:
        raise ContractStateError(
            f"the broad-check contract of adapter {verdict.adapter!r} is undecidable "
            f"({verdict.question}) on a side that holds the tree: {verdict.detail}"
        )
    raise ContractStateError(f"unreadable contract verdict {verdict.state!r}")


def module_contract(binding: dict[str, Any], *, instance: Path, project_root: Path) -> ModuleContract:
    """The worker's own resolution: the contract to run in THIS tree, or ``ContractUnusable``.

    `project_root` is the workspace the check will run in, which is what the adapter schema means
    when it resolves a relative interpreter. Having that tree, this side leaves no question open —
    and it does not re-decide anything either: the state it acts on is the one `decide` returned.
    """
    return contract_of(decide(binding, instance=instance, project_root=project_root, workspace=project_root))


def _executable(interpreter: str) -> bool:
    try:
        return os.access(interpreter, os.X_OK) and Path(interpreter).is_file()
    except OSError:
        return False


def _attests(project_root: Path, import_package: str) -> bool:
    """Whether the package the check must import has its sources in this project's checkout.

    A check that imports the project attests the project; a check that imports something the
    checkout does not contain attests whatever the interpreter's environment happens to hold. The
    common layouts are covered directly — flat, ``src/``, and one level of subdirectory for a
    repository that holds its service beside other things — rather than by importing anything.
    """
    top = import_package.split(".", 1)[0]
    if not top:
        return False
    try:
        roots = [project_root, project_root / "src"]
        for child in sorted(project_root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                roots += [child, child / "src"]
    except OSError:
        return False
    return any((root / top / "__init__.py").is_file() or (root / f"{top}.py").is_file() for root in roots)
