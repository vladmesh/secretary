from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import stat as stat_module
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from secretary.config import validate


LAYOUT_DIRS = ("board", "memory", "runs", "transcripts", "artifacts", "backups")
KANBOARD_DATA_PATH = "/var/www/app/data"
PIPELINE_WORKTREE = Path("/home/dev/orca/workspaces/triggered-agents/pipeline")
ORCA_WORKSPACES_ROOT = Path("/home/dev/orca/workspaces")
PANELMEM_KB = Path("/home/dev/panelmem-kb")
MEMORY_IMPORT_MARKER = "Op: import"
MEMORY_GIT_NAME = "Secretary Memory"
MEMORY_GIT_EMAIL = "secretary-memory@localhost"
PIPELINE_STATE_DIR = PIPELINE_WORKTREE / "state"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


@dataclass(frozen=True)
class DataLayout:
    data_dir: Path
    manifest_path: Path
    created_dirs: list[Path]


@dataclass(frozen=True)
class KanboardDump:
    dump_dir: Path
    source: str


@dataclass(frozen=True)
class DataExport:
    path: Path
    count: int
    source: str


@dataclass(frozen=True)
class MemoryImport:
    facts_dir: Path
    count: int
    source: str
    source_head: str
    commit: str | None
    changed: bool
    initialized: bool


def manifest_for(data_dir: Path) -> dict[str, Any]:
    data_dir = data_dir.expanduser().resolve()
    return {
        "version": 1,
        "data_dir": str(data_dir),
        "components": {
            "board": {"path": "board"},
            "memory": {
                "path": "memory",
                "facts": "memory/facts",
                "export": "memory/export.ndjson",
                "index": "memory/index.sqlite",
            },
            "runs": {"path": "runs"},
            "transcripts": {"path": "transcripts"},
            "artifacts": {"path": "artifacts"},
            "backups": {"path": "backups"},
        },
    }


def init_layout(data_dir: Path) -> DataLayout:
    data_dir = data_dir.expanduser().resolve()
    created_dirs: list[Path] = []
    try:
        for relative in LAYOUT_DIRS:
            directory = data_dir / relative
            existed = directory.is_dir()
            directory.mkdir(parents=True, exist_ok=True)
            if not existed:
                created_dirs.append(directory)
        init_memory_journal(data_dir)
    except OSError as exc:
        raise RuntimeError(f"cannot prepare secretary-data layout: {exc}") from None

    manifest_path = data_dir / "data-manifest.json"
    _write_data_manifest(manifest_path, manifest_for(data_dir))
    return DataLayout(
        data_dir=data_dir,
        manifest_path=manifest_path,
        created_dirs=created_dirs,
    )


