from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

from secretary.cli import main
from secretary.host import (
    Expectations,
    FixtureHostSource,
    HostInventory,
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
        actual = source.collect(Expectations())
        self.assertEqual(actual.projects, {"example-project", "stray-project"})
        self.assertEqual(actual.units, {"secretary-pipeline", "secretary-retro"})
        self.assertEqual(actual.orca_repos, {"example-project", "secretary", "extra-repo"})

    def test_missing_files_yield_empty_sets(self):
        source = FixtureHostSource(REPO_ROOT / "tests" / "fixtures" / "does-not-exist")
        actual = source.collect(Expectations())
        self.assertEqual(actual.projects, set())
        self.assertEqual(actual.units, set())
        self.assertEqual(actual.orca_repos, set())


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
