"""The suite runs on the card backend it chose, never on the one the shell it started from serves.

`SECRETARY_CARD_BACKEND` is the one named place the card backend is chosen in
(`secretary.board.backend`), its absence means `kanboard`, and `card_backend()` decides once per
process and remembers. The live installation exports `postgres` there — into every worker, reviewer
and operator pane — so a suite that inherits the name silently runs every construction that goes
through the switch against a store it has not got: about twenty-eight broad failures on content that
is green from a clean shell, and, worse than the failures, the non-hermetic CLI cases hidden behind
them (sprint:1437, secretary-1617).

`tests/__init__.py` closes that by clearing the name before any test module is imported. These tests
hold both ends of it: the default is in force and is `kanboard`, a case that means PostgreSQL can
still opt in for its own duration, and the clearing survives a shell that exported the live value —
which is checked in a child process, because by the time this one runs `tests` is long imported.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from secretary.board import backend

_REPO_ROOT = Path(__file__).resolve().parent.parent


class SuiteCardBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        # The decision is cached per process, and these cases revise it: that is exactly the
        # revision `reset_card_backend` exists for.
        backend.reset_card_backend()
        self.addCleanup(backend.reset_card_backend)

    def test_the_selector_is_absent_and_the_backend_is_the_product_default(self) -> None:
        self.assertNotIn(backend.CARD_BACKEND_ENV, os.environ)
        self.assertEqual(backend.card_backend(), backend.KANBOARD)
        self.assertEqual(backend.card_backend_status()["source"], "default")

    def test_a_case_that_means_postgres_still_opts_in_for_its_own_duration(self) -> None:
        """Not a vacuous absence: the selector still works, it is just not inherited."""
        os.environ[backend.CARD_BACKEND_ENV] = backend.POSTGRES
        self.addCleanup(os.environ.pop, backend.CARD_BACKEND_ENV, None)
        backend.reset_card_backend()
        self.assertEqual(backend.card_backend(), backend.POSTGRES)

    def test_importing_the_suite_takes_away_the_live_selector_an_operator_shell_exports(self) -> None:
        env = dict(os.environ)
        env[backend.CARD_BACKEND_ENV] = backend.POSTGRES
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_REPO_ROOT / "src"), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
        )
        probe = (
            "import os, tests\n"
            "from secretary.board import backend\n"
            "print(repr(os.environ.get(backend.CARD_BACKEND_ENV)), backend.card_backend())\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip().splitlines()[-1], f"None {backend.KANBOARD}")


if __name__ == "__main__":
    unittest.main()
