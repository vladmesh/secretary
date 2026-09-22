"""The dispatcher's audit follows its card client's backend.

On 2026-09-10 the production dispatcher ran on the PostgreSQL card store with
the file journal's `TaskAudit` over its data dir, which the PostgreSQL writer never touches. A worker's
`report:done` committed in `requests`/`board_events` was therefore invisible to the report wait, the
worker was declared stalled twice, and the observer got no wake for the Blocked move
(sprint:1437, secretary-1614). One helper now names the audit for every reader and writer: the SQL audit of
the card client `board_client` built, and the command host reads the same object.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.board.sql_audit import SqlTaskAudit
from secretary.dispatch import bootstrap as dispatcher_bootstrap
from secretary.dispatch.bootstrap import runtime_from_args
from secretary.dispatch.host import CommandHostRuntime
from secretary.dispatch.types import HostError
from secretary.head_registry import materialize_snapshot, record_source
from secretary.tasks import task_audit_for
from tests.fakes.dispatcher import dispatcher_seed
from tests.sql_backend_fixtures import CardStoreCase


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


class TaskAuditForTests(CardStoreCase):
    def test_a_card_client_gets_the_sql_audit_whatever_data_dir_it_is_handed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit = task_audit_for(self.card_store(dispatcher_seed()), tmp)
            self.assertIsInstance(audit, SqlTaskAudit)
            self.assertFalse((Path(tmp) / "board").exists())


class RuntimeAuditSelectionTests(CardStoreCase):
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
        with mock.patch.object(dispatcher_bootstrap, "board_client", return_value=client):
            return runtime_from_args(str(self.instance), None, host_mode="noop", owner="secretary-production")

    def test_every_reader_and_the_host_share_the_sql_audit(self) -> None:
        runtime = self._runtime(self.card_store(dispatcher_seed(), instance_dir=self.instance))
        self.assertIsInstance(runtime.audit, SqlTaskAudit)
        self.assertIsInstance(runtime.writer.audit, SqlTaskAudit)
        self.assertIsInstance(runtime.sprints.audit, SqlTaskAudit)
        # The command host's TASK.md feedback selector reads what the dispatcher reads.
        self.assertIs(runtime.host.audit, runtime.audit)
        self.assertEqual(runtime.data_dir, self.data_dir)

    def test_a_host_built_alone_has_no_card_audit_and_refuses_the_task_document_read(self) -> None:
        """No default: a file journal built from the data dir alone is one nobody writes."""
        host = CommandHostRuntime(mock.Mock(instance_dir=self.instance), self.data_dir, mode="noop")
        self.assertIsNone(host.audit)
        with self.assertRaisesRegex(HostError, "without the card audit"):
            host._card_audit()


if __name__ == "__main__":
    unittest.main()
