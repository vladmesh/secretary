from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.ci_test_shards import ManifestError, SUITES, load_manifest, main, modules


class CiTestSuiteManifestTests(unittest.TestCase):
    def test_live_manifest_partitions_every_top_level_test_once(self) -> None:
        root = Path(__file__).resolve().parents[1]

        grouped = load_manifest(root)

        declared = [path for suite in SUITES for path in grouped[suite]]
        discovered = sorted(path.relative_to(root).as_posix() for path in root.glob("tests/test_*.py"))
        self.assertEqual(sorted(declared), discovered)
        self.assertEqual(len(declared), len(set(declared)))
        self.assertTrue(all(grouped[suite] for suite in SUITES))

    def test_manifest_rejects_an_omission_duplicate_stale_path_and_unknown_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            manifest = tests / "ci-shards.txt"
            paths = {suite: f"tests/test_{number}.py" for number, suite in enumerate(SUITES)}
            for relative in paths.values():
                (root / relative).write_text("", encoding="utf-8")

            manifest.write_text(
                "\n".join(f"{suite} {relative}" for suite, relative in paths.items()) + "\n",
                encoding="utf-8",
            )
            (tests / "test_omitted.py").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "unclaimed tests: tests/test_omitted.py"):
                load_manifest(root, manifest)
            (tests / "test_omitted.py").unlink()

            manifest.write_text(
                manifest.read_text(encoding="utf-8") + f"unit {paths['unit']}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManifestError, "already belongs"):
                load_manifest(root, manifest)

            manifest.write_text(
                "\n".join(f"{suite} {relative}" for suite, relative in paths.items())
                + "\nunit tests/test_stale.py\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManifestError, "missing declared tests: tests/test_stale.py"):
                load_manifest(root, manifest)

            manifest.write_text(
                "\n".join(f"{suite} {relative}" for suite, relative in paths.items())
                + "\nunsupported tests/test_unknown.py\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManifestError, "unknown suite 'unsupported'"):
                load_manifest(root, manifest)

    def test_runner_executes_every_declared_suite(self) -> None:
        grouped = {suite: [f"tests/test_{number}.py"] for number, suite in enumerate(SUITES)}

        with (
            patch("scripts.ci_test_shards.load_manifest", return_value=grouped),
            patch("scripts.ci_test_shards.subprocess.call", return_value=0) as call,
        ):
            for suite in SUITES:
                self.assertEqual(main(["--suite", suite]), 0)
                call.assert_called_once()
                command = call.call_args.args[0]
                self.assertEqual(command[-1], modules(grouped[suite])[0])
                call.reset_mock()

    def test_runner_rejects_an_invalid_manifest_before_starting_a_suite(self) -> None:
        with (
            patch(
                "scripts.ci_test_shards.load_manifest",
                side_effect=ManifestError("unclaimed tests: tests/test_new.py"),
            ),
            patch("scripts.ci_test_shards.subprocess.call") as call,
            redirect_stderr(StringIO()),
        ):
            self.assertEqual(main(["--suite", "unit"]), 2)
            call.assert_not_called()

    def test_runner_rejects_an_undeclared_suite_name(self) -> None:
        with self.assertRaises(SystemExit) as error, redirect_stderr(StringIO()):
            main(["--suite", "unsupported"])
        self.assertEqual(error.exception.code, 2)

    def test_runner_propagates_a_suite_failure(self) -> None:
        grouped = {suite: ["tests/test_ci_shards.py"] for suite in SUITES}
        with (
            patch("scripts.ci_test_shards.load_manifest", return_value=grouped),
            patch("scripts.ci_test_shards.subprocess.call", return_value=1),
        ):
            self.assertEqual(main(["--suite", "unit"]), 1)

    def test_paths_become_unittest_module_names(self) -> None:
        self.assertEqual(modules(["tests/test_ci_shards.py"]), ["tests.test_ci_shards"])

    def test_workflow_keeps_a_test_aggregator_after_all_suites(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )

        for suite in SUITES:
            self.assertIn(suite, workflow)
        self.assertIn("python3 scripts/ci_test_shards.py --suite", workflow)
        self.assertIn("needs: test_suites", workflow)
        self.assertIn("name: test", workflow)


if __name__ == "__main__":
    unittest.main()
