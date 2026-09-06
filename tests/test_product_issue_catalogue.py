"""What one build of the sprint form's catalogue costs the board, and what it refuses to invent.

The catalogue used to be two full board passes with one `getTaskMetadata` per card: on the owner's
installed board (~1480 cards) that was 2960 metadata round trips and 26.6 seconds
(issue:d104896882f417c1be22, 2026-09-06). The budget here is counted in backend requests, never in
seconds, so it holds on any machine: one pass over the board, and metadata read in batches, so the
number of metadata requests grows with the number of batches and not with the number of cards.
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any

from secretary.product_issues import ProductIssueStore
from secretary.tasks import _BATCH_CHUNK, TaskError, _BatchCallRejected
from tests.fakes.product_issues import ProductBoard
from tests.webproto_sprint_fixtures import SprintProtocolFixture

#: A board of the size the owner measured on, mixed the way the live one is.
PRODUCTS = 5
OPEN_ISSUES = 900
CLOSED_ISSUES = 400
OTHER_CARDS = 195
BOARD_SIZE = PRODUCTS + OPEN_ISSUES + CLOSED_ISSUES + OTHER_CARDS


class CountingBoard(ProductBoard):
    """A mixed board that counts backend requests the way the transport spends them.

    One `call` is one request; one `call_batch` is one request per chunk, which is what
    `KanboardClient.call_batch` puts on the wire. Counting the members of a batch instead would
    make a batched read look exactly like the per-card reads it replaces.
    """

    def __init__(self, *, size: int = BOARD_SIZE) -> None:
        super().__init__()
        self.requests: Counter[str] = Counter()
        self.tasks = []
        self.metadata = {}
        self.comments = {}
        self._seed(size)

    def _seed(self, size: int) -> None:
        def add(task_id: int, reference: str, title: str, active: int, meta: dict[str, str]) -> None:
            self.tasks.append(
                {
                    "id": task_id,
                    "reference": reference,
                    "title": title,
                    "description": "",
                    "column_id": 1,
                    "position": task_id,
                    "swimlane_id": 4,
                    "is_active": active,
                }
            )
            self.metadata[task_id] = meta
            self.comments[task_id] = []

        task_id = 100
        for index in range(PRODUCTS):
            product = f"product-{index:02d}"
            add(
                task_id,
                f"product:{product}",
                f"Product {index}",
                1,
                {
                    "record_type": "product",
                    "product_id": product,
                    "product_projects": json.dumps([product]),
                },
            )
            task_id += 1
        for index in range(OPEN_ISSUES):
            add(
                task_id,
                f"issue:open-{index:04d}",
                f"Open issue {index}",
                1,
                {
                    "record_type": "issue",
                    "issue_product": f"product-{index % PRODUCTS:02d}",
                    "issue_kind": "bug",
                    "issue_priority": f"P{index % 4}",
                },
            )
            task_id += 1
        for index in range(CLOSED_ISSUES):
            add(
                task_id,
                f"issue:done-{index:04d}",
                f"Closed issue {index}",
                0,
                {
                    "record_type": "issue",
                    "issue_product": f"product-{index % PRODUCTS:02d}",
                    "issue_kind": "bug",
                    "issue_priority": "P2",
                    "issue_closed_reason": "resolved",
                },
            )
            task_id += 1
        for index in range(OTHER_CARDS):
            # Ordinary work cards: the majority of the live board, and not part of the catalogue.
            add(task_id, f"secretary-{index}", f"Card {index}", 1, {"project": "secretary"})
            task_id += 1
        assert len(self.tasks) == size

    def call(self, method: str, **params: object) -> object:
        self.requests[method] += 1
        return super().call(method, **params)

    def call_batch(self, calls):
        batch = list(calls)
        methods = {method for method, _ in batch}
        assert len(methods) <= 1, "this fixture only batches one method at a time"
        before = Counter(self.requests)
        results = super().call_batch(batch)
        # The members answered above are not requests of their own; the chunks are.
        self.requests = before
        for method in methods:
            self.requests[method] += math.ceil(len(batch) / _BATCH_CHUNK)
        return results


class RejectingBoard(CountingBoard):
    """A backend that rejects one member of a metadata batch, the way a live aggregate reply can."""

    def call_batch(self, calls):
        batch = list(calls)
        raise _BatchCallRejected({len(batch) - 1})


class CatalogueBudgetTests(unittest.TestCase):
    """One board pass, batched metadata, and a catalogue whose composition did not move."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.board = CountingBoard()
        self.store = ProductIssueStore(self.board, data_dir=self.root / "data", instance=self.root)

    def _store_with(self, board: ProductBoard) -> ProductIssueStore:
        return ProductIssueStore(board, data_dir=self.root / "data", instance=self.root)

    def test_one_catalogue_build_is_one_board_pass_and_batched_metadata(self) -> None:
        products, issues = self.store.catalogue(include_closed=False)

        self.assertEqual(len(self.board.tasks), BOARD_SIZE)
        self.assertEqual(self.board.requests["getProjectByName"], 1)
        self.assertEqual(self.board.requests["getColumns"], 1)
        # `all_project_cards` reads the open and the archived halves: that is one pass, not two.
        self.assertEqual(self.board.requests["getAllTasks"], 2)
        self.assertEqual(
            self.board.requests["getTaskMetadata"], math.ceil(BOARD_SIZE / _BATCH_CHUNK)
        )
        self.assertLess(
            self.board.requests["getTaskMetadata"],
            BOARD_SIZE,
            "metadata requests must grow with the number of batches, not with the number of cards",
        )

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
        self.board.requests.clear()
        self.store.list_products()
        self.assertEqual(self.board.requests["getAllTasks"], 2)
        self.assertEqual(
            self.board.requests["getTaskMetadata"], math.ceil(BOARD_SIZE / _BATCH_CHUNK)
        )

    def test_a_rejected_batch_member_refuses_instead_of_truncating_the_catalogue(self) -> None:
        store = self._store_with(RejectingBoard())
        with self.assertRaises(TaskError) as refused:
            store.catalogue(include_closed=False)
        self.assertEqual(refused.exception.code, "backend_error")
        with self.assertRaises(TaskError):
            store.list_products()
        with self.assertRaises(TaskError):
            store.list_issues(include_closed=True)


class CatalogueSectionRefusalTests(SprintProtocolFixture):
    """A backend that answers a metadata batch incompletely marks the form's sections unavailable."""

    def test_a_rejected_metadata_batch_marks_products_and_issues_unavailable(self) -> None:
        available = self.reads().sprint_options()
        self.assertEqual(available["products"]["source"]["state"], "available")

        def reject(calls: Any) -> list[Any]:
            raise _BatchCallRejected({0})

        self.board.call_batch = reject  # type: ignore[method-assign]
        options = self.reads().sprint_options()
        for section in ("products", "issues"):
            self.assertEqual(options[section]["source"]["state"], "unavailable")
            # `null` and never `[]`: a board that refused cannot say the installation has none.
            self.assertIsNone(options[section]["items"])
            self.assertIn("the board could not be read", options[section]["source"]["reason"])
