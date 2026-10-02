"""An archived Product keeps project links the registry no longer lists (ummanu-5).

The checkpoint (`checkpoint._validate_board`) and the restore (`restore._normalized_cards`) both
reach `validate_product_issue_records` through `board.normalized_checkpoint.validate_card_records`;
each entry point is exercised here once, on a staged export with no board store behind it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.restore_fixtures import _restore_card
from ummanu.checkpoint import CheckpointBlocked, _validate_board
from ummanu.data import init_layout
from ummanu.product_issues import ProductIssueValidationError, validate_product_issue_records
from ummanu.restore import RestoreError, _normalized_cards

REGISTERED = {"ummanu"}


def product(*, projects: str = '["retired"]', closed: bool = False) -> dict[str, object]:
    card = _restore_card(reference="product:ummanu", title="Ummanu", column="Issues", swimlane="ummanu")
    card["fields"]["task_type"] = ""
    card["fields"]["project"] = ""
    card["metadata"] = {"record_type": "product", "product_id": "ummanu", "product_projects": projects}
    card["closed"] = closed
    return card


class ClosedProductRegistryTests(unittest.TestCase):
    def test_a_closed_product_may_link_an_unregistered_project(self) -> None:
        validate_product_issue_records([product(closed=True)], registered_project_ids=REGISTERED)

    def test_an_open_product_on_an_unregistered_project_is_still_refused(self) -> None:
        with self.assertRaisesRegex(
            ProductIssueValidationError, r"^Product has unknown registered project\(s\): retired$"
        ):
            validate_product_issue_records([product()], registered_project_ids=REGISTERED)

    def test_a_closed_product_still_needs_a_valid_project_set(self) -> None:
        for projects in ("[]", '["retired", "retired"]', '["Retired"]', "not json"):
            with self.subTest(projects=projects), self.assertRaisesRegex(
                ProductIssueValidationError, "Product (needs a non-empty unique project set|has invalid)"
            ):
                validate_product_issue_records(
                    [product(projects=projects, closed=True)], registered_project_ids=REGISTERED
                )

    def test_a_closed_product_keeps_every_other_product_rule(self) -> None:
        with self.assertRaisesRegex(ProductIssueValidationError, "duplicate Product id"):
            validate_product_issue_records(
                [product(closed=True), product(projects='["ummanu"]')], registered_project_ids=REGISTERED
            )
        untitled = product(closed=True)
        untitled["title"] = " "
        with self.assertRaisesRegex(ProductIssueValidationError, "Product has no title"):
            validate_product_issue_records([untitled], registered_project_ids=REGISTERED)
        misnamed = product(closed=True)
        misnamed["reference"] = "product:other"
        with self.assertRaisesRegex(ProductIssueValidationError, "reference does not match"):
            validate_product_issue_records([misnamed], registered_project_ids=REGISTERED)

    def test_checkpoint_and_restore_both_admit_the_closed_product_and_refuse_the_open_one(self) -> None:
        for closed in (True, False):
            card = product(closed=closed)
            with self.subTest(closed=closed), tempfile.TemporaryDirectory() as tmpdir:
                data_dir = Path(tmpdir) / "ummanu-data"
                init_layout(data_dir)
                board = data_dir / "board"
                (board / "cards.json").write_text(json.dumps({"version": 1, "cards": [card]}), encoding="utf-8")
                (board / "cards.ndjson").write_text(json.dumps(card) + "\n", encoding="utf-8")
                (board / "sprints.ndjson").write_text("", encoding="utf-8")
                (board / "export.json").write_text(
                    json.dumps({"version": 1, "card_count": 1, "sprint_count": 0}), encoding="utf-8"
                )
                if closed:
                    _validate_board(board, registered_project_ids=REGISTERED)
                    restored = _normalized_cards(data_dir, registered_project_ids=REGISTERED)
                    self.assertEqual([c["reference"] for c in restored], ["product:ummanu"])
                else:
                    with self.assertRaisesRegex(CheckpointBlocked, r"unknown registered project\(s\): retired"):
                        _validate_board(board, registered_project_ids=REGISTERED)
                    with self.assertRaisesRegex(RestoreError, r"unknown registered project\(s\): retired"):
                        _normalized_cards(data_dir, registered_project_ids=REGISTERED)


if __name__ == "__main__":
    unittest.main()
