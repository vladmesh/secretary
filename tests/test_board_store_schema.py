"""The initial revision against a real PostgreSQL, in a throwaway container.

§10 of ``docs/BOARD_STORE.md`` is the reason this file exists: two earlier revisions of that
document each shipped a statement PostgreSQL refuses, and both were found by executing them, not
by reading them. The same applies to the schema's transcription into SQLAlchemy models, so the
Alembic revision is executed here and the result is counted against the numbers the document's own
run produced. Four revisions ship now, and §3.13 records the numbers of each: `0001_initial` built 22 tables,
34 `CHECK`, 36 foreign-key, 22 primary-key and 12 unique constraints and 4 partial unique indexes;
`0002_board_gaps`, which closes the gaps the first import of real data found, makes that 23, 37,
38, 23, 13 and 4; `0003_task_type_optional` leaves every one of those six numbers alone; and
`0005_sprint_sql` makes the head counts 24, 37, 40, 24, 15 and 4. The last table and
the last primary key are Alembic's `alembic_version`, which since the owner's decision of
2026-09-07 stands where §7.4's `schema_migrations` stood.

The counting is not the strongest thing here. `test_the_migrated_database_still_matches_the_models`
asks Alembic to autogenerate a diff between the database this revision built and the models, and
requires it to be empty: the models are the schema, and a revision that drifts from them is a red
test rather than a surprise on the next installation.

**There is no skip in this module.** A missing Docker, a missing dependency or a container that
never becomes ready is an error, not an absence: a schema test that quietly passes because it never
reached a database is worth less than no test at all. The suite is `integration-board`, so this
runs in the `test / integration-board` job of the exact-SHA gate, where the dependencies (core
since this card) and Docker are both present.

The container is verification, never delivery. It publishes on loopback, holds no volume, is
removed in `tearDownClass`, and nothing here reads or writes the live installation.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import unittest
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory

from secretary import upgrade
from secretary.board import migrate, schema
from secretary.board.backend import record_key
from secretary.board.store import BoardStoreConfig, BoardStoreError

IMAGE = "postgres:16"
DATABASE = "board_store_test"
OWNER = "secretary_owner"
OWNER_PASSWORD = "throwaway-owner-password"
#: Deliberately awkward: it carries the three characters that break a URL, a `text()` construct
#: and a naive SQL literal respectively.
APP_PASSWORD = "throwaway@app/pass:word"
READ_PASSWORD = "throwaway-read-password"
READY_TIMEOUT_SECONDS = 90

COUNTS = """
SELECT
  (SELECT count(*) FROM information_schema.tables
     WHERE table_schema = 'public' AND table_type = 'BASE TABLE'),
  (SELECT count(*) FROM pg_constraint WHERE contype = 'c' AND connamespace = 'public'::regnamespace),
  (SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND connamespace = 'public'::regnamespace),
  (SELECT count(*) FROM pg_constraint WHERE contype = 'p' AND connamespace = 'public'::regnamespace),
  (SELECT count(*) FROM pg_constraint WHERE contype = 'u' AND connamespace = 'public'::regnamespace),
  (SELECT count(*) FROM pg_index i
     JOIN pg_class c ON c.oid = i.indexrelid
     JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND i.indisunique AND i.indpred IS NOT NULL)
