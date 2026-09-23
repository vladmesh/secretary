"""A long-lived `SqlCardClient` outlives a board-store restart (docs/BOARD_STORE.md §5.6).

The driver is a stand-in at `psycopg.connect`: each connection it hands out can be closed or
broken the way psycopg reports a server that went away, so the rule is proven without a server.
The same rule against a real PostgreSQL is `tests/test_sql_card_reconnect_backend.py`.
"""

from __future__ import annotations

import tempfile
import unittest
from typing import Any, Self
from unittest import mock

import psycopg

from secretary.board.sql_cards import SqlCardClient
from secretary.tasks import TaskError

_PASSWORD = "s3cret-board-password"


class _Credentials:
    def conninfo(self) -> str:
        return f"host=board dbname=board user=app password={_PASSWORD}"


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.rowcount = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        connection = self.connection
        if connection.closed or connection.broken:
            raise psycopg.OperationalError("the connection is closed")
        if connection.die_on_next:
            # What psycopg does when the server drops the socket under a statement.
            connection.die_on_next = False
            connection.broken = True
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        connection.statements.append(sql)
        self.rowcount = 1

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [(1,)]


class _Connection:
    def __init__(self) -> None:
        self.closed = False
        self.broken = False
        self.die_on_next = False
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def _alive(self) -> None:
        if self.closed or self.broken:
            raise psycopg.OperationalError("the connection is lost")

    def commit(self) -> None:
        self._alive()
        self.commits += 1

    def rollback(self) -> None:
        self._alive()
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class ReconnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opened: list[_Connection] = []

        def connect(conninfo: str, **options: Any) -> _Connection:
            connection = _Connection()
            self.opened.append(connection)
            return connection

        self.enterContext(mock.patch("psycopg.connect", side_effect=connect))
        scratch = self.enterContext(tempfile.TemporaryDirectory())
        self.client = SqlCardClient(_Credentials(), scratch)  # type: ignore[arg-type]

    def read(self) -> list[tuple[Any, ...]]:
        return self.client._query("SELECT 1")

    def test_a_healthy_connection_is_reused(self) -> None:
        for _ in range(3):
            self.assertEqual(self.read(), [(1,)])
        with self.client.transaction():
            self.client._execute("UPDATE tasks SET title = title")
        self.read()
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(len(self.opened[0].statements), 5)

    def test_a_closed_connection_is_replaced_by_the_next_read(self) -> None:
        self.read()
        # The server restarted and psycopg already knows it: the kept object is closed.
        self.opened[0].closed = True
        self.assertEqual(self.read(), [(1,)])
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(self.opened[1].statements, ["SELECT 1"])

    def test_a_connection_broken_mid_read_fails_that_read_and_the_next_one_reconnects(self) -> None:
        self.read()
        self.opened[0].die_on_next = True
        with self.assertRaises(TaskError) as refused:
            self.read()
        self.assertEqual(refused.exception.code, "backend_unavailable")
        self.assertNotIn(_PASSWORD, refused.exception.message)
        self.assertEqual(len(self.opened), 1, "the failed call is not retried")
        self.assertEqual(self.read(), [(1,)])
        self.assertEqual(len(self.opened), 2)
        self.assertTrue(self.opened[0].closed)

    def test_a_statement_error_on_a_live_connection_keeps_the_connection(self) -> None:
        self.read()
        refusal = psycopg.errors.UndefinedTable("no")
        with mock.patch.object(_Cursor, "execute", side_effect=refusal), self.assertRaises(TaskError) as refused:
            self.read()
        self.assertEqual(refused.exception.code, "backend_error")
        self.read()
        self.assertEqual(len(self.opened), 1)

    def test_a_transaction_on_a_dead_connection_fails_and_the_next_one_starts_on_a_new_one(self) -> None:
        self.read()
        first = self.opened[0]
        with self.assertRaises(TaskError) as refused, self.client.transaction():
            self.client._execute("UPDATE tasks SET title = 'a'")
            first.die_on_next = True
            self.client._execute("UPDATE tasks SET title = 'b'")
        self.assertEqual(refused.exception.code, "backend_unavailable")
        self.assertEqual(first.commits, 0, "nothing of the failed transaction is committed")
        self.assertEqual(len(self.opened), 1, "no reconnect inside the transaction")
        self.assertIsNone(self.client._connection)
        with self.client.transaction():
            self.client._execute("UPDATE tasks SET title = 'c'")
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(self.opened[1].statements, ["UPDATE tasks SET title = 'c'"])
        self.assertEqual(self.opened[1].commits, 1)

    def test_a_caught_failure_inside_a_transaction_never_reconnects_mid_transaction(self) -> None:
        self.read()
        with self.assertRaises(TaskError), self.client.transaction():
            self.opened[0].die_on_next = True
            with self.assertRaises(TaskError):
                self.client._execute("UPDATE tasks SET title = 'a'")
            # The caller swallowed the failure; the rest of the transaction must not land
            # on a fresh connection that never saw its first half.
            self.client._execute("UPDATE tasks SET title = 'b'")
        self.assertEqual(len(self.opened), 1)
        self.read()
        self.assertEqual(len(self.opened), 2)

    def test_a_transaction_that_starts_on_a_closed_connection_opens_a_new_one(self) -> None:
        self.read()
        self.opened[0].closed = True
        with self.client.transaction():
            self.client._execute("UPDATE tasks SET title = 'a'")
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(self.opened[1].commits, 1)

    def test_a_rolled_back_transaction_on_a_live_connection_keeps_it(self) -> None:
        self.read()
        with self.assertRaises(RuntimeError), self.client.transaction():
            self.client._execute("UPDATE tasks SET title = 'a'")
            raise RuntimeError("the effect refused")
        self.assertEqual(self.opened[0].rollbacks, 1)
        self.read()
        self.assertEqual(len(self.opened), 1)


if __name__ == "__main__":
    unittest.main()
