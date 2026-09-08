"""The importer against a real ``postgres:16``: a synthetic board, migrated, imported, checked.

``tests/test_board_import_mapping.py`` argues about §8's mapping without a database.  This file
is the other half, and it exists because a plan that satisfies every assertion there can still be
refused by a `CHECK`, a foreign key or a privilege — which is exactly the class of defect §10 of
``docs/BOARD_STORE.md`` says only execution finds.

Three things it proves that nothing else can:

* the whole plan lands **as ``secretary_app``**.  That role has no `CREATE` (§5.5), so a run that
  commits is the proof the import is DML from first row to last and needs no owner and no DDL.
* the scoped keys of §3.3, §3.4, §3.8 and §3.9 accept the rows the importer builds — including
  the two sprint cursors, which are only satisfiable because they are `DEFERRABLE INITIALLY
  DEFERRED` and the import sets them inside the transaction that inserted the cards.
* a second run on the populated store **refuses**, by name, instead of duplicating the rows whose
  identity PostgreSQL generated.

**There is no skip in this module**, for the reason the schema suite gives: a test that quietly
passes because it never reached a database is worth less than no test at all.  The container is
verification, never delivery; it publishes on loopback, holds no volume and is removed in
``tearDownClass``.  Nothing here reads or writes the live installation or Kanboard.
"""

from __future__ import annotations

import json
import subprocess
import time
import unittest

from secretary.board import import_board, migrate, schema
from secretary.board.import_board import BoardImportError, BoardSource, RegistryEntry, SourceRow
from secretary.board.store import BoardStoreConfig

IMAGE = "postgres:16"
DATABASE = "board_import_test"
OWNER = "secretary_owner"
OWNER_PASSWORD = "throwaway-owner-password"
APP_PASSWORD = "throwaway@app/pass:word"
READ_PASSWORD = "throwaway-read-password"
READY_TIMEOUT_SECONDS = 90

COLUMNS = {1: "Issues", 2: "Ready", 3: "In progress", 6: "Blocked", 7: "Done"}
SWIMLANES = {1: "Default swimlane", 2: "secretary"}
ISSUE_A = "issue:" + "a" * 20
ISSUE_B = "issue:" + "b" * 20


def docker(*arguments: str) -> str:
    completed = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=300, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"docker {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _row(identifier, reference, *, meta, comments=(), **overrides):
    raw = {
        "id": identifier,
        "reference": reference,
        "title": f"title {reference}",
        "description": "description",
        "column_id": 2,
        "swimlane_id": 2,
        "is_active": 1,
        "position": 0,
        "date_creation": 1_700_000_000,
        "date_modification": 1_700_000_500,
    }
    raw.update(overrides)
    return SourceRow(raw=raw, meta=dict(meta), comments=tuple(comments))


def _comment(body: str, created: int = 1_700_000_200) -> dict[str, object]:
    return {"id": created, "date_creation": created, "comment": body, "user_id": 0}


