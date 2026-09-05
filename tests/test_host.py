from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from secretary import cli, host_commands
from secretary.cli import main
from secretary.config import validate_instance
from secretary.host import (
    SHIPPED_PACKAGING_ROOT,
    CollectResult,
    Expectations,
    FixtureHostSource,
    HostInventory,
    LiveHostSource,
    SystemdLayout,
    build_doctor_expectations,
    build_expectations,
    build_plan,
    inventory,
    load_managed_manifest,
    load_packaged_units,
    manifest_text,
    plan_changes,
    plan_input_errors,
)
from secretary.host import (
    _CmdResult as CmdResult,
)
from secretary.host_apply import resolve_packaged, resolve_systemd_layout
from secretary.projects.availability import ProjectAvailability
from tests.orca_fixtures import legacy_orca_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]
# The units this checkout ships. A plan or a doctor run reads the checkout its host is configured
# with, so a test about the shipped catalogue has to name it rather than let a home default decide.
SHIPPED_UNITS = load_packaged_units(SHIPPED_PACKAGING_ROOT, "secretary-")
EXAMPLE_INSTANCE = REPO_ROOT / "examples" / "instance"
HOST_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "host"


_UNSET = object()


def run_cli(argv: list[str], *, orca_executable: Path | object = _UNSET) -> tuple[int, str]:
    """Run the CLI, relying on the suite-wide hermetic Orca default.

    Pass ``orca_executable`` only to model a deliberately alternate or
    unavailable executable; the default leaves the suite's fixture patch
    (tests/__init__.py) in place instead of shadowing it with the same value.
    """
    output = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(contextlib.redirect_stdout(output))
        if orca_executable is not _UNSET:
            stack.enter_context(
                unittest.mock.patch("secretary.host_apply.find_orca_executable", return_value=orca_executable)
            )
        code = main(argv)
    return code, output.getvalue()


