"""The committed audit's narrowed reads over PostgreSQL: served by an index, sized by the slice.

`SqlTaskAudit.events` used to read every committed `requests` row and filter in Python. It now
narrows by a ref, a set of refs, a kind and a settle-time window in SQL, pages from the claim-order
index, and the occurrence projections read one kind's slice (secretary-1658). Four proofs:

* **Index availability.** Every SELECT the audit issues is explained with `enable_seqscan = off`:
  a tiny table makes the planner prefer a sequential scan whatever indexes exist, and with the
  setting off a plan still shows `Seq Scan on requests` only when no index can serve it.
* **Cost follows the slice.** One sprint's and one task's read touch the same number of rows
  whether the unrelated history is N or 10×N.
* **Same answers.** The old read-all-then-filter is kept below as the oracle (`_oracle_events`,
  verbatim in its SQL and its Python filter) and every narrowed read is compared against it,
  including kind matches through `_event_action`.
* **Correct before the migration runs.** With `0012`'s indexes dropped, the answers are the same.

Like the other `*_sql_backend` suites this needs Docker and never skips.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from secretary.board import sql_audit
from secretary.board.models import EventKind
from secretary.board.sql_audit import SqlTaskAudit
from secretary.board.sql_cards import SqlCardClient
from secretary.tasks import _event_action, _projection_slice
from tests.sql_backend_fixtures import PostgresBoard

BOARD: PostgresBoard


def setUpModule() -> None:
    global BOARD
    for module in ("psycopg", "sqlalchemy", "alembic"):
        __import__(module)
    BOARD = PostgresBoard()


def tearDownModule() -> None:
    BOARD.stop()


_EPOCH = datetime(2026, 9, 1, tzinfo=UTC)

#: The indexes `0012_request_read_indexes` adds, dropped by the before-the-migration proof.
_READ_INDEXES = (
    "requests_committed_by_ref",
    "requests_committed_in_claim_order",
    "requests_staged_in_claim_order",
    "requests_by_kind",
    "requests_by_event_id",
    "requests_owing_outcome",
)

SPRINT = "sprint:7"
CARDS = ("secretary-10", "secretary-11", "secretary-12")
TASK = "secretary-11"
USAGE = EventKind.ATTEMPT_USAGE.value
OUTCOME = EventKind.ATTEMPT_OUTCOME.value
REPORTED = EventKind.CARD_REPORTED.value


def _oracle_events(client: SqlCardClient, reference: str = "", *, kind: str = "") -> list[dict[str, Any]]:
    """`SqlTaskAudit.events` as it was before secretary-1658: read all, then filter in Python."""
    rows = client._query(
        "SELECT intent FROM requests WHERE status = 'committed' "
        "ORDER BY settled_at, created_at, request_id"
    )
    result = []
    for (intent,) in rows:
        event = SqlTaskAudit._document(intent)
        if reference and event.get("ref") != reference:
            continue
        if kind and event.get("kind") != kind and _event_action(event) != kind:
            continue
        result.append(event)
    return result


def _oracle_settled(client: SqlCardClient) -> list[tuple[dict[str, Any], datetime]]:
    rows = client._query(
        "SELECT intent, settled_at FROM requests WHERE status = 'committed' "
        "ORDER BY settled_at, created_at, request_id"
    )
    return [(SqlTaskAudit._document(intent), settled) for intent, settled in rows]


class _Clock:
    """A settle time that moves one second per claim, so windows have edges to fall on."""

    def __init__(self) -> None:
        self.now = _EPOCH

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def _record(request_id: str, ref: str, kind: str, **extra: Any) -> dict[str, Any]:
    record = {
        "request_id": request_id,
        "event_id": extra.pop("event_id", f"evt-{request_id}"),
        "ref": ref,
        "kind": kind,
        "outcome": "success",
        "actor": {"role": "worker", "id": "fixture"},
        # About the size of a real intent, so a scan of the history has something to pay for.
        "payload": {"note": "x" * 600, **extra.pop("payload", {})},
    }
    record.update(extra)
    return record


def _seed(client: SqlCardClient, audit: SqlTaskAudit, unrelated: int) -> None:
    """The sprint, its cards and one task's history, woven through `unrelated` foreign records."""
    ours: list[tuple[str, dict[str, Any]]] = []
    for index in range(4):
        ours.append(("commit", _record(f"sprint-{index}", SPRINT, "commented")))
    for card in CARDS:
        for index, kind in enumerate(("created", "moved", REPORTED, "reported", "routing", "commented")):
            ours.append(("commit", _record(f"{card}-{index}", card, kind)))
    # The occurrence projections' slice: usage and outcome records committed and staged, a
    # non-usage record sharing an event id with a usage one, and effects owing an outcome.
    ours += [
        ("commit", _record("usage-1", TASK, USAGE)),
        ("stage", _record("usage-2", TASK, USAGE)),
        ("commit", _record("usage-twin", TASK, "commented", event_id="evt-usage-1")),
        ("commit", _record("outcome-1", TASK, OUTCOME)),
        ("stage", _record("outcome-2", CARDS[0], OUTCOME)),
        ("commit", _record("effect-1", TASK, "moved", data={"attempt_outcome_owed": {"card_ref": TASK}})),
        ("stage", _record("effect-2", CARDS[2], "moved", data={"attempt_outcome_owed": {}})),
    ]
    foreign_kinds = ("created", "moved", "commented", REPORTED, "routing", "claimed")
    foreign = [
        ("commit", _record(f"foreign-{index}", f"other-{index % 97}", foreign_kinds[index % len(foreign_kinds)]))
        for index in range(unrelated)
    ]
    per = unrelated // len(ours)
    sequence: list[tuple[str, dict[str, Any]]] = []
    for index, entry in enumerate(ours):
        sequence += foreign[index * per : (index + 1) * per]
        sequence.append(entry)
    sequence += foreign[len(ours) * per :]
    with client.transaction():
        for action, record in sequence:
            status = "committed" if action == "commit" else "staged"
            audit._claim_row(record["request_id"], record, status=status)
    client._execute("ANALYZE requests")
    client._commit_unless_nested()