def synthetic_board() -> BoardSource:
    """A small board that exercises every table and every awkward case the live board has."""
    product = _row(
        1,
        "product:secretary",
        column=1,
        meta={
            "record_type": "product",
            "product_id": "secretary",
            "product_projects": '["secretary","secretary-instance"]',
        },
    )
    issues = (
        _row(
            2,
            ISSUE_A,
            column=1,
            meta={
                "record_type": "issue",
                "issue_product": "secretary",
                "issue_kind": "bug",
                "issue_priority": "P1",
            },
            comments=(_comment("[po]\nan issue comment with no table"),),
        ),
        _row(
            3,
            ISSUE_B,
            column=1,
            is_active=0,
            meta={
                "record_type": "issue",
                "issue_product": "secretary",
                "issue_kind": "feature",
                "issue_priority": "P3",
                "issue_closed_reason": "resolved",
            },
        ),
    )
    cards = (
        _row(
            10,
            "secretary-10",
            column=7,
            is_active=0,
            meta={
                "record_type": "task",
                "project": "secretary",
                "task_type": "code",
                "sprint_ref": "sprint:100",
                "claim": "secretary-10-slug",
                "slug": "slug",
                "retry_heads": "claude-opus,codex-high",
                "retry_same": "2",
                "steward_report": "1",
            },
            comments=(
                _comment("[report:done]\nit is done"),
                _comment("[validate:ci-green]\nthe run was green", created=1_700_000_300),
                _comment("plain prose with no marker at all", created=1_700_000_400),
            ),
        ),
        _row(
            11,
            "secretary-11",
            column=6,
            meta={
                "record_type": "task",
                "project": "secretary",
                "task_type": "research",
                "sprint_ref": "sprint:101",
                "blocked_by": "secretary-10",
                "supersedes": "secretary-10",
            },
        ),
        _row(
            12,
            "secretary-12",
            meta={
                "record_type": "task",
                "project": "secretary",
                "task_type": "code",
                "blocked_by": "ghost-1",
                "codex_launch_mode": "exec",
            },
        ),
        # `secretary-583` on the live board, in miniature: the two columns the card carries no
        # metadata for at all.  `project_id` is NULL since 0002 and `task_type` since 0003, and
        # the row records the second silence in `extensions` (§8.6).
        _row(13, "secretary-13", is_active=0, meta={"record_type": "task"}),
    )
    sprints = (
        _row(
            100,
            "sprint:100",
            meta={
                "sprint_goal": "the closed sprint",
                "sprint_definition_of_done": "dod 100",
                "sprint_status": "closed",
                "sprint_product": "secretary",
                "sprint_issues": json.dumps([ISSUE_A, ISSUE_B]),
                "sprint_reservations": '["secretary"]',
                "sprint_repositories": '["/home/dev/secretary","secretary-instance"]',
                "sprint_observer": '{"kind":"head","profile":"claude-observer"}',
                "sprint_worker": "codex-high",
                "sprint_budget": json.dumps({"by_type": {"red_review": 2, "blocked": 1}}),
                "sprint_budget_uncharged": json.dumps({"infrastructure_blocked": 1}),
                "sprint_current_task": "secretary-10",
                "sprint_resume": json.dumps(
                    {
                        "selected_step": "a",
                        "selected_why": "b",
                        "rejected_alternatives": "c",
                        "current_task": "secretary-10",
                        "dod_state": "e",
                        "next_safe_step": "f",
                        "recorded_at": "2026-09-01T00:00:00Z",
                    }
                ),
                "sprint_source_audit": json.dumps(
                    {"created_at": "2026-01-01T00:00:00Z", "updated_at": "", "board": "old"}
                ),
            },
            comments=(_comment("[sprint:resume]\nwhat happened"),),
        ),
        _row(
            101,
            "sprint:101",
            meta={
                "sprint_goal": "the open sprint",
                "sprint_definition_of_done": "dod 101",
                "sprint_status": "open",
                "sprint_reservations": '["secretary-instance"]',
                "sprint_repositories": "[]",
                "sprint_observer": '{"kind":"none"}',
            },
        ),
        # A row since 0002: the reference is the primary key, so an unnumbered one is storable.
        _row(102, "sprint:canary-20260813", meta={"sprint_goal": "a canary"}),
        # The shape of `sprint:1037` on the live board: one reference, a live row and an archived
        # one.  §9 option 1 stores the archived row under a distinguishing reference.
        _row(
            103,
            "sprint:101",
            is_active=0,
            meta={"sprint_goal": "the archived twin", "sprint_definition_of_done": "dod"},
            comments=(_comment("[po]\nthe archived twin's own journal", created=1_700_000_600),),
        ),
    )
    return BoardSource(
        pipeline=(product, *issues, *cards),
        sprints=sprints,
        pipeline_columns=COLUMNS,
        pipeline_swimlanes=SWIMLANES,
        registry=(
            RegistryEntry(
                project_id="secretary",
                repo="/home/dev/secretary",
                remote="https://example.invalid/secretary.git",
                default_branch="main",
                adapter="secretary",
                orca_binding="secretary",
                enabled=True,
                plane="orchestrator",
                curator_roots=("/home/dev/secretary-wt",),
            ),
            RegistryEntry(
                project_id="secretary-instance",
                repo="/home/dev/secretary-instance",
                remote=None,
                default_branch="main",
                adapter=None,
                orca_binding=None,
                enabled=True,
                plane="project",
                curator_roots=(),
            ),
        ),
        budget_records=(
            {
                "kind": "budget_recorded",
                "ref": "sprint:100",
                "request_id": "sprint-budget-evt_1",
                "event_id": "evt_1",
                "occurred_at": "2026-08-01T10:00:00Z",
                "payload": {"event_type": "red_review"},
            },
        ),
        transaction_documents=(
            {
                "version": 1,
                "request_id": "close-sprint-100",
                "kind": "closed",
                "intent": {"reference": "sprint:100"},
                "event": {
                    "ref": "sprint:100",
                    "occurred_at": "2026-09-01T12:00:00Z",
                    "payload": {
                        "decisions": {
                            "issues": [{"ref": ISSUE_A, "verdict": "open", "reason": "still open"}],
                            "cards": [
                                {"ref": "secretary-10", "verdict": "done", "reason": "it landed"}
                            ],
                        }
                    },
                },
            },
        ),
    )


