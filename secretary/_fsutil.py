from __future__ import annotations

import json
import os
import shutil
import stat as stat_module
import tempfile
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def write_ndjson(path: Path, rows: list[dict[str, Any]]) -> None:
    body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    write_text_atomic(path, body)


def write_text_atomic(path: Path, payload: str) -> None:
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


def copy_tree(source: Path, destination: Path) -> None:
    try:
        paths = sorted(source.rglob("*"))
    except OSError as exc:
        raise RuntimeError(f"could not list source tree {source}: {exc}") from None
    for path in paths:
        if has_git_part(source, path):
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


def regular_files_under(root: Path, *, context: str) -> list[tuple[Path, os.stat_result]]:
    files: list[tuple[Path, os.stat_result]] = []
    try:
        paths = sorted(root.rglob("*"))
    except OSError as exc:
        raise RuntimeError(f"could not list {context} {root}: {exc}") from None
    for path in paths:
        if has_git_part(root, path):
            continue
        try:
            file_stat = path.lstat()
        except OSError as exc:
            relative = display_relative(root, path)
            raise RuntimeError(f"could not inspect {context} {relative}: {exc}") from None
        mode = file_stat.st_mode
        if stat_module.S_ISLNK(mode):
            continue
        if stat_module.S_ISREG(mode):
            files.append((path, file_stat))
    return files


def has_git_part(root: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return ".git" in parts


def display_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def publish_component_entries(
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
            restore_component_entries(destination, backup, entries)
        except OSError as restore_exc:
            raise RuntimeError(
                f"could not publish {label}: {exc}; rollback failed: {restore_exc}"
            ) from None
        raise RuntimeError(f"could not publish {label}: {exc}") from None
    finally:
        shutil.rmtree(backup, ignore_errors=True)


def restore_component_entries(destination: Path, backup: Path, entries: list[str]) -> None:
    for entry in entries:
        current = destination / entry
        if current.exists():
            remove_path(current)
        saved = backup / entry
        if saved.exists():
            os.replace(saved, destination / entry)


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def ensure_dir(path: Path, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot prepare {label}: {exc}") from None


def cleanup_staging_dir(staging_dir: Path | None) -> None:
    if staging_dir is not None:
        shutil.rmtree(staging_dir, ignore_errors=True)
