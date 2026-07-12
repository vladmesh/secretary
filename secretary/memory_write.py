from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from secretary._fsutil import (
    cleanup_staging_dir as _cleanup_staging_dir,
    remove_path as _remove_path,
    write_json as _write_json,
    write_text_atomic as _write_text_atomic,
)
from secretary.memory_journal import (
    _git,
    _git_status,
    _journal_head,
    _memory_journal_lock,
    _publish_memory_export,
    _read_memory_facts,
    _recover_journal_worktree,
    init_memory_journal,
)
from secretary.memory_errors import (
    MemoryExportPublishError,
    MemoryLockError,
    MemoryPermissionError,
    MemoryProtocolError,
    MemoryValidationError,
)


MEMORY_PROPOSAL_TTL_SECONDS = 7 * 24 * 60 * 60
MEMORY_PROPOSAL_ACTIVE_SECONDS = 60 * 60
MEMORY_PROPOSAL_ACTIVE_MARKER = ".active"
MEMORY_PROPOSAL_DONE = "committed.json"


@dataclass(frozen=True)
class MemoryProposal:
    propose_id: str
    path: Path
    scope: str
    scope_dir: str
    slug: str
    actor: str
    source: str
    supersedes: tuple[str, ...]


@dataclass(frozen=True)
class MemoryWriteResult:
    op: str
    facts_dir: Path
    commit: str
    fact: str
    actor: str
    source: str
    changed_facts: tuple[str, ...]
    propose_id: str | None = None


@dataclass(frozen=True)
class MemoryProposalGCResult:
    removed: tuple[str, ...]
    kept_fresh: tuple[str, ...]
    kept_active: tuple[str, ...]


def propose_memory_fact(
    data_dir: Path,
    *,
    actor: str,
    scope: str,
    slug: str,
    fact_file: Path,
    source: str | None = None,
    tags: list[str] | None = None,
    pinned: bool = False,
    supersedes: list[str] | None = None,
) -> MemoryProposal:
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    _ensure_writer_actor(actor)
    scope_dir = _scope_dir(scope)
    slug = _clean_slug(slug)
    supersede_ids = _normalize_supersedes(scope_dir, supersedes or [])
    fact_text, fact_source = _prepare_fact_text(
        fact_file,
        actor=actor,
        source=source,
        tags=tags or [],
        pinned=pinned,
        supersedes=supersede_ids,
    )
    proposal_id = uuid.uuid4().hex
    with _memory_journal_lock(memory_dir):
        _gc_staging_proposals(memory_dir, now=int(time.time()))
        staging_dir = memory_dir / ".staging" / proposal_id
        try:
            staging_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise RuntimeError(f"could not create memory proposal: {exc}") from None
        proposal = {
            "version": 1,
            "id": proposal_id,
            "scope": scope,
            "scope_dir": scope_dir,
            "slug": slug,
            "actor": actor,
            "source": fact_source,
            "supersedes": list(supersede_ids),
            "fact_file": "fact.md",
            "created_at": int(time.time()),
        }
        try:
            _write_text_atomic(staging_dir / "fact.md", fact_text)
            _write_json(staging_dir / "proposal.json", proposal)
        except RuntimeError:
            _cleanup_staging_dir(staging_dir)
            raise
    return MemoryProposal(
        propose_id=proposal_id,
        path=staging_dir,
        scope=scope,
        scope_dir=scope_dir,
        slug=slug,
        actor=actor,
        source=fact_source,
        supersedes=supersede_ids,
    )


def commit_memory_proposal(
    data_dir: Path,
    *,
    actor: str,
    propose_id: str,
) -> MemoryWriteResult:
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    _ensure_writer_actor(actor)
    propose_id = _clean_proposal_id(propose_id)
    with _memory_journal_lock(memory_dir):
        proposal_dir = memory_dir / ".staging" / propose_id
        completed = _read_completed_proposal(proposal_dir)
        if completed is not None:
            _ensure_commit_actor(actor, completed.actor)
            _publish_write_export(memory_dir, completed)
            _cleanup_staging_dir(proposal_dir)
            return completed
        proposal = _read_proposal(proposal_dir)
        _ensure_commit_actor(actor, str(proposal["actor"]))
        _mark_proposal_active(proposal_dir)
        try:
            result = _apply_memory_write(memory_dir, proposal, op="commit")
        except Exception:
            _remove_proposal_active(proposal_dir)
            raise
        _write_completed_proposal(proposal_dir, result)
        _publish_write_export(memory_dir, result)
        _cleanup_staging_dir(proposal_dir)
        return result


