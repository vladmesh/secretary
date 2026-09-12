"""Every live audit reader reads the store its card client names, against a real `postgres:16`.

`secretary-1614` was one reader built from a data directory instead of from its client: the
dispatcher waited for a `report:done` that was committed in `requests` while it watched
`board/events.ndjson`, a file the PostgreSQL writer never touches. `task_audit_for` closed that for
the dispatcher. The readers here are the rest of the list — the checkpoint gate, `task verify-audit`,
the command reads, the product-run publication, the typed canon and the sprint status journal — and
the property is one property, asserted the same way each time: **with the file projection absent or
stale, the reader still answers from SQL, and it never publishes the file's emptiness.**

Each case says what the file journal holds while SQL answers, because that is the whole of the
regression: a case that ran with no file at all would pass just as well against a reader that had
silently read one. So the cases put a *stale* projection there — records of a board that is not this
one — and then assert the answer contains none of it.

Nothing here reaches the live installation. The store is a throwaway container database
(`tests/sql_backend_fixtures.py`), the instance and data directories are temporary, and the backend
selector is the per-case argument of a client rather than an environment name: the suite's own
default is `kanboard` (`tests/__init__.py`), and these cases opt in by handing a PostgreSQL client
to the construction seam every one of these readers already has.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from secretary.board.events import BoardEventCanon, BoardEventCanonUnowned
from secretary.board.kanboard import KanboardBoardHost
from secretary.board.models import Actor, EntityKind, Event, EventKind
from secretary.board.sql_audit import SqlTaskAudit
from secretary.checkpoint import CheckpointWriter
from secretary.sprints import SprintReader
from secretary.task_commands import run_task_verify_audit
from secretary.tasks import TaskAudit, TaskError, task_audit_for
from secretary.webproto.command_reads import (
    STATE_COMMITTED,
    STATE_NOT_FOUND,
    STATE_PENDING,
    CommandReadLayer,
)
from secretary.webproto.cursor import POSITION_ORDINAL, Cursor
from secretary.webproto.errors import InvalidCursor
from secretary.webproto.journal import CommittedAudit
from secretary.webproto.ops import OperationLayer
from secretary.webproto.reads import ReadLayer
from secretary.webproto.run_events import STARTED, publish_started, request_id_for
from secretary.webproto.runs import ProductRun
from tests.fakes.tasks import FakeKanboard
from tests.sql_backend_fixtures import PostgresBoard, seed_client

#: A committed record of a board that is not this one. Whatever a reader answers, none of this may
#: be in it: it is the stale projection a migrated installation still has lying under `<data>`.
STALE_JOURNAL_REF = "stale-999"


class SqlAuditCase(unittest.TestCase):
    """One container per module, one migrated store, one instance and one data plane per case."""

    board: PostgresBoard

    @classmethod
    def setUpClass(cls) -> None:
        for module in ("psycopg", "sqlalchemy", "alembic"):
            __import__(module)
        cls.board = PostgresBoard()
        cls.addClassCleanup(cls.board.stop)

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data_dir = self.tmp / "data"
        (self.data_dir / "board").mkdir(parents=True)
        (self.data_dir / "dispatcher").mkdir(parents=True)
        self.instance_dir = self._instance()
        self.client = seed_client(self.board.fresh_database(), FakeKanboard(), self.instance_dir)
        self.addCleanup(self.client.close)
        self.audit = task_audit_for(self.client, self.data_dir)
        self.assertIsInstance(self.audit, SqlTaskAudit)

    # -- the installation ----------------------------------------------------------------------

    def _instance(self) -> Path:
        instance_dir = self.tmp / "instance"
        (instance_dir / "projects").mkdir(parents=True)
        (instance_dir / "instance.yaml").write_text(
            "version: 1\nname: audit-canon-test\n"
            f"data_dir: {self.data_dir}\n"
            "offsite:\n  instance_remote: https://example.invalid/instance.git\n",
            encoding="utf-8",
        )
        repo = self.tmp / "repos" / "secretary"
        repo.mkdir(parents=True)
        (instance_dir / "projects" / "secretary.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "secretary",
                    "repo": str(repo),
                    "enabled": True,
                    "adapter": "secretary",
                    "default_branch": "main",
                }
            ),
            encoding="utf-8",
        )
        return instance_dir

    # -- what the two stores hold --------------------------------------------------------------

    def journal(self) -> Path:
        return self.data_dir / "board" / "events.ndjson"

    def stale_projection(self) -> None:
        """A file journal left behind by the Kanboard era, holding a board that is not this one."""
        record = {
            "schema_version": 1,
            "event_id": "evt_stale",
            "kind": "commented",
            "ref": STALE_JOURNAL_REF,
            "occurred_at": "2026-01-01T00:00:00Z",
            "actor": {"role": "po", "id": "somebody-else"},
            "outcome": "success",
            "request_id": "stale-request",
            "payload": {"marker": "dispatcher"},
        }
        self.journal().write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    def no_projection(self) -> None:
        """The other half of the same fact: a data plane that never had a file journal at all."""
        self.journal().unlink(missing_ok=True)

    def event(
        self,
        *,
        event_id: str,
        ref: str = "secretary-468",
        kind: EventKind = EventKind.CARD_STARTED,
        minute: int = 0,
    ) -> Event:
        return Event(
            event_id=event_id,
            kind=kind,
            entity_kind=EntityKind.CARD,
            ref=ref,
            actor=Actor("dispatcher", "secretary-production"),
            reason="claimed for the worker",
            occurred_at=datetime(2026, 9, 7, 12, minute, tzinfo=UTC),
            source_state="ready",
            target_state="in_progress",
        )

    def commit(self, request_id: str, **kwargs: Any) -> Event:
        """One committed typed protocol event, in SQL, written the way its writer writes one."""
        event = self.event(**kwargs)
        BoardEventCanon(self.data_dir, audit=self.audit).commit(request_id, event)
        return event

    def stage(self, request_id: str, **kwargs: Any) -> Event:
        """One staged event whose backend effect never confirmed: a request that is part-done."""
        event = self.event(**kwargs)
        BoardEventCanon(self.data_dir, audit=self.audit).stage(request_id, event)
        return event


class CommandReadTests(SqlAuditCase):
    """AC3: `command_history` and `command_request` answer from `requests`, not from a file."""

    def layer(self, **kwargs: Any) -> CommandReadLayer:
        options: dict[str, Any] = {
            "data_dir": self.data_dir,
            "board_client": self.client,
            "clock": lambda: 1788652800.0,
        }
        options.update(kwargs)
        return CommandReadLayer(self.instance_dir, **options)

    def test_a_committed_history_comes_from_sql_while_the_projection_is_stale(self) -> None:
        self.commit("req-1", event_id="evt_1", minute=1)
        self.commit("req-2", event_id="evt_2", minute=2, ref="secretary-12")
        self.stale_projection()

        document = self.layer().command_history()

        self.assertEqual(document["sources"]["audit"]["source"]["state"], "available")
        items = document["commands"]["items"]
        self.assertEqual([row["event_id"] for row in items], ["evt_2", "evt_1"])
        self.assertNotIn(STALE_JOURNAL_REF, [row["entity"]["ref"] for row in items])

    def test_a_newly_committed_report_is_in_the_history_the_file_never_gets(self) -> None:
        self.no_projection()
        self.commit("req-report", event_id="evt_report", kind=EventKind.CARD_STARTED)

        items = self.layer().command_history()["commands"]["items"]

        self.assertEqual([row["event_id"] for row in items], ["evt_report"])
        self.assertEqual(items[0]["result"]["reason"], "claimed for the worker")
        self.assertFalse(self.journal().exists())
        self.assertEqual(TaskAudit(self.data_dir).events(), [])

    def test_a_staged_request_is_pending_and_not_a_history_row(self) -> None:
        self.stale_projection()
        self.stage("req-staged", event_id="evt_staged")

        document = self.layer().command_request("req-staged")

        self.assertEqual(document["operation"]["state"], STATE_PENDING)
        self.assertEqual(document["operation"]["event_id"], "evt_staged")
        self.assertEqual(self.layer().command_history()["commands"]["items"], [])

    def test_committed_wins_over_staged_and_the_owed_repair_is_still_said(self) -> None:
        """The one state where both lookups answer for one id, in SQL as in the file journal."""
        self.no_projection()
        event = self.event(event_id="evt_both")
        canon = BoardEventCanon(self.data_dir, audit=self.audit)
        canon.stage("req-both", event)
        self.audit._claim_row("req-both", event.to_record("req-both"), status="committed")
        self.audit._commit()

        document = self.layer().command_request("req-both")

        self.assertEqual(document["operation"]["state"], STATE_COMMITTED)
        self.assertEqual(document["operation"]["event_id"], "evt_both")

    def test_a_request_this_store_never_saw_is_not_found_and_a_store_that_refuses_is_unknown(
        self,
    ) -> None:
        self.stale_projection()
        self.commit("req-1", event_id="evt_1")

        self.assertEqual(
            self.layer().command_request("stale-request")["operation"]["state"], STATE_NOT_FOUND
        )
        broken = self.layer(board_client=_RefusingClient())
        document = broken.command_request("req-1")
        self.assertEqual(document["sources"]["audit"]["source"]["state"], "unavailable")
        self.assertNotEqual(document["operation"]["state"], STATE_NOT_FOUND)
        history = broken.command_history()
        self.assertEqual(history["sources"]["audit"]["source"]["state"], "unavailable")
        self.assertIsNone(history["commands"]["items"])

    def test_the_refusal_names_the_backend_context_and_carries_no_credential(self) -> None:
        document = self.layer(board_client=_RefusingClient()).command_history()
        reason = document["sources"]["audit"]["source"]["reason"]
        self.assertIn("the committed board audit could not be read", reason)
        self.assertIn(STORE_REFUSAL, reason)
        self.assertNotIn("password", reason.lower())


#: What a store that will not answer says, in the vocabulary `SqlCardClient` translates psycopg
#: into (`board/sql_cards.py:_driver_error`): the reason without the connection string.
STORE_REFUSAL = "the board store could not answer a read: connection refused"


class _RefusingClient:
    """A PostgreSQL client whose store will not answer, to prove `unknown` is not `not_found`."""

    backend_kind = "postgres"
    _depth = 0

    def _refuse(self) -> Any:
        from secretary.tasks import TaskError

        raise TaskError("backend_unavailable", STORE_REFUSAL, 1)

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return self._refuse()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return self._refuse()


class CheckpointGateTests(SqlAuditCase):
    """AC1: the checkpoint gate is the configured backend's audit, and it fails closed."""

    def writer(self, client: Any = None) -> CheckpointWriter:
        return CheckpointWriter(
            self.data_dir, self.instance_dir, client=client if client is not None else self.client
        )

    def test_a_staged_request_in_sql_blocks_publication_with_its_pending_count(self) -> None:
        self.stage("req-staged", event_id="evt_staged")
        self.no_projection()

        result = self.writer().write()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(
            result.reason, "the postgres task audit has 1 unresolved pending record(s)"
        )

    def test_a_missing_or_stale_file_journal_cannot_make_the_gate_green_or_red(self) -> None:
        """The file journal is not consulted: a pending file neither passes nor blocks this gate.

        A leftover `pending-audit/` record under a migrated data plane would block every checkpoint
        forever if the gate still read it, and an empty one would pass a gate that had read nothing.
        So the assertion is about *which* refusal comes back: the audit has answered (there is
        nothing staged in SQL), and what stops this writer is the next step, the board export.
        """
        legacy = self.data_dir / "board" / "pending-audit"
        legacy.mkdir(parents=True)
        (legacy / "v2-0123.json").write_text("{}", encoding="utf-8")
        self.stale_projection()

        result = self.writer().write()

        self.assertEqual(result.status, "blocked")
        self.assertNotIn("unresolved pending record", result.reason)
        self.assertNotIn("Product/Issue", result.reason)

    def test_a_card_backend_that_cannot_be_established_blocks_by_name(self) -> None:
        result = self.writer(client=_RefusingClient()).write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("the postgres task audit could not be read", result.reason)


