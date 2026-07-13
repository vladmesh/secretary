from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
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
)
from secretary.memory_errors import MemoryLockError, MemoryProtocolError


PANELMEM_KB = Path("/home/dev/panelmem-kb")
MEMORY_IMPORT_MARKER = "Op: import"
MEMORY_GIT_NAME = "Secretary Memory"
MEMORY_GIT_EMAIL = "secretary-memory@localhost"
MEMORY_LOCK_NAME = ".write.lock"


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
class MemoryVerify:
    facts_dir: Path
    ok: bool
    findings: tuple[str, ...]
    journal_commit: str | None
    fact_count: int
    export_count: int | None
    index_count: int | None
    dirty: bool


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
    with _memory_journal_lock(memory_dir):
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
            staging = Path(
                tempfile.mkdtemp(prefix=".memory-export-", suffix=".tmp", dir=memory_dir)
            )
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


def verify_memory_journal(data_dir: Path) -> MemoryVerify:
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    facts_dir = memory_dir / "facts"
    findings: list[str] = []
    journal_commit: str | None = None
    fact_count = 0
    export_count: int | None = None
    index_count: int | None = None
    dirty = False

    with _memory_journal_lock(memory_dir):
        if not (facts_dir / ".git").is_dir():
            findings.append(f"memory facts journal is not a git repo: {facts_dir}")
        else:
            journal_commit = _journal_head(facts_dir)
            if journal_commit is None:
                findings.append("memory facts journal has no commits")
            status = _git_status(facts_dir)
            dirty = bool(status)
            if status:
                findings.append("memory facts journal has uncommitted changes")
            remotes = _git(facts_dir, ["remote"], context="inspect memory journal remotes")
            if remotes.splitlines():
                findings.append("memory facts journal has remotes configured")
            if journal_commit is not None:
                fact_count = len(_tracked_fact_ids(facts_dir))

        export_path = memory_dir / "export.ndjson"
        if not export_path.is_file():
            findings.append(f"memory export missing: {export_path}")
        else:
            export_ids = _read_export_fact_ids(export_path)
            export_count = len(export_ids)
            if fact_count and export_count != fact_count:
                findings.append(
                    f"memory export count mismatch: export={export_count} journal={fact_count}"
                )

        index_path = memory_dir / "index.sqlite"
        if not index_path.is_file():
            findings.append(f"memory index missing: {index_path}")
        else:
            index_count = _read_index_fact_count(index_path)
            if fact_count and index_count != fact_count:
                findings.append(
                    f"memory index count mismatch: index={index_count} journal={fact_count}"
                )

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


def _tracked_fact_ids(facts_dir: Path) -> list[str]:
    raw = _git(facts_dir, ["ls-files", "-z"], context="inspect memory journal files")
    fact_ids = []
    for item in raw.split("\0"):
        if item.endswith(".md"):
            fact_ids.append(item.removesuffix(".md"))
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