def board_with_a_record_the_schema_cannot_carry() -> BoardSource:
    """The same board plus one card no column can hold: `task_type` outside the vocabulary.

    Refused, named, and never silently dropped — and, since 2026-09-07, never an excuse either:
    the record is on the board and not in the store, so parity is red over it.
    """
    board = synthetic_board()
    refused = _row(
        14,
        "secretary-14",
        meta={"record_type": "task", "project": "secretary", "task_type": "chore"},
        comments=(_comment("[po]\na comment of a card nobody stores", created=1_700_000_700),),
    )
    return BoardSource(
        pipeline=(*board.pipeline, refused),
        sprints=board.sprints,
        pipeline_columns=board.pipeline_columns,
        pipeline_swimlanes=board.pipeline_swimlanes,
        registry=board.registry,
        budget_records=board.budget_records,
        transaction_documents=board.transaction_documents,
    )


class BoardImportIntegrationTests(unittest.TestCase):
    container = ""
    port = 0

    @classmethod
    def setUpClass(cls) -> None:
        for module, why in (
            ("psycopg", "the driver docs/BOARD_STORE.md §5.8 chose"),
            ("sqlalchemy", "the schema's source of truth"),
            ("alembic", "the migration tool"),
        ):
            try:
                __import__(module)
            except ImportError as exc:  # pragma: no cover - a venv without a core dependency
                raise RuntimeError(
                    f"{module} is a core dependency ({why}); reinstall the product "
                    "(`pip install -e .`) rather than skipping the import proof"
                ) from exc
        cls.container = docker(
            "run", "--rm", "-d",
            "-e", f"POSTGRES_DB={DATABASE}",
            "-e", f"POSTGRES_USER={OWNER}",
            "-e", f"POSTGRES_PASSWORD={OWNER_PASSWORD}",
            "-p", "127.0.0.1::5432",
            IMAGE,
        )
        cls.addClassCleanup(lambda: docker("rm", "-f", cls.container))
        published = json.loads(docker("inspect", "-f", "{{json .NetworkSettings.Ports}}", cls.container))
        cls.port = int(published["5432/tcp"][0]["HostPort"])
        cls._await_server()

    @classmethod
    def _await_server(cls) -> None:
        import psycopg

        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        last = ""
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(cls.credentials("owner").conninfo(), connect_timeout=3):
                    return
            except psycopg.Error as exc:
                last = str(exc).strip()
                time.sleep(0.5)
        raise RuntimeError(f"the throwaway {IMAGE} never accepted a connection: {last}")

    @classmethod
    def config(cls, dbname: str = DATABASE) -> BoardStoreConfig:
        return BoardStoreConfig(
            host="127.0.0.1",
            port=cls.port,
            dbname=dbname,
            owner_user=OWNER,
            owner_password=OWNER_PASSWORD,
            app_user=schema.APP_ROLE,
            app_password=APP_PASSWORD,
            read_user=schema.READ_ROLE,
            read_password=READ_PASSWORD,
        )

    @classmethod
    def credentials(cls, role: str, dbname: str = DATABASE):
        return cls.config(dbname).for_role(role)

    def setUp(self) -> None:
        """One empty database and no leftover roles per test: the import runs once, on an empty
        schema, and the revision's `CREATE ROLE` statements are cluster-wide."""
        import psycopg
        import sqlalchemy as sa

        with psycopg.connect(
            self.credentials("owner", "postgres").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
            maintenance.execute(f"DROP ROLE IF EXISTS {schema.APP_ROLE}")
            maintenance.execute(f"DROP ROLE IF EXISTS {schema.READ_ROLE}")
            maintenance.execute(f"CREATE DATABASE {DATABASE} OWNER {OWNER}")
        owner = sa.create_engine(migrate.sqlalchemy_url(self.credentials("owner")))
        self.addCleanup(owner.dispose)
        with owner.connect() as connection:
            migrate.apply(connection, passwords=migrate.passwords_for(self.config()))
        self.source = synthetic_board()
        self.plan = import_board.plan(self.source)

    def engine(self, role: str):
        import sqlalchemy as sa

        engine = sa.create_engine(migrate.sqlalchemy_url(self.credentials(role)))
        self.addCleanup(engine.dispose)
        return engine

    def imported(self, role: str = "app"):
        """Apply the plan as `role` and return the rows PostgreSQL actually holds."""
        with self.engine(role).connect() as connection:
            written = import_board.apply(self.plan, connection)
            return written, import_board.fetch_rows(connection)

    # --- the write --------------------------------------------------------------------

    def test_the_whole_plan_commits_under_the_app_role_alone(self) -> None:
        written, stored = self.imported()
        self.assertEqual(written["tasks"], 4)
        self.assertEqual(len(stored["tasks"]), 4)
        # secretary_app has no CREATE (§5.5), so a committed run is the proof this needs no DDL.
        self.assertEqual(
            {row["task_ref"] for row in stored["tasks"]},
            {"secretary-10", "secretary-11", "secretary-12", "secretary-13"},
        )

    def test_the_archived_card_and_the_closed_issue_are_rows_like_any_other(self) -> None:
        _, stored = self.imported()
        archived = {row["task_ref"]: row["archived"] for row in stored["tasks"]}
        self.assertTrue(archived["secretary-10"])
        closed = {row["issue_id"]: (row["state"], row["close_reason"]) for row in stored["issues"]}
        self.assertEqual(closed[ISSUE_B.removeprefix("issue:")], ("closed", "resolved"))

    def test_a_card_with_no_project_metadata_is_a_row_with_a_null_project(self) -> None:
        """0002 made the column nullable (§8.6); before it, this card was the import's one loss."""
        _, stored = self.imported()
        by_ref = {row["task_ref"]: row for row in stored["tasks"]}
        self.assertIsNone(by_ref["secretary-13"]["project_id"])
        self.assertIsNotNone(by_ref["secretary-13"]["task_number"])

    def test_a_card_the_board_never_gave_a_type_is_a_row_that_says_so(self) -> None:
        """0003 made the column nullable (§8.6); before it, this card was the import's one loss."""
        _, stored = self.imported()
        by_ref = {row["task_ref"]: row for row in stored["tasks"]}
        self.assertIsNone(by_ref["secretary-13"]["task_type"])
        self.assertEqual(
            by_ref["secretary-13"]["extensions"],
            # The two halves are namespaced: the card's lane disagrees with its (absent) product's,
            # so §8.2's provenance bag is there too and neither hides the other.
            {"kanboard": {"swimlane": "secretary"}, "board_never_named": ["task_type"]},
        )
        (named,) = self.plan.report.fields_the_board_never_named
        self.assertEqual((named["field"], named["ref"]), ("tasks.task_type", "secretary-13"))

    def test_a_dependency_on_a_card_the_board_does_not_hold_is_a_row(self) -> None:
        _, stored = self.imported()
        by_ref = {row["task_ref"]: row for row in stored["task_dependencies"]}
        self.assertEqual(by_ref["secretary-12"]["depends_on"], "ghost-1")
        self.assertIsNone(by_ref["secretary-12"]["depends_on_task"])
        self.assertEqual(by_ref["secretary-11"]["depends_on_task"], "secretary-10")

    def test_the_two_deferred_sprint_cursors_land_in_the_same_transaction_as_their_targets(self) -> None:
        _, stored = self.imported()
        sprints = {row["ref"]: row for row in stored["sprints"]}
        self.assertEqual(sprints["sprint:100"]["current_task_ref"], "secretary-10")
        self.assertIsNotNone(sprints["sprint:100"]["resume_id"])
        resumes = {row["resume_id"]: row for row in stored["sprint_resumes"]}
        self.assertEqual(resumes[sprints["sprint:100"]["resume_id"]]["sprint_ref"], "sprint:100")

    def test_an_unnumbered_sprint_reference_is_a_row_that_holds_no_number(self) -> None:
        _, stored = self.imported()
        sprints = {row["ref"]: row for row in stored["sprints"]}
        self.assertIsNone(sprints["sprint:canary-20260813"]["sprint_number"])
        self.assertEqual(sprints["sprint:100"]["sprint_number"], 100)

    def test_the_archived_twin_of_a_reference_keeps_its_record_and_its_provenance(self) -> None:
        """§9 option 1, executed: two rows, two references, one original spelling kept."""
        _, stored = self.imported()
        sprints = {row["ref"]: row for row in stored["sprints"]}
        twin = sprints["sprint:101-archived-103"]
        self.assertIsNone(twin["sprint_number"])
        self.assertEqual(
            twin["source_audit"][import_board.SOURCE_AUDIT_ORIGINAL_REF], "sprint:101"
        )
        self.assertEqual(
            [row["body"] for row in stored["sprint_comments"] if row["sprint_ref"] == twin["ref"]],
            ["the archived twin's own journal"],
        )
        (named,) = self.plan.report.disambiguated_references
        self.assertEqual(named["stored_ref"], "sprint:101-archived-103")

    def test_the_generated_ref_columns_carry_the_boards_own_spellings(self) -> None:
        self.imported()
        with self.engine("read").connect() as connection:
            self.assertEqual(
                sorted(value for (value,) in connection.exec_driver_sql("SELECT ref FROM sprints")),
                [
                    "sprint:100",
                    "sprint:101",
                    "sprint:101-archived-103",
                    "sprint:canary-20260813",
                ],
            )
            self.assertEqual(
                sorted(value for (value,) in connection.exec_driver_sql("SELECT ref FROM issues")),
                sorted([ISSUE_A, ISSUE_B]),
            )
            self.assertEqual(
                [value for (value,) in connection.exec_driver_sql("SELECT ref FROM products")],
                ["product:secretary"],
            )
            # `issue_comments.issue_ref` is generated too, and is what its claim key joins on.
            self.assertEqual(
                sorted(
                    value
                    for (value,) in connection.exec_driver_sql("SELECT issue_ref FROM issue_comments")
                ),
                [ISSUE_A],
            )

    def test_the_budget_counters_became_rows_that_claim_their_requests(self) -> None:
        _, stored = self.imported()
        self.assertEqual(len(stored["sprint_budget_events"]), 4)
        charged = [row for row in stored["sprint_budget_events"] if row["charged"]]
        self.assertEqual(len(charged), 3)
        claimed = {row["request_id"] for row in stored["requests"]}
        self.assertTrue({row["request_id"] for row in stored["sprint_budget_events"]} <= claimed)
        self.assertIn("sprint-budget-evt_1", claimed)

    def test_the_recovered_close_decisions_satisfy_both_scoped_keys(self) -> None:
        _, stored = self.imported()
        subjects = {row["subject_kind"]: row for row in stored["sprint_decisions"]}
        self.assertEqual(set(subjects), {"issue", "card"})
        self.assertEqual(subjects["card"]["task_ref"], "secretary-10")
        self.assertEqual(subjects["issue"]["issue_id"], ISSUE_A.removeprefix("issue:"))

    def test_comments_keep_their_marker_and_their_prose_apart(self) -> None:
        _, stored = self.imported()
        by_body = {row["body"]: row for row in stored["task_comments"]}
        self.assertEqual(by_body["it is done"]["marker"], "report:done")
        # §8.1 knows `validate:*` since 2026-09-07, on the live board's own evidence.
        self.assertEqual(by_body["the run was green"]["marker"], "validate:ci-green")
        self.assertIsNone(by_body["plain prose with no marker at all"]["marker"])
        self.assertEqual(
            sorted(row["body"] for row in stored["sprint_comments"]),
            ["the archived twin's own journal", "what happened"],
        )

    def test_a_comment_on_an_issue_is_a_row_in_the_table_0002_added(self) -> None:
        """479 comments on live Issue rows had no table; this is the one that holds them."""
        _, stored = self.imported()
        (issue_comment,) = stored["issue_comments"]
        self.assertEqual(issue_comment["issue_id"], ISSUE_A.removeprefix("issue:"))
        self.assertEqual(issue_comment["marker"], "po")
        self.assertEqual(issue_comment["body"], "an issue comment with no table")

    def test_an_issue_keeps_the_metadata_keys_the_schema_does_not_name(self) -> None:
        _, stored = self.imported()
        by_id = {row["issue_id"]: row for row in stored["issues"]}
        self.assertEqual(by_id[ISSUE_B.removeprefix("issue:")]["extensions"], {})
        counted = {item["where"] for item in self.plan.report.extensions_keys}
        self.assertIn("tasks.extensions.kanboard", counted)

    def test_the_released_reservation_is_history_and_the_live_one_is_unique(self) -> None:
        _, stored = self.imported()
        held = {row["project_id"]: row for row in stored["sprint_projects"]}
        self.assertFalse(held["secretary"]["reserved"])
        self.assertIsNotNone(held["secretary"]["released_at"])
        self.assertTrue(held["secretary-instance"]["reserved"])

    def test_a_repository_no_registry_entry_claims_is_stored_with_a_null_project(self) -> None:
        _, stored = self.imported()
        by_path = {row["path"]: row for row in stored["repositories"]}
        self.assertIsNone(by_path["secretary-instance"]["project_id"])
        self.assertEqual(by_path["/home/dev/secretary"]["role"], "primary")
        self.assertEqual(by_path["/home/dev/secretary-wt"]["role"], "curator_root")
        self.assertEqual(
            {(row["sprint_ref"], row["path"]) for row in stored["sprint_repositories"]},
            {("sprint:100", "/home/dev/secretary"), ("sprint:100", "secretary-instance")},
        )

    def test_task_issues_is_empty_and_the_report_says_why(self) -> None:
        _, stored = self.imported()
        self.assertEqual(stored["task_issues"], [])
        explained = {item["table"]: item["reason"] for item in self.plan.report.expected_zero}
        self.assertIn("no source field exists", explained["task_issues"])

    # --- parity -----------------------------------------------------------------------

    def test_parity_is_green_only_when_the_whole_board_is_in_the_database(self) -> None:
        _, stored = self.imported()
        checks = import_board.parity(self.source, stored, self.plan.report)
        failures = [
            check
            for axis in import_board.PARITY_AXES
            for check in checks[axis]
            if not check["ok"]
        ]
        self.assertEqual(failures, [], "parity must hold against the database, not against the plan")
        self.assertEqual(checks["records_missing"], [])
        self.assertTrue(checks["ok"])

    def test_a_refused_card_makes_parity_red_over_the_real_database(self) -> None:
        """A record the schema cannot carry is named, with a reason — and is still not a pass.

        This is the reviewer's first finding, executed: the run that refused 479 comments returned
        PASS because each axis subtracted the report's own refusals from what it expected.  A
        green parity is the claim "the whole board is in the store", and here it is not.
        """
        board = board_with_a_record_the_schema_cannot_carry()
        plan = import_board.plan(board)
        with self.engine("app").connect() as connection:
            import_board.apply(plan, connection)
            stored = import_board.fetch_rows(connection)
        self.assertIn(
            "secretary-14", {item["ref"] for item in plan.report.records_not_imported}
        )
        checks = import_board.parity(board, stored, plan.report)
        self.assertFalse(checks["ok"])
        # Named one line each, with the reason the plan gave — the card and its comment both.
        self.assertEqual(
            sorted((item["kind"], item["id"]) for item in checks["records_missing"]),
            [
                ("card", "secretary-14"),
                ("card comment", "secretary-14#comment-1700000700"),
            ],
        )
        # And the accounting axis is green, because both were named rather than lost silently.
        self.assertTrue(all(check["ok"] for check in checks["accounting"]))

    def test_parity_notices_a_row_that_vanished_between_the_plan_and_the_database(self) -> None:
        with self.engine("app").connect() as connection:
            import_board.apply(self.plan, connection)
            connection.rollback()
            with connection.begin():
                connection.exec_driver_sql("DELETE FROM tasks WHERE task_ref = 'secretary-12'")
            stored = import_board.fetch_rows(connection)
        checks = import_board.parity(self.source, stored, self.plan.report)
        self.assertFalse(checks["ok"])
        self.assertTrue(any(not check["ok"] for check in checks["identifiers"]))
        self.assertIn(
            ("card", "secretary-12"),
            {(item["kind"], item["id"]) for item in checks["records_missing"]},
        )

    def test_a_comment_deleted_from_the_database_is_named_by_its_own_identifier(self) -> None:
        """DoD 2 against PostgreSQL: one line per comment, not a count of them."""
        with self.engine("app").connect() as connection:
            import_board.apply(self.plan, connection)
            connection.rollback()
            with connection.begin():
                connection.exec_driver_sql("DELETE FROM issue_comments")
                connection.exec_driver_sql(
                    "DELETE FROM sprint_comments WHERE body = 'what happened'"
                )
            stored = import_board.fetch_rows(connection)
        checks = import_board.parity(self.source, stored, self.plan.report)
        self.assertFalse(checks["ok"])
        named = {(item["kind"], item["id"]) for item in checks["records_missing"]}
        self.assertEqual(
            named,
            {
                ("issue comment", f"{ISSUE_A}#comment-1700000200"),
                ("sprint comment", "sprint:100#comment-1700000200"),
            },
        )
        self.assertTrue(any(not check["ok"] for check in checks["accounting"]))

    def test_an_altered_sprint_comment_body_in_the_database_fails_the_content_axis(self) -> None:
        """The reviewer's finding of 2026-09-07, executed against PostgreSQL rather than a dict."""
        stored = self._altered("sprint_comments", "what happened")
        checks = import_board.parity(self.source, stored, self.plan.report)
        self.assertFalse(checks["ok"])
        (failed,) = [check for check in checks["content"] if not check["ok"]]
        self.assertIn("sprint_comments", failed["name"])

    def test_an_altered_issue_comment_body_in_the_database_fails_the_content_axis(self) -> None:
        stored = self._altered("issue_comments", "an issue comment with no table")
        checks = import_board.parity(self.source, stored, self.plan.report)
        self.assertFalse(checks["ok"])
        (failed,) = [check for check in checks["content"] if not check["ok"]]
        self.assertIn("issue_comments", failed["name"])

    def test_an_altered_card_comment_body_in_the_database_fails_the_content_axis(self) -> None:
        stored = self._altered("task_comments", "it is done")
        checks = import_board.parity(self.source, stored, self.plan.report)
        self.assertFalse(checks["ok"])
        (failed,) = [check for check in checks["content"] if not check["ok"]]
        self.assertIn("task_comments", failed["name"])

    def _altered(self, table: str, body: str):
        """Import, then rewrite one stored comment body from its original to `altered`."""
        import sqlalchemy as sa

        with self.engine("app").connect() as connection:
            import_board.apply(self.plan, connection)
            connection.rollback()
            with connection.begin():
                changed = connection.execute(
                    sa.text(f"UPDATE {table} SET body = 'altered' WHERE body = :body"),
                    {"body": body},
                ).rowcount
            self.assertEqual(changed, 1, f"{table} had no row spelling {body!r}")
            return import_board.fetch_rows(connection)

    # --- running it twice --------------------------------------------------------------

    def test_a_second_run_refuses_by_name_instead_of_duplicating_generated_identities(self) -> None:
        with self.engine("app").connect() as connection:
            import_board.apply(self.plan, connection)
            with self.assertRaises(BoardImportError) as refusal:
                import_board.apply(import_board.plan(self.source), connection)
            self.assertIn("runs exactly once on an empty schema", str(refusal.exception))
            self.assertIn("tasks", str(refusal.exception))
            connection.rollback()
            stored = import_board.fetch_rows(connection)
        self.assertEqual(len(stored["tasks"]), 4)
        self.assertEqual(len(stored["task_comments"]), 3)
        self.assertEqual(len(stored["issue_comments"]), 1)

    def test_a_fresh_database_after_migrate_reproduces_the_same_import(self) -> None:
        _, first = self.imported()
        self.setUp()
        _, second = self.imported()
        for table in import_board.TABLE_ORDER:
            with self.subTest(table=table):
                self.assertEqual(len(first[table]), len(second[table]))
        self.assertEqual(
            sorted(row["task_ref"] for row in first["tasks"]),
            sorted(row["task_ref"] for row in second["tasks"]),
        )

    # --- the store this card does not touch --------------------------------------------

    def test_the_schema_revision_is_asserted_before_anything_is_written(self) -> None:
        with self.engine("app").connect() as connection:
            self.assertEqual(migrate.assert_schema_revision(connection), "0006_sprint_transport_key")

    def test_the_read_role_can_query_the_imported_board_and_cannot_write_it(self) -> None:
        import sqlalchemy as sa

        self.imported()
        with self.engine("read").connect() as connection:
            (cards,) = connection.exec_driver_sql("SELECT count(*) FROM tasks").fetchone()
            self.assertEqual(cards, 4)
            with self.assertRaises(sa.exc.ProgrammingError):
                connection.exec_driver_sql("DELETE FROM tasks")