class VerifyAuditCommandTests(SqlAuditCase):
    """AC2: `secretary task verify-audit` verifies the configured backend's audit."""

    def _run(self) -> tuple[int, dict[str, Any]]:
        printed: list[Any] = []
        arguments = Namespace(instance=str(self.instance_dir), data_dir=str(self.data_dir))
        with (
            mock.patch("secretary.task_commands.card_client", return_value=self.client),
            mock.patch(
                "secretary.task_commands.print_json",
                side_effect=lambda document, **_options: printed.append(document),
            ),
        ):
            code = run_task_verify_audit(arguments)
        return code, printed[-1]

    def test_a_clean_sql_audit_verifies_even_with_a_stale_file_journal(self) -> None:
        self.commit("req-1", event_id="evt_1")
        self.stale_projection()
        (self.data_dir / "board" / "pending-audit").mkdir(parents=True)
        (self.data_dir / "board" / "pending-audit" / "v2-0123.json").write_text(
            "{}", encoding="utf-8"
        )

        code, document = self._run()

        self.assertEqual(code, 0)
        self.assertEqual(document, {"ok": True, "pending": 0, "backend": "postgres"})

    def test_a_staged_request_in_sql_fails_the_verification_with_the_released_exit_status(
        self,
    ) -> None:
        self.stage("req-staged", event_id="evt_staged")
        self.no_projection()

        code, document = self._run()

        self.assertEqual(code, 1)
        self.assertEqual(document, {"ok": False, "pending": 1, "backend": "postgres"})


