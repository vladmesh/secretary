"""What one sprint listing costs the installation, counted in requests rather than in seconds.

The rule this file holds is the one the form catalogue already paid for once
(knowledge `measurements/2026-09-06-sprint-form-catalogue-n-plus-one.md`, issue:d104896882f417c1be22):
a read of N things must not be N reads. Listing every sprint of this installation is one pass over
the sprint board, one listing of the Pipeline, one read of the dispatcher's production state and one
traversal of the committed audit — whatever the number of sprints. So the assertions below compare
two boards that differ only in how many sprints they carry, and require the same counts from both.

Counting is by request and not by batch member, for the same reason the catalogue's budget is:
`KanboardClient.call_batch` puts one request on the wire per chunk, and counting the members would
make a batched read look exactly like the per-row reads it replaces.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any
from unittest import mock

from secretary.tasks import _BATCH_CHUNK
from tests.fakes.sprints import ProductSprintKanboard
from tests.webproto_sprint_fixtures import SprintProtocolFixture

#: Cards on the Pipeline, held equal between the two boards so that the only thing that moves is the
#: number of sprints.
CARDS = 30


class CountingSprintBoard(ProductSprintKanboard):
    """The fixture's board, counting what a real transport would put on the wire."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: Counter[str] = Counter()

    def call(self, method: str, **params: Any) -> Any:
        self.requests[method] += 1
        return super().call(method, **params)

    def call_batch(self, calls):
        batch = list(calls)
        methods = {method for method, _ in batch}
        assert len(methods) <= 1, "this fixture only batches one method at a time"
        before = Counter(self.requests)
        results = super().call_batch(batch)
        self.requests = before
        for method in methods:
            self.requests[method] += math.ceil(len(batch) / _BATCH_CHUNK)
        return results


class SprintListingBudgetTests(SprintProtocolFixture):
    """One listing is a bounded number of requests, and adding sprints does not add any."""

    def setUp(self) -> None:
        super().setUp()
        self.board = CountingSprintBoard()

    def _seed(self, sprints: int) -> None:
        """`sprints` sprints on the sprint board, and a fixed set of cards on the Pipeline."""
        for index in range(sprints):
            reference = f"sprint:{2000 + index}"
            self.add_sprint_row(
                reference,
                status="closed" if index % 2 else "open",
                current_task=f"secretary-{9000 + index}",
            )
        pipeline = self.board.projects["Pipeline"]
        ready = next(column["id"] for column in self.board.columns[pipeline] if column["title"] == "Ready")
        for index in range(CARDS):
            task_id = 5000 + index
            self.board.tasks.append(
                {
                    "id": task_id,
                    "project_id": pipeline,
                    "reference": f"secretary-{9000 + index}",
                    "title": f"card {index}",
                    "description": "",
                    "column_id": ready,
                    "position": index + 1,
                    "swimlane_id": 0,
                    "date_creation": "1720000000",
                    "date_modification": "1720000000",
                }
            )
            self.board.metadata[task_id] = {
                "project": "secretary",
                "task_type": "code",
                "sprint_ref": f"sprint:{2000 + (index % max(sprints, 1))}",
            }
            self.board.comments[task_id] = []

    def _measure(self, sprints: int) -> tuple[Counter[str], int, dict[str, Any]]:
        """One listing over `sprints` sprints: its requests, its production-state reads, its answer."""
        self._seed(sprints)
        self.board.requests.clear()
        production = self.data_dir / "dispatcher" / "production-state.json"
        reads = {"count": 0}
        original = Path.read_text

        def counting(path: Path, *args: Any, **kwargs: Any) -> str:
            if path == production:
                reads["count"] += 1
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", counting):
            document = self.reads().sprint_list()
        self.assertEqual(len(document["sprints"]["items"]), sprints)
        return Counter(self.board.requests), reads["count"], document

    def test_the_listing_costs_the_same_whether_it_lists_two_sprints_or_forty(self) -> None:
        small, small_reads, _ = self._measure(2)
        self.setUp()
        large, large_reads, _ = self._measure(40)

        self.assertEqual(dict(small), dict(large), "a request budget that grows with N is the N+1")
        self.assertEqual(small_reads, 1)
        self.assertEqual(large_reads, 1, "one production state read for the whole listing")

    def test_the_listing_is_one_pass_per_board_and_batched_metadata(self) -> None:
        requests, production_reads, _ = self._measure(40)

        # One pass over the sprint board, one over the Pipeline.
        self.assertEqual(requests["getAllTasks"], 2)
        self.assertEqual(requests["getProjectByName"], 2)
        self.assertEqual(requests["getColumns"], 1)
        # Metadata grows with the number of batches and not with the number of rows: 40 sprints and
        # the whole Pipeline are two batched reads, one per board.
        self.assertEqual(requests["getTaskMetadata"], 2)
        self.assertLess(requests["getTaskMetadata"], 40)
        # Comments are the per-sprint read this listing must never make.
        self.assertEqual(requests["getAllComments"], 0)
        self.assertEqual(production_reads, 1)

    def test_one_listing_traverses_the_committed_audit_at_most_once(self) -> None:
        from secretary.tasks import TaskAudit

        traversals = {"count": 0}
        original = TaskAudit.events

        def counting(audit: TaskAudit, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            traversals["count"] += 1
            return original(audit, *args, **kwargs)

        self._seed(40)
        with mock.patch.object(TaskAudit, "events", counting):
            self.reads().sprint_list()

        self.assertLessEqual(traversals["count"], 1, "one audit walk for the whole listing")

    def test_watching_one_sprint_costs_what_listing_them_all_does(self) -> None:
        """The two operations are one read, so neither is the cheap path around the other."""
        listed, _, _ = self._measure(40)
        self.setUp()
        self._seed(40)
        self.board.requests.clear()
        self.reads().sprint_state("sprint:2000")

        self.assertEqual(dict(self.board.requests), dict(listed))
