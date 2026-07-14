"""Read-only command handlers for Phase 7 host planning."""

from __future__ import annotations

from pathlib import Path

from secretary.config import validate_instance
from secretary.host import FixtureHostSource, build_expectations, build_plan, load_managed_manifest, plan_changes


def run_reconcile_plan(args) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        print("secretary reconcile plan: invalid instance config")
        return 2
    collected = FixtureHostSource(Path(args.host_fixture)).collect(
        build_expectations(report.bindings, report.host)
    )
    if collected.errors:
        print("secretary reconcile plan: host inventory unavailable")
        return 2
    manifest = Path(args.managed_manifest) if args.managed_manifest else Path(report.instance["data_dir"]) / "host-managed.json"
    prefix = report.host.get("unit_prefix", "")
    changes = plan_changes(
        build_plan(report.instance, report.bindings),
        collected.inventory,
        load_managed_manifest(manifest),
        prefix if isinstance(prefix, str) else "",
    )
    for change in changes:
        print(f"{change.action} {change.logical_id} {change.kind} {change.name}")
    return 1 if any(change.action == "conflict" for change in changes) else 0
