"""What one sprint listing costs the installation, counted in statements rather than in seconds.

The rule this file holds is the one the form catalogue already paid for once
(knowledge `measurements/2026-09-06-sprint-form-catalogue-n-plus-one.md`, issue:d104896882f417c1be22):
a read of N things must not be N reads. Listing every sprint of this installation is a bounded
number of reads of the store, one read of the dispatcher's production state and one traversal of
the committed audit — whatever the number of sprints. So the assertions below compare two stores
that differ only in how many sprints they carry, and require the same counts from both.

Sprints have one implementation, PostgreSQL (secretary-1670), so the cost is counted where the
store pays it: the SQL statements the client sends, and the board verbs asked of it. A batched
per-record read is answered set-based (`SqlCardClient.call_batch`), so it is one statement whatever
the number of rows it names.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
from unittest import mock

from secretary.board.sql_audit import SqlTaskAudit
from tests.fakes.sprints import SprintStoreClient
from tests.webproto_sprint_fixtures import SprintProtocolFixture

#: Cards on the Pipeline, held equal between the two stores so that the only thing that moves is the
#: number of sprints.
CARDS = 30


class SprintListingBudgetTests(SprintProtocolFixture):
    """One listing is a bounded number of statements, and adding sprints does not add any."""

    def _seed(self, sprints: int) -> None:
        """`sprints` sprints, and a fixed set of cards on the Pipeline linked to them."""
        for index in range(sprints):
            self.add_sprint_row(f"sprint:{2000 + index}", status="closed" if index % 2 else "open")
        for index in range(CARDS):
            self.board.add_card(
                5000 + index,
                f"secretary-{9000 + index}",
                title=f"card {index}",
                metadata={"task_type": "code", "sprint_ref": f"sprint:{2000 + (index % max(sprints, 1))}"},
            )

    def _counting(self) -> tuple[Counter[str], Any]:
        """Count what the store is asked from here on: statements, board verbs and batches."""
        requests: Counter[str] = Counter()
        board = self.board
        originals = {name: getattr(SprintStoreClient, name) for name in ("_query", "_execute")}

        def counted(name: str):
            def method(client: Any, *args: Any, **kwargs: Any) -> Any:
                if client is board:
                    requests["statement"] += 1
                return originals[name](client, *args, **kwargs)

            return method

        board.calls.clear()
        board.batch_calls.clear()
        patches = mock.patch.multiple(SprintStoreClient, _query=counted("_query"), _execute=counted("_execute"))
        return requests, patches

    def _verbs(self, requests: Counter[str]) -> Counter[str]:
        result = Counter(requests)
        result.update(method for method, _ in self.board.calls)
        result.update(f"batch:{method}" for batch in self.board.batch_calls for method in {m for m, _ in batch})
        return result

    def _measure(self, sprints: int) -> tuple[Counter[str], int, dict[str, Any]]:
        """One listing over `sprints` sprints: its cost, its production-state reads, its answer."""
        self._seed(sprints)
        production = self.data_dir / "dispatcher" / "production-state.json"
        reads = {"count": 0}
        original = Path.read_text

        def counting(path: Path, *args: Any, **kwargs: Any) -> str:
            if path == production:
                reads["count"] += 1
            return original(path, *args, **kwargs)

        requests, patches = self._counting()
        with patches, mock.patch.object(Path, "read_text", counting):
            document = self.reads().sprint_list()
        self.assertEqual(len(document["sprints"]["items"]), sprints)
        return self._verbs(requests), reads["count"], document

    def test_the_listing_costs_the_same_whether_it_lists_two_sprints_or_forty(self) -> None:
        small, small_reads, _ = self._measure(2)
        self.setUp()
        large, large_reads, _ = self._measure(40)

        self.assertEqual(dict(small), dict(large), "a request budget that grows with N is the N+1")
        self.assertEqual(small_reads, 1)
        self.assertEqual(large_reads, 1, "one production state read for the whole listing")

    def test_the_listing_reads_no_sprint_comments(self) -> None:
        """Comments are the per-sprint read a listing must never make."""
        requests, _, _ = self._measure(40)

        self.assertEqual(requests["getAllComments"], 0)
        self.assertEqual(requests["batch:getAllComments"], 0)

    def test_one_listing_traverses_the_committed_audit_at_most_once(self) -> None:
        traversals = {"count": 0}
        original = SqlTaskAudit.events

        def counting(audit: SqlTaskAudit, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            traversals["count"] += 1
            return original(audit, *args, **kwargs)

        self._seed(40)
        with mock.patch.object(SqlTaskAudit, "events", counting):
            self.reads().sprint_list()

        self.assertLessEqual(traversals["count"], 1, "one audit walk for the whole listing")

    def test_watching_one_sprint_costs_what_listing_them_all_does(self) -> None:
        """The two operations are one read, so neither is the cheap path around the other."""
        listed, _, _ = self._measure(40)
        self.setUp()
        self._seed(40)
        requests, patches = self._counting()
        with patches:
            self.reads().sprint_state("sprint:2000")

        self.assertEqual(dict(self._verbs(requests)), dict(listed))
