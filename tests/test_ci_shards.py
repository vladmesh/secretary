from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from scripts.ci_test_shards import (
    CHANGED_LINES_JSON_NAME,
    COVERAGE_JSON_NAME,
    EVIDENCE_FILES,
    FAST_MODULES,
    SUITES,
    BoundedTee,
    CheckoutStatusError,
    CoverageError,
    ManifestError,
    SuiteEvidence,
    TestRecord,
    _changed_line_report,
    _read_evidence,
    _suite_coverage_data,
    _summary,
    _write_evidence,
    aggregate_coverage,
    aggregate_evidence,
    fast_environment,
    load_manifest,
    main,
    modules,
    report_summary,
    run_bounded,
    run_fast,
    run_reported_suite,
    run_suite_with_evidence,
    validate_fast_profile,
)

CANDIDATE_SHA = "a" * 40


class CiTestSuiteManifestTests(unittest.TestCase):
    def _commit_checkout(self, root: Path) -> None:
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "CI test"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "ci-test@example.invalid"], check=True
        )
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "--quiet", "-m", "fixture"], check=True)

    def _run_temporary_suite(self, root: Path, report_dir: Path) -> int:
        grouped = {suite: ["tests/test_passing.py"] for suite in SUITES}
        loaded_tests = {
            name: module
            for name, module in list(sys.modules.items())
            if name == "tests" or name.startswith("tests.")
        }
        for name in loaded_tests:
            del sys.modules[name]
        try:
            with (
                patch.object(sys, "dont_write_bytecode", True),
                redirect_stdout(StringIO()),
                patch("scripts.ci_test_shards.load_manifest", return_value=grouped),
            ):
                return run_suite_with_evidence(root, "unit", report_dir, CANDIDATE_SHA)
        finally:
            for name in list(sys.modules):
                if name == "tests" or name.startswith("tests."):
                    del sys.modules[name]
            sys.modules.update(loaded_tests)

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

    def test_workflow_publishes_and_preserves_reported_suite_evidence(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )

        for suite in SUITES:
            self.assertIn(suite, workflow)
        candidate_sha = "${{ github.event.pull_request.head.sha || github.sha }}"
        self.assertIn(f'--candidate-sha "{candidate_sha}"', workflow)
        self.assertNotIn('--candidate-sha "$GITHUB_SHA"', workflow)
        self.assertIn(
            f"name: ci-evidence-${{{{ matrix.suite }}}}-{candidate_sha}",
            workflow,
        )
        self.assertIn(
            f"""      - name: Run reported suite
        id: run_suite
        continue-on-error: true
        run: >-
          mkdir -p "$RUNNER_TEMP/ci-coverage/${{{{ matrix.suite }}}}"
          &&
          python3 -m coverage run
          --data-file "$RUNNER_TEMP/ci-coverage/${{{{ matrix.suite }}}}/coverage.${{{{ matrix.suite }}}}"
          scripts/ci_test_shards.py --suite "${{{{ matrix.suite }}}}"
          --report-dir "$RUNNER_TEMP/ci-evidence/${{{{ matrix.suite }}}}"
          --candidate-sha "{candidate_sha}""",
            workflow,
        )
        self.assertIn(
            """      - name: Write suite evidence summary
        if: ${{ always() }}
        continue-on-error: true
        run: >-
          python3 scripts/ci_test_shards.py --summary
          --report-dir "$RUNNER_TEMP/ci-evidence/${{ matrix.suite }}"
          >> "$GITHUB_STEP_SUMMARY""",
            workflow,
        )
        self.assertIn(
            f"""      - name: Upload suite JUnit and bounded log
        if: ${{{{ always() }}}}
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: ci-evidence-${{{{ matrix.suite }}}}-{candidate_sha}
          path: ${{{{ runner.temp }}}}/ci-evidence/${{{{ matrix.suite }}}}
          if-no-files-found: error
          retention-days: 14""",
            workflow,
        )
        self.assertIn(
            """      - name: Preserve suite result
        if: ${{ always() }}
        run: >-
          python3 scripts/ci_test_shards.py --summary
          --report-dir "$RUNNER_TEMP/ci-evidence/${{ matrix.suite }}"
          > /dev/null""",
            workflow,
        )
        self.assertIn(
            f"""      - name: Upload raw suite coverage
        if: ${{{{ always() }}}}
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: ci-coverage-${{{{ matrix.suite }}}}-{candidate_sha}
          path: ${{{{ runner.temp }}}}/ci-coverage/${{{{ matrix.suite }}}}/coverage.${{{{ matrix.suite }}}}
          if-no-files-found: error""",
            workflow,
        )

    def test_workflow_aggregate_downloads_and_classifies_all_suite_evidence(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            """  test:
    name: test
    if: ${{ always() }}
    needs: test_suites""",
            workflow,
        )
        self.assertIn(
            """      - name: Download suite evidence
        if: ${{ always() }}
        continue-on-error: true
        uses: actions/download-artifact@v4
        with:
          pattern: ci-evidence-*
          path: ${{ runner.temp }}/ci-evidence
          merge-multiple: false""",
            workflow,
        )
        self.assertIn(
            """      - name: Require and classify every test suite
        id: suite_aggregate
        if: ${{ always() }}
        continue-on-error: true
        env:
          SUITES_RESULT: ${{ needs.test_suites.result }}
        run: >-
          python3 scripts/ci_test_shards.py --aggregate
          --evidence-dir "$RUNNER_TEMP/ci-evidence"
          --needs-result "$SUITES_RESULT"
          >> "$GITHUB_STEP_SUMMARY""",
            workflow,
        )
        self.assertIn("pattern: ci-coverage-*", workflow)
        self.assertIn("--coverage-aggregate", workflow)
        self.assertIn('--base-sha "$BASE_SHA"', workflow)
        self.assertIn(
            "name: ci-coverage-combined-${{ github.event.pull_request.head.sha || github.sha }}", workflow
        )
        self.assertIn("name: ci-coverage-baseline-${{ github.sha }}", workflow)
        self.assertIn("## Main coverage baseline", workflow)
        self.assertIn("fetch-depth: 0", workflow)

    def _coverage_payload(self) -> dict[str, object]:
        return {
            "meta": {"branch_coverage": True},
            "files": {
                "src/secretary/example.py": {
                    "executed_lines": [2, 4],
                    "missing_lines": [3],
                    "excluded_lines": [5],
                    "executed_branches": [[2, 4]],
                    "missing_branches": [[2, 3]],
                    "summary": {"num_branches": 2},
                }
            },
        }

    def test_coverage_aggregate_combines_one_named_datum_per_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            for suite in SUITES:
                path = raw / f"ci-coverage-{suite}-{CANDIDATE_SHA}" / f"coverage.{suite}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"coverage data")
            output = root / "combined"

            def write_native_json(command: list[str], _root: Path) -> None:
                if "json" in command:
                    Path(command[command.index("-o") + 1]).write_text(
                        json.dumps(self._coverage_payload()), encoding="utf-8"
                    )

            with (
                patch("scripts.ci_test_shards._candidate_checkout"),
                patch("scripts.ci_test_shards._run_coverage", side_effect=write_native_json),
            ):
                self.assertEqual(aggregate_coverage(root, raw, output, CANDIDATE_SHA, None), 0)

            combined = json.loads((output / COVERAGE_JSON_NAME).read_text(encoding="utf-8"))
            changed = json.loads((output / CHANGED_LINES_JSON_NAME).read_text(encoding="utf-8"))

        self.assertEqual(combined["candidate_sha"], CANDIDATE_SHA)
        self.assertEqual(combined["source_roots"], ["src/secretary", "src/triggered_agents"])
        self.assertIn("executed_branches", combined["coverage"]["files"]["src/secretary/example.py"])
        self.assertFalse(changed["applicable"])
        self.assertIn("no pull-request base SHA", changed["reason"])

    def test_coverage_aggregate_rejects_missing_or_uncombinable_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            with self.assertRaisesRegex(CoverageError, "unit"):
                _suite_coverage_data(raw, CANDIDATE_SHA)
            for suite in SUITES:
                path = raw / f"ci-coverage-{suite}-{CANDIDATE_SHA}" / f"coverage.{suite}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"coverage data")
            with (
                patch("scripts.ci_test_shards._candidate_checkout"),
                patch("scripts.ci_test_shards._run_coverage", side_effect=CoverageError("incompatible data")),
            ):
                self.assertEqual(aggregate_coverage(root, raw, root / "combined", CANDIDATE_SHA, None), 3)

    def test_changed_line_report_classifies_coverage_and_non_executable_lines(self) -> None:
        report = _changed_line_report(
            self._coverage_payload(),
            {"src/secretary/example.py": [2, 3, 5, 9]},
            base_sha="b" * 40,
            candidate_sha=CANDIDATE_SHA,
        )

        self.assertTrue(report["applicable"])
        self.assertEqual(
            [entry["classification"] for entry in report["lines"]],
            ["covered", "missed", "excluded", "not_executable"],
        )

    def test_coverage_configuration_enables_branch_scope_without_a_threshold(self) -> None:
        config = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("[tool.coverage.run]", config)
        self.assertIn("branch = true", config)
        self.assertIn("relative_files = true", config)
        self.assertIn('source = ["src/secretary", "src/triggered_agents"]', config)
        self.assertNotIn("fail_under", config)

    def _write_report(self, directory: Path, evidence: SuiteEvidence) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        log = BoundedTee(StringIO(), directory / "test-output.log")
        log.write("test output\n")
        _write_evidence(directory, evidence, log)

    def _evidence(self, suite: str, outcome: str = "success") -> SuiteEvidence:
        return SuiteEvidence(
            suite,
            CANDIDATE_SHA,
            outcome,
            {
                "collected": 3,
                "passed": 2,
                "failed": 1 if outcome == "product_failure" else 0,
                "error": 0,
                "skipped": 0,
            },
            1.25,
            [TestRecord("tests.example.Case.test_slow", "tests.example.Case", "test_slow", 1.0)],
            ["tests.example.Case.test_slow: tests/example.py:12"] if outcome == "product_failure" else [],
        )

    def test_reported_runner_writes_exact_paths_and_product_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "sample_suite.py"
            module.write_text(
                "import unittest\n"
                "class Sample(unittest.TestCase):\n"
                "    def test_pass(self): self.assertTrue(True)\n"
                "    def test_fail(self): self.fail('broken')\n",
                encoding="utf-8",
            )
            report_dir = root / "artifacts" / "unit"
            report_dir.mkdir(parents=True)
            sys.path.insert(0, str(root))
            try:
                with patch("scripts.ci_test_shards.modules", return_value=["sample_suite"]):
                    log = BoundedTee(StringIO(), report_dir / "test-output.log")
                    evidence = run_reported_suite("unit", ["ignored"], CANDIDATE_SHA, log)
                    _write_evidence(report_dir, evidence, log)
            finally:
                sys.path.remove(str(root))
                sys.modules.pop("sample_suite", None)

            self.assertEqual(evidence.outcome, "product_failure")
            self.assertEqual(
                evidence.counts, {"collected": 2, "passed": 1, "failed": 1, "error": 0, "skipped": 0}
            )
            self.assertEqual({path.name for path in report_dir.iterdir()}, set(EVIDENCE_FILES))
            self.assertEqual(_read_evidence(report_dir).candidate_sha, CANDIDATE_SHA)
            junit = ElementTree.parse(report_dir / "junit.xml").getroot()
            self.assertEqual(junit.attrib["failures"], "1")
            self.assertEqual(len(junit.findall("testcase")), 2)
            self.assertIn("sample_suite.Sample.test_fail", evidence.failure_locations[0])

    def test_reported_runner_writes_success_evidence_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "checkout"
            root.mkdir()
            tests = root / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_passing.py").write_text(
                "import unittest\n"
                "class Passing(unittest.TestCase):\n"
                "    def test_pass(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            self._commit_checkout(root)
            report_dir = temporary / "runner-temp" / "unit"
            self.assertEqual(self._run_temporary_suite(root, report_dir), 0)

            summary = StringIO()
            with redirect_stdout(summary):
                self.assertEqual(report_summary(report_dir), 0)
            evidence = _read_evidence(report_dir)

        self.assertEqual(evidence.outcome, "success")
        self.assertIsNotNone(evidence.checkout_status)
        self.assertFalse(evidence.checkout_status.changed)
        self.assertIn("Candidate SHA", summary.getvalue())
        self.assertIn("collected 1, passed 1", summary.getvalue())
        self.assertIn("Checkout status: unchanged", summary.getvalue())

    def test_reported_runner_detects_a_tracked_checkout_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "checkout"
            root.mkdir()
            tests = root / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (root / "tracked.txt").write_text("before\n", encoding="utf-8")
            (tests / "test_passing.py").write_text(
                "import unittest\n"
                "from pathlib import Path\n"
                "class Passing(unittest.TestCase):\n"
                "    def test_pass(self):\n"
                "        Path('tracked.txt').write_text('after\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            self._commit_checkout(root)

            report_dir = temporary / "runner-temp" / "unit"
            self.assertEqual(self._run_temporary_suite(root, report_dir), 3)
            evidence = _read_evidence(report_dir)

        self.assertEqual(evidence.outcome, "infrastructure_failure")
        self.assertIsNotNone(evidence.checkout_status)
        self.assertTrue(evidence.checkout_status.changed)
        self.assertTrue(any("tracked.txt" in entry for entry in evidence.checkout_status.changed_entries))

    def test_reported_runner_detects_an_untracked_product_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "checkout"
            root.mkdir()
            tests = root / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_passing.py").write_text(
                "import unittest\n"
                "from pathlib import Path\n"
                "class Passing(unittest.TestCase):\n"
                "    def test_pass(self):\n"
                "        Path('product-artifact.txt').write_text('artifact\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            self._commit_checkout(root)

            report_dir = temporary / "runner-temp" / "unit"
            self.assertEqual(self._run_temporary_suite(root, report_dir), 3)
            evidence = _read_evidence(report_dir)
            summary = _summary(evidence)

        self.assertEqual(evidence.outcome, "infrastructure_failure")
        self.assertEqual(evidence.counts["passed"], 1)
        self.assertIsNotNone(evidence.checkout_status)
        self.assertTrue(evidence.checkout_status.changed)
        self.assertTrue(
            any("product-artifact.txt" in entry for entry in evidence.checkout_status.changed_entries)
        )
        self.assertIn("Checkout status: changed", summary)
        self.assertIn("product-artifact.txt", summary)

    def test_checkout_contamination_overrides_a_product_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "checkout"
            root.mkdir()
            tests = root / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_passing.py").write_text(
                "import unittest\n"
                "from pathlib import Path\n"
                "class Failing(unittest.TestCase):\n"
                "    def test_failure(self):\n"
                "        Path('product-artifact.txt').write_text('artifact\\n', encoding='utf-8')\n"
                "        self.fail('product failure')\n",
                encoding="utf-8",
            )
            self._commit_checkout(root)

            report_dir = temporary / "runner-temp" / "unit"
            self.assertEqual(self._run_temporary_suite(root, report_dir), 3)
            evidence = _read_evidence(report_dir)

        self.assertEqual(evidence.outcome, "infrastructure_failure")
        self.assertIn("test outcome before checkout verification: product_failure", evidence.detail)
        self.assertTrue(any("test_failure" in location for location in evidence.failure_locations))

    def test_unavailable_checkout_status_is_an_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            root.mkdir()
            tests = root / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_passing.py").write_text("", encoding="utf-8")
            self._commit_checkout(root)

            report_dir = Path(tmp) / "runner-temp" / "unit"
            with patch(
                "scripts.ci_test_shards._checkout_snapshot",
                side_effect=CheckoutStatusError("git status command failed"),
            ):
                self.assertEqual(self._run_temporary_suite(root, report_dir), 3)
            evidence = _read_evidence(report_dir)

        self.assertEqual(evidence.outcome, "infrastructure_failure")
        self.assertIn("checkout status unavailable before suite execution", evidence.failure_locations)

    def test_bounded_log_keeps_a_marker_after_its_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test-output.log"
            log = BoundedTee(StringIO(), path, maximum_bytes=5)
            log.write("abcdefgh")
            log.close()
            content = path.read_text(encoding="utf-8")

        self.assertTrue(log.truncated)
        self.assertTrue(content.startswith("abcde"))
        self.assertIn("truncated at 1000000 bytes", content)

    def test_reported_manifest_or_report_failure_is_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "report"
            with (
                redirect_stdout(StringIO()),
                patch("scripts.ci_test_shards.load_manifest", side_effect=ManifestError("bad manifest")),
            ):
                self.assertEqual(run_suite_with_evidence(Path(tmp), "unit", report_dir, CANDIDATE_SHA), 3)

            evidence = _read_evidence(report_dir)
            self.assertEqual(evidence.outcome, "infrastructure_failure")
            (report_dir / "junit.xml").unlink()
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(report_summary(report_dir), 3)

    def test_cancelled_run_is_recorded_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = BoundedTee(StringIO(), Path(tmp) / "test-output.log")
            with patch("scripts.ci_test_shards.unittest.TextTestRunner.run", side_effect=KeyboardInterrupt):
                evidence = run_reported_suite("unit", ["tests/test_any.py"], CANDIDATE_SHA, log)
            log.close()

        self.assertEqual(evidence.outcome, "cancelled")

    def test_aggregate_separates_product_infrastructure_cancellation_and_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for suite in SUITES:
                self._write_report(root / suite, self._evidence(suite))
            self._write_report(root / "unit", self._evidence("unit", "product_failure"))
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(aggregate_evidence(root, "failure"), 1)

            (root / "component" / "report.json").unlink()
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "failure"), 3)
            for suite in SUITES:
                report = root / suite / "report.json"
                if report.exists():
                    report.unlink()
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "cancelled"), 130)

            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "skipped"), 0)


if __name__ == "__main__":
    unittest.main()