def supersede_memory_fact(
    data_dir: Path,
    *,
    actor: str,
    scope: str,
    slug: str,
    fact_file: Path,
    supersedes: list[str],
    source: str | None = None,
    tags: list[str] | None = None,
    pinned: bool = False,
) -> MemoryWriteResult:
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    _ensure_writer_actor(actor)
    scope_dir = _scope_dir(scope)
    slug = _clean_slug(slug)
    supersede_ids = _normalize_supersedes(scope_dir, supersedes)
    if not supersede_ids:
        raise MemoryValidationError("supersede requires at least one superseded fact")
    fact_text, fact_source = _prepare_fact_text(
        fact_file,
        actor=actor,
        source=source,
        tags=tags or [],
        pinned=pinned,
        supersedes=supersede_ids,
    )
    proposal = {
        "version": 1,
        "id": None,
        "scope": scope,
        "scope_dir": scope_dir,
        "slug": slug,
        "actor": actor,
        "source": fact_source,
        "supersedes": list(supersede_ids),
        "fact_text": fact_text,
    }
    with _memory_journal_lock(memory_dir):
        result = _apply_memory_write(memory_dir, proposal, op="supersede")
        _publish_write_export(memory_dir, result)
        return result


def gc_memory_proposals(
    data_dir: Path,
    *,
    max_age_seconds: int = MEMORY_PROPOSAL_TTL_SECONDS,
    active_grace_seconds: int = MEMORY_PROPOSAL_ACTIVE_SECONDS,
) -> MemoryProposalGCResult:
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    if max_age_seconds < 0:
        raise MemoryValidationError("proposal max age must be non-negative")
    if active_grace_seconds < 0:
        raise MemoryValidationError("proposal active grace must be non-negative")
    with _memory_journal_lock(memory_dir):
        return _gc_staging_proposals(
            memory_dir,
            now=int(time.time()),
            max_age_seconds=max_age_seconds,
            active_grace_seconds=active_grace_seconds,
        )


def _ensure_writer_actor(actor: str) -> None:
    role = _actor_role(actor)
    if role not in {"curator", "secretary", "operator"}:
        raise MemoryPermissionError(f"actor is not allowed to write memory: {actor}")


def _ensure_commit_actor(actor: str, proposal_actor: str) -> None:
    if actor == proposal_actor:
        return
    if _actor_role(actor) in {"secretary", "operator"}:
        return
    raise MemoryPermissionError(
        f"actor {actor} cannot commit proposal owned by {proposal_actor}"
    )


def _actor_role(actor: str) -> str:
    actor = actor.strip()
    if not actor:
        raise MemoryValidationError("actor is required")
    return actor.split(":", 1)[0].split("/", 1)[0]


def _source_allowed(actor: str, source: str) -> bool:
    actor_role = _actor_role(actor)
    source_role = source.split(":", 1)[0].split("/", 1)[0]
    return actor_role in {"secretary", "operator"} or actor_role == source_role


def _scope_dir(scope: str) -> str:
    value = scope.strip()
    if value == "global":
        return "global"
    if value.startswith("project:"):
        return _clean_path_part(value.removeprefix("project:"), "scope")
    raise MemoryValidationError("scope must be global or project:<dir>")


def _clean_slug(slug: str) -> str:
    value = slug.strip()
    if value.endswith(".md"):
        value = value[:-3]
    return _clean_path_part(value, "slug")


def _clean_path_part(value: str, label: str) -> str:
    if not value:
        raise MemoryValidationError(f"{label} is required")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if value in {".", ".."} or any(char not in allowed for char in value):
        raise MemoryValidationError(f"{label} contains unsupported characters")
    return value


