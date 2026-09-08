"""Portable Sprint contract and SQL atomicity probes on PostgreSQL 16."""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
from pathlib import Path
from unittest import mock

from secretary.board.sql_audit import SqlTaskAudit
from secretary.board.sql_cards import SqlCardClient
from secretary.sprint_observer import head_choice
from secretary.sprints import SprintReader, SprintWriter
from secretary.tasks import TaskError, TaskReader, TaskWriter
from tests import test_sprint_executors as executors
from tests import test_sprint_listing_budget as listing_budget
from tests import test_sprint_restore as restore
from tests import test_sprints as shared
from tests.fakes.sprints import ProductSprintKanboard, SprintBackendFixture, _write_project_registry
from tests.sprint_close_fixtures import close_decisions
from tests.sql_backend_fixtures import PostgresBoard, seed_client

BOARD: PostgresBoard


class ContractSqlCardClient(SqlCardClient):
    """Expose the fake fixture's final row probe without changing the production client."""

    @property
    def tasks(self) -> list[dict]:
        return [
            row
            for board_id in (1, 2)
            for status_id in (1, 0)
            for row in self.call("getAllTasks", project_id=board_id, status_id=status_id)
        ]


def setUpModule() -> None:
    global BOARD
    for module in ("psycopg", "sqlalchemy", "alembic"):
        __import__(module)
    BOARD = PostgresBoard()


def tearDownModule() -> None:
    BOARD.stop()


