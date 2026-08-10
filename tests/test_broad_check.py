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
        (self.root / ".gitignore").write_text("/state/\n", encoding="utf-8")
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "base")
        self.stream = StringIO()

    def _script(self, name: str, body: str) -> str:
        path = self.scripts / name
        path.write_text(body, encoding="utf-8")
        return f"{sys.executable} {path}"

    def _run(self, command: str, **kwargs):
        return run_broad_check(command, root=self.root, stream=self.stream, **kwargs)


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
        exit_code, receipt = self._run("echo boom 1>&2; exit 3")

        self.assertEqual(exit_code, 3)
        self.assertEqual(receipt["exit_code"], 3)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["verdict"], "failed")
        self.assertIn("boom", receipt["tail"])
        # A red answer is a concrete answer: the point of the receipt is that nobody reruns the
        # suite to rediscover it.
        lookup = usable_receipt(self.root, "echo boom 1>&2; exit 3")
        self.assertTrue(lookup.usable)
        self.assertEqual(lookup.receipt["verdict"], "failed")

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

    def test_provenance_records_the_project_a_check_from_this_cwd_would_import(self) -> None:
        _, receipt = self._run("true")
        provenance = receipt["project_provenance"]

        # This workspace has no `secretary` package of its own, so the import resolves outside it
        # and the receipt says so plainly rather than implying the candidate was validated.
        self.assertFalse(provenance["inside_workspace"])

        (self.root / "secretary").mkdir()
        (self.root / "secretary" / "__init__.py").write_text("", encoding="utf-8")
        _, receipt = self._run("true")
        provenance = receipt["project_provenance"]
        self.assertTrue(provenance["inside_workspace"])
        self.assertTrue(
            provenance["imported_secretary"].startswith(str(self.root.resolve())),
            provenance["imported_secretary"],
        )

    def test_the_receipt_records_where_and_when_the_run_happened(self) -> None:
        _, receipt = self._run("echo timed")

        self.assertEqual(receipt["cwd"], str(self.root.resolve()))
        self.assertEqual(
            receipt["command_or_check_set_digest"], broad_check.command_digest("echo timed")
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

        self.assertIn("python3 -m secretary check show --command", text)
        self.assertIn("state/checks/", text)


class UnchangedContentReuseTests(BroadCheckTestCase):
    def test_an_unchanged_checkout_reuses_the_receipt_and_any_edit_invalidates_it(self) -> None:
        self._run("echo suite")
        lookup = usable_receipt(self.root, "echo suite")
        self.assertTrue(lookup.usable)
        self.assertEqual(lookup.reason, "receipt describes this exact content")

        # Writing the receipt is itself a change to the directory; it must not invalidate the
        # receipt it just wrote, or reuse would never be possible at all.
        self.assertTrue(usable_receipt(self.root, "echo suite").usable)

        (self.root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        stale = usable_receipt(self.root, "echo suite")
        self.assertFalse(stale.usable)
        self.assertIn("content changed", stale.reason)

        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.assertTrue(usable_receipt(self.root, "echo suite").usable)

    def test_committing_the_same_content_still_changes_the_identity(self) -> None:
        (self.root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self._run("echo suite")
        self.assertTrue(usable_receipt(self.root, "echo suite").usable)

        _git(self.root, "commit", "-q", "-a", "-m", "second")
        # A new candidate SHA is precisely the case that opens a justified new run.
        self.assertFalse(usable_receipt(self.root, "echo suite").usable)

    def test_a_new_untracked_file_changes_the_identity(self) -> None:
        self._run("echo suite")
        (self.root / "extra.py").write_text("HELPER = True\n", encoding="utf-8")

        self.assertFalse(usable_receipt(self.root, "echo suite").usable)

    def test_a_receipt_for_another_command_is_never_offered_for_this_one(self) -> None:
        self._run("echo suite")

        other = usable_receipt(self.root, "echo other-suite")
        self.assertFalse(other.usable)
        self.assertIsNone(other.receipt)

    def test_a_checkout_without_a_resolvable_identity_never_reuses_anything(self) -> None:
        bare = Path(self.tmpdir.name) / "not-a-repo"
        bare.mkdir()
        exit_code, receipt = run_broad_check("echo suite", root=bare, stream=self.stream)

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["content_identity"], {"head_sha": "", "worktree_digest": ""})
        lookup = usable_receipt(bare, "echo suite")
        self.assertFalse(lookup.usable)
        self.assertIn("no resolvable content identity", lookup.reason)


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
        # The marker lives outside the checkout: a command that edited the workspace would
        # legitimately change its content identity, which is a different test than this one.
        marker = self.scripts / "ran.txt"
        command = f"echo ran >> {marker}"
        self._main(["check", "broad", "--root", str(self.root), "--command", command])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 1)

        code, payload = self._main(["check", "show", "--root", str(self.root), "--command", command])

        self.assertEqual(code, 0)
        self.assertTrue(payload["usable"])
        self.assertEqual(payload["receipt"]["verdict"], "passed")
        self.assertIn("- command:", payload["summary"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 1)

    def test_show_refuses_a_receipt_that_no_longer_describes_the_checkout(self) -> None:
        self._main(["check", "broad", "--root", str(self.root), "--command", "echo suite"])
        (self.root / "app.py").write_text("VALUE = 9\n", encoding="utf-8")

        code, payload = self._main(
            ["check", "show", "--root", str(self.root), "--command", "echo suite"]
        )

        self.assertEqual(code, 1)
        self.assertFalse(payload["usable"])

    def test_reuse_skips_the_run_only_while_the_content_is_unchanged(self) -> None:
        marker = self.scripts / "runs.txt"
        command = f"echo ran >> {marker}"
        self._main(["check", "broad", "--root", str(self.root), "--command", command, "--reuse"])

        code, payload = self._main(
            ["check", "broad", "--root", str(self.root), "--command", command, "--reuse"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 1)

        (self.root / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        code, payload = self._main(
            ["check", "broad", "--root", str(self.root), "--command", command, "--reuse"]
        )
        self.assertEqual(code, 0)
        self.assertFalse(payload["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 2)

    def test_an_empty_command_is_a_usage_error_and_writes_nothing(self) -> None:
        stdout, stderr = StringIO(), StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            code = main(["check", "broad", "--root", str(self.root), "--command", "   "])

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "empty_command")
        self.assertFalse(broad_check.receipt_dir(self.root).exists())


if __name__ == "__main__":
    unittest.main()
