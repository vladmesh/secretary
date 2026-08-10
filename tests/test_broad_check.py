"""secretary-1406: a broad check must leave structured evidence behind.

The failure these tests pin is cheap to describe and expensive to repeat: a worker runs the broad
suite, the pane scrolls, and the only way back to "did it pass, and how many tests" is another
ninety-second run over code that did not change. The receipt is that answer, so the interesting
cases are all the ways it could quietly lie — a truncated artifact, a killed run, a receipt written
before the last edit — and every one of them has to read as "not usable" rather than as a summary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from secretary import broad_check
from secretary.broad_check import (
    BroadCheckError,
    CheckSpec,
    content_identity,
    load_receipt,
    parse_unittest_summary,
    receipt_path,
    run_broad_check,
    usable_receipt,
)
from secretary.cli import main


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _run_main(argv: list[str]) -> dict:
    """Run one CLI command, capturing the JSON document it prints on stdout."""
    stdout = StringIO()
    with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", StringIO()):
        main(argv)
    return json.loads(stdout.getvalue())


class BroadCheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name) / "workspace"
        self.root.mkdir()
        self.scripts = Path(self.tmpdir.name) / "scripts"
        self.scripts.mkdir()
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "worker@example.invalid")
        _git(self.root, "config", "user.name", "worker")
        # `__pycache__/` is ignored here for the same reason every real checkout ignores it:
        # a Python check writes bytecode as it runs, and that is not a change to the code.
        (self.root / ".gitignore").write_text("/state/\n__pycache__/\n", encoding="utf-8")
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        # A candidate workspace is a checkout of the project under check, and reuse is only ever
        # authorized for a check process that imported the project from here.
        (self.root / "secretary").mkdir()
        (self.root / "secretary" / "__init__.py").write_text("", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "base")
        self.stream = StringIO()

    def _script(self, name: str, body: str) -> str:
        path = self.scripts / name
        path.write_text(body, encoding="utf-8")
        return f"{sys.executable} {path}"

    def _run(self, command, **kwargs):
        return run_broad_check(command, root=self.root, stream=self.stream, **kwargs)

    def _suite(self, name: str, body: str, args: tuple[str, ...] = ()) -> CheckSpec:
        """A module-shaped check: the standard shape, and the only one that attests an import."""
        (self.root / f"{name}.py").write_text(body, encoding="utf-8")
        return CheckSpec.for_module(name, args)


class RunAndCaptureTests(BroadCheckTestCase):
    def test_large_interleaved_output_keeps_its_order_and_bounds_the_artifact(self) -> None:
        # One pipe for both streams is the whole point: a failing test's traceback on stderr has
        # to stay next to the dots on stdout that say which test it was.
        command = self._script(
            "interleave.py",
            "import sys\n"
            "for index in range(4000):\n"
            "    sys.stdout.write('out-%d\\n' % index); sys.stdout.flush()\n"
            "    sys.stderr.write('err-%d\\n' % index); sys.stderr.flush()\n",
        )
        exit_code, receipt = self._run(command)

        self.assertEqual(exit_code, 0)
        streamed = self.stream.getvalue().splitlines()
        self.assertEqual(len(streamed), 8000)
        self.assertEqual(streamed[:4], ["out-0", "err-0", "out-1", "err-1"])
        self.assertEqual(streamed[-2:], ["out-3999", "err-3999"])
        self.assertEqual(receipt["output_lines"], 8000)
        self.assertGreater(receipt["output_bytes"], 60000)

        # The receipt itself stays small whatever the suite printed, and the tail it does keep is
        # the end of the run, in order, with no half line pretending to be a whole one.
        self.assertTrue(receipt["tail_truncated"])
        self.assertLessEqual(len(receipt["tail"].encode("utf-8")), broad_check.TAIL_BYTES)
        tail_lines = receipt["tail"].splitlines()
        self.assertLessEqual(len(tail_lines), broad_check.TAIL_LINES)
        self.assertEqual(tail_lines[-2:], ["out-3999", "err-3999"])
        self.assertTrue(all(line.startswith(("out-", "err-")) for line in tail_lines))
        self.assertLess(receipt_path(self.root, command).stat().st_size, 32768)

    def test_one_unbroken_line_cannot_grow_the_receipt(self) -> None:
        command = self._script(
            "onebigline.py", "import sys\nsys.stdout.write('x' * 500000)\n"
        )
        _, receipt = self._run(command)

        self.assertEqual(receipt["output_bytes"], 500000)
        self.assertLessEqual(len(receipt["tail"].encode("utf-8")), broad_check.TAIL_BYTES)

    def test_nonzero_exit_is_preserved_and_remains_usable_evidence(self) -> None:
        suite = self._suite(
            "redsuite",
            "import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n",
        )
        exit_code, receipt = self._run(suite)

        self.assertEqual(exit_code, 3)
        self.assertEqual(receipt["exit_code"], 3)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["verdict"], "failed")
        self.assertIn("boom", receipt["tail"])
        # A red answer is a concrete answer: the point of the receipt is that nobody reruns the
        # suite to rediscover it.
        lookup = usable_receipt(self.root, suite)
        self.assertTrue(lookup.usable)
        self.assertEqual(lookup.receipt["verdict"], "failed")

    def test_a_shell_shape_keeps_its_exit_status_while_attesting_no_import(self) -> None:
        exit_code, receipt = self._run("echo boom 1>&2; exit 3")

        self.assertEqual(exit_code, 3)
        self.assertEqual(receipt["command_shape"], "shell")
        self.assertIn("boom", receipt["tail"])
        self.assertEqual(receipt["project_provenance"]["origin"], "unobservable")

    def test_a_killed_command_is_incomplete_and_never_usable(self) -> None:
        exit_code, receipt = self._run("echo starting; kill -9 $$")

        self.assertEqual(exit_code, -9)
        self.assertEqual(receipt["signal"], 9)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["verdict"], "unknown")
        self.assertIn("killed by signal 9", receipt["incomplete_reason"])
        self.assertIn("starting", receipt["tail"])

        lookup = usable_receipt(self.root, "echo starting; kill -9 $$")
        self.assertFalse(lookup.usable)
        self.assertIn("did not finish", lookup.reason)

    def test_a_command_that_hangs_silently_is_stopped_and_recorded_as_incomplete(self) -> None:
        command = self._script("hang.py", "import time\ntime.sleep(30)\n")
        exit_code, receipt = self._run(command, timeout_seconds=0.5)

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertIn("timed out", receipt["incomplete_reason"])
        self.assertFalse(usable_receipt(self.root, command).usable)

    def test_a_module_check_reports_the_import_its_own_process_made(self) -> None:
        suite = self._suite("quietsuite", "print('done')\n")

        _, receipt = self._run(suite)
        provenance = receipt["project_provenance"]

        self.assertEqual(provenance["origin"], "check-process")
        self.assertEqual(provenance["cwd"], str(self.root.resolve()))
        self.assertTrue(provenance["inside_workspace"])
        self.assertTrue(
            provenance["imported_secretary"].startswith(str(self.root.resolve())),
            provenance["imported_secretary"],
        )

    def test_the_receipt_records_where_and_when_the_run_happened(self) -> None:
        _, receipt = self._run("echo timed")

        self.assertEqual(receipt["cwd"], str(self.root.resolve()))
        self.assertEqual(
            receipt["command_or_check_set_digest"], CheckSpec.for_shell("echo timed").digest
        )
        self.assertLessEqual(receipt["started_at"], receipt["ended_at"])
        self.assertGreaterEqual(receipt["duration_seconds"], 0.0)
        self.assertEqual(
            receipt["content_identity"]["head_sha"], content_identity(self.root).head_sha
        )


class ParsedVerdictTests(unittest.TestCase):
    def test_a_green_unittest_summary_yields_counts_and_skips(self) -> None:
        parsed = parse_unittest_summary("....s\nRan 1421 tests in 94.512s\n\nOK (skipped=3)\n")

        self.assertEqual(parsed["tests"], 1421)
        self.assertEqual(parsed["runner_duration_seconds"], 94.512)
        self.assertEqual(parsed["summary"], "OK")
        self.assertEqual(parsed["skipped"], 3)

    def test_a_red_unittest_summary_yields_failure_and_error_counts(self) -> None:
        parsed = parse_unittest_summary(
            "Ran 12 tests in 1.5s\n\nFAILED (failures=2, errors=1, skipped=4, "
            "expected failures=1)\n"
        )

        self.assertEqual(parsed["summary"], "FAILED")
        self.assertEqual(parsed["failures"], 2)
        self.assertEqual(parsed["errors"], 1)
        self.assertEqual(parsed["skipped"], 4)
        self.assertEqual(parsed["expected_failures"], 1)

    def test_a_runner_that_prints_no_summary_parses_to_nothing_rather_than_to_green(self) -> None:
        self.assertEqual(parse_unittest_summary("built 4 targets\n"), {})


class ReceiptIntegrityTests(BroadCheckTestCase):
    def test_a_truncated_or_edited_receipt_fails_closed(self) -> None:
        self._run("echo one")
        path = receipt_path(self.root, "echo one")
        good = path.read_text(encoding="utf-8")

        path.write_text(good[: len(good) // 2], encoding="utf-8")
        self.assertIsNone(load_receipt(path))
        self.assertFalse(usable_receipt(self.root, "echo one").usable)

        payload = json.loads(good)
        payload["verdict"] = "passed"
        payload["exit_code"] = 0
        payload["status"] = "complete"
        payload["tail"] = "OK"
        path.write_text(json.dumps(payload), encoding="utf-8")
        # Every field is individually plausible; the receipt is still refused because its own
        # digest no longer covers them.
        self.assertIsNone(load_receipt(path))
        lookup = usable_receipt(self.root, "echo one")
        self.assertFalse(lookup.usable)
        self.assertIn("no intact receipt", lookup.reason)

    def test_a_receipt_from_another_schema_is_not_read_as_this_one(self) -> None:
        self._run("echo one")
        path = receipt_path(self.root, "echo one")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = broad_check.SCHEMA_VERSION + 1
        payload["receipt_digest"] = broad_check._receipt_digest(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(load_receipt(path))

    def test_a_failed_publish_leaves_the_previous_receipt_intact(self) -> None:
        self._run("echo first; exit 0")
        path = receipt_path(self.root, "echo first; exit 0")
        before = load_receipt(path)
        self.assertIsNotNone(before)

        real_replace = os.replace

        def refuse(source, target, *args, **kwargs):
            if str(target) == str(path):
                raise OSError("disk full")
            return real_replace(source, target, *args, **kwargs)

        with mock.patch("secretary._fsutil.os.replace", side_effect=refuse):
            with self.assertRaises(BroadCheckError) as caught:
                self._run("echo first; exit 0")
        self.assertEqual(caught.exception.code, "receipt_unwritable")

        # The reader still sees the whole previous receipt, never a partial new one, and no
        # staged temporary is left behind to be mistaken for one.
        self.assertEqual(load_receipt(path), before)
        self.assertEqual([entry.name for entry in path.parent.iterdir()], [path.name])

    def test_a_receipt_is_refused_where_git_would_offer_to_commit_it(self) -> None:
        (self.root / ".gitignore").write_text("nothing-here\n", encoding="utf-8")

        with self.assertRaises(BroadCheckError) as caught:
            self._run("echo one")
        self.assertEqual(caught.exception.code, "receipt_not_ignored")
        self.assertFalse(receipt_path(self.root, "echo one").exists())

    def test_the_committed_ignore_rules_cover_the_receipt_directory(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        target = broad_check.receipt_path(repo_root, "python3 -m unittest")
        result = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-q", str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self.assertEqual(result.returncode, 0, f"{target} must stay git-ignored")


class DocumentedCommandTests(unittest.TestCase):
    """One documented form, or roles invent their own and the evidence stops being comparable."""

    def test_the_wrapper_is_documented_where_a_role_looks_for_it(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for name in ("CONTRIBUTING.md", "docs/OPERATIONS.md", "docs/PROTOCOLS.md"):
            text = (repo_root / name).read_text(encoding="utf-8")
            self.assertIn("check broad", text, name)

    def test_the_operator_reference_documents_reading_a_receipt_back(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "docs" / "OPERATIONS.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("python3 -m secretary check show --module", text)
        self.assertIn("state/checks/", text)

    def test_the_documented_shapes_say_which_one_attests_an_import(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        operations = (repo_root / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        protocols = (repo_root / "docs" / "PROTOCOLS.md").read_text(encoding="utf-8")

        self.assertIn("origin: unobservable", operations)
        self.assertIn("--module", operations)
        self.assertIn("attests no import", protocols)
        # Both documents have to state the trust boundary reuse actually enforces.
        self.assertIn("outside the candidate", operations)
        self.assertIn("resolved inside the candidate workspace", protocols)


class UnchangedContentReuseTests(BroadCheckTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Reuse is only ever offered for the standard module shape, so these tests use it.
        self.suite = self._suite("reusesuite", "print('suite ran')\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "suite")

    def test_an_unchanged_checkout_reuses_the_receipt_and_any_edit_invalidates_it(self) -> None:
        self._run(self.suite)
        lookup = usable_receipt(self.root, self.suite)
        self.assertTrue(lookup.usable)
        self.assertEqual(lookup.reason, "receipt describes this exact content")

        # Writing the receipt is itself a change to the directory; it must not invalidate the
        # receipt it just wrote, or reuse would never be possible at all.
        self.assertTrue(usable_receipt(self.root, self.suite).usable)

        (self.root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        stale = usable_receipt(self.root, self.suite)
        self.assertFalse(stale.usable)
        self.assertIn("content changed", stale.reason)

        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.assertTrue(usable_receipt(self.root, self.suite).usable)

    def test_committing_the_same_content_still_changes_the_identity(self) -> None:
        (self.root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self._run(self.suite)
        self.assertTrue(usable_receipt(self.root, self.suite).usable)

        _git(self.root, "commit", "-q", "-a", "-m", "second")
        # A new candidate SHA is precisely the case that opens a justified new run.
        self.assertFalse(usable_receipt(self.root, self.suite).usable)

    def test_a_new_untracked_file_changes_the_identity(self) -> None:
        self._run(self.suite)
        (self.root / "extra.py").write_text("HELPER = True\n", encoding="utf-8")

        self.assertFalse(usable_receipt(self.root, self.suite).usable)

    def test_a_check_that_writes_into_the_checkout_invalidates_its_own_receipt(self) -> None:
        writer = self._suite(
            "writersuite", "open('artefact.txt', 'w', encoding='utf-8').write('made\\n')\n"
        )
        self._run(writer)

        # Not a defect: the content the receipt described is not the content now on disk, and
        # saying so is the whole contract.
        lookup = usable_receipt(self.root, writer)
        self.assertFalse(lookup.usable)
        self.assertIn("content changed", lookup.reason)

    def test_a_receipt_for_another_command_is_never_offered_for_this_one(self) -> None:
        self._run(self.suite)

        other = usable_receipt(self.root, CheckSpec.for_module("othersuite"))
        self.assertFalse(other.usable)
        self.assertIsNone(other.receipt)

    def test_a_checkout_without_a_resolvable_identity_never_reuses_anything(self) -> None:
        bare = Path(self.tmpdir.name) / "not-a-repo"
        bare.mkdir()
        (bare / "baresuite.py").write_text("print('suite ran')\n", encoding="utf-8")
        (bare / "secretary").mkdir()
        (bare / "secretary" / "__init__.py").write_text("", encoding="utf-8")
        spec = CheckSpec.for_module("baresuite")
        exit_code, receipt = run_broad_check(spec, root=bare, stream=self.stream)

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["content_identity"], {"head_sha": "", "worktree_digest": ""})
        lookup = usable_receipt(bare, spec)
        self.assertFalse(lookup.usable)
        self.assertIn("no resolvable content identity", lookup.reason)


class ProvenanceHonestyTests(BroadCheckTestCase):
    """secretary-1406 review, BLOCKER-ACTUAL-IMPORT-PROVENANCE.

    Provenance used to come from a preflight probe the wrapper ran in the workspace before it
    started the check. That probe answers a different question than the one asked: the check may
    `cd` elsewhere, or reach a different interpreter or import path, and the receipt would still
    report the candidate worktree as the imported project — on a complete, passing, reusable
    receipt. Nothing may claim an import it did not observe.
    """

    def _outside_project(self) -> Path:
        outside = Path(self.tmpdir.name) / "elsewhere"
        (outside / "secretary").mkdir(parents=True)
        (outside / "secretary" / "__init__.py").write_text("", encoding="utf-8")
        return outside

    def test_a_shell_check_that_changes_directory_cannot_claim_the_candidate_checkout(self) -> None:
        outside = self._outside_project()
        command = (
            f"cd {outside}; {sys.executable} -c "
            "\"import secretary, sys; sys.stdout.write(secretary.__file__)\""
        )

        exit_code, receipt = self._run(command)

        self.assertEqual(exit_code, 0)
        # The check really did import the other checkout, and it really did pass.
        self.assertIn(str(outside), receipt["tail"])
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["verdict"], "passed")
        # The receipt claims no import at all, and above all not this workspace's.
        provenance = receipt["project_provenance"]
        self.assertEqual(provenance["origin"], "unobservable")
        self.assertEqual(provenance["imported_secretary"], "")
        self.assertFalse(provenance["inside_workspace"])
        self.assertNotIn(str(self.root.resolve()), json.dumps(provenance))
        # And it can never stand in for running the check again.
        lookup = usable_receipt(self.root, command)
        self.assertFalse(lookup.usable)
        self.assertIn("provenance was not observed", lookup.reason)

    def test_no_shell_check_is_ever_reusable_however_ordinary_it_looks(self) -> None:
        for command in ("true", f"cd / && {sys.executable} -c 'pass'", "echo ok"):
            with self.subTest(command=command):
                self._run(command)
                lookup = usable_receipt(self.root, command)
                self.assertFalse(lookup.usable)
                self.assertEqual(lookup.receipt["project_provenance"]["origin"], "unobservable")

    def test_an_import_from_outside_the_candidate_is_recorded_and_refused(self) -> None:
        """The end-to-end case the second review reproduced: a real subprocess, a real safe-path
        import environment, and an alternate checkout ordered before the candidate."""
        outside = self._outside_project()
        suite = self._suite(
            "outsidesuite", "import secretary\nprint(secretary.__file__)\n"
        )
        env = dict(
            os.environ,
            PYTHONSAFEPATH="1",
            PYTHONPATH=os.pathsep.join([str(outside), str(self.root)]),
        )

        _, receipt = self._run(suite, env=env)

        # The receipt tells the truth about what the check process imported...
        provenance = receipt["project_provenance"]
        self.assertEqual(provenance["origin"], "check-process")
        self.assertTrue(
            provenance["imported_secretary"].startswith(str(outside.resolve())),
            provenance["imported_secretary"],
        )
        self.assertFalse(provenance["inside_workspace"])
        self.assertIn(str(outside), receipt["tail"])
        # ...and precisely because that import was not this candidate, it cannot stand in for a
        # run of this candidate.
        lookup = usable_receipt(self.root, suite)
        self.assertFalse(lookup.usable)
        self.assertIn("outside this candidate workspace", lookup.reason)
        self.assertIsNone(lookup.authorized())

        # The ordinary candidate-inside run of the same standard shape stays reusable.
        _, inside_receipt = self._run(suite)
        self.assertTrue(inside_receipt["project_provenance"]["inside_workspace"])
        self.assertTrue(usable_receipt(self.root, suite).usable)

    def test_a_check_process_outside_the_candidate_authorizes_nothing_even_when_it_fails(
        self,
    ) -> None:
        # The suite lives outside the candidate and the safe path keeps the working directory off
        # `sys.path`, so whatever this check process imported, it was not this checkout. Whether
        # the project is importable at all from there depends on the machine — an installed copy
        # in site-packages is exactly the case CI runs — so the assertion is about the candidate
        # boundary, which is the invariant, and not about that machine's site configuration.
        (self.scripts / "crashsuite.py").write_text("raise SystemExit(4)\n", encoding="utf-8")
        suite = CheckSpec.for_module("crashsuite")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONSAFEPATH": "1",
            "PYTHONPATH": str(self.scripts),
        }

        _, receipt = self._run(suite, env=env)

        self.assertEqual(receipt["exit_code"], 4)
        provenance = receipt["project_provenance"]
        self.assertEqual(provenance["origin"], "check-process")
        self.assertNotIn(str(self.root.resolve()), provenance["imported_secretary"])
        self.assertFalse(provenance["inside_workspace"])
        lookup = usable_receipt(self.root, suite)
        self.assertFalse(lookup.usable)
        self.assertIsNone(lookup.authorized())

    def test_every_untrusted_import_is_refused_by_the_one_predicate(self) -> None:
        """The refusal table itself, without an interpreter's site configuration in the way."""
        cases = {
            "no provenance at all": {},
            "an unobserved shape": {"project_provenance": dict(broad_check._UNOBSERVED_PROVENANCE)},
            "a check process that imported nothing": {
                "project_provenance": {"origin": "check-process", "imported_secretary": ""}
            },
            "an import from outside the candidate": {
                "project_provenance": {
                    "origin": "check-process",
                    "imported_secretary": str(self.scripts / "secretary" / "__init__.py"),
                }
            },
            "an unresolvable import path": {
                "project_provenance": {"origin": "check-process", "imported_secretary": "\x00"}
            },
        }
        for name, receipt in cases.items():
            with self.subTest(case=name):
                self.assertTrue(broad_check.candidate_import_refusal(receipt, self.root))

        trusted = {
            "project_provenance": {
                "origin": "check-process",
                "imported_secretary": str(self.root / "secretary" / "__init__.py"),
            }
        }
        self.assertEqual(broad_check.candidate_import_refusal(trusted, self.root), "")

    def test_every_route_refuses_the_same_receipts_for_the_same_reasons(self) -> None:
        """No route may authorize reuse the other would refuse: `check show` and `--reuse` read
        one predicate, so their verdicts cannot drift apart."""
        outside = self._outside_project()
        cases = {
            "inside": (self._suite("insidesuite", "print('in')\n"), None),
            "outside": (
                self._suite("outsidecase", "print('out')\n"),
                dict(os.environ, PYTHONSAFEPATH="1",
                     PYTHONPATH=os.pathsep.join([str(outside), str(self.root)])),
            ),
            "shell": (CheckSpec.for_shell("echo shell"), None),
        }
        for name, (spec, env) in cases.items():
            with self.subTest(case=name):
                self._run(spec, **({"env": env} if env else {}))
                lookup = usable_receipt(self.root, spec)
                argv = ["check", "broad", "--root", str(self.root), "--reuse"]
                argv += (
                    ["--module", spec.module] if spec.shape == "module"
                    else ["--command", spec.command]
                )
                stdout = StringIO()
                with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", StringIO()):
                    main(argv)
                reused = json.loads(stdout.getvalue())["reused"]

                self.assertEqual(
                    reused, lookup.usable, f"{name}: --reuse and the predicate disagree"
                )
                self.assertEqual(lookup.usable, lookup.authorized() is not None)