"""

#: What the same schema makes of a `postgres:16` at the head revision, and what §3.13 records
#: beside `0001`'s own numbers. A disagreement here is a defect of the transcription into models,
#: not of the document. Unchanged by `0003`, which trades one `CHECK` for one `CHECK`.
DOCUMENTED_COUNTS = (24, 37, 40, 24, 15, 4)

#: Every revision this build ships, oldest first: what an empty database owes.
REVISIONS = (
    "0001_initial",
    "0002_board_gaps",
    "0003_task_type_optional",
    "0004_product_issue_sql",
    "0005_sprint_sql",
)


def docker(*arguments: str) -> str:
    completed = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=180, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"docker {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


class BoardStoreSchemaTests(unittest.TestCase):
    container = ""
    port = 0

    @classmethod
    def setUpClass(cls) -> None:
        for module, why in (
            ("psycopg", "the driver docs/BOARD_STORE.md §5.8 chose"),
            ("sqlalchemy", "the schema's source of truth since 2026-09-07"),
            ("alembic", "the migration tool since 2026-09-07"),
        ):
            try:
                __import__(module)
            except ImportError as exc:  # pragma: no cover - a venv without a core dependency
                raise RuntimeError(
                    f"{module} is a core dependency ({why}); reinstall the product "
                    "(`pip install -e .`) rather than skipping the schema proof"
                ) from exc
        cls.container = docker(
            "run",
            "--rm",
            "-d",
            "-e",
            f"POSTGRES_DB={DATABASE}",
            "-e",
            f"POSTGRES_USER={OWNER}",
            "-e",
            f"POSTGRES_PASSWORD={OWNER_PASSWORD}",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        )
        cls.addClassCleanup(lambda: docker("rm", "-f", cls.container))
        published = json.loads(docker("inspect", "-f", "{{json .NetworkSettings.Ports}}", cls.container))
        cls.port = int(published["5432/tcp"][0]["HostPort"])
        cls._await_server()

    @classmethod
    def _await_server(cls) -> None:
        """Wait for the cluster the volume keeps, not the bootstrap one the entrypoint stops.

        `pg_isready` answers yes to the temporary initialisation server, which then shuts down;
        a real connection to the published port is the only readiness signal that cannot lie.
        """
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
        """One empty database and no leftover roles per test: the initial revision only ever runs
        on an empty database, and its `CREATE ROLE` statements are cluster-wide."""
        import psycopg

        with psycopg.connect(
            self.credentials("owner", "postgres").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
            maintenance.execute(f"DROP ROLE IF EXISTS {schema.APP_ROLE}")
            maintenance.execute(f"DROP ROLE IF EXISTS {schema.READ_ROLE}")
            maintenance.execute(f"CREATE DATABASE {DATABASE} OWNER {OWNER}")

    def engine(self, role: str = "owner"):
        import sqlalchemy as sa

        engine = sa.create_engine(migrate.sqlalchemy_url(self.credentials(role)))
        self.addCleanup(engine.dispose)
        return engine

    def owner_connection(self):
        connection = self.engine().connect()
        self.addCleanup(connection.close)
        return connection

    @property
    def passwords(self) -> dict[str, str]:
        return migrate.passwords_for(self.config())

    def run_migrations(self, connection, **kwargs) -> tuple[str, ...]:
        return migrate.apply(connection, passwords=self.passwords, **kwargs)

    def counts(self, connection) -> tuple[int, ...]:
        return tuple(connection.exec_driver_sql(COUNTS).fetchone())

    # --- the schema itself -------------------------------------------------------------

    def test_the_revisions_reproduce_the_numbers_the_document_records(self) -> None:
        connection = self.owner_connection()

        self.assertEqual(self.run_migrations(connection), REVISIONS)

        self.assertEqual(
            self.counts(connection),
            DOCUMENTED_COUNTS,
            "tables, CHECK, FK, PK, UNIQUE and partial unique indexes must match §3.13's numbers",
        )

    def test_the_migrated_database_still_matches_the_models(self) -> None:
        """The models are the schema, so a revision that drifts from them is a defect here.

        This is Alembic's own autogenerate comparison, run backwards: anything it would emit to
        make the database match `board.schema` is a difference the revision failed to build.
        """
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        connection = self.owner_connection()
        self.run_migrations(connection)

        with warnings.catch_warnings():
            # A persisted generated column cannot be altered, which autogenerate says out loud
            # every time it compares one; it is not a difference.
            warnings.filterwarnings("ignore", message="Computed default on")
            context = MigrationContext.configure(
                connection, opts={"compare_type": True, "compare_server_default": True}
            )
            difference = compare_metadata(context, schema.metadata)

        self.assertEqual(difference, [], "the built schema and the models disagree")

    def test_section_9s_sprint_number_sequence_exists(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)

        found = connection.exec_driver_sql(
            "SELECT count(*) FROM pg_sequences WHERE schemaname='public' AND sequencename=%s",
            ("sprint_number_seq",),
        ).fetchone()[0]

        self.assertEqual(found, 1)

    def test_the_version_lives_in_alembics_table_and_nowhere_else(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)

        stamped = connection.exec_driver_sql("SELECT version_num FROM alembic_version").fetchall()

        self.assertEqual(stamped, [(REVISIONS[-1],)])
        self.assertIsNone(
            connection.exec_driver_sql("SELECT to_regclass('public.schema_migrations')").fetchone()[0],
            "the hand-rolled version table is gone; Alembic's is the version",
        )
        self.assertEqual(migrate.assert_schema_revision(connection), migrate.head_revision())

    def test_the_version_assertion_refuses_a_schema_this_build_does_not_speak(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)

        with self.assertRaisesRegex(BoardStoreError, "refusing to write"):
            migrate.assert_schema_revision(connection, expected="0004_something_later")

    def test_a_second_run_applies_nothing_and_leaves_the_schema_alone(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)
        before = self.counts(connection)

        self.assertEqual(self.run_migrations(connection), ())

        self.assertEqual(self.counts(connection), before)
        self.assertEqual(
            connection.exec_driver_sql("SELECT count(*) FROM alembic_version").fetchone()[0], 1
        )

    def test_a_dry_run_reads_the_version_and_writes_nothing(self) -> None:
        connection = self.owner_connection()

        owed = self.run_migrations(connection, dry_run=True)

        self.assertEqual(owed, REVISIONS)
        self.assertIsNone(
            connection.exec_driver_sql("SELECT to_regclass('public.products')").fetchone()[0]
        )
        self.assertIsNone(
            connection.exec_driver_sql("SELECT to_regclass('public.alembic_version')").fetchone()[0]
        )

    def test_a_failing_revision_leaves_no_half_applied_schema(self) -> None:
        """PostgreSQL's transactional DDL, which is why §7.4 needs no down migration.

        The failure is a real one rather than a fabricated statement: §5.5's `CREATE ROLE` is the
        last thing the initial revision does, so a cluster that already has `secretary_app` fails it
        after all 21 tables have been created. Nothing may survive that.
        """
        import psycopg

        with psycopg.connect(
            self.credentials("owner", "postgres").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"CREATE ROLE {schema.APP_ROLE}")
        self.addCleanup(self._drop_roles)
        connection = self.owner_connection()

        with self.assertRaises(Exception):  # noqa: B017 - whatever the server raises, nothing survives
            self.run_migrations(connection)

        connection.rollback()
        self.assertIsNone(
            connection.exec_driver_sql("SELECT to_regclass('public.products')").fetchone()[0],
            "a failed revision must leave the schema it was moving from",
        )

    def _drop_roles(self) -> None:
        import psycopg

        with psycopg.connect(
            self.credentials("owner", "postgres").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"DROP ROLE IF EXISTS {schema.APP_ROLE}")

    def test_the_revision_refuses_to_run_without_the_generated_passwords(self) -> None:
        """§5.5's two passwords are parameters of the run, and an absent one is not a default."""
        connection = self.owner_connection()

        with self.assertRaisesRegex(Exception, "app_password"):
            migrate.apply(connection, passwords={})

        connection.rollback()

    # --- the records `secretary-1583` found the schema could not carry ------------------
    #
    # Each of these is a row the live board holds today and `0001_initial` refused. They are
    # written here as inserts rather than as a reading of the model, because "the schema can
    # represent it" is only true if PostgreSQL accepts it.

    def prepared(self):
        """A migrated database carrying the few rows the cases below reference."""
        connection = self.owner_connection()
        self.run_migrations(connection)
        connection.exec_driver_sql(
            "INSERT INTO products (product_id, board_key, title, created_at, updated_at) "
            "VALUES ('secretary', %s, 'Secretary', now(), now())",
            (record_key("product", "secretary"),),
        )
        connection.exec_driver_sql(
            "INSERT INTO projects (project_id) VALUES ('secretary')"
        )
        connection.exec_driver_sql(
            "INSERT INTO issues (issue_id, board_key, product_id, title, issue_kind, priority, "
            "created_at, updated_at) VALUES ('2fdac531', %s, 'secretary', 'An issue', 'bug', "
            "'P1', now(), now())",
            (record_key("issue", "2fdac531"),),
        )
        return connection

    def sprint(self, connection, ref: str, number: int | None) -> None:
        connection.exec_driver_sql(
            "INSERT INTO sprints (ref, sprint_number, goal, definition_of_done, created_at, "
            "updated_at) VALUES (%s, %s, 'g', 'd', now(), now())",
            (ref, number),
        )

    def card(
        self,
        connection,
        ref: str,
        *,
        project: str | None = "secretary",
        sprint=None,
        task_type: str | None = "code",
        extensions: str = "{}",
    ) -> None:
        connection.exec_driver_sql(
            "INSERT INTO tasks (task_ref, project_id, task_number, title, task_type, state, "
            "sprint_ref, extensions, created_at, updated_at) VALUES (%s, %s, %s, 'A card', %s, "
            "'ready', %s, %s, now(), now())",
            (ref, project, int(ref.rsplit("-", 1)[1]), task_type, sprint, extensions),
        )

    def test_issue_and_product_comments_have_entity_scoped_tables(self) -> None:
        """Product writes now need the same durable comment shape already used by Issues."""
        connection = self.prepared()

        connection.exec_driver_sql(
            "INSERT INTO issue_comments (issue_id, marker, body, actor_role, created_at) "
            "VALUES ('2fdac531', 'issue:closed', 'closed as resolved', 'po', now())"
        )

        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT issue_id, marker, body, issue_ref FROM issue_comments"
            ).fetchall(),
            [("2fdac531", "issue:closed", "closed as resolved", "issue:2fdac531")],
        )
        connection.exec_driver_sql(
            "INSERT INTO product_comments (product_id, marker, body, actor_role, created_at) "
            "VALUES ('secretary', 'product:note', 'product note', 'po', now())"
        )
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT product_id, marker, body, product_ref FROM product_comments"
            ).fetchall(),
            [("secretary", "product:note", "product note", "product:secretary")],
        )

    def test_an_issue_keeps_the_metadata_keys_the_model_does_not_name(self) -> None:
        """AC 2: nine metadata keys ride on 158 Issue rows, and a lane that is not the product's."""
        connection = self.prepared()

        connection.exec_driver_sql(
            "UPDATE issues SET extensions = %s::jsonb WHERE issue_id = '2fdac531'",
            ('{"kanboard": {"slug": "an-issue", "swimlane": "Codegen"}}',),
        )

        stored, default = connection.exec_driver_sql(
            "SELECT (SELECT extensions FROM issues), "
            "(SELECT column_default FROM information_schema.columns "
            " WHERE table_name = 'issues' AND column_name = 'extensions')"
        ).fetchone()
        self.assertEqual(stored["kanboard"]["swimlane"], "Codegen")
        self.assertIn("'{}'::jsonb", default)

    def test_a_sprint_reference_that_carries_no_number_is_a_row_and_keeps_its_cards(self) -> None:
        """AC 3: both canary sprints, and the two cards that lost their link to them."""
        connection = self.prepared()

        self.sprint(connection, "sprint:canary-terra-20260813", None)
        self.sprint(connection, "sprint:canary-terra-final-20260813", None)
        self.sprint(connection, "sprint:1037", 1037)
        self.card(connection, "secretary-1438", sprint="sprint:canary-terra-20260813")
        self.card(connection, "secretary-1439", sprint="sprint:canary-terra-final-20260813")

        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT task_ref, sprint_ref FROM tasks ORDER BY task_ref"
            ).fetchall(),
            [
                ("secretary-1438", "sprint:canary-terra-20260813"),
                ("secretary-1439", "sprint:canary-terra-final-20260813"),
            ],
        )
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT ref FROM sprints WHERE sprint_number IS NULL ORDER BY ref"
            ).fetchall(),
            [("sprint:canary-terra-20260813",), ("sprint:canary-terra-final-20260813",)],
        )

    def test_a_product_keeps_metadata_the_relational_model_does_not_name(self) -> None:
        connection = self.prepared()
        connection.exec_driver_sql(
            "UPDATE products SET extensions = %s::jsonb WHERE product_id = 'secretary'",
            ('{"kanboard": {"future_product_field": "kept"}}',),
        )
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT extensions->'kanboard'->>'future_product_field' FROM products "
                "WHERE product_id = 'secretary'"
            ).fetchone()[0],
            "kept",
        )

    def test_the_number_and_the_reference_may_not_disagree(self) -> None:
        """§9's spelling, as a constraint: a numbered reference carries exactly its number."""
        import sqlalchemy as sa

        connection = self.prepared()

        for ref, number in (("sprint:1037", 42), ("sprint:1037", None), ("sprint:x", 7)):
            with self.subTest(ref=ref, number=number):
                with self.assertRaises(sa.exc.IntegrityError):
                    self.sprint(connection, ref, number)
                connection.rollback()

    def test_one_reference_is_still_one_sprint(self) -> None:
        """The other half of §9: `sprints.ref` is the key, so a duplicate is refused, not stored.

        This is the finding the report names — `sprint:1037` is on the board twice, a live row and
        an archived one — recorded here as what the schema actually does with it.
        """
        import sqlalchemy as sa

        connection = self.prepared()
        self.sprint(connection, "sprint:1037", 1037)

        with self.assertRaises(sa.exc.IntegrityError):
            self.sprint(connection, "sprint:1037", 1037)
        connection.rollback()

    def test_the_sprint_cursor_is_still_scoped_to_its_own_sprint(self) -> None:
        """AC 3: the composite keys move to the reference without losing their reach."""
        import sqlalchemy as sa

        connection = self.prepared()
        self.sprint(connection, "sprint:1", 1)
        self.sprint(connection, "sprint:2", 2)
        self.card(connection, "secretary-1", sprint="sprint:2")
        connection.commit()

        with self.assertRaises(sa.exc.IntegrityError):
            connection.exec_driver_sql(
                "UPDATE sprints SET current_task_ref = 'secretary-1' WHERE ref = 'sprint:1'"
            )
            connection.commit()
        connection.rollback()

        connection.exec_driver_sql(
            "UPDATE sprints SET current_task_ref = 'secretary-1' WHERE ref = 'sprint:2'"
        )
        connection.commit()
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT current_task_ref FROM sprints WHERE ref = 'sprint:2'"
            ).fetchone()[0],
            "secretary-1",
        )

    def test_section_9s_allocator_still_hands_out_numbers(self) -> None:
        connection = self.prepared()

        connection.exec_driver_sql("SELECT setval('sprint_number_seq', 1037)")
        number = connection.exec_driver_sql("SELECT nextval('sprint_number_seq')").fetchone()[0]
        self.sprint(connection, f"sprint:{number}", number)

        self.assertEqual(number, 1038)
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT ref FROM sprints WHERE sprint_number = 1038"
            ).fetchone()[0],
            "sprint:1038",
        )

    def test_a_card_whose_metadata_names_no_project_is_still_a_row(self) -> None:
        """AC 5: `secretary-583` carries no `project`, and the board holds it anyway."""
        connection = self.prepared()

        self.card(connection, "secretary-583", project=None)

        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT project_id FROM tasks WHERE task_ref = 'secretary-583'"
            ).fetchone()[0],
            None,
        )

    def test_a_card_the_board_never_gave_a_type_is_still_a_row(self) -> None:
        """AC 1: `secretary-583` carries no `task_type` either, and 0003 keeps it (§8.6)."""
        import sqlalchemy as sa

        connection = self.prepared()

        self.card(
            connection,
            "secretary-583",
            project=None,
            task_type=None,
            extensions='{"board_never_named": ["task_type"]}',
        )
        connection.commit()

        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT task_type, extensions FROM tasks WHERE task_ref = 'secretary-583'"
            ).fetchone(),
            (None, {"board_never_named": ["task_type"]}),
        )
        # The vocabulary is still closed: NULL was added to what the column admits, not "any text".
        with self.assertRaises(sa.exc.IntegrityError):
            self.card(connection, "secretary-584", task_type="chore")
        connection.rollback()

    def test_a_dependency_on_a_card_the_board_does_not_hold_is_kept(self) -> None:
        """AC 5: nine `blocked_by` values name cards that are not on this board."""
        import sqlalchemy as sa

        connection = self.prepared()
        self.card(connection, "secretary-1584")
        self.card(connection, "secretary-1583")

        connection.exec_driver_sql(
            "INSERT INTO task_dependencies (task_ref, depends_on, depends_on_task) VALUES "
            "('secretary-1584', 'memory-mcp-12', NULL), "
            "('secretary-1584', 'secretary-1583', 'secretary-1583')"
        )
        connection.commit()

        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT depends_on, depends_on_task FROM task_dependencies ORDER BY depends_on"
            ).fetchall(),
            [("memory-mcp-12", None), ("secretary-1583", "secretary-1583")],
        )
        # The foreign key still means what it meant: a resolution that names another card, or a
        # card that is not there, is refused rather than silently kept.
        for depends_on, resolved in (
            ("secretary-1583", "secretary-1582"),
            ("triggered-agents-9", "triggered-agents-9"),
        ):
            with self.subTest(depends_on=depends_on):
                with self.assertRaises(sa.exc.IntegrityError):
                    connection.exec_driver_sql(
                        "INSERT INTO task_dependencies (task_ref, depends_on, depends_on_task) "
                        "VALUES ('secretary-1583', %s, %s)",
                        (depends_on, resolved),
                    )
                connection.rollback()

    # --- §7.4's lock -------------------------------------------------------------------

    def test_the_advisory_lock_makes_a_second_runner_wait_for_the_first(self) -> None:
        import psycopg

        holder = psycopg.connect(self.credentials("owner").conninfo(), autocommit=True)
        self.addCleanup(holder.close)
        holder.execute("SELECT pg_advisory_lock(%s)", (migrate.ADVISORY_LOCK_KEY,))
        connection = self.owner_connection()
        finished = threading.Event()

        def run() -> None:
            self.run_migrations(connection)
            finished.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertFalse(
                finished.wait(1.5),
                "the run must contend on the fixed advisory key, not migrate concurrently",
            )
        finally:
            holder.execute("SELECT pg_advisory_unlock(%s)", (migrate.ADVISORY_LOCK_KEY,))
        self.assertTrue(finished.wait(60), "the runner never acquired the released lock")
        worker.join(timeout=5)
        self.assertEqual(
            connection.exec_driver_sql("SELECT count(*) FROM alembic_version").fetchone()[0], 1
        )

    def test_the_lock_is_released_once_the_run_is_done(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)

        held = connection.exec_driver_sql(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
        ).fetchone()[0]

        self.assertEqual(held, 0)

    # --- §5.5's roles ------------------------------------------------------------------

    def test_the_revision_creates_the_two_roles_and_the_boundary_between_them(self) -> None:
        """§5.5: the writer writes, the reader cannot, and neither of them owns any DDL."""
        import sqlalchemy as sa

        connection = self.owner_connection()
        self.run_migrations(connection)
        connection.exec_driver_sql(
            "INSERT INTO products (product_id, board_key, title, created_at, updated_at) "
            "VALUES ('secretary', %s, 'Secretary', now(), now())",
            (record_key("product", "secretary"),),
        )
        connection.commit()

        with self.engine("app").connect() as app:
            app.exec_driver_sql(
                "INSERT INTO products (product_id, board_key, title, created_at, updated_at) "
                "VALUES ('written-by-app', %s, 'App', now(), now())",
                (record_key("product", "written-by-app"),),
            )
            app.commit()
            with self.assertRaises(sa.exc.ProgrammingError):
                app.exec_driver_sql("CREATE TABLE forbidden (a int)")

        with self.engine("read").connect() as reader:
            self.assertEqual(
                reader.exec_driver_sql("SELECT count(*) FROM products").fetchone()[0], 2
            )
            with self.assertRaises(sa.exc.ProgrammingError):
                reader.exec_driver_sql(
                    "INSERT INTO products (product_id, board_key, title, created_at, updated_at) "
                    "VALUES ('written-by-read', %s, 'Read', now(), now())",
                    (record_key("product", "written-by-read"),),
                )

    def test_a_table_a_later_revision_adds_is_reachable_without_a_further_grant(self) -> None:
        """The `ALTER DEFAULT PRIVILEGES` half of §5.5, which is the half that fails late."""
        connection = self.owner_connection()
        self.run_migrations(connection)

        connection.exec_driver_sql("CREATE TABLE later_table AS SELECT 1 AS a")
        connection.commit()

        with self.engine("app").connect() as app:
            app.exec_driver_sql("INSERT INTO later_table (a) VALUES (2)")
            app.commit()
        with self.engine("read").connect() as reader:
            self.assertEqual(
                reader.exec_driver_sql("SELECT count(*) FROM later_table").fetchone()[0], 2
            )

    def test_the_late_product_comment_table_has_app_and_read_role_grants(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)
        connection.exec_driver_sql(
            "INSERT INTO products (product_id, board_key, title, created_at, updated_at) "
            "VALUES ('secretary', %s, 'Secretary', now(), now())",
            (record_key("product", "secretary"),),
        )
        connection.commit()

        with self.engine("app").connect() as app:
            app.exec_driver_sql(
                "INSERT INTO product_comments (product_id, body, created_at) "
                "VALUES ('secretary', 'from app', now())"
            )
            app.commit()
        with self.engine("read").connect() as reader:
            self.assertEqual(
                reader.exec_driver_sql("SELECT body FROM product_comments").fetchall(),
                [("from app",)],
            )

    # --- `step_board_store` end to end -------------------------------------------------
    #
    # The unit suite proves the step's outcomes over a stubbed runner. These prove the wire
    # between them is real: that a complete `board-store.env` in an instance directory is what the
    # step resolves, connects with and migrates through, and that a second upgrade changes
    # nothing. They live in this class so one container serves the whole module.

    def write_store(self, directory: Path) -> Path:
        path = directory / "board-store.env"
        path.write_text(
            "".join(f"{key}={value}\n" for key, value in self.config().as_environ().items()),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def context(self, instance: Path, *, dry_run: bool = False):
        return upgrade.UpgradeContext(
            instance_path=instance,
            product_root=instance,
            base_branch="main",
            dry_run=dry_run,
            units=None,
            orca=None,
            automations=None,
        )

    def test_an_upgrade_migrates_a_configured_store_and_then_leaves_it_alone(self) -> None:
        with TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self.write_store(instance)

            preview = upgrade.step_board_store(self.context(instance, dry_run=True))
            self.assertEqual(preview.status, "would-change")
            self.assertIn("0001", preview.detail)
            self.assertIn("0002", preview.detail)

            connection = self.owner_connection()
            self.assertIsNone(
                connection.exec_driver_sql("SELECT to_regclass('public.alembic_version')").fetchone()[0],
                "a dry run must read and write nothing",
            )
            connection.close()

            applied = upgrade.step_board_store(self.context(instance))
            self.assertEqual(applied.status, "changed")
            self.assertIn("0001", applied.detail)
            self.assertIn("0002", applied.detail)

            again = upgrade.step_board_store(self.context(instance))
            self.assertEqual(again.status, "unchanged")

        self.assertEqual(self.counts(self.owner_connection()), DOCUMENTED_COUNTS)

    def test_a_store_that_will_not_answer_fails_the_step_with_its_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            instance = Path(tmp)
            path = self.write_store(instance)
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    f"SECRETARY_DB_OWNER_PASSWORD={OWNER_PASSWORD}",
                    "SECRETARY_DB_OWNER_PASSWORD=not-the-password",
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)

            result = upgrade.step_board_store(self.context(instance))

        self.assertTrue(result.failed)
        self.assertIn("board store", result.detail)


if __name__ == "__main__":
    unittest.main()
