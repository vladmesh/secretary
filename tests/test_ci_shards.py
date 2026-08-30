from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.ci_test_shards import (
    FAST_MODULES,
    SUITES,
    ManifestError,
    fast_environment,
    load_manifest,
    main,
    modules,
    run_bounded,
    run_fast,
    validate_fast_profile,
)


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

    def test_fast_profile_is_a_fixed_narrow_hermetic_module_list(self) -> None:
        root = Path(__file__).resolve().parents[1]

        validate_fast_profile(root)

        self.assertEqual(
            FAST_MODULES,
            (
                "tests.test_hermetic_kanboard",
                "tests.test_hermetic_orca",
                "tests.test_hermetic_pipeline_state",
            ),
        )

    def test_fast_profile_rejects_a_missing_declared_module_before_launch(self) -> None:
        root = Path(__file__).resolve().parents[1]

        with (
            patch("scripts.ci_test_shards.FAST_MODULES", ("tests.test_missing",)),
            self.assertRaisesRegex(ManifestError, "missing test module"),
        ):
            validate_fast_profile(root)

    def test_fast_action_skips_the_seven_suite_manifest(self) -> None:
        with (
            patch("scripts.ci_test_shards.load_manifest") as manifest,
            patch("scripts.ci_test_shards.run_fast", return_value=0) as fast,
        ):
            self.assertEqual(main(["--fast"]), 0)

        manifest.assert_not_called()
        fast.assert_called_once()

    def test_fast_runner_passes_only_the_fixed_modules_to_its_child(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with patch("scripts.ci_test_shards.run_bounded", return_value=0) as bounded:
            self.assertEqual(run_fast(root), 0)

        command = bounded.call_args.args[0]
        self.assertEqual(command, [sys.executable, "-P", "-m", "unittest", "-v", *FAST_MODULES])

    def test_fast_environment_discards_ambient_credentials_and_installs_guards(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp)
            environment = fast_environment(root, fixture_root)
            for name in ("KANBOARD_API_TOKEN", "OPENAI_API_KEY", "AWS_ACCESS_KEY_ID"):
                self.assertNotIn(name, environment)
            for name in (
                "HOME",
                "TA_CODEX_HOME",
                "TA_PIPELINE_STATE_DIR",
                "TEMP",
                "TMP",
                "TMPDIR",
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
            ):
                self.assertTrue(Path(environment[name]).is_relative_to(fixture_root), name)
            self.assertEqual(environment["PATH"], os.defpath)

            network = subprocess.run(
                [sys.executable, "-c", "import socket; socket.create_connection(('127.0.0.1', 1))"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            command = subprocess.run(
                [sys.executable, "-c", "import subprocess; subprocess.run(['docker', 'info'])"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(network.returncode, 0)
        self.assertIn("fast test profile forbids network access", network.stderr)
        self.assertNotEqual(command.returncode, 0)
        self.assertIn("fast test profile forbids external command execution", command.stderr)

    def test_bounded_runner_stops_and_reaps_a_timed_out_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            command = [
                sys.executable,
                "-c",
                (
                    "import os, time; from pathlib import Path; "
                    f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
                ),
            ]
            started = time.monotonic()
            with redirect_stderr(StringIO()) as stderr:
                result = run_bounded(
                    command,
                    root=Path(tmp),
                    environment=dict(os.environ),
                    timeout_seconds=0.5,
                )
            elapsed = time.monotonic() - started

            pid = int(pid_file.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

        self.assertEqual(result, 124)
        self.assertLess(elapsed, 2)
        self.assertIn("timed out after 0.5 seconds; test child was stopped", stderr.getvalue())

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
