from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import main
from secretary.data import export_board
from secretary.tasks import KanboardClient, TaskAudit, TaskError, TaskReader, TaskWriter


class FakeKanboard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.tasks = [
            {
                "id": "12", "reference": "secretary-468", "title": "Readonly task protocol",
                "description": "", "column_id": "2", "position": "3", "swimlane_id": "4",
                "date_creation": "1720000000", "date_modification": "1720000010",
            },
            {"id": 13, "reference": "old-1", "title": "Old", "column_id": 1, "position": "bad"},
        ]
        self.metadata = {
            12: {
                "project": "secretary", "task_type": "code", "claim": "codex-terra",
                "head": "codex-terra", "retry_same": "2", "retry_switch": "bad",
                "retry_heads": "codex-terra,claude-opus", "steward_report": "1",
            },
            13: {},
        }

    def call(self, method: str, **params: object) -> object:
        self.calls.append((method, params))
        if method == "getProjectByName":
            return {"id": 7}
        if method == "getColumns":
            return [{"id": 1, "title": "Идеи"}, {"id": 2, "title": "Ready"}]
        if method == "getActiveSwimlanes":
            return [{"id": 4, "name": "Secretary"}]
        if method == "getAllTasks":
            return self.tasks
        if method == "getTaskByReference":
            return next((task for task in self.tasks if task["reference"] == params["reference"]), None)
        if method == "getTaskMetadata":
            return self.metadata[int(params["task_id"])]
        if method == "getAllComments":
            return [{"date_creation": 1720000020, "comment": "[report:done]\nReady for review"}]
        raise AssertionError(method)


class TaskReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeKanboard()
        self.reader = TaskReader(self.client)  # type: ignore[arg-type]

    def test_list_normalizes_and_filters_deterministically(self) -> None:
        result = self.reader.list(states={"ready"}, project="secretary")

        self.assertEqual([task["ref"] for task in result], ["secretary-468"])
        task = result[0]
        self.assertEqual(task["id"], "task_kanboard_12")
        self.assertEqual(task["claim"], {"worker": "codex-terra", "claimed_at": None})
        self.assertEqual(task["retry"], {"same": 2, "switched": 0, "heads": ["codex-terra", "claude-opus"]})
        self.assertEqual(task["routing"]["complexity"], "standard")
        self.assertEqual(task["extensions"]["kanboard"], {"steward_report": "1", "swimlane": "Secretary"})
        self.assertNotIn("comments", task)

    def test_show_preserves_comments_and_legacy_defaults(self) -> None:
        task = self.reader.show("old-1")

        self.assertEqual(task["project"], "")
        self.assertEqual(task["type"], "")
        self.assertIsNone(task["blocked_by"])
        self.assertEqual(task["position"], 0)
        self.assertEqual(task["routing"]["family_preference"], "auto")
        self.assertEqual(task["comments"][0]["marker"], "report:done")
        self.assertEqual(task["comments"][0]["created_at"], "2024-07-03T09:47:00Z")

    def test_show_reports_missing_task(self) -> None:
        with self.assertRaisesRegex(TaskError, "not found") as raised:
            self.reader.show("missing")
        self.assertEqual(raised.exception.code, "not_found")

    def test_unknown_column_is_backend_error(self) -> None:
        self.client.tasks[0]["column_id"] = 999
        with self.assertRaisesRegex(TaskError, "schema") as raised:
            self.reader.list()
        self.assertEqual(raised.exception.code, "backend_error")