class SqlSprintFixture(SprintBackendFixture):
    BACKEND = "postgres"

    def make_sprint_client(self) -> SqlCardClient:
        root = Path(getattr(getattr(self, "tmp", None), "name", tempfile.gettempdir()))
        client = seed_client(BOARD.fresh_database(), ProductSprintKanboard(), root)
        client.__class__ = ContractSqlCardClient
        self.addCleanup(self._dispose_client, client)
        return client

    def make_ownership_client(self) -> SqlCardClient:
        root = Path(getattr(getattr(self, "tmp", None), "name", tempfile.gettempdir()))
        fake = ProductSprintKanboard()
        fake.tasks = [row for row in fake.tasks if str(row.get("reference", "")).startswith(("product:", "issue:"))]
        client = seed_client(BOARD.fresh_database(), fake, root)
        client.__class__ = ContractSqlCardClient
        self.addCleanup(self._dispose_client, client)
        return client

    @staticmethod
    def _dispose_client(client: SqlCardClient) -> None:
        name = client.credentials.dbname
        client.close()
        BOARD.drop_database(name)

    def setUp(self) -> None:
        self.skip_kanboard_only()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = _write_project_registry(
            Path(self.tmp.name), "secretary", "secretary-instance", "other"
        )
        self.client = self.make_sprint_client()
        self._bind_audit()
        self.writer = SprintWriter(self.client, data_dir=self.tmp.name, instance=self.instance)

    def _bind_audit(self) -> None:
        client = self.client

        class BoundSqlTaskAudit(SqlTaskAudit):
            def __init__(self, _data_dir=None) -> None:
                super().__init__(client)

        for module in (shared, executors):
            patcher = mock.patch.object(module, "TaskAudit", BoundSqlTaskAudit)
            patcher.start()
            self.addCleanup(patcher.stop)

    def arrange_metadata(self, reference: str, **values: object) -> None:
        budget = values.pop("sprint_budget", None)
        if budget is not None:
            document = json.loads(str(budget))
            for event_type, count in (document.get("by_type") or {}).items():
                for occurrence in range(int(count)):
                    self.writer.record_budget(
                        role="steward", actor="fixture", reference=reference,
                        event_type=str(event_type),
                        request_id=f"fixture-budget-{reference}-{event_type}-{occurrence}",
                    )
        if values:
            identity = hashlib.sha256(
                json.dumps(values, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]
            self.writer.restore(
                reference=reference,
                values={str(key): str(value) for key, value in values.items()},
                request_id=f"fixture-restore-{reference}-{identity}",
            )

    def _events(self) -> list[dict]:
        return SqlTaskAudit(self.client).events()


class SqlSprintOwnershipTests(SqlSprintFixture, shared.SprintOwnershipTests):
    pass


class SqlTwoOpenSprintAdmissionTests(SqlSprintFixture, shared.TwoOpenSprintAdmissionTests):
    def setUp(self) -> None:
        SqlSprintFixture.setUp(self)
        (self.instance / "projects" / "third.yaml").write_text("id: third\n", encoding="utf-8")
        self.arrange_product("third", projects=["third"])
        self.third_issue = self.arrange_issue("third", product="third")["ref"]
        self.roots = Path(self.tmp.name) / "repos"


class SqlTwoOpenSprintIsolationTests(SqlSprintFixture, shared.TwoOpenSprintIsolationTests):
    def setUp(self) -> None:
        SqlTwoOpenSprintAdmissionTests.setUp(self)
        self._limit(2)


class SqlSprintTests(SqlSprintFixture, shared.SprintTests):
    pass


class SqlSprintStatusHeadlessCommandTests(SqlSprintFixture, shared.SprintStatusHeadlessCommandTests):
    pass


class SqlSprintAuditTraversalTests(SqlSprintFixture, shared.SprintAuditTraversalTests):
    @contextlib.contextmanager
    def _traversals(self):
        counter = {"count": 0}
        original = SqlTaskAudit.events

        def counting(audit, *args, **kwargs):
            counter["count"] += 1
            return original(audit, *args, **kwargs)

        with mock.patch.object(SqlTaskAudit, "events", counting):
            yield counter


class SqlSprintSingleWriterGuardTests(SqlSprintFixture, shared.SprintSingleWriterGuardTests):
    def setUp(self) -> None:
        shared.SprintSingleWriterGuardTests.setUp(self)
        self._bind_audit()

    def test_observer_can_write_when_another_open_sprint_shares_the_repository(self) -> None:
        """SQL rejects the impossible duplicate reservation without a partial restore."""
        other_ref = self.sprints.restore_create(
            reference="sprint:overlap",
            goal="overlap",
            repositories=["secretary"],
            request_id="seed-overlap-sprint",
        )["sprint"]["ref"]
        with self.assertRaises(TaskError) as raised:
            self.sprints.restore(
                reference=other_ref,
                values={"sprint_reservations": json.dumps(["secretary"])},
                request_id="seed-overlap-reservation",
            )
        self.assertEqual(raised.exception.code, "backend_error")
        self.assertIsNone(self.sprints.audit.event("seed-overlap-reservation"))
        self.assertEqual(
            self.client._query(
                "SELECT sprint_ref FROM sprint_projects WHERE project_id=%s AND reserved",
                ("secretary",),
            ),
            [(self.ref,)],
        )


class SqlSprintReservedProjectGuardTests(SqlSprintFixture, shared.SprintReservedProjectGuardTests):
    def setUp(self) -> None:
        shared.SprintReservedProjectGuardTests.setUp(self)
        self._bind_audit()


class SqlSprintCloseDecisionTests(SqlSprintFixture, shared.SprintCloseDecisionTests):
    def setUp(self) -> None:
        SqlSprintFixture.setUp(self)
        self.second_issue = self.arrange_issue("second", product="secretary")["ref"]
        from secretary.tasks import TaskWriter

        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)

    def test_a_retry_that_states_other_decisions_is_refused(self) -> None:
        """A SQL failure erases the claim, so the retry may state a new complete intent."""
        ref = self._open(issues=["issue:open"])
        card = self._card(ref, "disposed once", "restated-card")
        decisions = {
            "issues": list(shared.KEEP_THE_ISSUE_OPEN["issues"]),
            "cards": [{"ref": card, "verdict": "drop", "reason": "not finished"}],
        }
        with (
            mock.patch.object(TaskWriter, "archive", side_effect=OSError("disk full")),
            self.assertRaises(TaskError) as raised,
        ):
            self.writer.close(
                role="po", actor="operator", reference=ref,
                request_id="restated-close", decisions=decisions,
            )
        self.assertEqual(raised.exception.code, "backend_error")
        self.assertIsNone(self.writer.audit.event("restated-close"))
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")
        self.assertEqual([row["ref"] for row in TaskReader(self.client).list(sprint=ref)], [card])

        changed = {
            "issues": list(shared.KEEP_THE_ISSUE_OPEN["issues"]),
            "cards": [{"ref": card, "verdict": "done", "reason": "landed after all"}],
        }
        result = self.writer.close(
            role="po", actor="operator", reference=ref,
            request_id="restated-close", decisions=changed,
        )
        self.assertEqual(result["archived_tasks"] + result["disposed_tasks"], [card])
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")
        self.assertEqual(len(self.writer.audit.events(reference=ref, kind="closed")), 1)


class SqlSprintExecutorPinTests(SqlSprintFixture, executors.SprintExecutorPinTests):
    pass


