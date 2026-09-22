"""What one build of the sprint form's catalogue costs the board, and what it refuses to invent.

The catalogue used to be two full board passes with one `getTaskMetadata` per card: on the owner's
installed board (~1480 cards) that was 2960 metadata round trips and 26.6 seconds
(issue:d104896882f417c1be22, 2026-09-06). The budget here is counted in what the store is asked,
never in seconds, so it holds on any machine: one pass over the board, and metadata read in batches,
so the cost does not grow with the number of cards.

Products and Issues have one implementation, PostgreSQL (secretary-1670), so the board is a real
store and the cost is counted as its SQL statements and board verbs: a batched per-record read is
answered set-based (`SqlCardClient.call_batch`), one statement whatever the number of rows.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any
from unittest import mock

from secretary.product_issues import ProductIssueStore
from secretary.tasks import TaskError
from tests.sql_backend_fixtures import CardStoreClient, card_store
from tests.webproto_sprint_fixtures import SprintProtocolFixture

#: A board mixed the way the live one is, at a tenth of the size the owner measured on: the budget
#: below is a comparison between two sizes, not a count at one.
PRODUCTS = 5
OPEN_ISSUES = 90
CLOSED_ISSUES = 40
OTHER_CARDS = 20


class CountingStore(CardStoreClient):
    """The card store client, counting the SQL statements it sends while `counting` is set."""

    counting = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.statements = 0

    def _query(self, *args: Any, **kwargs: Any) -> Any:
        self.statements += self.counting
        return super()._query(*args, **kwargs)

    def _execute(self, *args: Any, **kwargs: Any) -> Any:
        self.statements += self.counting
        return super()._execute(*args, **kwargs)


def _seed(board: CountingStore, *, scale: int = 1) -> None:
    """Products, open and closed Issues, and ordinary work cards, in that order."""
    for index in range(PRODUCTS):
        product = f"product-{index:02d}"
        board.add_record(
            f"product:{product}",
            f"Product {index}",
            {"record_type": "product", "product_id": product, "product_projects": json.dumps([product])},
        )
    for index in range(OPEN_ISSUES * scale):
        board.add_record(
            f"issue:open-{index:04d}",
            f"Open issue {index}",
            {
                "record_type": "issue",
                "issue_product": f"product-{index % PRODUCTS:02d}",
                "issue_kind": "bug",
                "issue_priority": f"P{index % 4}",
            },
        )
    for index in range(CLOSED_ISSUES * scale):
        board.add_record(
            f"issue:done-{index:04d}",
            f"Closed issue {index}",
            {
                "record_type": "issue",
                "issue_product": f"product-{index % PRODUCTS:02d}",
                "issue_kind": "bug",
                "issue_priority": "P2",
                "issue_closed_reason": "resolved",
            },
            closed=True,
        )
    for index in range(OTHER_CARDS * scale):
        # Ordinary work cards: part of the live board, and not part of the catalogue.
        board.add_card(10_000 + index, f"secretary-{index}", title=f"Card {index}")


class CatalogueBudgetTests(unittest.TestCase):
    """One board pass, batched metadata, and a catalogue whose composition did not move."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.board, self.store = self._store()

    def _store(self, *, scale: int = 1) -> tuple[CountingStore, ProductIssueStore]:
        board = card_store(self, instance_dir=self.root, client_class=CountingStore)
        _seed(board, scale=scale)
        return board, ProductIssueStore(board, data_dir=self.root / "data", instance=self.root)

    @staticmethod
    def _cost(board: CountingStore, read: Any) -> tuple[Counter[str], Any]:
        """What one read asks the store: statements, board verbs, and batches by method."""
        board.calls.clear()
        board.batch_calls.clear()
        board.statements = 0
        board.counting = True
        try:
            answer = read()
        finally:
            board.counting = False
        # The board vocabulary (`getProjectByName`, `getColumns`) is answered in process by the
        # client and costs the store nothing, so only the reads that reach it are counted.
        cost: Counter[str] = Counter(
            method for method, _ in board.calls if method in {"getAllTasks", "getTaskMetadata"}
        )
        cost.update(f"batch:{method}" for batch in board.batch_calls for method in {m for m, _ in batch})
        cost["statement"] = board.statements
        return cost, answer

    def test_one_catalogue_build_is_one_board_pass_and_batched_metadata(self) -> None:
        cost, (products, issues) = self._cost(self.board, lambda: self.store.catalogue(include_closed=False))

        # `all_project_cards` reads the open and the archived halves: that is one pass, not two.
        self.assertEqual(cost["getAllTasks"], 2)
        self.assertEqual(cost["getTaskMetadata"], 0, "no per-card metadata read")
        self.assertEqual(cost["batch:getTaskMetadata"], 1)

        self.assertEqual(
            [item["id"] for item in products],
            [f"product-{index:02d}" for index in range(PRODUCTS)],
        )
        self.assertEqual([item["projects"] for item in products][0], ["product-00"])
        self.assertEqual(len(issues), OPEN_ISSUES)
        self.assertEqual(
            [item["ref"] for item in issues],
            sorted(
                (f"issue:open-{index:04d}" for index in range(OPEN_ISSUES)),
                key=lambda ref: (f"P{int(ref.rsplit('-', 1)[1]) % 4}", ref),
            ),
        )
        self.assertFalse(
            [item for item in issues if item["ref"].startswith("issue:done")],
            "a closed issue is refused by create, so the form never offers one",
        )
        self.assertFalse(any(item["closed"] for item in issues))

    def test_the_catalogue_costs_the_same_on_a_board_three_times_the_size(self) -> None:
        """The cost grows with nothing: not with records, not with cards."""
        small, _ = self._cost(self.board, lambda: self.store.catalogue(include_closed=False))
        board, store = self._store(scale=3)
        large, (_, issues) = self._cost(board, lambda: store.catalogue(include_closed=False))

        self.assertEqual(len(issues), OPEN_ISSUES * 3)
        self.assertEqual(dict(small), dict(large), "a request budget that grows with N is the N+1")

    def test_the_catalogue_is_what_the_two_list_reads_answer(self) -> None:
        """The single pass is a saving, not a different answer."""
        products, issues = self.store.catalogue(include_closed=False)
        self.assertEqual(products, self.store.list_products())
        self.assertEqual(issues, self.store.list_issues(include_closed=False))
        self.assertEqual(
            [item["ref"] for item in self.store.list_issues(product="product-01")],
            [
                item["ref"]
                for item in issues
                if item["product"] == "product-01"
            ],
        )
        closed = self.store.list_issues(include_closed=True)
        self.assertEqual(len(closed), OPEN_ISSUES + CLOSED_ISSUES)

    def test_each_list_read_also_pays_one_pass_and_batched_metadata(self) -> None:
        cost, _ = self._cost(self.board, self.store.list_products)
        self.assertEqual(cost["getAllTasks"], 2)
        self.assertEqual(cost["getTaskMetadata"], 0)
        self.assertEqual(cost["batch:getTaskMetadata"], 1)

    def test_a_rejected_batch_member_refuses_instead_of_truncating_the_catalogue(self) -> None:
        with mock.patch.object(self.board, "call_batch", side_effect=TaskError("backend_error", "the board rejected a batch member", 1)):
            with self.assertRaises(TaskError) as refused:
                self.store.catalogue(include_closed=False)
            self.assertEqual(refused.exception.code, "backend_error")
            with self.assertRaises(TaskError):
                self.store.list_products()
            with self.assertRaises(TaskError):
                self.store.list_issues(include_closed=True)


class CatalogueSectionRefusalTests(SprintProtocolFixture):
    """A backend that answers a metadata batch incompletely marks the form's sections unavailable."""

    def test_a_rejected_metadata_batch_marks_products_and_issues_unavailable(self) -> None:
        available = self.reads().sprint_options()
        self.assertEqual(available["products"]["source"]["state"], "available")

        def reject(calls: Any) -> list[Any]:
            raise TaskError("backend_error", "the board rejected a batch member", 1)

        self.board.call_batch = reject  # type: ignore[method-assign]
        options = self.reads().sprint_options()
        for section in ("products", "issues"):
            self.assertEqual(options[section]["source"]["state"], "unavailable")
            # `null` and never `[]`: a board that refused cannot say the installation has none.
            self.assertIsNone(options[section]["items"])
            self.assertIn("the board could not be read", options[section]["source"]["reason"])
