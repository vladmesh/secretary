from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from secretary import state_repo
from secretary._fsutil import (
    cleanup_staging_dir as _cleanup_staging_dir,
)
from secretary._fsutil import (
    copy_tree as _copy_tree,
)
from secretary._fsutil import (
    ensure_dir as _ensure_dir,
)
from secretary._fsutil import (
    publish_component_entries as _publish_component_entries,
)
from secretary._fsutil import (
    regular_files_under as _regular_files_under,
)
from secretary._fsutil import (
    write_json as _write_json,
)
from secretary._fsutil import (
    write_ndjson as _write_ndjson,
)
from secretary.memory_errors import MemoryLockError, MemoryProtocolError
from secretary.state_repo import MEMORY_PATHSPEC, StateRepoError

MEMORY_LOCK_NAME = ".write.lock"


@dataclass(frozen=True)
class MemoryExportSnapshot:
    path: Path
    count: int
    source: str


@dataclass(frozen=True)
class MemoryVerify:
    facts_dir: Path
    ok: bool
    findings: tuple[str, ...]
    journal_commit: str | None
    fact_count: int
    export_count: int | None
    index_count: int | None
    dirty: bool


def init_memory_journal(instance_dir: Path) -> tuple[Path, bool]:
    """Resolve `state/memory/facts` in the private repo.

    Contract: docs/RECOVERY.md, "Layout". Facts live flat in the single instance
    repository; there is no nested journal to initialize.
    """
    instance_dir = state_repo.require_repo(instance_dir)
    facts_dir = state_repo.memory_facts_dir(instance_dir)
    created = not facts_dir.is_dir()
    try:
        facts_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot prepare memory facts dir: {exc}") from None
    return facts_dir, created


def reject_legacy_memory_journal(memory_dir: Path) -> None:
    """Refuse to write or publish while a pre-flatten journal sits in the data dir.

    Facts live in `state/memory/facts` of the instance repo and nowhere else, so
    a `<data-dir>/memory/facts` left by an older release holds facts this
    release cannot read. Carrying them over on the fly is the compatibility
    promise this product dropped, so the boundary refuses instead: the operator
    moves them, and until then nothing writes a canon that silently excludes
    them.
    """
    legacy = memory_dir / "facts"
    if not _legacy_journal_facts(legacy):
        return
    raise MemoryProtocolError(
        f"legacy memory journal is still present: {legacy}. "
        "This release reads memory only from state/memory/facts in the instance repo; "
        "move these facts there and remove the directory before writing memory again."
    )


def _legacy_journal_facts(legacy: Path) -> bool:
    if (legacy / ".git").exists():
        return True
    try:
        return any(legacy.rglob("*.md"))
    except OSError:
        return False


def export_memory_snapshot(data_dir: Path, instance_dir: Path) -> MemoryExportSnapshot:
    """Refresh the derived export from the live facts in the private repo.

    The instance repo is the only source, so an export cannot carry facts that
    are not this installation's canon.
    """
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    _ensure_dir(memory_dir, "memory data dir")
    facts_dir = state_repo.memory_facts_dir(instance_dir)
    with _memory_journal_lock(memory_dir):
        source_head = _journal_head(instance_dir) or "unknown"
        try:
            staging = Path(tempfile.mkdtemp(prefix=".memory-export-", suffix=".tmp", dir=memory_dir))
        except OSError as exc:
            raise RuntimeError(f"could not create memory export staging: {exc}") from None
        try:
            _copy_tree(facts_dir, staging)
            facts = _read_memory_facts(staging)
            _publish_memory_export(
                memory_dir,
                facts=facts,
                source_memory=facts_dir,
                source_root=facts_dir,
                source_head=source_head,
                commit=source_head,
                changed=False,
                record_import=False,
            )
        finally:
            _cleanup_staging_dir(staging)

    return MemoryExportSnapshot(
        path=memory_dir / "export.ndjson",
        count=len(facts),
        source=str(facts_dir),
    )


