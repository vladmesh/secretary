"""The one registry of contract shapes an older writer may have left on disk.

A merge that tightens `onboarding-contract.schema.json` does not touch the files
already written, so every path that reads a contract migrates through this module
before validating. That way a tightening merge cannot leave an installation whose
every tick fails on a draft nobody can rewrite: the read path repairs it.

Only the sections listed here are dropped. Any other unexpected key stays in
place and keeps failing validation, because that is a corrupt contract, not an
obsolete one.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from secretary._fsutil import publish_state_atomic, try_file_lock

# Top-level sections removed from the contract schema. One entry per removal, with
# the reason, so the list stays auditable instead of growing `pop` calls in code.
LEGACY_CONTRACT_SECTIONS = (
    # Declared a dispatcher consumer the pipeline never had.
    "compatibility_manifest",
)

# Binding-owned fields an older writer copied into the contract's ``identity``.
MUTABLE_BINDING_FIELDS = ("plane", "policy")

_persist = True


@contextlib.contextmanager
def suspended() -> Iterator[None]:
    """Migrate in memory only, for a caller that promised to write nothing.

    Process-wide on purpose. A command reaches the read path through many helpers
    that validate the instance themselves, so a flag threaded through one call
    would be honored there and silently ignored by every nested one. `--dry-run`
    sets this once, at the CLI boundary, and gets the same verdict as a real run.
    """
    global _persist
    previous = _persist
    _persist = False
    try:
        yield
    finally:
        _persist = previous


def normalize_contract(document: dict[str, Any]) -> None:
    """Drop the legacy sections an older writer left in a contract on disk.

    Two of them: the mutable binding fields copied into ``identity``, and the
    ``compatibility_manifest`` block that declared a dispatcher consumer the
    pipeline never had.
    """
    for section in LEGACY_CONTRACT_SECTIONS:
        document.pop(section, None)
    identity = document.get("identity")
    if not isinstance(identity, dict):
        return
    for field in MUTABLE_BINDING_FIELDS:
        identity.pop(field, None)


def migrate_contract_dir(
    directory: Path,
) -> tuple[dict[Path, dict[str, Any]], list[tuple[Path, str]]]:
    """Migrate every contract in ``directory`` that carries a known legacy section.

    Returns the migrated documents by path, so the caller validates what the file
    now holds, plus the files that could not be rewritten. A second run finds
    nothing left to change and writes nothing.

    A file that does not parse is left alone: unreadable YAML is a config error
    the caller reports, not something a migration may guess at.
    """
    migrated: dict[Path, dict[str, Any]] = {}
    failures: list[tuple[Path, str]] = []
    if not directory.is_dir():
        return migrated, failures
    for path in sorted(directory.glob("*.yaml")):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if not isinstance(document, dict):
            continue
        normalized = copy.deepcopy(document)
        normalize_contract(normalized)
        if normalized == document:
            continue
        migrated[path] = normalized
        if not _persist:
            continue
        failure = _write_back(path, normalized)
        if failure is not None:
            failures.append((path, failure))
    return migrated, failures


def project_lock_path(instance_dir: Path, project_id: str) -> Path:
    return instance_dir / ".locks" / f"{project_id}.lock"


def _write_back(path: Path, document: dict[str, Any]) -> str | None:
    """Write the migrated contract back under the lock its writers take.

    Onboarding, provision and the gate serialize their draft writes on the
    project lock. If one of them holds it right now it is mid-transition and
    normalizes the contract on its own path, so there is nothing to repair here
    and the next read finds the file already current.
    """
    with try_file_lock(project_lock_path(path.parent.parent, path.stem)) as acquired:
        if not acquired:
            return None
        try:
            publish_state_atomic([(path, yaml.safe_dump(document, sort_keys=False))])
        except OSError as exc:
            return exc.strerror or "I/O error"
    return None
