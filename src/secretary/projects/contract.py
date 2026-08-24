"""Whether a registered project's broad-check contract can attest that project.

A card's broad check is the one evidence a worker's round produces about its own code, and the
contract behind it — which interpreter runs it and which package the run must import — belongs to
the registered project's adapter. Until secretary-1458 that contract was resolved lazily, inside
the workspace, by the worker itself: an adapter that could not name a usable one was discovered
after a workspace had been created, a head brought up and the card put in work, and the round was
already spent by the time anybody could read the refusal.

The rules are here, once, so both sides ask the same question of the same registry:

* the worker's own resolution (``secretary check broad --module``, in ``check_commands``), and
* the dispatcher's preflight before a card is given to a worker at all.

The refusal shapes are enumerated once, below, and neither side may add a sixth on its own.

The last shape is the one that is not about the file being broken. An adapter that declares no
``broad_check`` falls back to the long-standing Secretary default — this interpreter, importing
``secretary`` — and for the Secretary project itself that default is a true contract: the checkout
being attested is exactly the sources that get imported. For any other project the same default
attests an installed copy of somebody else's package, which is a check that cannot fail for the
right reason and cannot pass for one either. So usability is asked relative to a project: can this
contract attest THIS checkout, rather than does the adapter happen to spell the key.

The rules are one implementation; what differs between the two callers is not the rules but the
set of questions each is in a position to ask. The adapter schema resolves a *relative*
interpreter against the candidate workspace (``schemas/adapter.schema.json``), and at preflight no
candidate workspace exists yet — so "does that interpreter exist" is a question only the side
holding the tree can answer. `preflight` therefore answers everything a workspace is not needed
for and stops there; `module_contract` is the worker's own resolution, which has the tree and
answers the rest. Neither side may state something about a tree it is not looking at: testing a
relative interpreter in the registered checkout would be an assertion about a different directory,
and it is exactly that assertion which approved contracts the worker then refused.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.config import ConfigError, load_config, validate

# --- The enumerated refusal shapes. Both sides read these; nobody invents a sixth. ---------------
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

# The default that predates any adapter contract, and the diagnosis a caller shows for it.
LEGACY_IMPORT_PACKAGE = "secretary"
LEGACY_REASON_MISSING_BROAD_CHECK = "adapter_missing_broad_check"

# What the worker's CLI calls each refusal. A missing interpreter keeps the code the worker already
# reported for exactly this condition when it discovered it by trying to start the process; the
# preflight only reports it earlier, and never with a second name.
WORKER_ERROR_CODES = {
    ADAPTER_UNAVAILABLE: "invalid_project_adapter",
    ADAPTER_INVALID: "invalid_project_adapter",
    BROAD_CHECK_INCOMPLETE: "invalid_project_adapter",
    INTERPRETER_UNAVAILABLE: "interpreter_start_failed",
    CANNOT_ATTEST_PROJECT: "invalid_project_adapter",
}


@dataclass(frozen=True)
class ModuleContract:
    """The adapter-owned module-check runtime, plus any legacy-default diagnosis."""

    interpreter: str
    import_package: str
    reason: str = ""

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


def module_contract(binding: dict[str, Any], *, instance: Path, project_root: Path) -> ModuleContract:
    """The worker's own resolution: the contract to run in THIS tree, or ``ContractUnusable``.

    `project_root` is the workspace the check will run in, which is what the adapter schema means
    when it resolves a relative interpreter. Having that tree, this side answers every question —
    including whether the interpreter the contract names is actually there.
    """
    return _usable_contract(
        binding, instance=instance, project_root=project_root, workspace=project_root
    )


def preflight(binding: dict[str, Any], *, instance: Path, project_root: Path) -> None:
    """Whether a card may be given to a worker at all, or ``ContractUnusable`` (secretary-1458).

    Cheap and offline by construction: the binding it was handed, the adapter beside it, and the
    registered checkout's own layout. Nothing here starts a process, creates a workspace or brings
    up a head, which is what lets the dispatcher ask it before a claim.

    It answers only what a candidate workspace is not needed for. A relative interpreter is
    resolved from that workspace, and no workspace exists yet, so this side neither approves nor
    refuses on it: the tree that will run the check is the only place that question has an answer,
    and the worker's own resolution asks it there.
    """
    _usable_contract(binding, instance=instance, project_root=project_root, workspace=None)


def _usable_contract(
    binding: dict[str, Any], *, instance: Path, project_root: Path, workspace: Path | None,
) -> ModuleContract:
    """The one implementation of the rules. `workspace` is the tree the check will run in, or None
    when no candidate workspace exists yet and the workspace-dependent question is not this
    caller's to answer."""
    adapter_name = binding.get("adapter")
    if not isinstance(adapter_name, str) or not adapter_name:
        raise ContractUnusable(ADAPTER_UNAVAILABLE, "", "registered project has no adapter")
    try:
        adapter = load_config(Path(instance) / "adapters" / f"{adapter_name}.yaml")
    except ConfigError as exc:
        raise ContractUnusable(
            ADAPTER_UNAVAILABLE, adapter_name, f"adapter {adapter_name!r} is unavailable"
        ) from exc
    if not isinstance(adapter, dict) or validate(adapter, "adapter", f"{adapter_name}.yaml"):
        raise ContractUnusable(
            ADAPTER_INVALID, adapter_name, f"adapter {adapter_name!r} is invalid"
        )
    contract, interpreter_answerable = _declared_contract(adapter, adapter_name, workspace)
    if interpreter_answerable and not _executable(contract.interpreter):
        raise ContractUnusable(
            INTERPRETER_UNAVAILABLE, adapter_name,
            f"could not start configured interpreter {contract.interpreter!r}: "
            "it is not an executable file",
        )
    if contract.reason and not _attests(Path(project_root), contract.import_package):
        # Only the legacy default is judged against the checkout's own layout. An adapter that
        # declares a contract has stated which package attests that project, and OPERATIONS.md
        # promises that statement is honoured rather than second-guessed by a layout heuristic;
        # what such a contract actually imported is still checked by the receipt's provenance. The
        # default states nothing about this project: it names Secretary's package for every
        # registered project alike, so for a checkout that does not hold those sources the check it
        # buys attests an installed copy of somebody else's code.
        raise ContractUnusable(
            CANNOT_ATTEST_PROJECT, adapter_name,
            f"adapter {adapter_name!r} declares no broad-check contract, so the legacy default "
            f"attests package {contract.import_package!r}, which is not part of this project's "
            f"checkout {Path(project_root)}",
        )
    return contract


