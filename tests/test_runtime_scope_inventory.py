"""Disposable host/doctor consumers of the deployed runtime owner format."""

from __future__ import annotations

import json
import runpy
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import cli, status, upgrade
from secretary.host import (
    FixtureHostSource,
    LiveHostSource,
    PlannedResource,
    build_doctor_expectations,
    inventory,
    plan_changes,
)
from secretary.host_apply import ApplyInputs, apply_host
from secretary.infra.systemd import CommandResult
from secretary.runtime.head.local_pty import protocol
from secretary.runtime.head.local_pty import scope_inventory as reader
from secretary.runtime.head.local_pty.journal import RUN_STARTED, JournalWriter
from secretary.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from secretary.runtime.head.memory import scope_unit
from secretary.runtime.local_pty_head import runtime_scope_inventory
from tests.fakes.upgrade import FakeUnitInstaller
from tests.runtime_scope_fixtures import host_fixture


class RuntimeScopeConsumerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = self.root / "data"
        self.data.mkdir()
        self.workspace = self.root / "other-registered-project"
        self.workspace.mkdir()
        (self.root / "instance").mkdir()
        self.instance, self.packaged, self.desired, self.fixture = host_fixture(self.root, self.data)
        self.expected = build_doctor_expectations(self.instance, [], packaged=self.packaged,
                                                 data_dir=self.data)
        self.native = {}
        self.boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.enterContext(mock.patch.object(reader, "CGROUP_ROOT", self.root / "cgroups"))
        self.enterContext(mock.patch.object(reader, "launch_identity", return_value=None))
        self.enterContext(mock.patch.object(reader, "_unit_state", side_effect=lambda unit: dict(
            self.native.get(unit, dict(Id=unit, LoadState="loaded", ActiveState="active", SubState="running",
                                       ControlGroup=f"/system.slice/{unit}", InvocationID="unowned-invocation",
                                       ActiveEnterTimestampMonotonic="1500000", Transient="yes")))))

    def owner(self, role="po", run_id="po-self", directory_root="po-heads", pending=False):
        directory = self.data / directory_root / run_id
        directory.mkdir(parents=True)
        owner = ScopedHeadLifecycle(run_id, 96)
        owner.persist(directory, role=role, task=f"{role}:other-project:operation", workspace=str(self.workspace))
        record = owner.read_owner(directory)
        record.update(launch_pid=99999999, launch_identity=f"{self.boot}:100",
                      launch_allowed=not pending)
        owner.update_owner(directory, record)
        heartbeat = directory / protocol.PID_FILE_NAME
        heartbeat.write_text(json.dumps(dict(version=1, pid=99999998, boot_id=self.boot,
                                            proc_starttime_ticks="200", run_id=run_id,
                                            role=role, task=record["task"])))
        with JournalWriter(directory / protocol.JOURNAL_NAME, run_id).open() as writer:
            writer.append(RUN_STARTED, head_pid=99999998, supervisor_pid=99999997,
                          role=role, task=record["task"], pid_file=str(heartbeat),
                          socket_path=str(directory / protocol.SOCKET_NAME))
        unit = scope_unit(run_id)
        group = reader.CGROUP_ROOT / "system.slice" / unit
        group.mkdir(parents=True)
        (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        (group / "cgroup.procs").write_text("99999998\n")
        child = group / "detached-descendants"
        child.mkdir()
        (child / "cgroup.procs").write_text("99999996\n")
        self.native[unit] = dict(Id=unit, LoadState="loaded", ActiveState="active", SubState="running",
                                 ControlGroup=f"/system.slice/{unit}", InvocationID="original-invocation",
                                 ActiveEnterTimestampMonotonic="1500000", Transient="yes")
        return owner, directory, unit

    def collect(self, *units):
        (self.fixture / "units.txt").write_text("\n".join([r.name for r in self.desired] + list(units)))
        (self.fixture / "unit-states.txt").write_text("\n".join(
            f"{r.name} enabled active" for r in self.desired))
        return FixtureHostSource(self.fixture).collect(self.expected)

    def inputs(self, actual):
        return ApplyInputs(self.instance, [], actual, self.desired, self.data / "host-managed.json", self.packaged)

    def doctor_and_status(self):
        report = SimpleNamespace(instance=self.instance, bindings=[], host=self.instance["host"],
                                 data_dir=self.data, instance_path=self.root / "instance" / "instance.yaml",
                                 name="disposable", projects=[])
        with (
            mock.patch.object(cli, "resolve_installed_packaged", return_value=self.packaged),
            mock.patch.object(status, "resolve_installed_packaged", return_value=self.packaged),
        ):
            _, collected, diffs = cli.collect_host_inventory(report, SimpleNamespace(host_fixture=str(self.fixture)))
            snapshot = status.collect_status(report, host_fixture=str(self.fixture), sprints=False,
                                             recovery={"fixture": True})
        return collected, diffs, snapshot

    def test_all_roles_other_project_and_pending_descendants_preserved_by_both_consumers(self):
        units = [self.owner(role, role, "heads", pending=role == "reviewer")[2]
                 for role in ("worker", "reviewer", "observer", "po")]
        collected = self.collect(*units)
        self.assertFalse(collected.errors)
        self.assertEqual(set(collected.inventory.runtime_scopes.scopes), set(units))
        self.assertFalse(inventory(self.expected, collected.inventory)["units"].unmanaged_on_host)
        self.assertFalse(inventory(self.expected, collected.inventory)["units"].missing_on_host)
        for dry in (True, False):
            installer = FakeUnitInstaller()
            before = {unit: (self.data / "heads" / role / "scope-owner.json").read_bytes()
                      for role, unit in zip(("worker", "reviewer", "observer", "po"), units)}
            applied = apply_host(self.inputs(collected.inventory), units=installer, dry_run=dry)
            self.assertFalse(applied.errors)
            self.assertFalse(applied.conflicts)
            self.assertEqual(applied.preserved_runtime_scopes, sorted(units))
            self.assertTrue(all(name not in units for _, name in installer.calls))
            self.assertTrue(all(change.name not in units for change in applied.changes))
            for role, unit in zip(("worker", "reviewer", "observer", "po"), units):
                self.assertEqual((self.data / "heads" / role / "scope-owner.json").read_bytes(), before[unit])
            manifest = self.data / "host-managed.json"
            if manifest.exists():
                self.assertTrue(all(unit not in manifest.read_text() for unit in units))
        # A new reader has no prior process-local cache. The retained launch,
        # journal and heartbeat still prove the original native incarnation.
        self.assertEqual(set(runtime_scope_inventory(self.data, set(units)).scopes), set(units))

    def test_supported_upgrade_host_step_and_doctor_inventory_preserve_active_self_po(self):
        _, directory, unit = self.owner()
        self.collect(unit)
        report = SimpleNamespace(instance=self.instance, bindings=[], host=self.instance["host"],
                                 data_dir=self.data, instance_path=self.root / "instance" / "instance.yaml")
        for dry in (True, False):
            installer = FakeUnitInstaller()
            context = upgrade.UpgradeContext(instance_path=self.root / "instance", product_root=self.root,
                                              base_branch="main", dry_run=dry, units=installer,
                                              report=report, host_fixture=self.fixture)
            result = upgrade.step_host(context)
            self.assertNotEqual(result.status, "failed", result)
            self.assertTrue(all(name != unit for _, name in installer.calls))
            self.assertNotIn(unit, (self.data / "host-managed.json").read_text() if not dry else "")
        with mock.patch.object(cli, "resolve_installed_packaged", return_value=self.packaged):
            _, collected, diffs = cli.collect_host_inventory(report, SimpleNamespace(host_fixture=str(self.fixture)))
        self.assertFalse(collected.errors)
        self.assertNotIn(unit, diffs["units"].unmanaged_on_host)
        self.assertNotIn(unit, diffs["units"].missing_on_host)
        self.assertFalse(ScopedHeadLifecycle.read_owner(directory)["cleanup_complete"])
        self.assertNotIn(unit, [row["name"] for row in status._units(self.expected, collected, offline=False)])

    def test_ci_self_upgrade_program_exercises_all_consumers_without_scope_effects(self):
        _, directory, unit = self.owner()
        fixture_root = self.root / "ci-program"
        fixture_root.mkdir()
        (fixture_root / "finish").touch()
        program = Path(__file__).parent / "fixtures" / "scope_self_upgrade.py"

        def native_reply(command):
            if command[1] == "list-unit-files":
                return CommandResult(True, 0, "", "")
            if command[1] == "list-units":
                return CommandResult(True, 0, f"{unit} loaded active running Disposable scope\n", "")
            raise AssertionError(f"unexpected native observation: {command}")

        before = (directory / "scope-owner.json").read_bytes()
        with (
            mock.patch.object(sys, "argv", [str(program), str(fixture_root), str(self.data), "po-self"]),
            mock.patch.object(LiveHostSource, "_run", side_effect=native_reply),
        ):
            runpy.run_path(str(program), run_name="__main__")
        proof = json.loads((fixture_root / "proof.json").read_text())
        self.assertEqual(len(proof["results"]), 4)
        self.assertTrue(all(value != "failed" for value in proof["results"]))
        self.assertEqual(proof["results"][-2:], ["unchanged", "unchanged"])
        self.assertTrue(all(name != unit for _, name in proof["effects"]))
        self.assertEqual((directory / "scope-owner.json").read_bytes(), before)

    def test_loaded_scope_enumeration_failure_refuses_upgrade_and_doctor(self):
        _, directory, unit = self.owner()
        report = SimpleNamespace(instance=self.instance, bindings=[], host=self.instance["host"],
                                 data_dir=self.data, instance_path=self.root / "instance" / "instance.yaml")
        before = (directory / "scope-owner.json").read_bytes()

        def native_reply(command):
            if command[1] == "list-unit-files":
                return CommandResult(True, 0, "\n".join(f"{r.name} enabled enabled" for r in self.desired), "")
            if command[1] == "list-units":
                return CommandResult(True, 1, "", "Failed to connect to bus")
            raise AssertionError(f"observation continued after loaded-unit enumeration failed: {command}")

        with mock.patch.object(LiveHostSource, "_run", side_effect=native_reply):
            for dry in (True, False):
                installer = FakeUnitInstaller()
                context = upgrade.UpgradeContext(instance_path=self.root / "instance", product_root=self.root,
                                                  base_branch="main", dry_run=dry, units=installer, report=report)
                result = upgrade.step_host(context)
                self.assertEqual(result.status, "failed")
                self.assertIn("system manager/bus unavailable", result.detail)
                self.assertEqual(installer.calls, [])
            with mock.patch.object(cli, "resolve_installed_packaged", return_value=self.packaged):
                _, collected, _ = cli.collect_host_inventory(report, SimpleNamespace(host_fixture=None))
            self.assertIn("system manager/bus unavailable", collected.errors["units"])
            self.assertEqual(collected.inventory.units, set())
        self.assertEqual((directory / "scope-owner.json").read_bytes(), before)

    def test_foreign_and_missing_owner_remain_conflicts_and_prevent_all_effects(self):
        _, _, owned = self.owner("worker", "previous-1896", "heads")
        unknown = "secretary-head-foreign.scope"
        collected = self.collect(owned, unknown)
        self.assertFalse(collected.errors)
        self.assertEqual(inventory(self.expected, collected.inventory)["units"].unmanaged_on_host, [unknown])
        for dry in (True, False):
            installer = FakeUnitInstaller()
            result = apply_host(self.inputs(collected.inventory), units=installer, dry_run=dry)
            self.assertEqual([change.name for change in result.conflicts], [unknown])
            self.assertEqual(installer.calls, [])
        foreign = replace(self.expected, foreign_units={unknown})
        self.assertFalse(inventory(foreign, collected.inventory)["units"].unmanaged_on_host)

    def test_manifest_never_adopts_or_deletes_even_an_owned_runtime_scope(self):
        _, _, unit = self.owner()
        actual = self.collect(unit).inventory
        resource = PlannedResource("bad-resource", "unit", unit, "{}", "digest")
        for desired, managed in (([resource], []), ([], [resource])):
            changes = plan_changes(desired, actual, managed, "secretary-")
            self.assertEqual([change.action for change in changes], ["conflict"])

    def test_selected_data_root_and_swapped_or_escaping_directories_refused(self):
        _, directory, unit = self.owner()
        other = self.root / "other-data"
        other.mkdir()
        self.assertFalse(runtime_scope_inventory(other, {unit}).scopes)
        original = directory.with_name("saved")
        directory.rename(original)
        directory.symlink_to(original, target_is_directory=True)
        self.assertTrue(self.collect(unit).errors)

    def test_duplicate_malformed_and_unreadable_owner_inventory_is_not_empty_success(self):
        _, directory, unit = self.owner()
        original = (directory / "scope-owner.json").read_bytes()
        for content in (b"not json", b"[]", original.replace(b'"generation":', b'"generation":null,"duplicate":')):
            (directory / "scope-owner.json").write_bytes(content)
            self.assertTrue(self.collect(unit).errors)
        (directory / "scope-owner.json").write_bytes(original)
        with mock.patch.object(reader, "_json", side_effect=PermissionError):
            collected = self.collect(unit)
        self.assertIn("units", collected.errors)
        duplicate = self.data / "heads" / directory.name
        duplicate.mkdir(parents=True)
        (duplicate / "scope-owner.json").write_bytes(original)
        self.assertTrue(self.collect(unit).errors)

    def test_completed_cleanup_claim_and_reused_native_incarnation_are_refused(self):
        owner, directory, unit = self.owner()
        record = owner.read_owner(directory)
        record.update(launch_allowed=False, cleanup_complete=True)
        owner.update_owner(directory, record)
        self.assertTrue(self.collect(unit).errors)
        record["cleanup_complete"] = False
        owner.update_owner(directory, record)
        self.native[unit]["ActiveEnterTimestampMonotonic"] = "3000000"
        self.assertTrue(self.collect(unit).errors)

    def test_generation_and_invocation_changes_between_inventory_and_apply_abort_effects(self):
        owner, directory, unit = self.owner()
        for field in ("generation", "InvocationID"):
            collected = self.collect(unit)
            self.assertFalse(collected.errors)
            if field == "generation":
                record = owner.read_owner(directory)
                record[field] = "replacement-generation"
                owner.update_owner(directory, record)
            else:
                self.native[unit][field] = "replacement-invocation"
            for dry in (True, False):
                installer = FakeUnitInstaller()
                result = apply_host(self.inputs(collected.inventory), units=installer, dry_run=dry)
                self.assertTrue(result.errors)
                self.assertEqual(installer.calls, [])

    def test_observed_disappearance_does_not_claim_missing_packaged_scope(self):
        _, _, unit = self.owner()
        collected = self.collect(unit)
        self.native[unit].update(LoadState="not-found", ActiveState="inactive", ControlGroup="")
        installer = FakeUnitInstaller()
        result = apply_host(self.inputs(collected.inventory), units=installer, dry_run=True)
        self.assertFalse(result.errors)
        self.assertFalse(result.conflicts)
        self.assertEqual(installer.calls, [])

    def test_review14_disappeared_scope_reappears_without_owner_must_refuse(self):
        _, directory, unit = self.owner()
        self.native[unit].update(LoadState="not-found", ActiveState="inactive", ControlGroup="")
        collected = self.collect(unit)
        self.assertFalse(collected.errors)
        self.assertIn(unit, collected.inventory.runtime_scopes.disappeared)
        # The observer requires the raw originally observed set to survive filtering.
        self.assertIn(unit, collected.inventory.units)
        self.assertEqual(collected.inventory.runtime_scopes.observed, frozenset(collected.inventory.units))
        self.assertNotIn(unit, inventory(self.expected, collected.inventory)["units"].unmanaged_on_host)
        (directory / "scope-owner.json").unlink()
        self.native[unit].update(LoadState="loaded", ActiveState="active",
                                ControlGroup=f"/system.slice/{unit}", InvocationID="foreign-replacement")
        for dry in (True, False):
            with self.subTest(dry_run=dry):
                installer = FakeUnitInstaller()
                result = apply_host(self.inputs(collected.inventory), units=installer, dry_run=dry)
                self.assertTrue(result.errors or result.conflicts, f"accepted unowned replacement: {result}")
                self.assertEqual(installer.calls, [])
        fresh = self.collect(unit)
        self.assertIn(unit, inventory(self.expected, fresh.inventory)["units"].unmanaged_on_host)
        doctor, diffs, snapshot = self.doctor_and_status()
        self.assertIn(unit, diffs["units"].unmanaged_on_host)
        self.assertFalse(doctor.inventory.runtime_scopes.scopes)
        self.assertEqual(snapshot["host"]["runtime_scopes"], [])

    def test_ownerless_genuine_disappearance_is_freshly_observed_before_apply(self):
        _, directory, unit = self.owner()
        self.native[unit].update(LoadState="not-found", ActiveState="inactive", ControlGroup="")
        collected = self.collect(unit)
        (directory / "scope-owner.json").unlink()
        for dry in (True, False):
            installer = FakeUnitInstaller()
            result = apply_host(self.inputs(collected.inventory), units=installer, dry_run=dry)
            self.assertFalse(result.errors)
            self.assertFalse(result.conflicts)
            self.assertEqual(installer.calls, [])
        with mock.patch.object(reader, "_unit_state", side_effect=PermissionError):
            result = apply_host(self.inputs(collected.inventory), units=FakeUnitInstaller())
        self.assertTrue(result.errors)
        doctor, diffs, snapshot = self.doctor_and_status()
        self.assertFalse(doctor.errors)
        self.assertNotIn(unit, diffs["units"].unmanaged_on_host)
        self.assertEqual(snapshot["host"]["runtime_scopes"], [])

    def test_review14_forged_nonempty_generation_or_absolute_workspace_must_refuse(self):
        owner, directory, unit = self.owner()
        original = owner.read_owner(directory)
        for key, value in (("generation", "forged-generation"),
                           ("workspace", str(self.root / "forged-workspace"))):
            owner.update_owner(directory, {**original, key: value})
            collected = self.collect(unit)
            doctor, _, snapshot = self.doctor_and_status()
            for dry in (True, False):
                with self.subTest(field=key, dry_run=dry):
                    installer = FakeUnitInstaller()
                    result = apply_host(self.inputs(collected.inventory), units=installer, dry_run=dry)
                    self.assertTrue(collected.errors and result.errors and doctor.errors
                                    and snapshot["host"]["inventory_errors"],
                                    f"accepted changed {key}: {collected.inventory.runtime_scopes}")
                    self.assertEqual(installer.calls, [])
        owner.update_owner(directory, original)

    def test_released_live_scope_arguments_have_no_independent_po_generation(self):
        owner, directory, _ = self.owner()
        supervisor = ["python", "-P", "-m", "secretary.runtime.head.local_pty.supervisor",
                      "--run-dir", str(directory), "--run-id", owner.run_id, "--role", "po",
                      "--task", "po:other-project:operation", "--cwd", str(self.workspace)]
        original = owner.launcher_argv(supervisor, run_dir=directory, log_path=directory / "supervisor.log",
                                       timeout=5, pythonpath="")
        other = ScopedHeadLifecycle(owner.run_id, owner.limit_mib, generation="different-generation")
        changed = other.launcher_argv(supervisor, run_dir=directory, log_path=directory / "supervisor.log",
                                      timeout=5, pythonpath="")
        self.assertNotEqual(original[7], changed[7])
        # launch_until_started gives its --exec-gated child arguments[4:],
        # dropping the outer control process's generation at argv[7].
        self.assertEqual(original[8:], changed[8:])
        self.assertNotIn(owner.generation, original[8:])

    def test_live_generation_substitution_must_refuse(self):
        owner, directory, unit = self.owner()
        original = owner.read_owner(directory)
        supervisor = ["python", "-P", "-m", "secretary.runtime.head.local_pty.supervisor",
                      "--run-dir", str(directory), "--run-id", owner.run_id, "--role", "po",
                      "--task", original["task"], "--cwd", original["workspace"]]
        launch = owner.launcher_argv(supervisor, run_dir=directory, log_path=directory / "supervisor.log",
                                     timeout=5, pythonpath="")
        native_argv = b"\0".join(word.encode() for word in launch[8:]) + b"\0"
        (reader.CGROUP_ROOT / "system.slice" / unit / "cgroup.procs").write_text(str(original["launch_pid"]))
        cmdline = Path(f"/proc/{original['launch_pid']}/cmdline")
        original_read = Path.read_bytes
        with (
            mock.patch.object(reader, "launch_identity", return_value=original["launch_identity"]),
            mock.patch.object(Path, "read_bytes", autospec=True,
                              side_effect=lambda path: native_argv if path == cmdline else original_read(path)),
        ):
            # Native argv binds workspace, but the generation control argument
            # was discarded by the deployed producer before this process began.
            owner.update_owner(directory, {**original, "workspace": str(self.root / "forged-workspace")})
            self.assertTrue(self.collect(unit).errors)
            owner.update_owner(directory, {**original, "generation": "forged-live-generation"})
            collected = self.collect(unit)
            for dry in (True, False):
                with self.subTest(dry_run=dry):
                    installer = FakeUnitInstaller()
                    result = apply_host(self.inputs(collected.inventory), units=installer, dry_run=dry)
                    self.assertTrue(collected.errors and result.errors,
                                    "live argv carries no independent original PO generation")
                    self.assertEqual(installer.calls, [])
        owner.update_owner(directory, original)

    def test_owner_lock_contention_during_cleanup_is_unavailable_and_read_only(self):
        owner, directory, unit = self.owner(pending=True)
        before = (directory / "scope-owner.json").read_bytes()
        with owner.ownership():
            self.assertTrue(self.collect(unit).errors)
        self.assertEqual((directory / "scope-owner.json").read_bytes(), before)

    def test_forged_owner_native_identity_and_damaged_heartbeat_remain_unavailable(self):
        owner, directory, unit = self.owner()
        original = owner.read_owner(directory)
        for field, value in (("run_id", "forged-run"), ("unit", "secretary-head-forged.scope"),
                             ("generation", ""), ("role", "forged-role"), ("task", "forged-task"),
                             ("workspace", "relative"), ("launch_identity", "other-boot:100")):
            record = {**original, field: value}
            owner.update_owner(directory, record)
            self.assertTrue(self.collect(unit).errors, (field, value))
        owner.update_owner(directory, original)
        heartbeat = directory / protocol.PID_FILE_NAME
        original_heartbeat = heartbeat.read_bytes()
        for value in ([], {"version": 1}, {**json.loads(original_heartbeat), "boot_id": "other-boot"}):
            heartbeat.write_text(json.dumps(value))
            self.assertTrue(self.collect(unit).errors)
        heartbeat.write_bytes(original_heartbeat)
        owner_file = directory / "scope-owner.json"
        saved = directory / "saved-owner"
        owner_file.rename(saved)
        owner_file.symlink_to(saved)
        self.assertTrue(self.collect(unit).errors)

    def test_native_replacement_during_observation_and_cgroup_inode_reuse_refused(self):
        _, _, unit = self.owner()
        original = dict(self.native[unit])
        changed = {**original, "InvocationID": "replacement"}
        with mock.patch.object(reader, "_unit_state", side_effect=[original, changed]):
            self.assertTrue(self.collect(unit).errors)
        collected = self.collect(unit)
        group = reader.CGROUP_ROOT / "system.slice" / unit
        group.rename(group.with_name("saved-original"))
        group.mkdir()
        (group / "cgroup.events").write_text("populated 1\n")
        (group / "cgroup.procs").write_text("99999998\n")
        installer = FakeUnitInstaller()
        result = apply_host(self.inputs(collected.inventory), units=installer)
        self.assertTrue(result.errors)
        self.assertEqual(installer.calls, [])

    def test_retained_journal_cannot_lend_an_old_generation_to_a_second_launch(self):
        _, directory, unit = self.owner()
        with JournalWriter(directory / protocol.JOURNAL_NAME, "po-self").open() as writer:
            writer.append(RUN_STARTED, role="po", task="po:other-project:operation", head_pid=99999998,
                          pid_file=str(directory / protocol.PID_FILE_NAME),
                          socket_path=str(directory / protocol.SOCKET_NAME))
        self.assertTrue(self.collect(unit).errors)

    def test_copied_records_from_another_installation_and_root_symlinks_refused(self):
        _, directory, unit = self.owner()
        other = self.root / "other-data"
        copy = other / "po-heads" / directory.name
        copy.mkdir(parents=True)
        for name in ("scope-owner.json", "scope-owner.lock", protocol.JOURNAL_NAME, protocol.PID_FILE_NAME):
            (copy / name).write_bytes((directory / name).read_bytes())
        self.assertTrue(runtime_scope_inventory(other, {unit}).errors)
        alias = self.root / "alias-data"
        alias.symlink_to(self.data)
        self.assertTrue(runtime_scope_inventory(alias, {unit}).errors)

    def test_packaged_deletion_cannot_stop_a_preserved_scope_through_binds_to(self):
        _, _, unit = self.owner()
        self.native[unit]["BindsTo"] = "secretary-memory.service"
        collected = self.collect(unit)
        instance = {**self.instance, "host": {**self.instance["host"], "components": {"memory": {"enabled": False}}}}
        for dry in (True, False):
            installer = FakeUnitInstaller()
            inputs = replace(self.inputs(collected.inventory), instance=instance)
            result = apply_host(inputs, units=installer, dry_run=dry)
            self.assertTrue(result.errors)
            self.assertIn("bound", result.errors[0])
            self.assertEqual(installer.calls, [])

    def test_empty_pending_cleanup_retains_runtime_ownership_without_host_settlement(self):
        owner, directory, unit = self.owner(pending=True)
        group = reader.CGROUP_ROOT / "system.slice" / unit
        (group / "cgroup.events").write_text("populated 0\n")
        collected = self.collect(unit)
        self.assertFalse(collected.errors)
        scope = collected.inventory.runtime_scopes.scopes[unit]
        self.assertFalse(scope["populated"])
        self.assertFalse(scope["cleanup_complete"])
        self.assertFalse(scope["launch_allowed"])
        result = apply_host(self.inputs(collected.inventory), units=FakeUnitInstaller())
        self.assertFalse(result.errors)
        self.assertFalse(owner.read_owner(directory)["cleanup_complete"])

    def test_lifecycle_completed_empty_scope_is_preserved_until_native_disappearance(self):
        owner, directory, unit = self.owner(pending=True)
        group = reader.CGROUP_ROOT / "system.slice" / unit
        (group / "cgroup.events").write_text("populated 0\n")
        record = owner.read_owner(directory)
        record["cleanup_complete"] = True
        owner.update_owner(directory, record)
        collected = self.collect(unit)
        self.assertFalse(collected.errors)
        scope = collected.inventory.runtime_scopes.scopes[unit]
        self.assertTrue(scope["cleanup_complete"])
        self.assertFalse(scope["populated"])
        self.assertFalse(inventory(self.expected, collected.inventory)["units"].unmanaged_on_host)
        installer = FakeUnitInstaller()
        applied = apply_host(self.inputs(collected.inventory), units=installer)
        self.assertFalse(applied.errors)
        self.assertTrue(all(name != unit for _, name in installer.calls))
