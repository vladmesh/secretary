"""New writes carry the PostgreSQL identity; the retired Kanboard identity is only ever read.

Cards (secretary-1669), Sprints and Products/Issues (secretary-1670) have one implementation, so
every audit event a mutation commits names `task_postgres_<n>` / `sprint_postgres_<n>` and backend
kind `postgres`. History written before the cutover keeps its `task_kanboard_<n>` and
`sprint_kanboard_<n>` ids, and those still resolve: by request id, in the ref's history, and to the
number the live row carries.
"""

from __future__ import annotations

import json
from typing import Any

from secretary.board.backend import entity_id, entity_number, record_key
from secretary.board.sql_audit import SqlTaskAudit
from secretary.tasks import TaskReader, TaskWriter
from tests.fakes.sprints import SprintFixture
from tests.observer_identity import as_observer


def _identity_fields(event: dict[str, Any]) -> list[str]:
    """The identity an event names: its entity id, its backend kind, and a typed event's ref."""
    backend = event.get("backend") if isinstance(event.get("backend"), dict) else {}
    return [str(event.get("task_id") or ""), str(backend.get("kind") or ""), str(event.get("ref") or "")]


class NewWritesNamePostgresTests(SprintFixture):
    """One card, one Product, one Issue and one Sprint mutation, each on the store."""

    def _committed(self, request_id: str) -> dict[str, Any]:
        event = SqlTaskAudit(self.client).committed_event(request_id)
        self.assertIsNotNone(event, request_id)
        return event  # type: ignore[return-value]

    def assert_no_retired_identity(self, event: dict[str, Any]) -> None:
        for field in _identity_fields(event):
            self.assertNotIn("kanboard", field, event)
        self.assertNotIn('"kind": "kanboard"', json.dumps(event), event)

    def test_every_event_of_the_four_mutations_names_postgres(self) -> None:
        product = self.arrange_product("identity", projects=["secretary"])
        issue = self.arrange_issue("identity", product="secretary")
        sprint = self._create(goal="identity", request_id="identity-sprint")["sprint"]
        self.writer.comment(
            role="po", actor="operator", reference=sprint["ref"], body="hello", request_id="identity-comment"
        )
        with as_observer(sprint["ref"]):
            card = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="identity",
                target="ready",
                sprint=sprint["ref"],
                request_id="identity-card",
            )["task"]

        # The Sprint's own events carry its PostgreSQL identity and the store's backend kind.
        for request_id in ("identity-sprint", "identity-comment"):
            event = self._committed(request_id)
            self.assertEqual(event["task_id"], sprint["id"])
            self.assertTrue(event["task_id"].startswith("sprint_postgres_"), event)
            self.assertEqual(event["backend"]["kind"], "postgres")
        # The card's creation names `task_postgres_<n>`, the id its reader answers.
        created = self._committed("identity-card")
        self.assertEqual(created["task_id"], TaskReader(self.client).show(card["ref"])["id"])  # type: ignore[arg-type]
        self.assertTrue(created["task_id"].startswith("task_postgres_"), created)
        self.assertEqual(created["backend"]["kind"], "postgres")
        # Product and Issue writes publish typed protocol events of their refs, with no backend.
        refs = {event.get("ref") for event in SqlTaskAudit(self.client).events()}
        self.assertLessEqual({f"product:{product['id']}", issue["ref"]}, refs)

        events = SqlTaskAudit(self.client).events()
        self.assertTrue(events)
        for event in events:
            with self.subTest(kind=event.get("kind"), ref=event.get("ref")):
                self.assert_no_retired_identity(event)

    def test_a_released_product_issue_transaction_completes_under_the_postgres_identity(self) -> None:
        """The file-staged transaction a release before the typed writer left behind, retried now."""
        store = self.product_issue_store()
        intent = {
            "record_type": "product",
            "product_id": "released",
            "product_projects": '["secretary"]',
            "title": "Released",
            "description": "",
            "actor": "po",
        }
        event = store._transaction_event(
            kind="product_created",
            actor="po",
            reference="product:released",
            request_id="released-product",
            intent=intent,
        )
        store.transactions.begin("released-product", kind="product_created", intent=intent, event=event)

        store.retry_transaction("released-product")

        committed = self._committed("released-product")
        number = committed["backend"]["task_id"]
        self.assertEqual(committed["task_id"], entity_id("task", "postgres", number))
        self.assertEqual(committed["backend"]["kind"], "postgres")
        self.assertEqual(number, record_key("product", "released"))
        self.assert_no_retired_identity(committed)


class HistoricalKanboardIdentityTests(SprintFixture):
    """A pre-cutover `task_kanboard_<n>` / `sprint_kanboard_<n>` event still resolves."""

    def _seed_history(self, request_id: str, *, ref: str, kind: str, number: int) -> dict[str, Any]:
        entity = "sprint" if ref.startswith("sprint:") else "task"
        event = {
            "event_id": f"evt_{request_id}",
            "schema_version": 1,
            "occurred_at": "2026-09-01T00:00:00Z",
            "actor": {"role": "po", "id": "operator"},
            "kind": kind,
            "outcome": "success",
            "task_id": f"{entity}_kanboard_{number}",
            "ref": ref,
            "backend": {"kind": "kanboard", "task_id": number, "revision": "history"},
            "request_id": request_id,
            "payload": {},
        }
        SqlTaskAudit(self.client).append(request_id, event)
        return event

    def test_a_card_s_kanboard_history_resolves_by_request_ref_and_number(self) -> None:
        card = TaskReader(self.client).show("secretary-12")  # type: ignore[arg-type]
        number = entity_number("task", card["id"])
        seeded = self._seed_history("history-card", ref="secretary-12", kind="commented", number=number or 0)

        audit = SqlTaskAudit(self.client)
        self.assertEqual(audit.committed_event("history-card"), seeded)
        self.assertIn(seeded, audit.events(reference="secretary-12"))
        self.assertEqual(entity_number("task", seeded["task_id"]), number)
        self.assertTrue(card["id"].startswith("task_postgres_"), card)

    def test_a_sprint_s_kanboard_history_resolves_by_request_ref_and_number(self) -> None:
        sprint = self._create(goal="history", request_id="history-create")["sprint"]
        number = entity_number("sprint", sprint["id"])
        seeded = self._seed_history("history-sprint", ref=sprint["ref"], kind="commented", number=number or 0)

        audit = SqlTaskAudit(self.client)
        self.assertEqual(audit.committed_event("history-sprint"), seeded)
        self.assertIn(seeded, audit.events(reference=sprint["ref"]))
        self.assertEqual(entity_number("sprint", seeded["task_id"]), number)
        # The sprint's own new events beside it name the store.
        self.assertTrue(audit.committed_event("history-create")["task_id"].startswith("sprint_postgres_"))
        self.assertEqual(self.sprint(sprint["ref"])["id"], sprint["id"])
