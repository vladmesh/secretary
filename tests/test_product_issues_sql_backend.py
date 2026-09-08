"""The shared Product/Issue contract and SQL-only atomicity probes on PostgreSQL 16."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from secretary.board import backend
from secretary.board.sql_cards import SqlCardClient
from secretary.product_issues import ProductIssueStore
from secretary.tasks import TaskError
from tests import test_product_issues as shared
from tests.sql_backend_fixtures import PostgresBoard

BOARD: PostgresBoard


def setUpModule() -> None:
    global BOARD
    for module in ("psycopg", "sqlalchemy", "alembic"):
        __import__(module)
    BOARD = PostgresBoard()


def tearDownModule() -> None:
    BOARD.stop()


class SqlProductIssueFixture:
    BACKEND = "postgres"

    def make_store(
        self, *, root: Path, lanes: list[dict[str, object]] | None = None
    ) -> ProductIssueStore:
        config = BOARD.fresh_database()
        client = SqlCardClient(config.for_role("app"), root)
        client._lanes = sorted(str(lane["name"]) for lane in (lanes or []))
        self._clients[id(client)] = client
        store = ProductIssueStore(client, data_dir=root / "data", instance=root)
        self._clients[id(store)] = client
        self.addCleanup(client.close)
        return store

    def add_external_lane(
        self,
        lane: dict[str, object],
        *,
        store: ProductIssueStore | None = None,
        first: bool = False,
    ) -> None:
        client = self._client_for(store or self.store)
        names = client._lane_names()
        names.insert(0 if first else len(names), str(lane["name"]))

    def lane_binding(self, reference: str, *, store: ProductIssueStore | None = None) -> object:
        client = self._client_for(store or self.store)
        row = client.call("getTaskByReference", project_id=1, reference=reference)
        lanes = client.call("getActiveSwimlanes", project_id=1)
        return next((lane["name"] for lane in lanes if lane["id"] == row["swimlane_id"]), None)

    def record_count(self, reference: str, *, store: ProductIssueStore | None = None) -> int:
        client = self._client_for(store or self.store)
        kind, identifier = reference.split(":", 1)
        table, column = ("products", "product_id") if kind == "product" else ("issues", "issue_id")
        return int(client._query(f"SELECT count(*) FROM {table} WHERE {column} = %s", (identifier,))[0][0])


class SqlProductIssueSwimlaneTests(SqlProductIssueFixture, shared.ProductIssueSwimlaneTests):
    KANBOARD_ONLY = shared.ProductIssueSwimlaneTests.KANBOARD_ONLY


class SqlProductIssueStoreTests(SqlProductIssueFixture, shared.ProductIssueStoreTests):
    KANBOARD_ONLY = shared.ProductIssueStoreTests.KANBOARD_ONLY

    def _counts(self, request_id: str, *, reference: str = "") -> tuple[int, int, int, int]:
        client = self.client
        product_id = reference.removeprefix("product:") if reference.startswith("product:") else ""
        issue_id = reference.removeprefix("issue:") if reference.startswith("issue:") else ""
        return (
            int(client._query("SELECT count(*) FROM requests WHERE request_id = %s", (request_id,))[0][0]),
            int(client._query("SELECT count(*) FROM board_events WHERE request_id = %s", (request_id,))[0][0]),
            int(client._query("SELECT count(*) FROM products WHERE product_id = %s", (product_id,))[0][0]),
            int(client._query("SELECT count(*) FROM issues WHERE issue_id = %s", (issue_id,))[0][0]),
        )

    def _create_product(self, request_id: str = "product") -> dict:
        return self.store.create_product(
            product_id="secretary",
            projects=["secretary"],
            title="Secretary",
            description="",
            actor="po",
            request_id=request_id,
        )

    def test_failure_after_claim_rolls_back_and_same_request_retries_once(self) -> None:
        original = self.client.records.create

        def fail_after_stage(**values):
            original(**values)
            raise TaskError("backend_error", "injected after claim", 1)

        with (
            mock.patch.object(self.client.records, "create", side_effect=fail_after_stage),
            self.assertRaises(TaskError) as raised,
        ):
            self._create_product("claim-failure")
        self.assertNotEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self._counts("claim-failure", reference="product:secretary"), (0, 0, 0, 0))
        self._create_product("claim-failure")
        self.assertEqual(self._counts("claim-failure", reference="product:secretary"), (1, 1, 1, 0))

    def test_failure_after_entity_and_relationship_rolls_back(self) -> None:
        original = self.client.records.save_metadata

        def fail_after_entity(task_id, values):
            original(task_id, values)
            raise TaskError("backend_error", "injected after entity", 1)

        with (
            mock.patch.object(self.client.records, "save_metadata", side_effect=fail_after_entity),
            self.assertRaises(TaskError) as raised,
        ):
            self._create_product("entity-failure")
        self.assertNotEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self._counts("entity-failure", reference="product:secretary"), (0, 0, 0, 0))
        self.assertEqual(self.client._query("SELECT count(*) FROM product_projects"), [(0,)])
        self._create_product("entity-failure")
        self.assertEqual(self._counts("entity-failure", reference="product:secretary"), (1, 1, 1, 0))

    def test_failure_after_comment_rolls_back_comment_effect_and_claim(self) -> None:
        self._create_product("seed-product")
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash",
            description="", actor="po", request_id="seed-issue",
        )
        original = self.client.records.create_comment

        def fail_after_comment(task_id, content):
            original(task_id, content)
            raise TaskError("backend_error", "injected after comment", 1)

        with (
            mock.patch.object(self.client.records, "create_comment", side_effect=fail_after_comment),
            self.assertRaises(TaskError) as raised,
        ):
            self.store.update_priority(
                reference=issue["ref"], priority="P0", reason="urgent", actor="po",
                request_id="comment-failure",
            )
        self.assertNotEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self._counts("comment-failure"), (0, 0, 0, 0))
        self.assertEqual(self.client._query("SELECT count(*) FROM issue_comments"), [(0,)])
        self.assertEqual(self.store.show_issue(issue["ref"])["priority"], "P2")
        self.store.update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po",
            request_id="comment-failure",
        )
        self.assertEqual(self.store.show_issue(issue["ref"])["priority"], "P0")
        self.assertEqual(self._counts("comment-failure"), (1, 1, 0, 0))
        self.assertEqual(self.client._query("SELECT count(*) FROM issue_comments"), [(1,)])

    def test_failure_after_event_insert_rolls_back_every_row(self) -> None:
        audit = self.store.audit
        original = audit._write_board_event

        def fail_after_event(request_id, event):
            original(request_id, event)
            raise RuntimeError("injected after event")

        with (
            mock.patch.object(audit, "_write_board_event", side_effect=fail_after_event),
            self.assertRaises(TaskError) as raised,
        ):
            self._create_product("event-failure")
        self.assertNotEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self._counts("event-failure", reference="product:secretary"), (0, 0, 0, 0))
        self._create_product("event-failure")
        self.assertEqual(self._counts("event-failure", reference="product:secretary"), (1, 1, 1, 0))

    def test_unknown_product_and_issue_metadata_round_trips_without_overriding_known_fields(self) -> None:
        with self.client.transaction():
            product_key = self.client.call(
                "createTask", project_id=1, title="Secretary", description="", column_id=1,
                swimlane_id=0, reference="product:secretary",
            )
            self.client.call(
                "saveTaskMetadata", task_id=product_key,
                values={"record_type": "product", "product_id": "secretary",
                        "product_projects": '["secretary"]', "future_product": "kept"},
            )
            issue_key = self.client.call(
                "createTask", project_id=1, title="Crash", description="", column_id=1,
                swimlane_id=0, reference="issue:abc",
            )
            self.client.call(
                "saveTaskMetadata", task_id=issue_key,
                values={"record_type": "issue", "issue_product": "secretary",
                        "issue_kind": "bug", "issue_priority": "P2", "future_issue": "kept"},
            )
        self.assertEqual(self.client.call("getTaskMetadata", task_id=product_key)["future_product"], "kept")
        issue_meta = self.client.call("getTaskMetadata", task_id=issue_key)
        self.assertEqual(issue_meta["future_issue"], "kept")
        self.assertEqual(issue_meta["issue_priority"], "P2")

    def test_board_key_lookup_is_indexed_and_collision_refuses(self) -> None:
        self._create_product("first")
        first_key = backend.record_key("product", "secretary")
        statements: list[str] = []
        query = self.client._query

        def traced(sql, params=()):
            statements.append(sql)
            return query(sql, params)

        with mock.patch.object(self.client, "_query", side_effect=traced):
            self.client.call("getTaskMetadata", task_id=first_key)
        self.assertTrue(any("WHERE board_key = %s" in sql for sql in statements))
        self.assertFalse(any(sql.strip() == "SELECT product_id FROM products" for sql in statements))

        with (
            mock.patch("secretary.board.sql_product_issues.record_key", return_value=first_key),
            self.assertRaises(TaskError),
        ):
            self.store.create_product(
                product_id="other", projects=["secretary"], title="Other", description="",
                actor="po", request_id="collision",
            )
        self.assertEqual(self.record_count("product:other"), 0)

    def test_record_observation_includes_archived_product_and_closed_issue(self) -> None:
        self._create_product("visible-product")
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash",
            description="", actor="po", request_id="visible-issue",
        )
        self.store.close_issue(
            reference=issue["ref"], reason="resolved", actor="po", request_id="closed-issue"
        )
        self.client.call("closeTask", task_id=backend.record_key("product", "secretary"))

        self.assertEqual(self.record_count("product:secretary"), 1)
        self.assertEqual(self.record_count(issue["ref"]), 1)
        self.assertTrue(self.store.show_product("secretary")["closed"])
        self.assertTrue(self.store.show_issue(issue["ref"])["closed"])


class SqlBackendProductIssueSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        backend.reset_card_backend()
        self.addCleanup(backend.reset_card_backend)

    def test_postgres_serves_product_issue_but_still_refuses_sprint(self) -> None:
        self.assertIn(backend.PRODUCT_ISSUE, backend.POSTGRES_SERVES)
        self.assertNotIn(backend.SPRINT, backend.POSTGRES_SERVES)

    def test_record_keys_are_stable_disjoint_and_not_card_numbers(self) -> None:
        product = backend.record_key("product", "secretary")
        issue = backend.record_key("issue", "secretary")
        self.assertEqual(product, backend.record_key("product", "secretary"))
        self.assertEqual(backend.record_key_kind(product), "product")
        self.assertEqual(backend.record_key_kind(issue), "issue")
        self.assertIsNone(backend.record_key_kind(1596))
        self.assertNotEqual(product, issue)