class SqlSprintCardExecutorTests(SqlSprintFixture, executors.SprintCardExecutorTests):
    pass


class SqlCardEditExecutorTests(SqlSprintFixture, executors.CardEditExecutorTests):
    pass


class SqlSprintExecutorRecoveryTests(SqlSprintFixture, executors.SprintExecutorRecoveryTests):
    def make_empty_sprint_client(self) -> SqlCardClient:
        return self.make_ownership_client()

    def persisted_record_count(self, client: SqlCardClient) -> int:
        return int(client._query("SELECT count(*) FROM tasks")[0][0]) + int(
            client._query("SELECT count(*) FROM sprints")[0][0]
        )


class SqlSprintRestoreTests(SqlSprintFixture, restore.SprintRestoreTests):
    def setUp(self) -> None:
        # The shared recovery fixture owns its source/target directory layout.
        restore.SprintRestoreTests.setUp(self)
        # The shared fake deliberately exports a cursor to secretary-13 beside a canned
        # secretary-12 card. SQL keeps the scoped current-task FK, so its checkpoint is complete.
        self._align_checkpoint()

    def _align_checkpoint(self) -> None:
        cards_path = self.target_data / "board" / "cards.json"
        cards_payload = json.loads(cards_path.read_text(encoding="utf-8"))
        cards = cards_payload.get("cards", [])
        card_refs = {str(card["reference"]) for card in cards}
        for card in cards:
            if card["reference"] == "secretary-12":
                card.setdefault("metadata", {})["sprint_ref"] = "sprint:entity"
        cards_path.write_text(json.dumps(cards_payload, sort_keys=True) + "\n", encoding="utf-8")

        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for sprint in payload["sprints"]:
            sprint["current_task"] = (
                "secretary-12"
                if sprint["reference"] == "sprint:entity" and "secretary-12" in card_refs
                else ""
            )
            sprint["audit"] = {
                **sprint["audit"],
                "created_at": "2000-01-01T00:00:00Z",
                "updated_at": "2000-01-01T00:00:00Z",
            }
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    def _restore(self, client: object | None = None) -> tuple[object, int]:
        self._align_checkpoint()
        return restore.SprintRestoreTests._restore(self, client)

    def make_target_client(self) -> SqlCardClient:
        return self.make_ownership_client()

    def persisted_record_count(self, client: SqlCardClient) -> int:
        return int(client._query("SELECT count(*) FROM tasks")[0][0]) + int(
            client._query("SELECT count(*) FROM sprints")[0][0]
        )

    def test_export_carries_the_sprint_set_next_to_the_cards(self) -> None:
        exported = self._exported_sprint()
        self.assertEqual(exported["current_task"], "secretary-12")
        self.assertEqual(exported["budget"]["by_type"]["red_ci"], 1)
        self.assertEqual(exported["resume"]["selected_step"], restore.RESUME["selected_step"])


