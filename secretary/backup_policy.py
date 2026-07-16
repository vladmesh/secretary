from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from secretary.data import DataExport


ARCHIVE_ROOT = "secretary-backup"
BACKUP_VERSION = 1
BACKUPS_MAX_BYTES = 512 * 1024 * 1024

BackupKind = Literal["core", "full"]
RestoreAction = Literal["restore", "exclude"]


@dataclass(frozen=True)
class ComponentPolicy:
    name: str
    path: str
    source_export: str | None = None
    required_entries: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    requires_raw_board_data: bool = False
    restore_action: RestoreAction = "restore"


@dataclass(frozen=True)
class BackupPolicy:
    kind: BackupKind
    components: tuple[ComponentPolicy, ...]
    forbidden_entries: tuple[str, ...]
    max_age: timedelta
    max_age_label: str
    retention_seconds: int | None
    restore_capability: str

    @property
    def required_components(self) -> tuple[str, ...]:
        return tuple(component.name for component in self.components)

    @property
    def required_entries(self) -> tuple[str, ...]:
        base = (
            f"{ARCHIVE_ROOT}/versions.json",
            f"{ARCHIVE_ROOT}/instance/instance.yaml",
            f"{ARCHIVE_ROOT}/secretary-data/data-manifest.json",
        )
        entries: list[str] = list(base)
        for component in self.components:
            entries.append(component_archive_name(component.path))
            entries.extend(component_archive_name(entry) for entry in component.required_entries)
        return tuple(dict.fromkeys(entries))


RUNS_STATE = ComponentPolicy(
    "runs_state",
    "runs/watermarks.json",
    required_entries=("runs/cards.json", "runs/claims.json"),
    required_fields=("cards", "claims"),
)

MEMORY = ComponentPolicy(
    "memory",
    "memory/export.ndjson",
    source_export="memory",
)

CORE_POLICY = BackupPolicy(
    kind="core",
    components=(
        ComponentPolicy(
            "board",
            "board/cards.json",
            source_export="board",
            required_entries=("board/cards.ndjson", "board/export.json"),
        ),
        MEMORY,
        RUNS_STATE,
    ),
    forbidden_entries=(
        f"{ARCHIVE_ROOT}/secretary-data/runs/runs.ndjson",
        f"{ARCHIVE_ROOT}/secretary-data/transcripts/inventory.json",
        f"{ARCHIVE_ROOT}/secretary-data/artifacts/inventory.json",
        f"{ARCHIVE_ROOT}/debug/orca-state/inventory.json",
    ),
    max_age=timedelta(days=1),
    max_age_label="1d",
    retention_seconds=None,
    restore_capability="normalized-core",
)

FULL_POLICY = BackupPolicy(
    kind="full",
    components=(
        ComponentPolicy(
            "raw_board",
            "board",
            required_entries=("board",),
            requires_raw_board_data=True,
        ),
        ComponentPolicy(
            "board",
            "board/cards.json",
            source_export="board",
            required_entries=("board/cards.ndjson", "board/export.json"),
        ),
        MEMORY,
        RUNS_STATE,
        ComponentPolicy("runs", "runs/runs.ndjson", source_export="runs"),
        ComponentPolicy("transcripts", "transcripts/inventory.json", source_export="transcripts"),
        ComponentPolicy("artifacts", "artifacts/inventory.json", source_export="artifacts"),
        ComponentPolicy(
            "debug_orca_state",
            "debug/orca-state/inventory.json",
            restore_action="exclude",
        ),
    ),
    forbidden_entries=(),
    max_age=timedelta(hours=48),
    max_age_label="48h",
    retention_seconds=48 * 60 * 60,
    restore_capability="full-snapshot",
)

POLICIES: dict[BackupKind, BackupPolicy] = {
    "core": CORE_POLICY,
    "full": FULL_POLICY,
}
BACKUP_KINDS: tuple[BackupKind, ...] = tuple(POLICIES)


def policy_for(kind: object) -> BackupPolicy | None:
    if not isinstance(kind, str):
        return None
    return POLICIES.get(kind) if kind in POLICIES else None


def component_archive_name(path: str) -> str:
    if path.startswith("debug/"):
        return f"{ARCHIVE_ROOT}/{path}"
    return f"{ARCHIVE_ROOT}/secretary-data/{path}"


def restore_plan_components(policy: BackupPolicy, *, empty: bool = False) -> tuple[dict[str, str], ...]:
    components = tuple(
        {
            "name": component.name,
            "action": "initialized" if empty and component.restore_action == "restore" else component.restore_action,
        }
        for component in policy.components
    )
    return (
        *components,
        {"name": "memory_index", "action": "rebuild"},
        {"name": "board_restore", "action": "handoff"},
        {"name": "host_reconcile", "action": "handoff"},
    )


def build_components_manifest(
    *,
    policy: BackupPolicy,
    data_dir: Path,
    raw_dump: Path,
    exports: dict[str, DataExport],
) -> dict[str, Any]:
    components: dict[str, Any] = {}
    for component in policy.components:
        if component.name == "raw_board":
            components[component.name] = {"path": _relative_to_data(data_dir, raw_dump)}
        elif component.name == "runs_state":
            components[component.name] = {
                "path": component.path,
                "cards": "runs/cards.json",
                "claims": "runs/claims.json",
                "source": exports["runs"].source,
            }
        elif component.name == "debug_orca_state":
            components[component.name] = {"path": component.path}
        elif component.source_export is not None:
            components[component.name] = _component_manifest(
                data_dir,
                exports[component.source_export],
            )
    return components


def should_skip_data_entry(relative: Path, *, policy: BackupPolicy) -> bool:
    allowed_roots = {"board", "memory", "runs", "transcripts", "artifacts"}
    if not relative.parts:
        return False
    if relative.name.startswith(".env") or relative.name == "index.sqlite":
        return True
    if relative.parts[0] == "backups":
        return True
    if relative.parts[0] not in allowed_roots and relative.name != "data-manifest.json":
        return True
    if any(part.startswith(".") for part in relative.parts) and relative.parts[:2] != ("memory", "facts"):
        return True
    if policy.kind == "core":
        return _skip_core_data_entry(relative)
    return False


def _skip_core_data_entry(relative: Path) -> bool:
    if not relative.parts:
        return False
    root = relative.parts[0]
    if root in {"transcripts", "artifacts"}:
        return True
    if root == "board" and len(relative.parts) > 1:
        return relative.parts[1].startswith("kanboard-raw-")
    if root == "runs" and len(relative.parts) > 1:
        return relative.parts[1] not in {"watermarks.json", "cards.json", "claims.json"}
    return False


def _component_manifest(data_dir: Path, export: DataExport) -> dict[str, Any]:
    return {
        "path": _relative_to_data(data_dir, export.path),
        "count": export.count,
        "source": export.source,
    }


def _relative_to_data(data_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(data_dir).as_posix()
    except ValueError:
        return str(path)
