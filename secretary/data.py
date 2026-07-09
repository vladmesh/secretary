from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary.config import validate


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

    staging_dir = Path(
        tempfile.mkdtemp(prefix=".kanboard-raw-", suffix=".tmp", dir=board_dir)
    )
    destination = staging_dir / "data"

    try:
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
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise RuntimeError("docker command not found") from None
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        reason = (exc.stderr or exc.stdout or "docker cp failed").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else "docker cp failed") from None
    except OSError as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise RuntimeError(f"could not create raw dump: {exc}") from None

    return KanboardDump(dump_dir=dump_dir, source=f"{container}:{source_path}")


def _write_data_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, indent=2, sort_keys=False) + "\n"
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{manifest_path.name}.",
        suffix=".tmp",
        dir=manifest_path.parent,
        text=True,
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
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
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


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
