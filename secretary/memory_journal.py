from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from secretary._fsutil import (
    cleanup_staging_dir as _cleanup_staging_dir,
    copy_tree as _copy_tree,
    ensure_dir as _ensure_dir,
    publish_component_entries as _publish_component_entries,
    regular_files_under as _regular_files_under,
    remove_path as _remove_path,
    write_json as _write_json,
    write_ndjson as _write_ndjson,
    write_text_atomic as _write_text_atomic,
)


PANELMEM_KB = Path("/home/dev/panelmem-kb")
MEMORY_IMPORT_MARKER = "Op: import"
MEMORY_GIT_NAME = "Secretary Memory"
MEMORY_GIT_EMAIL = "secretary-memory@localhost"
MEMORY_LOCK_NAME = ".write.lock"


class MemoryProtocolError(RuntimeError):
    pass


class MemoryValidationError(MemoryProtocolError):
    pass


class MemoryPermissionError(MemoryProtocolError):
    pass


class MemoryLockError(MemoryProtocolError):
    pass


@dataclass(frozen=True)
class MemoryImport:
    facts_dir: Path
    count: int
    source: str
    source_head: str
    commit: str | None
    changed: bool
    initialized: bool


@dataclass(frozen=True)
class MemoryExportSnapshot:
    path: Path
    count: int
    source: str


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


def init_memory_journal(data_dir: Path) -> tuple[Path, bool]:
    memory_dir = data_dir / "memory"
    _ensure_dir(memory_dir, "memory data dir")
    facts_dir = memory_dir / "facts"
    try:
        facts_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot prepare memory facts journal: {exc}") from None

    initialized = False
    if not (facts_dir / ".git").is_dir():
        _git(
            facts_dir,
            ["init", "--initial-branch=main"],
            context="initialize memory journal",
        )
        initialized = True
    _git(facts_dir, ["config", "user.name", MEMORY_GIT_NAME], context="configure memory journal")
    _git(
        facts_dir,
        ["config", "user.email", MEMORY_GIT_EMAIL],
        context="configure memory journal",
    )
    _git(facts_dir, ["config", "commit.gpgsign", "false"], context="configure memory journal")
    remotes = _git(facts_dir, ["remote"], context="inspect memory journal remotes").splitlines()
    if remotes:
        raise RuntimeError("memory facts journal must not have remotes")
    return facts_dir, initialized


def import_memory_journal(
    data_dir: Path,
    *,
    source_dir: Path = PANELMEM_KB,
) -> MemoryImport:
    data_dir = data_dir.expanduser().resolve()
    source_root, source_memory = _resolve_memory_source(source_dir)
    if not source_memory.is_dir():
        raise RuntimeError(f"memory source not found: {source_memory}")

    memory_dir = data_dir / "memory"
    _ensure_dir(memory_dir, "memory data dir")
    facts: list[dict[str, Any]] = []
    changed = False
    commit: str | None = None
    initialized = False
    with _memory_journal_lock(memory_dir):
        facts_dir, initialized = init_memory_journal(data_dir)
        _recover_journal_worktree(facts_dir)
        _ensure_import_only_journal(facts_dir)
        source_head = _source_git_head(source_root)
        staging: Path | None = None

        try:
            staging = Path(
                tempfile.mkdtemp(prefix=".memory-import-", suffix=".tmp", dir=memory_dir)
            )
        except OSError as exc:
            raise RuntimeError(f"could not create memory import staging: {exc}") from None
        rollback: Path | None = None
        try:
            _copy_tree(source_memory, staging)
            facts = _read_memory_facts(staging)
            changed, rollback = _publish_memory_facts(staging, facts_dir)
            try:
                commit = _commit_memory_import(
                    facts_dir,
                    source_root=source_root,
                    source_head=source_head,
                    changed=changed,
                )
            except RuntimeError:
                _restore_journal_files(facts_dir, rollback)
                raise
            _cleanup_staging_dir(rollback)
            rollback = None
            _publish_memory_export(
                memory_dir,
                facts=facts,
                source_memory=source_memory,
                source_root=source_root,
                source_head=source_head,
                commit=commit,
                changed=changed,
                record_import=True,
            )
        except RuntimeError:
            _cleanup_staging_dir(staging)
            raise
        finally:
            _cleanup_staging_dir(staging)
            _cleanup_staging_dir(rollback)

    return MemoryImport(
        facts_dir=facts_dir,
        count=len(facts),
        source=str(source_memory),
        source_head=source_head,
        commit=commit,
        changed=changed,
        initialized=initialized,
    )