class CheckSetIdentityTests(BroadCheckTestCase):
    """secretary-1406 review, BLOCKER-CHECK-SET-IDENTITY-COLLISION.

    Identity used to be the rendered command line, and a rendering cannot carry an argument vector:
    `--module-arg 'one two'` and `--module-arg one --module-arg two` render identically, so the
    second invocation was handed the first one's receipt. The digest covers the structured check
    set instead, and the receipt stores it so a reader validates rather than trusts the filename.
    """

    def _record(self) -> Path:
        """A suite that logs its own argv — outside the checkout, so running it is not an edit."""
        log = self.scripts / "argv.log"
        (self.root / "argsuite.py").write_text(
            "import sys\n"
            f"open({str(log)!r}, 'a', encoding='utf-8').write(repr(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )
        return log

    def test_one_argument_with_a_space_is_not_the_same_check_as_two_arguments(self) -> None:
        log = self._record()
        joined = CheckSpec.for_module("argsuite", ("one two",))
        split = CheckSpec.for_module("argsuite", ("one", "two"))

        # They still *read* the same, which is exactly why the rendering cannot be the identity.
        self.assertEqual(joined.identity, split.identity)
        self.assertNotEqual(joined.digest, split.digest)
        self.assertNotEqual(receipt_path(self.root, joined), receipt_path(self.root, split))

        self._run(joined)
        # The second invocation must run: nothing here has evidence about it.
        self.assertFalse(usable_receipt(self.root, split).usable)
        self.assertIsNone(usable_receipt(self.root, split).receipt)
        self._run(split)

        logged = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(logged, ["['one two']", "['one', 'two']"])

    def test_the_cli_runs_the_second_invocation_instead_of_reusing_the_first(self) -> None:
        log = self._record()
        base = ["check", "broad", "--root", str(self.root), "--reuse", "--module", "argsuite"]

        first = _run_main(base + ["--module-arg", "one two"])
        second = _run_main(base + ["--module-arg", "one", "--module-arg", "two"])
        # The same invocation again is the case reuse exists for, and it must still work.
        third = _run_main(base + ["--module-arg", "one", "--module-arg", "two"])

        self.assertFalse(first["reused"])
        self.assertFalse(second["reused"], "a different argument vector must run, not reuse")
        self.assertTrue(third["reused"])
        self.assertEqual(
            log.read_text(encoding="utf-8").splitlines(), ["['one two']", "['one', 'two']"]
        )

    def test_a_receipt_whose_stored_check_set_was_edited_is_refused(self) -> None:
        self._record()
        spec = CheckSpec.for_module("argsuite", ("one", "two"))
        self._run(spec)
        path = receipt_path(self.root, spec)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["check_set"]["args"] = ["one two"]
        payload["receipt_digest"] = broad_check._receipt_digest(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

        # The receipt's own digest is intact, so only the check set's digest catches this.
        self.assertIsNone(load_receipt(path))
        self.assertFalse(usable_receipt(self.root, spec).usable)

    def test_a_receipt_found_under_another_check_s_name_is_refused(self) -> None:
        self._record()
        joined = CheckSpec.for_module("argsuite", ("one two",))
        split = CheckSpec.for_module("argsuite", ("one", "two"))
        self._run(joined)
        # Move the first receipt to where a lookup for the second would find it.
        receipt_path(self.root, joined).rename(receipt_path(self.root, split))

        lookup = usable_receipt(self.root, split)
        self.assertFalse(lookup.usable)
        self.assertEqual(lookup.reason, "receipt is for a different check")

    def test_shell_and_module_checks_never_share_an_identity(self) -> None:
        shell = CheckSpec.for_shell("python -m unittest")
        module = CheckSpec.for_module("unittest")

        self.assertEqual(shell.identity, module.identity)
        self.assertNotEqual(shell.digest, module.digest)


class StreamingSummaryTests(BroadCheckTestCase):
    """secretary-1406 review, BLOCKER-PARSED-SUMMARY-LOST-BEYOND-TAIL.

    The parsed verdict used to be read back out of the bounded tail, so a runner that printed its
    summary and then more than the tail's worth of cleanup output produced a passing receipt with
    no parsed fields at all. The verdict is scanned off the stream instead, in constant memory.
    """

    def test_a_summary_followed_by_more_than_the_tail_still_parses(self) -> None:
        command = self._script(
            "noisy.py",
            "import sys\n"
            "sys.stdout.write('Ran 5 tests in 0.100s\\n\\nOK (skipped=2)\\n')\n"
            "sys.stdout.write('cleanup\\n' * 4000)\n",
        )

        _, receipt = self._run(command)

        self.assertGreater(receipt["output_bytes"], broad_check.TAIL_BYTES)
        self.assertNotIn("OK (skipped=2)", receipt["tail"])
        self.assertEqual(
            receipt["parsed"],
            {"tests": 5, "runner_duration_seconds": 0.1, "summary": "OK", "skipped": 2},
        )
        # Parsing kept the verdict without keeping the logs.
        self.assertLessEqual(len(receipt["tail"].encode("utf-8")), broad_check.TAIL_BYTES)

    def test_a_red_summary_buried_under_cleanup_output_is_not_lost_either(self) -> None:
        command = self._script(
            "noisyred.py",
            "import sys\n"
            "sys.stdout.write('Ran 9 tests in 2.000s\\n\\nFAILED (failures=1, skipped=3)\\n')\n"
            "sys.stdout.write('x' * 40000 + '\\n')\n"
            "sys.exit(1)\n",
        )

        exit_code, receipt = self._run(command)

        self.assertEqual(exit_code, 1)
        self.assertEqual(receipt["parsed"]["summary"], "FAILED")
        self.assertEqual(receipt["parsed"]["failures"], 1)
        self.assertEqual(receipt["parsed"]["skipped"], 3)
        self.assertEqual(receipt["parsed"]["tests"], 9)

    def test_a_summary_split_across_reads_is_still_seen(self) -> None:
        scanner = broad_check._SummaryScanner()
        for chunk in (b"Ran 7 te", b"sts in 1.5s\n\nOK (ski", b"pped=1)\n"):
            scanner.feed(chunk)

        self.assertEqual(
            scanner.finish(),
            {"tests": 7, "runner_duration_seconds": 1.5, "summary": "OK", "skipped": 1},
        )

    def test_the_scanner_keeps_constant_state_across_an_enormous_line(self) -> None:
        scanner = broad_check._SummaryScanner()
        for _ in range(64):
            scanner.feed(b"y" * 65536)
        scanner.feed(b"\nRan 2 tests in 0.5s\nOK\n")

        self.assertEqual(len(scanner._carry), 0)
        self.assertEqual(
            scanner.finish(), {"tests": 2, "runner_duration_seconds": 0.5, "summary": "OK"}
        )

    def test_the_last_summary_wins_when_a_runner_prints_several(self) -> None:
        parsed = parse_unittest_summary(
            "Ran 1 test in 0.1s\nOK\nRan 4 tests in 0.4s\nFAILED (errors=2)\n"
        )

        self.assertEqual(
            parsed,
            {"tests": 4, "runner_duration_seconds": 0.4, "summary": "FAILED", "errors": 2},
        )


class CheckCommandTests(BroadCheckTestCase):
    def _main(self, argv: list[str]) -> tuple[int, dict]:
        stdout = StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", StringIO()):
            code = main(argv)
        return code, json.loads(stdout.getvalue())

    def test_the_command_exit_status_survives_the_wrapper(self) -> None:
        code, payload = self._main(
            ["check", "broad", "--root", str(self.root), "--command", "exit 7"]
        )

        self.assertEqual(code, 7)
        self.assertEqual(payload["receipt"]["exit_code"], 7)
        self.assertFalse(payload["reused"])
        self.assertIn("exit_code: 7", payload["summary"])

    def test_a_signal_death_is_reported_as_a_shell_status_not_flattened_to_one(self) -> None:
        code, payload = self._main(
            ["check", "broad", "--root", str(self.root), "--command", "kill -9 $$"]
        )

        self.assertEqual(code, 137)
        self.assertEqual(payload["receipt"]["status"], "incomplete")

    def test_show_reports_usability_without_running_the_check_again(self) -> None:
        # The marker lives outside the checkout: a check that edited the workspace would
        # legitimately change its content identity, which is a different test than this one.
        marker = self.scripts / "ran.txt"
        self._suite(
            "marksuite",
            f"open({str(marker)!r}, 'a', encoding='utf-8').write('ran\\n')\n",
        )
        self._main(["check", "broad", "--root", str(self.root), "--module", "marksuite"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 1)

        code, payload = self._main(
            ["check", "show", "--root", str(self.root), "--module", "marksuite"]
        )

        self.assertEqual(code, 0)
        self.assertTrue(payload["usable"])
        self.assertEqual(payload["receipt"]["verdict"], "passed")
        self.assertIn("- command:", payload["summary"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 1)

    def test_show_refuses_a_shell_receipt_because_it_attests_no_import(self) -> None:
        self._main(["check", "broad", "--root", str(self.root), "--command", "echo suite"])

        code, payload = self._main(
            ["check", "show", "--root", str(self.root), "--command", "echo suite"]
        )

        self.assertEqual(code, 1)
        self.assertFalse(payload["usable"])
        self.assertIn("provenance was not observed", payload["reason"])
        # The summary is still there to read; it just cannot replace a run.
        self.assertIn("attests no import", payload["summary"])

    def test_show_refuses_a_receipt_that_no_longer_describes_the_checkout(self) -> None:
        self._suite("showsuite", "print('ran')\n")
        self._main(["check", "broad", "--root", str(self.root), "--module", "showsuite"])
        (self.root / "app.py").write_text("VALUE = 9\n", encoding="utf-8")

        code, payload = self._main(
            ["check", "show", "--root", str(self.root), "--module", "showsuite"]
        )

        self.assertEqual(code, 1)
        self.assertFalse(payload["usable"])

    def test_reuse_skips_the_run_only_while_the_content_is_unchanged(self) -> None:
        marker = self.scripts / "runs.txt"
        self._suite(
            "reruns",
            f"open({str(marker)!r}, 'a', encoding='utf-8').write('ran\\n')\n",
        )
        argv = ["check", "broad", "--root", str(self.root), "--module", "reruns", "--reuse"]
        self._main(argv)

        code, payload = self._main(argv)
        self.assertEqual(code, 0)
        self.assertTrue(payload["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 1)

        (self.root / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        code, payload = self._main(argv)
        self.assertEqual(code, 0)
        self.assertFalse(payload["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 2)

    def test_a_shell_receipt_is_never_reused_in_place_of_a_run(self) -> None:
        marker = self.scripts / "shell-runs.txt"
        command = f"echo ran >> {marker}"
        argv = ["check", "broad", "--root", str(self.root), "--command", command, "--reuse"]
        self._main(argv)
        code, payload = self._main(argv)

        self.assertEqual(code, 0)
        self.assertFalse(payload["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 2)

    def test_a_check_needs_exactly_one_shape(self) -> None:
        stdout, stderr = StringIO(), StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            neither = main(["check", "broad", "--root", str(self.root)])
            both = main([
                "check", "broad", "--root", str(self.root), "--module", "unittest",
                "--command", "true",
            ])

        self.assertEqual(neither, 2)
        self.assertEqual(both, 2)

    def test_an_empty_command_is_a_usage_error_and_writes_nothing(self) -> None:
        stdout, stderr = StringIO(), StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            code = main(["check", "broad", "--root", str(self.root), "--command", "   "])

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "empty_command")
        self.assertFalse(broad_check.receipt_dir(self.root).exists())


if __name__ == "__main__":
    unittest.main()
