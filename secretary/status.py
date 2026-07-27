"""Read-only installation status snapshot used by operators and automation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary.checkpoint import checkpoint_snapshot
from secretary.dispatcher_observer import observer_snapshot
from secretary.dispatcher_pause import ProductionPause
from secretary.dispatcher_review import command_terminal_status
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_types import HostError
from secretary.host import CollectResult, FixtureHostSource, LiveHostSource, build_doctor_expectations
from secretary.host_apply import resolve_packaged
from secretary.secret_store import store_health
from secretary.sprints import SprintReader, budget_thresholds
from secretary.tasks import KanboardClient, TaskError


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
            "cards": {"total": _card_count(data_dir), "active_attempts": len(_attempts(production, probe_panels=False))},
            "sprints": _sprints(data_dir, report.instance, production),
        },
        "host": {
            "units": _units(expected, collected, offline=offline),
            "schedules": _schedules(expected, collected, offline=offline),
            "inventory_errors": collected.errors,
            "resources": _host_resources(data_dir),
            "external_runtime": _external_runtime(expected, collected, offline=offline),
        },
        "dispatcher": {
            "phase": _text(production.get("phase")) or "new",
            "active_attempts": _attempts(production, probe_panels=not offline and host_fixture is None),
            "observers": _observers(production),
            "pause": _pause_status(data_dir, production),
            "divergences": _divergences(production),
            "reconciliation": _reconciliation(production),
        },
        "checkpoint": checkpoint_snapshot(
            report.instance_path.parent,
            write_state=_object(production.get("checkpoint")),
            push_state=_object(production.get("checkpoint_push")),
        ),
        "memory": _memory_status(data_dir),
        "secret_store": store_health(report.instance_path.parent),
    }


def expected_to_empty_inventory():
    """Avoid probing the host in --offline mode.

    Empty inventory and no errors deliberately mean host facts are unavailable;
    status represents each expected resource with null presence instead.
    """
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


def _sprints(data_dir: Path, instance: dict[str, Any], production: dict[str, Any]) -> dict[str, Any]:
    """Read the sprint entity and live board without consulting observer context."""
    try:
        reader = SprintReader(
            KanboardClient(), data_dir=data_dir, thresholds=budget_thresholds(instance),
        )
        observers = {row["sprint"]: row for row in observer_snapshot(production)}
        return {
            "items": [
                reader.status(sprint["ref"], observer=observers.get(sprint["ref"]))
                for sprint in reader.list(create=False)
            ],
            "error": None,
        }
    except TaskError as exc:
        return {"items": [], "error": {"code": exc.code, "message": exc.message}}


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


def _schedules(expected, collected: CollectResult, *, offline: bool) -> list[dict[str, Any]]:
    return [row for row in _units(expected, collected, offline=offline) if row["kind"] == "timer"]


def _external_runtime(expected, collected: CollectResult, *, offline: bool) -> dict[str, Any]:
    """The host-owned Orca server: outside Secretary's unit ownership parity (`host.units`),

    but the local scheduler depends on it, so status/doctor need its own non-null evidence
    instead of a silent absence from that list.
    """
    name = str(getattr(expected, "external_runtime", "") or "")
    enabled, active = (None, None)
    if not offline and "units" not in collected.errors:
        enabled, active = collected.inventory.unit_states.get(name, (None, None))
    return {"name": name or None, "enabled": enabled, "active": active}


def _attempts(production: dict[str, Any], *, probe_panels: bool) -> list[dict[str, Any]]:
    records = production.get("records")
    if not isinstance(records, dict):
        return []
    attempts = []
    for reference, record in sorted(records.items()):
        if not isinstance(reference, str) or not isinstance(record, dict):
            continue
        worker = _watchdog(record, reference, "worker", probe_panels)
        reviewer = _watchdog(record, reference, "review", probe_panels)
        attempts.append({
            "reference": reference,
            "attempt_id": _text(record.get("attempt_id")) or None,
            "state": _text(record.get("state")) or None,
            "worker": _text(record.get("worker")) or None,
            "head": _text(record.get("head")) or None,
            "review_head": _text(record.get("review_head")) or None,
            "workspace": _text(record.get("workspace")) or None,
            "watchdogs": {"worker": worker, "reviewer": reviewer},
            "paused": {
                "worker": _float(record.get("paused_worker_at")) > 0,
                "reviewer": _float(record.get("paused_reviewer_at")) > 0,
            },
        })
    return attempts


def _watchdog(record: dict[str, Any], reference: str, kind: str, probe_panels: bool) -> dict[str, Any]:
    prefix = "review" if kind == "review" else "worker"
    panel: dict[str, Any] = {"known": False, "live": None, "reason": "not-probed"}
    if probe_panels:
        try:
            panel = command_terminal_status(
                _StatusWatchdogHost(), {"ref": reference}, DispatcherRecord.from_json(record), kind=kind
            )
        except (HostError, OSError, OverflowError, TypeError, ValueError) as exc:
            panel = {"known": False, "live": None, "reason": str(exc)}
    return {
        "panel": panel,
        "last_progress_at": _epoch(_float(record.get(f"{prefix}_progress_at"))),
        "waiting_since": _epoch(_float(record.get(f"{prefix}_waiting_since"))),
        "respawns": int(_float(record.get(f"{prefix}_respawns"))),
    }


class _StatusWatchdogHost:
    """Read-only adapter for the same pane probe used by the dispatcher watchdog."""

    mode = "real"

    def _run_json(self, args: list[str]) -> dict[str, Any]:
        try:
            completed = subprocess.run(args, text=True, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError(f"terminal inventory unavailable: {exc}") from None
        if completed.returncode:
            raise HostError("terminal inventory failed")
        try:
            payload = json.loads(completed.stdout or "{}")
        except ValueError:
            raise HostError("terminal inventory returned invalid JSON") from None
        return payload.get("result", payload) if isinstance(payload, dict) else {}

    def codex_tui_activity(self, _task, _record, _kind):
        return None


def _observers(production: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per sprint the dispatcher tracks an observer head for.

    Enough to answer "is my sprint being watched, and if not, why" without opening a transcript:
    the head profile, whether its pid is alive right now, when the dispatcher last acted on it, and
    the reason a launch is parked.
    """
    return [
        {
            "sprint": row["sprint"],
            "head": row["head"] or None,
            "state": row["state"],
            "alive": row["alive"],
            "pid_known": row["pid_known"],
            "launches": row["launches"],
            # A live pid here belongs to a bring-up that failed with its terminal still up, not to
            # a working observer: without this flag `alive: true` would read as a watched sprint.
            "abandoned_handle": row["abandoned_handle"],
            # False for a head adopted from a launch intent: it is watching its sprint, but its
            # terminal handle died with the tick that opened it and its stop goes by workspace.
            "handle_known": row["handle_known"],
            "workspace": row["workspace"] or None,
            "last_action": row["last_action"] or None,
            "last_action_at": _epoch(row["last_action_at"]),
            "deferred_reason": row["deferred_reason"] or None,
            "stopped_reason": row["stopped_reason"] or None,
            "paused": row["paused"],
        }
        for row in observer_snapshot(production)
    ]