class SqlSprintAtomicityTests(SqlSprintFixture, shared.unittest.TestCase):
    """Real-connection rollback probes for every multi-row Sprint mutation family."""

    def setUp(self) -> None:
        SqlSprintFixture.setUp(self)
        self.client = self.make_ownership_client()
        self._bind_audit()
        self.writer = SprintWriter(self.client, data_dir=self.tmp.name, instance=self.instance)

    def _create(self, request_id: str = "atomic-create") -> dict:
        return self.writer.create(
            role="po",
            actor="operator",
            goal="atomic sprint",
            definition_of_done="every row commits together",
            repositories=[str(Path(self.tmp.name) / "repo")],
            product="secretary",
            issues=["issue:open"],
            projects=["secretary"],
            observer=head_choice("codex-observer"),
            reference="sprint:atomic",
            request_id=request_id,
        )

    def _assert_no_request(self, request_id: str) -> None:
        self.assertEqual(
            self.client._query("SELECT count(*) FROM requests WHERE request_id=%s", (request_id,)),
            [(0,)],
        )
        self.assertEqual(
            self.client._query("SELECT count(*) FROM board_events WHERE request_id=%s", (request_id,)),
            [(0,)],
        )

    @staticmethod
    def _after(original):
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("deterministic failure point")

        return fail

    def test_create_rolls_back_after_request_claim_and_retries_once(self) -> None:
        with (
            mock.patch.object(
                self.client.sprints, "create", side_effect=RuntimeError("after claim")
            ),
            self.assertRaises(TaskError),
        ):
            self._create()
        self._assert_no_request("atomic-create")
        self.assertEqual(self.client._query("SELECT count(*) FROM sprints"), [(0,)])
        self._create()
        self.assertEqual(self.client._query("SELECT count(*) FROM sprints"), [(1,)])

    def test_create_rolls_back_entity_relations_and_reservation(self) -> None:
        original = self.client.sprints._replace_relations
        with mock.patch.object(
            self.client.sprints, "_replace_relations", side_effect=self._after(original)
        ), self.assertRaises(TaskError):
            self._create()
        self._assert_no_request("atomic-create")
        for table in ("sprints", "sprint_repositories", "sprint_issues", "sprint_projects"):
            self.assertEqual(self.client._query(f"SELECT count(*) FROM {table}"), [(0,)])
        self._create()
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_projects WHERE reserved"), [(1,)])

    def test_comment_rolls_back_body_claim_and_event(self) -> None:
        self._create()
        original = self.client.sprints.create_comment
        with mock.patch.object(
            self.client.sprints, "create_comment", side_effect=self._after(original)
        ), self.assertRaises(TaskError):
            self.writer.comment(
                role="po", actor="operator", reference="sprint:atomic",
                body="one body", request_id="atomic-comment",
            )
        self._assert_no_request("atomic-comment")
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_comments"), [(0,)])
        self.writer.comment(
            role="po", actor="operator", reference="sprint:atomic",
            body="one body", request_id="atomic-comment",
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_comments"), [(1,)])

    def test_resume_rolls_back_resume_comment_claim_and_event(self) -> None:
        self._create()
        entry = {
            "selected_step": "continue", "selected_why": "safe",
            "rejected_alternatives": "none", "current_task": "none",
            "dod_state": "pending", "next_safe_step": "run tests",
            "recorded_at": "2026-09-08T00:00:00Z",
        }
        original = self.client.sprints.create_comment
        with mock.patch.object(
            self.client.sprints, "create_comment", side_effect=self._after(original)
        ), self.assertRaises(TaskError):
            self.writer.resume(
                role="po", actor="operator", reference="sprint:atomic",
                entry=entry, request_id="atomic-resume",
            )
        self._assert_no_request("atomic-resume")
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_resumes"), [(0,)])
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_comments"), [(0,)])
        self.writer.resume(
            role="po", actor="operator", reference="sprint:atomic",
            entry=entry, request_id="atomic-resume",
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_resumes"), [(1,)])

    def test_budget_rolls_back_occurrence_claim_and_event(self) -> None:
        self._create()
        original = self.client.sprints._apply
        with (
            mock.patch.object(self.client.sprints, "_apply", side_effect=self._after(original)),
            self.assertRaises(TaskError),
        ):
            self.writer.record_budget(
                role="po", actor="operator", reference="sprint:atomic",
                event_type="red_ci", request_id="atomic-budget",
            )
        self._assert_no_request("atomic-budget")
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_budget_events"), [(0,)])
        self.writer.record_budget(
            role="po", actor="operator", reference="sprint:atomic",
            event_type="red_ci", request_id="atomic-budget",
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_budget_events"), [(1,)])

    def test_close_rolls_back_decisions_transition_and_releases(self) -> None:
        self._create()
        decisions = close_decisions(self.writer, "sprint:atomic")
        original = self.writer._transition_host
        with (
            mock.patch.object(
                self.writer, "_transition_host", side_effect=self._after(original)
            ),
            self.assertRaises(TaskError),
        ):
            self.writer.close(
                role="po", actor="operator", reference="sprint:atomic",
                decisions=decisions, request_id="atomic-close",
            )
        self._assert_no_request("atomic-close")
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_decisions"), [(0,)])
        self.assertEqual(self.client._query("SELECT status FROM sprints"), [("open",)])
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_projects WHERE reserved"), [(1,)])
        self.writer.close(
            role="po", actor="operator", reference="sprint:atomic",
            decisions=decisions, request_id="atomic-close",
        )
        self.assertEqual(self.client._query("SELECT status FROM sprints"), [("closed",)])


class SqlSprintListingBudgetTests(SqlSprintFixture, listing_budget.SprintListingBudgetTests):
    """The four request-count methods execute as their named Kanboard-only exclusions."""


class SqlCloseDecisionFileTests(shared.CloseDecisionFileTests):
    pass


class SqlExecutorValueTests(executors.ExecutorValueTests):
    pass


class SqlObserverPromptExecutorTests(executors.ObserverPromptExecutorTests):
    pass