def verify_memory_journal(data_dir: Path, instance_dir: Path) -> MemoryVerify:
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    findings: list[str] = []
    journal_commit: str | None = None
    fact_count = 0
    export_count: int | None = None
    index_count: int | None = None
    dirty = False

    try:
        instance_dir = state_repo.require_repo(instance_dir)
    except StateRepoError as exc:
        instance_dir = Path(instance_dir).expanduser().resolve()
        findings.append(str(exc))
    facts_dir = state_repo.memory_facts_dir(instance_dir)

    with _memory_journal_lock(memory_dir):
        if not findings:
            legacy = memory_dir / "facts"
            if (legacy / ".git").is_dir():
                findings.append(f"nested memory journal is still present: {legacy}")
            journal_commit = _journal_head(instance_dir)
            if journal_commit is None:
                findings.append("no memory commit in the instance repo")
            status = _git_status(instance_dir)
            dirty = bool(status)
            if status:
                findings.append("state/memory has uncommitted changes")
            if journal_commit is not None:
                fact_count = len(_tracked_fact_ids(instance_dir))

        export_path = memory_dir / "export.ndjson"
        if not export_path.is_file():
            findings.append(f"memory export missing: {export_path}")
        else:
            export_ids = _read_export_fact_ids(export_path)
            export_count = len(export_ids)
            if fact_count and export_count != fact_count:
                findings.append(f"memory export count mismatch: export={export_count} journal={fact_count}")

        index_path = memory_dir / "index.sqlite"
        if not index_path.is_file():
            findings.append(f"memory index missing: {index_path}")
        else:
            index_count = _read_index_fact_count(index_path)
            if fact_count and index_count != fact_count:
                findings.append(f"memory index count mismatch: index={index_count} journal={fact_count}")

    return MemoryVerify(
        facts_dir=facts_dir,
        ok=not findings,
        findings=tuple(findings),
        journal_commit=journal_commit,
        fact_count=fact_count,
        export_count=export_count,
        index_count=index_count,
        dirty=dirty,
    )


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


def _tracked_fact_ids(instance_dir: Path) -> list[str]:
    prefix = f"{state_repo.MEMORY_FACTS_RELATIVE.as_posix()}/"
    raw = state_repo.git(
        instance_dir,
        ["ls-files", "-z", "--", *MEMORY_PATHSPEC],
        label="inspect memory files",
    )
    fact_ids = []
    for item in raw.split("\0"):
        if item.startswith(prefix) and item.endswith(".md"):
            fact_ids.append(item.removeprefix(prefix).removesuffix(".md"))
    return fact_ids


def _read_export_fact_ids(path: Path) -> list[str]:
    ids: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"could not read memory export {path}: {exc}") from None
    except UnicodeError as exc:
        raise RuntimeError(f"could not decode memory export {path}: {exc}") from None
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid memory export JSON at line {number}: {exc}") from None
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid memory export row at line {number}: not an object")
        fact_id = payload.get("id")
        if not isinstance(fact_id, str) or not fact_id:
            raise RuntimeError(f"invalid memory export row at line {number}: missing id")
        ids.append(fact_id)
    return ids


def _read_index_fact_count(path: Path) -> int:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            return int(conn.execute("select count(*) from memories").fetchone()[0])
    except sqlite3.Error as exc:
        raise RuntimeError(f"could not read memory index {path}: {exc}") from None


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
    reject_legacy_memory_journal(memory_dir)
    try:
        staging = Path(tempfile.mkdtemp(prefix=".memory-export-", suffix=".tmp", dir=memory_dir))
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
    if record_import and (not imports or any(imports[-1].get(key) != entry[key] for key in provenance_keys)):
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


def _recover_journal_worktree(instance_dir: Path) -> None:
    """Roll back a half-applied write, touching `state/memory` and nothing else.

    The repo also carries board, runs and the operator's uncommitted config, so
    a repo-wide `reset --hard` is not available here: recovery is scoped to the
    memory pathspec by construction.
    """
    if not state_repo.status(instance_dir, MEMORY_PATHSPEC):
        return
    if _journal_head(instance_dir) is not None:
        state_repo.git(
            instance_dir,
            ["checkout", "--", *MEMORY_PATHSPEC],
            label="recover memory worktree",
        )
    state_repo.git(
        instance_dir,
        ["clean", "-fdq", "--", *MEMORY_PATHSPEC],
        label="recover memory worktree",
    )


def _journal_head(instance_dir: Path) -> str | None:
    """The last commit that touched `state/memory`, not the repo tip."""
    if state_repo.head(instance_dir) is None:
        return None
    raw = state_repo.git(
        instance_dir,
        ["log", "-1", "--format=%H", "--", *MEMORY_PATHSPEC],
        label="inspect memory head",
    ).strip()
    return raw or None


def _git_status(instance_dir: Path) -> str:
    return state_repo.status(instance_dir, MEMORY_PATHSPEC)
