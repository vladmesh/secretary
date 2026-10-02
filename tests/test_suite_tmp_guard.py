"""The test run leaves the host's temporary directory as it found it (secretary-1663).

`tests/__init__.py` claims one temporary root for the whole run, points TMPDIR and `tempfile` at it,
and at exit fails the run when a `ummanu-*` or `orca-*` entry is still in it. Before that, one
module alone had left 1,320 `ummanu-web-process-coherence-*` directories in the production host's
`/tmp`, one batch per suite run. These tests hold both halves: the redirect is in force, and a run
that leaks fails, names the entry, and still leaves the host's temporary directory empty.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tests

_REPO_ROOT = Path(__file__).resolve().parent.parent


class SuiteTemporaryRootTests(unittest.TestCase):
    def test_tempfile_and_children_write_under_the_suite_root(self) -> None:
        root = tests._SUITE_TMP
        self.assertEqual(Path(tempfile.gettempdir()), root)
        self.assertEqual(os.environ["TMPDIR"], str(root))
        with tempfile.TemporaryDirectory() as scratch:
            self.assertEqual(Path(scratch).parent, root)

    def test_only_ummanu_and_orca_entries_count_as_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for name in (
                "ummanu-web-process-coherence-abc",
                "orca-watcher-canary-x",
                "tmpabc123",
                "ummanu-agent-prompt-locks",
            ):
                (root / name).mkdir()
            (root / "ummanu-report.md").write_text("", encoding="utf-8")

            self.assertEqual(
                tests.suite_tmp_leaks(root),
                ["orca-watcher-canary-x", "ummanu-report.md", "ummanu-web-process-coherence-abc"],
            )
        self.assertEqual(tests.suite_tmp_leaks(root), [])


class SuiteTemporaryGuardProcessTests(unittest.TestCase):
    """The guard runs at interpreter exit, so it is exercised in a child that imports `tests`."""

    def run_child(self, body: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as host_tmp:
            env = dict(os.environ)
            env["TMPDIR"] = host_tmp
            env["PYTHONPATH"] = os.pathsep.join(
                [str(_REPO_ROOT / "src"), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
            )
            done = subprocess.run(
                [sys.executable, "-c", f"import tempfile, tests\n{body}\n"],
                cwd=str(_REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            left = sorted(os.listdir(host_tmp))
        return done, left

    def test_a_run_that_cleans_up_exits_as_it_would_and_leaves_nothing(self) -> None:
        done, left = self.run_child("with tempfile.TemporaryDirectory(prefix='ummanu-clean-'):\n    pass")

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(left, [])

    def test_a_leaking_run_fails_names_the_entry_and_still_leaves_nothing(self) -> None:
        done, left = self.run_child(
            "tempfile.mkdtemp(prefix='ummanu-leaked-')\ntempfile.mkdtemp(prefix='unrelated-')"
        )

        self.assertEqual(done.returncode, 1)
        self.assertIn("left temporary entries behind", done.stderr)
        self.assertIn("ummanu-leaked-", done.stderr)
        self.assertNotIn("unrelated-", done.stderr)
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
