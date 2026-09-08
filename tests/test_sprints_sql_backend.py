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
from secretary.board.sql_sprints import sprint_key
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

    def test_sql_unique_live_reservation_rolls_back_an_unrepresentable_overlap(self) -> None:
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

    def test_sql_rolled_back_claim_allows_a_changed_intent_as_a_new_request(self) -> None:
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
        restore.SprintRestoreTests.setUp(self)

    def make_target_client(self) -> SqlCardClient:
        return self.make_ownership_client()

    def persisted_record_count(self, client: SqlCardClient) -> int:
        return int(client._query("SELECT count(*) FROM tasks")[0][0]) + int(
            client._query("SELECT count(*) FROM sprints")[0][0]
        )

    def test_sql_restore_refuses_an_unlinked_current_task_before_writes(self) -> None:
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["current_task"] = "secretary:not-in-export"
        path.write_text(json.dumps(payload), encoding="utf-8")
        client = self.make_target_client()

        with self.assertRaisesRegex(restore.RestoreError, "not an included Card already linked"):
            restore.import_normalized_board(self.target_data, client=client, instance=self.instance)
        self.assertEqual(self.persisted_record_count(client), 0)


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

    @staticmethod
    def _resume_entry() -> dict[str, str]:
        return {
            "selected_step": "continue", "selected_why": "safe",
            "rejected_alternatives": "none", "current_task": "none",
            "dod_state": "pending", "next_safe_step": "run tests",
            "recorded_at": "2026-09-08T00:00:00Z",
        }

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

    def test_comment_rolls_back_after_claim_before_body_and_retries(self) -> None:
        self._create()
        with mock.patch.object(
            self.client.sprints, "create_comment", side_effect=RuntimeError("after claim")
        ), self.assertRaises(TaskError):
            self.writer.comment(
                role="po", actor="operator", reference="sprint:atomic",
                body="one body", request_id="atomic-comment-claim",
            )
        self._assert_no_request("atomic-comment-claim")
        self.writer.comment(
            role="po", actor="operator", reference="sprint:atomic",
            body="one body", request_id="atomic-comment-claim",
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_comments"), [(1,)])

    def test_resume_rolls_back_resume_comment_claim_and_event(self) -> None:
        self._create()
        entry = self._resume_entry()
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

    def test_resume_rolls_back_after_claim_before_relations_and_retries(self) -> None:
        self._create()
        with mock.patch.object(
            self.client.sprints, "_apply", side_effect=RuntimeError("after claim")
        ), self.assertRaises(TaskError):
            self.writer.resume(
                role="po", actor="operator", reference="sprint:atomic",
                entry=self._resume_entry(), request_id="atomic-resume-claim",
            )
        self._assert_no_request("atomic-resume-claim")
        self.writer.resume(
            role="po", actor="operator", reference="sprint:atomic",
            entry=self._resume_entry(), request_id="atomic-resume-claim",
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

    def test_budget_rolls_back_after_claim_before_occurrence_and_retries(self) -> None:
        self._create()
        with mock.patch.object(
            self.client.sprints, "_apply", side_effect=RuntimeError("after claim")
        ), self.assertRaises(TaskError):
            self.writer.record_budget(
                role="po", actor="operator", reference="sprint:atomic",
                event_type="red_ci", request_id="atomic-budget-claim",
            )
        self._assert_no_request("atomic-budget-claim")
        self.writer.record_budget(
            role="po", actor="operator", reference="sprint:atomic",
            event_type="red_ci", request_id="atomic-budget-claim",
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

    def test_close_rolls_back_after_claim_before_decisions_and_retries(self) -> None:
        self._create()
        decisions = close_decisions(self.writer, "sprint:atomic")
        with mock.patch.object(
            self.client.sprints, "save_close", side_effect=RuntimeError("after claim")
        ), self.assertRaises(TaskError):
            self.writer.close(
                role="po", actor="operator", reference="sprint:atomic",
                decisions=decisions, request_id="atomic-close-claim",
            )
        self._assert_no_request("atomic-close-claim")
        self.writer.close(
            role="po", actor="operator", reference="sprint:atomic",
            decisions=decisions, request_id="atomic-close-claim",
        )
        self.assertEqual(self.client._query("SELECT status FROM sprints"), [("closed",)])

    def test_reservation_rolls_back_at_both_boundaries_and_retries(self) -> None:
        self.writer.restore_create(
            reference="sprint:atomic", goal="reservation target", request_id="seed-reservation"
        )
        for suffix, side_effect in (
            ("claim", RuntimeError("after claim")),
            ("relations", self._after(self.client.sprints._replace_relations)),
        ):
            with self.subTest(boundary=suffix):
                request_id = f"atomic-reservation-{suffix}"
                with mock.patch.object(
                    self.client.sprints, "_replace_relations", side_effect=side_effect
                ), self.assertRaises(TaskError):
                    self.writer.restore(
                        reference="sprint:atomic",
                        values={"sprint_reservations": json.dumps(["secretary"])},
                        request_id=request_id,
                    )
                self._assert_no_request(request_id)
                self.writer.restore(
                    reference="sprint:atomic",
                    values={"sprint_reservations": json.dumps(["secretary"])},
                    request_id=request_id,
                )
                self.assertEqual(
                    self.client._query("SELECT count(*) FROM sprint_projects WHERE reserved"), [(1,)]
                )
                self.client._execute("DELETE FROM sprint_projects")
                self.client.connection.commit()

    def _reservation_rows(self, reference: str) -> list[tuple]:
        return self.client._query(
            "SELECT project_id, reserved, ordinal, released_at IS NOT NULL "
            "FROM sprint_projects WHERE sprint_ref=%s ORDER BY project_id",
            (reference,),
        )

    def _seed_two_reservations(self, reference: str = "sprint:reservation-replace") -> str:
        self.writer.restore_create(
            reference=reference,
            goal="reservation replacement",
            request_id=f"seed-{reference}",
        )
        self.writer.restore(
            reference=reference,
            values={"sprint_reservations": json.dumps(["secretary", "other"])},
            request_id=f"seed-reservations-{reference}",
        )
        return reference

    def test_restore_replaces_the_active_reservation_set_and_replay_is_exact(self) -> None:
        reference = self._seed_two_reservations()

        first = self.writer.restore(
            reference=reference,
            values={"sprint_reservations": json.dumps(["other"])},
            request_id="narrow-reservations",
        )
        rows = self._reservation_rows(reference)
        second = self.writer.restore(
            reference=reference,
            values={"sprint_reservations": json.dumps(["other"])},
            request_id="narrow-reservations",
        )

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(self._reservation_rows(reference), rows)
        self.assertEqual(
            rows,
            [("other", True, 0, False), ("secretary", False, 0, True)],
        )
        self.assertEqual(first["sprint"]["reservations"], ["other"])

    def test_reservation_narrowing_rolls_back_at_both_boundaries(self) -> None:
        for suffix in ("claim", "relations"):
            with self.subTest(boundary=suffix):
                self.client = self.make_ownership_client()
                self.writer = SprintWriter(
                    self.client, data_dir=self.tmp.name, instance=self.instance
                )
                reference = self._seed_two_reservations(f"sprint:narrow-{suffix}")
                before = self._reservation_rows(reference)
                original = self.client.sprints._replace_relations
                effect = RuntimeError("after claim") if suffix == "claim" else self._after(original)
                request_id = f"narrow-rollback-{suffix}"

                with mock.patch.object(
                    self.client.sprints, "_replace_relations", side_effect=effect
                ), self.assertRaises(TaskError):
                    self.writer.restore(
                        reference=reference,
                        values={"sprint_reservations": json.dumps(["other"])},
                        request_id=request_id,
                    )

                self._assert_no_request(request_id)
                self.assertEqual(self._reservation_rows(reference), before)
                self.writer.restore(
                    reference=reference,
                    values={"sprint_reservations": '["other"]'},
                    request_id=request_id,
                )
                self.assertEqual(
                    self._reservation_rows(reference),
                    [("other", True, 0, False), ("secretary", False, 0, True)],
                )

    def test_restore_cannot_take_a_project_reserved_by_another_sprint(self) -> None:
        holder = self._seed_two_reservations("sprint:reservation-holder")
        contender = "sprint:reservation-contender"
        self.writer.restore_create(
            reference=contender, goal="contender", request_id="seed-contender"
        )

        with self.assertRaises(TaskError) as raised:
            self.writer.restore(
                reference=contender,
                values={"sprint_reservations": json.dumps(["other"])},
                request_id="contested-reservation",
            )

        self.assertEqual(raised.exception.code, "backend_error")
        self._assert_no_request("contested-reservation")
        self.assertEqual(self._reservation_rows(contender), [])
        self.assertEqual(
            [row for row in self._reservation_rows(holder) if row[0] == "other"],
            [("other", True, 1, False)],
        )

    def test_restore_create_rolls_back_at_both_boundaries_and_retries(self) -> None:
        for suffix in ("claim", "relations"):
            with self.subTest(boundary=suffix):
                reference = f"sprint:restore-{suffix}"
                request_id = f"atomic-restore-create-{suffix}"
                target = "create" if suffix == "claim" else "_replace_relations"
                original = getattr(self.client.sprints, target)
                effect = RuntimeError("after claim") if suffix == "claim" else self._after(original)
                with mock.patch.object(
                    self.client.sprints, target, side_effect=effect
                ), self.assertRaises(TaskError):
                    self.writer.restore_create(
                        reference=reference, goal="restored", repositories=["/repo"],
                        request_id=request_id,
                    )
                self._assert_no_request(request_id)
                self.assertEqual(
                    self.client._query("SELECT count(*) FROM sprints WHERE ref=%s", (reference,)), [(0,)]
                )
                self.writer.restore_create(
                    reference=reference, goal="restored", repositories=["/repo"],
                    request_id=request_id,
                )
                self.assertEqual(
                    self.client._query("SELECT count(*) FROM sprints WHERE ref=%s", (reference,)), [(1,)]
                )

    def test_reopen_rolls_back_at_both_boundaries_and_retries(self) -> None:
        for suffix in ("claim", "entity"):
            with self.subTest(boundary=suffix):
                client = self.make_ownership_client()
                writer = SprintWriter(client, data_dir=self.tmp.name, instance=self.instance)
                ref = writer.create(
                    role="po", actor="operator", goal="reopen", repositories=["/repo"],
                    product="secretary", issues=["issue:open"], projects=["secretary"],
                    observer=head_choice("codex-observer"), reference=f"sprint:reopen-{suffix}",
                    request_id=f"seed-reopen-{suffix}",
                )["sprint"]["ref"]
                writer.close(
                    role="po", actor="operator", reference=ref,
                    decisions=close_decisions(writer, ref), request_id=f"close-reopen-{suffix}",
                )
                original = writer._transition_host
                effect = RuntimeError("after claim") if suffix == "claim" else self._after(original)
                request_id = f"atomic-reopen-{suffix}"
                with mock.patch.object(
                    writer, "_transition_host", side_effect=effect
                ), self.assertRaises(TaskError):
                    writer.reopen(
                        role="po", actor="operator", reference=ref,
                        observer=head_choice("codex-observer"), request_id=request_id,
                    )
                self.assertEqual(
                    client._query("SELECT count(*) FROM requests WHERE request_id=%s", (request_id,)), [(0,)]
                )
                self.assertEqual(client._query("SELECT status FROM sprints WHERE ref=%s", (ref,)), [("closed",)])
                writer.reopen(
                    role="po", actor="operator", reference=ref,
                    observer=head_choice("codex-observer"), request_id=request_id,
                )
                self.assertEqual(client._query("SELECT status FROM sprints WHERE ref=%s", (ref,)), [("open",)])


class SqlTransportNamespaceTests(SqlSprintFixture, shared.unittest.TestCase):
    def test_canonical_and_unicode_digit_refs_are_distinct_and_lossless(self) -> None:
        writer = SprintWriter(self.client, data_dir=self.tmp.name)
        ascii_ref = writer.restore_create(
            reference="sprint:1", goal="ASCII", request_id="ascii-ref"
        )["sprint"]["ref"]
        unicode_ref = writer.restore_create(
            reference="sprint:١", goal="Unicode", request_id="unicode-ref"
        )["sprint"]["ref"]

        self.assertEqual((ascii_ref, unicode_ref), ("sprint:1", "sprint:١"))
        self.assertEqual(
            self.client._query(
                "SELECT ref, sprint_number FROM sprints "
                "WHERE ref IN (%s,%s) ORDER BY ref",
                (ascii_ref, unicode_ref),
            ),
            [("sprint:1", 1), ("sprint:١", None)],
        )
        self.assertNotEqual(sprint_key(ascii_ref), sprint_key(unicode_ref))

    def test_a_leading_zero_ref_is_rejected_before_sql_persistence(self) -> None:
        writer = SprintWriter(self.client, data_dir=self.tmp.name)

        with self.assertRaisesRegex(TaskError, "must be canonical") as raised:
            writer.restore_create(
                reference="sprint:01", goal="alias", request_id="leading-zero-ref"
            )

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(
            self.client._query("SELECT count(*) FROM sprints WHERE ref='sprint:01'"), [(0,)]
        )
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM requests WHERE request_id='leading-zero-ref'"
            ),
            [(0,)],
        )

    def test_secretary_5_and_sprint_5_never_cross_dispatch(self) -> None:
        self.client = self.make_ownership_client()
        self.client.call(
            "createTask", project_id=1, title="card five", reference="secretary-5", column_id=2
        )
        self.client.call(
            "saveTaskMetadata", task_id=5,
            values={"project": "secretary", "task_type": "code", "worker_profile": "codex-high"},
        )
        self.client.call("createComment", task_id=5, content="card comment")
        writer = SprintWriter(self.client, data_dir=self.tmp.name)
        writer.restore_create(
            reference="sprint:5", goal="goal", definition_of_done="dod",
            request_id="transport-sprint-create",
        )

        key = sprint_key("sprint:5")
        self.assertNotEqual(key, 5)
        self.assertEqual(self.client.call("getTaskMetadata", task_id=5)["worker_profile"], "codex-high")
        self.assertEqual(
            [row["comment"] for row in self.client.call("getAllComments", task_id=5)],
            ["card comment"],
        )
        self.assertEqual(self.client.call("getTaskMetadata", task_id=key)["sprint_goal"], "goal")

        self.client.call("saveTaskMetadata", task_id=5, values={"worker_profile": "claude-opus"})
        self.client.call("createComment", task_id=5, content="second card comment")
        self.client.call("updateTask", id=5, title="card five edited")
        self.client.call("closeTask", task_id=5)

        self.assertEqual(
            self.client._query("SELECT title, archived FROM tasks WHERE task_ref='secretary-5'"),
            [("card five edited", True)],
        )
        self.assertEqual(
            self.client._query("SELECT body FROM task_comments WHERE task_ref='secretary-5' ORDER BY comment_id"),
            [("card comment",), ("second card comment",)],
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_comments"), [(0,)])
        self.assertEqual(self.client._query("SELECT status FROM sprints WHERE ref='sprint:5'"), [("open",)])

    def test_restore_current_task_never_attaches_or_reparents_a_card(self) -> None:
        self.client = self.make_sprint_client()
        writer = SprintWriter(self.client, data_dir=self.tmp.name)
        writer.restore_create(
            reference="sprint:900", goal="cursor target", request_id="cursor-create"
        )

        with self.assertRaisesRegex(TaskError, "not a Card linked to sprint:900") as raised:
            writer.restore(
                reference="sprint:900",
                values={"sprint_current_task": "secretary-12"},
                request_id="cursor-restore",
            )
        self.assertEqual(raised.exception.code, "backend_error")

        self.assertEqual(
            self.client._query("SELECT sprint_ref FROM tasks WHERE task_ref='secretary-12'"),
            [(None,)],
        )
        self.assertEqual(
            self.client._query("SELECT current_task_ref FROM sprints WHERE ref='sprint:900'"),
            [(None,)],
        )
        self.assertEqual(
            self.client._query("SELECT count(*) FROM requests WHERE request_id='cursor-restore'"),
            [(0,)],
        )


class SqlSprintListingBudgetTests(SqlSprintFixture, listing_budget.SprintListingBudgetTests):
    """The four request-count methods execute as their named Kanboard-only exclusions."""


class SqlCloseDecisionFileTests(shared.CloseDecisionFileTests):
    pass


class SqlExecutorValueTests(executors.ExecutorValueTests):
    pass


class SqlObserverPromptExecutorTests(executors.ObserverPromptExecutorTests):
    pass
