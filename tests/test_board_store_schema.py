"""The initial revision against a real PostgreSQL, in a throwaway container.

§10 of ``docs/BOARD_STORE.md`` is the reason this file exists: two earlier revisions of that
document each shipped a statement PostgreSQL refuses, and both were found by executing them, not
by reading them. The same applies to the schema's transcription into SQLAlchemy models, so the
Alembic revision is executed here and the result is counted against the numbers the document's own
run produced — 22 tables, 34 `CHECK`, 36 foreign-key, 22 primary-key and 12 unique constraints, and
4 partial unique indexes. The 22nd table and the 22nd primary key are Alembic's `alembic_version`,
which since the owner's decision of 2026-09-07 stands where §7.4's `schema_migrations` stood.

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

#: What §10 counted after running the same schema against `postgres:16`. A disagreement here is a
#: defect of the transcription into models, not of the document.
DOCUMENTED_COUNTS = (22, 34, 36, 22, 12, 4)


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

    def test_the_initial_revision_reproduces_the_documents_own_numbers(self) -> None:
        connection = self.owner_connection()

        self.assertEqual(self.run_migrations(connection), ("0001_initial",))

        self.assertEqual(
            self.counts(connection),
            DOCUMENTED_COUNTS,
            "tables, CHECK, FK, PK, UNIQUE and partial unique indexes must match §10's run",
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

        self.assertEqual(stamped, [("0001_initial",)])
        self.assertIsNone(
            connection.exec_driver_sql("SELECT to_regclass('public.schema_migrations')").fetchone()[0],
            "the hand-rolled version table is gone; Alembic's is the version",
        )
        self.assertEqual(migrate.assert_schema_revision(connection), migrate.head_revision())

    def test_the_version_assertion_refuses_a_schema_this_build_does_not_speak(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)

        with self.assertRaisesRegex(BoardStoreError, "refusing to write"):
            migrate.assert_schema_revision(connection, expected="0002_something_later")

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

        self.assertEqual(owed, ("0001_initial",))
        self.assertIsNone(
            connection.exec_driver_sql("SELECT to_regclass('public.products')").fetchone()[0]
        )
        self.assertIsNone(
            connection.exec_driver_sql("SELECT to_regclass('public.alembic_version')").fetchone()[0]
        )

    def test_a_failing_revision_leaves_no_half_applied_schema(self) -> None:
        """PostgreSQL's transactional DDL, which is why §7.4 needs no down migration.

        The failure is a real one rather than a fabricated statement: §5.5's `CREATE ROLE` is the
        last thing the revision does, so a cluster that already has `secretary_app` fails it after
        all 21 tables have been created. Nothing may survive that.
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
            "INSERT INTO products (product_id, title, created_at, updated_at) "
            "VALUES ('secretary', 'Secretary', now(), now())"
        )
        connection.commit()

        with self.engine("app").connect() as app:
            app.exec_driver_sql(
                "INSERT INTO products (product_id, title, created_at, updated_at) "
                "VALUES ('written-by-app', 'App', now(), now())"
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
                    "INSERT INTO products (product_id, title, created_at, updated_at) "
                    "VALUES ('written-by-read', 'Read', now(), now())"
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

            connection = self.owner_connection()
            self.assertIsNone(
                connection.exec_driver_sql("SELECT to_regclass('public.alembic_version')").fetchone()[0],
                "a dry run must read and write nothing",
            )
            connection.close()

            applied = upgrade.step_board_store(self.context(instance))
            self.assertEqual(applied.status, "changed")
            self.assertIn("0001", applied.detail)

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
