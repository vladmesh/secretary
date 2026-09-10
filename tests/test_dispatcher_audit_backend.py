"""The dispatcher's audit follows its card client's backend.

On 2026-09-10 the production dispatcher ran on `SECRETARY_CARD_BACKEND=postgres` with
`TaskAudit(data_dir)`: the file journal, which the PostgreSQL writer never touches. A worker's
`report:done` committed in `requests`/`board_events` was therefore invisible to the report wait, the
worker was declared stalled twice, and the observer got no wake for the Blocked move
(sprint:1437, secretary-1614). One helper now decides the audit for every reader and writer from
the client that was built by the switch, and the command host reads the same object.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary import dispatcher as dispatcher_module
from secretary.board.sql_audit import SqlTaskAudit
from secretary.dispatch.host import CommandHostRuntime
from secretary.dispatcher import runtime_from_args
from secretary.head_registry import materialize_snapshot, record_source
from secretary.tasks import TaskAudit, task_audit_for
from tests.fakes.dispatcher import FakeKanboard


class _PostgresClient(FakeKanboard):
    """What the switch hands out on the PostgreSQL backend, as far as construction is concerned."""

    backend_kind = "postgres"

    def _query(self, sql, params=()):  # pragma: no cover - never reached at construction
        raise AssertionError("no query is issued while the runtime is built")


def _instance(root: Path, data_dir: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "instance.yaml").write_text(
        "version: 1\n"
        "name: audit-backend-test\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n"
        "  instance_remote: https://example.invalid/instance.git\n",
        encoding="utf-8",
    )
    return root


class TaskAuditForTests(unittest.TestCase):
    def test_a_postgres_client_gets_the_sql_audit_and_a_kanboard_client_the_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sql = task_audit_for(_PostgresClient(), tmp)
            self.assertIsInstance(sql, SqlTaskAudit)
            journal = task_audit_for(FakeKanboard(), tmp)
            self.assertIsInstance(journal, TaskAudit)
            self.assertEqual(Path(journal.board_dir), Path(tmp) / "board")

    def test_a_client_that_names_no_backend_is_kanboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(task_audit_for(object(), tmp), TaskAudit)


class RuntimeAuditSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        self.data_dir = root / "data"
        self.data_dir.mkdir()
        self.instance = _instance(root / "instance", self.data_dir)
        # A real instance the dispatcher accepts, so the runtime is built by the code the unit runs.
        materialize_snapshot(self.instance, Path(__file__).resolve().parents[1])
        record_source(self.instance, Path(__file__).resolve().parents[1])
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def _runtime(self, client):
        with mock.patch.object(dispatcher_module, "board_client", return_value=client):
            return runtime_from_args(str(self.instance), None, host_mode="noop", owner="secretary-production")

    def test_on_postgres_every_reader_and_the_host_share_the_sql_audit(self) -> None:
        runtime = self._runtime(_PostgresClient())
        self.assertIsInstance(runtime.audit, SqlTaskAudit)
        self.assertIsInstance(runtime.writer.audit, SqlTaskAudit)
        self.assertIsInstance(runtime.sprints.audit, SqlTaskAudit)
        # The command host's TASK.md feedback selector reads what the dispatcher reads.
        self.assertIs(runtime.host.audit, runtime.audit)
        self.assertEqual(runtime.data_dir, self.data_dir)

    def test_on_kanboard_the_journal_stays_and_the_host_still_shares_it(self) -> None:
        runtime = self._runtime(FakeKanboard())
        self.assertIsInstance(runtime.audit, TaskAudit)
        self.assertEqual(Path(runtime.audit.board_dir), self.data_dir / "board")
        self.assertIs(runtime.host.audit, runtime.audit)

    def test_a_host_built_alone_keeps_the_journal_default(self) -> None:
        host = CommandHostRuntime(mock.Mock(instance_dir=self.instance), self.data_dir, mode="noop")
        self.assertIsInstance(host.audit, TaskAudit)


if __name__ == "__main__":
    unittest.main()
