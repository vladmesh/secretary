"""Read-only command handlers for Phase 7 host planning."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from secretary._fsutil import directory_lock, write_text_atomic
from secretary.config import validate_instance
from secretary.host import (
    FixtureHostSource,
    LiveHostSource,
    build_expectations,
    build_plan,
    load_managed_manifest,
    plan_changes,
    plan_input_errors,
)
from secretary.restore import mark_reconcile_status


def run_reconcile_plan(args) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        print("secretary reconcile plan: invalid instance config")
        return 2
    errors = plan_input_errors(report.instance, report.bindings)
    if errors:
        print("secretary reconcile plan: " + errors[0])
        return 2
    if args.offline:
        print("secretary reconcile plan: --offline cannot produce a plan; use --host-fixture instead")
        return 2
    expected = build_expectations(report.bindings, report.host)
    source = FixtureHostSource(Path(args.host_fixture)) if args.host_fixture else LiveHostSource()
    collected = source.collect(expected)
    if collected.errors:
        print("secretary reconcile plan: host inventory unavailable")
        for kind in ("projects", "units", "orca repos"):
            if reason := collected.errors.get(kind):
                print(f"  {kind}: unavailable: {reason}")
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
    mark_reconcile_status(Path(report.instance["data_dir"]), applied=all(change.action == "unchanged" for change in changes))
    return 1 if any(change.action == "conflict" for change in changes) else 0


def _strict_manifest(path: Path) -> tuple[list, str]:
    """Load state for a write path. Unlike plan, adoption must fail closed."""
    if path.is_symlink():
        return [], "managed manifest must not be a symlink"
    if not path.exists():
        return [], ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeError:
        return [], "managed manifest is not valid UTF-8"
    except OSError:
        return [], "managed manifest is unreadable"
    except ValueError:
        return [], "managed manifest is not valid JSON"
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("resources"), list):
        return [], "managed manifest has an unsupported shape"
    resources = load_managed_manifest(path)
    if len(resources) != len(payload["resources"]):
        return [], "managed manifest contains invalid resource records"
    logical_ids: set[str] = set()
    names: set[tuple[str, str]] = set()
    for resource in resources:
        if resource.kind not in {"unit", "orca"} or not resource.spec:
            return [], "managed manifest contains non-canonical resource records"
        value = json.dumps(
            [resource.logical_id, resource.kind, resource.name, resource.spec],
            separators=(",", ":"),
        )
        if hashlib.sha256(value.encode()).hexdigest() != resource.fingerprint:
            return [], "managed manifest contains a fingerprint mismatch"
        if resource.logical_id in logical_ids:
            return [], "managed manifest has duplicate logical ids"
        logical_ids.add(resource.logical_id)
        key = (resource.kind, resource.name)
        if key in names:
            return [], "managed manifest has duplicate resource names"
        names.add(key)
    return resources, ""


def _manifest_text(resources) -> str:
    records = [
        {
            "fingerprint": resource.fingerprint,
            "kind": resource.kind,
            "logical_id": resource.logical_id,
            "name": resource.name,
            "spec": resource.spec,
        }
        for resource in sorted(resources, key=lambda item: (item.kind, item.logical_id))
    ]
    return json.dumps({"version": 1, "resources": records}, indent=2, sort_keys=True) + "\n"


def _merge_adoption(managed, resource):
    by_id = {item.logical_id: item for item in managed}
    current = by_id.get(resource.logical_id)
    if current is not None and current != resource:
        return [], current, "existing managed record has drifted"
    for item in managed:
        if item.logical_id != resource.logical_id and item.kind == resource.kind and item.name == resource.name:
            return [], current, "resource name is already owned by another logical id"
    updated = [item for item in managed if item.logical_id != resource.logical_id] + [resource]
    return updated, current, ""


def run_reconcile_adopt(args) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        print("secretary reconcile adopt: invalid instance config")
        return 2
    errors = plan_input_errors(report.instance, report.bindings)
    if errors:
        print("secretary reconcile adopt: " + errors[0])
        return 2
    desired = {resource.logical_id: resource for resource in build_plan(report.instance, report.bindings)}
    resource = desired.get(args.logical_id)
    if resource is None:
        print("secretary reconcile adopt: logical id is not in desired state")
        return 2
    if resource.kind != "orca":
        print("secretary reconcile adopt: resource kind has no verifiable adoption identity")
        return 2
    try:
        expected_repo = json.loads(resource.spec)["repo"]
    except (ValueError, KeyError, TypeError):
        print("secretary reconcile adopt: desired resource identity is invalid")
        return 2
    source = LiveHostSource()
    live_paths, reason = source.orca_repo_paths()
    if reason:
        print("secretary reconcile adopt: live Orca inventory unavailable: " + reason)
        return 2
    live_repo = live_paths.get(resource.name)
    if live_repo is None:
        print("secretary reconcile adopt: desired Orca registration is missing")
        return 2
    expected_path = Path(expected_repo).expanduser()
    if not expected_path.is_absolute():
        print("secretary reconcile adopt: desired repo path must be absolute")
        return 2
    try:
        normalized_expected = str(expected_path.resolve(strict=False))
    except (OSError, RuntimeError):
        print("secretary reconcile adopt: desired repo path could not be normalized")
        return 2
    if live_repo != normalized_expected:
        print("secretary reconcile adopt: Orca registration repo path does not match desired state")
        return 2

    manifest = Path(args.managed_manifest) if args.managed_manifest else Path(report.instance["data_dir"]) / "host-managed.json"
    managed, error = _strict_manifest(manifest)
    if error:
        print("secretary reconcile adopt: " + error)
        return 2
    updated, current, error = _merge_adoption(managed, resource)
    if error:
        print("secretary reconcile adopt: " + error)
        return 2
    print(f"adopt {resource.logical_id} {resource.kind} {resource.name} {resource.fingerprint}")
    if not args.yes:
        print("preview only; pass --yes to write the managed manifest")
        return 0

    with directory_lock(manifest.parent):
        managed, error = _strict_manifest(manifest)
        if error:
            print("secretary reconcile adopt: " + error)
            return 2
        updated, current, error = _merge_adoption(managed, resource)
        if error:
            print("secretary reconcile adopt: " + error)
            return 2
        if current == resource:
            print("managed manifest already records this resource")
            return 0
        try:
            if manifest.is_symlink():
                raise RuntimeError("managed manifest must not be a symlink")
            write_text_atomic(manifest, _manifest_text(updated))
        except RuntimeError as exc:
            print("secretary reconcile adopt: " + str(exc))
            return 2
    print("managed manifest updated")
    return 0


def add_reconcile_subcommands(subcommands) -> None:
    """Register the Phase 7 host commands outside the already busy CLI module."""
    plan = subcommands.add_parser("plan", help="show the read-only desired host plan")
    plan.add_argument("--instance", required=True)
    plan.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    source = plan.add_mutually_exclusive_group()
    source.add_argument(
        "--host-fixture", metavar="DIR",
        help="read a deterministic fixture inventory instead of the live host",
    )
    source.add_argument(
        "--offline", action="store_true",
        help="reject live inventory; use --host-fixture for an offline plan",
    )
    plan.add_argument("--managed-manifest", metavar="FILE")
    plan.set_defaults(handler=run_reconcile_plan)

    adopt = subcommands.add_parser(
        "adopt", help="record one verified existing desired resource as managed"
    )
    adopt.add_argument("--instance", required=True)
    adopt.add_argument("--logical-id", required=True)
    adopt.add_argument("--managed-manifest", metavar="FILE")
    adopt.add_argument(
        "--yes", action="store_true", help="write the managed manifest after showing the record"
    )
    adopt.set_defaults(handler=run_reconcile_adopt)