def snapshot(root: Path) -> dict[str, tuple[float, int]]:
    """Map every file under ``root`` to its mtime and size, to detect writes."""
    result: dict[str, tuple[float, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            result[str(path.relative_to(root))] = (stat.st_mtime, stat.st_size)
    return result


class ExpectationTests(unittest.TestCase):
    def test_doctor_uses_exact_checkout_paths_and_canonical_resources(self):
        instance = {
            "data_dir": "/var/lib/secretary-data",
            "host": {"projects_root": "/srv/projects", "unit_prefix": "secretary-"},
        }
        bindings = [
            {"id": "outside", "repo": "/opt/checkouts/widget", "enabled": True, "orca_binding": "widget"},
        ]
        expected = build_doctor_expectations(
            instance,
            bindings,
            data_dir=Path("/var/lib/secretary-data"),
        )
        self.assertEqual(expected.projects, {"/opt/checkouts/widget"})
        self.assertIn("secretary-dispatcher-production.timer", expected.units)
        self.assertEqual(expected.orca_repos, {"widget"})
        self.assertEqual(
            expected.lazy_orca_repos,
            {"observers": "/var/lib/secretary-data/dispatcher/observer-root/observers"},
        )

    def test_observer_root_is_lazily_managed_only_with_production_dispatcher(self):
        enabled = {
            "data_dir": "/srv/secretary-data",
            "host": {"unit_prefix": "secretary-"},
        }
        expected = build_doctor_expectations(
            enabled,
            [],
            packaged=[],
            data_dir=Path("/srv/secretary-data"),
        )
        empty = inventory(expected, HostInventory())
        self.assertEqual(empty["orca repos"].missing_on_host, [])
        self.assertEqual(empty["orca repos"].unmanaged_on_host, [])

        repo = "/srv/secretary-data/dispatcher/observer-root/observers"
        present = inventory(
            expected,
            HostInventory(orca_repos={"observers"}, orca_repo_paths={"observers": repo}),
        )
        self.assertEqual(present["orca repos"].matched, ["observers"])
        self.assertEqual(present["orca repos"].unmanaged_on_host, [])

        disabled = build_doctor_expectations(
            {
                "data_dir": "/srv/secretary-data",
                "host": {
                    "unit_prefix": "secretary-",
                    "components": {"dispatcher-production": {"enabled": False}},
                },
            },
            [],
            packaged=[],
        )
        self.assertEqual(disabled.lazy_orca_repos, {})
        self.assertNotIn("secretary-dispatcher-production.timer", disabled.units)

    def test_observer_root_does_not_change_reconcile_plan(self):
        instance = {"data_dir": "/srv/secretary-data", "host": {"unit_prefix": "secretary-"}}
        desired = build_plan(instance, [], packaged=[])
        changes = plan_changes(
            desired,
            HostInventory(
                units={resource.name for resource in desired if resource.kind == "unit"},
                orca_repos={"observers"},
                orca_repo_paths={"observers": "/srv/secretary-data/dispatcher/observer-root/observers"},
            ),
            desired,
            "secretary-",
        )
        self.assertTrue(changes)
        self.assertTrue(all(change.action == "unchanged" for change in changes))
        self.assertNotIn("observers", {change.name for change in changes})

    def test_observer_root_name_at_another_path_stays_unmanaged(self):
        expected = build_doctor_expectations(
            {"data_dir": "/srv/secretary-data", "host": {"unit_prefix": "secretary-"}},
            [],
            packaged=[],
            data_dir=Path("/srv/secretary-data"),
        )
        diff = inventory(
            expected,
            HostInventory(
                orca_repos={"observers", "someone-else"},
                orca_repo_paths={
                    "observers": "/srv/not-secretary/observers",
                    "someone-else": "/srv/someone-else",
                },
            ),
        )["orca repos"]
        self.assertEqual(diff.matched, [])
        self.assertEqual(diff.missing_on_host, [])
        self.assertEqual(diff.unmanaged_on_host, ["observers", "someone-else"])

    def test_doctor_checks_relative_checkout_path(self):
        repo = "missing-relative-doctor-checkout"
        expected = build_doctor_expectations(
            {"host": {"projects_root": "/srv/projects", "unit_prefix": "secretary-"}},
            [{"id": "relative", "repo": repo, "enabled": True, "orca_binding": "relative"}],
        )

        self.assertEqual(expected.projects, {str(Path(repo).resolve(strict=False))})
        self.assertEqual(
            inventory(expected, HostInventory())["projects"].missing_on_host,
            [str(Path(repo).resolve(strict=False))],
        )

    def test_doctor_runtime_expectations_distinguish_service_and_timer(self):
        expected = build_doctor_expectations(
            {"host": {"unit_prefix": "secretary-"}}, [], packaged=SHIPPED_UNITS
        )
        self.assertEqual(expected.unit_runtime["secretary-memory.service"], (True, True))
        self.assertEqual(expected.unit_runtime["secretary-curator.timer"], (True, True))
        # A oneshot unit fired by its timer has no [Install] section and is only briefly active
        # around the run, so neither is required. It still gets an entry: without one the live
        # collector never probes it, and a completed run would read to status/doctor as an
        # unprobed unit instead of the truthful, if transient, state it actually has.
        self.assertEqual(expected.unit_runtime["secretary-curator.service"], (False, False))

    def test_project_name_from_repo_path(self):
        exp = build_expectations([{"id": "an-id", "repo": "/srv/projects/on-disk-name"}], {})
        # The host-facing name comes from the repo directory, not the id.
        self.assertEqual(exp.projects, {"on-disk-name"})

    def test_project_name_falls_back_to_id(self):
        exp = build_expectations([{"id": "an-id", "repo": "an-id"}], {})
        self.assertEqual(exp.projects, {"an-id"})

    def test_git_suffix_stripped(self):
        exp = build_expectations([{"id": "x", "repo": "git@example.invalid:acme/widget.git"}], {})
        self.assertEqual(exp.projects, {"widget"})

    def test_host_block_feeds_units_and_repos(self):
        exp = build_expectations(
            [],
            {"units": ["u-a", "u-b"], "orca_repos": ["r-a"], "unit_prefix": "u-"},
        )
        self.assertEqual(exp.units, {"u-a", "u-b"})
        self.assertEqual(exp.orca_repos, {"r-a"})
        self.assertEqual(exp.unit_prefix, "u-")

    def test_diff_partitions_names(self):
        expected = Expectations(projects={"a", "b"}, units={"u"}, orca_repos={"r1", "r2"})
        actual = HostInventory(projects={"b", "c"}, units={"u"}, orca_repos={"r1", "r3"})
        result = inventory(expected, actual)
        self.assertEqual(result["projects"].matched, ["b"])
        self.assertEqual(result["projects"].missing_on_host, ["a"])
        self.assertEqual(result["projects"].unmanaged_on_host, ["c"])
        self.assertEqual(result["units"].matched, ["u"])
        self.assertEqual(result["orca repos"].missing_on_host, ["r2"])
        self.assertEqual(result["orca repos"].unmanaged_on_host, ["r3"])

    def test_foreign_unit_is_not_an_unmanaged_conflict(self):
        expected = Expectations(units={"secretary-memory.service"}, foreign_units={"secretary-other.service"})
        result = inventory(
            expected, HostInventory(units={"secretary-memory.service", "secretary-other.service"})
        )
        self.assertEqual(result["units"].unmanaged_on_host, [])

    def test_foreign_shipped_unit_is_outside_desired_doctor_and_reconcile_parity(self):
        owned = build_plan({"host": {"unit_prefix": "secretary-"}}, [], packaged=SHIPPED_UNITS)
        memory = next(resource for resource in owned if resource.name == "secretary-memory.service")
        instance = {
            "host": {
                "unit_prefix": "secretary-",
                "foreign_units": ["secretary-memory.service"],
            }
        }

        desired = build_plan(instance, [], packaged=SHIPPED_UNITS)
        expected = build_doctor_expectations(instance, [], packaged=SHIPPED_UNITS)
        diff = inventory(
            expected,
            HostInventory(units={resource.name for resource in desired} | {"secretary-memory.service"}),
        )
        changes = plan_changes(
            desired,
            HostInventory(units={"secretary-memory.service"}),
            [memory],
            "secretary-",
            {"secretary-memory.service"},
        )

        self.assertNotIn("secretary-memory.service", expected.units)
        self.assertNotIn("secretary-memory.service", expected.unit_runtime)
        self.assertNotIn("secretary-memory.service", {resource.name for resource in desired})
        self.assertNotIn("secretary-memory.service", diff["units"].matched)
        self.assertEqual(diff["units"].missing_on_host, [])
        self.assertEqual(diff["units"].unmanaged_on_host, [])
        self.assertNotIn("secretary-memory.service", {change.name for change in changes})


class FixtureSourceTests(unittest.TestCase):
    def test_collect_reads_project_paths(self):
        source = FixtureHostSource(HOST_FIXTURE)
        result = source.collect(Expectations())
        self.assertEqual(result.errors, {})
        actual = result.inventory
        self.assertEqual(actual.projects, {"/srv/projects/example-project", "/srv/projects/stray-project"})
        # Full unit file names, exactly as systemctl list-unit-files prints them.
        self.assertEqual(actual.units, {"secretary-pipeline.service", "secretary-retro.timer"})
        self.assertEqual(actual.orca_repos, {"example-project", "secretary", "extra-repo"})

    def test_legacy_project_directories_keep_fixture_paths(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "projects" / "same-name"
            checkout.mkdir(parents=True)
            result = FixtureHostSource(root).collect(Expectations(projects={"/outside/same-name"}))
        self.assertEqual(result.inventory.projects, {str(checkout)})

    def test_missing_per_kind_files_yield_empty_sets(self):
        # An existing root with no unit/repo files and no projects dir is a
        # deliberately empty host, not an inspection failure.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = FixtureHostSource(Path(tmp)).collect(Expectations())
        self.assertEqual(result.errors, {})
        self.assertEqual(result.inventory.projects, set())
        self.assertEqual(result.inventory.units, set())
        self.assertEqual(result.inventory.orca_repos, set())

    def test_missing_root_is_unavailable_not_empty(self):
        # A root that does not exist was never read: every kind must be marked
        # unavailable instead of reporting an empty host (the fixture fail-open).
        source = FixtureHostSource(REPO_ROOT / "tests" / "fixtures" / "does-not-exist")
        result = source.collect(Expectations())
        self.assertEqual(set(result.errors), {"projects", "units", "orca repos"})

    def test_invalid_utf8_marks_only_that_fixture_kind_unavailable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "units.txt"
            path.write_bytes(b"\xff")
            result = FixtureHostSource(Path(tmp)).collect(Expectations())
        self.assertEqual(result.errors, {"units": "fixture host file is not valid UTF-8"})


class ManagedManifestReadTests(unittest.TestCase):
    def test_missing_manifest_is_distinct_from_a_permission_denied_read(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "host-managed.json"
            self.assertEqual(load_managed_manifest(missing), ([], ""))

            with unittest.mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                self.assertEqual(load_managed_manifest(missing), ([], "managed manifest is unreadable"))


class ReconcilePlanTests(unittest.TestCase):
    def test_resolve_packaged_never_yields_an_orca_component(self):
        """Orca runs external to Secretary (secretary-739/755): the product ships no
        ``secretary-orca.*`` unit, so ``resolve_packaged`` cannot depend on an Orca
        executable being installed and must not raise over one being unavailable.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            instance.mkdir()
            with unittest.mock.patch("secretary.host_apply.find_orca_executable", return_value=None):
                packaged = resolve_packaged(
                    {"data_dir": str(root / "data"), "host": {"unit_prefix": "secretary-"}},
                    instance_path=instance,
                    data_dir=root / "data",
                )

        self.assertNotIn("orca", {unit.component for unit in packaged})

    def test_relative_direct_config_path_renders_canonical_absolute_layout(self):
        import tempfile

        # The host under test runs this checkout, and says so the way a real one does: the
        # rendered units come from the product an installation is configured with, never from
        # whichever copy of the code is executing the command.
        with (
            tempfile.TemporaryDirectory() as tmp,
            contextlib.chdir(tmp),
            unittest.mock.patch.dict(os.environ, {"TA_SECRETARY_REPO": str(REPO_ROOT)}),
        ):
            root = Path(tmp)
            instance = root / "instance"
            instance.mkdir()
            data_dir = instance / "relative-data"
            config = instance / "instance.yaml"
            config.write_text(
                "version: 1\nname: operator\ndata_dir: relative-data"
                + "\noffsite:\n  instance_remote: git@example.invalid:x/y\nhost:\n  unit_prefix: secretary-\n",
                encoding="utf-8",
            )
            product_root = root / "product"
            account = SimpleNamespace(pw_dir="/srv/operator")
            with (
                unittest.mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
                unittest.mock.patch(
                    "secretary.host_apply.find_orca_executable", return_value=Path("/usr/local/bin/orca")
                ),
                unittest.mock.patch("secretary.host_apply._is_executable", return_value=True),
            ):
                directory_report = validate_instance(Path("instance"))
                relative_report = validate_instance(Path("instance/instance.yaml"))
                absolute_report = validate_instance(config)
                self.assertTrue(directory_report.ok, directory_report.errors)
                self.assertTrue(relative_report.ok, relative_report.errors)
                self.assertTrue(absolute_report.ok, absolute_report.errors)
                directory = resolve_packaged(
                    directory_report.instance,
                    product_root=Path("product"),
                    instance_path=directory_report.instance_path.parent,
                    data_dir=directory_report.data_dir,
                    runtime_user="operator",
                )
                relative = resolve_packaged(
                    relative_report.instance,
                    product_root=Path("product"),
                    instance_path=relative_report.instance_path.parent,
                    data_dir=relative_report.data_dir,
                    runtime_user="operator",
                )
                absolute = resolve_packaged(
                    absolute_report.instance,
                    product_root=product_root,
                    instance_path=absolute_report.instance_path.parent,
                    data_dir=absolute_report.data_dir,
                    runtime_user="operator",
                )
                layout = resolve_systemd_layout(
                    relative_report.instance,
                    product_root=Path("product"),
                    instance_path=relative_report.instance_path.parent,
                    data_dir=relative_report.data_dir,
                    runtime_user="operator",
                )

        self.assertEqual(
            [(unit.name, unit.content, unit.digest) for unit in directory],
            [(unit.name, unit.content, unit.digest) for unit in relative],
        )
        self.assertEqual(
            [(unit.name, unit.content, unit.digest) for unit in relative],
            [(unit.name, unit.content, unit.digest) for unit in absolute],
        )
        self.assertEqual(layout.product_root, product_root)
        self.assertEqual(layout.instance_path, instance)
        self.assertEqual(layout.data_dir, data_dir)
        rendered = b"\n".join(unit.content for unit in relative)
        self.assertIn(str(product_root).encode(), rendered)
        self.assertIn(str(instance).encode(), rendered)
        self.assertNotIn(b"EnvironmentFile=instance/runtime.env", rendered)
        self.assertIn(f"EnvironmentFile=-{instance}/runtime.env".encode(), rendered)

    def test_plan_keeps_materialized_owner_layout_when_process_user_differs(self):
        import tempfile

        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch.dict(os.environ, {"USER": "root", "TA_SECRETARY_REPO": str(REPO_ROOT)}),
        ):
            root = Path(tmp)
            instance_path = root / "instance"
            instance_path.mkdir()
            (instance_path / "instance.yaml").write_text(
                "version: 1\nname: operator\ndata_dir: "
                + str(root / "data")
                + "\noffsite:\n  instance_remote: git@example.invalid:x/y\nhost:\n  unit_prefix: secretary-\n",
                encoding="utf-8",
            )
            report_instance = {
                "data_dir": str(root / "data"),
                "host": {"unit_prefix": "secretary-"},
            }
            account = SimpleNamespace(pw_name="operator", pw_dir="/srv/operator")
            with (
                unittest.mock.patch("secretary.host_apply.pwd.getpwuid", return_value=account),
                unittest.mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
                unittest.mock.patch(
                    "secretary.host_apply.find_orca_executable", return_value=Path("/usr/local/bin/orca")
                ),
                unittest.mock.patch("secretary.host_apply._is_executable", return_value=True),
            ):
                packaged = resolve_packaged(
                    report_instance,
                    instance_path=instance_path,
                    data_dir=root / "data",
                )
                desired = build_plan(report_instance, [], packaged=packaged)
                self.assertIn(
                    b"User=operator",
                    next(unit.content for unit in packaged if unit.name == "secretary-memory.service"),
                )
                fixture = root / "host"
                fixture.mkdir()
                (fixture / "units.txt").write_text(
                    "\n".join(resource.name for resource in desired if resource.kind == "unit") + "\n",
                    encoding="utf-8",
                )
                manifest = root / "managed.json"
                manifest.write_text(manifest_text(desired), encoding="utf-8")
                code, output = run_cli(
                    [
                        # Commands also accept the config file itself. The resolved
                        # checkout, rather than that file path or this process's
                        # user, defines the rendered unit layout.
                        "reconcile",
                        "plan",
                        "--instance",
                        str(instance_path / "instance.yaml"),
                        "--host-fixture",
                        str(fixture),
                        "--managed-manifest",
                        str(manifest),
                    ]
                )

                # Apply has its own command boundary. It must compile the same
                # bytes before it decides whether the manifest has drifted.
                apply_code, apply_output = run_cli(
                    [
                        "reconcile",
                        "apply",
                        "--dry-run",
                        "--instance",
                        str(instance_path / "instance.yaml"),
                        "--host-fixture",
                        str(fixture),
                        "--managed-manifest",
                        str(manifest),
                    ]
                )

        self.assertEqual(code, 0, output)
        self.assertIn("unchanged systemd:unit:secretary-memory.service", output)
        self.assertEqual(apply_code, 0, apply_output)
        self.assertNotIn("update systemd:unit:secretary-memory.service", apply_output)
        self.assertIn("already reconciled", apply_output)

    def test_cli_plan_uses_live_source_by_default(self):
        class FakeLiveHost:
            def collect(self, expected):
                return CollectResult(HostInventory(), {})

        with unittest.mock.patch.object(
            host_commands, "LiveHostSource", return_value=FakeLiveHost()
        ) as source:
            code, output = run_cli(["reconcile", "plan", "--instance", str(EXAMPLE_INSTANCE)])
        self.assertEqual(code, 0, output)
        source.assert_called_once_with()

    def test_cli_plan_reports_each_unavailable_live_kind(self):
        class FakeLiveHost:
            def collect(self, expected):
                return CollectResult(
                    HostInventory(),
                    {"units": "systemctl not found", "orca repos": "orca not found"},
                )

        with unittest.mock.patch.object(host_commands, "LiveHostSource", return_value=FakeLiveHost()):
            code, output = run_cli(["reconcile", "plan", "--instance", str(EXAMPLE_INSTANCE)])
        self.assertEqual(code, 2, output)
        self.assertIn("units: unavailable: systemctl not found", output)
        self.assertIn("orca repos: unavailable: orca not found", output)

    def test_cli_plan_reports_an_unreadable_managed_manifest(self):
        class FakeLiveHost:
            def collect(self, expected):
                return CollectResult(HostInventory(), {})

        with (
            unittest.mock.patch.object(host_commands, "LiveHostSource", return_value=FakeLiveHost()),
            unittest.mock.patch.object(
                host_commands,
                "load_managed_manifest",
                return_value=([], "managed manifest is unreadable"),
            ),
        ):
            code, output = run_cli(["reconcile", "plan", "--instance", str(EXAMPLE_INSTANCE)])
        self.assertEqual(code, 2, output)
        self.assertIn("managed manifest is unreadable", output)

    def test_production_host_drift_reports_an_unreadable_managed_manifest(self):
        report = SimpleNamespace(
            host={"unit_prefix": "secretary-"},
            instance={},
            bindings=[],
            instance_path=Path("/tmp/secretary-instance.yaml"),
            data_dir=Path("/tmp/secretary-data"),
        )
        collected = CollectResult(HostInventory(), {})
        with (
            unittest.mock.patch.object(cli, "resolve_installed_packaged", return_value=[]),
            unittest.mock.patch.object(
                cli,
                "load_managed_manifest",
                return_value=([], "managed manifest is unreadable"),
            ),
        ):
            findings = cli._production_host_findings(report, report.data_dir, collected)
        self.assertEqual(
            findings, ["production dispatcher managed manifest unavailable: managed manifest is unreadable"]
        )

    def test_live_plan_does_not_write_instance_or_managed_manifest(self):
        import tempfile

        class FakeLiveHost:
            def collect(self, expected):
                return CollectResult(HostInventory(units={"secretary-worker.service"}), {})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance.yaml"
            data_dir = root / "data"
            data_dir.mkdir()
            instance.write_text(
                "version: 1\nname: plan\ndata_dir: "
                + str(data_dir)
                + "\noffsite:\n  instance_remote: git@example.invalid:x/y\nhost:\n"
                "  unit_prefix: secretary-\nheads:\n  - role: worker\n    model: test\n",
                encoding="utf-8",
            )
            manifest = data_dir / "host-managed.json"
            manifest.write_text('{"resources": []}', encoding="utf-8")
            before = snapshot(root)
            with unittest.mock.patch.object(host_commands, "LiveHostSource", return_value=FakeLiveHost()):
                code, output = run_cli(["reconcile", "plan", "--instance", str(instance)])
            self.assertEqual(code, 1, output)
            self.assertEqual(snapshot(root), before)

    def test_cli_plan_offline_cannot_plan_and_is_incompatible_with_fixture(self):
        code, output = run_cli(["reconcile", "plan", "--instance", str(EXAMPLE_INSTANCE), "--offline"])
        self.assertEqual(code, 2, output)
        self.assertIn("--offline cannot produce a plan", output)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = main(
                [
                    "reconcile",
                    "plan",
                    "--instance",
                    str(EXAMPLE_INSTANCE),
                    "--offline",
                    "--host-fixture",
                    str(HOST_FIXTURE),
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "usage")

    def test_runtime_payload_changes_require_an_update(self):
        instance = {"host": {"unit_prefix": "secretary-"}, "heads": [{"role": "worker", "model": "old"}]}
        bindings = [
            {"id": "project-id", "repo": "/srv/old-path", "orca_binding": "project_id", "enabled": True}
        ]
        original = build_plan(instance, bindings, packaged=[])
        actual = HostInventory(
            units={
                "secretary-worker.service",
                "secretary-dispatcher-production.service",
                "secretary-dispatcher-production.timer",
            },
            orca_repos={"project_id"},
        )
        instance["heads"][0]["model"] = "new"
        bindings[0]["repo"] = "/srv/new-path"
        changed = build_plan(instance, bindings, packaged=[])
        changes = [
            change for change in plan_changes(changed, actual, original) if change.action != "unchanged"
        ]
        self.assertEqual({change.action for change in changes}, {"update"})

    def test_plan_rejects_enabled_binding_without_explicit_orca_binding(self):
        errors = plan_input_errors({}, [{"id": "foo-bar", "repo": "/srv/foo_bar", "enabled": True}])
        self.assertEqual(errors, ["enabled binding requires explicit orca_binding"])

    def test_disabled_inventory_binding_can_own_orca_registration(self):
        bindings = [
            {
                "id": "inventory-project",
                "repo": "/srv/inventory-project",
                "orca_binding": "inventory-project",
                "enabled": False,
            }
        ]
        desired = build_plan({}, bindings, packaged=[])
        self.assertEqual(
            [(resource.logical_id, resource.name) for resource in desired],
            [("orca:project:inventory-project", "inventory-project")],
        )

    def test_plan_rejects_heads_without_unit_prefix(self):
        errors = plan_input_errors({"heads": [{"role": "worker", "model": "test"}]}, [])
        self.assertEqual(errors, ["host.unit_prefix is required when heads are configured"])

    def test_plan_rejects_duplicate_logical_id_and_host_name(self):
        duplicate_heads = {
            "host": {"unit_prefix": "secretary-"},
            "heads": [
                {"role": "worker", "model": "one"},
                {"role": "worker", "model": "two"},
            ],
        }
        self.assertIn(
            "duplicate desired logical_id: systemd:head:worker", plan_input_errors(duplicate_heads, [])
        )
        bindings = [
            {"id": "alpha", "repo": "/srv/a", "orca_binding": "shared", "enabled": True},
            {"id": "beta", "repo": "/srv/b", "orca_binding": "shared", "enabled": True},
        ]
        self.assertIn("duplicate desired resource name: orca shared", plan_input_errors({}, bindings))

    def test_renamed_managed_resource_is_deleted_alongside_create(self):
        old_instance = {"host": {"unit_prefix": "old-"}, "heads": [{"role": "worker", "model": "test"}]}
        new_instance = {"host": {"unit_prefix": "new-"}, "heads": [{"role": "worker", "model": "test"}]}
        managed = [
            resource
            for resource in build_plan(old_instance, [])
            if resource.logical_id == "systemd:head:worker"
        ]
        desired = [
            resource
            for resource in build_plan(new_instance, [])
            if resource.logical_id == "systemd:head:worker"
        ]
        actual = HostInventory(units={"old-worker.service"})
        changes = plan_changes(desired, actual, managed, "new-")
        self.assertEqual(
            [(change.action, change.name) for change in changes],
            [("create", "new-worker.service"), ("delete", "old-worker.service")],
        )

    def test_unavailable_desired_project_preserves_its_registered_managed_resource(self):
        binding = {
            "id": "alpha",
            "repo": "/srv/alpha",
            "orca_binding": "alpha-repo",
            "enabled": True,
        }
        managed = build_plan({}, [binding], packaged=[])
        actual = HostInventory(orca_repos={"alpha-repo"})
        unavailable = ProjectAvailability(frozenset({"alpha"}))

        changes = plan_changes(
            managed,
            actual,
            managed,
            project_availability=unavailable,
        )

        self.assertEqual(
            [(change.logical_id, change.action) for change in changes],
            [("orca:project:alpha", "unchanged")],
        )

    def test_unavailable_desired_project_defers_replacement_without_deleting_old_registration(self):
        old = {
            "id": "alpha",
            "repo": "/srv/alpha",
            "orca_binding": "alpha-repo",
            "enabled": True,
        }
        changed = {**old, "repo": "/srv/recovered-alpha", "orca_binding": "recovered-alpha"}

        changes = plan_changes(
            build_plan({}, [changed], packaged=[]),
            HostInventory(orca_repos={"alpha-repo"}),
            build_plan({}, [old], packaged=[]),
            project_availability=ProjectAvailability(frozenset({"alpha"})),
        )

        self.assertEqual(
            [(change.name, change.action) for change in changes],
            [("recovered-alpha", "deferred")],
        )

    def test_unavailable_present_project_reports_same_name_drift_as_deferred(self):
        old = {
            "id": "alpha",
            "repo": "/srv/alpha",
            "orca_binding": "alpha-repo",
            "enabled": True,
        }
        changed = {**old, "repo": "/srv/recovered-alpha"}

        changes = plan_changes(
            build_plan({}, [changed], packaged=[]),
            HostInventory(orca_repos={"alpha-repo"}),
            build_plan({}, [old], packaged=[]),
            project_availability=ProjectAvailability(frozenset({"alpha"})),
        )

        self.assertEqual(
            [(change.logical_id, change.action) for change in changes],
            [("orca:project:alpha", "deferred")],
        )

    def test_plan_is_stable_and_name_match_without_manifest_is_conflict(self):
        instance = {
            "host": {"unit_prefix": "secretary-"},
            "heads": [{"role": "worker", "model": "test"}],
        }
        bindings = [
            {"id": "project-id", "repo": "/srv/project_id", "orca_binding": "project_id", "enabled": True}
        ]
        desired = build_plan(instance, bindings, packaged=[])
        self.assertEqual(
            [resource.name for resource in desired],
            [
                "project_id",
                "secretary-dispatcher-production.service",
                "secretary-dispatcher-production.timer",
                "secretary-worker.service",
            ],
        )
        actual = HostInventory(
            units={
                "secretary-worker.service",
                "secretary-dispatcher-production.service",
                "secretary-dispatcher-production.timer",
            },
            orca_repos={"project_id"},
        )
        first = plan_changes(desired, actual, [])
        second = plan_changes(desired, actual, [])
        self.assertEqual(first, second)
        self.assertEqual({change.action for change in first}, {"conflict"})

    def test_production_dispatcher_units_carry_runtime_bindings(self):
        resources = build_plan({"host": {"unit_prefix": "secretary-"}}, [])
        by_id = {resource.logical_id: resource for resource in resources}

        service = json.loads(by_id["systemd:dispatcher:production.service"].spec)
        timer = json.loads(by_id["systemd:dispatcher:production.timer"].spec)

        self.assertEqual(
            by_id["systemd:dispatcher:production.service"].name, "secretary-dispatcher-production.service"
        )
        self.assertEqual(
            by_id["systemd:dispatcher:production.timer"].name, "secretary-dispatcher-production.timer"
        )
        self.assertEqual(service["managed_by"], "secretary")
        self.assertIn("production-tick", service["runtime"])
        self.assertNotIn("KANBOARD_API_TOKEN", service["env"])
        self.assertIn("SECRETARY_INSTANCE", service["env"])
        self.assertEqual(timer["service"], "secretary-dispatcher-production.service")

    def test_production_dispatcher_unit_sets_path_for_orca_lookup(self):
        units = load_packaged_units(
            REPO_ROOT / "packaging" / "systemd",
            "secretary-",
            SystemdLayout(
                REPO_ROOT, Path("/srv/instance"), Path("/srv/data"), "operator", Path("/home/operator")
            ),
        )
        unit = next(unit for unit in units if unit.name == "secretary-dispatcher-production.service")
        lines = unit.content.decode("utf-8").splitlines()
        path_lines = [line for line in lines if line.startswith("Environment=PATH=")]
        self.assertEqual(len(path_lines), 1)
        path_value = path_lines[0].split("=", 2)[2]
        self.assertIn("/home/operator/.local/bin", path_value.split(":"))
        for standard_dir in ("/usr/local/bin", "/usr/bin", "/bin"):
            self.assertIn(standard_dir, path_value.split(":"))

    def test_memory_unit_uses_persistent_cache_and_configured_thread_limit(self):
        units = load_packaged_units(
            REPO_ROOT / "packaging" / "systemd",
            "secretary-",
            SystemdLayout(
                REPO_ROOT,
                Path("/srv/instance"),
                Path("/srv/data"),
                "operator",
                Path("/home/operator"),
                memory_model="test-model",
                memory_dim=4,
                memory_threads=2,
            ),
        )
        unit = next(unit for unit in units if unit.name == "secretary-memory.service").content
        self.assertIn(b"Environment=MEMORY_CACHE_DIR=/srv/data/memory/fastembed-cache", unit)
        self.assertIn(b"Environment=MEMORY_MODEL=test-model", unit)
        self.assertIn(b"Environment=MEMORY_DIM=4", unit)
        self.assertIn(b"Environment=MEMORY_THREADS=2", unit)

    def test_scheduler_units_order_after_the_external_orca_runtime_without_starting_it(self):
        units = load_packaged_units(
            REPO_ROOT / "packaging" / "systemd",
            "secretary-",
            SystemdLayout(
                REPO_ROOT, Path("/srv/instance"), Path("/srv/data"), "operator", Path("/home/operator")
            ),
        )
        scheduler_services = {
            "secretary-curator.service",
            "secretary-dispatcher-production.service",
            "secretary-retro.service",
            "secretary-steward.service",
            "secretary-steward-deep-sweep.service",
        }

        rendered = {unit.name: unit.content for unit in units}
        self.assertTrue(scheduler_services <= rendered.keys())
        for name in scheduler_services:
            content = rendered[name]
            self.assertIn(b"After=", content)
            self.assertIn(b"orca-server.service", content)
            self.assertNotIn(b"Wants=orca-server.service", content)
            self.assertNotIn(b"secretary-orca.service", content)

    def test_cli_plan_reports_update_delete_and_conflict_without_writing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            (instance / "projects").mkdir(parents=True)
            (instance / "instance.yaml").write_text(
                "version: 1\nname: plan\ndata_dir: "
                + str(root / "data")
                + "\noffsite:\n  instance_remote: git@example.invalid:x/y\nhost:\n  unit_prefix: secretary-\nheads:\n  - role: worker\n    model: test\n",
                encoding="utf-8",
            )
            (instance / "projects" / "project-id.yaml").write_text(
                "id: project-id\nrepo: /srv/project_id\norca_binding: project_id\nenabled: true\nadapter: project-id\ndefault_branch: main\n",
                encoding="utf-8",
            )
            fixture = root / "host"
            fixture.mkdir()
            (fixture / "units.txt").write_text("secretary-worker.service\n", encoding="utf-8")
            (fixture / "orca-repos.txt").write_text("project_id\n", encoding="utf-8")
            manifest = root / "managed.json"
            manifest.write_text(
                json.dumps(
                    {
                        "resources": [
                            {
                                "logical_id": "systemd:head:worker",
                                "kind": "unit",
                                "name": "secretary-worker.service",
                                "fingerprint": "old",
                            },
                            {
                                "logical_id": "systemd:head:retired",
                                "kind": "unit",
                                "name": "secretary-retired.service",
                                "fingerprint": "old",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (fixture / "units.txt").write_text(
                "secretary-worker.service\nsecretary-retired.service\n", encoding="utf-8"
            )
            before = manifest.read_bytes()
            argv = [
                "reconcile",
                "plan",
                "--instance",
                str(instance),
                "--host-fixture",
                str(fixture),
                "--managed-manifest",
                str(manifest),
            ]
            first = run_cli(argv)
            second = run_cli(argv)
            self.assertEqual(first, second)
            self.assertEqual(first[0], 1)
            self.assertIn("update systemd:head:worker", first[1])
            self.assertIn("delete systemd:head:retired", first[1])
            self.assertIn("conflict orca:project:project-id", first[1])
            self.assertEqual(manifest.read_bytes(), before)

    def test_cli_plan_reports_foreign_resource_under_unit_prefix(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance.yaml"
            instance.write_text(
                "version: 1\nname: plan\ndata_dir: /tmp/data\noffsite:\n  instance_remote: git@example.invalid:x/y\nhost:\n  unit_prefix: secretary-\nheads:\n  - role: worker\n    model: test\n",
                encoding="utf-8",
            )
            fixture = root / "host"
            fixture.mkdir()
            # A name in our namespace that the product does not ship: nothing in
            # the plan claims it and no managed record owns it.
            (fixture / "units.txt").write_text("secretary-legacy-sweep.timer\n", encoding="utf-8")
            code, output = run_cli(
                ["reconcile", "plan", "--instance", str(instance), "--host-fixture", str(fixture)]
            )
        self.assertEqual(code, 1, output)
        self.assertIn("conflict systemd:conflict:secretary-legacy-sweep.timer", output)

    def test_cli_plan_rejects_heads_without_unit_prefix(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance.yaml"
            instance.write_text(
                "version: 1\nname: plan\ndata_dir: /tmp/data\noffsite:\n  instance_remote: git@example.invalid:x/y\nheads:\n  - role: worker\n    model: test\n",
                encoding="utf-8",
            )
            fixture = root / "host"
            fixture.mkdir()
            code, output = run_cli(
                ["reconcile", "plan", "--instance", str(instance), "--host-fixture", str(fixture)]
            )
        self.assertEqual(code, 2, output)
        self.assertIn("host.unit_prefix is required", output)

    def test_cli_fixture_decode_error_returns_controlled_exit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "host"
            fixture.mkdir()
            (fixture / "units.txt").write_bytes(b"\xff")
            plan_code, plan_output = run_cli(
                ["reconcile", "plan", "--instance", str(EXAMPLE_INSTANCE), "--host-fixture", str(fixture)]
            )
            doctor_code, doctor_output = run_cli(
                ["doctor", "--instance", str(EXAMPLE_INSTANCE), "--host-fixture", str(fixture)]
            )
        self.assertEqual(plan_code, 2, plan_output)
        self.assertIn("host inventory unavailable", plan_output)
        self.assertEqual(doctor_code, 2, doctor_output)
        self.assertIn("units:\n  unavailable: fixture host file is not valid UTF-8", doctor_output)


class ReconcileAdoptTests(unittest.TestCase):
    @staticmethod
    def _record(logical_id: str, kind: str, name: str, spec: str) -> dict[str, str]:
        value = json.dumps([logical_id, kind, name, spec], separators=(",", ":"))
        return {
            "logical_id": logical_id,
            "kind": kind,
            "name": name,
            "spec": spec,
            "fingerprint": hashlib.sha256(value.encode()).hexdigest(),
        }

    def _instance(self, root: Path) -> tuple[Path, Path]:
        instance = root / "instance"
        (instance / "projects").mkdir(parents=True)
        data = root / "data"
        repo = root / "repo"
        repo.mkdir()
        (instance / "instance.yaml").write_text(
            "version: 1\nname: adopt\ndata_dir: "
            + str(data)
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y\nhost:\n"
            "  projects_root: " + str(root) + "\n  unit_prefix: secretary-\n",
            encoding="utf-8",
        )
        (instance / "projects" / "project.yaml").write_text(
            "id: project\nrepo: "
            + str(repo)
            + "\norca_binding: project-live\nenabled: true\nadapter: project\n"
            "default_branch: main\n",
            encoding="utf-8",
        )
        return instance, repo

    def _live(self, paths):
        class FakeLiveHost:
            def orca_repo_paths(self):
                return paths, ""

        return FakeLiveHost()

    def test_preview_requires_confirmation_and_does_not_write(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, repo = self._instance(root)
            manifest = root / "managed.json"
            with unittest.mock.patch.object(
                host_commands,
                "LiveHostSource",
                return_value=self._live({"project-live": str(repo.resolve())}),
            ):
                code, output = run_cli(
                    [
                        "reconcile",
                        "adopt",
                        "--instance",
                        str(instance),
                        "--logical-id",
                        "orca:project:project",
                        "--managed-manifest",
                        str(manifest),
                    ]
                )
            self.assertEqual(code, 0, output)
            self.assertIn("preview only", output)
            self.assertFalse(manifest.exists())
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["instance", "repo"])

    def test_confirmed_adopt_is_idempotent_and_plan_becomes_unchanged(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, repo = self._instance(root)
            manifest = root / "managed.json"
            argv = [
                "reconcile",
                "adopt",
                "--instance",
                str(instance),
                "--logical-id",
                "orca:project:project",
                "--managed-manifest",
                str(manifest),
                "--yes",
            ]
            with unittest.mock.patch.object(
                host_commands,
                "LiveHostSource",
                return_value=self._live({"project-live": str(repo.resolve())}),
            ):
                first = run_cli(argv)
                before = manifest.read_bytes()
                second = run_cli(argv)
            self.assertEqual(first[0], 0, first[1])
            self.assertEqual(second[0], 0, second[1])
            self.assertEqual(manifest.read_bytes(), before)
            payload = json.loads(before)
            self.assertEqual(payload["version"], 1)
            self.assertEqual([row["logical_id"] for row in payload["resources"]], ["orca:project:project"])

            fixture = root / "host"
            fixture.mkdir()
            (fixture / "orca-repos.txt").write_text("project-live\n", encoding="utf-8")
            code, output = run_cli(
                [
                    "reconcile",
                    "plan",
                    "--instance",
                    str(instance),
                    "--host-fixture",
                    str(fixture),
                    "--managed-manifest",
                    str(manifest),
                ]
            )
            self.assertEqual(code, 0, output)
            self.assertIn("unchanged orca:project:project", output)

    def test_adopt_rejects_missing_or_mismatched_live_identity(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, _repo = self._instance(root)
            base = [
                "reconcile",
                "adopt",
                "--instance",
                str(instance),
                "--logical-id",
                "orca:project:project",
                "--yes",
            ]
            for paths, message in (
                ({}, "is missing"),
                ({"project-live": str(root / "other")}, "does not match"),
            ):
                with (
                    self.subTest(message=message),
                    unittest.mock.patch.object(
                        host_commands, "LiveHostSource", return_value=self._live(paths)
                    ),
                ):
                    code, output = run_cli(base)
                self.assertEqual(code, 2, output)
                self.assertIn(message, output)
            self.assertFalse((root / "data" / "host-managed.json").exists())

    def test_adopt_rejects_unknown_id_and_unverifiable_kind(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, _ = self._instance(root)
            code, output = run_cli(
                [
                    "reconcile",
                    "adopt",
                    "--instance",
                    str(instance),
                    "--logical-id",
                    "orca:project:missing",
                    "--yes",
                ]
            )
            self.assertEqual(code, 2, output)
            self.assertIn("not in desired state", output)

    def test_adopt_rejects_drifted_existing_owned_record(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, repo = self._instance(root)
            manifest = root / "managed.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "resources": [
                            self._record("orca:project:project", "orca", "old-name", "{}"),
                        ],
                    }
                ),
                encoding="utf-8",
            )
            before = manifest.read_bytes()
            with unittest.mock.patch.object(
                host_commands,
                "LiveHostSource",
                return_value=self._live({"project-live": str(repo.resolve())}),
            ):
                code, output = run_cli(
                    [
                        "reconcile",
                        "adopt",
                        "--instance",
                        str(instance),
                        "--logical-id",
                        "orca:project:project",
                        "--managed-manifest",
                        str(manifest),
                        "--yes",
                    ]
                )
            self.assertEqual(code, 2, output)
            self.assertIn("has drifted", output)
            self.assertEqual(manifest.read_bytes(), before)

    def test_adopt_fails_closed_for_corrupt_duplicate_or_symlink_manifest(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, repo = self._instance(root)
            manifest = root / "managed.json"
            cases = [
                ("not-json", "not valid JSON"),
                (
                    json.dumps(
                        {
                            "version": 1,
                            "resources": [
                                self._record("x", "orca", "x", "{}"),
                                self._record("x", "orca", "y", "{}"),
                            ],
                        }
                    ),
                    "duplicate logical ids",
                ),
            ]
            for body, message in cases:
                with self.subTest(message=message):
                    manifest.unlink(missing_ok=True)
                    manifest.write_text(body, encoding="utf-8")
                    before = manifest.read_bytes()
                    with unittest.mock.patch.object(
                        host_commands,
                        "LiveHostSource",
                        return_value=self._live({"project-live": str(repo.resolve())}),
                    ):
                        code, output = run_cli(
                            [
                                "reconcile",
                                "adopt",
                                "--instance",
                                str(instance),
                                "--logical-id",
                                "orca:project:project",
                                "--managed-manifest",
                                str(manifest),
                                "--yes",
                            ]
                        )
                    self.assertEqual(code, 2, output)
                    self.assertIn(message, output)
                    self.assertEqual(manifest.read_bytes(), before)
            target = root / "target.json"
            target.write_text('{"version": 1, "resources": []}', encoding="utf-8")
            manifest.unlink()
            manifest.symlink_to(target)
            with unittest.mock.patch.object(
                host_commands,
                "LiveHostSource",
                return_value=self._live({"project-live": str(repo.resolve())}),
            ):
                code, output = run_cli(
                    [
                        "reconcile",
                        "adopt",
                        "--instance",
                        str(instance),
                        "--logical-id",
                        "orca:project:project",
                        "--managed-manifest",
                        str(manifest),
                        "--yes",
                    ]
                )
            self.assertEqual(code, 2, output)
            self.assertIn("must not be a symlink", output)

    def test_adopt_preserves_neighbors_and_reports_atomic_write_failure(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, repo = self._instance(root)
            manifest = root / "managed.json"
            neighbor = self._record("orca:project:neighbor", "orca", "neighbor", "{}")
            manifest.write_text(json.dumps({"version": 1, "resources": [neighbor]}), encoding="utf-8")
            argv = [
                "reconcile",
                "adopt",
                "--instance",
                str(instance),
                "--logical-id",
                "orca:project:project",
                "--managed-manifest",
                str(manifest),
                "--yes",
            ]
            with unittest.mock.patch.object(
                host_commands,
                "LiveHostSource",
                return_value=self._live({"project-live": str(repo.resolve())}),
            ):
                code, output = run_cli(argv)
            self.assertEqual(code, 0, output)
            self.assertEqual(
                [row["logical_id"] for row in json.loads(manifest.read_text())["resources"]],
                ["orca:project:neighbor", "orca:project:project"],
            )

            before = manifest.read_bytes()
            # Force a new record so the write path is exercised again.
            manifest.write_text(json.dumps({"version": 1, "resources": [neighbor]}), encoding="utf-8")
            before = manifest.read_bytes()
            with (
                unittest.mock.patch.object(
                    host_commands,
                    "LiveHostSource",
                    return_value=self._live({"project-live": str(repo.resolve())}),
                ),
                unittest.mock.patch.object(
                    host_commands, "write_text_atomic", side_effect=RuntimeError("injected publish failure")
                ),
            ):
                code, output = run_cli(argv)
            self.assertEqual(code, 2, output)
            self.assertIn("injected publish failure", output)
            self.assertEqual(manifest.read_bytes(), before)


def _cmd(ran=True, returncode=0, stdout="", stderr="", reason=""):
    return CmdResult(ran, returncode, stdout, stderr, reason)


class LiveSourceErrorTests(unittest.TestCase):
    """A host we cannot inspect must be reported as unavailable, never as empty."""

    def _host(self, responses):
        """A LiveHostSource whose _run replies from a {tool: _CmdResult} map."""

        class FakeHost(LiveHostSource):
            def _run(self, cmd):
                return responses[cmd[0]]

        return FakeHost()

    def test_missing_tool_is_reported_not_swallowed(self):
        host = self._host(
            {
                "systemctl": _cmd(ran=False, reason="systemctl not found"),
                "orca": _cmd(ran=False, reason="orca not found"),
            }
        )
        result = host.collect(Expectations(units={"u-a"}, unit_prefix="u-", orca_repos={"r-a"}))
        self.assertIn("units", result.errors)
        self.assertIn("orca repos", result.errors)
        self.assertEqual(result.inventory.units, set())
        self.assertEqual(result.inventory.orca_repos, set())

    def test_systemctl_no_match_is_empty_not_error(self):
        # list-unit-files exits 1 with empty stderr when nothing matches: that
        # is a real empty result, so the declared unit reads as missing-on-host.
        host = self._host(
            {
                "systemctl": _cmd(returncode=1, stdout="", stderr=""),
                "orca": _cmd(stdout=""),
            }
        )
        expected = Expectations(units={"secretary-pipeline.service"}, unit_prefix="secretary-")
        result = host.collect(expected)
        self.assertNotIn("units", result.errors)
        self.assertEqual(result.inventory.units, set())
        diff = inventory(expected, result.inventory)
        self.assertEqual(diff["units"].missing_on_host, ["secretary-pipeline.service"])

    def test_full_unit_names_match_systemctl_output(self):
        # The reviewer's scenario: systemctl list-unit-files prints full file
        # names with .service / .timer suffixes and one logical service can own
        # both. Declared full names must match exactly, and any extra unit in the
        # namespace surfaces as unmanaged-on-host, never as a false diff.
        host = self._host(
            {
                "systemctl": _cmd(
                    stdout=(
                        "secretary-pipeline.service static  -\nsecretary-pipeline.timer   enabled enabled\n"
                    )
                ),
                "orca": _cmd(stdout=""),
            }
        )
        expected = Expectations(units={"secretary-pipeline.service"}, unit_prefix="secretary-")
        result = host.collect(expected)
        self.assertNotIn("units", result.errors)
        self.assertEqual(
            result.inventory.units,
            {"secretary-pipeline.service", "secretary-pipeline.timer"},
        )
        diff = inventory(expected, result.inventory)
        self.assertEqual(diff["units"].matched, ["secretary-pipeline.service"])
        self.assertEqual(diff["units"].missing_on_host, [])
        self.assertEqual(diff["units"].unmanaged_on_host, ["secretary-pipeline.timer"])

    def test_runtime_probe_records_enabled_and_active_states(self):
        class RuntimeHost(LiveHostSource):
            def _run(self, cmd):
                if cmd[1] == "list-unit-files":
                    return _cmd(stdout="secretary-memory.service enabled enabled\n")
                if cmd[1] == "is-enabled":
                    return _cmd(returncode=1, stdout="disabled\n")
                if cmd[1] == "is-active":
                    return _cmd(returncode=3, stdout="failed\n")
                return _cmd(stdout="")

        expected = Expectations(
            units={"secretary-memory.service"},
            unit_prefix="secretary-",
            unit_runtime={"secretary-memory.service": (True, True)},
        )
        result = RuntimeHost().collect(expected)
        self.assertEqual(result.errors, {})
        self.assertEqual(result.inventory.unit_states["secretary-memory.service"], ("disabled", "failed"))

    def test_systemctl_stderr_is_a_failure(self):
        host = self._host(
            {
                "systemctl": _cmd(returncode=1, stdout="", stderr="Failed to connect to bus"),
                "orca": _cmd(stdout=""),
            }
        )
        result = host.collect(Expectations(units={"u"}, unit_prefix="u-"))
        self.assertIn("units", result.errors)

    def test_units_without_prefix_are_unavailable_not_silent(self):
        # No namespace means unmanaged-on-host cannot be computed. The live path
        # must refuse rather than emit a diff that silently omits stray units.
        host = self._host({"systemctl": _cmd(stdout=""), "orca": _cmd(stdout="")})
        result = host.collect(Expectations(units={"secretary-pipeline.service"}, unit_prefix=""))
        self.assertIn("units", result.errors)
        self.assertIn("unit_prefix", result.errors["units"])
        self.assertEqual(result.inventory.units, set())

    def test_prefix_enumerates_namespace_even_with_no_declared_units(self):
        # A declared prefix with no expected units still surfaces stray units as
        # unmanaged-on-host, so ownership of the namespace is not silently dropped.
        host = self._host(
            {
                "systemctl": _cmd(stdout="secretary-retro.service enabled enabled\n"),
                "orca": _cmd(stdout=""),
            }
        )
        expected = Expectations(units=set(), unit_prefix="secretary-")
        result = host.collect(expected)
        self.assertNotIn("units", result.errors)
        diff = inventory(expected, result.inventory)
        self.assertEqual(diff["units"].unmanaged_on_host, ["secretary-retro.service"])

    def test_orca_non_zero_exit_is_a_failure(self):
        host = self._host(
            {
                "systemctl": _cmd(stdout=""),
                "orca": _cmd(returncode=1, stderr="cannot reach daemon"),
            }
        )
        result = host.collect(Expectations(orca_repos={"r"}))
        self.assertIn("orca repos", result.errors)

    def test_orca_json_paths_are_normalized_and_duplicates_fail(self):
        root = Path("/tmp") / "orca-json-path"
        payload = json.dumps(
            {
                "result": {
                    "repos": [
                        {"displayName": "one", "path": str(root / "a" / ".." / "repo")},
                    ]
                }
            }
        )
        host = self._host({"orca": _cmd(stdout=payload), "systemctl": _cmd()})
        paths, error = host.orca_repo_paths()
        self.assertEqual(error, "")
        self.assertEqual(paths, {"one": str((root / "repo").resolve(strict=False))})

        duplicate = json.dumps(
            {
                "result": {
                    "repos": [
                        {"displayName": "one", "path": "/srv/a"},
                        {"displayName": "one", "path": "/srv/b"},
                    ]
                }
            }
        )
        host = self._host({"orca": _cmd(stdout=duplicate), "systemctl": _cmd()})
        self.assertEqual(host.orca_repo_paths()[1], "orca returned duplicate registration names")

    def test_orca_inventory_keeps_paths_for_lazy_observer_ownership(self):
        repo = "/srv/secretary-data/dispatcher/observer-root/observers"
        host = self._host(
            {
                "orca": _cmd(stdout=f"id-1 observers {repo}\n"),
                "systemctl": _cmd(stdout=""),
            }
        )
        expected = Expectations(lazy_orca_repos={"observers": repo})
        collected = host.collect(expected)
        self.assertEqual(collected.inventory.orca_repo_paths, {"observers": repo})
        self.assertEqual(inventory(expected, collected.inventory)["orca repos"].matched, ["observers"])

    def test_declared_projects_without_root_is_unavailable(self):
        expected = Expectations(projects={"a"}, projects_root="")
        projects, reason = LiveHostSource()._projects(expected)
        self.assertEqual(projects, set())
        self.assertTrue(reason)

    def test_unreadable_expected_checkout_is_unavailable_not_missing(self):
        expected = Expectations(projects={"/opt/checkouts/outside-root"})
        with unittest.mock.patch.object(Path, "stat", side_effect=PermissionError):
            projects, reason = LiveHostSource()._projects(expected)
        self.assertEqual(projects, set())
        self.assertEqual(reason, "expected project checkout path could not be inspected")

    def test_symlink_loop_in_expected_checkout_is_unavailable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            loop = Path(tmp) / "loop"
            loop.symlink_to(loop)
            expected = build_doctor_expectations(
                {"host": {"projects_root": tmp, "unit_prefix": "secretary-"}},
                [{"id": "loop", "repo": str(loop), "enabled": True, "orca_binding": "loop"}],
            )
        self.assertEqual(expected.project_error, "expected project checkout path could not be normalized")
        projects, reason = LiveHostSource()._projects(expected)
        self.assertEqual(projects, set())
        self.assertEqual(reason, expected.project_error)

    def test_projects_root_error_does_not_echo_value(self):
        secret = "/srv/sk-live-projects-root-DO-NOT-LEAK-8c1d"
        expected = Expectations(projects={"a"}, projects_root=secret)
        _, reason = LiveHostSource()._projects(expected)
        self.assertTrue(reason)
        self.assertNotIn(secret, reason)
        self.assertIn("host.projects_root", reason)

    def test_projects_outside_root_are_checked_by_exact_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside" / "same-name"
            outside.parent.mkdir()
            inside = root / "projects" / "same-name"
            inside.mkdir(parents=True)
            expected = Expectations(projects={str(outside)}, projects_root=str(root / "projects"))
            actual, reason = LiveHostSource()._projects(expected)
        self.assertEqual(reason, "")
        self.assertNotIn(str(outside), actual)
        self.assertIn(str(inside), actual)

    def test_run_reports_missing_binary(self):
        result = LiveHostSource()._run(["definitely-no-such-binary-xyz"])
        self.assertFalse(result.ran)
        self.assertIn("not found", result.reason)

    def test_run_times_out(self):
        class SlowHost(LiveHostSource):
            timeout_seconds = 0.2

        result = SlowHost()._run(["sleep", "5"])
        self.assertFalse(result.ran)
        self.assertIn("timed out", result.reason)

    def test_run_captures_non_zero_exit(self):
        result = LiveHostSource()._run(["false"])
        self.assertTrue(result.ran)
        self.assertEqual(result.returncode, 1)


class DoctorHostCliTests(unittest.TestCase):
    def setUp(self) -> None:
        # Doctor reports an installation, and an installation runs a checkout. These fixtures are
        # hosts configured with this one; without the name, doctor would have no units to compare.
        env = unittest.mock.patch.dict(os.environ, {"TA_SECRETARY_REPO": str(REPO_ROOT)})
        env.start()
        self.addCleanup(env.stop)

    def _dispatcher_instance(self, root: Path) -> tuple[Path, Path]:
        instance = root / "instance.yaml"
        data = root / "data"
        (data / "dispatcher").mkdir(parents=True)
        instance.write_text(
            "version: 1\n"
            "name: dispatcher-doctor\n"
            f"data_dir: {data}\n"
            "offsite:\n"
            "  instance_remote: git@example.invalid:x/y.git\n"
            "host:\n"
            "  unit_prefix: secretary-\n"
            "  components:\n"
            "    curator: {enabled: false}\n"
            "    memory: {enabled: false}\n"
            "    retro: {enabled: false}\n"
            "    steward: {enabled: false}\n"
            "    steward-deep-sweep: {enabled: false}\n",
            encoding="utf-8",
        )
        return instance, data

    def test_offline_doctor_does_not_construct_live_host_source(self):
        class ForbiddenHost(LiveHostSource):
            def __init__(self):
                raise AssertionError("offline doctor touched host")

        original = cli.LiveHostSource
        cli.LiveHostSource = ForbiddenHost
        try:
            code, output = run_cli(["doctor", "--offline", "--instance", str(EXAMPLE_INSTANCE)])
        finally:
            cli.LiveHostSource = original
        self.assertEqual(code, 0, output)
        self.assertNotIn("host inventory", output)

    def test_doctor_reds_without_the_production_service(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, data = self._dispatcher_instance(root)
            (data / "dispatcher" / "production-state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mode": "production",
                        "phase": "production",
                        "owner": "secretary-dispatcher",
                    }
                ),
                encoding="utf-8",
            )
            fixture = root / "host"
            fixture.mkdir()

            code, output = run_cli(
                [
                    "doctor",
                    "--dry-run",
                    "--instance",
                    str(instance),
                    "--host-fixture",
                    str(fixture),
                ]
            )

        self.assertEqual(code, 1, output)
        self.assertIn("state: production-owner", output)
        self.assertIn("create secretary-dispatcher-production.service", output)

    def test_doctor_accepts_a_managed_production_owner(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, data = self._dispatcher_instance(root)
            (data / "dispatcher" / "production-state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mode": "production",
                        "phase": "production",
                        "owner": "secretary-dispatcher",
                    }
                ),
                encoding="utf-8",
            )
            report = validate_instance(instance)
            self.assertTrue(report.ok, report.errors)
            with legacy_orca_runtime(root) as legacy_orca:
                with unittest.mock.patch(
                    "secretary.host_apply.find_orca_executable", return_value=None
                ) as find_executable:
                    packaged = resolve_packaged(
                        report.instance,
                        instance_path=report.instance_path.parent,
                        data_dir=report.data_dir,
                        orca_executable=legacy_orca,
                    )
                find_executable.assert_not_called()
            desired = [
                resource
                for resource in build_plan(report.instance, report.bindings, packaged=packaged)
                if resource.logical_id.startswith("systemd:dispatcher:production")
            ]
            (data / "host-managed.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "resources": [
                            {
                                "logical_id": resource.logical_id,
                                "kind": resource.kind,
                                "name": resource.name,
                                "spec": resource.spec,
                                "fingerprint": resource.fingerprint,
                            }
                            for resource in desired
                        ],
                    }
                ),
                encoding="utf-8",
            )
            fixture = root / "host"
            fixture.mkdir()
            (fixture / "units.txt").write_text(
                "secretary-dispatcher-production.service\nsecretary-dispatcher-production.timer\n",
                encoding="utf-8",
            )

            account = SimpleNamespace(pw_name="operator", pw_dir=str(root / "operator"))
            with (
                unittest.mock.patch("secretary.host_apply.pwd.getpwuid", return_value=account),
                unittest.mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
            ):
                code, output = run_cli(
                    [
                        "doctor",
                        "--dry-run",
                        "--instance",
                        str(instance),
                        "--host-fixture",
                        str(fixture),
                    ],
                    orca_executable=legacy_orca,
                )

        self.assertEqual(code, 0, output)
        self.assertIn("state: production-owner", output)
        self.assertNotIn("dispatcher findings", output)

    def test_host_inventory_reports_three_sections(self):
        code, output = run_cli(
            [
                "doctor",
                "--dry-run",
                "--instance",
                str(EXAMPLE_INSTANCE),
                "--host-fixture",
                str(HOST_FIXTURE),
            ]
        )

        self.assertEqual(code, 1, output)
        self.assertIn("host inventory: read-only", output)
        # projects
        self.assertIn("projects:\n  matched: /srv/projects/example-project", output)
        self.assertIn("unmanaged-on-host: /srv/projects/stray-project", output)
        # units: one of each outcome, full unit file names
        self.assertIn("units:\n  matched: (none)", output)
        self.assertIn("missing-on-host: secretary-curator.service", output)
        self.assertIn("secretary-retro.timer", output)
        # orca repos
        self.assertIn("orca repos:\n  matched: (none)", output)
        self.assertIn("extra-repo", output)
        self.assertIn("status: findings", output)

    def test_doctor_reports_missing_canonical_resources_and_runtime_drift(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            data = root / "data"
            instance = root / "instance.yaml"
            instance.write_text(
                "version: 1\nname: doctor\ndata_dir: " + str(data) + "\noffsite:\n"
                "  instance_remote: git@example.invalid:x/y\nhost:\n"
                "  projects_root: " + str(root) + "\n  unit_prefix: secretary-\n",
                encoding="utf-8",
            )
            projects = root / "projects"
            projects.mkdir()
            (projects / "demo.yaml").write_text(
                "id: demo\nrepo: "
                + str(repo)
                + "\nenabled: true\norca_binding: demo\nadapter: demo\ndefault_branch: main\n",
                encoding="utf-8",
            )
            fixture = root / "host"
            fixture.mkdir()
            (fixture / "projects.txt").write_text(str(repo) + "\n", encoding="utf-8")
            (fixture / "units.txt").write_text("secretary-memory.service\n", encoding="utf-8")

            code, output = run_cli(["doctor", "--instance", str(instance), "--host-fixture", str(fixture)])

        self.assertEqual(code, 1, output)
        self.assertIn("missing-on-host: demo", output)
        self.assertIn("secretary-dispatcher-production.service", output)

    def test_doctor_fixture_does_not_match_checkout_by_basename(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside" / "same-name"
            outside.parent.mkdir()
            projects_root = root / "projects"
            projects_root.mkdir()
            data = root / "data"
            instance = root / "instance.yaml"
            instance.write_text(
                "version: 1\nname: doctor\ndata_dir: " + str(data) + "\noffsite:\n"
                "  instance_remote: git@example.invalid:x/y\nhost:\n"
                "  projects_root: " + str(projects_root) + "\n  unit_prefix: secretary-\n",
                encoding="utf-8",
            )
            (projects_root / "demo.yaml").write_text(
                "id: demo\nrepo: "
                + str(outside)
                + "\nenabled: true\norca_binding: demo\nadapter: demo\ndefault_branch: main\n",
                encoding="utf-8",
            )
            fixture = root / "host"
            (fixture / "projects" / "same-name").mkdir(parents=True)

            code, output = run_cli(["doctor", "--instance", str(instance), "--host-fixture", str(fixture)])

        self.assertEqual(code, 1, output)
        self.assertIn("missing-on-host: " + str(outside), output)
        self.assertIn("unmanaged-on-host: " + str(fixture / "projects" / "same-name"), output)

    def test_doctor_fails_for_required_inactive_service(self):
        expected = build_doctor_expectations({"host": {"unit_prefix": "secretary-"}}, [])

        class HealthyFilesFailedRuntime:
            def collect(self, ignored):
                states = {name: ("enabled", "active") for name in expected.unit_runtime}
                states["secretary-memory.service"] = ("enabled", "failed")
                return CollectResult(HostInventory(units=expected.units, unit_states=states), {})

        with unittest.mock.patch.object(cli, "LiveHostSource", return_value=HealthyFilesFailedRuntime()):
            code, output = run_cli(["doctor", "--instance", str(EXAMPLE_INSTANCE)])

        self.assertEqual(code, 1, output)
        self.assertIn("secretary-memory.service: expected active, got failed", output)

    def test_doctor_returns_unavailable_for_symlink_loop_checkout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance.yaml"
            data = root / "data"
            projects = root / "projects"
            projects.mkdir()
            loop = root / "loop"
            loop.symlink_to(loop)
            instance.write_text(
                "version: 1\nname: doctor\ndata_dir: " + str(data) + "\noffsite:\n"
                "  instance_remote: git@example.invalid:x/y\nhost:\n"
                "  projects_root: " + str(root) + "\n  unit_prefix: secretary-\n",
                encoding="utf-8",
            )
            (projects / "loop.yaml").write_text(
                "id: loop\nrepo: "
                + str(loop)
                + "\nenabled: true\norca_binding: loop\nadapter: loop\ndefault_branch: main\n",
                encoding="utf-8",
            )

            class ExpectedCheckoutUnavailable:
                def collect(self, expected):
                    return CollectResult(
                        HostInventory(),
                        {"projects": expected.project_error},
                    )

            with unittest.mock.patch.object(
                cli, "LiveHostSource", return_value=ExpectedCheckoutUnavailable()
            ):
                code, output = run_cli(["doctor", "--host", "--instance", str(instance)])

        self.assertEqual(code, 2, output)
        self.assertIn(
            "projects:\n  unavailable: expected project checkout path could not be normalized", output
        )
        self.assertNotIn("projects:\n  missing-on-host", output)

    def test_without_host_flag_no_inventory(self):
        code, output = run_cli(["doctor", "--dry-run", "--instance", str(EXAMPLE_INSTANCE)])
        self.assertEqual(code, 0, output)
        self.assertNotIn("host inventory", output)
        # Phase 1 summary line is unchanged.
        self.assertIn("projects: 1", output)

    def test_missing_fixture_root_exits_nonzero_not_false_missing(self):
        # A wrong --host-fixture path must not fail open: every kind reads as
        # unavailable and doctor exits non-zero, instead of printing all expected
        # resources as missing-on-host against a host that was never inspected.
        missing_root = str(REPO_ROOT / "tests" / "fixtures" / "no-such-fixture-root")
        code, output = run_cli(
            [
                "doctor",
                "--dry-run",
                "--instance",
                str(EXAMPLE_INSTANCE),
                "--host-fixture",
                missing_root,
            ]
        )
        self.assertEqual(code, 2, output)
        self.assertIn("unavailable: fixture host directory not found", output)
        self.assertIn("status: host inventory incomplete", output)
        # The false-clean symptom is gone: no expected resource is listed as
        # missing-on-host, because no comparison ran.
        self.assertNotIn("missing-on-host", output)
        self.assertNotIn("example-project", output)

    def test_host_inventory_is_read_only(self):
        before_fixture = snapshot(HOST_FIXTURE)
        before_instance = snapshot(EXAMPLE_INSTANCE)

        code, _ = run_cli(
            [
                "doctor",
                "--dry-run",
                "--instance",
                str(EXAMPLE_INSTANCE),
                "--host-fixture",
                str(HOST_FIXTURE),
            ]
        )

        self.assertEqual(code, 1)
        self.assertEqual(snapshot(HOST_FIXTURE), before_fixture)
        self.assertEqual(snapshot(EXAMPLE_INSTANCE), before_instance)

    def test_uninspectable_host_marks_unavailable_and_exits_nonzero(self):
        class StubSource(LiveHostSource):
            def collect(self, expected):
                return CollectResult(
                    inventory=HostInventory(orca_repos={"secretary"}),
                    errors={"orca repos": "orca not found"},
                )

        original = cli.LiveHostSource
        cli.LiveHostSource = StubSource
        try:
            code, output = run_cli(["doctor", "--dry-run", "--instance", str(EXAMPLE_INSTANCE), "--host"])
        finally:
            cli.LiveHostSource = original

        self.assertEqual(code, 2, output)
        self.assertIn("orca repos:\n  unavailable: orca not found", output)
        self.assertIn("status: host inventory incomplete", output)
        # A kind that did read is still reported normally.
        self.assertIn("projects:\n  matched", output)

    def test_projects_root_value_never_reaches_output(self):
        secret = "/srv/sk-live-projects-root-DO-NOT-LEAK-8c1d"
        import tempfile

        # Tools stubbed to a clean empty read, so only projects can error and we
        # isolate the config-value leak the reviewer found.
        class QuietHost(LiveHostSource):
            def _run(self, cmd):
                return CmdResult(True, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance.yaml"
            instance.write_text(
                "version: 1\n"
                "name: leak-check\n"
                "data_dir: /var/lib/secretary-data\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n"
                "host:\n"
                f"  projects_root: {secret}\n",
                encoding="utf-8",
            )

            original = cli.LiveHostSource
            cli.LiveHostSource = QuietHost
            try:
                code, output = run_cli(["doctor", "--dry-run", "--instance", str(instance), "--host"])
            finally:
                cli.LiveHostSource = original

            self.assertEqual(code, 2, output)
        self.assertNotIn(secret, output)
        self.assertIn("projects:\n  unavailable:", output)

    def test_units_without_prefix_rejected_before_inventory(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance.yaml"
            instance.write_text(
                "version: 1\n"
                "name: no-prefix\n"
                "data_dir: /var/lib/secretary-data\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n"
                "host:\n"
                "  units:\n"
                "    - secretary-pipeline\n",
                encoding="utf-8",
            )
            code, output = run_cli(["doctor", "--dry-run", "--instance", str(instance), "--host"])

        # Config validation rejects the shape, so the misleading inventory that
        # would print unmanaged-on-host: (none) is never reached.
        self.assertEqual(code, 1, output)
        self.assertIn("config problem", output)
        self.assertIn("unit_prefix", output)
        self.assertNotIn("host inventory", output)

    def test_output_excludes_env_file_contents(self):
        secret = "sk-live-host-inventory-DO-NOT-LEAK-71af"
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            project = fixture / "projects" / "example-project"
            project.mkdir(parents=True)
            # A secret sitting inside a project dir must never be opened or printed.
            (project / ".env").write_text(f"API_KEY={secret}\n", encoding="utf-8")
            (fixture / "units.txt").write_text("secretary-pipeline\n", encoding="utf-8")
            (fixture / "orca-repos.txt").write_text("secretary\n", encoding="utf-8")

            code, output = run_cli(
                [
                    "doctor",
                    "--dry-run",
                    "--instance",
                    str(EXAMPLE_INSTANCE),
                    "--host-fixture",
                    str(fixture),
                ]
            )

        self.assertEqual(code, 1, output)
        self.assertNotIn(secret, output)
        self.assertIn("host inventory: read-only", output)


if __name__ == "__main__":
    unittest.main()