def export_memory_snapshot(
    data_dir: Path,
    *,
    source_dir: Path = PANELMEM_KB,
) -> MemoryExportSnapshot:
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    _ensure_dir(memory_dir, "memory data dir")
    facts_dir = memory_dir / "facts"

    if (
        facts_dir.is_dir()
        and (facts_dir / ".git").is_dir()
        and _journal_head(facts_dir) is not None
    ):
        source_memory = facts_dir
        source_root = facts_dir
        source_head = _journal_head(facts_dir) or "unknown"
    else:
        source_root, source_memory = _resolve_memory_source(source_dir)
        if not source_memory.is_dir():
            raise RuntimeError(f"memory source not found: {source_memory}")
        source_head = _source_git_head(source_root)

    try:
        staging = Path(tempfile.mkdtemp(prefix=".memory-export-", suffix=".tmp", dir=memory_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create memory export staging: {exc}") from None
    try:
        _copy_tree(source_memory, staging)
        facts = _read_memory_facts(staging)
        _publish_memory_export(
            memory_dir,
            facts=facts,
            source_memory=source_memory,
            source_root=source_root,
            source_head=source_head,
            commit=source_head if source_memory == facts_dir else None,
            changed=False,
            record_import=False,
        )
    finally:
        _cleanup_staging_dir(staging)

    return MemoryExportSnapshot(
        path=memory_dir / "export.ndjson",
        count=len(facts),
        source=str(source_memory),
    )


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
    _ensure_dir(memory_dir, "memory data dir")
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
    _ensure_dir(memory_dir, "memory data dir")
    _ensure_writer_actor(actor)
    propose_id = _clean_proposal_id(propose_id)
    with _memory_journal_lock(memory_dir):
        proposal_dir = memory_dir / ".staging" / propose_id
        proposal = _read_proposal(proposal_dir)
        _ensure_commit_actor(actor, str(proposal["actor"]))
        result = _apply_memory_write(memory_dir, proposal, op="commit")
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
    _ensure_dir(memory_dir, "memory data dir")
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
        return _apply_memory_write(memory_dir, proposal, op="supersede")


def _read_memory_facts(facts_dir: Path) -> list[dict[str, Any]]:
    facts = []
    for path, file_stat in _regular_files_under(facts_dir, context="memory snapshot"):
        if path.suffix != ".md":
            continue
        relative = path.relative_to(facts_dir).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"could not read memory fact {relative}: {exc}") from None
        except UnicodeError as exc:
            raise RuntimeError(f"could not decode memory fact {relative}: {exc}") from None
        facts.append(
            {
                "id": relative.removesuffix(".md"),
                "path": relative,
                "bytes": file_stat.st_size,
                "mtime": int(file_stat.st_mtime),
                "metadata": _memory_fact_metadata(text),
                "text": text,
            }
        )
    return facts


def _memory_fact_metadata(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    try:
        loaded = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): _jsonable_metadata(value) for key, value in sorted(loaded.items())}