def _divergences(production: dict[str, Any]) -> dict[str, Any]:
    """Explicit counts and rows, never null, so a reader cannot mistake "we have not looked"

    for "there are none". A divergence closes once its card leaves the active dispatcher cycle
    (`dispatcher_production._reconcile_production`); one still open is either tied to a card
    still in flight or is genuinely unresolved.
    """
    raw = production.get("controlled_divergences")
    items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    open_items = [item for item in items if item.get("status") != "closed"]
    return {
        "open_count": len(open_items),
        "total_count": len(items),
        "open": [
            {
                "id": _text(item.get("id")),
                "pilot_ref": _text(item.get("pilot_ref")),
                "step": _text(item.get("step")),
                "reason": _text(item.get("reason")),
                "opened_at": _text(item.get("at")),
            }
            for item in open_items
        ],
    }


def _reconciliation(production: dict[str, Any]) -> dict[str, Any]:
    """Evidence that the production tick has actually run its reconciliation pass.

    `last_tick_finished_at` predates reconciliation and is stamped by every tick regardless of
    dispatcher version, so it cannot tell a reconciled host from a pre-deployment one still
    running the old code. `last_reconciled_at` is only ever written by the reconciliation pass
    itself (`dispatcher_production._reconcile_production`), so it stays null, honestly reporting
    "unknown", until a tick running the new code has actually completed one.
    """
    records = production.get("records")
    records = records if isinstance(records, dict) else {}
    return {
        "last_tick_finished_at": _text(production.get("last_tick_finished_at")) or None,
        "last_reconciled_at": _text(production.get("last_reconciled_at")) or None,
        "records_tracked": len(records),
    }


def _pause_status(data_dir: Path, production: dict[str, Any]) -> dict[str, Any]:
    pause = ProductionPause(data_dir).summary()
    paused = {
        ref: {
            "worker": _float(record.get("paused_worker_at")) > 0,
            "reviewer": _float(record.get("paused_reviewer_at")) > 0,
        }
        for ref, record in production.get("records", {}).items()
        if isinstance(ref, str) and isinstance(record, dict)
    }
    return {
        "paused": bool(pause.get("paused")), "mode": _text(pause.get("mode")) or None,
        "since": _text(pause.get("since")) or None, "actor": _text(pause.get("actor")) or None,
        "reason": _text(pause.get("reason")) or None,
        "auto_resume": pause.get("auto_resume") if isinstance(pause.get("auto_resume"), dict) else None,
        "cards": paused,
        "warnings": pause.get("warnings") if isinstance(pause.get("warnings"), list) else [],
    }


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


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _epoch(value: float) -> str | None:
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None
