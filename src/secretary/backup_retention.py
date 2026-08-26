from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from secretary.backup_policy import POLICIES, BackupKind


def apply_retention(backups_dir: Path, *, keep: set[Path], now: datetime) -> None:
    archives = backup_archives(backups_dir)
    for kind, policy in POLICIES.items():
        kind_archives = [archive for archive in archives if archive_kind_from_name(archive) == kind]
        if policy.retention_seconds is None:
            newest = max(kind_archives, key=archive_sort_key, default=None)
            for archive in kind_archives:
                if archive != newest and archive not in keep:
                    remove_path_quietly(archive)
            continue

        cutoff = now.timestamp() - policy.retention_seconds
        for archive in kind_archives:
            if archive in keep:
                continue
            created_at = archive_created_at(archive)
            if created_at is not None and created_at.timestamp() < cutoff:
                remove_path_quietly(archive)


def backup_archives(backups_dir: Path) -> list[Path]:
    return sorted(path for path in backups_dir.iterdir() if path.name.endswith(".tar") and path.is_file())


def archive_kind_from_name(path: Path) -> BackupKind:
    name = path.name
    if name.startswith("secretary-backup-core-"):
        return "core"
    return "full"


def archive_sort_key(path: Path) -> tuple[float, str]:
    created_at = archive_created_at(path)
    timestamp = created_at.timestamp() if created_at is not None else archive_mtime(path).timestamp()
    return (timestamp, path.name)


def archive_created_at(path: Path) -> datetime | None:
    match = re.fullmatch(
        r"secretary-backup-(?:(?:core|full)-)?(\d{8}T\d{6}Z)(?:-\d+)?\.tar",
        path.name,
    )
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def archive_mtime(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return datetime.fromtimestamp(0, tz=UTC)


def remove_path_quietly(path: Path) -> None:
    import shutil

    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        pass
