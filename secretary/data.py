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
from secretary._fsutil import (
    cleanup_staging_dir as _cleanup_staging_dir,
    copy_tree as _copy_tree,
    display_relative as _display_relative,
    ensure_dir as _ensure_dir,
    publish_component_entries as _publish_component_entries,
    regular_files_under as _regular_files_under,
    remove_path as _remove_path,
    write_json as _write_json,
    write_ndjson as _write_ndjson,
)
from secretary.memory_journal import (
    PANELMEM_KB,
    export_memory_snapshot,
    import_memory_journal,
)
from secretary.tasks import TaskAudit


LAYOUT_DIRS = ("board", "memory", "runs", "transcripts", "artifacts", "backups")
KANBOARD_DATA_PATH = "/var/www/app/data"
PIPELINE_WORKTREE = Path.home() / "orca" / "workspaces" / "secretary" / "pipeline"
ORCA_WORKSPACES_ROOT = Path.home() / "orca" / "workspaces"
PIPELINE_STATE_DIR = PIPELINE_WORKTREE / "state" / "pipeline"
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
                "facts": "state/memory/facts",
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
    sprint_client: Any = None,
) -> DataExport:
    data_dir = data_dir.expanduser().resolve()
    board_dir = data_dir / "board"
    _ensure_dir(board_dir, "board data dir")
    audit = TaskAudit(data_dir).status()
    if not audit["ok"]:
        raise RuntimeError(f"board export blocked by {audit['pending']} unresolved pending audit record(s)")
    # Product/Issue writes own their private staged journals.  They cannot be
    # reconstructed from an untyped partial backend row, so no checkpoint may
    # export the board while one remains.
    from secretary.product_issues import ProductIssueTransaction
    product_issue = ProductIssueTransaction(data_dir, TaskAudit(data_dir)).status()
    if not product_issue["ok"]:
        raise RuntimeError(
            f"board export blocked by {product_issue['pending']} unresolved Product/Issue transaction(s)"
        )

    # One `pipeline export` instead of a `show` per card: the checkpoint writer runs this on
    # every dispatcher tick under `tick_lock`, and the per-card path cost a subprocess and five
    # API round trips each (~66s on a 200-card board, longer than the tick itself).
    cards = _pipeline_json(["export"], pipeline_worktree=pipeline_worktree, command=command)
    if not isinstance(cards, list):
        raise RuntimeError("pipeline export did not return a card list")

    normalized = []
    for card in sorted(cards, key=lambda item: str(item.get("reference", ""))):
        if not isinstance(card, dict):
            raise RuntimeError("pipeline export returned an invalid card")
        if not str(card.get("reference") or ""):
            continue
        normalized.append(normalize_board_card(card, card))

    # Sprint entities live on their own board and never reach `pipeline export`, so the
    # checkpoint reads them separately instead of inferring them from linked cards.
    sprints = export_sprint_entities(sprint_client)

    raw_active_task_count = _latest_raw_active_task_count(
        board_dir,
        board_name=os.environ.get("TA_PIPELINE_BOARD", "Pipeline"),
    )
    summary = {
        "version": 1,
        "source": "triggered_agents pipeline",
        "card_count": len(normalized),
        "sprint_count": len(sprints),
        "raw_active_task_count": raw_active_task_count,
    }
    try:
        staging = Path(tempfile.mkdtemp(prefix=".board-export-", suffix=".tmp", dir=board_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create board export staging: {exc}") from None
    try:
        _write_json(staging / "cards.json", {"version": 1, "cards": normalized})
        _write_ndjson(staging / "cards.ndjson", normalized)
        _write_json(staging / "sprints.json", {"version": 1, "sprints": sprints})
        _write_ndjson(staging / "sprints.ndjson", sprints)
        _write_json(staging / "export.json", summary)
        _publish_component_entries(
            staging,
            board_dir,
            ["cards.json", "cards.ndjson", "sprints.json", "sprints.ndjson", "export.json"],
            "board export",
        )
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise
    return DataExport(path=board_dir / "cards.json", count=len(normalized), source=summary["source"])


def normalize_board_card(list_card: dict[str, Any], shown_card: dict[str, Any]) -> dict[str, Any]:
    """Checkpoint record for one card. `pipeline export` carries both views, so the export
    path passes the same payload twice; the list/show pair stays for callers holding both."""
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
        "closed": bool(shown_card.get("closed", list_card.get("closed", False))),
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
            {
                "ts": str(comment.get("ts", "")),
                "text": str(comment.get("text", "")),
            }
            for comment in comments
            if isinstance(comment, dict)
        ],
    }