def _jsonable_metadata(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_jsonable_metadata(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable_metadata(item) for key, item in sorted(value.items())}
    return str(value)


def _resolve_memory_source(source_dir: Path) -> tuple[Path, Path]:
    candidate = source_dir.expanduser().resolve()
    if (candidate / "memory").is_dir():
        source_memory = candidate / "memory"
        source_root = candidate
    else:
        source_memory = candidate
        source_root = _source_git_root(candidate) or candidate.parent
    return source_root, source_memory


def _source_git_root(path: Path) -> Path | None:
    try:
        root = _git(path, ["rev-parse", "--show-toplevel"], context="inspect memory source")
    except RuntimeError:
        return None
    return Path(root.strip()).resolve() if root.strip() else None


def _source_git_head(source_root: Path) -> str:
    try:
        return _git(
            source_root,
            ["rev-parse", "HEAD"],
            context="inspect memory source head",
        ).strip()
    except RuntimeError:
        return "unknown"


@contextmanager
def _memory_journal_lock(memory_dir: Path):
    _ensure_dir(memory_dir, "memory data dir")
    lock_path = memory_dir / MEMORY_LOCK_NAME
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "created_at": int(time.time()),
    }
    _create_memory_lock(lock_path, payload)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _create_memory_lock(lock_path: Path, payload: dict[str, Any]) -> None:
    while True:
        temp_path = lock_path.with_name(f".{lock_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
            os.link(temp_path, lock_path)
            return
        except FileExistsError:
            if not _remove_stale_lock(lock_path):
                raise MemoryLockError(f"memory facts journal is locked: {lock_path}") from None
        except OSError as exc:
            raise RuntimeError(f"cannot lock memory facts journal: {exc}") from None
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _remove_stale_lock(lock_path: Path) -> bool:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("host") != socket.gethostname():
        return False
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        try:
            lock_path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False
    except PermissionError:
        return False
    return False


def _publish_memory_facts(staging: Path, facts_dir: Path) -> tuple[bool, Path]:
    backup = Path(
        tempfile.mkdtemp(prefix=".facts-rollback-", suffix=".tmp", dir=facts_dir.parent)
    )
    try:
        _copy_journal_files(facts_dir, backup)
        _replace_journal_files(staging, facts_dir)
    except RuntimeError:
        _restore_journal_files(facts_dir, backup)
        _cleanup_staging_dir(backup)
        raise
    return bool(_git_status(facts_dir)), backup


def _replace_journal_files(source: Path, facts_dir: Path) -> None:
    try:
        for path in sorted(facts_dir.iterdir()):
            if path.name == ".git":
                continue
            _remove_path(path)
        _copy_tree(source, facts_dir)
    except OSError as exc:
        raise RuntimeError(f"could not publish memory facts: {exc}") from None
    except RuntimeError as exc:
        raise RuntimeError(str(exc).replace("source", "memory import", 1)) from None


def _copy_journal_files(facts_dir: Path, destination: Path) -> None:
    for path in sorted(facts_dir.iterdir()):
        if path.name == ".git":
            continue
        target = destination / path.name
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.copytree(path, target, symlinks=False)
            elif path.is_file():
                shutil.copy2(path, target, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(f"could not prepare memory facts rollback: {exc}") from None


def _restore_journal_files(facts_dir: Path, backup: Path) -> None:
    try:
        for path in sorted(facts_dir.iterdir()):
            if path.name == ".git":
                continue
            _remove_path(path)
        _copy_tree(backup, facts_dir)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"could not restore memory facts journal: {exc}") from None


def _commit_memory_import(
    facts_dir: Path,
    *,
    source_root: Path,
    source_head: str,
    changed: bool,
) -> str | None:
    current = _journal_head(facts_dir)
    if not changed:
        return current
    _git(facts_dir, ["add", "-A", "."], context="stage memory import")
    subject = "memory import: seed from panelmem-kb"
    if current is not None:
        subject = "memory import: sync from panelmem-kb"
    message = (
        f"{subject} @ {source_head}\n\n"
        f"Source: {source_root}\n"
        f"Source-Head: {source_head}\n"
        f"{MEMORY_IMPORT_MARKER}\n"
    )
    _git(facts_dir, ["commit", "-m", message], context="commit memory import")
    return _journal_head(facts_dir)


def _publish_memory_export(
    memory_dir: Path,
    *,
    facts: list[dict[str, Any]],
    source_memory: Path,
    source_root: Path,
    source_head: str,
    commit: str | None,
    changed: bool,
    record_import: bool,
) -> None:
    try:
        staging = Path(
            tempfile.mkdtemp(prefix=".memory-export-", suffix=".tmp", dir=memory_dir)
        )
    except OSError as exc:
        raise RuntimeError(f"could not create memory export staging: {exc}") from None
    try:
        _write_ndjson(staging / "export.ndjson", facts)
        _write_json(
            staging / "export.json",
            {
                "version": 1,
                "source": str(source_memory),
                "fact_count": len(facts),
            },
        )
        _write_json(
            staging / "manifest.json",
            _memory_manifest(
                memory_dir,
                facts=facts,
                source_memory=source_memory,
                source_root=source_root,
                source_head=source_head,
                commit=commit,
                changed=changed,
                record_import=record_import,
            ),
        )
        _publish_component_entries(
            staging,
            memory_dir,
            ["export.ndjson", "export.json", "manifest.json"],
            "memory export",
        )
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise


def _memory_manifest(
    memory_dir: Path,
    *,
    facts: list[dict[str, Any]],
    source_memory: Path,
    source_root: Path,
    source_head: str,
    commit: str | None,
    changed: bool,
    record_import: bool,
) -> dict[str, Any]:
    old = _read_json_file_if_valid(memory_dir / "manifest.json")
    imports = old.get("imports", []) if isinstance(old.get("imports"), list) else []
    entry = {
        "source_head": source_head,
        "journal_commit": commit,
        "fact_count": len(facts),
        "changed": changed,
    }
    provenance_keys = ("source_head", "journal_commit", "fact_count")
    if record_import and (
        not imports or any(imports[-1].get(key) != entry[key] for key in provenance_keys)
    ):
        imports = [*imports, entry]
    return {
        "version": 1,
        "layout": {
            "facts": "facts",
            "export": "export.ndjson",
            "index": "index.sqlite",
        },
        "source": {
            "path": str(source_memory),
            "repo": str(source_root),
            "head": source_head,
            "readonly_fallback": True,
        },
        "journal": {
            "path": str(memory_dir / "facts"),
            "commit": commit,
            "fact_count": len(facts),
        },
        "imports": imports,
    }


def _read_json_file_if_valid(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _ensure_import_only_journal(facts_dir: Path) -> None:
    commits = _git_log_messages(facts_dir)
    for commit_hash, message in commits:
        if MEMORY_IMPORT_MARKER not in message:
            short = commit_hash[:12]
            raise RuntimeError(
                f"memory import refused after non-import journal commit {short}"
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


def _recover_journal_worktree(facts_dir: Path) -> None:
    if not (facts_dir / ".git").is_dir():
        return
    if not _git_status(facts_dir):
        return
    if _journal_head(facts_dir) is None:
        for path in sorted(facts_dir.iterdir()):
            if path.name != ".git":
                _remove_path(path)
        return
    _git(facts_dir, ["reset", "--hard", "HEAD"], context="recover memory journal")
    _git(facts_dir, ["clean", "-fd"], context="recover memory journal")


def _git_log_messages(facts_dir: Path) -> list[tuple[str, str]]:
    if _journal_head(facts_dir) is None:
        return []
    raw = _git(facts_dir, ["log", "--format=%H%x00%B%x1e"], context="inspect memory journal")
    commits: list[tuple[str, str]] = []
    for item in raw.split("\x1e"):
        item = item.strip("\n")
        if not item:
            continue
        commit_hash, _, message = item.partition("\x00")
        commits.append((commit_hash, message))
    return commits


def _journal_head(facts_dir: Path) -> str | None:
    try:
        return _git(facts_dir, ["rev-parse", "--verify", "HEAD"], context="inspect memory journal")
    except RuntimeError:
        return None


def _git_status(facts_dir: Path) -> str:
    return _git(
        facts_dir,
        ["status", "--porcelain", "--untracked-files=all"],
        context="inspect memory journal status",
    )


def _git(cwd: Path, args: list[str], *, context: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("git command not found") from None
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or exc.stdout or f"git {' '.join(args)} failed").strip().splitlines()
        raise RuntimeError(f"{context}: {reason[-1] if reason else 'git failed'}") from None
    return result.stdout.strip()