def _declared_contract(
    adapter: dict[str, Any], adapter_name: str, workspace: Path | None,
) -> tuple[ModuleContract, bool]:
    """The contract the adapter declares, and whether its interpreter is this caller's to check."""
    configured = adapter.get("broad_check")
    if configured is None:
        # The legacy default names an absolute interpreter — the one running now — so it is fully
        # answerable from either side.
        legacy = ModuleContract(
            sys.executable, LEGACY_IMPORT_PACKAGE, LEGACY_REASON_MISSING_BROAD_CHECK
        )
        return legacy, True
    if not isinstance(configured, dict):  # schema validation above normally catches this.
        raise ContractUnusable(
            BROAD_CHECK_INCOMPLETE, adapter_name,
            f"adapter {adapter_name!r} has no broad-check contract",
        )
    interpreter = str(configured.get("interpreter") or "").strip()
    import_package = str(configured.get("import_package") or "").strip()
    if not interpreter or not import_package:
        raise ContractUnusable(
            BROAD_CHECK_INCOMPLETE, adapter_name,
            f"adapter {adapter_name!r} has no broad-check contract",
        )
    interpreter_path = Path(interpreter)
    if interpreter_path.is_absolute():
        return ModuleContract(interpreter, import_package), True
    if workspace is None:
        # Resolved from the candidate workspace, per the adapter schema, and there is none yet.
        # Left as the adapter spells it and not tested: the answer belongs to the tree that will
        # run the check, and any other tree would answer a different question.
        return ModuleContract(interpreter, import_package), False
    # Keep a venv's symlink spelling. Resolving the final component would turn
    # `.venv/bin/python` into the base interpreter and discard that environment's site paths.
    return ModuleContract(str(Path(workspace).resolve() / interpreter_path), import_package), True


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
    return any(
        (root / top / "__init__.py").is_file() or (root / f"{top}.py").is_file() for root in roots
    )
