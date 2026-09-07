"""Migration `0001` against a real PostgreSQL, in a throwaway container.

§10 of ``docs/BOARD_STORE.md`` is the reason this file exists: two earlier revisions of that
document each shipped a statement PostgreSQL refuses, and both were found by executing them, not
by reading them. The same applies to a transcription of that schema into the product, so the
migration is executed here and the result is counted against the numbers the document's own run
produced — 22 tables, 34 `CHECK`, 36 foreign-key, 22 primary-key and 12 unique constraints, and
4 partial unique indexes.

**There is no skip in this module.** A missing Docker, a missing driver or a container that never
becomes ready is an error, not an absence: a schema test that quietly passes because it never
reached a database is worth less than no test at all. The suite is `integration-board`, so this
runs in the `test / integration-board` job of the exact-SHA gate, where both the driver (a core
dependency since this card) and Docker are present.

The container is verification, never delivery. It publishes on loopback, holds no volume, is
removed in `tearDownClass`, and nothing here reads or writes the live installation.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import unittest
import uuid
from pathlib import Path

from secretary.board import migrator
from secretary.board.store import BoardStoreConfig, BoardStoreError

IMAGE = "postgres:16"
DATABASE = "board_store_test"
OWNER = "secretary_owner"
OWNER_PASSWORD = "throwaway-owner-password"
APP_PASSWORD = "throwaway-app-password"
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

#: What §10 counted after running the same statements against `postgres:16`. A disagreement here
#: is a defect of the transcription, not of the document.
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
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - a venv without the core dependency
            raise RuntimeError(
                "psycopg is a core dependency since docs/BOARD_STORE.md §5.8; reinstall the "
                "product (`pip install -e .`) rather than skipping the schema proof"
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
    def credentials(cls, role: str, dbname: str = DATABASE):
        config = BoardStoreConfig(
            host="127.0.0.1",
            port=cls.port,
            dbname=dbname,
            owner_user=OWNER,
            owner_password=OWNER_PASSWORD,
            app_user="secretary_app",
            app_password=APP_PASSWORD,
            read_user="secretary_read",
            read_password=READ_PASSWORD,
        )
        return config.for_role(role)

    def setUp(self) -> None:
        """One empty database and no leftover roles per test: `0001` only ever runs on an empty
        database, and its `CREATE ROLE` statements are cluster-wide."""
        import psycopg

        with psycopg.connect(
            self.credentials("owner", "postgres").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
            maintenance.execute("DROP ROLE IF EXISTS secretary_app")
            maintenance.execute("DROP ROLE IF EXISTS secretary_read")
            maintenance.execute(f"CREATE DATABASE {DATABASE} OWNER {OWNER}")

    def owner_connection(self):
        import psycopg

        conn = psycopg.connect(self.credentials("owner").conninfo())
        self.addCleanup(conn.close)
        return conn

    @property
    def passwords(self) -> dict[str, str]:
        return {"app_password": APP_PASSWORD, "read_password": READ_PASSWORD}

    def migrate(self, conn) -> tuple[int, ...]:
        return migrator.apply(conn, passwords=self.passwords)

    def test_the_initial_migration_reproduces_the_documents_own_numbers(self) -> None:
        conn = self.owner_connection()

        self.assertEqual(self.migrate(conn), (1,))

        counts = conn.execute(COUNTS).fetchone()
        self.assertEqual(
            tuple(counts),
            DOCUMENTED_COUNTS,
            "tables, CHECK, FK, PK, UNIQUE and partial unique indexes must match §10's run",
        )

    def test_it_records_the_version_it_applied_with_the_checksum_of_the_shipped_file(self) -> None:
        conn = self.owner_connection()
        self.migrate(conn)

        rows = conn.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()

        shipped = migrator.discover()[0]
        self.assertEqual(rows, [(1, "initial", shipped.checksum)])
        self.assertEqual(migrator.assert_schema_version(conn), migrator.EXPECTED_SCHEMA_VERSION)

    def test_a_second_run_applies_nothing_and_leaves_the_schema_alone(self) -> None:
        conn = self.owner_connection()
        self.migrate(conn)
        before = tuple(conn.execute(COUNTS).fetchone())

        self.assertEqual(self.migrate(conn), ())

        self.assertEqual(tuple(conn.execute(COUNTS).fetchone()), before)
        self.assertEqual(conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 1)

    def test_an_edited_applied_migration_is_refused_rather_than_reapplied(self) -> None:
        conn = self.owner_connection()
        self.migrate(conn)
        shipped = migrator.discover()[0]
        edited = migrator.Migration(
            shipped.version,
            shipped.name,
            shipped.path,
            shipped.sql + "\n-- an edit after the fact\n",
            migrator.checksum(shipped.sql + "\n-- an edit after the fact\n"),
        )

        with self.assertRaisesRegex(BoardStoreError, "was edited after it was applied"):
            migrator.apply(conn, passwords=self.passwords, migrations=(edited,))

        self.assertEqual(conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 1)
        self.assertEqual(tuple(conn.execute(COUNTS).fetchone()), DOCUMENTED_COUNTS)

    def test_the_version_assertion_refuses_a_schema_this_build_does_not_speak(self) -> None:
        conn = self.owner_connection()
        self.migrate(conn)

        with self.assertRaisesRegex(BoardStoreError, "refusing to write"):
            migrator.assert_schema_version(conn, expected=migrator.EXPECTED_SCHEMA_VERSION + 1)

    def test_a_failing_migration_leaves_no_half_applied_schema(self) -> None:
        """PostgreSQL's transactional DDL, which is why the runner needs no down migration."""
        import psycopg

        conn = self.owner_connection()
        broken = migrator.Migration(
            1,
            "broken",
            Path("0001_broken.sql"),
            "CREATE TABLE early (a int);\nCREATE TABLE syntax error;\n",
            "checksum",
        )

        with self.assertRaises(psycopg.Error):
            migrator.apply(conn, passwords=self.passwords, migrations=(broken,))

        self.assertIsNone(conn.execute("SELECT to_regclass('public.early')").fetchone()[0])
        self.assertIsNone(conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0])

    def test_the_advisory_lock_makes_a_second_runner_wait_for_the_first(self) -> None:
        import psycopg

        holder = psycopg.connect(self.credentials("owner").conninfo(), autocommit=True)
        self.addCleanup(holder.close)
        holder.execute("SELECT pg_advisory_lock(%s)", (migrator.ADVISORY_LOCK_KEY,))
        conn = self.owner_connection()
        finished = threading.Event()

        def run() -> None:
            self.migrate(conn)
            finished.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertFalse(
                finished.wait(1.5),
                "the runner must contend on the fixed advisory key, not migrate concurrently",
            )
        finally:
            holder.execute("SELECT pg_advisory_unlock(%s)", (migrator.ADVISORY_LOCK_KEY,))
        self.assertTrue(finished.wait(30), "the runner never acquired the released lock")
        worker.join(timeout=5)
        self.assertEqual(conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 1)

    def test_the_lock_is_released_once_the_runner_is_done(self) -> None:
        conn = self.owner_connection()
        self.migrate(conn)

        held = conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
        ).fetchone()[0]

        self.assertEqual(held, 0)

    def test_the_migration_creates_the_two_roles_and_the_boundary_between_them(self) -> None:
        """§5.5: the writer writes, the reader cannot, and neither of them owns any DDL."""
        import psycopg

        conn = self.owner_connection()
        self.migrate(conn)
        conn.execute(
            "INSERT INTO products (product_id, title, created_at, updated_at) "
            "VALUES ('secretary', 'Secretary', now(), now())"
        )
        conn.commit()

        with psycopg.connect(self.credentials("app").conninfo(), autocommit=True) as app:
            app.execute(
                "INSERT INTO products (product_id, title, created_at, updated_at) "
                "VALUES ('written-by-app', 'App', now(), now())"
            )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                app.execute("CREATE TABLE forbidden (a int)")

        with psycopg.connect(self.credentials("read").conninfo(), autocommit=True) as reader:
            self.assertEqual(reader.execute("SELECT count(*) FROM products").fetchone()[0], 2)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                reader.execute(
                    "INSERT INTO products (product_id, title, created_at, updated_at) "
                    "VALUES ('written-by-read', 'Read', now(), now())"
                )

    def test_a_table_a_later_migration_adds_is_reachable_without_a_further_grant(self) -> None:
        """The `ALTER DEFAULT PRIVILEGES` half of §5.5, which is the half that fails late."""
        import psycopg

        conn = self.owner_connection()
        self.migrate(conn)
        later = migrator.Migration(
            2,
            "later",
            Path("0002_later.sql"),
            f"CREATE TABLE later_{uuid.uuid4().hex[:8]} AS SELECT 1 AS a;",
            "checksum",
        )
        migrator.apply(conn, passwords=self.passwords, migrations=(migrator.discover()[0], later))
        table = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'later_%'"
        ).fetchone()[0]

        with psycopg.connect(self.credentials("app").conninfo(), autocommit=True) as app:
            app.execute(f"INSERT INTO {table} (a) VALUES (2)")
        with psycopg.connect(self.credentials("read").conninfo(), autocommit=True) as reader:
            self.assertEqual(reader.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
