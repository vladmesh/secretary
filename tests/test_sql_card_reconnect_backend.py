"""A `SqlCardClient` whose server session is killed answers again on its next read (§5.6).

`pg_terminate_backend` from a second connection is what a board-store restart does to the
client's session, without restarting the shared test server.  Like the other `*_sql_backend`
suites this needs Docker and never skips.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from secretary.board.sql_cards import SqlCardClient
from secretary.tasks import TaskError
from tests.sql_backend_fixtures import PostgresBoard, terminate_session


class TerminatedSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        board = PostgresBoard.shared()
        config = board.fresh_database()
        scratch = self.enterContext(tempfile.TemporaryDirectory())
        self.client = SqlCardClient(config.for_role("owner"), Path(scratch))

        def dispose() -> None:
            self.client.close()
            board.release_database(config.dbname)

        self.addCleanup(dispose)

    def count(self) -> int:
        return int(self.client._query("SELECT count(*) FROM tasks")[0][0])

    def pid(self) -> int:
        return int(self.client._query("SELECT pg_backend_pid()")[0][0])

    def test_the_next_read_through_the_same_client_succeeds(self) -> None:
        self.assertEqual(self.count(), 0)
        first_pid = self.pid()
        terminate_session(self.client)

        # The pool finds the connection hung up before handing it out, and opens another.
        self.assertEqual(self.count(), 0)
        self.assertNotEqual(self.pid(), first_pid)
        self.assertEqual(self.client._open, 1)

    def test_a_terminated_connection_is_never_handed_out_again(self) -> None:
        # Two pooled connections, both idle; one of them is terminated between two reads.
        together = threading.Barrier(2, timeout=10)
        pids: list[int] = []

        def read_with_the_other() -> None:
            with self.client._session():
                pids.append(self.pid())
                together.wait()

        workers = [threading.Thread(target=read_with_the_other) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        self.assertEqual(len(set(pids)), 2)
        terminated = self.client.connection.info.backend_pid
        terminate_session(self.client)

        seen = {self.pid() for _ in range(4)}
        self.assertNotIn(terminated, seen)
        self.assertEqual(self.count(), 0)

    def test_a_session_whose_connection_dies_under_it_fails_that_read_and_the_next_reconnects(
        self,
    ) -> None:
        # Inside a session the pinned connection is not re-inspected: the read that finds the
        # session killed fails, is not retried, and the next read opens a new connection.
        with self.client._session():
            first_pid = self.pid()
            terminate_session(self.client)
            with self.assertRaises(TaskError) as refused:
                self.count()
            self.assertEqual(refused.exception.code, "backend_unavailable")
            self.assertNotIn(self.client.credentials.password, refused.exception.message)
            self.assertEqual(self.count(), 0)
            self.assertNotEqual(self.pid(), first_pid)

    def test_a_transaction_after_a_terminated_session_commits_on_a_new_one(self) -> None:
        self.count()
        with self.assertRaises(TaskError), self.client.transaction():
            self.client._execute("INSERT INTO projects (project_id) VALUES ('lost')")
            terminate_session(self.client)
            self.client._execute("INSERT INTO projects (project_id) VALUES ('also lost')")
        with self.client.transaction():
            self.client._execute("INSERT INTO projects (project_id) VALUES ('kept')")
        rows = self.client._query("SELECT project_id FROM projects ORDER BY project_id")
        self.assertEqual(rows, [("kept",)])

if __name__ == "__main__":
    unittest.main()
