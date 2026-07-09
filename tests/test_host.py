from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

import secretary.cli as cli
from secretary.cli import main
from secretary.host import (
    CollectResult,
    Expectations,
    FixtureHostSource,
    HostInventory,
    LiveHostSource,
    _CmdResult as CmdResult,
    build_expectations,
    inventory,
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
        self.assertEqual(actual.units, {"secretary-pipeline", "secretary-retro"})
        self.assertEqual(actual.orca_repos, {"example-project", "secretary", "extra-repo"})

    def test_missing_files_yield_empty_sets(self):
        source = FixtureHostSource(REPO_ROOT / "tests" / "fixtures" / "does-not-exist")
        result = source.collect(Expectations())
        self.assertEqual(result.errors, {})
        self.assertEqual(result.inventory.projects, set())
        self.assertEqual(result.inventory.units, set())
        self.assertEqual(result.inventory.orca_repos, set())


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
        expected = Expectations(units={"secretary-pipeline"}, unit_prefix="secretary-")
        result = host.collect(expected)
        self.assertNotIn("units", result.errors)
        self.assertEqual(result.inventory.units, set())
        diff = inventory(expected, result.inventory)
        self.assertEqual(diff["units"].missing_on_host, ["secretary-pipeline"])

    def test_systemctl_stderr_is_a_failure(self):
        host = self._host(
            {
                "systemctl": _cmd(returncode=1, stdout="", stderr="Failed to connect to bus"),
                "orca": _cmd(stdout=""),
            }
        )
        result = host.collect(Expectations(units={"u"}, unit_prefix="u-"))
        self.assertIn("units", result.errors)

    def test_orca_non_zero_exit_is_a_failure(self):
        host = self._host(
            {
                "systemctl": _cmd(stdout=""),
                "orca": _cmd(returncode=1, stderr="cannot reach daemon"),
            }
        )
        result = host.collect(Expectations(orca_repos={"r"}))
        self.assertIn("orca repos", result.errors)

    def test_declared_projects_without_root_is_unavailable(self):
        expected = Expectations(projects={"a"}, projects_root="")
        projects, reason = LiveHostSource()._projects(expected)
        self.assertEqual(projects, set())
        self.assertTrue(reason)

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
        # units: one of each outcome
        self.assertIn("units:\n  matched: secretary-pipeline", output)
        self.assertIn("missing-on-host: secretary-curator", output)
        self.assertIn("unmanaged-on-host: secretary-retro", output)
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