class TaskCliTests(unittest.TestCase):
    def test_backend_error_never_echoes_credentials(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", {"KANBOARD_URL": "https://board.invalid/token", "KANBOARD_API_USER": "user", "KANBOARD_API_TOKEN": "super-secret"}, clear=False), mock.patch("secretary.tasks.urllib.request.urlopen", side_effect=OSError("super-secret")), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["task", "list"])

        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")
        self.assertNotIn("super-secret", errors.getvalue())

    def test_missing_runtime_configuration_is_json_error(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["task", "show", "--ref", "secretary-468"])

        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")


class KanboardClientTests(unittest.TestCase):
    def test_rpc_error_is_sanitized(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"error":{"message":"super-secret"}}'
        response.__enter__.return_value = response
        with mock.patch("secretary.tasks.urllib.request.urlopen", return_value=response):
            client = KanboardClient({"KANBOARD_URL": "https://board.invalid", "KANBOARD_API_USER": "user", "KANBOARD_API_TOKEN": "super-secret"})
            with self.assertRaises(TaskError) as raised:
                client.call("getAllTasks", project_id=1)
        self.assertEqual(raised.exception.code, "backend_error")
        self.assertNotIn("super-secret", raised.exception.message)


class WriteKanboard(FakeKanboard):
    fail_comments = False
    fail_metadata = False

    def call(self, method: str, **params: object) -> object:
        if method == "getColumns":
            return [
                {"id": 1, "title": "Идеи"}, {"id": 2, "title": "Ready"},
                {"id": 3, "title": "In progress"}, {"id": 4, "title": "Validate"},
                {"id": 5, "title": "Blocked"}, {"id": 6, "title": "Done"},
            ]
        if method == "createComment":
            self.calls.append((method, params))
            if self.fail_comments:
                raise TaskError("backend_error", "Kanboard rejected the write", 1)
            return 1
        if method == "moveTaskPosition":
            self.calls.append((method, params))
            self.tasks[0]["column_id"] = params["column_id"]
            self.tasks[0]["date_modification"] = "1720000100"
            return True
        if method == "saveTaskMetadata":
            self.calls.append((method, params))
            if self.fail_metadata:
                raise TaskError("backend_error", "Kanboard rejected the metadata write", 1)
            self.metadata[int(params["task_id"])].update(params["values"])
            return True
        return super().call(method, **params)


class TaskWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_forbidden_role_does_not_write(self) -> None:
        with self.assertRaisesRegex(TaskError, "not permitted") as raised:
            self.writer.report(role="reviewer", actor="r", reference="secretary-468", kind="done", body="")
        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertEqual(self.client.calls, [])

    def test_stale_transition_does_not_write(self) -> None:
        with self.assertRaisesRegex(TaskError, "may not move") as raised:
            self.writer.move(role="po", actor="p", reference="secretary-468", target="ready", reason="")
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))

    def test_retry_does_not_repeat_backend_write_or_event(self) -> None:
        result = self.writer.comment(role="worker", actor="w", reference="secretary-468", body="safe", request_id="same")
        second = self.writer.comment(role="worker", actor="w", reference="secretary-468", body="safe", request_id="same")
        self.assertEqual(result["event_id"], second["event_id"])
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createComment"]), 1)
        audit = TaskAudit(self.tmpdir.name)
        with open(audit.events_path, encoding="utf-8") as events:
            self.assertEqual(len(events.readlines()), 1)

    def test_backend_failure_removes_uncommitted_pending_record(self) -> None:
        self.client.fail_comments = True
        with self.assertRaisesRegex(TaskError, "rejected"):
            self.writer.comment(role="worker", actor="w", reference="secretary-468", body="safe")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_pending_is_visible_and_reconciles_without_backend_retry(self) -> None:
        with mock.patch.object(self.writer.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "committed") as raised:
                self.writer.comment(role="worker", actor="w", reference="secretary-468", body="safe")
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        writes = len([call for call in self.client.calls if call[0] == "createComment"])
        self.assertEqual(self.writer.audit.reconcile(), (1, 0))
        self.assertEqual(writes, len([call for call in self.client.calls if call[0] == "createComment"]))

    def test_partial_move_failure_keeps_pending_until_reconcile(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        self.client.fail_comments = True
        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.move(role="dispatcher", actor="d", reference="secretary-468", target="validate", reason="why")
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.client.tasks[0]["column_id"], 4)
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.client.fail_comments = False
        self.assertEqual(self.writer.reconcile(), (1, 0))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_reconcile_completes_stale_ready_cleanup_before_closing_pending(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        self.client.fail_metadata = True
        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.move(role="dispatcher", actor="d", reference="secretary-468", target="ready", reason="")
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.client.tasks[0]["column_id"], 2)
        self.assertEqual(self.client.metadata[12]["claim"], "codex-terra")
        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

        self.client.fail_metadata = False
        self.assertEqual(self.writer.reconcile(), (1, 0))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        task = self.writer.reader.show("secretary-468")
        self.assertIsNone(task["claim"]["worker"])
        self.assertEqual(task["retry"], {"same": 0, "switched": 0, "heads": []})

    def test_pending_blocks_export_from_the_same_data_root(self) -> None:
        self.writer.audit.stage("pending", {"request_id": "pending", "event_id": "evt_pending"})
        with self.assertRaisesRegex(RuntimeError, "unresolved pending"):
            export_board(Path(self.tmpdir.name), command=["pipeline"])