def raw_kanboard_dump(
    data_dir: Path,
    *,
    container: str = "cp-kanboard",
    source_path: str = KANBOARD_DATA_PATH,
) -> KanboardDump:
    data_dir = data_dir.expanduser().resolve()
    board_dir = data_dir / "board"
    try:
        board_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot prepare board data dir: {exc}") from None

    staging_dir: Path | None = None

    try:
        staging_dir = Path(
            tempfile.mkdtemp(prefix=".kanboard-raw-", suffix=".tmp", dir=board_dir)
        )
        destination = staging_dir / "data"
        subprocess.run(
            ["docker", "cp", f"{container}:{source_path}", str(destination)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        metadata = {
            "version": 1,
            "kind": "kanboard-raw",
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "container": container,
            "source_path": source_path,
        }
        (staging_dir / "manifest.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        dump_dir = _publish_dump_dir(staging_dir, board_dir)
    except FileNotFoundError:
        _cleanup_staging_dir(staging_dir)
        raise RuntimeError("docker command not found") from None
    except subprocess.CalledProcessError as exc:
        _cleanup_staging_dir(staging_dir)
        reason = (exc.stderr or exc.stdout or "docker cp failed").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else "docker cp failed") from None
    except OSError as exc:
        _cleanup_staging_dir(staging_dir)
        raise RuntimeError(f"could not create raw dump: {exc}") from None

    return KanboardDump(dump_dir=dump_dir, source=f"{container}:{source_path}")


def export_board(
    data_dir: Path,
    *,
    pipeline_worktree: Path = PIPELINE_WORKTREE,
    command: list[str] | None = None,
) -> DataExport:
    data_dir = data_dir.expanduser().resolve()
    board_dir = data_dir / "board"
    _ensure_dir(board_dir, "board data dir")

    cards = _pipeline_json(["list"], pipeline_worktree=pipeline_worktree, command=command)
    if not isinstance(cards, list):
        raise RuntimeError("pipeline list did not return a card list")

    normalized = []
    for card in sorted(cards, key=lambda item: str(item.get("reference", ""))):
        reference = str(card.get("reference") or "")
        if not reference:
            continue
        shown = _pipeline_json(
            ["show", "--ref", reference],
            pipeline_worktree=pipeline_worktree,
            command=command,
        )
        if not isinstance(shown, dict):
            raise RuntimeError(f"pipeline show returned invalid payload for {reference}")
        normalized.append(normalize_board_card(card, shown))

    raw_active_task_count = _latest_raw_active_task_count(
        board_dir,
        board_name=os.environ.get("TA_PIPELINE_BOARD", "Pipeline"),
    )
    if raw_active_task_count is not None and raw_active_task_count != len(normalized):
        raise RuntimeError(
            "board export count mismatch: "
            f"pipeline={len(normalized)} raw_active={raw_active_task_count}"
        )

    summary = {
        "version": 1,
        "source": "triggered_agents pipeline",
        "card_count": len(normalized),
        "raw_active_task_count": raw_active_task_count,
    }
    try:
        staging = Path(tempfile.mkdtemp(prefix=".board-export-", suffix=".tmp", dir=board_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create board export staging: {exc}") from None
    try:
        _write_json(staging / "cards.json", {"version": 1, "cards": normalized})
        _write_ndjson(staging / "cards.ndjson", normalized)
        _write_json(staging / "export.json", summary)
        _publish_component_entries(
            staging,
            board_dir,
            ["cards.json", "cards.ndjson", "export.json"],
            "board export",
        )
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise
    return DataExport(path=board_dir / "cards.json", count=len(normalized), source=summary["source"])


def normalize_board_card(list_card: dict[str, Any], shown_card: dict[str, Any]) -> dict[str, Any]:
    metadata = shown_card.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    comments = shown_card.get("comments")
    if not isinstance(comments, list):
        comments = []
    return {
        "id": _int_or_none(shown_card.get("id", list_card.get("id"))),
        "reference": str(shown_card.get("reference") or list_card.get("reference") or ""),
        "title": str(shown_card.get("title") or list_card.get("title") or ""),
        "description": str(shown_card.get("description") or ""),
        "swimlane": str(list_card.get("swimlane") or ""),
        "column": str(shown_card.get("column") or list_card.get("column") or ""),
        "position": _int_or_none(list_card.get("position")) or 0,
        "date_moved": _int_or_none(list_card.get("date_moved")),
        "metadata": {str(k): str(v) for k, v in sorted(metadata.items())},
        "fields": {
            "task_type": str(shown_card.get("task_type") or list_card.get("task_type") or ""),
            "project": str(shown_card.get("project") or list_card.get("project") or ""),
            "blocked_by": str(shown_card.get("blocked_by") or list_card.get("blocked_by") or ""),
            "head": str(shown_card.get("head") or list_card.get("head") or ""),
            "effective_head": str(
                shown_card.get("effective_head") or list_card.get("effective_head") or ""
            ),
            "review_head": str(shown_card.get("review_head") or list_card.get("review_head") or ""),
            "effective_review_head": str(
                shown_card.get("effective_review_head")
                or list_card.get("effective_review_head")
                or ""
            ),
            "claim": str(shown_card.get("claim") or list_card.get("claim") or ""),
            "slug": str(shown_card.get("slug") or list_card.get("slug") or ""),
            "base_branch": str(shown_card.get("base_branch") or list_card.get("base_branch") or ""),
        },
        "comments": [
            {"ts": str(comment.get("ts", "")), "text": str(comment.get("text", ""))}
            for comment in comments
            if isinstance(comment, dict)
        ],
    }


def export_memory(
    data_dir: Path,
    *,
    source_dir: Path = PANELMEM_KB,
) -> DataExport:
    data_dir = data_dir.expanduser().resolve()
    result = import_memory_journal(data_dir, source_dir=source_dir)
    return DataExport(
        path=data_dir / "memory" / "export.ndjson",
        count=result.count,
        source=result.source,
    )


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
        return _git(source_root, ["rev-parse", "HEAD"], context="inspect memory source head").strip()
    except RuntimeError:
        return "unknown"


@contextmanager
def _memory_journal_lock(memory_dir: Path):
    lock_path = memory_dir / ".journal.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RuntimeError(f"memory facts journal is locked: {lock_path}") from None
    except OSError as exc:
        raise RuntimeError(f"cannot lock memory facts journal: {exc}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\n")
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


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
    if not imports or any(imports[-1].get(key) != entry[key] for key in provenance_keys):
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


def export_runs(
    data_dir: Path,
    *,
    state_dir: Path = PIPELINE_STATE_DIR,
) -> DataExport:
    data_dir = data_dir.expanduser().resolve()
    state_dir = state_dir.expanduser().resolve()
    if not state_dir.is_dir():
        raise RuntimeError(f"state source not found: {state_dir}")

    runs_dir = data_dir / "runs"
    _ensure_dir(runs_dir, "runs data dir")
    records: list[dict[str, Any]] = []
    watermarks: list[dict[str, Any]] = []
    try:
        snapshot = Path(tempfile.mkdtemp(prefix=".state-", suffix=".tmp", dir=runs_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create runs snapshot: {exc}") from None
    try:
        _copy_tree(state_dir, snapshot)
        for path, file_stat in _regular_files_under(snapshot, context="runs snapshot"):
            if path.name.endswith(".lock") or path.suffix not in {".json", ".jsonl"}:
                continue
            relative = path.relative_to(snapshot).as_posix()
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise RuntimeError(f"could not read state file {relative}: {exc}") from None
            except UnicodeError as exc:
                raise RuntimeError(f"could not decode state file {relative}: {exc}") from None
            watermarks.append(
                {
                    "path": relative,
                    "bytes": file_stat.st_size,
                    "mtime": int(file_stat.st_mtime),
                    "lines": len(lines),
                }
            )
            if path.suffix == ".jsonl":
                for number, line in enumerate(lines, start=1):
                    if not line.strip():
                        continue
                    records.append(
                        {
                            "source": relative,
                            "line": number,
                            "record": _parse_jsonl_line(line, relative, number),
                        }
                    )

        cards_path = snapshot / "pipeline" / "cards.json"
        cards = _read_json_file_strict(cards_path) if cards_path.is_file() else {}
        if not isinstance(cards, dict):
            raise RuntimeError(f"state card mapping must be an object: {cards_path}")
        claims_path = snapshot / "pipeline" / "claims.json"
        claims = _read_json_file_strict(claims_path) if claims_path.is_file() else {}
        if not isinstance(claims, dict):
            raise RuntimeError(f"state claims must be an object: {claims_path}")
    finally:
        _cleanup_staging_dir(snapshot)

    try:
        staging = Path(tempfile.mkdtemp(prefix=".runs-export-", suffix=".tmp", dir=runs_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create runs export staging: {exc}") from None
    try:
        _write_ndjson(staging / "runs.ndjson", records)
        _write_json(staging / "watermarks.json", {"version": 1, "files": watermarks})
        _write_json(staging / "cards.json", {"version": 1, "cards": cards})
        _write_json(staging / "claims.json", {"version": 1, "claims": claims})
        _write_json(
            staging / "export.json",
            {
                "version": 1,
                "source": str(state_dir),
                "run_record_count": len(records),
                "watermark_count": len(watermarks),
                "card_mapping_count": len(cards) if isinstance(cards, dict) else 0,
                "claim_count": len(claims),
            },
        )
        _publish_component_entries(
            staging,
            runs_dir,
            ["runs.ndjson", "watermarks.json", "cards.json", "claims.json", "export.json"],
            "runs export",
        )
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise
    return DataExport(path=runs_dir / "runs.ndjson", count=len(records), source=str(state_dir))


def export_transcripts(
    data_dir: Path,
    *,
    roots: list[Path] | None = None,
    copy: bool = False,
) -> DataExport:
    data_dir = data_dir.expanduser().resolve()
    roots = roots or [CLAUDE_PROJECTS_DIR, CODEX_SESSIONS_DIR]
    transcripts_dir = data_dir / "transcripts"
    _ensure_dir(transcripts_dir, "transcripts data dir")

    entries = []
    for root in roots:
        root = root.expanduser().resolve()
        if not root.exists():
            continue
        for path, file_stat in _regular_files_under(root, context="transcript source"):
            if path.suffix != ".jsonl":
                continue
            entry = {
                "path": str(path),
                "root": str(root),
                "relative_path": path.relative_to(root).as_posix(),
                "bytes": file_stat.st_size,
                "mtime": int(file_stat.st_mtime),
            }
            entries.append(entry)

    try:
        staging = Path(tempfile.mkdtemp(prefix=".transcripts-export-", suffix=".tmp", dir=transcripts_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create transcripts export staging: {exc}") from None
    try:
        _write_json(staging / "inventory.json", {"version": 1, "transcripts": entries})
        _write_ndjson(staging / "inventory.ndjson", entries)
        if copy:
            copy_dir = staging / "copies"
            try:
                copy_dir.mkdir(parents=True, exist_ok=True)
                for entry in entries:
                    destination = copy_dir / _safe_relative_copy_path(
                        entry["root"],
                        entry["relative_path"],
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(entry["path"], destination, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"could not copy transcripts: {exc}") from None
        _publish_component_entries(
            staging,
            transcripts_dir,
            ["inventory.json", "inventory.ndjson", "copies"],
            "transcripts export",
        )
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise
    return DataExport(path=transcripts_dir / "inventory.json", count=len(entries), source=", ".join(str(p) for p in roots))


def export_artifacts(
    data_dir: Path,
    *,
    workspaces_root: Path = ORCA_WORKSPACES_ROOT,
) -> DataExport:
    data_dir = data_dir.expanduser().resolve()
    artifacts_dir = data_dir / "artifacts"
    _ensure_dir(artifacts_dir, "artifacts data dir")

    entries = []
    for path, file_stat in _regular_files_under(artifacts_dir, context="artifact source"):
        relative = path.relative_to(artifacts_dir)
        if _skip_artifact_relative(relative):
            continue
        entries.append(
            {
                "kind": "existing",
                "relative_path": relative.as_posix(),
                "bytes": file_stat.st_size,
                "mtime": int(file_stat.st_mtime),
            }
        )

    task_docs = _task_artifact_docs(workspaces_root)
    entries.extend(task_docs)

    try:
        staging = Path(
            tempfile.mkdtemp(prefix=".artifacts-export-", suffix=".tmp", dir=artifacts_dir)
        )
    except OSError as exc:
        raise RuntimeError(f"could not create artifacts export staging: {exc}") from None
    try:
        _write_json(
            staging / "inventory.json",
            {
                "version": 1,
                "policy": {
                    "project_worktrees": "only TASK.md and REVIEW.md root docs are copied",
                    "project_env": "excluded",
                },
                "artifacts": entries,
            },
        )
        _write_ndjson(staging / "inventory.ndjson", entries)
        for entry in task_docs:
            source = Path(entry["path"])
            destination = staging / "task-docs" / entry["relative_path"]
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(
                    f"could not copy task artifact {entry['relative_path']}: {exc}"
                ) from None
        _publish_component_entries(
            staging,
            artifacts_dir,
            ["inventory.json", "inventory.ndjson", "task-docs"],
            "artifacts export",
        )
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise
    return DataExport(
        path=artifacts_dir / "inventory.json",
        count=len(entries),
        source=str(workspaces_root),
    )


def export_all(data_dir: Path, *, copy_transcripts: bool = False) -> dict[str, DataExport]:
    return {
        "board": export_board(data_dir),
        "memory": export_memory(data_dir),
        "runs": export_runs(data_dir),
        "transcripts": export_transcripts(data_dir, copy=copy_transcripts),
        "artifacts": export_artifacts(data_dir),
    }


def _write_data_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, indent=2, sort_keys=False) + "\n"
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{manifest_path.name}.",
            suffix=".tmp",
            dir=manifest_path.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        os.close(fd)
        temp_path.write_text(payload, encoding="utf-8")
        try:
            candidate = json.loads(temp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError(f"generated invalid data manifest: {exc}") from None
        errors = validate(candidate, "data-manifest", manifest_path.name)
        if errors:
            details = "; ".join(str(error) for error in errors)
            raise RuntimeError(f"generated invalid data manifest: {details}") from None
        os.replace(temp_path, manifest_path)
    except OSError as exc:
        raise RuntimeError(f"could not write data manifest: {exc}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _pipeline_json(
    args: list[str],
    *,
    pipeline_worktree: Path,
    command: list[str] | None,
) -> Any:
    cmd = command or [sys.executable, "-m", "triggered_agents", "pipeline"]
    env = os.environ.copy()
    pythonpath = str(pipeline_worktree)
    if env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}{os.pathsep}{env['PYTHONPATH']}"
    env["PYTHONPATH"] = pythonpath
    try:
        result = subprocess.run(
            [*cmd, *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        raise RuntimeError(f"pipeline command not found: {cmd[0]}") from None
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or exc.stdout or "pipeline command failed").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else "pipeline command failed") from None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"pipeline command returned invalid JSON: {exc}") from None


def _write_json(path: Path, payload: Any) -> None:
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_ndjson(path: Path, rows: list[dict[str, Any]]) -> None:
    body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    _write_text_atomic(path, body)


def _write_text_atomic(path: Path, payload: str) -> None:
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temp_path, path)
    except OSError as exc:
        raise RuntimeError(f"could not write export file {path}: {exc}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _replace_dir_from_tree(source: Path, destination: Path) -> None:
    try:
        staging = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent)
        )
    except OSError as exc:
        raise RuntimeError(f"could not mirror {source}: {exc}") from None
    try:
        _copy_tree(source, staging)
        _replace_dir(staging, destination)
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise
    except OSError as exc:
        _cleanup_staging_dir(staging)
        raise RuntimeError(f"could not mirror {source}: {exc}") from None


def _copy_tree(source: Path, destination: Path) -> None:
    try:
        paths = sorted(source.rglob("*"))
    except OSError as exc:
        raise RuntimeError(f"could not list source tree {source}: {exc}") from None
    for path in paths:
        if _has_git_part(source, path):
            continue
        relative = path.relative_to(source)
        target = destination / relative
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise RuntimeError(f"could not inspect source file {relative}: {exc}") from None
        if stat_module.S_ISLNK(mode):
            continue
        if stat_module.S_ISDIR(mode):
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(f"could not copy source directory {relative}: {exc}") from None
        elif stat_module.S_ISREG(mode):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"could not copy source file {relative}: {exc}") from None


def _regular_files_under(root: Path, *, context: str) -> list[tuple[Path, os.stat_result]]:
    files: list[tuple[Path, os.stat_result]] = []
    try:
        paths = sorted(root.rglob("*"))
    except OSError as exc:
        raise RuntimeError(f"could not list {context} {root}: {exc}") from None
    for path in paths:
        if _has_git_part(root, path):
            continue
        try:
            file_stat = path.lstat()
        except OSError as exc:
            relative = _display_relative(root, path)
            raise RuntimeError(f"could not inspect {context} {relative}: {exc}") from None
        mode = file_stat.st_mode
        if stat_module.S_ISLNK(mode):
            continue
        if stat_module.S_ISREG(mode):
            files.append((path, file_stat))
    return files


def _has_git_part(root: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return ".git" in parts


def _display_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _replace_dir(staging: Path, destination: Path) -> None:
    backup = destination.with_name(f".{destination.name}.old")
    try:
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            os.replace(destination, backup)
        os.replace(staging, destination)
        if backup.exists():
            shutil.rmtree(backup)
    except OSError:
        if not destination.exists() and backup.exists():
            os.replace(backup, destination)
        raise


def _publish_component_entries(
    staging: Path,
    destination: Path,
    entries: list[str],
    label: str,
) -> None:
    try:
        backup = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}-old-", suffix=".tmp", dir=destination)
        )
    except OSError as exc:
        raise RuntimeError(f"could not publish {label}: {exc}") from None

    try:
        for entry in entries:
            current = destination / entry
            if current.exists():
                os.replace(current, backup / entry)

        for entry in entries:
            source = staging / entry
            target = destination / entry
            if source.exists():
                os.replace(source, target)

        shutil.rmtree(staging)
    except OSError as exc:
        try:
            _restore_component_entries(destination, backup, entries)
        except OSError as restore_exc:
            raise RuntimeError(
                f"could not publish {label}: {exc}; rollback failed: {restore_exc}"
            ) from None
        raise RuntimeError(f"could not publish {label}: {exc}") from None
    finally:
        shutil.rmtree(backup, ignore_errors=True)


def _restore_component_entries(destination: Path, backup: Path, entries: list[str]) -> None:
    for entry in entries:
        current = destination / entry
        if current.exists():
            _remove_path(current)
        saved = backup / entry
        if saved.exists():
            os.replace(saved, destination / entry)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _ensure_dir(path: Path, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot prepare {label}: {exc}") from None


def _latest_raw_active_task_count(board_dir: Path, *, board_name: str) -> int | None:
    dumps = sorted(board_dir.glob("kanboard-raw-*"), key=lambda path: path.name, reverse=True)
    for dump in dumps:
        database = dump / "data" / "db.sqlite"
        if not database.is_file():
            continue
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
                columns = {
                    row[1]
                    for row in conn.execute("pragma table_info(tasks)").fetchall()
                }
                project_columns = {
                    row[1]
                    for row in conn.execute("pragma table_info(projects)").fetchall()
                }
                project = None
                if {"id", "name"}.issubset(project_columns):
                    project = conn.execute(
                        "select id from projects where name = ?",
                        (board_name,),
                    ).fetchone()
                if project is None or "project_id" not in columns:
                    # Нельзя привязать tasks к нужной доске: глобальный счёт зацепил бы
                    # чужие проекты, поэтому сверку пропускаем, а не считаем что попало.
                    return None
                if "is_active" in columns:
                    query = "select count(*) from tasks where is_active = 1 and project_id = ?"
                else:
                    query = "select count(*) from tasks where project_id = ?"
                return int(conn.execute(query, (int(project[0]),)).fetchone()[0])
        except sqlite3.Error:
            continue
    return None


def _parse_jsonl_line(line: str, relative: str, number: int) -> Any:
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSONL in state file {relative}:{number}: {exc}") from None


def _read_json_file_strict(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"could not read state file {path}: {exc}") from None
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RuntimeError(f"invalid JSON in state file {path}: {exc}") from None


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_relative_copy_path(root: str, relative: str) -> Path:
    prefix = root.strip("/").replace("/", "__") or "root"
    return Path(prefix) / relative


def _task_artifact_docs(workspaces_root: Path) -> list[dict[str, Any]]:
    workspaces_root = workspaces_root.expanduser().resolve()
    if not workspaces_root.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    try:
        projects = sorted(path for path in workspaces_root.iterdir() if path.is_dir())
    except OSError as exc:
        raise RuntimeError(f"could not list workspaces root {workspaces_root}: {exc}") from None
    for project_dir in projects:
        if project_dir.name.startswith("."):
            continue
        try:
            workspaces = sorted(path for path in project_dir.iterdir() if path.is_dir())
        except OSError:
            continue
        for workspace in workspaces:
            if workspace.name.startswith("."):
                continue
            for name in ("TASK.md", "REVIEW.md"):
                path = workspace / name
                try:
                    file_stat = path.lstat()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    relative = _display_relative(workspaces_root, path)
                    raise RuntimeError(f"could not inspect task artifact {relative}: {exc}") from None
                mode = file_stat.st_mode
                if not stat_module.S_ISREG(mode):
                    continue
                relative = path.relative_to(workspaces_root)
                if _skip_artifact_relative(relative):
                    continue
                entries.append(
                    {
                        "kind": "task-doc",
                        "path": str(path),
                        "relative_path": relative.as_posix(),
                        "bytes": file_stat.st_size,
                        "mtime": int(file_stat.st_mtime),
                    }
                )
    return entries


def _skip_artifact_relative(relative: Path) -> bool:
    return (
        relative.name in {"inventory.json", "inventory.ndjson"}
        or (relative.parts and relative.parts[0] == "task-docs")
        or ".git" in relative.parts
        or any(part.startswith(".") for part in relative.parts)
        or any(part.startswith(".env") for part in relative.parts)
        or any(part.endswith(".service") or part.endswith(".timer") for part in relative.parts)
        or "index.sqlite" in relative.parts
        or "backups" in relative.parts
    )


def _cleanup_staging_dir(staging_dir: Path | None) -> None:
    if staging_dir is not None:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _publish_dump_dir(staging_dir: Path, board_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = board_dir / f"kanboard-raw-{stamp}"
    suffix = 1
    while True:
        try:
            os.rename(staging_dir, candidate)
            return candidate
        except FileExistsError:
            candidate = board_dir / f"kanboard-raw-{stamp}-{suffix}"
            suffix += 1
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                candidate = board_dir / f"kanboard-raw-{stamp}-{suffix}"
                suffix += 1
                continue
            raise
