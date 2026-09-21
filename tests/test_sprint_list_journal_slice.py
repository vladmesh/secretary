"""The sprint listing reads only the committed events its verdicts need (secretary-1660).

`sprint_list` used to walk the whole committed audit for every listing: 34,530 rows and 1.7 s of a
2.2 s warm listing on production. Only a non-terminal sprint consults the journal -- its resume
freshness over its own ref and its linked cards, its current card's last transition -- so the
listing now asks the store for exactly those refs, and a listing of closed or stopped sprints asks
it for nothing.

Three things are held here. The document is the one the whole read produced, verdict for verdict,
with the old path itself as the oracle. The rows it touches follow the listing and not the history
beside it. And no unfiltered audit read passes through `sprint_list`, whichever view is listed.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar
from unittest import mock

from secretary.tasks import TaskAudit
from secretary.webproto import sprint_reads as sprint_reads_module
from secretary.webproto.sprint_reads import SprintReadLayer
from tests.webproto_sprint_fixtures import SprintProtocolFixture

#: The fixture clock is 2026-09-06T00:00:00Z; every moment below is before it.
BEFORE = "2026-09-05T08:00:00Z"
RESUMED = "2026-09-05T10:00:00Z"
AFTER = "2026-09-05T12:00:00Z"

#: The views a listing is asked for, the archive one included.
VIEWS: tuple[list[str] | None, ...] = (None, ["open"], ["closed", "stopped"], ["closed"])


def _resume(recorded_at: str, card: str) -> dict[str, Any]:
    return {
        "selected_step": "the next card",
        "selected_why": "it is next",
        "rejected_alternatives": "none",
        "current_task": card,
        "dod_state": "in progress",
        "next_safe_step": "cut it",
        "recorded_at": recorded_at,
    }


class _NarrowOnlyAudit:
    """An audit owner that refuses to be read whole and counts every row it hands out."""

    def __init__(self, inner: TaskAudit) -> None:
        self.inner = inner
        self.rows = 0
        self.reads: list[set[str]] = []

    def events(self, reference: str = "", **kwargs: Any) -> list[dict[str, Any]]:
        references = kwargs.get("references")
        if references is None and not reference:
            raise AssertionError("the sprint listing read the committed audit unfiltered")
        self.reads.append(set(references or {reference}))
        events = self.inner.events(reference, **kwargs)
        self.rows += len(events)
        return events


class SprintListJournalSliceTests(SprintProtocolFixture):
    """Two open sprints with cards and history, and closed and stopped ones beside them."""

    OPEN = ("sprint:3000", "sprint:3001")
    ENDED: ClassVar[dict[str, str]] = {"sprint:3002": "closed", "sprint:3003": "stopped", "sprint:3004": "closed"}

    def setUp(self) -> None:
        super().setUp()
        self.cards: dict[str, list[str]] = {}
        for index, reference in enumerate((*self.OPEN, *self.ENDED)):
            cards = [f"secretary-{7000 + index * 10 + offset}" for offset in range(3)]
            self.cards[reference] = cards
            self.add_sprint_row(
                reference,
                status=self.ENDED.get(reference, "open"),
                current_task=cards[0],
                resume=_resume(RESUMED, cards[0]),
            )
            for offset, card in enumerate(cards):
                self._pipeline_card(card, reference, position=index * 10 + offset)
        # sprint:3000 has a significant event after its resume, so its verdict is stale; sprint:3001
        # has only earlier ones and stays fresh. Both current cards carry a transition to date.
        self._move(self.cards["sprint:3000"][1], BEFORE, "in_progress", "assessment")
        self._move(self.cards["sprint:3000"][0], AFTER, "validate", "blocked")
        self._move(self.cards["sprint:3001"][0], BEFORE, "ready", "in_progress")
        self._move(self.cards["sprint:3001"][2], BEFORE, "validate", "done")
        self._po_comment("sprint:3001", BEFORE)
        # The ended sprints have history of their own after their resumes; none of it may be read.
        for reference in self.ENDED:
            self._move(self.cards[reference][0], AFTER, "validate", "done")
            self._po_comment(reference, AFTER)

    # -- the board and the journal -------------------------------------------------------------

    def _pipeline_card(self, card: str, sprint: str, *, position: int) -> None:
        pipeline = self.board.projects["Pipeline"]
        column = next(
            entry["id"] for entry in self.board.columns[pipeline] if entry["title"] == "In progress"
        )
        task_id = 8000 + position
        self.board.tasks.append(
            {
                "id": task_id,
                "project_id": pipeline,
                "reference": card,
                "title": card,
                "description": "",
                "column_id": column,
                "position": position + 1,
                "swimlane_id": 0,
                "date_creation": "1720000000",
                "date_modification": "1720000000",
            }
        )
        self.board.metadata[task_id] = {"project": "secretary", "task_type": "code", "sprint_ref": sprint}
        self.board.comments[task_id] = []

    def _append(self, event: dict[str, Any]) -> None:
        with (self.data_dir / "board" / "events.ndjson").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def _move(self, card: str, at: str, source: str, target: str) -> None:
        self._append(
            {
                "event_id": f"board-event-{card}-{at}-{target}",
                "schema_version": 1,
                "record_type": TaskAudit._PROTOCOL_EVENT_RECORD_TYPE,
                "kind": "card.moved",
                "ref": card,
                "occurred_at": at,
                "actor": {"role": "dispatcher", "id": "dispatcher"},
                "transition": {"source": source, "target": target},
            }
        )

    def _po_comment(self, sprint: str, at: str) -> None:
        self._append(
            {
                "event_id": f"evt-{sprint}-{at}-commented",
                "schema_version": 1,
                "kind": "commented",
                "outcome": "success",
                "ref": sprint,
                "occurred_at": at,
                "actor": {"role": "po", "id": "po"},
                "payload": {"body": "how is it going?"},
            }
        )

    def _unrelated_history(self, count: int) -> None:
        """History of cards no listed sprint links, in every shape the journal holds."""
        for index in range(count):
            card = f"secretary-{100000 + index}"
            self._move(card, BEFORE, "ready", "done")
            self._po_comment(f"sprint:{90000 + index}", AFTER)

    # -- the reads -----------------------------------------------------------------------------

    def _whole_read(self, statuses: list[str] | None) -> dict[str, Any]:
        """The listing as the read-everything path assembled it: the oracle."""
        original = SprintReadLayer._read_once

        def whole(layer: SprintReadLayer, *args: Any, **kwargs: Any) -> Any:
            kwargs.pop("listing", None)
            return original(layer, *args, **kwargs)

        with mock.patch.object(SprintReadLayer, "_read_once", whole):
            return self.reads().sprint_list(statuses=statuses)

    def _narrow(self, statuses: list[str] | None) -> tuple[dict[str, Any], _NarrowOnlyAudit]:
        audit = _NarrowOnlyAudit(TaskAudit(self.data_dir))
        with mock.patch.object(sprint_reads_module, "task_audit_for", return_value=audit):
            return self.reads().sprint_list(statuses=statuses), audit

    def _entry(self, document: dict[str, Any], reference: str) -> dict[str, Any]:
        return next(item for item in document["sprints"]["items"] if item["ref"] == reference)

    # -- the cases -----------------------------------------------------------------------------

    def test_the_document_is_the_whole_read_s_document(self) -> None:
        """Criterion 2: every view, every verdict, byte for byte against the old path."""
        for statuses in VIEWS:
            with self.subTest(statuses=statuses):
                narrowed, _audit = self._narrow(statuses)
                self.assertEqual(narrowed, self._whole_read(statuses))

        # The comparison is only worth something if the verdicts it compares differ from each other.
        listed, _audit = self._narrow(["open"])
        stale = self._entry(listed, "sprint:3000")["decision"]["freshness"]["value"]
        fresh = self._entry(listed, "sprint:3001")["decision"]["freshness"]["value"]
        self.assertEqual((stale["fresh"], stale["last_event_at"]), (False, AFTER))
        self.assertEqual((fresh["fresh"], fresh["last_event_at"]), (True, BEFORE))
        standing = self._entry(listed, "sprint:3000")["current_card_state"]
        self.assertEqual((standing["transition"], standing["since"]), ("recorded", AFTER))

    def test_the_read_asks_for_the_open_sprints_refs_and_their_cards_only(self) -> None:
        """Criterion 1: one narrowed read, of the listed non-terminal sprints and their cards."""
        expected = set(self.OPEN) | {card for sprint in self.OPEN for card in self.cards[sprint]}
        for statuses in (None, ["open"]):
            with self.subTest(statuses=statuses):
                _document, audit = self._narrow(statuses)
                self.assertEqual(audit.reads, [expected])

    def test_the_archive_view_reads_no_journal_at_all(self) -> None:
        """Criterion 1: closed and stopped sprints are judged against their own record."""
        for statuses in (["closed", "stopped"], ["closed"], ["stopped"]):
            with self.subTest(statuses=statuses):
                document, audit = self._narrow(statuses)
                self.assertEqual(audit.reads, [])
                self.assertEqual(audit.rows, 0)
                self.assertTrue(document["sprints"]["items"])
                self.assertEqual(document["journal"]["source"]["state"], "available")

    def test_the_rows_touched_follow_the_listing_and_not_the_history(self) -> None:
        """Criterion 3: ten times the unrelated history, the same rows read."""
        self._unrelated_history(20)
        _document, small = self._narrow(["open"])
        _document, small_all = self._narrow(None)
        journal = self.data_dir / "board" / "events.ndjson"
        before = len(journal.read_text(encoding="utf-8").splitlines())

        self._unrelated_history(200)
        after = len(journal.read_text(encoding="utf-8").splitlines())
        self.assertGreaterEqual(after - before, 10 * 40)
        _document, large = self._narrow(["open"])
        _document, large_all = self._narrow(None)

        self.assertGreater(small.rows, 0)
        self.assertEqual(small.rows, large.rows)
        self.assertEqual(small_all.rows, large_all.rows)
        self.assertEqual(small.rows, small_all.rows, "closed sprints add no rows to the read")

    def test_an_unfiltered_read_never_passes_through_either_view(self) -> None:
        """Criterion 3: the raising double sits under both the open and the whole listing."""
        for statuses in (["open"], None):
            with self.subTest(statuses=statuses):
                document, audit = self._narrow(statuses)
                self.assertEqual(document["journal"]["source"]["state"], "available")
                self.assertTrue(audit.reads)
