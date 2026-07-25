"""Read-only installation status snapshot used by operators and automation."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary.checkpoint import checkpoint_snapshot
from secretary.host import CollectResult, FixtureHostSource, LiveHostSource, build_doctor_expectations
from secretary.host_apply import resolve_packaged


STATUS_SCHEMA_VERSION = 1


def collect_status(report, *, host_fixture: str | None = None, offline: bool = False) -> dict[str, Any]:
    """Return a stable, non-mutating snapshot for one validated instance."""
    data_dir = Path(report.instance["data_dir"]).expanduser()
    production = _read_object(data_dir / "dispatcher" / "production-state.json")
    expected = build_doctor_expectations(
        report.instance, report.bindings,
        packaged=resolve_packaged(report.instance, instance_path=report.instance_path.parent),
    )
    if offline:
        collected = CollectResult(expected_to_empty_inventory())
    else:
        source = FixtureHostSource(Path(host_fixture)) if host_fixture else LiveHostSource()
        collected = source.collect(expected)
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "installation": {
            "name": report.name or None,
            "instance": str(report.instance_path),
            "projects": report.projects,
            "heads": _heads(report.instance),
            "cards": _card_count(data_dir),
        },
        "host": {
            "units": _units(expected, collected, offline=offline),
            "inventory_errors": collected.errors,
            "resources": _host_resources(data_dir),
        },
        "dispatcher": {
            "phase": _text(production.get("phase")) or "new",
            "active_attempts": _attempts(production),
        },
        "checkpoint": checkpoint_snapshot(
            report.instance_path.parent,
            write_state=_object(production.get("checkpoint")),
            push_state=_object(production.get("checkpoint_push")),
        ),
        "memory": _memory_status(data_dir),
    }


def expected_to_empty_inventory():
    # Avoid probing the host in --offline mode while preserving CollectResult's
    # explicit unavailable status for every host kind.
    from secretary.host import HostInventory
    return HostInventory()


def _heads(instance: dict[str, Any]) -> list[dict[str, str]]:
    heads = instance.get("heads") if isinstance(instance, dict) else None
    if not isinstance(heads, list):
        return []
    return [
        {"role": item["role"], "model": item.get("model", "")}
        for item in heads
        if isinstance(item, dict) and isinstance(item.get("role"), str)
    ]


def _units(expected, collected: CollectResult, *, offline: bool) -> list[dict[str, Any]]:
    rows = []
    for name in sorted(expected.units):
        enabled, active = collected.inventory.unit_states.get(name, (None, None))
        rows.append({
            "name": name,
            "kind": "timer" if name.endswith(".timer") else "service",
            "present": None if offline or "units" in collected.errors else name in collected.inventory.units,
            "enabled": enabled,
            "active": active,
        })
    return rows


def _attempts(production: dict[str, Any]) -> list[dict[str, Any]]:
    records = production.get("records")
    if not isinstance(records, dict):
        return []
    attempts = []
    for reference, record in sorted(records.items()):
        if not isinstance(reference, str) or not isinstance(record, dict):
            continue
        attempts.append({
            "reference": reference,
            "attempt_id": _text(record.get("attempt_id")) or None,
            "state": _text(record.get("state")) or None,
            "worker": _text(record.get("worker")) or None,
            "head": _text(record.get("head")) or None,
            "review_head": _text(record.get("review_head")) or None,
            "workspace": _text(record.get("workspace")) or None,
        })
    return attempts


def _memory_status(data_dir: Path) -> dict[str, Any]:
    memory = data_dir / "memory"
    manifest = _read_object(memory / "manifest.json")
    journal = _object(manifest.get("journal")) or {}
    facts = journal.get("fact_count")
    if not isinstance(facts, int) or isinstance(facts, bool):
        facts = _export_fact_count(memory / "export.ndjson")
    index = memory / "index.sqlite"
    return {
        "fact_count": facts,
        "last_reindex_at": _mtime(index),
        "index_present": index.is_file(),
    }


def _host_resources(data_dir: Path) -> dict[str, Any]:
    try:
        probe = data_dir
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        usage = shutil.disk_usage(probe)
        disk_free = usage.free
    except OSError:
        disk_free = None
    return {"disk_free_bytes": disk_free, "memory_available_bytes": _memory_available(), "load_average": _load_average()}


def _memory_available() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _load_average() -> list[float] | None:
    try:
        return list(os.getloadavg())
    except OSError:
        return None


def _card_count(data_dir: Path) -> int | None:
    path = data_dir / "board" / "cards.ndjson"
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except (OSError, UnicodeError):
        return None


def _export_fact_count(path: Path) -> int | None:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except (OSError, UnicodeError):
        return None


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except OSError:
        return None


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _object(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
