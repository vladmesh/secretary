"""The board store's connection file and the parts of its migration runner that need no server.

Everything here runs without PostgreSQL and without the driver: the resolver is a parse over a
local file, and the runner's discovery, plan, checksum refusal and version assertion are ordinary
Python over a connection object. What genuinely needs a server — applying `0001` and counting what
it produced — is `tests/test_board_store_schema.py`, which raises a throwaway container.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from secretary.board import migrator
from secretary.board.store import (
    ROLES,
    STORE_ENV,
    BoardStoreError,
    findings,
    resolve,
    resolve_role,
    store_path,
)

COMPLETE = {
    "SECRETARY_DB_HOST": "127.0.0.1",
    "SECRETARY_DB_PORT": "5432",
    "SECRETARY_DB_NAME": "secretary",
    "SECRETARY_DB_OWNER_USER": "secretary_owner",
    "SECRETARY_DB_OWNER_PASSWORD": "owner-secret",
    "SECRETARY_DB_APP_USER": "secretary_app",
    "SECRETARY_DB_APP_PASSWORD": "app-secret",
    "SECRETARY_DB_READ_USER": "secretary_read",
    "SECRETARY_DB_READ_PASSWORD": "read-secret",
}


def write_store(directory: Path, values: dict[str, str] | None = None, *, mode: int = 0o600) -> Path:
    path = store_path(directory)
    body = "".join(f"{key}={value}\n" for key, value in (COMPLETE if values is None else values).items())
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


class ConnectionFileTests(unittest.TestCase):
    """§5.4: nine keys, all required, all-or-nothing, and mode 0600."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = Path(self.tmp.name)

    def test_it_declares_exactly_the_nine_keys_of_the_document(self) -> None:
        self.assertEqual(
            STORE_ENV,
            (
                "SECRETARY_DB_HOST",
                "SECRETARY_DB_PORT",
                "SECRETARY_DB_NAME",
                "SECRETARY_DB_OWNER_USER",
                "SECRETARY_DB_OWNER_PASSWORD",
                "SECRETARY_DB_APP_USER",
                "SECRETARY_DB_APP_PASSWORD",
                "SECRETARY_DB_READ_USER",
                "SECRETARY_DB_READ_PASSWORD",
            ),
        )

    def test_a_complete_file_resolves_one_credential_per_role(self) -> None:
        write_store(self.instance)

        config = resolve(self.instance)

        self.assertEqual((config.host, config.port, config.dbname), ("127.0.0.1", 5432, "secretary"))
        self.assertEqual(
            [(role, resolve_role(self.instance, role).user) for role in ROLES],
            [("owner", "secretary_owner"), ("app", "secretary_app"), ("read", "secretary_read")],
        )
        self.assertNotEqual(
            resolve_role(self.instance, "app").password,
            resolve_role(self.instance, "read").password,
            "a single credential would make §5.5's three-role boundary unreachable",
        )

    def test_the_conninfo_escapes_a_password_that_carries_a_space_or_a_quote(self) -> None:
        values = dict(COMPLETE, SECRETARY_DB_APP_PASSWORD="a b'c\\d")
        write_store(self.instance, values)

        conninfo = resolve_role(self.instance, "app").conninfo()

        self.assertIn("password='a b\\'c\\\\d'", conninfo)
        self.assertIn("user='secretary_app'", conninfo)

    def test_a_missing_file_refuses_with_a_reason(self) -> None:
        with self.assertRaisesRegex(BoardStoreError, "board store configuration is missing"):
            resolve(self.instance)

    def test_a_partial_file_refuses_and_names_what_is_absent(self) -> None:
        for absent in STORE_ENV:
            with self.subTest(absent=absent):
                write_store(self.instance, {k: v for k, v in COMPLETE.items() if k != absent})
                with self.assertRaisesRegex(BoardStoreError, absent):
                    resolve(self.instance)

    def test_an_empty_value_is_a_partial_file_and_not_an_empty_password(self) -> None:
        write_store(self.instance, dict(COMPLETE, SECRETARY_DB_APP_PASSWORD=""))

        with self.assertRaisesRegex(BoardStoreError, "line 7 is invalid"):
            resolve(self.instance)

    def test_an_unknown_or_repeated_key_refuses_the_whole_file(self) -> None:
        path = store_path(self.instance)
        for body, line in (
            ("".join(f"{k}={v}\n" for k, v in COMPLETE.items()) + "SECRETARY_DB_EXTRA=x\n", 10),
            ("SECRETARY_DB_HOST=a\n" + "".join(f"{k}={v}\n" for k, v in COMPLETE.items()), 2),
        ):
            with self.subTest(line=line):
                path.write_text(body, encoding="utf-8")
                path.chmod(0o600)
                with self.assertRaisesRegex(BoardStoreError, f"line {line} is invalid"):
                    resolve(self.instance)

    def test_a_line_without_an_equals_sign_refuses(self) -> None:
        path = write_store(self.instance)
        path.write_text(path.read_text(encoding="utf-8") + "nonsense\n", encoding="utf-8")

        with self.assertRaisesRegex(BoardStoreError, "line 10 must use KEY=VALUE"):
            resolve(self.instance)

    def test_a_readable_by_others_file_refuses_until_it_is_chmodded(self) -> None:
        write_store(self.instance, mode=0o644)

        with self.assertRaisesRegex(BoardStoreError, "permissions are too broad"):
            resolve(self.instance)

    def test_a_symlink_is_refused_even_when_its_target_is_private(self) -> None:
        real = self.instance / "elsewhere.env"
        real.write_text("".join(f"{k}={v}\n" for k, v in COMPLETE.items()), encoding="utf-8")
        real.chmod(0o600)
        store_path(self.instance).symlink_to(real)

        with self.assertRaisesRegex(BoardStoreError, "regular file, not a symlink"):
            resolve(self.instance)

    def test_a_port_that_is_not_a_tcp_port_refuses(self) -> None:
        for port in ("0", "70000", "5432a"):
            with self.subTest(port=port):
                write_store(self.instance, dict(COMPLETE, SECRETARY_DB_PORT=port))
                with self.assertRaisesRegex(BoardStoreError, "not a TCP port"):
                    resolve(self.instance)

    def test_an_unknown_role_refuses_rather_than_falling_back_to_the_owner(self) -> None:
        write_store(self.instance)

        with self.assertRaisesRegex(BoardStoreError, "role must be one of"):
            resolve_role(self.instance, "admin")

    def test_a_checkout_with_no_store_and_no_ignore_entry_reports_nothing(self) -> None:
        """A pre-store installation is not an unhealthy one; only a lifecycle marker makes
        absence a finding, exactly as `board_transport.findings` decides it."""
        self.assertEqual(findings(self.instance), [])

    def test_findings_reports_a_broken_file_without_disclosing_it(self) -> None:
        write_store(self.instance, mode=0o644)

        reported = findings(self.instance)

        self.assertEqual(len(reported), 1)
        self.assertIn("permissions are too broad", reported[0])
        self.assertNotIn("owner-secret", reported[0])

    def test_resolving_never_writes_anything_into_the_installation(self) -> None:
        before = sorted(os.listdir(self.instance))

        with self.assertRaises(BoardStoreError):
            resolve(self.instance)
        findings(self.instance)

        self.assertEqual(sorted(os.listdir(self.instance)), before)


class FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    """Only what the runner uses: `execute` returning something with `fetchone`/`fetchall`."""

    def __init__(self, *, migrations_table: bool = True, rows: list[tuple] | None = None) -> None:
        self.migrations_table = migrations_table
        self.rows = rows or []
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement, params=None):
        self.statements.append(statement)
        if "to_regclass" in statement:
            return FakeCursor([("public.schema_migrations" if self.migrations_table else None,)])
        if statement.startswith("SELECT version, checksum"):
            return FakeCursor(self.rows)
        return FakeCursor([])

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class MigrationDiscoveryTests(unittest.TestCase):
    def test_the_tree_ships_exactly_the_initial_migration_this_build_expects(self) -> None:
        shipped = migrator.discover()

        self.assertEqual([m.version for m in shipped], [1])
        self.assertEqual(shipped[0].name, "initial")
        self.assertEqual(migrator.EXPECTED_SCHEMA_VERSION, shipped[-1].version)

    def test_the_initial_migration_declares_only_the_two_generated_passwords(self) -> None:
        self.assertEqual(migrator.discover()[0].parameters, migrator.PASSWORD_PARAMETERS)

    def test_no_password_is_a_literal_in_the_file(self) -> None:
        """§7.4: a migration whose bytes differ per installation cannot have a stable checksum."""
        sql = migrator.discover()[0].sql

        self.assertIn("CREATE ROLE secretary_app  LOGIN PASSWORD :'app_password';", sql)
        self.assertIn("CREATE ROLE secretary_read LOGIN PASSWORD :'read_password';", sql)
        self.assertNotIn("PASSWORD '", sql)

    def test_a_misnamed_or_duplicated_file_refuses_before_anything_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "initial.sql").write_text("SELECT 1;", encoding="utf-8")
            with self.assertRaisesRegex(BoardStoreError, "not NNNN_name.sql"):
                migrator.discover(directory)
            (directory / "initial.sql").unlink()
            (directory / "0002_second.sql").write_text("SELECT 1;", encoding="utf-8")
            with self.assertRaisesRegex(BoardStoreError, "without a gap"):
                migrator.discover(directory)

    def test_render_substitutes_the_passwords_and_quotes_them(self) -> None:
        migration = migrator.discover()[0]

        rendered = migrator.render(migration, {"app_password": "it's", "read_password": "r"})

        self.assertIn("CREATE ROLE secretary_app  LOGIN PASSWORD 'it''s';", rendered)
        self.assertIn("CREATE ROLE secretary_read LOGIN PASSWORD 'r';", rendered)
        self.assertNotRegex(rendered, r":'[a-z_]+'")

    def test_render_refuses_rather_than_sending_an_empty_password(self) -> None:
        migration = migrator.discover()[0]

        with self.assertRaisesRegex(BoardStoreError, "read_password"):
            migrator.render(migration, {"app_password": "a", "read_password": ""})

    def test_render_refuses_a_parameter_the_runner_does_not_supply(self) -> None:
        migration = migrator.Migration(1, "x", Path("0001_x.sql"), "SELECT :'other';", "c")

        with self.assertRaisesRegex(BoardStoreError, "does not supply: other"):
            migrator.render(migration, {"app_password": "a", "read_password": "b"})


class MigrationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shipped = migrator.discover()
        self.first = self.shipped[0]

    def test_an_empty_database_owes_every_migration(self) -> None:
        self.assertEqual(migrator.plan(self.shipped, {}), self.shipped)

    def test_an_up_to_date_database_owes_nothing(self) -> None:
        self.assertEqual(migrator.plan(self.shipped, {1: self.first.checksum}), ())

    def test_an_edited_applied_migration_is_refused_and_not_reapplied(self) -> None:
        with self.assertRaisesRegex(BoardStoreError, "was edited after it was applied") as caught:
            migrator.plan(self.shipped, {1: "0" * 64})

        self.assertIn(self.first.checksum, str(caught.exception))

    def test_a_database_ahead_of_the_tree_is_refused(self) -> None:
        with self.assertRaisesRegex(BoardStoreError, "ahead of the code"):
            migrator.plan(self.shipped, {1: self.first.checksum, 2: "x"})

    def test_an_unmigrated_database_reports_no_version_rather_than_raising(self) -> None:
        self.assertIsNone(migrator.current_version(FakeConnection(migrations_table=False)))


class SchemaVersionAssertionTests(unittest.TestCase):
    """§7.4's startup check. It exists and has both outcomes; this card wires it into nothing."""

    def test_a_matching_version_is_accepted_and_returned(self) -> None:
        conn = FakeConnection(rows=[(1, "checksum")])

        self.assertEqual(migrator.assert_schema_version(conn, expected=1), 1)

    def test_a_mismatching_version_refuses_and_names_both_numbers(self) -> None:
        conn = FakeConnection(rows=[(1, "checksum")])

        with self.assertRaises(BoardStoreError) as caught:
            migrator.assert_schema_version(conn, expected=2)

        self.assertIn("0001", str(caught.exception))
        self.assertIn("0002", str(caught.exception))
        self.assertIn("refusing to write", str(caught.exception))

    def test_an_unmigrated_database_refuses_too(self) -> None:
        conn = FakeConnection(migrations_table=False)

        with self.assertRaisesRegex(BoardStoreError, "no schema at all"):
            migrator.assert_schema_version(conn, expected=1)


class MigrationApplicationTests(unittest.TestCase):
    """The lock, the per-migration transaction and the refusal, against a recording connection."""

    def test_it_locks_applies_records_and_unlocks_in_that_order(self) -> None:
        conn = FakeConnection(migrations_table=False)
        migration = migrator.Migration(1, "x", Path("0001_x.sql"), "CREATE TABLE t (a int);", "c")

        applied = migrator.apply(
            conn, passwords={}, migrations=(migration,), applied_at="2026-09-07T00:00:00Z"
        )

        self.assertEqual(applied, (1,))
        self.assertIn("pg_advisory_lock", conn.statements[0])
        self.assertIn("CREATE TABLE t (a int);", conn.statements)
        self.assertTrue(
            any(statement.startswith("INSERT INTO schema_migrations") for statement in conn.statements)
        )
        self.assertIn("pg_advisory_unlock", conn.statements[-1])
        self.assertEqual(conn.rollbacks, 0)

    def test_a_failing_migration_rolls_back_and_still_releases_the_lock(self) -> None:
        class Failing(FakeConnection):
            def execute(self, statement, params=None):
                if statement.startswith("CREATE"):
                    raise RuntimeError("syntax error at or near")
                return super().execute(statement, params)

        conn = Failing(migrations_table=False)
        migration = migrator.Migration(1, "x", Path("0001_x.sql"), "CREATE TABLE t (a int);", "c")

        with self.assertRaisesRegex(RuntimeError, "syntax error"):
            migrator.apply(conn, passwords={}, migrations=(migration,))

        self.assertEqual(conn.rollbacks, 1)
        self.assertIn("pg_advisory_unlock", conn.statements[-1])


if __name__ == "__main__":
    unittest.main()