class _Case(unittest.TestCase):
    def store(self, unrelated: int) -> tuple[SqlCardClient, SqlTaskAudit]:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        client = SqlCardClient(BOARD.fresh_database().for_role("owner"), Path(root.name))
        self.addCleanup(client.close)
        audit = SqlTaskAudit(client)
        with mock.patch.object(sql_audit, "_now", _Clock()):
            _seed(client, audit, unrelated)
        return client, audit

    @staticmethod
    def reads(audit: SqlTaskAudit, window: datetime) -> dict[str, Any]:
        """Every read the tick and web callers make, and the lookups beside them."""
        return {
            "task": audit.events(TASK),
            "task_reported": audit.events(TASK, kind="reported"),
            "task_routing": audit.events(TASK, kind="routing"),
            "created": audit.events(kind="created"),
            "sprint": audit.events(references={SPRINT, *CARDS}),
            "window": audit.events(since=window),
            "first_page": audit.events_page(end=None, limit=5),
            "older_page": audit.events_page(end=9, limit=5),
            "owner": audit.event_id_owner("evt-usage-1"),
            "pending": audit.pending_events(),
            "status": audit.status(),
            "usage": audit._occurrence_projection_records((USAGE,)),
            "outcome": audit._occurrence_projection_records((OUTCOME,), outcome_owed=True),
            "all": audit.events(),
        }