class ProductRunPublicationTests(SqlAuditCase):
    """AC4: a product run's events are published into the store the client names."""

    def _run_record(self) -> ProductRun:
        return ProductRun(
            run_id="run_1",
            request_id="req-run-1",
            ref="secretary-468",
            project="secretary",
            role="worker",
            profile="claude-worker",
            adapter="claude",
            runtime="local-pty",
            workspace=str(self.tmp / "workspace"),
            run_dir=str(self.tmp / "run"),
            started_at=1788652800.0,
        )

    def test_the_layer_publishes_through_the_audit_its_client_names(self) -> None:
        layer = OperationLayer(
            self.instance_dir, data_dir=self.data_dir, board_client=self.client
        )
        self.assertIsInstance(layer._audit(self.data_dir), SqlTaskAudit)

    def test_a_published_run_event_is_in_sql_and_not_in_the_file_journal(self) -> None:
        self.no_projection()
        run = self._run_record()

        published = publish_started(
            OperationLayer(
                self.instance_dir, data_dir=self.data_dir, board_client=self.client
            )._audit(self.data_dir),
            run,
        )

        request_id = request_id_for(run.run_id, STARTED)
        self.assertEqual(published["request_id"], request_id)
        committed = self.audit.committed_event(request_id)
        self.assertIsNotNone(committed)
        self.assertEqual(committed["kind"], STARTED)
        self.assertFalse(self.journal().exists())

    def test_republishing_the_same_run_writes_no_second_record(self) -> None:
        run = self._run_record()
        audit = OperationLayer(
            self.instance_dir, data_dir=self.data_dir, board_client=self.client
        )._audit(self.data_dir)
        publish_started(audit, run)
        publish_started(audit, run)

        self.assertEqual(
            [record["kind"] for record in self.audit.events("secretary-468", kind=STARTED)],
            [STARTED],
        )


