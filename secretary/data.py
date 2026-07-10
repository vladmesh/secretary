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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary.config import validate


LAYOUT_DIRS = ("board", "memory", "runs", "transcripts", "artifacts", "backups")
KANBOARD_DATA_PATH = "/var/www/app/data"
PIPELINE_WORKTREE = Path("/home/dev/orca/workspaces/triggered-agents/pipeline")
PANELMEM_KB = Path("/home/dev/panelmem-kb")
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
    _write_json(board_dir / "cards.json", {"version": 1, "cards": normalized})
    _write_ndjson(board_dir / "cards.ndjson", normalized)
    _write_json(board_dir / "export.json", summary)
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
    source_dir = source_dir.expanduser().resolve()
    source_memory = source_dir / "memory"
    if not source_memory.is_dir():
        raise RuntimeError(f"memory source not found: {source_memory}")

    memory_dir = data_dir / "memory"
    facts_dir = memory_dir / "facts"
    _ensure_dir(memory_dir, "memory data dir")
    _replace_dir_from_tree(source_memory, facts_dir)

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
                "text": text,
            }
        )
    _write_ndjson(memory_dir / "export.ndjson", facts)
    _write_json(
        memory_dir / "export.json",
        {"version": 1, "source": str(source_memory), "fact_count": len(facts)},
    )
    return DataExport(path=memory_dir / "export.ndjson", count=len(facts), source=str(source_memory))


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
    finally:
        _cleanup_staging_dir(snapshot)

    _write_ndjson(runs_dir / "runs.ndjson", records)
    _write_json(runs_dir / "watermarks.json", {"version": 1, "files": watermarks})
    _write_json(runs_dir / "cards.json", {"version": 1, "cards": cards})
    _write_json(
        runs_dir / "export.json",
        {
            "version": 1,
            "source": str(state_dir),
            "run_record_count": len(records),
            "watermark_count": len(watermarks),
            "card_mapping_count": len(cards) if isinstance(cards, dict) else 0,
        },
    )
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

    _write_json(transcripts_dir / "inventory.json", {"version": 1, "transcripts": entries})
    _write_ndjson(transcripts_dir / "inventory.ndjson", entries)
    if copy:
        copy_dir = transcripts_dir / "copies"
        try:
            staging = Path(tempfile.mkdtemp(prefix=".copies-", suffix=".tmp", dir=transcripts_dir))
        except OSError as exc:
            raise RuntimeError(f"could not create transcripts copy staging: {exc}") from None
        try:
            for entry in entries:
                destination = staging / _safe_relative_copy_path(entry["root"], entry["relative_path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry["path"], destination, follow_symlinks=False)
            _replace_dir(staging, copy_dir)
        except OSError as exc:
            _cleanup_staging_dir(staging)
            raise RuntimeError(f"could not copy transcripts: {exc}") from None
    elif (transcripts_dir / "copies").exists():
        try:
            shutil.rmtree(transcripts_dir / "copies")
        except OSError as exc:
            raise RuntimeError(f"could not remove transcript copies: {exc}") from None
    return DataExport(path=transcripts_dir / "inventory.json", count=len(entries), source=", ".join(str(p) for p in roots))


def export_all(data_dir: Path, *, copy_transcripts: bool = False) -> dict[str, DataExport]:
    return {
        "board": export_board(data_dir),
        "memory": export_memory(data_dir),
        "runs": export_runs(data_dir),
        "transcripts": export_transcripts(data_dir, copy=copy_transcripts),
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
