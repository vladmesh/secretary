"""A staged audit row its writer never settled is settled by a later tick (secretary-1664).

On PostgreSQL `SqlTaskAudit.stage` and `claim` commit a `staged` row on their own when no
transaction is open, and a process that dies before `append` or `discard` leaves it staged for
good: `status()` counts it and every checkpoint is blocked on it. These cases run against a real
`postgres:16` (`tests/sql_backend_fixtures.py`) and prove the four halves of the settlement:

* a row whose effect is present is **committed**, and the effect is not applied a second time;
* a row whose effect is absent is **refused** terminally, durably and visibly, and its request id
  cannot be claimed again;
* a row younger than the grace is **left alone**;
* a checkpoint blocked by a stale row **publishes on the next tick**.

"The writer died" is written the only way it can be on this backend: the effect's transaction
committed and the row that claims it was left `staged` (or, for an absent effect, the row was staged
on its own and nothing followed). Like the other `*_sql_backend` suites this needs Docker.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary.board.events import BoardEventCanon
from secretary.board.models import Actor, EntityKind, Event, EventKind
from secretary.board.sql_audit import (
    REFUSAL_KIND,
    STALE_STAGED_GRACE_SECONDS,
    SqlTaskAudit,
)
from secretary.checkpoint import CheckpointWriter, checkpoint_snapshot
from secretary.dispatch.production import _write_checkpoint
from secretary.sprints import SprintWriter
from secretary.tasks import TaskError
from tests import test_checkpoint as checkpoint_cases
from tests.fakes.sprints import ProductSprintKanboard, _write_project_registry
from tests.sql_backend_fixtures import PostgresBoard, seed_client

BOARD: PostgresBoard


def setUpModule() -> None:
    global BOARD
    for module in ("psycopg", "sqlalchemy", "alembic"):
        __import__(module)
    BOARD = PostgresBoard()


def tearDownModule() -> None:
    BOARD.stop()


#: Past the grace, by as much again: the row a dead writer left an hour of ticks ago.
STALE_MINUTES = STALE_STAGED_GRACE_SECONDS // 60 + 5


class SettlementCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        instance = _write_project_registry(self.tmp, "secretary", "secretary-instance", "other")
        config = BOARD.fresh_database()
        self.client = seed_client(config, ProductSprintKanboard(), self.tmp)
        self.addCleanup(BOARD.drop_database, config.dbname)
        self.addCleanup(self.client.close)
        self.audit = SqlTaskAudit(self.client)
        self.sprints = SprintWriter(self.client, data_dir=str(self.tmp), instance=instance)
        self.sprint = self.sprints.restore_create(
            reference="sprint:41", goal="settle stale rows", repositories=["secretary"], request_id="seed-sprint"
        )["sprint"]["ref"]

    # -- the dead writer ------------------------------------------------------------------------

    def died_after_its_effect(self, request_id: str) -> None:
        """The effect committed and the process died before the row was appended."""
        self.client._execute(
            "UPDATE requests SET status = 'staged', settled_at = NULL WHERE request_id = %s",
            (request_id,),
        )
        self.client._commit_unless_nested()

    def age(self, request_id: str, minutes: float) -> None:
        self.client._execute(
            "UPDATE requests SET created_at = now() - make_interval(secs => %s) WHERE request_id = %s",
            (minutes * 60, request_id),
        )
        self.client._commit_unless_nested()

    def status_of(self, request_id: str) -> str | None:
        rows = self.client._query("SELECT status FROM requests WHERE request_id = %s", (request_id,))
        return rows[0][0] if rows else None

    def budget_rows(self) -> int:
        return int(self.client._query("SELECT count(*) FROM sprint_budget_events")[0][0])

    def charge(self, request_id: str) -> dict[str, Any]:
        self.sprints.record_budget(
            role="steward", actor="fixture", reference=self.sprint, event_type="red_ci", request_id=request_id
        )
        committed = self.audit.committed_event(request_id)
        assert committed is not None
        return committed

    def card_event(self, event_id: str) -> Event:
        return Event(
            event_id=event_id,
            kind=EventKind.CARD_STARTED,
            entity_kind=EntityKind.CARD,
            ref="secretary-468",
            actor=Actor("dispatcher", "secretary-production"),
            reason="claimed for the worker",
            occurred_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
            source_state="ready",
            target_state="in_progress",
        )


class StaleStagedSettlementTests(SettlementCase):
    def test_a_row_whose_effect_is_present_is_committed_without_applying_it_again(self) -> None:
        self.charge("req-charge")
        self.died_after_its_effect("req-charge")
        self.age("req-charge", STALE_MINUTES)
        self.assertEqual(self.audit.status(), {"ok": False, "pending": 1})
        rows_before = self.budget_rows()

        outcomes = self.audit.settle_stale_staged()

        self.assertEqual([(o["request_id"], o["outcome"]) for o in outcomes], [("req-charge", "committed")])
        self.assertIn("sprint_budget_events holds a row claiming req-charge", outcomes[0]["reason"])
        self.assertEqual(self.status_of("req-charge"), "committed")
        self.assertEqual(self.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(self.budget_rows(), rows_before)
        self.assertEqual(self.audit.settle_stale_staged(), [])

    def test_a_row_whose_effect_is_absent_is_refused_terminally_and_visibly(self) -> None:
        record = dict(self.charge("req-template"))
        record.update({"request_id": "req-lost", "event_id": "evt_lost"})
        self.audit.stage("req-lost", record)
        self.age("req-lost", STALE_MINUTES)
        rows_before = self.budget_rows()

        outcomes = self.audit.settle_stale_staged()

        self.assertEqual([(o["request_id"], o["outcome"]) for o in outcomes], [("req-lost", "refused")])
        self.assertIn("its effect is absent", outcomes[0]["reason"])
        self.assertEqual(self.status_of("req-lost"), "discarded")
        self.assertEqual(self.audit.status(), {"ok": True, "pending": 0})
        self.assertIsNone(self.audit.event("req-lost"))
        self.assertEqual(self.budget_rows(), rows_before, "a refusal never applies the effect")
        refusal = self.audit.refusal("req-lost")
        assert refusal is not None
        self.assertEqual(refusal["kind"], REFUSAL_KIND)
        self.assertEqual(refusal["ref"], self.sprint)
        self.assertEqual(refusal["payload"]["refused_request_id"], "req-lost")
        self.assertEqual(refusal["payload"]["refused_kind"], "budget_recorded")
        self.assertIn("its effect is absent", refusal["payload"]["reason"])
        self.assertIn(refusal, self.audit.events(self.sprint, kind=REFUSAL_KIND))
        with self.assertRaises(TaskError) as retried:
            self.audit.stage("req-lost", record)
        self.assertEqual(retried.exception.code, "validation")
        self.assertIn("refused when its stale staged record was settled", retried.exception.message)
        self.assertEqual(self.status_of("req-lost"), "discarded")

    def requests_snapshot(self) -> list[tuple[Any, ...]]:
        return self.client._query(
            "SELECT request_id, operation, intent::text, status, protocol, entity_kind, ref, "
            "created_at, settled_at FROM requests ORDER BY request_id"
        )

    def test_a_refusal_never_writes_over_a_record_that_owns_its_id(self) -> None:
        """The reviewer's collision: a caller already committed `audit-refused:req-lost`."""
        self.charge("audit-refused:req-lost")
        owner_before = [row for row in self.requests_snapshot() if row[0] == "audit-refused:req-lost"]
        record = dict(self.charge("req-template"))
        record.update({"request_id": "req-lost", "event_id": "evt_lost"})
        self.audit.stage("req-lost", record)
        self.age("req-lost", STALE_MINUTES)

        outcomes = self.audit.settle_stale_staged()

        self.assertEqual([o["outcome"] for o in outcomes], ["refused"])
        owner_after = [row for row in self.requests_snapshot() if row[0] == "audit-refused:req-lost"]
        self.assertEqual(owner_after, owner_before, "the owner of the id stays byte-identical")
        self.assertEqual(self.audit.committed_event("audit-refused:req-lost")["kind"], "budget_recorded")
        self.assertEqual(self.status_of("req-lost"), "discarded")
        refusal = self.audit.refusal("req-lost")
        assert refusal is not None
        self.assertEqual(refusal["request_id"], "audit-refused:req-lost#1")
        self.assertEqual(refusal["kind"], REFUSAL_KIND)
        self.assertEqual(refusal["payload"]["refused_request_id"], "req-lost")
        self.assertIn(refusal, self.audit.events(self.sprint, kind=REFUSAL_KIND))
        with self.assertRaises(TaskError) as retried:
            self.audit.stage("req-lost", record)
        self.assertIn("its effect is absent", retried.exception.message)

    def test_a_second_settlement_pass_writes_nothing_new(self) -> None:
        record = dict(self.charge("req-template"))
        record.update({"request_id": "req-lost", "event_id": "evt_lost"})
        self.audit.stage("req-lost", record)
        self.age("req-lost", STALE_MINUTES)
        self.charge("req-present")
        self.died_after_its_effect("req-present")
        self.age("req-present", STALE_MINUTES)
        self.assertEqual(len(self.audit.settle_stale_staged()), 2)
        settled = self.requests_snapshot()
        budget = self.budget_rows()

        self.assertEqual(self.audit.settle_stale_staged(), [])

        self.assertEqual(self.requests_snapshot(), settled)
        self.assertEqual(self.budget_rows(), budget)

    def test_a_row_whose_effect_cannot_be_proven_is_refused_rather_than_guessed(self) -> None:
        BoardEventCanon(self.tmp, audit=self.audit).stage("req-transition", self.card_event("evt_transition"))
        self.age("req-transition", STALE_MINUTES)

        outcomes = self.audit.settle_stale_staged()

        self.assertEqual([o["outcome"] for o in outcomes], ["refused"])
        self.assertIn("cannot be verified", outcomes[0]["reason"])
        self.assertEqual(self.status_of("req-transition"), "discarded")
        self.assertEqual(
            self.client._query("SELECT count(*) FROM board_events WHERE event_id = 'evt_transition'"),
            [(0,)],
        )

    def test_a_record_only_row_is_committed_as_the_record_it_is(self) -> None:
        record = {
            "event_id": "evt_denied",
            "schema_version": 1,
            "occurred_at": "2026-09-21T12:00:00Z",
            "actor": {"role": "worker", "id": "codex"},
            "kind": "sprint_guard_denied",
            "outcome": "denied",
            "task_id": "",
            "ref": "secretary-468",
            "backend": {"kind": "postgres", "task_id": None, "revision": "not_written"},
            "request_id": "req-denied",
            "payload": {"code": "sprint_guard", "message": "denied"},
        }
        self.audit.stage("req-denied", record)
        self.age("req-denied", STALE_MINUTES)

        outcomes = self.audit.settle_stale_staged()

        self.assertEqual([o["outcome"] for o in outcomes], ["committed"])
        self.assertEqual(self.audit.committed_event("req-denied"), record)

    def test_a_row_younger_than_the_grace_is_left_alone(self) -> None:
        record = dict(self.charge("req-template"))
        record.update({"request_id": "req-in-flight", "event_id": "evt_in_flight"})
        self.audit.stage("req-in-flight", record)
        self.age("req-in-flight", STALE_STAGED_GRACE_SECONDS / 60 - 1)

        self.assertEqual(self.audit.settle_stale_staged(), [])

        self.assertEqual(self.status_of("req-in-flight"), "staged")
        self.assertIsNone(self.audit.refusal("req-in-flight"))
        # Its own writer still finishes it normally.
        self.audit.append("req-in-flight", record)
        self.assertEqual(self.status_of("req-in-flight"), "committed")


