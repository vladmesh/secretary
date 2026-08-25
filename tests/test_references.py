"""The one rule that numbers a card and a sprint reference (issue:f32aa04c)."""

from __future__ import annotations

import unittest

from triggered_agents.runtime.references import (
    BoardRowsUnavailable,
    board_rows,
    next_reference,
)


class NextReferenceTests(unittest.TestCase):
    def test_allocation_clears_every_number_the_family_has_used(self) -> None:
        rows = [
            {"reference": "secretary-1"},
            {"reference": "secretary-1404"},
            {"reference": "secretary-7"},
        ]
        self.assertEqual(next_reference(rows, "secretary-"), "secretary-1405")

    def test_a_family_with_no_rows_starts_at_one(self) -> None:
        self.assertEqual(next_reference([], "secretary-"), "secretary-1")
        self.assertEqual(next_reference([{"reference": "other-9"}], "secretary-"), "secretary-1")

    def test_only_this_family_is_counted_and_only_when_it_is_a_number(self) -> None:
        rows = [
            {"reference": "sprint:1404"},
            {"reference": "secretary-instance-9"},
            {"reference": "secretary-510-pilot"},
            {"reference": "secretary-x"},
            {"reference": ""},
            {},
        ]
        self.assertEqual(next_reference(rows, "secretary-"), "secretary-1")
        self.assertEqual(next_reference(rows, "sprint:"), "sprint:1405")


class BoardRowsTests(unittest.TestCase):
    def test_open_and_archived_rows_are_one_enumeration(self) -> None:
        answers = {
            1: [{"id": 1, "reference": "secretary-1"}],
            0: [{"id": 2, "reference": "secretary-2"}, {"id": 1, "reference": "secretary-1"}],
        }
        calls: list[dict] = []

        def call(method: str, **params: object) -> object:
            calls.append({"method": method, **params})
            return answers[int(params["status_id"])]  # type: ignore[arg-type]

        rows = board_rows(call, 7)
        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertEqual([entry["status_id"] for entry in calls], [1, 0])

    def test_an_answer_that_is_not_a_list_is_loud(self) -> None:
        with self.assertRaises(BoardRowsUnavailable):
            board_rows(lambda method, **params: None, 7)


if __name__ == "__main__":
    unittest.main()