class CardHistoryReadTests(SqlAuditCase):
    """AC4/AC9: `task_snapshot` and `task_events` page the store the card client names.

    The defect this class exists for: product-run publication had moved to `requests` while both of
    these read `EventJournal(<data>/board/events.ndjson)`, so a migrated installation answered a
    card's history from a projection nobody writes — unavailable where it had been swept, a
    successful empty or stale page where an old one was left behind. Every case below therefore says
    what the file holds while SQL answers: absent, empty, or holding a board that is not this one.
    """

    REF = "secretary-468"

    def layer(self, **kwargs: Any) -> ReadLayer:
        options: dict[str, Any] = {
            "data_dir": self.data_dir,
            "board_client": self.client,
            "clock": lambda: 1788652800.0,
            "offline": True,
        }
        options.update(kwargs)
        return ReadLayer(self.instance_dir, **options)

    def empty_projection(self) -> None:
        """The third shape of the same fact: a file that is there and holds nothing."""
        self.journal().write_text("", encoding="utf-8")

    def _run_record(self) -> ProductRun:
        return ProductRun(
            run_id="run_1",
            request_id="req-run-1",
            ref=self.REF,
            project="secretary",
            role="worker",
            profile="claude-worker",
            adapter="claude",
            runtime="local-pty",
            workspace=str(self.tmp / "workspace"),
            run_dir=str(self.tmp / "run"),
            started_at=1788652800.0,
        )

    def test_the_layer_reads_the_card_history_of_the_backend_its_client_names(self) -> None:
        reader, semantics = self.layer()._events(self.data_dir)

        self.assertIsInstance(reader, CommittedAudit)
        self.assertIsInstance(reader.audit, SqlTaskAudit)
        self.assertEqual(semantics, POSITION_ORDINAL)

    def test_the_snapshot_tail_holds_the_sql_events_with_no_projection_at_all(self) -> None:
        self.commit("req-1", event_id="evt_1", minute=1)
        publish_started(self.audit, self._run_record())
        self.no_projection()

        events = self.layer().task_snapshot(self.REF)["events"]

        self.assertEqual(events["source"]["state"], "available")
        self.assertEqual([row["kind"] for row in events["items"]], ["card.started", STARTED])
        self.assertEqual(events["items"][0]["event_id"], "evt_1")
        self.assertFalse(self.journal().exists())

    def test_an_empty_projection_is_not_an_empty_history(self) -> None:
        self.commit("req-1", event_id="evt_1", minute=1)
        publish_started(self.audit, self._run_record())
        self.empty_projection()

        page = self.layer().task_events(self.REF, None, limit=10)

        self.assertEqual(page["source"]["state"], "available")
        self.assertEqual([row["kind"] for row in page["items"]], ["card.started", STARTED])
        self.assertEqual(TaskAudit(self.data_dir).events(self.REF), [])

    def test_a_stale_projection_is_in_no_page_and_in_no_snapshot(self) -> None:
        self.commit("req-1", event_id="evt_1", minute=1)
        self.stale_projection()

        page = self.layer().task_events(self.REF, None, limit=10)
        snapshot = self.layer().task_snapshot(self.REF)["events"]

        self.assertEqual([row["event_id"] for row in page["items"]], ["evt_1"])
        self.assertNotIn(STALE_JOURNAL_REF, [row["ref"] for row in page["items"]])
        self.assertEqual([row["event_id"] for row in snapshot["items"]], ["evt_1"])
        self.assertEqual(
            self.layer().task_events(STALE_JOURNAL_REF, None, limit=10)["items"], []
        )

    def test_a_page_continues_once_and_repeats_the_same_page(self) -> None:
        self.no_projection()
        for index in range(5):
            self.commit(f"req-{index}", event_id=f"evt_{index}", minute=index)

        first = self.layer().task_events(self.REF, None, limit=2)
        again = self.layer().task_events(self.REF, None, limit=2)
        second = self.layer().task_events(self.REF, first["next_cursor"], limit=2)
        repeated = self.layer().task_events(self.REF, first["next_cursor"], limit=2)

        self.assertEqual([row["event_id"] for row in first["items"]], ["evt_0", "evt_1"])
        self.assertEqual(again["items"], first["items"])
        self.assertTrue(first["has_more"])
        self.assertEqual([row["event_id"] for row in second["items"]], ["evt_2", "evt_3"])
        self.assertEqual(repeated["items"], second["items"])
        last = self.layer().task_events(self.REF, second["next_cursor"], limit=2)
        self.assertEqual([row["event_id"] for row in last["items"]], ["evt_4"])
        self.assertFalse(last["has_more"])
        drained = self.layer().task_events(self.REF, last["next_cursor"], limit=2)
        self.assertEqual(drained["items"], [])

    def test_a_cursor_issued_before_the_backend_moved_is_refused_and_the_snapshot_replaces_it(
        self,
    ) -> None:
        """A file byte offset is never read as an ordinal, and the refusal has a way out."""
        self.no_projection()
        self.commit("req-1", event_id="evt_1", minute=1)
        released = Cursor(ref=self.REF, offset=137).encode()

        with self.assertRaises(InvalidCursor) as refused:
            self.layer().task_events(self.REF, released, limit=10)

        self.assertIn("'offset'", str(refused.exception))
        self.assertIn("fresh task snapshot", str(refused.exception))
        continuation = self.layer().task_snapshot(self.REF)["events"]["next_cursor"]
        self.assertEqual(self.layer().task_events(self.REF, continuation, limit=10)["items"], [])
        self.commit("req-2", event_id="evt_2", minute=2)
        resumed = self.layer().task_events(self.REF, continuation, limit=10)
        self.assertEqual([row["event_id"] for row in resumed["items"]], ["evt_2"])

    def test_a_cursor_of_another_card_and_one_past_the_end_are_both_refused(self) -> None:
        self.no_projection()
        self.commit("req-1", event_id="evt_1", minute=1)
        mine = self.layer().task_events(self.REF, None, limit=1)["next_cursor"]

        with self.assertRaises(InvalidCursor):
            self.layer().task_events("secretary-12", mine, limit=1)
        beyond = Cursor(ref=self.REF, offset=99, position=POSITION_ORDINAL).encode()
        with self.assertRaises(InvalidCursor):
            self.layer().task_events(self.REF, beyond, limit=1)

    def test_a_store_that_will_not_answer_is_unavailable_and_never_an_empty_history(self) -> None:
        self.stale_projection()
        layer = self.layer(board_client=_RefusingClient())

        page = layer.task_events(self.REF, None, limit=10)

        self.assertEqual(page["source"]["state"], "unavailable")
        self.assertIn(STORE_REFUSAL, page["source"]["reason"])
        self.assertNotIn("password", page["source"]["reason"].lower())
        self.assertEqual(page["items"], [])

    def test_a_card_backend_that_cannot_be_established_takes_the_events_and_nothing_else(
        self,
    ) -> None:
        self.stale_projection()
        self.commit("req-1", event_id="evt_1", minute=1)
        layer = ReadLayer(self.instance_dir, data_dir=self.data_dir, clock=lambda: 1788652800.0)

        with mock.patch(
            "secretary.webproto.reads.board_client",
            side_effect=TaskError("backend_unavailable", STORE_REFUSAL, 1),
        ):
            page = layer.task_events(self.REF, None, limit=10)

        self.assertEqual(page["source"]["state"], "unavailable")
        self.assertIn("could not be established", page["source"]["reason"])
        self.assertEqual(page["items"], [])

    def test_a_kanboard_client_still_pages_the_file_journal_by_byte_offset(self) -> None:
        """The released behavior, unchanged, beside the same data plane: file in, offsets out."""
        record = {
            "schema_version": 1,
            "event_id": "evt_file",
            "kind": "commented",
            "ref": self.REF,
            "occurred_at": "2026-09-07T12:00:00Z",
            "actor": {"role": "po", "id": "owner"},
            "outcome": "success",
            "request_id": "file-request",
            "payload": {},
        }
        self.journal().write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        kanboard = FakeKanboard()
        layer = self.layer(board_client=kanboard)

        reader, semantics = layer._events(self.data_dir)
        page = layer.task_events(self.REF, None, limit=10)

        self.assertEqual(semantics, "offset")
        self.assertEqual(reader.path, self.journal())
        self.assertEqual([row["event_id"] for row in page["items"]], ["evt_file"])
        self.assertEqual(
            page["next_cursor"], Cursor(ref=self.REF, offset=self.journal().stat().st_size).encode()
        )
        with self.assertRaises(InvalidCursor):
            layer.task_events(
                self.REF, Cursor(ref=self.REF, offset=1, position=POSITION_ORDINAL).encode()
            )


