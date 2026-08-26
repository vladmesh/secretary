"""The legacy pipeline surface against the blocked-report contract (secretary-1034).

A blocked report now carries a classification of the blocker, and `secretary.tasks` owns that
vocabulary. This surface still writes board comments, so without a guard it would keep writing a
`[report:blocked]` comment with no classification at all — a record the protocol owner refuses,
written by the second engine. It refuses instead, and names the writer that owns the protocol.
"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

from secretary import tasks
from triggered_agents.agents.pipeline import model, ops


class LegacyBlockedReportTests(unittest.TestCase):
    def _report(self, kind, body="stuck"):
        comments: list[dict] = []

        def call(method, **params):
            if method == "getColumns":
                return [
                    {"id": index, "title": title, "position": index}
                    for index, title in enumerate(model.COLUMNS, 1)
                ]
            if method == "getTaskByReference":
                return {"id": 7, "reference": "secretary-1", "column_id": 3, "swimlane_id": 1}
            if method == "createComment":
                comments.append(params)
                return 1
            raise AssertionError(f"unexpected call {method} {params}")

        with (
            mock.patch.object(ops, "call", side_effect=call),
            mock.patch.object(ops, "board_id", return_value=2),
        ):
            return ops.report("secretary-1", kind, body), comments

    def test_a_blocked_report_is_refused_and_names_the_writer_that_owns_it(self):
        with self.assertRaises(model.GuardError) as raised:
            self._report("blocked")

        message = str(raised.exception)
        self.assertIn("python3 -P -m secretary task report --kind blocked", message)
        self.assertIn("--classification", message)

    def test_the_refusal_writes_nothing_to_the_board(self):
        with mock.patch.object(ops, "call", side_effect=AssertionError("board was touched")):
            with self.assertRaises(model.GuardError):
                ops.report("secretary-1", "blocked", "stuck")

    def test_the_vocabulary_is_not_duplicated_into_this_surface(self):
        """One definition of the two values, in `secretary.tasks`. Two copies would drift."""
        for module in (ops, model):
            source = inspect.getsource(module)
            for value in tasks._BLOCK_CLASSIFICATIONS:
                with self.subTest(module=module.__name__, value=value):
                    self.assertNotIn(value, source)

    def test_a_done_report_is_unchanged(self):
        result, comments = self._report("done", "the PR is open")
        self.assertEqual(result["action"], "reported")
        self.assertEqual(result["kind"], "done")
        self.assertEqual(len(comments), 1)
        self.assertTrue(comments[0]["content"].startswith(f"[{model.MARKER_REPORT_DONE}]\n"))

    def test_an_unknown_kind_is_still_refused_by_its_own_message(self):
        with self.assertRaisesRegex(model.GuardError, "report kind must be"):
            self._report("whatever")


if __name__ == "__main__":
    unittest.main()
