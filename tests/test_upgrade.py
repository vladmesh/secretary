"""Tests for the upgrade materializer: packaged units, reconcile apply, automations."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import status, upgrade
from secretary.automations import (
    AutomationSpec,
    create_argv,
    drifted_fields,
    load_specs,
    plan_automations,
    repoint_argv,
)
from secretary.config import DataDirError
from secretary.head_registry import (
    INSTANCE_ORIGIN,
    PRODUCT_ORIGIN,
    HeadRegistryConfigError,
    assert_snapshot_current,
    canonical_heads,
    canonical_path,
    installed_heads,
    load_snapshot,
    product_revision,
    read_source,
    snapshot_path,
)
from secretary.host import (
    HostInventory,
    SystemdLayout,
    build_plan,
    component_enabled,
    load_managed_manifest,
    load_packaged_units,
    manifest_text,
    plan_changes,
    strict_manifest,
)
from secretary.host_apply import ApplyInputs, apply_host
from tests.fakes.upgrade import FakeRegistrar, FakeUnitInstaller
from triggered_agents.agents.pipeline import heads, health

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
                    instance_config(root / "data"),
                    instance_path=root / "instance",
                    data_dir=root / "data",
                    runtime_user="operator",
                )

        self.assertNotIn("secretary-orca.service", {unit.name for unit in units})

    def test_render_is_stable_and_uses_the_installation_layout(self):
        layout = SystemdLayout(
            Path("/opt/secretary"),
            Path("/srv/secretary-instance"),
            Path("/srv/secretary-data"),
            "operator",
            Path("/home/operator"),
        )
        first = load_packaged_units(
            upgrade.running_product_root() / "packaging" / "systemd", UNIT_PREFIX, layout
        )
        second = load_packaged_units(
            upgrade.running_product_root() / "packaging" / "systemd", UNIT_PREFIX, layout
        )

        self.assertEqual(
            [(unit.name, unit.content, unit.digest) for unit in first],
            [(unit.name, unit.content, unit.digest) for unit in second],
        )
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
        packaged = load_packaged_units(upgrade.running_product_root() / "packaging" / "systemd", UNIT_PREFIX)
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

    def inputs(
        self,
        inventory: HostInventory,
        managed=(),
        instance=None,
        bindings=(),
        runtime_user: str | None = None,
    ) -> ApplyInputs:
        return ApplyInputs(
            instance=instance or instance_config(self.data),
            bindings=list(bindings),
            inventory=inventory,
            managed=list(managed),
            manifest_path=self.manifest,
            packaged=self.packaged,
            runtime_user=runtime_user,
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

    def test_root_published_manifest_is_private_to_the_installation_user(self):
        account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        with (
            mock.patch("secretary.host_apply.os.geteuid", return_value=0),
            mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
        ):
            result = apply_host(
                self.inputs(HostInventory(), runtime_user="operator"),
                units=FakeUnitInstaller(),
                orca=FakeRegistrar(),
            )

        self.assertTrue(result.ok, result.errors)
        self.assertTrue(load_managed_manifest(self.manifest)[0], "installation user can read the published manifest")
        info = self.manifest.stat()
        self.assertEqual((info.st_uid, info.st_gid), (account.pw_uid, account.pw_gid))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)

    def test_unprivileged_reconcile_never_attempts_manifest_ownership_repair(self):
        with (
            mock.patch("secretary.host_apply.os.geteuid", return_value=1000),
            mock.patch("secretary.host_apply.pwd.getpwnam") as account,
            mock.patch("secretary.host_apply.os.chown") as chown,
            mock.patch("secretary.host_apply.os.chmod") as chmod,
        ):
            result = apply_host(
                self.inputs(HostInventory(), runtime_user="operator"),
                units=FakeUnitInstaller(),
                orca=FakeRegistrar(),
            )

        self.assertTrue(result.ok, result.errors)
        account.assert_not_called()
        chown.assert_not_called()
        chmod.assert_not_called()

    def test_root_repair_hands_an_unchanged_manifest_to_the_installation_user(self):
        desired = build_plan(instance_config(self.data), [], packaged=self.packaged)
        self.manifest.write_text(manifest_text(desired), encoding="utf-8")
        account = SimpleNamespace(pw_uid=1234, pw_gid=5678)
        inventory = HostInventory(units={resource.name for resource in desired if resource.kind == "unit"})
        with (
            mock.patch("secretary.host_apply.os.geteuid", return_value=0),
            mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
            mock.patch("secretary.host_apply.os.chown") as chown,
            mock.patch("secretary.host_apply.os.chmod") as chmod,
        ):
            result = apply_host(
                self.inputs(inventory, managed=desired, runtime_user="operator"),
                units=FakeUnitInstaller(),
                orca=FakeRegistrar(),
            )

        self.assertTrue(result.ok, result.errors)
        self.assertFalse(result.applied)
        chown.assert_called_once_with(self.manifest, 1234, 5678, follow_symlinks=False)
        chmod.assert_called_once_with(self.manifest, 0o600, follow_symlinks=False)

    def test_manifest_write_refuses_untrusted_existing_state(self):
        self.manifest.write_text("not json", encoding="utf-8")
        before = self.manifest.read_bytes()

        result = apply_host(self.inputs(HostInventory()), units=FakeUnitInstaller(), orca=FakeRegistrar())

        self.assertFalse(result.ok)
        self.assertIn("managed manifest is not valid JSON", result.errors)
        self.assertEqual(self.manifest.read_bytes(), before)

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
        specs = {spec.name: spec for spec in load_specs(upgrade.running_product_root())}
        self.assertNotIn("pipeline", specs)
        self.assertEqual(specs["steward"].prompt, "/steward")
        self.assertTrue(specs["steward"].workspace.endswith("/secretary/steward"))

    def test_every_background_role_disables_its_orca_trigger(self):
        # The systemd timer is the sole schedule owner on the headless box; the Orca automation
        # itself stays --disabled so a non-headless orca (or a GUI re-enable) cannot double-fire a
        # role alongside its timer. retro used to ship without this and would be created --enabled.
        specs = {spec.name: spec for spec in load_specs(upgrade.running_product_root())}
        for role in ("curator", "retro", "steward"):
            self.assertFalse(specs[role].enabled, f"{role} Orca automation must be disabled")
            self.assertIn("--disabled", create_argv(specs[role]))


class UpgradeStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memory_probe = mock.patch("secretary.upgrade.probe_memory").start()

    def tearDown(self) -> None:
        self.memory_probe.stop()

    def context(self, units: FakeUnitInstaller, **overrides) -> upgrade.UpgradeContext:
        base = upgrade.UpgradeContext(
            instance_path=Path("/tmp/instance"),
            product_root=upgrade.running_product_root(),
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

        result = upgrade.step_memory(self.context(units, code_changed=True, runtime_user="memory-runtime"))

        self.assertEqual(result.status, "changed")
        self.assertIn("code or dependencies changed", result.detail)
        self.assertIn(("restart", "secretary-memory.service"), units.calls)
        self.memory_probe.assert_called_once()
        self.assertEqual(self.memory_probe.call_args.kwargs["runtime_user"], "memory-runtime")

    def test_host_step_reports_a_configured_data_dir_resolution_error(self):
        report = SimpleNamespace(
            data_dir=Path("/tmp/data"),
            instance={"host": {"unit_prefix": UNIT_PREFIX}},
            bindings=[],
            host={"unit_prefix": UNIT_PREFIX},
        )
        with mock.patch(
            "secretary.upgrade.resolve_packaged",
            side_effect=DataDirError("invalid instance data_dir"),
        ):
            result = upgrade.step_host(self.context(FakeUnitInstaller(), report=report))

        self.assertEqual(result.status, "failed")
        self.assertIn("invalid instance data_dir", result.detail)

    def test_board_transport_step_imports_retires_and_reports_every_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            subprocess.run(["git", "-C", str(instance), "init", "--quiet"], check=True)
            subprocess.run(["git", "-C", str(instance), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(instance), "config", "user.email", "test@example.invalid"], check=True
            )
            runtime = instance / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://legacy/jsonrpc.php\nKANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=legacy-token\n",
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            result = upgrade.step_board_transport(self.context(FakeUnitInstaller(), instance_path=instance))
            retired = runtime.read_text(encoding="utf-8")
        self.assertEqual(result.status, "changed")
        self.assertIn("imported legacy transport", result.detail)
        self.assertIn("retired legacy runtime values", result.detail)
        self.assertEqual(retired, "")

    def test_board_transport_step_fails_closed_without_writing_on_mismatch_or_missing_tuple(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            subprocess.run(["git", "-C", str(instance), "init", "--quiet"], check=True)
            runtime = instance / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://legacy/jsonrpc.php\nKANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=legacy-token\n",
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            (instance / "board-transport.env").write_text(
                "KANBOARD_URL=http://other/jsonrpc.php\nKANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=other-token\n",
                encoding="utf-8",
            )
            (instance / "board-transport.env").chmod(0o600)
            mismatch = upgrade.step_board_transport(self.context(FakeUnitInstaller(), instance_path=instance))
            self.assertEqual(mismatch.status, "failed")
            self.assertIn("mismatch", mismatch.detail)
            self.assertIn("legacy-token", runtime.read_text(encoding="utf-8"))
            (instance / "board-transport.env").unlink()
            runtime.write_text("OTHER=value\n", encoding="utf-8")
            runtime.chmod(0o600)
            missing = upgrade.step_board_transport(self.context(FakeUnitInstaller(), instance_path=instance))
        self.assertEqual(missing.status, "failed")
        self.assertIn("refuse to guess", missing.detail)

    def test_board_transport_step_dry_run_and_insecure_runtime_do_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            subprocess.run(["git", "-C", str(instance), "init", "--quiet"], check=True)
            runtime = instance / "runtime.env"
            body = "KANBOARD_URL=http://legacy/jsonrpc.php\nKANBOARD_API_USER=jsonrpc\nKANBOARD_API_TOKEN=legacy-token\n"
            runtime.write_text(body, encoding="utf-8")
            runtime.chmod(0o600)
            preview = upgrade.step_board_transport(
                self.context(FakeUnitInstaller(), instance_path=instance, dry_run=True)
            )
            self.assertEqual(preview.status, "would-change")
            self.assertIn("would import legacy transport", preview.detail)
            self.assertIn("would retire legacy runtime values", preview.detail)
            self.assertEqual(runtime.read_text(encoding="utf-8"), body)
            self.assertFalse((instance / "board-transport.env").exists())
            runtime.chmod(0o644)
            insecure = upgrade.step_board_transport(self.context(FakeUnitInstaller(), instance_path=instance))
        self.assertEqual(insecure.status, "failed")
        self.assertIn("permissions are too broad", insecure.detail)

    def test_board_transport_step_reports_an_already_configured_transport_as_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            subprocess.run(["git", "-C", str(instance), "init", "--quiet"], check=True)
            (instance / ".gitignore").write_text("/board-transport.env\n", encoding="utf-8")
            transport = instance / "board-transport.env"
            transport.write_text(
                "KANBOARD_URL=http://127.0.0.1:8080/jsonrpc.php\n"
                "KANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=local-token\n",
                encoding="utf-8",
            )
            transport.chmod(0o600)
            result = upgrade.step_board_transport(self.context(FakeUnitInstaller(), instance_path=instance))
        self.assertEqual((result.status, result.detail), ("unchanged", "unchanged"))

    def test_board_transport_step_ignores_unrelated_padded_runtime_lines_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            subprocess.run(["git", "-C", str(instance), "init", "--quiet"], check=True)
            (instance / ".gitignore").write_text("/board-transport.env\n", encoding="utf-8")
            transport = instance / "board-transport.env"
            transport.write_text(
                "KANBOARD_URL=http://127.0.0.1:8080/jsonrpc.php\n"
                "KANBOARD_API_USER=jsonrpc\nKANBOARD_API_TOKEN=local-token\n",
                encoding="utf-8",
            )
            transport.chmod(0o600)
            runtime = instance / "runtime.env"
            runtime.write_text("# host settings\n  GITHUB_TOKEN=abc\nOTHER=xyz \n", encoding="utf-8")
            runtime.chmod(0o600)
            result = upgrade.step_board_transport(self.context(FakeUnitInstaller(), instance_path=instance))
        self.assertEqual((result.status, result.detail), ("unchanged", "unchanged"))

    def test_memory_restarts_when_its_unit_file_changed(self):
        units = FakeUnitInstaller(active={"secretary-memory.service"})

        result = upgrade.step_memory(self.context(units, unit_changed=True))

        self.assertEqual(result.status, "changed")
        self.assertIn(("restart", "secretary-memory.service"), units.calls)
        self.memory_probe.assert_called_once()

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
        self.memory_probe.assert_called_once()

    def test_memory_restart_is_failed_when_the_authenticated_probe_fails(self):
        units = FakeUnitInstaller(active={"secretary-memory.service"})
        self.memory_probe.side_effect = upgrade.MemoryProbeError("MCP did not return an allowed read")

        result = upgrade.step_memory(self.context(units, code_changed=True))

        self.assertEqual(result.status, "failed")
        self.assertIn("authenticated probe failed", result.detail)
        self.assertIn(("restart", "secretary-memory.service"), units.calls)

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

    @staticmethod
    def _venv(root: Path, direct_url: dict | None, ruff_version: str | None = "0.16.4") -> Path:
        """A product checkout whose .venv holds the product installed the given way."""
        (root / ".venv" / "bin").mkdir(parents=True)
        (root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            "[project]\n[project.optional-dependencies]\ndev = ['ruff==0.16.4']\n",
            encoding="utf-8",
        )
        if ruff_version is not None:
            ruff = root / ".venv" / "bin" / "ruff"
            ruff.write_text(f"#!/bin/sh\necho 'ruff {ruff_version}'\n", encoding="utf-8")
            ruff.chmod(0o755)
        dist_info = root / ".venv" / "lib" / "python3.12" / "site-packages" / "secretary-0.1.0.dist-info"
        dist_info.mkdir(parents=True)
        if direct_url is not None:
            (dist_info / "direct_url.json").write_text(json.dumps(direct_url), encoding="utf-8")
        return root

    def test_an_editable_install_that_moved_no_manifest_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(Path(tmp), {"url": "file:///product", "dir_info": {"editable": True}})
            context = self.context(FakeUnitInstaller(), product_root=root, dry_run=True)

            result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "unchanged")

    def test_an_editable_install_with_missing_pinned_ruff_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(
                Path(tmp), {"url": "file:///product", "dir_info": {"editable": True}}, None
            )
            context = self.context(FakeUnitInstaller(), product_root=root, dry_run=True)

            result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertIn("pinned Ruff 0.16.4 is missing", result.detail)

    def test_an_editable_install_with_the_wrong_ruff_version_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(
                Path(tmp), {"url": "file:///product", "dir_info": {"editable": True}}, "0.15.0"
            )
            context = self.context(FakeUnitInstaller(), product_root=root, dry_run=True)

            result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertIn("not 0.16.4", result.detail)

    def test_an_editable_install_with_an_unrunnable_ruff_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(Path(tmp), {"url": "file:///product", "dir_info": {"editable": True}})
            (root / ".venv" / "bin" / "ruff").chmod(0o644)
            context = self.context(FakeUnitInstaller(), product_root=root, dry_run=True)

            result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertIn("cannot run", result.detail)

    def test_ruff_repair_installs_the_declared_dev_extra(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(
                Path(tmp), {"url": "file:///product", "dir_info": {"editable": True}}, None
            )
            context = self.context(FakeUnitInstaller(), product_root=root)
            with mock.patch(
                "secretary.upgrade._proc.run", return_value=subprocess.CompletedProcess([], 0)
            ) as run:
                result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertEqual(
            run.call_args.args[0],
            [
                str(root / ".venv" / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "--quiet",
                "-e",
                f"{root}[dev]",
            ],
        )

    def test_a_snapshot_install_is_reinstalled_even_with_no_manifest_move(self):
        """The 2026-08-05 outage: `step_board_transport` retired the legacy KANBOARD_* tuple while
        this venv still held a copy of the previous day's reader, and every tick failed for 26h."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(Path(tmp), {"url": "file:///product", "dir_info": {}})
            context = self.context(FakeUnitInstaller(), product_root=root, dry_run=True)

            result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertIn("snapshot install", result.detail)

    def test_an_install_that_cannot_prove_it_is_editable_is_treated_as_a_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(Path(tmp), None)
            context = self.context(FakeUnitInstaller(), product_root=root, dry_run=True)

            result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertIn("snapshot install", result.detail)

    def test_head_registry_step_materializes_the_product_canon_idempotently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir)
            (instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            context = self.context(FakeUnitInstaller(), instance_path=instance)

            result = upgrade.step_head_registry(context)
            again = upgrade.step_head_registry(context)

            self.assertEqual(result.status, "changed")
            self.assertEqual(again.status, "unchanged")
            self.assertEqual(load_snapshot(instance), canonical_heads(context.product_root, instance))
            self.assertEqual(load_snapshot(instance)["role_defaults"]["new_card"], "codex")

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

    def test_root_materialization_hands_the_recovery_pair_to_the_runtime_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir)
            (instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            context = self.context(FakeUnitInstaller(), instance_path=instance, runtime_user="operator")
            account = SimpleNamespace(pw_uid=123, pw_gid=456)

            with (
                mock.patch("secretary.upgrade.os.geteuid", return_value=0),
                mock.patch("secretary.upgrade.pwd.getpwnam", return_value=account),
                mock.patch("secretary.upgrade.os.chown") as chown,
            ):
                result = upgrade.step_head_registry(context)

            owned = {Path(call.args[0]) for call in chown.call_args_list}
            self.assertEqual(result.status, "changed")
            self.assertIn(instance / "heads" / "heads.yaml", owned)
            self.assertIn(instance / "heads" / "source.yaml", owned)

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
                "version: 1\nname: upgrade\ndata_dir: "
                + str(data_dir)
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
                    data_dir=context.report.data_dir,
                    runtime_user="operator",
                )
                rendered.append({unit.name: unit.content for unit in packaged})
                return upgrade.UpgradeResult()

            with (
                mock.patch.object(upgrade, "run_steps", side_effect=capture),
                mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
                mock.patch(
                    "secretary.host_apply.find_orca_executable", return_value=Path("/usr/local/bin/orca")
                ),
                mock.patch("secretary.host_apply._is_executable", return_value=True),
            ):
                for value in (instance, config):
                    code = upgrade.run_upgrade(
                        SimpleNamespace(
                            instance=str(value),
                            # The question here is the instance spelling, so the checkout is named
                            # rather than defaulted: the default is a configured path or a home, and
                            # neither has to be a checkout with unit templates in it.
                            product_root=str(upgrade.running_product_root()),
                            base_branch="main",
                            dry_run=True,
                            no_pull=True,
                            host_fixture=None,
                            json=False,
                        )
                    )
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
                assert_snapshot_current(instance, upgrade.running_product_root())

    def test_the_shipped_registry_runs_every_role_on_either_subscription(self):
        """The portable default has to bring a clean host up with only one account authed.

        Both directions: an OpenAI-only host runs the role defaults as written, and a Claude-only
        host reaches a green head for every one of them through the fallback chains.
        """
        canon = canonical_heads(upgrade.running_product_root())
        registry = heads.Registry(canon["resources"], canon["profiles"], canon["role_defaults"])
        roles = ("new_card", "reviewer", "observer", "curator", "retro", "steward")

        self.assertEqual(set(canon["resources"]), {"claude-sub", "openai-sub"})
        for role in roles:
            with self.subTest(role=role):
                preferred = registry.role_default(role)
                self.assertIsNotNone(preferred, f"{role} is routed nowhere")
                for red in ("claude-sub", "openai-sub"):
                    statuses = {red: health.RED}
                    resolved = health.resolve_head(preferred, statuses, registry)
                    self.assertIsNotNone(resolved, f"{role} has no head with {red} red")
                    self.assertNotEqual(
                        registry.profile(resolved)["resource"],
                        red,
                        f"{role} resolved onto the red resource",
                    )

    def test_the_shipped_registry_keeps_worker_and_reviewer_apart_on_one_subscription(self):
        """secretary-1165: a card whose preferred family is dead is transferred, not collapsed.

        The chains are written by hand, so nothing but a test stops a canon from routing both roles
        onto one head the moment a resource goes red — and the dispatcher refuses to claim that
        card, which turns a transfer into a stall the shipped registry should never cause.
        """
        canon = canonical_heads(upgrade.running_product_root())
        registry = heads.Registry(canon["resources"], canon["profiles"], canon["role_defaults"])

        for red in ("claude-sub", "openai-sub"):
            with self.subTest(red=red):
                statuses = {red: health.RED}
                worker = health.resolve_head(registry.role_default("new_card"), statuses, registry)
                reviewer = health.resolve_head(registry.role_default("reviewer"), statuses, registry)
                self.assertIsNotNone(worker)
                self.assertIsNotNone(reviewer)
                self.assertNotEqual(worker, reviewer, "the review would be the worker's own")

    def test_the_shipped_registry_carries_no_installation_account_policy(self):
        """Account policy and model routing are the private canon's, not the product's."""
        canon = canonical_heads(upgrade.running_product_root())

        self.assertEqual(
            [
                name
                for name, resource in canon["resources"].items()
                if resource.get("account") not in {"claude-subscription", "openai-subscription"}
            ],
            [],
        )
        # A pinned model version is a spend decision that ages out of the product; the shipped
        # profiles name a family or nothing at all.
        self.assertEqual(
            [
                name
                for name, profile in canon["profiles"].items()
                if any(char.isdigit() for char in str(profile.get("model", "")))
            ],
            [],
        )

    def test_missing_role_worktrees_are_recreated_from_product_head(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            product = root / "product"
            product.mkdir()
            agent = product / "src" / "triggered_agents" / "agents" / "curator"
            agent.mkdir(parents=True)
            (agent / "automation.toml").write_text("name = 'curator'\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(product)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(product), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(product), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(["git", "-C", str(product), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(product), "commit", "-m", "product"], check=True, capture_output=True
            )
            subprocess.run(["git", "-C", str(product), "remote", "add", "origin", str(product)], check=True)

            with mock.patch.dict(os.environ, {"TA_WORKSPACES_ROOT": str(root / "workspaces")}):
                result = upgrade.step_worktrees(self.context(FakeUnitInstaller(), product_root=product))
                again = upgrade.step_worktrees(self.context(FakeUnitInstaller(), product_root=product))

            worktree = root / "workspaces" / "secretary" / "curator"
            self.assertEqual(result.status, "changed")
            self.assertTrue((worktree / ".git").is_file())
            self.assertEqual(again.status, "unchanged")

    def test_root_materialization_assigns_linked_worktree_and_git_admin_to_runtime_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            product = root / "product"
            product.mkdir()
            agent = product / "src" / "triggered_agents" / "agents" / "curator"
            agent.mkdir(parents=True)
            (agent / "automation.toml").write_text("name = 'curator'\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(product)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(product), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(product), "config", "user.email", "test@example.invalid"], check=True
            )
            subprocess.run(["git", "-C", str(product), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(product), "commit", "-m", "product"], check=True, capture_output=True
            )
            subprocess.run(["git", "-C", str(product), "remote", "add", "origin", str(product)], check=True)
            account = SimpleNamespace(pw_uid=123, pw_gid=456)

            with (
                mock.patch.dict(
                    os.environ, {"TA_WORKSPACES_ROOT": str(root / "home" / "orca" / "workspaces")}
                ),
                mock.patch("secretary.upgrade.os.geteuid", return_value=0),
                mock.patch("secretary.upgrade.pwd.getpwnam", return_value=account),
                mock.patch("secretary.upgrade.os.chown") as chown,
            ):
                result = upgrade.step_worktrees(
                    self.context(FakeUnitInstaller(), product_root=product, runtime_user="operator")
                )

            workspace_root = root / "home" / "orca" / "workspaces"
            worktree = workspace_root / "secretary" / "curator"
            admin = upgrade._worktree_git_dir(worktree)
            owned = {Path(call.args[0]) for call in chown.call_args_list}
            self.assertEqual(result.status, "changed")
            self.assertIn(worktree, owned)
            self.assertIn(worktree.parent, owned)
            self.assertIn(workspace_root, owned)
            self.assertIn(workspace_root.parent, owned)
            self.assertIsNotNone(admin)
            self.assertIn(admin, owned)
            self.assertIn(admin.parent, owned)

    def test_root_ownership_repair_skips_a_hardlinked_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = root / "first"
            linked = root / "linked"
            first.write_text("shared\n", encoding="utf-8")
            os.link(first, linked)
            account = SimpleNamespace(pw_uid=123, pw_gid=456)

            with (
                mock.patch("secretary.upgrade.os.geteuid", return_value=0),
                mock.patch("secretary.upgrade.pwd.getpwnam", return_value=account),
                mock.patch("secretary.upgrade.os.chown") as chown,
            ):
                upgrade._set_runtime_owner(root, "operator")

            owned = {Path(call.args[0]) for call in chown.call_args_list}
            self.assertIn(root, owned)
            self.assertNotIn(first, owned)
            self.assertNotIn(linked, owned)


class HeadRegistryCheckpointTests(unittest.TestCase):
    """The generated registry is a pair in the private recovery repository."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = self.root / "instance-remote.git"
        self.instance = self.root / "instance"
        self.instance.mkdir()
        self._git(self.root, "init", "--quiet", "--bare", "--initial-branch", "main", str(self.remote))
        self._git(self.instance, "init", "--quiet", "--initial-branch", "main")
        self._git(self.instance, "config", "user.name", "test operator")
        self._git(self.instance, "config", "user.email", "test@example.invalid")
        (self.instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        self._git(self.instance, "add", "instance.yaml")
        self._git(self.instance, "commit", "--quiet", "-m", "instance config")
        self._git(self.instance, "remote", "add", "origin", str(self.remote))
        self.context = upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=upgrade.running_product_root(),
            base_branch="main",
            dry_run=False,
            units=FakeUnitInstaller(),
            orca=FakeRegistrar(),
            automations=None,
            report=_Report(),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def _publish(self) -> tuple[upgrade.StepResult, upgrade.StepResult]:
        return upgrade.step_head_registry(self.context), upgrade.step_publish_head_registry(self.context)

    def test_changed_pair_is_scoped_committed_published_and_cleanly_restored(self):
        # These are deliberately all outside the registry writer's pathspec.
        for relative in (
            "state/board/foreign.ndjson",
            "state/memory/facts/foreign.md",
            "state/knowledge/foreign.md",
            "secrets/foreign.age",
            "operator-note.txt",
        ):
            path = self.instance / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("foreign\n", encoding="utf-8")

        generated, published = self._publish()

        self.assertEqual(generated.status, "changed")
        self.assertEqual(published.status, "changed", published.detail)
        changed = self._git(self.instance, "show", "--format=", "--name-only", "HEAD").splitlines()
        self.assertEqual(changed, ["heads/heads.yaml", "heads/source.yaml"])
        self.assertEqual(self._git(self.instance, "diff", "--cached", "--name-only"), "")
        self.assertEqual(
            self._git(self.instance, "status", "--porcelain", "--untracked-files=all").splitlines(),
            [
                "?? operator-note.txt",
                "?? secrets/foreign.age",
                "?? state/board/foreign.ndjson",
                "?? state/knowledge/foreign.md",
                "?? state/memory/facts/foreign.md",
            ],
        )
        remote_files = self._git(self.remote, "show", "--format=", "--name-only", "main").splitlines()
        self.assertIn("heads/heads.yaml", remote_files)
        self.assertIn("heads/source.yaml", remote_files)
        source = read_source(self.instance)
        self.assertIsNotNone(source)
        self.assertEqual(
            source["canonical"],
            str(canonical_path(self.context.product_root, self.instance)[0].resolve()),
        )
        self.assertEqual(source["product_root"], str(self.context.product_root.resolve()))
        self.assertEqual(source["revision"], product_revision(self.context.product_root))

        recovered = self.root / "recovered"
        self._git(self.root, "clone", "--quiet", str(self.remote), str(recovered))
        self.assertEqual(installed_heads(recovered), installed_heads(self.instance))
        self.assertEqual(read_source(recovered), read_source(self.instance))

    def test_unchanged_pair_still_confirms_the_remote_checkpoint(self):
        self._publish()

        generated, published = self._publish()

        self.assertEqual(generated.status, "unchanged")
        self.assertEqual(published.status, "unchanged", published.detail)
        self.assertEqual(
            self._git(self.remote, "rev-parse", "main"),
            self._git(self.instance, "rev-parse", "HEAD"),
        )

    def test_verify_accepts_a_published_pair_with_unrelated_instance_dirt(self):
        tracked = self.instance / "projects" / "operator.yaml"
        tracked.parent.mkdir(parents=True)
        tracked.write_text("id: operator\n", encoding="utf-8")
        self._git(self.instance, "add", "projects/operator.yaml")
        self._git(self.instance, "commit", "--quiet", "-m", "operator project")
        foreign = self.instance / "skills" / "operator-overlay.toml"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("[roles]\n", encoding="utf-8")
        generated, published = self._publish()
        self.assertEqual(generated.status, "changed")
        self.assertEqual(published.status, "changed", published.detail)
        tracked.write_text("id: operator\nname: changed locally\n", encoding="utf-8")

        with (
            mock.patch("secretary.upgrade.step_host", return_value=upgrade.StepResult("host", "unchanged")),
            mock.patch("secretary.upgrade.role_skills.audit", return_value={"ok": True}),
        ):
            verified = upgrade.step_verify(self.context)

        self.assertEqual(verified.status, "unchanged", verified.detail)
        self.assertEqual(
            self._git(self.instance, "status", "--porcelain", "--untracked-files=all").splitlines(),
            ["M projects/operator.yaml", "?? skills/operator-overlay.toml"],
        )

    def test_commit_or_push_failure_refuses_success_and_keeps_an_actionable_checkpoint(self):
        generated = upgrade.step_head_registry(self.context)
        self.assertEqual(generated.status, "changed")
        with mock.patch(
            "secretary.upgrade.state_repo.commit",
            side_effect=upgrade.state_repo.StateRepoError("index locked"),
        ):
            failed_commit = upgrade.step_publish_head_registry(self.context)
        self.assertEqual(failed_commit.status, "failed")
        self.assertIn("index locked", failed_commit.detail)

        failed_push = upgrade.step_publish_head_registry(self.context)
        self.assertEqual(failed_push.status, "changed", failed_push.detail)
        self._git(self.instance, "remote", "set-url", "origin", str(self.root / "missing.git"))
        (self.instance / "heads" / "heads.toml").write_text(
            (
                self.context.product_root / "src" / "triggered_agents" / "agents" / "pipeline" / "heads.toml"
            ).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        upgrade.step_head_registry(self.context)
        failed_push = upgrade.step_publish_head_registry(self.context)
        self.assertEqual(failed_push.status, "failed")
        self.assertIn("local checkpoint", failed_push.detail)
        self.assertNotEqual(
            self._git(self.instance, "rev-parse", "HEAD"), self._git(self.remote, "rev-parse", "main")
        )

    def test_incomplete_or_stale_pair_fails_closed_before_routing(self):
        self._publish()
        source = self.instance / "heads" / "source.yaml"
        source.unlink()
        with self.assertRaisesRegex(HeadRegistryConfigError, "source pin .* is missing"):
            installed_heads(self.instance)

        upgrade.step_head_registry(self.context)
        (self.instance / "heads" / "heads.yaml").write_text(
            (self.instance / "heads" / "heads.yaml").read_text(encoding="utf-8") + "# stale\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(HeadRegistryConfigError, "stale or mismatched"):
            installed_heads(self.instance)


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
        # A host runs a checkout, and these fixtures run this one. Reconcile and the role-skill
        # audit read the configured product, so an installation that names none has no units and
        # no manifest to compare against.
        env = mock.patch.dict(os.environ, {"TA_SECRETARY_REPO": str(upgrade.running_product_root())})
        env.start()
        self.addCleanup(env.stop)

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

        code, output = self.run_cli(
            [
                "reconcile",
                "apply",
                "--instance",
                str(self.instance),
                "--host-fixture",
                str(self.fixture),
                "--dry-run",
            ]
        )

        self.assertEqual(code, 0, output)
        self.assertIn("create systemd:unit:secretary-curator.timer", output)
        self.assertFalse(manifest.exists())

    def test_apply_refuses_an_unowned_name_and_names_the_way_out(self):
        (self.fixture / "units.txt").write_text("secretary-curator.timer\n", encoding="utf-8")

        code, output = self.run_cli(
            [
                "reconcile",
                "apply",
                "--instance",
                str(self.instance),
                "--host-fixture",
                str(self.fixture),
                "--dry-run",
            ]
        )

        self.assertEqual(code, 1, output)
        self.assertIn("secretary reconcile adopt", output)
        self.assertIn("host.foreign_units", output)

    def test_a_declared_foreign_unit_does_not_block_apply(self):
        (self.fixture / "units.txt").write_text("secretary-supervisor.timer\n", encoding="utf-8")

        code, output = self.run_cli(
            [
                "reconcile",
                "apply",
                "--instance",
                str(self.instance),
                "--host-fixture",
                str(self.fixture),
                "--dry-run",
            ]
        )

        self.assertEqual(code, 0, output)

    def test_a_unit_is_adopted_only_when_it_matches_the_shipped_file(self):
        unit_dir = self.root / "units"
        unit_dir.mkdir()
        shipped = upgrade.running_product_root() / "packaging" / "systemd" / "secretary-curator.timer"
        (unit_dir / "secretary-curator.timer").write_bytes(shipped.read_bytes())
        argv = [
            "reconcile",
            "adopt",
            "--instance",
            str(self.instance),
            "--logical-id",
            "systemd:unit:secretary-curator.timer",
            "--unit-dir",
            str(unit_dir),
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


class RunUpgradeClientOwnerTests(unittest.TestCase):
    """`run_upgrade` builds the Orca clients for the account that owns the installation."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.instance = self.root / "instance"
        self.instance.mkdir()

    def build_context(self, *, euid: int):
        report = SimpleNamespace(ok=True, errors=[], instance_path=self.instance / "instance.yaml")
        args = SimpleNamespace(
            instance=str(self.instance),
            product_root=str(self.root / "product"),
            base_branch="main",
            dry_run=True,
            host_fixture=None,
            no_pull=True,
            json=False,
            runtime_user="operator",
        )
        captured = {}

        def remember(name):
            def build(user=None):
                captured[name] = user
                return mock.Mock()

            return build

        with (
            mock.patch.object(upgrade, "validate_instance", return_value=report),
            mock.patch.object(
                upgrade, "resolve_runtime_owner", return_value=("operator", Path("/home/operator"))
            ),
            mock.patch.object(upgrade.os, "geteuid", return_value=euid),
            mock.patch.object(upgrade, "LiveOrcaRegistrar", remember("orca")),
            mock.patch.object(upgrade, "OrcaAutomationClient", remember("automations")),
            mock.patch.object(upgrade, "SystemdUnitInstaller", mock.Mock()),
            mock.patch.object(upgrade, "run_steps", return_value=upgrade.UpgradeResult()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            upgrade.run_upgrade(args)
        return captured

    def test_root_run_reaches_orca_through_the_runtime_user(self):
        """Root has no Orca runtime of its own; the automations step must not call the CLI as root."""
        self.assertEqual(self.build_context(euid=0), {"orca": "operator", "automations": "operator"})

    def test_unprivileged_run_calls_orca_directly(self):
        """`runuser` is root's tool: an owner running the upgrade is already the right account."""
        self.assertEqual(self.build_context(euid=1000), {"orca": None, "automations": None})


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
                upgrade.running_product_root() / "packaging" / "systemd", UNIT_PREFIX
            )
        }
        for agent in AGENTS:
            self.assertIn(health.timer_unit(agent), shipped, agent)


class InstanceHeadCanonTests(unittest.TestCase):
    """Which registry an installation materializes from, and what the snapshot says about it.

    Every fixture here is a temporary instance directory, never the host's own: the point is what
    an arbitrary installation gets, and the developing machine's installation owns a canon that
    would answer for it.
    """

    CANON = (
        '[resources.local-sub]\naccount = "local"\nprobe = "true"\n'
        '[profiles.local-head]\nresource = "local-sub"\nadapter = "claude"\nfallback = []\n'
        '[profiles.local-reviewer]\nresource = "local-sub"\nadapter = "claude"\nfallback = []\n'
        '[role_defaults]\nnew_card = "local-head"\nreviewer = "local-reviewer"\n'
        'curator = "local-head"\nretro = "local-head"\nsteward = "local-head"\n'
        'observer = "local-reviewer"\n'
    )

    def instance(self, root: Path, canon: str | None = None) -> Path:
        (root / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        (root / "heads").mkdir(exist_ok=True)
        if canon is not None:
            (root / "heads" / "heads.toml").write_text(canon, encoding="utf-8")
        return root

    def test_an_installation_that_owns_a_canon_materializes_from_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir), self.CANON)
            product = upgrade.running_product_root()

            path, origin = canonical_path(product, instance)
            heads_data = canonical_heads(product, instance)

            self.assertEqual(path, instance / "heads" / "heads.toml")
            self.assertEqual(origin, INSTANCE_ORIGIN)
            self.assertEqual(heads_data["role_defaults"]["new_card"], "local-head")
            self.assertNotEqual(heads_data, canonical_heads(product))

    def test_an_installation_with_no_canon_stays_runnable_on_the_product_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir))
            product = upgrade.running_product_root()

            path, origin = canonical_path(product, instance)

            self.assertEqual(
                path,
                product / "src" / "triggered_agents" / "agents" / "pipeline" / "heads.toml",
            )
            self.assertEqual(origin, PRODUCT_ORIGIN)
            self.assertEqual(canonical_heads(product, instance), canonical_heads(product))

    def test_a_present_but_unusable_canon_fails_by_name_instead_of_falling_back(self):
        product = upgrade.running_product_root()
        for name, build in (
            (
                "malformed",
                lambda root: (root / "heads" / "heads.toml").write_text("nope = [", encoding="utf-8"),
            ),
            ("directory", lambda root: (root / "heads" / "heads.toml").mkdir()),
            ("dangling", lambda root: (root / "heads" / "heads.toml").symlink_to(root / "gone.toml")),
            (
                "unreadable",
                lambda root: (
                    (root / "heads" / "heads.toml").write_text(self.CANON, encoding="utf-8"),
                    (root / "heads" / "heads.toml").chmod(0o000),
                ),
            ),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmpdir:
                instance = self.instance(Path(tmpdir))
                build(instance)
                self.addCleanup(_restore_mode, instance / "heads" / "heads.toml")

                with self.assertRaises(HeadRegistryConfigError) as caught:
                    canonical_heads(product, instance)

                self.assertIn(str(instance / "heads" / "heads.toml"), str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, "root traverses a directory with no search bit")
    def test_an_unsearchable_heads_directory_fails_the_step_by_name(self):
        """The probe that decides which canon wins can itself fail on the filesystem.

        `Path.is_file()` does not swallow EACCES, so a `heads/` directory with no search bit used
        to hand `secretary upgrade` a raw PermissionError. The step catches the bounded config
        error and nothing else, so that crashed the upgrade instead of failing one step by path.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir), self.CANON)
            owned = instance / "heads" / "heads.toml"
            self.addCleanup(_restore_mode, instance / "heads", mode=0o755)
            (instance / "heads").chmod(0o000)

            with self.assertRaises(HeadRegistryConfigError) as caught:
                canonical_path(upgrade.running_product_root(), instance)
            result = upgrade.step_head_registry(self.context(instance))

            self.assertIn(str(owned), str(caught.exception))
            self.assertEqual(result.status, "failed")
            self.assertIn(str(owned), result.detail)

    def test_a_canon_with_a_malformed_entry_fails_the_upgrade_step_by_name(self):
        """Not just unparseable files: a parsed canon whose entries are the wrong shape.

        The entries are hand-written, so any of them can be a list where a table or a name
        belongs. Every one of those has to come back as the bounded config error naming the file
        — the upgrade step handles that error and nothing else, so a raw AttributeError or
        TypeError would escape the step instead of failing it.
        """
        broken = {
            "list profile": '[resources.local-sub]\naccount = "local"\nprofiles = { local-head = [] }\n',
            "list resource": "resources = { local-sub = [] }\n"
            '[profiles.local-head]\nresource = "local-sub"\nadapter = "claude"\n'
            '[role_defaults]\nnew_card = "local-head"\n',
            "list role default": '[resources.local-sub]\naccount = "local"\n'
            '[profiles.local-head]\nresource = "local-sub"\nadapter = "claude"\n'
            "[role_defaults]\nnew_card = []\n",
            "list fallback entry": '[resources.local-sub]\naccount = "local"\n'
            '[profiles.local-head]\nresource = "local-sub"\n'
            'adapter = "claude"\nfallback = [[]]\n'
            '[role_defaults]\nnew_card = "local-head"\n',
            "list adapter": '[resources.local-sub]\naccount = "local"\n'
            '[profiles.local-head]\nresource = "local-sub"\nadapter = []\n'
            '[role_defaults]\nnew_card = "local-head"\n',
        }
        for name, canon in broken.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmpdir:
                instance = self.instance(Path(tmpdir), canon)
                owned = str(instance / "heads" / "heads.toml")

                with self.assertRaises(HeadRegistryConfigError) as caught:
                    canonical_heads(upgrade.running_product_root(), instance)
                result = upgrade.step_head_registry(self.context(instance))

                self.assertIn(owned, str(caught.exception))
                self.assertEqual(result.status, "failed")
                self.assertIn(owned, result.detail)
                self.assertFalse(snapshot_path(instance).exists())

    def test_status_reports_a_malformed_installed_snapshot_instead_of_crashing(self):
        """`secretary status` validates the snapshot on its own, so it meets the same shapes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir))
            snapshot_path(instance).write_text(
                "resources:\n  local-sub:\n    account: local\n"
                "profiles:\n  local-head: []\n"
                "role_defaults:\n  new_card: local-head\n",
                encoding="utf-8",
            )

            snapshot = str(snapshot_path(instance))
            with self.assertRaises(HeadRegistryConfigError) as caught:
                installed_heads(instance)
            record = status._head_registry(instance)

            self.assertIn(snapshot, str(caught.exception))
            self.assertIn(snapshot, record["error"])

    def test_the_snapshot_and_pin_name_the_canon_that_actually_won(self):
        for name, canon, origin in (
            ("instance", self.CANON, INSTANCE_ORIGIN),
            ("product", None, PRODUCT_ORIGIN),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmpdir:
                instance = self.instance(Path(tmpdir), canon)
                context = self.context(instance)
                expected, _ = canonical_path(context.product_root, instance)

                upgrade.step_head_registry(context)

                header = snapshot_path(instance).read_text(encoding="utf-8").splitlines()[0]
                pin = read_source(instance)
                self.assertIn(str(expected), header)
                self.assertEqual(pin["canonical"], str(expected))
                self.assertEqual(pin["canonical_owner"], origin)
                self.assertEqual(pin["product_root"], str(context.product_root.resolve()))

    def test_a_snapshot_built_from_the_instance_canon_is_not_stale_against_it(self):
        """Verify compares the snapshot with the canon that made it, not with the product's."""
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir), self.CANON)
            context = self.context(instance)

            upgrade.step_head_registry(context)

            self.assertEqual(
                assert_snapshot_current(instance, context.product_root)["role_defaults"]["new_card"],
                "local-head",
            )

    def context(self, instance: Path) -> upgrade.UpgradeContext:
        return upgrade.UpgradeContext(
            instance_path=instance,
            product_root=upgrade.running_product_root(),
            base_branch="main",
            dry_run=False,
            units=FakeUnitInstaller(),
            orca=FakeRegistrar(),
            automations=None,
            report=_Report(),
        )


def _restore_mode(path: Path, mode: int = 0o644) -> None:
    """Give a deliberately unreadable fixture back its permissions so cleanup can remove it."""
    try:
        path.chmod(mode)
    except OSError:
        pass


class _Report:
    """The slice of an InstanceReport the host and memory steps read."""

    host = {"unit_prefix": UNIT_PREFIX}
    instance = {"host": {"unit_prefix": UNIT_PREFIX}, "data_dir": "/tmp/does-not-matter"}
    data_dir = Path("/tmp/does-not-matter")
    bindings: list = []


if __name__ == "__main__":
    unittest.main()