def _clean_proposal_id(propose_id: str) -> str:
    value = propose_id.strip()
    if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
        raise MemoryValidationError("invalid propose-id")
    return value


def _normalize_supersedes(scope_dir: str, supersedes: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_item in supersedes:
        for raw_part in raw_item.split(","):
            item = raw_part.strip()
            if not item:
                continue
            if "/" in item:
                old_scope, old_slug = item.split("/", 1)
                old_scope = _clean_path_part(old_scope, "supersede scope")
                old_slug = _clean_slug(old_slug)
            else:
                old_scope = scope_dir
                old_slug = _clean_slug(item)
            fact_id = f"{old_scope}/{old_slug}"
            if fact_id not in normalized:
                normalized.append(fact_id)
    return tuple(normalized)


def _prepare_fact_text(
    fact_file: Path,
    *,
    actor: str,
    source: str | None,
    tags: list[str],
    pinned: bool,
    supersedes: tuple[str, ...],
) -> tuple[str, str]:
    try:
        raw = fact_file.expanduser().read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MemoryValidationError(f"fact file not found: {fact_file}") from None
    except OSError as exc:
        raise RuntimeError(f"could not read fact file {fact_file}: {exc}") from None
    except UnicodeError as exc:
        raise MemoryValidationError(f"could not decode fact file {fact_file}: {exc}") from None
    if not raw.strip():
        raise MemoryValidationError("fact file is empty")

    metadata, body = _split_fact(raw)
    fact_source = source or str(metadata.get("source") or "")
    if not fact_source:
        raise MemoryValidationError("fact source is required")
    if not _source_allowed(actor, fact_source):
        raise MemoryPermissionError(
            f"source {fact_source} is not allowed for actor {actor}"
        )
    metadata["source"] = fact_source
    if tags:
        metadata["tags"] = tags
    if pinned:
        metadata["pinned"] = True
    if supersedes:
        metadata["supersedes"] = ",".join(item.rsplit("/", 1)[1] for item in supersedes)
    if "created" not in metadata:
        metadata["created"] = time.strftime("%Y-%m-%d", time.gmtime())
    return _join_fact(metadata, body), fact_source


def _split_fact(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        raise MemoryValidationError("fact frontmatter is not closed")
    try:
        loaded = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as exc:
        raise MemoryValidationError(f"fact frontmatter is invalid: {exc}") from None
    if not isinstance(loaded, dict):
        raise MemoryValidationError("fact frontmatter must be a mapping")
    return {str(key): value for key, value in loaded.items()}, text[end + 5 :]


def _join_fact(metadata: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return f"---\n{frontmatter}---\n{body.lstrip()}"


def _read_proposal(proposal_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((proposal_dir / "proposal.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MemoryValidationError(f"proposal not found: {proposal_dir.name}") from None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MemoryValidationError(f"could not read proposal {proposal_dir.name}: {exc}") from None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise MemoryValidationError(f"invalid proposal {proposal_dir.name}")
    required = ("scope_dir", "slug", "actor", "source")
    for key in required:
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise MemoryValidationError(f"proposal {proposal_dir.name} missing {key}")
    supersedes = payload.get("supersedes", [])
    if not isinstance(supersedes, list) or not all(isinstance(item, str) for item in supersedes):
        raise MemoryValidationError(f"proposal {proposal_dir.name} has invalid supersedes")
    try:
        payload["fact_text"] = (proposal_dir / "fact.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise MemoryValidationError(
            f"could not read proposal fact {proposal_dir.name}: {exc}"
        ) from None
    return payload


def _read_completed_proposal(proposal_dir: Path) -> MemoryWriteResult | None:
    try:
        payload = json.loads((proposal_dir / MEMORY_PROPOSAL_DONE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MemoryValidationError(
            f"could not read completed proposal {proposal_dir.name}: {exc}"
        ) from None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise MemoryValidationError(f"invalid completed proposal {proposal_dir.name}")
    changed_facts = payload.get("changed_facts")
    if not isinstance(changed_facts, list) or not all(
        isinstance(item, str) for item in changed_facts
    ):
        raise MemoryValidationError(
            f"completed proposal {proposal_dir.name} has invalid changed facts"
        )
    try:
        return MemoryWriteResult(
            op=str(payload["op"]),
            facts_dir=Path(str(payload["facts_dir"])),
            commit=str(payload["commit"]),
            fact=str(payload["fact"]),
            actor=str(payload["actor"]),
            source=str(payload["source"]),
            changed_facts=tuple(changed_facts),
            propose_id=str(payload["propose_id"]) if payload.get("propose_id") else None,
        )
    except KeyError as exc:
        raise MemoryValidationError(
            f"completed proposal {proposal_dir.name} missing {exc.args[0]}"
        ) from None


def _write_completed_proposal(proposal_dir: Path, result: MemoryWriteResult) -> None:
    _write_json(
        proposal_dir / MEMORY_PROPOSAL_DONE,
        {
            "version": 1,
            "op": result.op,
            "facts_dir": str(result.facts_dir),
            "commit": result.commit,
            "fact": result.fact,
            "actor": result.actor,
            "source": result.source,
            "changed_facts": list(result.changed_facts),
            "propose_id": result.propose_id,
        },
    )
    _remove_proposal_active(proposal_dir)


def _mark_proposal_active(proposal_dir: Path) -> None:
    _write_json(
        proposal_dir / MEMORY_PROPOSAL_ACTIVE_MARKER,
        {"pid": os.getpid(), "updated_at": int(time.time())},
    )


def _remove_proposal_active(proposal_dir: Path) -> None:
    try:
        (proposal_dir / MEMORY_PROPOSAL_ACTIVE_MARKER).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _gc_staging_proposals(
    memory_dir: Path,
    *,
    now: int,
    max_age_seconds: int = MEMORY_PROPOSAL_TTL_SECONDS,
    active_grace_seconds: int = MEMORY_PROPOSAL_ACTIVE_SECONDS,
) -> MemoryProposalGCResult:
    staging_root = memory_dir / ".staging"
    if not staging_root.is_dir():
        return MemoryProposalGCResult(removed=(), kept_fresh=(), kept_active=())

    removed: list[str] = []
    kept_fresh: list[str] = []
    kept_active: list[str] = []
    for proposal_dir in sorted(staging_root.iterdir()):
        if not proposal_dir.is_dir():
            continue
        try:
            proposal_id = _clean_proposal_id(proposal_dir.name)
        except MemoryValidationError:
            continue
        if (proposal_dir / MEMORY_PROPOSAL_DONE).exists():
            continue
        if _proposal_is_active(proposal_dir, now=now, active_grace_seconds=active_grace_seconds):
            kept_active.append(proposal_id)
            continue
        created_at = _proposal_created_at(proposal_dir)
        if created_at is None:
            continue
        if now - created_at <= max_age_seconds:
            kept_fresh.append(proposal_id)
            continue
        _cleanup_staging_dir(proposal_dir)
        removed.append(proposal_id)
    return MemoryProposalGCResult(
        removed=tuple(removed),
        kept_fresh=tuple(kept_fresh),
        kept_active=tuple(kept_active),
    )


def _proposal_is_active(
    proposal_dir: Path,
    *,
    now: int,
    active_grace_seconds: int,
) -> bool:
    marker = proposal_dir / MEMORY_PROPOSAL_ACTIVE_MARKER
    try:
        marker_mtime = int(marker.stat().st_mtime)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return now - marker_mtime <= active_grace_seconds


def _proposal_created_at(proposal_dir: Path) -> int | None:
    try:
        payload = json.loads((proposal_dir / "proposal.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    created_at = payload.get("created_at")
    if not isinstance(created_at, int):
        return None
    return created_at


def _apply_memory_write(
    memory_dir: Path,
    proposal: dict[str, Any],
    *,
    op: str,
) -> MemoryWriteResult:
    facts_dir, _initialized = init_memory_journal(memory_dir.parent)
    _recover_journal_worktree(facts_dir)
    scope_dir = _clean_path_part(str(proposal["scope_dir"]), "scope")
    slug = _clean_slug(str(proposal["slug"]))
    actor = str(proposal["actor"])
    source = str(proposal["source"])
    fact_id = f"{scope_dir}/{slug}"
    target = facts_dir / scope_dir / f"{slug}.md"
    supersedes = tuple(str(item) for item in proposal.get("supersedes", []))

    if target.exists():
        raise MemoryValidationError(f"memory fact already exists: {fact_id}")
    supersede_paths = _supersede_paths(facts_dir, supersedes)
    if op == "supersede" and not supersede_paths:
        raise MemoryValidationError("supersede requires at least one superseded fact")
    if fact_id in supersedes:
        raise MemoryValidationError("new fact cannot supersede itself")

    try:
        _write_text_atomic(target, str(proposal["fact_text"]))
        for _old_id, old_path in supersede_paths:
            _remove_path(old_path)
        _git(facts_dir, ["add", "-A", "."], context=f"stage memory {op}")
        changed = _git_status(facts_dir)
        if not changed:
            raise MemoryValidationError("memory write produced no journal changes")
        _git(
            facts_dir,
            ["commit", "-m", _commit_message(op, proposal, fact_id)],
            context=f"commit memory {op}",
        )
        commit = _journal_head(facts_dir)
        if commit is None:
            raise RuntimeError("memory write did not create a commit")
        if _git_status(facts_dir):
            raise RuntimeError("memory journal dirty after commit")
    except Exception:
        _recover_journal_worktree(facts_dir)
        raise

    return MemoryWriteResult(
        op=op,
        facts_dir=facts_dir,
        commit=commit,
        fact=fact_id,
        actor=actor,
        source=source,
        changed_facts=(fact_id, *supersedes),
        propose_id=proposal.get("id") if isinstance(proposal.get("id"), str) else None,
    )


def _publish_write_export(memory_dir: Path, result: MemoryWriteResult) -> None:
    try:
        facts = _read_memory_facts(result.facts_dir)
        _publish_memory_export(
            memory_dir,
            facts=facts,
            source_memory=result.facts_dir,
            source_root=result.facts_dir,
            source_head=result.commit,
            commit=result.commit,
            changed=True,
            record_import=False,
        )
    except RuntimeError as exc:
        raise MemoryExportPublishError(
            f"memory export publish failed after journal commit {result.commit}: {exc}",
            result=result,
        ) from None


def _supersede_paths(facts_dir: Path, supersedes: tuple[str, ...]) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    for fact_id in supersedes:
        if "/" not in fact_id:
            raise MemoryValidationError(f"invalid superseded fact: {fact_id}")
        scope_dir, slug = fact_id.split("/", 1)
        scope_dir = _clean_path_part(scope_dir, "supersede scope")
        slug = _clean_slug(slug)
        normalized = f"{scope_dir}/{slug}"
        path = facts_dir / scope_dir / f"{slug}.md"
        if not path.is_file():
            raise MemoryValidationError(f"superseded fact not found: {normalized}")
        paths.append((normalized, path))
    return paths


def _commit_message(op: str, proposal: dict[str, Any], fact_id: str) -> str:
    actor = str(proposal["actor"])
    source = str(proposal["source"])
    supersedes = tuple(str(item) for item in proposal.get("supersedes", []))
    changed_facts = ", ".join((fact_id, *supersedes))
    if op == "supersede":
        subject = f"memory supersede: {fact_id}"
    else:
        subject = f"memory commit: {fact_id}"
    lines = [
        subject,
        "",
        f"Op: {op}",
        f"Principal: {actor}",
        f"Source: {source}",
        f"Fact: {fact_id}",
        f"Changed-Facts: {changed_facts}",
    ]
    proposal_id = proposal.get("id")
    if isinstance(proposal_id, str) and proposal_id:
        lines.append(f"Proposal: {proposal_id}")
    if supersedes:
        lines.append(f"Supersedes: {', '.join(supersedes)}")
    return "\n".join(lines) + "\n"
