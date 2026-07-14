from __future__ import annotations

import contextlib
import hashlib
import io
import json
import unittest
import unittest.mock
from pathlib import Path

import secretary.cli as cli
import secretary.host_commands as host_commands
from secretary.cli import main
from secretary.host import (
    CollectResult,
    Expectations,
    FixtureHostSource,
    HostInventory,
    LiveHostSource,
    _CmdResult as CmdResult,
    build_expectations,
    build_plan,
    inventory,
    plan_input_errors,
    plan_changes,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_INSTANCE = REPO_ROOT / "examples" / "instance"
HOST_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "host"


def run_cli(argv: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
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
    def test_project_name_from_repo_path(self):
        exp = build_expectations(
            [{"id": "an-id", "repo": "/srv/projects/on-disk-name"}], {}
        )
        # The host-facing name comes from the repo directory, not the id.
        self.assertEqual(exp.projects, {"on-disk-name"})

    def test_project_name_falls_back_to_id(self):
        exp = build_expectations([{"id": "an-id", "repo": "an-id"}], {})
        self.assertEqual(exp.projects, {"an-id"})

    def test_git_suffix_stripped(self):
        exp = build_expectations(
            [{"id": "x", "repo": "git@example.invalid:acme/widget.git"}], {}
        )
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
        expected = Expectations(
            projects={"a", "b"}, units={"u"}, orca_repos={"r1", "r2"}
        )
        actual = HostInventory(
            projects={"b", "c"}, units={"u"}, orca_repos={"r1", "r3"}
        )
        result = inventory(expected, actual)
        self.assertEqual(result["projects"].matched, ["b"])
        self.assertEqual(result["projects"].missing_on_host, ["a"])
        self.assertEqual(result["projects"].unmanaged_on_host, ["c"])
        self.assertEqual(result["units"].matched, ["u"])
        self.assertEqual(result["orca repos"].missing_on_host, ["r2"])
        self.assertEqual(result["orca repos"].unmanaged_on_host, ["r3"])


class FixtureSourceTests(unittest.TestCase):
    def test_collect_reads_names_only(self):
        source = FixtureHostSource(HOST_FIXTURE)
        result = source.collect(Expectations())
        self.assertEqual(result.errors, {})
        actual = result.inventory
        self.assertEqual(actual.projects, {"example-project", "stray-project"})
        # Full unit file names, exactly as systemctl list-unit-files prints them.
        self.assertEqual(actual.units, {"secretary-pipeline.service", "secretary-retro.timer"})
        self.assertEqual(actual.orca_repos, {"example-project", "secretary", "extra-repo"})

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


class ReconcilePlanTests(unittest.TestCase):
    def test_cli_plan_uses_live_source_by_default(self):
        class FakeLiveHost:
            def collect(self, expected):
                return CollectResult(HostInventory(), {})

        with unittest.mock.patch.object(host_commands, "LiveHostSource", return_value=FakeLiveHost()) as source:
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
                "version: 1\nname: plan\ndata_dir: " + str(data_dir)
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
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main([
                "reconcile", "plan", "--instance", str(EXAMPLE_INSTANCE), "--offline",
                "--host-fixture", str(HOST_FIXTURE),
            ])
        self.assertEqual(error.exception.code, 2)

    def test_runtime_payload_changes_require_an_update(self):
        instance = {"host": {"unit_prefix": "secretary-"}, "heads": [{"role": "worker", "model": "old"}]}
        bindings = [{"id": "project-id", "repo": "/srv/old-path", "orca_binding": "project_id", "enabled": True}]
        original = build_plan(instance, bindings)
        actual = HostInventory(units={"secretary-worker.service"}, orca_repos={"project_id"})
        instance["heads"][0]["model"] = "new"
        bindings[0]["repo"] = "/srv/new-path"
        changed = build_plan(instance, bindings)
        self.assertEqual({change.action for change in plan_changes(changed, actual, original)}, {"update"})

    def test_plan_rejects_enabled_binding_without_explicit_orca_binding(self):
        errors = plan_input_errors({}, [{"id": "foo-bar", "repo": "/srv/foo_bar", "enabled": True}])
        self.assertEqual(errors, ["enabled binding requires explicit orca_binding"])

    def test_plan_rejects_heads_without_unit_prefix(self):
        errors = plan_input_errors({"heads": [{"role": "worker", "model": "test"}]}, [])
        self.assertEqual(errors, ["host.unit_prefix is required when heads are configured"])

    def test_plan_rejects_duplicate_logical_id_and_host_name(self):
        duplicate_heads = {"host": {"unit_prefix": "secretary-"}, "heads": [
            {"role": "worker", "model": "one"}, {"role": "worker", "model": "two"},
        ]}
        self.assertIn("duplicate desired logical_id: systemd:head:worker", plan_input_errors(duplicate_heads, []))
        bindings = [
            {"id": "alpha", "repo": "/srv/a", "orca_binding": "shared", "enabled": True},
            {"id": "beta", "repo": "/srv/b", "orca_binding": "shared", "enabled": True},
        ]
        self.assertIn("duplicate desired resource name: orca shared", plan_input_errors({}, bindings))

    def test_renamed_managed_resource_is_deleted_alongside_create(self):
        old_instance = {"host": {"unit_prefix": "old-"}, "heads": [{"role": "worker", "model": "test"}]}
        new_instance = {"host": {"unit_prefix": "new-"}, "heads": [{"role": "worker", "model": "test"}]}
        managed = build_plan(old_instance, [])
        desired = build_plan(new_instance, [])
        actual = HostInventory(units={"old-worker.service"})
        changes = plan_changes(desired, actual, managed, "new-")
        self.assertEqual([(change.action, change.name) for change in changes], [("create", "new-worker.service"), ("delete", "old-worker.service")])

    def test_plan_is_stable_and_name_match_without_manifest_is_conflict(self):
        instance = {
            "host": {"unit_prefix": "secretary-"},
            "heads": [{"role": "worker", "model": "test"}],
        }
        bindings = [{"id": "project-id", "repo": "/srv/project_id", "orca_binding": "project_id", "enabled": True}]
        desired = build_plan(instance, bindings)
        self.assertEqual([resource.name for resource in desired], ["project_id", "secretary-worker.service"])
        actual = HostInventory(units={"secretary-worker.service"}, orca_repos={"project_id"})
        first = plan_changes(desired, actual, [])
        second = plan_changes(desired, actual, [])
        self.assertEqual(first, second)
        self.assertEqual({change.action for change in first}, {"conflict"})

    def test_cli_plan_reports_update_delete_and_conflict_without_writing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            (instance / "projects").mkdir(parents=True)
            (instance / "instance.yaml").write_text(
                "version: 1\nname: plan\ndata_dir: " + str(root / "data") + "\noffsite:\n  instance_remote: git@example.invalid:x/y\nhost:\n  unit_prefix: secretary-\nheads:\n  - role: worker\n    model: test\n",
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
            manifest.write_text(json.dumps({"resources": [
                {"logical_id": "systemd:head:worker", "kind": "unit", "name": "secretary-worker.service", "fingerprint": "old"},
                {"logical_id": "systemd:head:retired", "kind": "unit", "name": "secretary-retired.service", "fingerprint": "old"},
            ]}), encoding="utf-8")
            (fixture / "units.txt").write_text("secretary-worker.service\nsecretary-retired.service\n", encoding="utf-8")
            before = manifest.read_bytes()
            argv = ["reconcile", "plan", "--instance", str(instance), "--host-fixture", str(fixture), "--managed-manifest", str(manifest)]
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
            (fixture / "units.txt").write_text("secretary-retro.timer\n", encoding="utf-8")
            code, output = run_cli(["reconcile", "plan", "--instance", str(instance), "--host-fixture", str(fixture)])
        self.assertEqual(code, 1, output)
        self.assertIn("conflict systemd:conflict:secretary-retro.timer", output)

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
            code, output = run_cli(["reconcile", "plan", "--instance", str(instance), "--host-fixture", str(fixture)])
        self.assertEqual(code, 2, output)
        self.assertIn("host.unit_prefix is required", output)

    def test_cli_fixture_decode_error_returns_controlled_exit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "host"
            fixture.mkdir()
            (fixture / "units.txt").write_bytes(b"\xff")
            plan_code, plan_output = run_cli(["reconcile", "plan", "--instance", str(EXAMPLE_INSTANCE), "--host-fixture", str(fixture)])
            doctor_code, doctor_output = run_cli(["doctor", "--instance", str(EXAMPLE_INSTANCE), "--host-fixture", str(fixture)])
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
            "version: 1\nname: adopt\ndata_dir: " + str(data)
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y\nhost:\n"
            "  projects_root: " + str(root) + "\n  unit_prefix: secretary-\n",
            encoding="utf-8",
        )
        (instance / "projects" / "project.yaml").write_text(
            "id: project\nrepo: " + str(repo)
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
            with unittest.mock.patch.object(host_commands, "LiveHostSource", return_value=self._live({"project-live": str(repo.resolve())})):
                code, output = run_cli([
                    "reconcile", "adopt", "--instance", str(instance),
                    "--logical-id", "orca:project:project", "--managed-manifest", str(manifest),
                ])
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
                "reconcile", "adopt", "--instance", str(instance),
                "--logical-id", "orca:project:project", "--managed-manifest", str(manifest), "--yes",
            ]
            with unittest.mock.patch.object(host_commands, "LiveHostSource", return_value=self._live({"project-live": str(repo.resolve())})):
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
            code, output = run_cli([
                "reconcile", "plan", "--instance", str(instance), "--host-fixture", str(fixture),
                "--managed-manifest", str(manifest),
            ])
            self.assertEqual(code, 0, output)
            self.assertIn("unchanged orca:project:project", output)

    def test_adopt_rejects_missing_or_mismatched_live_identity(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, repo = self._instance(root)
            base = ["reconcile", "adopt", "--instance", str(instance), "--logical-id", "orca:project:project", "--yes"]
            for paths, message in (({}, "is missing"), ({"project-live": str(root / "other")}, "does not match")):
                with self.subTest(message=message), unittest.mock.patch.object(
                    host_commands, "LiveHostSource", return_value=self._live(paths)
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
            code, output = run_cli([
                "reconcile", "adopt", "--instance", str(instance), "--logical-id", "orca:project:missing", "--yes",
            ])
            self.assertEqual(code, 2, output)
            self.assertIn("not in desired state", output)

    def test_adopt_rejects_drifted_existing_owned_record(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, repo = self._instance(root)
            manifest = root / "managed.json"
            manifest.write_text(json.dumps({"version": 1, "resources": [
                self._record("orca:project:project", "orca", "old-name", "{}"),
            ]}), encoding="utf-8")
            before = manifest.read_bytes()
            with unittest.mock.patch.object(host_commands, "LiveHostSource", return_value=self._live({"project-live": str(repo.resolve())})):
                code, output = run_cli([
                    "reconcile", "adopt", "--instance", str(instance), "--logical-id", "orca:project:project",
                    "--managed-manifest", str(manifest), "--yes",
                ])
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
                (json.dumps({"version": 1, "resources": [
                    self._record("x", "orca", "x", "{}"),
                    self._record("x", "orca", "y", "{}"),
                ]}), "duplicate logical ids"),
            ]
            for body, message in cases:
                with self.subTest(message=message):
                    manifest.unlink(missing_ok=True)
                    manifest.write_text(body, encoding="utf-8")
                    before = manifest.read_bytes()
                    with unittest.mock.patch.object(host_commands, "LiveHostSource", return_value=self._live({"project-live": str(repo.resolve())})):
                        code, output = run_cli([
                            "reconcile", "adopt", "--instance", str(instance), "--logical-id", "orca:project:project",
                            "--managed-manifest", str(manifest), "--yes",
                        ])
                    self.assertEqual(code, 2, output)
                    self.assertIn(message, output)
                    self.assertEqual(manifest.read_bytes(), before)
            target = root / "target.json"
            target.write_text('{"version": 1, "resources": []}', encoding="utf-8")
            manifest.unlink()
            manifest.symlink_to(target)
            with unittest.mock.patch.object(host_commands, "LiveHostSource", return_value=self._live({"project-live": str(repo.resolve())})):
                code, output = run_cli([
                    "reconcile", "adopt", "--instance", str(instance), "--logical-id", "orca:project:project",
                    "--managed-manifest", str(manifest), "--yes",
                ])
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
                "reconcile", "adopt", "--instance", str(instance), "--logical-id", "orca:project:project",
                "--managed-manifest", str(manifest), "--yes",
            ]
            with unittest.mock.patch.object(host_commands, "LiveHostSource", return_value=self._live({"project-live": str(repo.resolve())})):
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
            with unittest.mock.patch.object(host_commands, "LiveHostSource", return_value=self._live({"project-live": str(repo.resolve())})), unittest.mock.patch.object(
                host_commands, "write_text_atomic", side_effect=RuntimeError("injected publish failure")
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
                        "secretary-pipeline.service static  -\n"
                        "secretary-pipeline.timer   enabled enabled\n"
                    )
                ),
                "orca": _cmd(stdout=""),
            }
        )
        expected = Expectations(
            units={"secretary-pipeline.service"}, unit_prefix="secretary-"
        )
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
        payload = json.dumps({"result": {"repos": [
            {"displayName": "one", "path": str(root / "a" / ".." / "repo")},
        ]}})
        host = self._host({"orca": _cmd(stdout=payload), "systemctl": _cmd()})
        paths, error = host.orca_repo_paths()
        self.assertEqual(error, "")
        self.assertEqual(paths, {"one": str((root / "repo").resolve(strict=False))})

        duplicate = json.dumps({"result": {"repos": [
            {"displayName": "one", "path": "/srv/a"},
            {"displayName": "one", "path": "/srv/b"},
        ]}})
        host = self._host({"orca": _cmd(stdout=duplicate), "systemctl": _cmd()})
        self.assertEqual(host.orca_repo_paths()[1], "orca returned duplicate registration names")

    def test_declared_projects_without_root_is_unavailable(self):
        expected = Expectations(projects={"a"}, projects_root="")
        projects, reason = LiveHostSource()._projects(expected)
        self.assertEqual(projects, set())
        self.assertTrue(reason)

    def test_projects_root_error_does_not_echo_value(self):
        secret = "/srv/sk-live-projects-root-DO-NOT-LEAK-8c1d"
        expected = Expectations(projects={"a"}, projects_root=secret)
        _, reason = LiveHostSource()._projects(expected)
        self.assertTrue(reason)
        self.assertNotIn(secret, reason)
        self.assertIn("host.projects_root", reason)

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

        self.assertEqual(code, 0, output)
        self.assertIn("host inventory: read-only", output)
        # projects
        self.assertIn("projects:\n  matched: example-project", output)
        self.assertIn("unmanaged-on-host: stray-project", output)
        # units: one of each outcome, full unit file names
        self.assertIn("units:\n  matched: secretary-pipeline.service", output)
        self.assertIn("missing-on-host: secretary-curator.timer", output)
        self.assertIn("unmanaged-on-host: secretary-retro.timer", output)
        # orca repos
        self.assertIn("orca repos:\n  matched: example-project, secretary", output)
        self.assertIn("missing-on-host: secretary-instance", output)
        self.assertIn("unmanaged-on-host: extra-repo", output)
        self.assertIn("status: ok", output)

    def test_without_host_flag_no_inventory(self):
        code, output = run_cli(
            ["doctor", "--dry-run", "--instance", str(EXAMPLE_INSTANCE)]
        )
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
        self.assertEqual(code, 1, output)
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

        self.assertEqual(code, 0)
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
            code, output = run_cli(
                ["doctor", "--dry-run", "--instance", str(EXAMPLE_INSTANCE), "--host"]
            )
        finally:
            cli.LiveHostSource = original

        self.assertEqual(code, 1, output)
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
                code, output = run_cli(
                    ["doctor", "--dry-run", "--instance", str(instance), "--host"]
                )
            finally:
                cli.LiveHostSource = original

        self.assertEqual(code, 1, output)
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
            code, output = run_cli(
                ["doctor", "--dry-run", "--instance", str(instance), "--host"]
            )

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

        self.assertEqual(code, 0, output)
        self.assertNotIn(secret, output)
        self.assertIn("host inventory: read-only", output)


if __name__ == "__main__":
    unittest.main()