def export_sprint_entities(client: Any = None) -> list[dict[str, Any]]:
    """Read the sprint board into deterministic checkpoint records."""
    from secretary.sprints import SprintReader
    from secretary.tasks import KanboardClient, TaskError

    try:
        reader = SprintReader(client if client is not None else KanboardClient())
        return [normalize_sprint_entity(sprint) for sprint in reader.export()]
    except TaskError as exc:
        raise RuntimeError(f"sprint export failed: {exc.message}") from None


def normalize_sprint_entity(sprint: dict[str, Any]) -> dict[str, Any]:
    """Checkpoint record for one sprint entity.

    The record describes the contract, not the Kanboard row it currently sits on:
    a restored sprint gets a new task id, and comparing it back to the export has
    to stay possible. Budget totals and thresholds are left out; they are derived
    from `by_type` and from installation config.
    """
    audit = sprint.get("audit")
    audit = audit if isinstance(audit, dict) else {}
    budget = sprint.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    by_type = budget.get("by_type")
    by_type = by_type if isinstance(by_type, dict) else {}
    resume = sprint.get("resume")
    comments = sprint.get("comments")
    return {
        "reference": str(sprint.get("ref") or ""),
        "goal": str(sprint.get("goal") or ""),
        "definition_of_done": str(sprint.get("definition_of_done") or ""),
        "repositories": [str(repo) for repo in sprint.get("repositories") or []],
        # A sprint that predates ownership has none of the three fields, and the record
        # keeps them absent rather than storing an empty value it was never given.
        **({"product": str(sprint["product"])} if "product" in sprint else {}),
        **({"issues": [str(issue) for issue in sprint["issues"] or []]} if "issues" in sprint else {}),
        **(
            {"reservations": [str(project) for project in sprint["reservations"] or []]}
            if "reservations" in sprint else {}
        ),
        # A row the observer migration has not reached carries no observer key at all, and the
        # record keeps it absent.  A key present and `None` is a value that is not one of the four
        # tagged forms: restore refuses the whole set on it rather than guessing a repair.
        **({"observer": sprint["observer"]} if "observer" in sprint else {}),
        "status": str(sprint.get("status") or ""),
        "budget": {"by_type": {str(key): _int_or_none(value) or 0 for key, value in sorted(by_type.items())}},
        "current_task": str(sprint.get("current_task") or ""),
        "resume": (
            {str(key): str(value) for key, value in sorted(resume.items())}
            if isinstance(resume, dict) else None
        ),
        "audit": _sprint_audit(audit),
        "comments": [
            {"ts": str(comment.get("created_at") or ""), "text": str(comment.get("body") or "")}
            for comment in (comments if isinstance(comments, list) else [])
            if isinstance(comment, dict)
        ],
    }


def _sprint_audit(audit: dict[str, Any]) -> dict[str, str]:
    """Prefer the audit a restored sprint came from over its recovery row."""
    from secretary.sprints import SPRINT_BOARD_NAME

    source = audit.get("source")
    if isinstance(source, dict) and any(source.values()):
        return {
            "created_at": str(source.get("created_at") or ""),
            "updated_at": str(source.get("updated_at") or ""),
            "board": str(source.get("board") or SPRINT_BOARD_NAME),
        }
    backend = audit.get("backend")
    backend = backend if isinstance(backend, dict) else {}
    return {
        "created_at": str(audit.get("created_at") or ""),
        "updated_at": str(audit.get("updated_at") or ""),
        "board": str(backend.get("board") or SPRINT_BOARD_NAME),
    }


def export_memory(
    data_dir: Path,
    instance_dir: Path | None,
    *,
    source_dir: Path = PANELMEM_KB,
) -> DataExport:
    """Export canon facts. `instance_dir` is required: passing `None` opts into
    the seed fallback, and a caller that forgets it fails instead of silently
    exporting somebody else's memory."""
    data_dir = data_dir.expanduser().resolve()
    result = export_memory_snapshot(data_dir, instance_dir, source_dir=source_dir)
    return DataExport(
        path=result.path,
        count=result.count,
        source=result.source,
    )


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


def export_all(
    data_dir: Path,
    instance_dir: Path | None,
    *,
    copy_transcripts: bool = False,
) -> dict[str, DataExport]:
    return {
        "memory": export_memory(data_dir, instance_dir),
        "board": export_board(data_dir),
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
                    # The tasks cannot be tied to the right board: a global count would pull in
                    # other projects, so the check is skipped rather than counting the wrong set.
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
