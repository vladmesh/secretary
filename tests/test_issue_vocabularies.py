from __future__ import annotations

import unittest

from secretary.board.models import (
    Issue,
    IssueCloseReason,
    IssueKind,
    IssuePriority,
)


class IssueVocabularyTests(unittest.TestCase):
    def test_string_inputs_normalize_to_typed_vocabularies(self) -> None:
        issue = Issue(
            "issue:typed",
            "Typed issue",
            "product:secretary",
            priority="P1",
            issue_kind="bug",
            close_reason="resolved",
        )

        self.assertIs(issue.priority, IssuePriority.P1)
        self.assertIs(issue.issue_kind, IssueKind.BUG)
        self.assertIs(issue.close_reason, IssueCloseReason.RESOLVED)
        self.assertEqual(issue.priority, "P1")
        self.assertEqual(issue.issue_kind, "bug")
        self.assertEqual(issue.close_reason, "resolved")

    def test_absent_wire_values_normalize_to_none(self) -> None:
        issue = Issue(
            "issue:empty",
            "Empty metadata",
            "product:secretary",
            priority="",
            issue_kind="",
            close_reason="",
        )

        self.assertIsNone(issue.priority)
        self.assertIsNone(issue.issue_kind)
        self.assertIsNone(issue.close_reason)

    def test_staged_pending_pair_does_not_extend_vocabularies(self) -> None:
        issue = Issue(
            "issue:pending",
            "Pending metadata",
            "product:pending",
            priority="pending",
            issue_kind="pending",
        )

        self.assertIsNone(issue.priority)
        self.assertIsNone(issue.issue_kind)
        self.assertNotIn("pending", set(IssuePriority))
        self.assertNotIn("pending", set(IssueKind))

    def test_half_pending_issue_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "omit both priority and kind"):
            Issue(
                "issue:half-pending",
                "Half pending",
                "product:pending",
                priority="pending",
                issue_kind="bug",
            )

    def test_unknown_values_are_rejected(self) -> None:
        cases = (
            {"priority": "PX", "issue_kind": "bug"},
            {"priority": "P1", "issue_kind": "incident"},
            {"priority": "P1", "issue_kind": "bug", "close_reason": "done"},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "invalid Issue vocabulary"):
                Issue(
                    "issue:invalid",
                    "Invalid metadata",
                    "product:secretary",
                    **values,
                )


if __name__ == "__main__":
    unittest.main()
