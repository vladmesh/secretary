"""The default Kanboard fake for `secretary.status.collect_status` is suite-wide
and overridable.

secretary-1026: the default unit-test run must not read or write a live board
merely because the process inherited a real installation's `KANBOARD_*`
variables. `tests/__init__.py` patches `secretary.status.KanboardClient` to
`tests.kanboard_fixtures.OfflineKanboard` before any test module is imported;
these tests prove that default wins even when the environment holds
live-looking credentials for a reachable-shaped URL, and that a test can
still opt in to a real sprint board's shape locally.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.config import validate_instance
from secretary.status import collect_status
from tests.test_dispatcher import FakeKanboard


def _report(root: Path):
    instance = root / "instance.yaml"
    instance.write_text(
        "version: 1\nname: test\n"
        f"data_dir: {root / 'data'}\n"
        "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
        encoding="utf-8",
    )
    return validate_instance(instance)


class HermeticKanboardTests(unittest.TestCase):
    def test_default_never_dials_out_even_with_live_looking_credentials(self):
        # No local secretary.status.KanboardClient patch here: this exercises
        # the process-wide default installed by tests/__init__.py, with
        # KANBOARD_* set to a URL that looks exactly like a live
        # installation's. If collect_status ever went back to building a real
        # KanboardClient() from bare os.environ, the urlopen patch below would
        # turn that network attempt into a loud test failure instead of a
        # silent (or slow) real request.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _report(root)
            with mock.patch.dict("os.environ", {
                "KANBOARD_URL": "https://board.invalid/jsonrpc.php",
                "KANBOARD_API_USER": "svc",
                "KANBOARD_API_TOKEN": "secret",
            }), mock.patch(
                "secretary.tasks.urllib.request.urlopen",
                side_effect=AssertionError("unit test reached a real Kanboard network call"),
            ):
                snapshot = collect_status(report, offline=True)

        self.assertIsNone(snapshot["installation"]["sprints"]["error"])
        self.assertEqual(snapshot["installation"]["sprints"]["items"], [])

    def test_a_test_can_still_opt_in_to_a_real_sprint_boards_shape(self):
        # This opt-in shadows the default fake with FakeKanboard (undoing the
        # tests/__init__.py default for the duration of the `with` block),
        # proving the escape hatch reaches sprint content and not just a
        # second layer of the same empty fake.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _report(root)
            board = FakeKanboard()
            board.add_sprint("sprint:1")
            with mock.patch("secretary.status.KanboardClient", return_value=board):
                snapshot = collect_status(report, offline=True)

        self.assertIsNone(snapshot["installation"]["sprints"]["error"])
        self.assertEqual(len(snapshot["installation"]["sprints"]["items"]), 1)


if __name__ == "__main__":
    unittest.main()
