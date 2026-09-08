"""`TaskReader` and `TaskWriter` on the PostgreSQL backend, against a real `postgres:16`.

Two things are proved here and they are deliberately different in kind.

`SqlBackendSwitchTests` is about the switch itself (`board/backend.py`) and needs no database.

Everything below it is the card contract on the second implementation: the reader returns what
`TaskReader` returns today, and the writer's mutations land — request claim, card effect and
event together — as one transaction (`docs/BOARD_STORE.md` §7.1).  The classes reuse the Kanboard
fakes' own board as their starting state (`tests/sql_backend_fixtures.py`) so the two backends are
asked the same questions about the same cards rather than about two boards that happen to look
alike.

The writer's cases reach the board the same way: since secretary-1590 they arrange it through
the card client's own protocol (`BoardFixture` in tests/test_tasks.py) and read it back through
`TaskReader`, rather than assigning into the Kanboard fake's rows and metadata map.  That is why
`KANBOARD_ONLY` below is now the whole list of omissions and "the fixture looks inside the fake"
is no longer one of its reasons.

Where an expectation genuinely cannot be shared, the difference is stated here rather than
softened: the card's identity.  §9 makes a card's reference its stable identifier and the store
holds no Kanboard integer, so `tasks.task_number` is the number this backend answers with and the
normalized `id` reads `task_postgres_468` where Kanboard's reads `task_kanboard_12`.  That is a
different value for the same card, and a test that asserted one spelling cannot assert both.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from secretary.board import backend
from secretary.board.sql_cards import _COLUMN_ID_BY_STATE
from secretary.tasks import TaskError, TaskReader, TaskWriter

# Imported as a module, not by name: a bare import would make unittest collect the Kanboard
# cases a second time here, once more on the backend they already run on in test_tasks.py.
from tests import test_tasks as kanboard_cases
from tests.fakes.tasks import FakeKanboard, WriteKanboard
from tests.sql_backend_fixtures import PostgresBoard, seed_client


class SqlBackendSwitchTests(unittest.TestCase):
    """The switch is one named place, decided once, and refuses what it does not know."""

    def setUp(self) -> None:
        backend.reset_card_backend()
        self.addCleanup(backend.reset_card_backend)

    def _with(self, value: str | None):
        previous = os.environ.get(backend.CARD_BACKEND_ENV)
        if value is None:
            os.environ.pop(backend.CARD_BACKEND_ENV, None)
        else:
            os.environ[backend.CARD_BACKEND_ENV] = value
        self.addCleanup(
            lambda: os.environ.__setitem__(backend.CARD_BACKEND_ENV, previous)
            if previous is not None
            else os.environ.pop(backend.CARD_BACKEND_ENV, None)
        )

    def test_the_default_is_kanboard_and_no_file_is_consulted(self) -> None:
        self._with(None)
        self.assertEqual(backend.card_backend(), "kanboard")
        self.assertEqual(backend.card_backend_status()["source"], "default")

    def test_postgres_is_chosen_by_the_name_and_not_by_a_present_store_file(self) -> None:
        self._with("postgres")
        self.assertEqual(backend.card_backend(), "postgres")
        self.assertEqual(backend.card_backend_status()["backend"], "postgres")

    def test_an_unknown_value_refuses_with_its_reason(self) -> None:
        self._with("mysql")
        with self.assertRaises(backend.BoardBackendError) as raised:
            backend.card_backend()
        self.assertIn("kanboard, postgres", str(raised.exception))
        self.assertIn("mysql", str(raised.exception))

    def test_the_decision_is_taken_once_per_process(self) -> None:
        self._with("postgres")
        self.assertEqual(backend.card_backend(), "postgres")
        os.environ[backend.CARD_BACKEND_ENV] = "kanboard"
        self.assertEqual(backend.card_backend(), "postgres")

    def test_status_reports_the_refusal_rather_than_a_backend(self) -> None:
        self._with("mysql")
        report = backend.card_backend_status()
        self.assertIsNone(report["backend"])
        self.assertTrue(report["findings"])


class SqlBoardCase(unittest.TestCase):
    """One container per module, one migrated database and one seeded board per test."""

    board: PostgresBoard

    @classmethod
    def setUpClass(cls) -> None:
        for module in ("psycopg", "sqlalchemy", "alembic"):
            __import__(module)
        cls.board = PostgresBoard()
        cls.addClassCleanup(cls.board.stop)

    def client_for(self, fake) -> object:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        config = self.board.fresh_database()
        client = seed_client(config, fake, Path(self.tmpdir.name))
        self.addCleanup(client.close)
        return client


class SqlTaskReaderTests(SqlBoardCase):
    """AC2: everything the reader returns today, returned from PostgreSQL in the same shape."""

    def setUp(self) -> None:
        self.client = self.client_for(FakeKanboard())
        self.reader = TaskReader(self.client)  # type: ignore[arg-type]

    def test_list_normalizes_and_filters_the_same_fields(self) -> None:
        result = self.reader.list(states={"ready"}, project="secretary")

        self.assertEqual([task["ref"] for task in result], ["secretary-468"])
        task = result[0]
        # The one field that cannot be shared with the Kanboard run: §9's identity.
        self.assertEqual(task["id"], "task_postgres_468")
        self.assertEqual(task["claim"], {"worker": "codex-terra", "claimed_at": None})
        self.assertEqual(
            task["retry"], {"same": 2, "switched": 0, "heads": ["codex-terra", "claude-opus"]}
        )
        self.assertEqual(task["routing"]["complexity"], "standard")
        self.assertEqual(task["routing"]["codex_launch_mode"], "tui")
        self.assertEqual(
            task["extensions"]["kanboard"], {"steward_report": "1", "swimlane": "Secretary"}
        )
        self.assertNotIn("comments", task)

    def test_show_preserves_comments_and_legacy_defaults(self) -> None:
        task = self.reader.show("old-1")

        self.assertEqual(task["project"], "")
        self.assertEqual(task["type"], "")
        self.assertIsNone(task["blocked_by"])
        self.assertEqual(task["position"], 0)
        self.assertEqual(task["state"], "issues")
        self.assertEqual(task["audit"]["backend"]["kind"], "postgres")

    def test_export_carries_the_same_projection(self) -> None:
        rows = {row["reference"]: row for row in self.reader.export()}

        self.assertEqual(set(rows), {"secretary-468", "old-1"})
        self.assertEqual(rows["secretary-468"]["column"], "Ready")
        self.assertEqual(rows["secretary-468"]["swimlane"], "Secretary")
        self.assertEqual(rows["secretary-468"]["project"], "secretary")
        self.assertEqual(rows["secretary-468"]["task_type"], "code")

    def test_restore_snapshot_returns_every_card_by_reference(self) -> None:
        snapshot = self.reader.restore_snapshot()

        self.assertEqual(set(snapshot), {"secretary-468", "old-1"})
        self.assertIn("comments", snapshot["secretary-468"])

    def test_steward_signal_cards_report_the_bounded_view(self) -> None:
        cards = self.reader.steward_signal_cards(project="secretary")

        self.assertEqual(
            cards,
            [
                {
                    "reference": "secretary-468",
                    "state": "ready",
                    "column": "Ready",
                    "project": "secretary",
                    "date_moved": None,
                    "steward_report": "1",
                }
            ],
        )


class SqlTaskWriterTests(SqlBoardCase):
    """AC3: one transaction per protocol mutation, and the request-id refusal it must keep."""

    def setUp(self) -> None:
        fake = WriteKanboard()
        self.client = self.client_for(fake)
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)  # type: ignore[arg-type]
        self.reader = TaskReader(self.client)  # type: ignore[arg-type]

    # --- the transition's transaction boundary ---------------------------------------

    def _place(self, reference: str, state: str) -> None:
        """Put a card in a column through the client's own protocol, not through its rows."""
        card = self.reader.show(reference)
        self.client.call(
            "moveTaskPosition",
            project_id=1,
            task_id=int(card["id"].rsplit("_", 1)[1]),
            column_id=_COLUMN_ID_BY_STATE[state],
            position=1,
            swimlane_id=0,
        )

    def _set_metadata(self, reference: str, **values: str) -> None:
        card = self.reader.show(reference)
        self.client.call(
            "saveTaskMetadata", task_id=int(card["id"].rsplit("_", 1)[1]), values=dict(values)
        )

    @contextlib.contextmanager
    def _drops_the_call_after(self, method: str):
        """The round trip after `method` is lost; `method` itself already landed."""
        served = self.client.call
        armed = False

        def call(name: str, /, **params):
            nonlocal armed
            if armed:
                armed = False
                raise TaskError("backend_unavailable", "the board is unavailable", 1)
            result = served(name, **params)
            if name == method:
                armed = True
            return result

        with mock.patch.object(self.client, "call", side_effect=call):
            yield

    @contextlib.contextmanager
    def _loses_the_reply_to(self, method: str):
        """`method` is applied and then its reply is lost, once."""
        served = self.client.call
        lost = False

        def call(name: str, /, **params):
            nonlocal lost
            result = served(name, **params)
            if name == method and not lost:
                lost = True
                raise TaskError("backend_unavailable", "the board is unavailable", 1)
            return result

        with mock.patch.object(self.client, "call", side_effect=call):
            yield

    def assertNoRepairIsOwed(self, error: TaskError) -> None:
        """The refusal says the mutation did not happen, and the audit agrees with it.

        The point of the check is the *absence* of a repair obligation. `audit_pending` promises
        the caller that a board write is committed and that `reconcile` owes it a repair; after a
        rollback both halves are false, and a caller that believed the sentence would wait for a
        repair `SqlTaskAudit.reconcile` can never perform.
        """
        self.assertNotEqual(error.code, "audit_pending")
        self.assertEqual(error.code, "backend_error")
        self.assertEqual(error.exit_code, 1)
        self.assertIn("no repair is owed", str(error))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(self.writer.reconcile(), (0, 0))

    def assertNothingSurvived(self, request_id: str) -> None:
        """Neither half of the mutation is there: no request row, and no published event."""
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM requests WHERE request_id = %s", (request_id,)
            ),
            [(0,)],
        )
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM board_events WHERE request_id = %s", (request_id,)
            ),
            [(0,)],
        )

    def test_a_failure_after_the_column_move_leaves_neither_the_move_nor_a_staged_request(
        self,
    ) -> None:
        """§7.1 for the transition, at the first of its three post-effect points.

        `moveTaskPosition` returned and the round trip after it was lost, which on Kanboard is
        exactly the state `test_a_transport_failure_after_the_move_keeps_the_typed_pending_record`
        recovers from: a card in Validate beside a staged request.  Here the move is a statement of
        the same transaction as the claim, so the rollback takes both and there is nothing to
        recover.
        """
        self._place("secretary-468", "in_progress")

        with self._drops_the_call_after("moveTaskPosition"), self.assertRaises(TaskError) as raised:
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="validate",
                reason="submit",
                request_id="rq-move-lost-read-back",
            )

        self.assertNoRepairIsOwed(raised.exception)
        self.assertEqual(self.reader.show("secretary-468")["state"], "in_progress")
        self.assertNothingSurvived("rq-move-lost-read-back")

    def test_a_failure_after_the_claim_metadata_write_leaves_neither_the_claim_nor_a_staged_request(
        self,
    ) -> None:
        """The second point: the claim's own board work landed and then the reply was lost.

        On Kanboard that is `test_a_claim_whose_metadata_write_fails_keeps_its_pending_event` — a
        card in In progress, its claim metadata half-written, and the event held open.  The
        metadata write here is issued inside the transition's transaction, so it rolls back with
        the column effect: the card is still Ready and still unclaimed.
        """
        self._set_metadata("secretary-468", claim="")

        with (
            self._loses_the_reply_to("saveTaskMetadata"),
            self.assertRaises(TaskError) as raised,
        ):
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="codex-terra",
                request_id="rq-claim-lost-metadata-reply",
            )

        self.assertNoRepairIsOwed(raised.exception)
        card = self.reader.show("secretary-468")
        self.assertEqual(card["state"], "ready")
        self.assertIsNone(card["claim"]["worker"])
        self.assertNothingSurvived("rq-claim-lost-metadata-reply")

    def test_a_failure_after_the_ready_cleanup_leaves_neither_the_reset_nor_a_staged_request(
        self,
    ) -> None:
        """The third point: the Ready reset landed and then the reply was lost.

        On Kanboard this is `test_pending_ready_replay_finishes_cleanup_before_success_audit` and
        `test_reconcile_completes_stale_ready_cleanup_before_closing_pending`: a card in Ready
        whose reset is owed, held by a pending record.  Under one transaction the reset, the
        column effect and the claim are undone together, so the card keeps the routing the reset
        would have cleared.
        """
        self._place("secretary-468", "in_progress")
        self._set_metadata(
            "secretary-468", resolved_head="codex-terra", resolved_review_head="codex-reviewer"
        )

        with (
            self._loses_the_reply_to("saveTaskMetadata"),
            self.assertRaises(TaskError) as raised,
        ):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="ready",
                reason="",
                request_id="rq-ready-lost-reset-reply",
            )

        self.assertNoRepairIsOwed(raised.exception)
        card = self.reader.show("secretary-468")
        self.assertEqual(card["state"], "in_progress")
        self.assertEqual(card["routing"]["resolved_worker_head"], "codex-terra")
        self.assertEqual(card["routing"]["resolved_review_head"], "codex-reviewer")
        self.assertEqual(card["claim"]["worker"], "codex-terra")
        self.assertNothingSurvived("rq-ready-lost-reset-reply")

    # --- Done retention, the fourth path with a board effect -------------------------

    def _move_time(self, reference: str) -> int:
        candidates = self.reader.done_retention_candidates()
        row = next(candidate for candidate in candidates if candidate["reference"] == reference)
        self.assertIsInstance(row["date_moved"], int)
        return int(row["date_moved"])

    def test_historical_done_row_without_observed_move_time_is_skipped(self) -> None:
        """Migration does not invent a timestamp for an episode the SQL store never observed."""
        self.client._execute(
            "UPDATE tasks SET state = 'done', date_moved = NULL WHERE task_ref = %s",
            ("secretary-468",),
        )
        self.client._commit_unless_nested()
        self.assertEqual(
            self.reader.done_retention_candidates(),
            [{"reference": "secretary-468", "date_moved": None}],
        )
        result = self.writer.retire_done(
            reference="secretary-468", expected_date_moved=100, cutoff=101,
            retention_days=14, request_id="rq-retire-unknown",
        )
        self.assertTrue(result["skipped"])
        self.assertNothingSurvived("rq-retire-unknown")

    def test_a_lost_close_reply_in_done_retention_leaves_neither_the_close_nor_a_staged_request(
        self,
    ) -> None:
        """The counterpart `DoneRetentionTests.test_lost_close_reply_recovers_through_generic_reconcile` had none of.

        That case is retention's half of the §7.3 class: `closeTask` landed, its reply was lost,
        and an archived card survived beside a staged request for `reconcile` to settle.  Until
        secretary-1591 `retire_done` staged its request and issued the close outside
        `_mutation()`, so on this backend both committed at `_depth == 0` and the same
        half-applied state was reachable the moment the store could name a Done episode.  The
        whole of it — the freshness guard, the close, its proof and the record — is now one
        transaction, so the lost reply takes the close with it.
        """
        self._place("secretary-468", "done")
        moved_at = self._move_time("secretary-468")

        with (
            self._loses_the_reply_to("closeTask"),
            self.assertRaises(TaskError) as raised,
        ):
            self.writer.retire_done(
                reference="secretary-468",
                expected_date_moved=moved_at,
                cutoff=moved_at + 1,
                retention_days=14,
                request_id="rq-retire-lost-close-reply",
            )

        self.assertNoRepairIsOwed(raised.exception)
        card = self.reader.show("secretary-468")
        self.assertEqual(card["state"], "done")
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM tasks WHERE task_ref = %s AND archived", ("secretary-468",)
            ),
            [(0,)],
        )
        self.assertNothingSurvived("rq-retire-lost-close-reply")

    def test_done_retention_that_completes_closes_the_card_and_commits_its_record(self) -> None:
        """The positive control for the case above: with no failure the same path retires.

        Without it the rollback proof would be satisfied by a fixture that never reached the
        close at all, which is exactly the vacuity the parked-case block is about.
        """
        self._place("secretary-468", "done")
        moved_at = self._move_time("secretary-468")

        result = self.writer.retire_done(
            reference="secretary-468",
            expected_date_moved=moved_at,
            cutoff=moved_at + 1,
            retention_days=14,
            request_id="rq-retire-committed",
        )

        self.assertTrue(result["retired"])
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM tasks WHERE task_ref = %s AND archived", ("secretary-468",)
            ),
            [(1,)],
        )
        self.assertEqual(
            self.client._query(
                "SELECT status FROM requests WHERE request_id = %s", ("rq-retire-committed",)
            ),
            [("committed",)],
        )
        replay = self.writer.retire_done(
            reference="secretary-468",
            expected_date_moved=moved_at,
            cutoff=moved_at + 1,
            retention_days=14,
            request_id="rq-retire-committed",
        )
        self.assertTrue(replay["skipped"])
        self.assertFalse(replay["retired"])
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM requests WHERE request_id = %s",
                ("rq-retire-committed",),
            ),
            [(1,)],
        )

    def test_done_retention_fresh_guard_uses_the_real_move_episode(self) -> None:
        self._place("secretary-468", "done")
        moved_at = self._move_time("secretary-468")

        result = self.writer.retire_done(
            reference="secretary-468", expected_date_moved=moved_at, cutoff=moved_at,
            retention_days=14, request_id="rq-retire-fresh",
        )

        self.assertTrue(result["skipped"])
        self.assertFalse(result["retired"])
        self.assertEqual(
            self.client._query(
                "SELECT archived FROM tasks WHERE task_ref = %s", ("secretary-468",)
            ),
            [(False,)],
        )
        self.assertNothingSurvived("rq-retire-fresh")

    def test_a_comment_lands_with_its_request_row_committed(self) -> None:
        result = self.writer.comment(
            role="po", actor="operator", reference="secretary-468", body="hello", request_id="rq-1"
        )

        self.assertEqual(result["action"], "commented")
        self.assertFalse(result["replayed"])
        rows = self.client._query(
            "SELECT status, operation, ref FROM requests WHERE request_id = %s", ("rq-1",)
        )
        self.assertEqual(rows, [("committed", "commented", "secretary-468")])
        bodies = [row["comment"] for row in self.client.call("getAllComments", task_id=468)]
        self.assertIn("hello", "\n".join(bodies))

    def test_the_same_request_id_replays_instead_of_writing_twice(self) -> None:
        self.writer.comment(
            role="po", actor="operator", reference="secretary-468", body="once", request_id="rq-2"
        )
        again = self.writer.comment(
            role="po", actor="operator", reference="secretary-468", body="once", request_id="rq-2"
        )

        self.assertTrue(again["replayed"])
        bodies = [row["comment"] for row in self.client.call("getAllComments", task_id=468)]
        self.assertEqual(sum("once" in body for body in bodies), 1)

    def test_a_request_id_reused_for_another_operation_is_refused(self) -> None:
        from secretary.tasks import TaskError

        self.writer.comment(
            role="po", actor="operator", reference="secretary-468", body="first", request_id="rq-3"
        )
        with self.assertRaises(TaskError) as raised:
            self.writer.comment(
                role="po",
                actor="operator",
                reference="secretary-468",
                body="second",
                request_id="rq-3",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertIn("another operation or payload", str(raised.exception))

    def _create(self, *, request_id: str, title: str = "A created card") -> dict:
        """A create against an open sprint the *store* holds, not only the sprint reader.

        `tasks.sprint_ref` is a foreign key here (§3.3), so the row a Kanboard fake can invent by
        mocking `SprintReader.show` has to exist for the card to be storable at all.  Sprints on
        this backend are a later card; this is the one row that card's absence makes necessary.
        """
        now = datetime.now(UTC)
        with self.client.transaction():
            self.client._execute(
                "INSERT INTO sprints (ref, board_key, goal, definition_of_done, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, 'open', %s, %s) ON CONFLICT (ref) DO NOTHING",
                ("sprint:test", backend.record_key("sprint", "sprint:test"), "a goal", "a definition", now, now),
            )
        with (
            mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True),
            kanboard_cases.open_sprint() as sprint,
        ):
            return self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title=title,
                request_id=request_id,
                sprint=sprint,
            )

    def test_a_created_card_names_its_reference_in_its_own_request_row(self) -> None:
        """§3.9's `requests.ref`, written by the transaction that chose the reference.

        A create claims its request id before the reference exists — the reference comes from the
        board's high-water mark inside the mutation — so the claim wrote `ref = NULL` and the
        statement that finally named it only replaced `intent`.  The column stayed NULL for the
        life of every created card, which made the record's own subject index answer nothing.
        """
        result = self._create(request_id="rq-create-1")

        reference = result["task"]["ref"]
        self.assertEqual(
            self.client._query(
                "SELECT status, operation, ref FROM requests WHERE request_id = %s", ("rq-create-1",)
            ),
            [("committed", "created", reference)],
        )

    def test_a_create_that_fails_after_its_claim_leaves_no_row_of_any_kind(self) -> None:
        """The boundary the same defect sat on: one transaction from the claim to the record.

        The claim used to commit on its own, before the card was written and long before the
        record was, so a failure in between left a staged `requests` row and, on the Kanboard
        journal, a pending file to reconcile.  §7.3 says that class of half-applied write does not
        exist on this backend; it only actually did not once the whole create became one
        transaction.

        What it still asserts about the *refusal* is the create's own contract and not this
        card's: `audit_pending` here says "backend write committed; audit repair is required"
        beside two `count(*) = 0` checks that prove the opposite, and secretary-1591 repaired that
        sentence only for the two paths its observer decision named — the transition and Done
        retention (`TaskWriter._post_effect_refusal`).  The create, `_write_effect` and
        `_marker_write` still answer the old sentence, and the report of that round carries it as
        a finding rather than changing it here.
        """
        with (
            mock.patch.object(
                type(self.client),
                "_rpc_saveTaskMetadata",
                side_effect=TaskError("backend_error", "metadata refused", 1),
            ),
            self.assertRaises(TaskError) as raised,
        ):
            self._create(request_id="rq-create-2", title="A card that must not survive")

        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM requests WHERE request_id = %s", ("rq-create-2",)
            ),
            [(0,)],
        )
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM tasks WHERE title = %s", ("A card that must not survive",)
            ),
            [(0,)],
        )

    def test_a_failed_mutation_leaves_neither_effect_nor_claim(self) -> None:
        from secretary.tasks import TaskError

        with self.assertRaises(TaskError):
            self.writer.comment(
                role="nobody", actor="x", reference="secretary-468", body="no", request_id="rq-4"
            )
        self.assertEqual(
            self.client._query("SELECT count(*) FROM requests WHERE request_id = %s", ("rq-4",)),
            [(0,)],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


#: The cases of `tests/test_tasks.py` that cannot be asked of this backend, and why each one
#: cannot.  They are listed rather than quietly dropped: a case absent from a run is a hole, and a
#: hole nobody named is the failure the card's fourth criterion is about.  None of them is
#: excluded for being inconvenient, and none for how its fixture is written: since
#: secretary-1590 the writer cases arrange the board through the card client's own protocol and
#: read it back through `TaskReader`, so "the test looks inside the Kanboard fake" is no longer a
#: reason anything is here.  What is left is exactly three kinds of reason, and each entry says
#: which one it is:
#:
#: * a Kanboard *transport* fact with no equivalent here (a malformed row on the wire, two
#:   threads on one connection, a batch that is a round trip, the order two writes were issued
#:   in);
#: * a board state the store's constraints make unrepresentable (§9's primary key and task
#:   number, §3.5's CHECKs);
#: * a half-applied write of the kind §7.3 removes, where the whole mutation is one transaction
#:   (§7.1) and the failure leaves nothing to recover.  `SqlTaskWriterTests` proves that
#:   property directly, at the create's boundary and at the transition's three post-effect
#:   points.  Since secretary-1591 this covers the transition too: the eleven cases that were
#:   parked here as a defect of this backend — a state edge whose effect could outlive its
#:   record — are §7.3 omissions like the rest, and the block that names them says so.
KANBOARD_ONLY = {
    "test_duplicate_reference_retry_reallocates_a_stolen_pending_target": (
        "two cards under one reference again; `tasks.task_ref` is the primary key (§9)"
    ),
    "test_reconcile_audit_reallocates_a_stolen_pending_target": (
        "the same duplicate-reference fixture, plus the `reconcile` that §7.3 removes"
    ),
    "test_restoring_a_card_preserves_ordinary_long_text_byte_for_byte": (
        "it creates `secretary-restore-long`, a reference that does not end in -<number>.  "
        "`tasks.task_number` is NOT NULL and UNIQUE (project_id, task_number) has no value for "
        "such a reference, which is the refusal `board/import_board.py` already records for the "
        "same shape.  A finding about the schema, not a licence to change 0003"
    ),
    "test_auto_reference_serializes_concurrent_creates": (
        "it creates from two threads at once.  `SqlCardClient` holds one connection, and one "
        "libpq connection cannot carry two transactions, so the concurrency this asserts needs a "
        "pool (§5.6) that this card does not ship.  A finding for the cutover card"
    ),
    "test_pending_blocks_export_from_the_same_data_root": (
        "the export's gate reads `pending-audit/` on disk.  §6.3 says it becomes a query over "
        "`requests WHERE status = 'staged'`; `export_board` is a group-(c) consumer and is "
        "explicitly out of this card's scope, so the gate is still blind to a staged row here"
    ),
    "test_steward_signal_cards_reject_invalid_backend_shapes": (
        "injects a metadata value that is not a map and a task list that is not a list.  Both are "
        "malformed JSON-RPC replies; `tasks.state`'s CHECK and the column types make neither "
        "storable, so the refusal has nothing to refuse"
    ),
    "test_unknown_column_is_backend_error": (
        "sets a column id the board does not have.  §3.5's CHECK on `tasks.state` admits exactly "
        "the seven states, so an eighth cannot be written to be read back"
    ),
    "test_show_prefers_live_duplicate_reference": (
        "two cards sharing one reference.  `tasks.task_ref` is the primary key (§9), so the "
        "duplicate this case resolves cannot exist in the store"
    ),
    "test_show_returns_archived_reference_when_no_live_duplicate_exists": (
        "the other half of the same duplicate-reference fixture, and it asserts the Kanboard row "
        "id.  Reading an archived card is covered here by "
        "SqlTaskReaderTests.test_restore_snapshot_returns_every_card_by_reference"
    ),
    # --- §7.3, the transition's eleven: a half-applied state edge, which this backend no longer
    # has.  Until secretary-1591 `TaskWriter._transition_card` called `board_host.transition`
    # outside `_mutation()`, so the staged `requests` row and the card RPC committed separately
    # and a post-effect failure really did leave a moved card beside a staged request.  These
    # eleven were parked here as *"the invariant is not met yet"*: they would have passed
    # precisely because §7.1's promise was broken.  The transition now runs inside
    # `_mutation()`, the rollback takes the column effect, the caller's `finish` work and the
    # claim together, and the state every one of them asserts — an applied board write beside a
    # record that is not committed — does not exist here.  So they are the same kind of omission
    # as the §7.3 block at the end of this table, not a debt: the class is impossible, and the
    # eleven are listed one by one rather than by their class so that nothing hides in the
    # summary.  What replaces them is not a recount of them: `SqlTaskWriterTests` proves the
    # invariant positively at the three post-effect points they covered — after the column move,
    # after the claim's metadata write, and after the Ready cleanup — by showing that neither the
    # board effect nor a staged request survives the failure.  Each of the eleven keeps running,
    # unchanged, on Kanboard, where the state and its `recover_*` path are exactly as before.
    "test_typed_pending_transition_recovers_only_after_proving_the_live_target": (
        "asserts an In progress card beside a pending protocol record after the journal append "
        "failed, which is the §7.3 state the transition's transaction removes; see the header "
        "above"
    ),
    "test_reconcile_publishes_a_typed_pending_transition_whose_board_work_is_done": (
        "recovers from that same half-applied move (`_pending_typed_move`)"
    ),
    "test_recovery_refuses_a_typed_pending_transition_the_board_contradicts": (
        "the same half-applied move, refused by recovery"
    ),
    "test_recovery_refuses_a_typed_pending_transition_whose_card_vanished": (
        "the same half-applied move, with the card gone"
    ),
    "test_a_transport_failure_after_the_move_keeps_the_typed_pending_record": (
        "asserts a moved Validate card beside one pending record after the read back was lost"
    ),
    "test_a_state_race_after_the_move_keeps_the_typed_pending_record": (
        "its twin: the move landed, another writer moved the card on, and the record stays "
        "pending beside it"
    ),
    "test_a_claim_whose_metadata_write_fails_keeps_its_pending_event": (
        "asserts the column moved and the claim metadata did not, with the event held open"
    ),
    "test_reconcile_publishes_a_proven_start_and_leaves_the_claim_to_the_dispatcher": (
        "recovery over that same half-applied claim"
    ),
    "test_pending_ready_replay_finishes_cleanup_before_success_audit": (
        "asserts a moved card and a pending record after the Ready reset's metadata write failed"
    ),
    "test_reconcile_completes_stale_ready_cleanup_before_closing_pending": (
        "the same half-applied Ready reset, from the reconcile side"
    ),
    "test_partial_move_failure_keeps_pending_until_reconcile": (
        "asserts a moved Validate card and a pending record after the follow-up comment failed; "
        "the comment is issued inside the transition's transaction, so its failure takes the move"
    ),
    # --- Kanboard's wire behaviour, not the product's request.  These three assert what a
    # *batch* is: one JSON-RPC round trip carrying several reads, in the order they were asked
    # for.  `SqlCardClient.call_batch` is `[self.call(...) for ...]` — one batch because there is
    # no round trip — so the same assertion passes here while proving nothing about the economy
    # it exists to protect.  Vacuous is not the same as true, so they stay Kanboard-only.
    "test_export_includes_archived_cards_in_one_metadata_comments_batch": (
        "asserts the JSON-RPC batch the export posts and its order.  The store answers the same "
        "reads in one connection and posts no batch, so there is no equivalent fact"
    ),
    "test_steward_signal_cards_are_bounded_normalized_and_filtered": (
        "same: its bound is the number of JSON-RPC batches.  The projection it also asserts is "
        "covered on this backend by SqlTaskReaderTests.test_steward_signal_cards_report_the_"
        "bounded_view"
    ),
    "test_archive_retry_after_failed_comment_recreates_reason_before_close": (
        "its subject is the order of two board writes — the reason comment is recreated before "
        "the close — which is a claim about the sequence of calls rather than about any state a "
        "reader can see.  The archive's own effects are covered on both backends by "
        "test_archive_closes_card_and_writes_audit"
    ),
    "test_steward_report_read_is_bounded_and_exposes_no_backend_row": (
        "same bound, and the case builds its own Kanboard board rather than taking "
        "`board_client`'s: the read under test would never reach this backend's client, so "
        "counting it as a PostgreSQL execution would be counting a Kanboard run twice"
    ),
    # --- §9: one reference, one card.  `tasks.task_ref` is the primary key, so every fixture
    # that puts two cards under one reference builds a board this store cannot hold.
    "test_duplicate_reference_preview_and_apply_use_exact_producer_evidence": (
        "the duplicate-reference fixture: four rows under two references (§9)"
    ),
    "test_duplicate_reference_repair_refuses_missing_evidence_and_dependent_state": (
        "the same fixture"
    ),
    "test_duplicate_reference_repair_refuses_missing_metadata": ("the same fixture"),
    "test_duplicate_reference_preview_refuses_unrecognized_active_column": (
        "the same fixture, plus a column id no board has; §3.5's CHECK on `tasks.state` admits "
        "exactly the seven states"
    ),
    "test_duplicate_reference_repair_resumes_after_reference_write": ("the same fixture"),
    "test_restore_placement_uses_live_duplicate_reference": (
        "an archived and a live card under `secretary-468`, which the primary key forbids (§9)"
    ),
    # --- §9 again, from the other side: a reference the store cannot number, or none at all.
    "test_pending_create_repairs_legacy_orphaned_reference_by_recorded_id": (
        "it creates `secretary-restore` and then blanks that row's reference.  "
        "`tasks.task_number` is NOT NULL and has no value for a reference that does not end in "
        "-<number>, and `task_ref` is the primary key, so neither state is storable"
    ),
    "test_pending_create_does_not_repair_a_different_task_with_its_reference": (
        "the same two shapes: `secretary-interrupted`, and a row whose reference is cleared"
    ),
    "test_auto_reference_uses_board_wide_project_high_water_mark": (
        "it seeds `secretary-nope` so the allocator has a malformed reference to skip.  "
        "`tasks.task_number` cannot hold one, which is the refusal `board/import_board.py` "
        "already records for the same shape"
    ),
    "test_backend_ignoring_atomic_reference_leaves_pending_create_unrepaired": (
        "it drops the reference from `createTask`.  `SqlCardClient` refuses that write outright "
        "(§9: the store identifies a card by its reference), so the card it repairs never exists"
    ),
    # --- §3.5: a value the schema's CHECK does not admit.
    "test_a_card_already_carrying_exec_reads_as_carrying_no_mode": (
        "it stamps the retired `codex_launch_mode = exec` on a card to prove the reader ignores "
        "it.  `tasks_codex_launch_mode_check` refuses that value, so the legacy row this reads "
        "cannot be written here.  A finding about the importer's treatment of legacy `exec` "
        "rows, not a licence to widen the CHECK"
    ),
    # --- §7.3: the effect and the record are one transaction (§7.1), so the state each of these
    # recovers from — a board write that landed while its record did not — does not exist here.
    # The refusal itself is proved on this backend by `SqlTaskWriterTests`.
    "test_pending_is_visible_and_reconciles_without_backend_retry": (
        "the comment lands and the journal append fails; here both roll back, so there is no "
        "pending record to see or reconcile (§7.3)"
    ),
    "test_archive_retry_after_lost_close_reply_does_not_close_twice": (
        "the close lands and its reply is lost; the transaction rolls the close back with it"
    ),
    "test_archive_reconcile_without_missing_reason_does_not_close": (
        "the same half-applied archive, from the reconcile side"
    ),
    "test_restore_move_failure_keeps_pending_audit": (
        "a refused move leaves nothing staged here, because the claim rolls back with it"
    ),
    "test_reconcile_finishes_pending_restore_before_auditing_success": (
        "the same, from the reconcile side: there is no owed board work to finish"
    ),
    "test_restore_comment_retry_after_lost_reply_does_not_duplicate_history": (
        "the comment lands and its reply is lost; the comment rolls back, so the retry is a "
        "first write rather than a replay"
    ),
    "test_pending_atomic_create_without_recorded_id_stays_unresolved": (
        "the card is created and the staging that records its id fails; here the card is not "
        "created either, so there is no unresolved half to leave"
    ),
    "test_pending_create_replay_restores_metadata_before_audit": (
        "the same create, failing at the metadata write instead"
    ),
    "test_steward_report_pending_metadata_recovers_without_duplicate_create": (
        "the same, for a steward report: nothing survives the failure to recover from, so the "
        "retry creates rather than replays"
    ),
    "test_sigint_before_atomic_create_does_not_adopt_same_identity_later_reference": (
        "a SIGINT inside the create.  The interrupt rolls the transaction back, leaving no "
        "staged record for a later card to be wrongly adopted into"
    ),
}


class KanboardFixtureCase(SqlBoardCase):
    """A parity class: the same cases, the other backend, and every omission named."""

    def setUp(self) -> None:
        reason = KANBOARD_ONLY.get(self._testMethodName)
        if reason is not None:
            self.skipTest(f"Kanboard-only: {reason}")
        super().setUp()


class SqlTaskReaderParityTests(KanboardFixtureCase, kanboard_cases.TaskReaderTests):
    """`tests/test_tasks.py`'s reader cases, unchanged, over the PostgreSQL backend.

    Nothing is overridden but the board the cases run against and the one case that asserts the
    card identity, which §9 spells differently here.
    """

    def board_client(self):
        return self.client_for(FakeKanboard())

    def test_list_names_the_card_identity_of_its_backend(self) -> None:
        task = self.reader.list(states={"ready"}, project="secretary")[0]
        self.assertEqual(task["id"], "task_postgres_468")
        self.assertEqual(task["audit"]["backend"]["kind"], "postgres")


class SqlTaskWriterParityTests(KanboardFixtureCase, kanboard_cases.TaskWriterTests):
    """The same, for the writer's cases."""

    def board_client(self):
        return self.client_for(WriteKanboard())

    @contextlib.contextmanager
    def open_sprint(self, ref: str = "sprint:test", project: str = "secretary"):
        """The same open sprint, plus the row `tasks.sprint_ref` refers to (§3.3).

        Sprints on this backend are a later card; this is the one row that card's absence makes
        necessary, and it is stated here so no create case has to know which backend it runs on.
        """
        now = datetime.now(UTC)
        with self.client.transaction():
            self.client._execute(
                "INSERT INTO sprints (ref, board_key, goal, definition_of_done, status, created_at, "
                "updated_at) VALUES (%s, %s, %s, %s, 'open', %s, %s) ON CONFLICT (ref) DO NOTHING",
                (ref, backend.record_key("sprint", ref), "a goal", "a definition", now, now),
            )
        with (
            mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True),
            kanboard_cases.open_sprint(ref, project) as sprint,
        ):
            yield sprint

    def remove_card(self, reference: str) -> None:
        """The one fixture verb the card protocol does not carry (see `BoardFixture`)."""
        with self.client.transaction():
            self.client._execute("DELETE FROM tasks WHERE task_ref = %s", (reference,))

    def tearDown(self) -> None:
        pass