class StaleStagedCheckpointTests(SettlementCase):
    """A checkpoint blocked by a stale staged row publishes on the next tick."""

    def setUp(self) -> None:
        super().setUp()
        self.data_dir = self.tmp / "secretary-data"
        self.instance_dir = self.tmp / "secretary-instance"
        (self.data_dir / "board").mkdir(parents=True)
        (self.data_dir / "runs").mkdir(parents=True)
        self.instance_dir.mkdir()
        checkpoint_cases.git(self.instance_dir, "init", "--quiet", "--initial-branch", "main")
        checkpoint_cases.git(self.instance_dir, "config", "user.name", "operator")
        checkpoint_cases.git(self.instance_dir, "config", "user.email", "operator@example.invalid")
        (self.instance_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        checkpoint_cases.git(self.instance_dir, "add", "instance.yaml")
        checkpoint_cases.git(self.instance_dir, "commit", "--quiet", "-m", "config")
        checkpoint_cases.CheckpointWriterTests.seed_board(self, [checkpoint_cases.CARD])  # type: ignore[arg-type]
        checkpoint_cases.CheckpointWriterTests.seed_runs(self, [])  # type: ignore[arg-type]

    def write(self) -> Any:
        return checkpoint_cases.CheckpointWriterTests.write(self, self.client)  # type: ignore[arg-type]

    def writer(self, client: Any) -> CheckpointWriter:
        return CheckpointWriter(self.data_dir, self.instance_dir, client=client)

    def test_the_tick_after_the_grace_publishes_the_checkpoint_the_row_blocked(self) -> None:
        self.charge("req-charge")
        self.died_after_its_effect("req-charge")
        self.age("req-charge", STALE_STAGED_GRACE_SECONDS / 60 - 2)

        blocked = self.write()

        self.assertEqual(blocked.status, "blocked")
        self.assertRegex(
            blocked.reason,
            r"^the postgres task audit has 1 unresolved pending record\(s\); oldest budget_recorded "
            r"req-charge staged since \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$",
        )

        # The dispatcher records that run and retries on its next tick; by then the grace is over.
        state = {"last_success_epoch": 0.0}
        runtime = type("Runtime", (), {})()
        runtime.checkpoint = type("Blocked", (), {"write": lambda _self: blocked})()
        state = _write_checkpoint(runtime, state, 1_000.0)
        self.assertTrue(state["retry_pending"])
        self.age("req-charge", STALE_MINUTES)

        published = self.write()

        self.assertEqual(published.status, "committed", published.reason)
        self.assertEqual(self.status_of("req-charge"), "committed")
        self.assertEqual(self.audit.status(), {"ok": True, "pending": 0})


class FailingSinceTests(unittest.TestCase):
    """The dispatcher keeps the first failure after the last success, so doctor can say since when."""

    @staticmethod
    def run_once(state: dict[str, Any], status: str, now: float) -> dict[str, Any]:
        result = type("Result", (), {"to_json": lambda _self: {"status": status, "reason": "gate shut"}})()
        runtime = type("Runtime", (), {})()
        runtime.checkpoint = type("Writer", (), {"write": lambda _self: result})()
        return _write_checkpoint(runtime, state, now)

    def test_failing_since_survives_repeated_failures_and_clears_on_success(self) -> None:
        instance = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state = self.run_once({}, "committed", 1_000.0)
        state = self.run_once(state, "blocked", 1_300.0)
        state = self.run_once(state, "blocked", 1_360.0)
        self.assertEqual(state["failing_since_epoch"], 1_300.0)
        self.assertEqual(state["last_failure_epoch"], 1_360.0)
        snapshot = checkpoint_snapshot(instance, write_state=state, now=1_000.0 + 31 * 60)
        self.assertTrue(snapshot["rpo_exceeded"])
        self.assertEqual(snapshot["unpublished_minutes"], 31)
        self.assertEqual(
            snapshot["rpo_reason"], "checkpoint gate blocked since 1970-01-01T00:21:40Z: gate shut"
        )
        state = self.run_once(state, "unchanged", 1_420.0)
        self.assertNotIn("failing_since_epoch", state)
        self.assertFalse(checkpoint_snapshot(instance, write_state=state, now=1_500.0)["rpo_exceeded"])


if __name__ == "__main__":
    unittest.main()