class IndexAvailabilityTests(_Case):
    """AC1: every query the audit issues is served by an index."""

    def test_no_query_the_audit_issues_scans_requests_sequentially(self) -> None:
        client, audit = self.store(200)
        issued: list[tuple[str, tuple[Any, ...]]] = []
        original = client._query

        def recording(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
            if sql.lstrip().upper().startswith(("SELECT", "WITH")) and "requests" in sql:
                issued.append((sql, params))
            return original(sql, params)

        with mock.patch.object(client, "_query", recording):
            self.reads(audit, _EPOCH + timedelta(seconds=100))
            audit.committed_event("usage-1")
            audit.pending_event("usage-2")
        self.assertGreaterEqual(len({sql for sql, _params in issued}), 10)
        client._execute("SET enable_seqscan = off")
        for sql, params in issued:
            with self.subTest(sql=sql):
                plan = "\n".join(row[0] for row in client._query("EXPLAIN " + sql, params))
                self.assertNotIn("Seq Scan on requests", plan, plan)


class SliceCostTests(_Case):
    """AC2: the rows a sprint's and a task's read touch do not grow with unrelated history."""

    N = 300

    @staticmethod
    def touched(client: SqlCardClient, sql: str, params: tuple[Any, ...]) -> int:
        """Rows the executed plan read from `requests`: returned plus filtered away, per loop.

        With `enable_seqscan = off`, for the reason the index-availability proof gives: the small
        store here would otherwise be scanned whole because that is cheaper at its size, which is
        a statement about 300 rows and not about the 33,675 of production, where the planner takes
        the index for a slice this selective on its own (the card report carries those plans).
        """
        client._execute("SET enable_seqscan = off")
        (document,) = client._query("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)[0]
        plan = document if isinstance(document, list) else json.loads(document)
        total = 0

        def walk(node: dict[str, Any]) -> None:
            nonlocal total
            if node.get("Relation Name") == "requests":
                loops = int(node.get("Actual Loops") or 1)
                removed = int(node.get("Rows Removed by Filter") or 0)
                removed += int(node.get("Rows Removed by Index Recheck") or 0)
                total += (int(node.get("Actual Rows") or 0) + removed) * loops
            for child in node.get("Plans") or ():
                walk(child)

        walk(plan[0]["Plan"])
        return total

    def measure(self, unrelated: int) -> dict[str, int]:
        client, audit = self.store(unrelated)
        issued: dict[str, tuple[str, tuple[Any, ...]]] = {}
        original = client._query

        def capture(name: str, read: Any) -> None:
            def recording(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
                issued[name] = (sql, params)
                return original(sql, params)

            with mock.patch.object(client, "_query", recording):
                read()

        capture("sprint", lambda: audit.events(references={SPRINT, *CARDS}))
        capture("task", lambda: audit.events(TASK))
        return {name: self.touched(client, *issued[name]) for name in issued}

    def test_one_sprint_and_one_task_touch_the_same_rows_over_ten_times_the_history(self) -> None:
        small = self.measure(self.N)
        large = self.measure(10 * self.N)

        self.assertEqual(small, large, f"rows touched grew with unrelated history: {small} -> {large}")
        self.assertEqual(large["task"], len(_oracle_events(self.store(0)[0], TASK)))


class PageSnapshotTests(unittest.TestCase):
    """One traversal of `/commands` pages skips and repeats nothing while commands commit.

    A second connection commits a new record after every statement the reading connection issues,
    which is where a command commit lands when the count and the page are two statements: page one
    then counts the old total and reads past a record the next page will not reach.
    """

    def test_a_commit_between_page_reads_skips_or_repeats_no_row(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        config = BOARD.fresh_database().for_role("owner")
        reader = SqlCardClient(config, Path(root.name))
        self.addCleanup(reader.close)
        writer = SqlCardClient(config, Path(root.name))
        self.addCleanup(writer.close)
        audit, other = SqlTaskAudit(reader), SqlTaskAudit(writer)
        for index in range(23):
            other.append(f"seed-{index}", _record(f"seed-{index}", f"card-{index % 3}", "commented"))
        before = [event["request_id"] for event in _oracle_events(reader)]
        reader._commit_unless_nested()
        commits = iter(range(1000))
        original = reader._query

        def interleaved(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
            rows = original(sql, params)
            reader._commit_unless_nested()
            index = next(commits)
            other.append(f"late-{index}", _record(f"late-{index}", "card-late", "commented"))
            return rows

        seen: list[str] = []
        end: int | None = None
        with mock.patch.object(reader, "_query", interleaved):
            while end != 0:
                total, page = audit.events_page(end=end, limit=5)
                stop = total if end is None else end
                seen = [event["request_id"] for event in page] + seen
                end = max(0, stop - 5)

        self.assertGreater(next(commits), 4, "commands committed while the traversal read")
        self.assertEqual(seen, before)


class SameAnswersTests(_Case):
    """AC3: the SQL-filtered reads answer what read-all-then-filter answered."""

    def assert_same(self, client: SqlCardClient, audit: SqlTaskAudit) -> None:
        for reference in ("", SPRINT, TASK, CARDS[0], "other-3", "nobody"):
            for kind in ("", "created", "reported", REPORTED, "verdict", "routing", USAGE, "nothing"):
                with self.subTest(reference=reference, kind=kind):
                    self.assertEqual(
                        audit.events(reference, kind=kind), _oracle_events(client, reference, kind=kind)
                    )
        everything = _oracle_events(client)
        for refs in ({SPRINT, *CARDS}, {TASK}, {"other-1", "other-2"}, set()):
            with self.subTest(references=refs):
                self.assertEqual(
                    audit.events(references=refs), [event for event in everything if event.get("ref") in refs]
                )
        self.assertEqual(
            audit.events(TASK, kind="reported", references={TASK, SPRINT}),
            _oracle_events(client, TASK, kind="reported"),
        )
        settled = _oracle_settled(client)
        for index in (0, 1, len(settled) // 2, len(settled) - 1):
            since = settled[index][1]
            with self.subTest(since=since):
                self.assertEqual(
                    audit.events(since=since), [event for event, at in settled if at >= since]
                )
        for end in (None, 0, 3, 9, len(everything)):
            for limit in (1, 5, 50):
                with self.subTest(end=end, limit=limit):
                    stop = len(everything) if end is None else end
                    self.assertEqual(
                        audit.events_page(end=end, limit=limit),
                        (len(everything), everything[max(0, stop - limit) : stop]),
                    )
        self.assertEqual(audit.events_page(end=len(everything) + 1, limit=5), (len(everything), []))
        full = audit._occurrence_projection_records()
        for kinds, owed in (((USAGE,), False), ((OUTCOME,), False), ((OUTCOME,), True)):
            with self.subTest(kinds=kinds, owed=owed):
                self.assertEqual(
                    audit._occurrence_projection_records(kinds, outcome_owed=owed),
                    _projection_slice(full, frozenset(kinds), owed),
                )
        usage = [record["request_id"] for record, _pending in audit._occurrence_projection_records((USAGE,))]
        self.assertEqual(usage, ["usage-1", "usage-twin", "usage-2"])
        self.assertEqual(audit.event_id_owner("evt-usage-1"), "usage-1")

    def test_narrowed_reads_equal_the_read_all_oracle(self) -> None:
        client, audit = self.store(150)
        self.assert_same(client, audit)

    def test_the_answers_are_the_same_before_the_indexes_exist(self) -> None:
        """AC5: the dispatcher runs this code before the PO's upgrade adds `0012`'s indexes."""
        client, audit = self.store(150)
        indexed = self.reads(audit, _EPOCH + timedelta(seconds=100))
        with client.transaction():
            for name in _READ_INDEXES:
                client._execute(f"DROP INDEX {name}")
        self.assertEqual(
            client._query(
                "SELECT count(*) FROM pg_indexes WHERE tablename = 'requests' AND indexname = ANY(%s)",
                (list(_READ_INDEXES),),
            )[0][0],
            0,
        )

        self.assertEqual(self.reads(audit, _EPOCH + timedelta(seconds=100)), indexed)
        self.assert_same(client, audit)


if __name__ == "__main__":
    unittest.main()
