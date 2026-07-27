"""Tests for the upgrade materializer: packaged units, reconcile apply, automations."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import upgrade
from secretary.automations import (
    AutomationSpec,
    create_argv,
    drifted_fields,
    load_specs,
    plan_automations,
    repoint_argv,
)
from secretary.host import (
    HostInventory,
    build_plan,
    component_enabled,
    load_packaged_units,
    plan_changes,
    strict_manifest,
    SystemdLayout,
)
from secretary.host_apply import ApplyInputs, HostCommandError, apply_host
from secretary.head_registry import (
    HeadRegistryConfigError,
    assert_snapshot_current,
    canonical_heads,
    load_snapshot,
    product_revision,
    read_source,
)

UNIT_PREFIX = "secretary-"

TIMER = """[Unit]
Description=Example timer

[Timer]
OnCalendar=hourly
Unit=secretary-example.service

[Install]
WantedBy=timers.target
"""

SERVICE = """[Unit]
Description=Example service

[Service]
Type=oneshot
ExecStart=/bin/true
"""


def write_packaging(root: Path) -> Path:
    packaging = root / "packaging" / "systemd"
    packaging.mkdir(parents=True)
    (packaging / "secretary-example.service").write_text(SERVICE, encoding="utf-8")
    (packaging / "secretary-example.timer").write_text(TIMER, encoding="utf-8")
    (packaging / "secretary-memory.service").write_text(SERVICE, encoding="utf-8")
    # The dispatcher pair is always in the desired plan, so a fixture that omits
    # it would be testing an installation the product cannot actually ship.
    (packaging / "secretary-dispatcher-production.service").write_text(SERVICE, encoding="utf-8")
    (packaging / "secretary-dispatcher-production.timer").write_text(TIMER, encoding="utf-8")
    (packaging / "README.md").write_text("not a unit\n", encoding="utf-8")
    return packaging


def instance_config(data_dir: Path, **host: object) -> dict:
    return {
        "version": 1,
        "name": "test",
        "data_dir": str(data_dir),
        "offsite": {"instance_remote": "git@example.invalid:x/y"},
        "host": {"unit_prefix": UNIT_PREFIX, **host},
    }


class FakeUnitInstaller:
    """A systemd double that records the calls a reconcile makes."""

    def __init__(self, present: dict[str, bytes] | None = None, active: set[str] | None = None) -> None:
        self.files = dict(present or {})
        self.active = set(active or set())
        self.calls: list[tuple[str, str]] = []
        self.fail_on: set[str] = set()

    def installed(self, name: str) -> bytes | None:
        return self.files.get(name)

    def install(self, unit) -> None:
        if unit.name in self.fail_on:
            raise HostCommandError(f"install {unit.name}: exited 1")
        self.calls.append(("install", unit.name))
        self.files[unit.name] = unit.content

    def remove(self, name: str) -> None:
        self.calls.append(("remove", name))
        self.files.pop(name, None)

    def daemon_reload(self) -> None:
        self.calls.append(("daemon-reload", ""))

    def enable(self, name: str) -> None:
        self.calls.append(("enable", name))
        self.active.add(name)

    def disable(self, name: str) -> None:
        self.calls.append(("disable", name))
        self.active.discard(name)

    def restart(self, name: str) -> None:
        self.calls.append(("restart", name))

    def is_active(self, name: str) -> bool:
        return name in self.active


class FakeRegistrar:
    def __init__(self) -> None:
        self.added: list[tuple[str, str]] = []

    def add(self, name: str, repo: str) -> None:
        self.added.append((name, repo))


class PackagedUnitTests(unittest.TestCase):
    def test_orca_runtime_is_not_rendered_or_owned_by_an_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "operator" / ".local" / "bin" / "orca"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("#!/bin/sh\n", encoding="utf-8")
            legacy.chmod(0o755)
            account = SimpleNamespace(pw_dir=str(root / "operator"))
            with mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account):
                units = upgrade.resolve_packaged(
                    instance_config(root / "data"), instance_path=root / "instance", runtime_user="operator"
                )

        self.assertNotIn("secretary-orca.service", {unit.name for unit in units})

    def test_render_is_stable_and_uses_the_installation_layout(self):
        layout = SystemdLayout(
            Path("/opt/secretary"), Path("/srv/secretary-instance"), Path("/srv/secretary-data"),
            "operator", Path("/home/operator"),
        )
        first = load_packaged_units(upgrade.default_product_root() / "packaging" / "systemd", UNIT_PREFIX, layout)
        second = load_packaged_units(upgrade.default_product_root() / "packaging" / "systemd", UNIT_PREFIX, layout)

        self.assertEqual([(unit.name, unit.content, unit.digest) for unit in first], [(unit.name, unit.content, unit.digest) for unit in second])
        rendered = b"\n".join(unit.content for unit in first)
        self.assertIn(b"User=operator", rendered)
        self.assertIn(b"/opt/secretary", rendered)
        self.assertIn(b"/srv/secretary-instance", rendered)
        self.assertIn(b"/srv/secretary-data", rendered)
        self.assertNotIn(b"/home/dev", rendered)
    def test_catalogue_reads_component_digest_and_installability(self):
        with tempfile.TemporaryDirectory() as tmp:
            packaging = write_packaging(Path(tmp))
            units = {unit.name: unit for unit in load_packaged_units(packaging, UNIT_PREFIX)}

        self.assertEqual(
            sorted(units),
            [
                "secretary-dispatcher-production.service",
                "secretary-dispatcher-production.timer",
                "secretary-example.service",
                "secretary-example.timer",
                "secretary-memory.service",
            ],
        )
        self.assertEqual(units["secretary-example.timer"].component, "example")
        self.assertTrue(units["secretary-example.timer"].installable)
        # No [Install] section, so enabling it would fail: it is pulled in by the timer.
        self.assertFalse(units["secretary-example.service"].installable)
        self.assertNotEqual(
            units["secretary-example.timer"].digest, units["secretary-example.service"].digest
        )

    def test_a_unit_outside_our_prefix_is_not_ours(self):
        with tempfile.TemporaryDirectory() as tmp:
            packaging = write_packaging(Path(tmp))
            (packaging / "other-thing.timer").write_text(TIMER, encoding="utf-8")
            names = {unit.name for unit in load_packaged_units(packaging, UNIT_PREFIX)}
        self.assertNotIn("other-thing.timer", names)

    def test_component_is_enabled_unless_the_instance_opts_out(self):
        self.assertTrue(component_enabled({}, "curator"))
        self.assertTrue(component_enabled({"components": {"curator": {"reason": "note"}}}, "curator"))
        self.assertFalse(component_enabled({"components": {"curator": {"enabled": False}}}, "curator"))

    def test_disabled_component_leaves_the_desired_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            packaged = load_packaged_units(write_packaging(Path(tmp)), UNIT_PREFIX)
            instance = instance_config(Path(tmp), components={"example": {"enabled": False}})
            names = {r.name for r in build_plan(instance, [], packaged=packaged)}
        self.assertNotIn("secretary-example.timer", names)
        self.assertIn("secretary-memory.service", names)

    def test_editing_a_shipped_unit_makes_the_resource_an_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            packaging = write_packaging(Path(tmp))
            instance = instance_config(Path(tmp))
            before = build_plan(instance, [], packaged=load_packaged_units(packaging, UNIT_PREFIX))
            (packaging / "secretary-example.timer").write_text(
                TIMER.replace("hourly", "daily"), encoding="utf-8"
            )
            after = build_plan(instance, [], packaged=load_packaged_units(packaging, UNIT_PREFIX))
            actual = HostInventory(units={r.name for r in before})
            changes = {c.name: c.action for c in plan_changes(after, actual, before, UNIT_PREFIX)}

        self.assertEqual(changes["secretary-example.timer"], "update")
        self.assertEqual(changes["secretary-example.service"], "unchanged")

    def test_dispatcher_units_carry_the_shipped_file_digest(self):
        packaged = load_packaged_units(
            upgrade.default_product_root() / "packaging" / "systemd", UNIT_PREFIX
        )
        by_id = {r.logical_id: r for r in build_plan(instance_config(Path("/tmp")), [], packaged=packaged)}
        spec = json.loads(by_id["systemd:dispatcher:production.service"].spec)
        self.assertIn("digest", spec)
        self.assertIn("production-tick", spec["runtime"])

    def test_declared_foreign_unit_is_not_a_conflict(self):
        actual = HostInventory(units={"secretary-supervisor.timer"})
        conflicts = [c for c in plan_changes([], actual, [], UNIT_PREFIX) if c.action == "conflict"]
        self.assertEqual([c.name for c in conflicts], ["secretary-supervisor.timer"])

        declared = plan_changes([], actual, [], UNIT_PREFIX, {"secretary-supervisor.timer"})
        self.assertEqual([c for c in declared if c.action == "conflict"], [])


class ApplyHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.packaging = write_packaging(self.root)
        self.data = self.root / "data"
        self.data.mkdir()
        self.manifest = self.data / "host-managed.json"
        self.packaged = load_packaged_units(self.packaging, UNIT_PREFIX)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def inputs(self, inventory: HostInventory, managed=(), instance=None, bindings=()) -> ApplyInputs:
        return ApplyInputs(
            instance=instance or instance_config(self.data),
            bindings=list(bindings),
            inventory=inventory,
            managed=list(managed),
            manifest_path=self.manifest,
            packaged=self.packaged,
        )

    def test_empty_host_is_installed_enabled_and_recorded(self):
        units, orca = FakeUnitInstaller(), FakeRegistrar()

        result = apply_host(self.inputs(HostInventory()), units=units, orca=orca)

        self.assertTrue(result.ok, result.errors)
        self.assertIn(("install", "secretary-example.timer"), units.calls)
        self.assertIn(("enable", "secretary-example.timer"), units.calls)
        # The service has no [Install]; enabling it would fail, so we never try.
        self.assertNotIn(("enable", "secretary-example.service"), units.calls)
        self.assertIn(("daemon-reload", ""), units.calls)
        recorded = {r.name for r in strict_manifest(self.manifest)[0]}
        self.assertIn("secretary-example.timer", recorded)

    def test_second_run_against_the_reconciled_host_changes_nothing(self):
        units, orca = FakeUnitInstaller(), FakeRegistrar()
        apply_host(self.inputs(HostInventory()), units=units, orca=orca)
        managed, error = strict_manifest(self.manifest)
        self.assertEqual(error, "")
        installed = HostInventory(units=set(units.files))
        units.calls.clear()

        result = apply_host(self.inputs(installed, managed), units=units, orca=orca)

        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(units.calls, [])
        self.assertEqual({c.action for c in result.changes}, {"unchanged"})

    def test_an_unowned_name_in_our_namespace_aborts_before_any_write(self):
        units, orca = FakeUnitInstaller(present={"secretary-example.timer": b"hand written"}), FakeRegistrar()
        inventory = HostInventory(units={"secretary-example.timer"})

        result = apply_host(self.inputs(inventory), units=units, orca=orca)

        self.assertFalse(result.ok)
        self.assertEqual([c.name for c in result.conflicts], ["secretary-example.timer"])
        self.assertEqual(units.calls, [])
        self.assertFalse(self.manifest.exists())
        self.assertEqual(units.files["secretary-example.timer"], b"hand written")

    def test_a_conflict_anywhere_stops_the_units_that_would_have_been_fine(self):
        units, orca = FakeUnitInstaller(), FakeRegistrar()
        inventory = HostInventory(units={"secretary-legacy.timer"})

        result = apply_host(self.inputs(inventory), units=units, orca=orca)

        self.assertFalse(result.ok)
        self.assertEqual(units.calls, [])

    def test_dry_run_reports_the_same_changes_and_writes_nothing(self):
        units, orca = FakeUnitInstaller(), FakeRegistrar()

        preview = apply_host(self.inputs(HostInventory()), units=units, orca=orca, dry_run=True)

        self.assertTrue(preview.ok)
        self.assertEqual({c.action for c in preview.changes}, {"create"})
        self.assertEqual(units.calls, [])
        self.assertFalse(self.manifest.exists())

    def test_a_dropped_component_is_disabled_removed_and_forgotten(self):
        units, orca = FakeUnitInstaller(), FakeRegistrar()
        apply_host(self.inputs(HostInventory()), units=units, orca=orca)
        managed, _ = strict_manifest(self.manifest)
        installed = HostInventory(units=set(units.files))
        units.calls.clear()
        shed = instance_config(self.data, components={"example": {"enabled": False}})

        result = apply_host(self.inputs(installed, managed, instance=shed), units=units, orca=orca)

        self.assertTrue(result.ok, result.errors)
        self.assertIn(("disable", "secretary-example.timer"), units.calls)
        self.assertIn(("remove", "secretary-example.timer"), units.calls)
        # The service was never enabled (no [Install]), so disabling it would fail.
        self.assertNotIn(("disable", "secretary-example.service"), units.calls)
        self.assertIn(("remove", "secretary-example.service"), units.calls)
        recorded = {r.name for r in strict_manifest(self.manifest)[0]}
        self.assertNotIn("secretary-example.timer", recorded)

    def test_a_failed_install_is_never_recorded_as_managed(self):
        units, orca = FakeUnitInstaller(), FakeRegistrar()
        units.fail_on = {"secretary-example.timer"}

        result = apply_host(self.inputs(HostInventory()), units=units, orca=orca)

        self.assertFalse(result.ok)
        recorded = {r.name for r in strict_manifest(self.manifest)[0]}
        self.assertNotIn("secretary-example.timer", recorded)

    def test_orca_registration_is_created_from_the_binding(self):
        units, orca = FakeUnitInstaller(), FakeRegistrar()
        binding = {"id": "demo", "repo": "/srv/demo", "orca_binding": "demo", "enabled": True}

        apply_host(self.inputs(HostInventory(), bindings=[binding]), units=units, orca=orca)

        self.assertEqual(orca.added, [("demo", "/srv/demo")])

    def test_an_orca_deletion_is_refused_rather_than_silently_recorded(self):
        units, orca = FakeUnitInstaller(), FakeRegistrar()
        binding = {"id": "demo", "repo": "/srv/demo", "orca_binding": "demo", "enabled": True}
        apply_host(self.inputs(HostInventory(), bindings=[binding]), units=units, orca=orca)
        managed, _ = strict_manifest(self.manifest)
        inventory = HostInventory(units=set(units.files), orca_repos={"demo"})

        result = apply_host(self.inputs(inventory, managed), units=units, orca=orca)

        self.assertFalse(result.ok)
        self.assertTrue(any("no repo removal command" in error for error in result.errors), result.errors)
        recorded = {r.name for r in strict_manifest(self.manifest)[0]}
        self.assertIn("demo", recorded)


class AutomationSpecTests(unittest.TestCase):
    def spec(self, **overrides) -> AutomationSpec:
        base = AutomationSpec(
            name="steward",
            prompt="/steward",
            provider="claude",
            precheck="python3 -m triggered_agents steward precheck",
            reuse_session=True,
            trigger="0 */3 * * *",
            enabled=False,
            workspace="/home/dev/orca/workspaces/secretary/steward",
            repo="/home/dev/secretary",
        )
        return replace(base, **overrides)

    def live(self, **overrides) -> dict:
        record = {
            "id": "auto-1",
            "name": "steward",
            "prompt": "/steward",
            "agentId": "claude",
            "precheck": {"command": "python3 -m triggered_agents steward precheck"},
            "reuseSession": True,
            "enabled": False,
            "workspaceMode": "existing",
            "workspaceId": "repo-1::/home/dev/orca/workspaces/secretary/steward",
            "runContext": {"path": "/home/dev/secretary"},
            "rrule": "0 */3 * * *",
        }
        record.update(overrides)
        return record

    def test_a_matching_automation_has_no_drift(self):
        self.assertEqual(drifted_fields(self.spec(), self.live()), ())

    def test_a_stale_workspace_is_drift_even_when_prompt_and_precheck_match(self):
        stale = self.live(
            workspaceId="repo-2::/home/dev/orca/workspaces/triggered-agents/steward",
            runContext={"path": "/home/dev/triggered-agents"},
        )
        self.assertEqual(drifted_fields(self.spec(), stale), ("repo", "workspace"))

    def test_a_stale_workspace_becomes_a_repoint_at_the_desired_path(self):
        stale = self.live(workspaceId="repo-2::/home/dev/orca/workspaces/triggered-agents/steward")
        change = plan_automations([self.spec()], [stale])[0]

        self.assertEqual((change.action, change.drifted), ("repoint", ("workspace",)))
        argv = repoint_argv(self.spec(), change.automation_id)
        self.assertIn("--id", argv)
        self.assertIn("path:/home/dev/orca/workspaces/secretary/steward", argv)
        self.assertNotIn("--repo", argv)
        self.assertIn("--disabled", argv)
        self.assertIn("--reuse-session", argv)

    def test_orca_trigger_state_is_owned_by_the_spec(self):
        self.assertEqual(drifted_fields(self.spec(), self.live(enabled=True)), ("enabled",))

    def test_an_explicit_cron_is_compared_but_a_preset_expansion_is_not(self):
        self.assertEqual(drifted_fields(self.spec(), self.live(rrule="0 */6 * * *")), ("trigger",))
        preset = self.spec(trigger="daily")
        self.assertEqual(drifted_fields(preset, self.live(rrule="FREQ=DAILY;BYHOUR=9")), ())

    def test_a_missing_automation_is_a_create(self):
        change = plan_automations([self.spec()], [])[0]
        self.assertEqual((change.action, change.automation_id), ("create", ""))

    def test_new_per_run_uses_repo_instead_of_workspace_selector(self):
        argv = create_argv(self.spec(workspace_mode="new-per-run"))
        self.assertIn("--repo", argv)
        self.assertIn("path:/home/dev/secretary", argv)
        self.assertNotIn("--workspace", argv)

    def test_shipped_specs_skip_the_deterministic_dispatcher(self):
        specs = {spec.name: spec for spec in load_specs(upgrade.default_product_root())}
        self.assertNotIn("pipeline", specs)
        self.assertEqual(specs["steward"].prompt, "/steward")
        self.assertTrue(specs["steward"].workspace.endswith("/secretary/steward"))

    def test_every_background_role_disables_its_orca_trigger(self):
        # The systemd timer is the sole schedule owner on the headless box; the Orca automation
        # itself stays --disabled so a non-headless orca (or a GUI re-enable) cannot double-fire a
        # role alongside its timer. retro used to ship without this and would be created --enabled.
        specs = {spec.name: spec for spec in load_specs(upgrade.default_product_root())}
        for role in ("curator", "retro", "steward"):
            self.assertFalse(specs[role].enabled, f"{role} Orca automation must be disabled")
            self.assertIn("--disabled", create_argv(specs[role]))


class UpgradeStepTests(unittest.TestCase):
    def context(self, units: FakeUnitInstaller, **overrides) -> upgrade.UpgradeContext:
        base = upgrade.UpgradeContext(
            instance_path=Path("/tmp/instance"),
            product_root=upgrade.default_product_root(),
            base_branch="main",
            dry_run=False,
            units=units,
            orca=FakeRegistrar(),
            automations=None,
            report=_Report(),
        )
        return replace(base, **overrides)

    def test_memory_restarts_when_only_the_code_moved(self):
        units = FakeUnitInstaller(active={"secretary-memory.service"})

        result = upgrade.step_memory(self.context(units, code_changed=True))

        self.assertEqual(result.status, "changed")
        self.assertIn("code or dependencies changed", result.detail)
        self.assertIn(("restart", "secretary-memory.service"), units.calls)

    def test_memory_restarts_when_its_unit_file_changed(self):
        units = FakeUnitInstaller(active={"secretary-memory.service"})

        result = upgrade.step_memory(self.context(units, unit_changed=True))

        self.assertEqual(result.status, "changed")
        self.assertIn(("restart", "secretary-memory.service"), units.calls)

    def test_memory_is_left_alone_when_nothing_moved(self):
        units = FakeUnitInstaller(active={"secretary-memory.service"})

        result = upgrade.step_memory(self.context(units))

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(units.calls, [])

    def test_a_stopped_memory_service_is_started_even_with_no_change(self):
        units = FakeUnitInstaller()

        result = upgrade.step_memory(self.context(units))

        self.assertEqual(result.status, "changed")
        self.assertIn("not active", result.detail)

    def test_dry_run_decides_the_restart_without_performing_it(self):
        units = FakeUnitInstaller(active={"secretary-memory.service"})

        result = upgrade.step_memory(self.context(units, code_changed=True, dry_run=True))

        self.assertEqual(result.status, "changed")
        self.assertEqual(units.calls, [])

    # secretary-756: the two scenarios formerly here (materializing a foreign
    # `secretary-orca.service` before the ownership migration, and `step_host` failing over
    # an unavailable Orca executable before writing ownership) both depended on the product
    # shipping a `secretary-orca.*` systemd unit. Orca is host-owned and external
    # (secretary-739/755): packaging/systemd ships no such unit, `resolve_packaged` no longer
    # raises over a missing Orca executable, and `step_host` can no longer materialize or
    # gate on one. Deleted rather than rewritten.

    def test_the_run_stops_at_the_first_failed_step(self):
        calls: list[str] = []

        def ok(context):
            calls.append("ok")
            return upgrade.StepResult("ok", "unchanged")

        def bad(context):
            calls.append("bad")
            return upgrade.StepResult("bad", "failed", "boom")

        result = upgrade.run_steps(self.context(FakeUnitInstaller()), steps=(ok, bad, ok))

        self.assertEqual(calls, ["ok", "bad"])
        self.assertFalse(result.ok)
        self.assertIn("failed", result.render())

    def test_a_dependency_manifest_move_triggers_a_reinstall_decision(self):
        units = FakeUnitInstaller()
        context = self.context(units, changed_paths=("pyproject.toml",), dry_run=True)

        result = upgrade.step_dependencies(context)

        self.assertIn(result.status, {"changed", "skipped"})
        if result.status == "changed":
            self.assertIn("reinstall", result.detail)

    def test_a_code_only_move_leaves_dependencies_alone(self):
        context = self.context(FakeUnitInstaller(), changed_paths=("secretary/cli.py",), dry_run=True)

        result = upgrade.step_dependencies(context)

        self.assertIn(result.status, {"unchanged", "skipped"})

    def test_head_registry_step_materializes_the_product_canon_idempotently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir)
            (instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            context = self.context(FakeUnitInstaller(), instance_path=instance)

            result = upgrade.step_head_registry(context)
            again = upgrade.step_head_registry(context)

            self.assertEqual(result.status, "changed")
            self.assertEqual(again.status, "unchanged")
            self.assertEqual(load_snapshot(instance), canonical_heads(context.product_root))
            self.assertEqual(load_snapshot(instance)["profiles"]["codex-terra"]["model"], "gpt-5.6-terra")

    def test_head_registry_dry_run_reports_drift_without_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir)
            (instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            context = self.context(FakeUnitInstaller(), instance_path=instance, dry_run=True)

            result = upgrade.step_head_registry(context)

            self.assertEqual(result.status, "changed")
            self.assertFalse((instance / "heads" / "heads.yaml").exists())
            self.assertFalse((instance / "heads" / "source.yaml").exists())

    def test_head_registry_step_pins_the_checkout_the_snapshot_came_from(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir)
            (instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            context = self.context(FakeUnitInstaller(), instance_path=instance)

            upgrade.step_head_registry(context)

            pin = read_source(instance)
            self.assertEqual(pin["product_root"], str(context.product_root.resolve()))
            self.assertEqual(pin["revision"], product_revision(context.product_root))

    def test_head_registry_step_repins_a_moved_checkout_without_snapshot_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir)
            (instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            context = self.context(FakeUnitInstaller(), instance_path=instance)
            upgrade.step_head_registry(context)
            snapshot_before = load_snapshot(instance)
            (instance / "heads" / "source.yaml").write_text(
                "product_root: /somewhere/else\nrevision: deadbeef\n", encoding="utf-8"
            )

            result = upgrade.step_head_registry(context)

            self.assertEqual(result.status, "changed")
            self.assertIn("source.yaml", result.detail)
            self.assertEqual(load_snapshot(instance), snapshot_before)
            self.assertEqual(read_source(instance)["product_root"], str(context.product_root.resolve()))

    def test_upgrade_direct_config_path_renders_the_same_units_as_its_checkout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            instance.mkdir()
            data_dir = root / "data"
            data_dir.mkdir()
            config = instance / "instance.yaml"
            config.write_text(
                "version: 1\nname: upgrade\ndata_dir: " + str(data_dir)
                + "\noffsite:\n  instance_remote: git@example.invalid:x/y\nhost:\n  unit_prefix: secretary-\n",
                encoding="utf-8",
            )
            account = SimpleNamespace(pw_dir="/srv/operator")
            rendered: list[dict[str, bytes]] = []

            def capture(context: upgrade.UpgradeContext) -> upgrade.UpgradeResult:
                packaged = upgrade.resolve_packaged(
                    context.report.instance,
                    context.product_root / "packaging" / "systemd",
                    product_root=context.product_root,
                    instance_path=context.instance_path,
                    runtime_user="operator",
                )
                rendered.append({unit.name: unit.content for unit in packaged})
                return upgrade.UpgradeResult()

            with mock.patch.object(upgrade, "run_steps", side_effect=capture), mock.patch(
                "secretary.host_apply.pwd.getpwnam", return_value=account
            ), mock.patch("secretary.host_apply.find_orca_executable", return_value=Path("/usr/local/bin/orca")), mock.patch(
                "secretary.host_apply._is_executable", return_value=True
            ):
                for value in (instance, config):
                    code = upgrade.run_upgrade(SimpleNamespace(
                        instance=str(value),
                        product_root=None,
                        base_branch="main",
                        dry_run=True,
                        no_pull=True,
                        host_fixture=None,
                        json=False,
                    ))
                    self.assertEqual(code, 0)

            self.assertEqual(rendered[0], rendered[1])
            self.assertIn(str(instance).encode(), rendered[1]["secretary-memory.service"])
            self.assertNotIn(str(config).encode(), rendered[1]["secretary-memory.service"])

    def test_stale_head_snapshot_fails_the_upgrade_verify(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir)
            (instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            (instance / "heads").mkdir()
            (instance / "heads" / "heads.yaml").write_text(
                "profiles:\n  codex:\n    model: gpt-5.5\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(HeadRegistryConfigError, "is stale"):
                assert_snapshot_current(instance, upgrade.default_product_root())

    def test_product_profiles_have_no_gpt_5_5_pin_except_openrouter_hermes(self):
        profiles = canonical_heads(upgrade.default_product_root())["profiles"]

        stale = {
            name: profile.get("model")
            for name, profile in profiles.items()
            if name != "hermes" and profile.get("model") == "gpt-5.5"
        }
        self.assertEqual(stale, {})
        self.assertEqual(profiles["codex-reviewer"]["model"], "gpt-5.6-terra")

    def test_missing_role_worktrees_are_recreated_from_product_head(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            product = root / "product"
            product.mkdir()
            agent = product / "triggered_agents" / "agents" / "curator"
            agent.mkdir(parents=True)
            (agent / "automation.toml").write_text("name = 'curator'\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(product)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(product), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(product), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(["git", "-C", str(product), "add", "."], check=True)
            subprocess.run(["git", "-C", str(product), "commit", "-m", "product"], check=True,
                           capture_output=True)
            subprocess.run(["git", "-C", str(product), "remote", "add", "origin", str(product)], check=True)

            with mock.patch.dict(os.environ, {"TA_WORKSPACES_ROOT": str(root / "workspaces")}):
                result = upgrade.step_worktrees(self.context(FakeUnitInstaller(), product_root=product))
                again = upgrade.step_worktrees(self.context(FakeUnitInstaller(), product_root=product))

            worktree = root / "workspaces" / "secretary" / "curator"
            self.assertEqual(result.status, "changed")
            self.assertTrue((worktree / ".git").is_file())
            self.assertEqual(again.status, "unchanged")


class CommandSurfaceTests(unittest.TestCase):
    """The health and materialize commands as an operator and a gate see them."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.instance = self.root / "instance"
        self.instance.mkdir()
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: apply\ndata_dir: "
            + str(self.root / "data")
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y\n"
            + "host:\n  unit_prefix: secretary-\n  foreign_units:\n    - secretary-supervisor.timer\n",
            encoding="utf-8",
        )
        (self.root / "data").mkdir()
        self.fixture = self.root / "host"
        self.fixture.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        import contextlib
        import io

        from secretary.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()

    def test_apply_dry_run_shows_the_plan_and_writes_no_manifest(self):
        (self.fixture / "units.txt").write_text("", encoding="utf-8")
        manifest = self.root / "data" / "host-managed.json"

        code, output = self.run_cli([
            "reconcile", "apply", "--instance", str(self.instance),
            "--host-fixture", str(self.fixture), "--dry-run",
        ])

        self.assertEqual(code, 0, output)
        self.assertIn("create systemd:unit:secretary-curator.timer", output)
        self.assertFalse(manifest.exists())

    def test_apply_refuses_an_unowned_name_and_names_the_way_out(self):
        (self.fixture / "units.txt").write_text("secretary-curator.timer\n", encoding="utf-8")

        code, output = self.run_cli([
            "reconcile", "apply", "--instance", str(self.instance),
            "--host-fixture", str(self.fixture), "--dry-run",
        ])

        self.assertEqual(code, 1, output)
        self.assertIn("secretary reconcile adopt", output)
        self.assertIn("host.foreign_units", output)

    def test_a_declared_foreign_unit_does_not_block_apply(self):
        (self.fixture / "units.txt").write_text("secretary-supervisor.timer\n", encoding="utf-8")

        code, output = self.run_cli([
            "reconcile", "apply", "--instance", str(self.instance),
            "--host-fixture", str(self.fixture), "--dry-run",
        ])

        self.assertEqual(code, 0, output)

    def test_a_unit_is_adopted_only_when_it_matches_the_shipped_file(self):
        unit_dir = self.root / "units"
        unit_dir.mkdir()
        shipped = upgrade.default_product_root() / "packaging" / "systemd" / "secretary-curator.timer"
        (unit_dir / "secretary-curator.timer").write_bytes(shipped.read_bytes())
        argv = [
            "reconcile", "adopt", "--instance", str(self.instance),
            "--logical-id", "systemd:unit:secretary-curator.timer",
            "--unit-dir", str(unit_dir),
        ]

        code, output = self.run_cli(argv)
        self.assertEqual(code, 0, output)
        self.assertIn("adopt systemd:unit:secretary-curator.timer", output)

        (unit_dir / "secretary-curator.timer").write_text("hand written\n", encoding="utf-8")
        code, output = self.run_cli(argv)
        self.assertEqual(code, 2, output)
        self.assertIn("does not match the shipped file", output)

    def test_role_skills_audit_is_available_as_a_health_command(self):
        code, output = self.run_cli(["role-skills", "audit"])
        self.assertIn(code, (0, 1))
        self.assertIn("role skills:", output)


class HealthUnitNameTests(unittest.TestCase):
    def test_agents_map_to_the_packaged_units_not_the_retired_ta_names(self):
        from triggered_agents.runtime import health

        self.assertEqual(health.timer_unit("curator"), "secretary-curator.timer")
        self.assertEqual(health.timer_unit("steward"), "secretary-steward.timer")
        # The pipeline's clock is the production dispatcher's timer.
        self.assertEqual(health.timer_unit("pipeline"), "secretary-dispatcher-production.timer")

    def test_every_checked_unit_is_one_the_product_ships(self):
        from triggered_agents.__main__ import AGENTS
        from triggered_agents.runtime import health

        shipped = {
            unit.name
            for unit in load_packaged_units(
                upgrade.default_product_root() / "packaging" / "systemd", UNIT_PREFIX
            )
        }
        for agent in AGENTS:
            self.assertIn(health.timer_unit(agent), shipped, agent)


class _Report:
    """The slice of an InstanceReport the host and memory steps read."""

    host = {"unit_prefix": UNIT_PREFIX}
    instance = {"host": {"unit_prefix": UNIT_PREFIX}, "data_dir": "/tmp/does-not-matter"}
    bindings: list = []


if __name__ == "__main__":
    unittest.main()
