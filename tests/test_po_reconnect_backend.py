"""The live PO service resumes reads and queue dispatch after a board-store session dies.

The database belongs to the ownership-scoped PostgreSQL fixture. A second connection ends one
in-flight service-store transaction, as a store restart would; the transaction fails, and the
same service and store handle later operations. This suite runs in the Docker integration shard.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

import psycopg

from secretary.po.runner import PoRunner
from secretary.po.service import PoService
from secretary.po.store import COMPLETED, PoStore, PoStoreError
from tests.po_cli_fakes import FAKE_CLAUDE, eventually, unscoped_test_launch
from tests.sql_backend_fixtures import PostgresBoard


class PoServiceReconnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="po-reconnect-")))
        self.data = self.root / "data"
        (self.data / "po").mkdir(parents=True)
        board = PostgresBoard.shared()
        self.config = board.fresh_database()
        self.addCleanup(board.release_database, self.config.dbname)

        executable = self.root / "claude"
        executable.write_text(FAKE_CLAUDE, encoding="utf-8")
        executable.chmod(0o700)
        self.store = PoStore(self.config.for_role("app"))
        self.runner = PoRunner(
            self.store,
            self.data,
            executables={"claude": str(executable)},
            turn_launcher=unscoped_test_launch,
            env={**os.environ, "FAKE_LOG": str(self.root / "fake.log")},
        )
        self.service = PoService(
            self.runner,
            data_dir=self.data,
            models={"claude": ("opus",)},
            efforts={"claude": ("high",)},
        )
        self.service_thread: threading.Thread | None = None

        def stop() -> None:
            self.service.stop()
            if self.service_thread is not None:
                self.service_thread.join(timeout=10)
            for live in list(self.runner._live.values()):
                if live.process.poll() is None:
                    live.process.kill()
                live.thread.join(timeout=5)

        self.addCleanup(stop)

    def test_the_same_service_reads_and_dispatches_after_an_in_flight_read_fails(self) -> None:
        self.service.start()
        self.service_thread = threading.Thread(
            target=self.service.run, kwargs={"tick": 0.05, "say": lambda _line: None}
        )
        self.service_thread.start()
        opened = self.service.handle(
            {
                "op": "create_session",
                "cli": "claude",
                "model": "opus",
                "effort": "high",
                "request_id": "po-reconnect-first",
            }
        )
        self.assertTrue(opened["ok"], opened)
        session_id = opened["result"]["session_id"]
        self.assertEqual([session.session_id for session in self.store.sessions()], [session_id])

        with self.assertRaises(PoStoreError) as failed, self.store._transaction() as connection:
            pid = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
            with psycopg.connect(self.config.for_role("owner").conninfo(), autocommit=True) as admin:
                ended = admin.execute("SELECT pg_terminate_backend(%s, 5000)", (pid,)).fetchone()
            self.assertEqual(ended, (True,))
            connection.execute("SELECT 1")
        self.assertIn("did not answer", str(failed.exception))

        # A later safe read and the service's session operation use the same store object.
        self.assertEqual([session.session_id for session in self.store.sessions()], [session_id])
        self.assertEqual(self.store.session(session_id).model, "opus")
        next_opened = self.service.handle(
            {
                "op": "create_session",
                "cli": "claude",
                "model": "opus",
                "effort": "high",
                "request_id": "po-reconnect-second",
            }
        )
        self.assertTrue(next_opened["ok"], next_opened)
        self.assertNotEqual(next_opened["result"]["session_id"], session_id)

        # Submit drives `pump`, the same queue tick the running service calls. The fake turn
        # settles through the real PO store, proving more than a connection-health probe.
        sent = self.service.handle(
            {
                "op": "submit",
                "session_id": session_id,
                "text": "after the store restart",
                "request_id": "po-reconnect-turn",
            }
        )
        self.assertTrue(sent["ok"], sent)
        eventually(
            lambda: bool(self.store.turns(session_id))
            and self.store.turns(session_id)[0].state == COMPLETED,
            "the kept PO service did not dispatch and settle its turn",
        )
        self.assertEqual(self.store.feed(session_id)[0].text, "after the store restart")
        self.assertTrue(self.service_thread.is_alive())
        self.assertFalse(self.service.exiting)


if __name__ == "__main__":
    unittest.main()
