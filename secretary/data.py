from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


LAYOUT_DIRS = ("board", "memory", "runs", "transcripts", "artifacts", "backups")
KANBOARD_DATA_PATH = "/var/www/app/data"


@dataclass(frozen=True)
class DataLayout:
    data_dir: Path
    manifest_path: Path
    created_dirs: list[Path]


@dataclass(frozen=True)
class KanboardDump:
    dump_dir: Path
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
    for relative in LAYOUT_DIRS:
        directory = data_dir / relative
        existed = directory.is_dir()
        directory.mkdir(parents=True, exist_ok=True)
        if not existed:
            created_dirs.append(directory)

    manifest_path = data_dir / "data-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_for(data_dir), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
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
    board_dir.mkdir(parents=True, exist_ok=True)

    dump_dir = _next_dump_dir(board_dir)
    dump_dir.mkdir(parents=True)
    destination = dump_dir / "data"

    try:
        subprocess.run(
            ["docker", "cp", f"{container}:{source_path}", str(destination)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        shutil.rmtree(dump_dir, ignore_errors=True)
        raise RuntimeError("docker command not found") from None
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(dump_dir, ignore_errors=True)
        reason = (exc.stderr or exc.stdout or "docker cp failed").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else "docker cp failed") from None

    metadata = {
        "version": 1,
        "kind": "kanboard-raw",
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "container": container,
        "source_path": source_path,
    }
    (dump_dir / "manifest.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return KanboardDump(dump_dir=dump_dir, source=f"{container}:{source_path}")


def _next_dump_dir(board_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = board_dir / f"kanboard-raw-{stamp}"
    suffix = 1
    while candidate.exists():
        candidate = board_dir / f"kanboard-raw-{stamp}-{suffix}"
        suffix += 1
    return candidate