class TypedCanonAndSprintReadTests(SqlAuditCase):
    """AC5: the typed canon and the sprint reads cannot read a file beside a PostgreSQL client."""

    def test_a_host_over_a_postgres_client_owns_the_sql_canon(self) -> None:
        host = KanboardBoardHost(self.client, data_dir=str(self.data_dir))
        self.assertIsInstance(host.canon.audit, SqlTaskAudit)

    def test_the_canon_refuses_when_no_store_can_be_chosen_for_it(self) -> None:
        with self.assertRaises(BoardEventCanonUnowned):
            BoardEventCanon(None)

    def test_the_canon_reads_sql_while_the_projection_is_stale(self) -> None:
        event = self.commit("req-1", event_id="evt_1")
        self.stale_projection()

        canon = BoardEventCanon(self.data_dir, audit=self.audit)

        self.assertEqual([held.event_id for held in canon.events(ref="secretary-468")], [event.event_id])
        self.assertEqual(BoardEventCanon(self.data_dir).events(ref="secretary-468"), ())

    def test_the_sprint_reader_traverses_the_sql_audit_and_never_the_file(self) -> None:
        self.commit("req-1", event_id="evt_1")
        self.stale_projection()

        reader = SprintReader(self.client, data_dir=self.data_dir)

        self.assertIsInstance(reader.audit, SqlTaskAudit)
        self.assertEqual([record["event_id"] for record in reader.audit.events()], ["evt_1"])

    def test_a_reader_with_no_data_dir_on_postgres_still_has_its_audit(self) -> None:
        """The PostgreSQL audit needs no data directory at all; the file journal is what needed one."""
        reader = SprintReader(self.client)
        self.assertIsInstance(reader.audit, SqlTaskAudit)


if __name__ == "__main__":
    unittest.main()
