"""Status reads fail closed and accept an explicit in-memory board seam.

secretary-1026: ambient `KANBOARD_*` credentials must never make a unit test
read or write a live board.  Tests that need sprint data pass their fake board
through `collect_status(..., sprint_client=...)`.
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
        # No injected board here.  Even with live-looking credentials, the
        # temporary instance has no transport and must fail before any network
        # request; urlopen makes an accidental dial-out loud.
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

        self.assertEqual(snapshot["installation"]["sprints"]["error"]["code"], "backend_unavailable")
        self.assertEqual(snapshot["installation"]["sprints"]["items"], [])

    def test_a_test_can_still_opt_in_to_a_real_sprint_boards_shape(self):
        # The explicit fake board is the status injection seam.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _report(root)
            board = FakeKanboard()
            board.add_sprint("sprint:1")
            snapshot = collect_status(report, offline=True, sprint_client=board)

        self.assertIsNone(snapshot["installation"]["sprints"]["error"])
        self.assertEqual(len(snapshot["installation"]["sprints"]["items"]), 1)


if __name__ == "__main__":
    unittest.main()
