"""Opening the secret store on a recovered installation.

A recovery starts with a clone of the private instance repo and nothing else:
the catalog and the sealed values are there, the installation key is not, because
it is the one file the repo never carries. This module turns that clone plus a
recovery phrase back into readable secrets and into the env files the catalog
says they belong to, and, when there is no phrase, into a report of what stayed
closed instead of a dead end.

Two failure shapes are worth telling apart, so they are separate lists:

    locked   the ciphertext is here, the key is not; a phrase reopens it
    missing  the catalog names a secret whose envelope is not in the repo

Only ids and open catalog metadata leave this module. A value never appears in a
report, and a target file whose secrets are not all readable is not written at
all: half an env file is a component that starts with a plausible-looking
configuration and fails somewhere further away.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.secret_store import (
    MaterializeResult,
    RecoveryPhraseError,
    SecretStoreStateError,
    is_initialized,
    list_secrets,
    load_installation_key,
    materialize_path,
    materialize_secrets,
    restore_installation_key,
    value_path,
    verify_recovery_phrase,
)


@dataclass(frozen=True)
class SecretRecovery:
    """What opening the store did, and what it could not do.

    `locked` and `missing` hold one open metadata record per secret: the id, its
    scope, the environment variable it materializes into and the file it belongs
    to. No value, sealed or otherwise, is ever part of this.
    """

    store_present: bool
    unlocked: bool
    materialized: tuple[MaterializeResult, ...] = ()
    locked: tuple[dict[str, Any], ...] = ()
    missing: tuple[dict[str, Any], ...] = ()
    withheld: tuple[Path, ...] = ()

    @property
    def complete(self) -> bool:
        """Every catalogued secret is open and written where it belongs."""
        return self.store_present and self.unlocked and not self.locked and not self.missing

    @property
    def changed(self) -> bool:
        return any(result.changed for result in self.materialized)

    def summary(self) -> str:
        if not self.store_present:
            return "no secret store in the instance repo"
        if not self.unlocked:
            return (
                f"store is locked: {len(self.locked)} secret(s) locked, "
                f"{len(self.missing)} missing; rerun with the recovery phrase"
            )
        parts = [
            f"{len(self.materialized)} env file(s) written"
            if self.materialized
            else "nothing to materialize"
        ]
        if self.missing:
            parts.append(f"{len(self.missing)} secret(s) missing from the repo")
        if self.withheld:
            parts.append(
                "not written: " + ", ".join(str(path) for path in sorted(self.withheld))
            )
        return "; ".join(parts)

    def report(self) -> dict[str, Any]:
        """The machine-readable half of the same thing, for `--json` callers."""
        return {
            "store_present": self.store_present,
            "unlocked": self.unlocked,
            "complete": self.complete,
            "materialized": [
                {
                    "target": result.target,
                    "path": str(result.path),
                    "variables": list(result.variables),
                    "changed": result.changed,
                }
                for result in self.materialized
            ],
            "locked": [dict(entry) for entry in self.locked],
            "missing": [dict(entry) for entry in self.missing],
            "withheld": [str(path) for path in sorted(self.withheld)],
        }

    def render(self) -> list[str]:
        """The operator-facing report: identifiers and where they belong."""
        lines: list[str] = []
        for name, entries in (("locked", self.locked), ("missing", self.missing)):
            for entry in entries:
                where = entry.get("path") or entry.get("target") or "-"
                variable = entry.get("environment") or "-"
                lines.append(f"  {name:8} {entry['id']}  {variable}  {where}")
        for path in sorted(self.withheld):
            lines.append(f"  withheld {path}: not every secret in this file is readable")
        return lines


def recover_secrets(
    instance_dir: Path, *, phrase: str | None = None, dry_run: bool = False
) -> SecretRecovery:
    """Open the store with the phrase, or report what stays closed without it.

    A wrong phrase is rejected by the verifier before anything is written, so a
    failed attempt leaves the clone exactly as it was. An installation whose key
    file survived opens without a phrase at all: recovery is then the same
    idempotent materialization a running host does.

    `dry_run` still checks the phrase against the verifier, so the preview cannot
    promise an opening that would not happen, but writes neither the key file nor
    any env file.
    """
    instance_dir = Path(instance_dir)
    if not is_initialized(instance_dir):
        return SecretRecovery(store_present=False, unlocked=False)

    entries = [dict(entry) for entry in list_secrets(instance_dir)]
    missing = tuple(
        _describe(instance_dir, entry)
        for entry in entries
        if not value_path(instance_dir, entry["id"]).is_file()
    )
    if not _unlock(instance_dir, phrase, dry_run=dry_run):
        stored = {record["id"] for record in missing}
        return SecretRecovery(
            store_present=True,
            unlocked=False,
            locked=tuple(
                _describe(instance_dir, entry)
                for entry in entries
                if entry["id"] not in stored
            ),
            missing=missing,
        )

    complete, withheld = _writable_targets(instance_dir, entries, missing)
    materialized = ()
    if complete and not dry_run:
        materialized = materialize_secrets(instance_dir, paths=complete)
    return SecretRecovery(
        store_present=True,
        unlocked=True,
        materialized=materialized,
        missing=missing,
        withheld=withheld,
    )


def _unlock(instance_dir: Path, phrase: str | None, *, dry_run: bool) -> bool:
    """Make the installation key usable, from the phrase if one was given.

    The phrase is checked first, against the verifier and without touching a
    file, so a wrong one fails the same way whether or not a key file is lying
    around. A key file that already opens the store is then left alone: a second
    recover with the same phrase has nothing to rebuild, and rewriting the file
    would only move its mtime.
    """
    if phrase is not None:
        verify_recovery_phrase(instance_dir, phrase)
        if dry_run or _key_usable(instance_dir):
            return True
        restore_installation_key(instance_dir, phrase)
    return _key_usable(instance_dir)


def _key_usable(instance_dir: Path) -> bool:
    """Whether the key file on disk is there and opens this installation."""
    try:
        load_installation_key(instance_dir)
    except (SecretStoreStateError, RecoveryPhraseError):
        return False
    return True


def _writable_targets(
    instance_dir: Path,
    entries: list[dict[str, Any]],
    missing: tuple[dict[str, Any], ...],
) -> tuple[set[Path], tuple[Path, ...]]:
    """Split the catalog's target files into the whole ones and the gapped ones."""
    absent = {record["id"] for record in missing}
    complete: set[Path] = set()
    withheld: set[Path] = set()
    for entry in entries:
        if not entry.get("materialize"):
            continue
        path = materialize_path(instance_dir, entry)
        if entry["id"] in absent:
            withheld.add(path)
        else:
            complete.add(path)
    return complete - withheld, tuple(sorted(withheld))


def _describe(instance_dir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """One catalog entry as a report line. Open metadata only."""
    record: dict[str, Any] = {"id": entry["id"], "scope": entry.get("scope", "")}
    if entry.get("environment"):
        record["environment"] = entry["environment"]
    instruction = entry.get("materialize")
    if instruction:
        record["target"] = instruction.get("target", "")
        try:
            record["path"] = str(materialize_path(instance_dir, entry))
        except SecretStoreStateError:
            pass
    return record


__all__ = ["SecretRecovery", "recover_secrets"]
