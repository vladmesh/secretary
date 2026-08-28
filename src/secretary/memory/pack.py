"""Validated materialization of shipped memory packs into the instance canon.

The pack is an input to the one existing memory journal.  Its ownership record
lives beside the facts it owns, so a future manifest can remove only its own
facts without learning anything about locally curated facts.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from secretary import state_repo
from secretary._fsutil import remove_path, write_json, write_text_atomic
from secretary.memory_errors import MemoryValidationError
from secretary.memory_journal import (
    _git_status,
    _memory_journal_lock,
    _publish_memory_export,
    _read_memory_facts,
    _recover_journal_worktree,
    init_memory_journal,
    reject_legacy_memory_journal,
)

PACK_NAME = "product-secretary"
PACK_NAMESPACE = "product:secretary"
PACK_FACT_DIRECTORY = "product-secretary"
LEDGER_RELATIVE = Path("state") / "memory" / "packs" / f"{PACK_NAME}.json"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class MemoryPackError(MemoryValidationError):
    """A shipped pack is invalid or cannot be reconciled."""


class MemoryPackDegradedError(MemoryPackError):
    """The canon committed but its derived export could not be published."""

    def __init__(self, message: str, *, commit: str) -> None:
        super().__init__(message)
        self.commit = commit


@dataclass(frozen=True)
class PackFact:
    id: str
    path: str
    digest: str
    text: str

    @property
    def canon_id(self) -> str:
        return f"{PACK_FACT_DIRECTORY}/{self.id}"


@dataclass(frozen=True)
class MemoryPack:
    root: Path
    digest: str
    facts: tuple[PackFact, ...]


@dataclass(frozen=True)
class MaterializedPack:
    changed: bool
    commit: str | None
    added: int
    updated: int
    deleted: int
    retained: int


def product_pack_root(product_root: Path) -> Path:
    return Path(product_root).expanduser() / "packaging" / "memory" / PACK_NAME


def load_product_pack(product_root: Path) -> MemoryPack:
    """Read a complete product pack without following links or accepting escapes."""
    root = product_pack_root(product_root)
    _require_directory(root, "pack root")
    manifest_path = root / "manifest.yaml"
    raw_manifest = _regular_file_under(root, manifest_path, "manifest")
    try:
        manifest = yaml.safe_load(raw_manifest.decode("utf-8"))
    except UnicodeError as exc:
        raise MemoryPackError(f"pack manifest is not UTF-8: {exc}") from None
    except yaml.YAMLError as exc:
        raise MemoryPackError(f"pack manifest is malformed: {exc.problem or 'invalid YAML'}") from None
    if not isinstance(manifest, dict):
        raise MemoryPackError("pack manifest must be a mapping")
    _validate_manifest_header(manifest)
    entries = manifest.get("facts")
    if not isinstance(entries, list) or not entries:
        raise MemoryPackError("pack manifest requires a non-empty facts list")
    facts: list[PackFact] = []
    ids: set[str] = set()
    paths: set[str] = set()
    digest = hashlib.sha256()
    digest.update(raw_manifest)
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise MemoryPackError(f"pack fact {number} must be a mapping")
        fact_id = entry.get("id")
        relative = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(fact_id, str) or not _safe_id(fact_id):
            raise MemoryPackError(f"pack fact {number} has an unsafe id")
        if not isinstance(relative, str) or not _safe_relative(relative):
            raise MemoryPackError(f"pack fact {fact_id} has an unsafe path")
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise MemoryPackError(f"pack fact {fact_id} has an invalid sha256")
        if fact_id in ids:
            raise MemoryPackError(f"pack manifest repeats fact id: {fact_id}")
        if relative in paths:
            raise MemoryPackError(f"pack manifest repeats fact path: {relative}")
        source = root.joinpath(*PurePosixPath(relative).parts)
        raw = _regular_file_under(root, source, f"fact {fact_id}")
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise MemoryPackError(f"pack fact digest mismatch: {fact_id}")
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise MemoryPackError(f"pack fact is not UTF-8: {fact_id}: {exc}") from None
        ids.add(fact_id)
        paths.add(relative)
        digest.update(b"\0")
        digest.update(fact_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        facts.append(PackFact(fact_id, relative, actual, text))
    return MemoryPack(root=root, digest=digest.hexdigest(), facts=tuple(facts))


def materialize_product_pack(
    pack: MemoryPack,
    *,
    instance_dir: Path,
    data_dir: Path,
    dry_run: bool = False,
) -> MaterializedPack:
    """Reconcile one validated pack, its ledger, and the derived export.

    Validation is deliberately separate from this function.  Callers can parse
    the product input before an upgrade performs any instance mutation.
    """
    instance_dir = state_repo.require_repo(instance_dir)
    data_dir = Path(data_dir).expanduser().resolve()
    memory_dir = data_dir / "memory"
    reject_legacy_memory_journal(memory_dir)
    facts_dir, _created = init_memory_journal(instance_dir)
    with _memory_journal_lock(memory_dir), state_repo.state_repo_lock(instance_dir):
        _recover_journal_worktree(instance_dir)
        ledger_path = instance_dir / LEDGER_RELATIVE
        prior = _read_ledger(ledger_path)
        desired = {fact.canon_id: fact for fact in pack.facts}
        _check_collisions(facts_dir, desired, prior)
        write_ids = [fact_id for fact_id, fact in desired.items() if _fact_text(facts_dir, fact_id) != fact.text]
        delete_ids = sorted(set(prior["facts"]) - set(desired))
        ledger = _ledger_payload(pack)
        ledger_changed = _read_json(ledger_path) != ledger
        changed = bool(write_ids or delete_ids or ledger_changed)
        if not changed:
            return MaterializedPack(False, None, 0, 0, 0, len(desired))
        if dry_run:
            added = sum(not _fact_path(facts_dir, fact_id).exists() for fact_id in write_ids)
            return MaterializedPack(True, None, added, len(write_ids) - added, len(delete_ids), len(desired) - len(write_ids))
        try:
            for fact_id in write_ids:
                write_text_atomic(_fact_path(facts_dir, fact_id), desired[fact_id].text)
            for fact_id in delete_ids:
                target = _fact_path(facts_dir, fact_id)
                if target.exists() or target.is_symlink():
                    remove_path(target)
            write_json(ledger_path, ledger)
            commit = state_repo.commit(
                instance_dir,
                state_repo.MEMORY_PATHSPEC,
                f"memory pack: reconcile {PACK_NAMESPACE}",
            )
            if commit is None:
                raise MemoryPackError("memory pack reconciliation produced no journal changes")
            if _git_status(instance_dir):
                raise MemoryPackError("state/memory dirty after pack reconciliation")
        except Exception:
            _recover_journal_worktree(instance_dir)
            raise
        try:
            facts = _read_memory_facts(facts_dir)
            _publish_memory_export(
                memory_dir,
                facts=facts,
                source_memory=facts_dir,
                source_root=facts_dir,
                source_head=commit,
                commit=commit,
                changed=True,
                record_import=False,
            )
        except Exception as exc:
            raise MemoryPackDegradedError(
                f"memory pack export publish failed after journal commit {commit}: {exc}", commit=commit
            ) from None
        # Targets now exist, so derive this from the old ownership rather than the filesystem.
        added = sum(fact_id not in prior["facts"] for fact_id in write_ids)
        updated = len(write_ids) - added
        return MaterializedPack(True, commit, added, updated, len(delete_ids), len(desired) - len(write_ids))


def _validate_manifest_header(manifest: dict[str, Any]) -> None:
    required = {
        "schema": 1,
        "product": "secretary",
        "namespace": PACK_NAMESPACE,
        "status": "active",
        "ownership": "shipped",
        "fact_format": "markdown-frontmatter-v1",
    }
    for key, expected in required.items():
        if manifest.get(key) != expected:
            raise MemoryPackError(f"pack manifest requires {key}={expected!r}")
    reconciliation = manifest.get("reconciliation")
    expected_reconciliation = {
        "identity": "id",
        "digest": "sha256",
        "manifest_is_complete": True,
        "absent_id": "delete",
        "unchanged_digest": "retain_embedding",
    }
    if not isinstance(reconciliation, dict) or any(
        reconciliation.get(key) != value for key, value in expected_reconciliation.items()
    ):
        raise MemoryPackError("pack manifest must declare complete sha256 reconciliation")
    overlay = manifest.get("overlay_policy")
    if not isinstance(overlay, dict) or overlay.get("local_overlay_allowed") is not True or overlay.get(
        "shipped_id_collision"
    ) != "reject":
        raise MemoryPackError("pack manifest must declare local overlays and shipped-id collision rejection")


def _safe_id(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in {".", ".."} and all(
        char.isalnum() or char in "._-" for char in value
    )


def _safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and "\\" not in value and all(
        part not in {"", ".", ".."} for part in path.parts
    )


def _require_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise MemoryPackError(f"{label} is missing or unreadable: {path}: {exc}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MemoryPackError(f"{label} is not a real directory: {path}")


def _regular_file_under(root: Path, path: Path, label: str) -> bytes:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise MemoryPackError(f"{label} escapes pack root") from None
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except OSError as exc:
            raise MemoryPackError(f"{label} is missing or unreadable: {relative}: {exc}") from None
        if stat.S_ISLNK(info.st_mode):
            raise MemoryPackError(f"{label} is symlinked: {relative}")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise MemoryPackError(f"{label} is not a regular file: {relative}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MemoryPackError(f"could not read {label}: {relative}: {exc}") from None


def _read_ledger(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if not payload:
        return {"digest": None, "facts": {}}
    if payload.get("version") != 1 or payload.get("namespace") != PACK_NAMESPACE:
        raise MemoryPackError(f"invalid installed pack ledger: {path}")
    digest = payload.get("digest")
    facts = payload.get("facts")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or not isinstance(facts, dict):
        raise MemoryPackError(f"invalid installed pack ledger: {path}")
    normalized: dict[str, str] = {}
    for fact_id, fact_digest in facts.items():
        prefix = f"{PACK_FACT_DIRECTORY}/"
        if (
            not isinstance(fact_id, str)
            or not fact_id.startswith(prefix)
            or not _safe_id(fact_id.removeprefix(prefix))
        ):
            raise MemoryPackError(f"invalid installed pack ledger: {path}")
        if not isinstance(fact_digest, str) or not _SHA256.fullmatch(fact_digest):
            raise MemoryPackError(f"invalid installed pack ledger: {path}")
        normalized[fact_id] = fact_digest
    return {"digest": digest, "facts": normalized}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MemoryPackError(f"invalid installed pack ledger: {path}: {exc}") from None
    if not isinstance(payload, dict):
        raise MemoryPackError(f"invalid installed pack ledger: {path}")
    return payload


def _ledger_payload(pack: MemoryPack) -> dict[str, Any]:
    return {
        "version": 1,
        "namespace": PACK_NAMESPACE,
        "digest": pack.digest,
        "facts": {fact.canon_id: fact.digest for fact in pack.facts},
    }


def _fact_path(facts_dir: Path, fact_id: str) -> Path:
    scope, slug = fact_id.split("/", 1)
    return facts_dir / scope / f"{slug}.md"


def _fact_text(facts_dir: Path, fact_id: str) -> str | None:
    target = _fact_path(facts_dir, fact_id)
    if target.is_symlink():
        raise MemoryPackError(f"memory fact is symlinked: {fact_id}")
    if not target.exists():
        return None
    if not target.is_file():
        raise MemoryPackError(f"memory fact is not a regular file: {fact_id}")
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise MemoryPackError(f"could not read memory fact {fact_id}: {exc}") from None


def _check_collisions(facts_dir: Path, desired: dict[str, PackFact], prior: dict[str, Any]) -> None:
    owned = set(prior["facts"])
    for fact_id in desired:
        target = _fact_path(facts_dir, fact_id)
        if target.exists() or target.is_symlink():
            _fact_text(facts_dir, fact_id)
            if fact_id not in owned:
                raise MemoryPackError(f"local memory fact collides with shipped id: {fact_id}")
