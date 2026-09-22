"""The extension bag's one key and the one rule that reads a record written before it (§8.2)."""

from __future__ import annotations

import hashlib
import importlib
import unittest

from secretary import tasks
from secretary.board.extension_bag import EXTENSION_BAG, EXTENSION_MARKERS, fold_extension_bags


class FoldExtensionBagsTests(unittest.TestCase):
    def test_a_record_already_on_the_current_key_reads_as_it_did(self) -> None:
        record = {EXTENSION_BAG: {"swimlane": "Secretary", "note": "kept"}}

        self.assertEqual(fold_extension_bags(record), record)

    def test_an_older_key_folds_into_the_current_bag(self) -> None:
        # `retired_board` stands for the retired board's own key: the rule names no key but the current one.
        self.assertEqual(
            fold_extension_bags({"retired_board": {"swimlane": "Secretary", "steward_report": "1"}}),
            {EXTENSION_BAG: {"swimlane": "Secretary", "steward_report": "1"}},
        )

    def test_the_current_bag_wins_a_field_both_name(self) -> None:
        self.assertEqual(
            fold_extension_bags(
                {"retired_board": {"note": "old", "other": "1"}, EXTENSION_BAG: {"note": "new"}}
            ),
            {EXTENSION_BAG: {"note": "new", "other": "1"}},
        )

    def test_a_non_object_key_is_kept_as_one_field(self) -> None:
        self.assertEqual(fold_extension_bags({"stray": "value"}), {EXTENSION_BAG: {"stray": "value"}})

    def test_markers_stay_beside_the_bag(self) -> None:
        self.assertEqual(
            fold_extension_bags({"retired_board": {"swimlane": "x"}, "board_never_named": ["task_type"]}),
            {EXTENSION_BAG: {"swimlane": "x"}, "board_never_named": ["task_type"]},
        )

    def test_nothing_is_no_bag(self) -> None:
        for value in (None, {}, [], "text"):
            with self.subTest(value=value):
                self.assertEqual(fold_extension_bags(value), {})


class NeutralKeyRevisionTests(unittest.TestCase):
    def test_the_revision_copies_the_key_and_markers_it_was_written_against(self) -> None:
        revision = importlib.import_module(
            "secretary.board.migrations.versions.0014_neutral_extension_bag"
        )

        self.assertEqual(revision.NEW_KEY, EXTENSION_BAG)
        self.assertEqual(revision.MARKERS, EXTENSION_MARKERS)
        self.assertTrue(tasks._done_retention_request_id(1, 2).startswith(revision.DONE_RETENTION_PREFIX))

    def test_the_done_retention_identity_is_neutral(self) -> None:
        self.assertEqual(
            tasks._done_retention_request_id(12, 100),
            "done-retention-" + hashlib.sha256(b"card:12:done:100").hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
