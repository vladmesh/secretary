from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import main
from secretary.data import export_board
from secretary.routing_journal import (
    HeadRun,
    attempts,
    head_run_from_profile,
    routing_payload,
)
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
                "codex_launch_mode": "tui",
            },
            13: {},
        }

    def call(self, method: str, **params: object) -> object:
        self.calls.append((method, params))
        if method == "getProjectByName":
            return {"id": 7}
        if method == "getColumns":
            return [{"id": 1, "title": "Ideas"}, {"id": 2, "title": "Ready"}]
        if method == "getActiveSwimlanes":
            return [{"id": 4, "name": "Secretary"}]
        if method == "getAllTasks":
            if params.get("status_id") == 1:
                return [
                    task for task in self.tasks
                    if int(task.get("is_active", task.get("status", 1)) or 0) != 0
                ]
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
        self.assertEqual(task["routing"]["codex_launch_mode"], "tui")
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

    def test_create_rejects_codex_mode_for_non_codex_head_before_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "heads").mkdir()
            (root / "instance.yaml").write_text("version: 1\nname: test\ndata_dir: /tmp/data\n", encoding="utf-8")
            (root / "heads" / "heads.yaml").write_text(
                "\n".join([
                    "profiles:",
                    "  claude-opus:",
                    "    adapter: claude",
                    "role_defaults:",
                    "  new_card: claude-opus",
                ]),
                encoding="utf-8",
            )
            output, errors = io.StringIO(), io.StringIO()
            with mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = main([
                    "task", "create",
                    "--role", "po",
                    "--instance", str(root),
                    "--project", "secretary",
                    "--type", "code",
                    "--title", "T",
                    "--head", "claude-opus",
                    "--codex-mode", "tui",
                ])

        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        error = json.loads(errors.getvalue())["error"]
        self.assertEqual(error["code"], "validation")
        self.assertIn("requires a Codex worker head", error["message"])

    def test_archive_cli_reads_reason_file_and_closes_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reason = root / "reason.md"
            reason.write_text("backlog cleanup\n", encoding="utf-8")
            client = WriteKanboard()
            client.metadata[12]["claim"] = ""
            output, errors = io.StringIO(), io.StringIO()
            with mock.patch("secretary.task_commands.KanboardClient", return_value=client), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = main([
                    "task", "archive",
                    "--role", "po",
                    "--ref", "secretary-468",
                    "--data-dir", str(data_dir),
                    "--reason-file", str(reason),
                    "--request-id", "archive-cli",
                ])

        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["action"], "archived")
        self.assertEqual(client.tasks[0]["is_active"], 0)


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
    lose_comment_reply = False
    fail_metadata = False
    fail_move = False
    fail_update = False
    fail_close = False

    def __init__(self) -> None:
        super().__init__()
        self.comments: dict[int, list[dict[str, object]]] = {
            int(task["id"]): [] for task in self.tasks
        }

    def call(self, method: str, **params: object) -> object:
        if method == "getColumns":
            return [
                {"id": 1, "title": "Ideas"}, {"id": 2, "title": "Ready"},
                {"id": 3, "title": "In progress"}, {"id": 4, "title": "Validate"},
                {"id": 5, "title": "Blocked"}, {"id": 6, "title": "Done"},
            ]
        if method == "createComment":
            self.calls.append((method, params))
            if self.fail_comments:
                raise TaskError("backend_error", "Kanboard rejected the write", 1)
            self.comments[int(params["task_id"])].append(
                {"date_creation": "1720000020", "comment": params["content"]}
            )
            if self.lose_comment_reply:
                raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1)
            return 1
        if method == "getAllComments":
            return self.comments[int(params["task_id"])]
        if method == "createTask":
            self.calls.append((method, params))
            task_id = max(int(task["id"]) for task in self.tasks) + 1
            self.tasks.append({
                "id": task_id,
                "reference": "",
                "title": params["title"],
                "description": params.get("description", ""),
                "column_id": params["column_id"],
                "position": len(self.tasks) + 1,
                "swimlane_id": params.get("swimlane_id") or 0,
                "date_creation": "1720000200",
                "date_modification": "1720000200",
            })
            self.metadata[task_id] = {}
            self.comments[task_id] = []
            return task_id
        if method == "updateTask":
            self.calls.append((method, params))
            if self.fail_update:
                raise TaskError("backend_error", "Kanboard rejected the write", 1)
            task = next(task for task in self.tasks if int(task["id"]) == int(params["id"]))
            for field in ("reference", "title", "description"):
                if field in params:
                    task[field] = params[field]
            task["date_modification"] = "1720000201"
            return True
        if method == "moveTaskPosition":
            self.calls.append((method, params))
            if self.fail_move:
                raise TaskError("backend_error", "Kanboard rejected the move", 1)
            task = next(task for task in self.tasks if int(task["id"]) == int(params["task_id"]))
            task_index = self.tasks.index(task)
            column_id = params["column_id"]
            swimlane_id = params["swimlane_id"]
            self.tasks.remove(task)
            siblings = sorted(
                (
                    candidate for candidate in self.tasks
                    if candidate["column_id"] == column_id and candidate["swimlane_id"] == swimlane_id
                ),
                key=lambda candidate: int(candidate.get("position") or 0),
            )
            position = min(max(1, int(params["position"])), len(siblings) + 1)
            task["column_id"] = column_id
            task["swimlane_id"] = swimlane_id
            siblings.insert(position - 1, task)
            for index, candidate in enumerate(siblings, start=1):
                candidate["position"] = index
            self.tasks.insert(task_index, task)
            task["date_modification"] = "1720000100"
            return True
        if method == "saveTaskMetadata":
            self.calls.append((method, params))
            if self.fail_metadata:
                raise TaskError("backend_error", "Kanboard rejected the metadata write", 1)
            self.metadata[int(params["task_id"])].update(params["values"])
            return True
        if method == "closeTask":
            self.calls.append((method, params))
            if self.fail_close:
                raise TaskError("backend_error", "Kanboard rejected the archive", 1)
            task = next(task for task in self.tasks if int(task["id"]) == int(params["task_id"]))
            task["is_active"] = 0
            task["date_modification"] = "1720000300"
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

    def test_edit_is_po_only_and_requires_a_change(self) -> None:
        with self.assertRaisesRegex(TaskError, "not permitted") as raised:
            self.writer.edit(role="worker", actor="w", reference="secretary-468", description="new spec")
        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertEqual(self.client.calls, [])

        with self.assertRaisesRegex(TaskError, "requires a new") as raised:
            self.writer.edit(role="po", actor="operator", reference="secretary-468")
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.calls, [])

    def test_edit_refuses_active_states(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        with self.assertRaisesRegex(TaskError, "Ideas, Ready or Blocked") as raised:
            self.writer.edit(role="po", actor="operator", reference="secretary-468", description="new spec")
        self.assertEqual(raised.exception.code, "edit_forbidden")
        self.assertFalse(any(call[0] == "updateTask" for call in self.client.calls))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_edit_updates_spec_and_routing_and_writes_audit(self) -> None:
        old_description = str(self.client.tasks[0]["description"])

        result = self.writer.edit(
            role="po",
            actor="operator",
            reference="secretary-468",
            description="revised spec",
            head="codex-terra",
            review_head="claude-opus",
            request_id="edit-once",
        )

        self.assertEqual(result["action"], "edited")
        self.assertEqual(result["task"]["description"], "revised spec")
        update = next(params for method, params in self.client.calls if method == "updateTask")
        self.assertEqual(update, {"id": 12, "description": "revised spec"})
        metadata = next(params for method, params in self.client.calls if method == "saveTaskMetadata")
        self.assertEqual(metadata["values"], {"head": "codex-terra", "review_head": "claude-opus"})
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            event = json.loads(events.readline())
        self.assertEqual(event["kind"], "edited")
        payload = event["payload"]
        self.assertEqual(payload["description_sha256"], hashlib.sha256(b"revised spec").hexdigest())
        self.assertEqual(payload["description_sha256_was"], hashlib.sha256(old_description.encode()).hexdigest())
        self.assertIsNone(payload["title_sha256"])
        self.assertEqual(payload["head"], "codex-terra")
        self.assertEqual(payload["review_head"], "claude-opus")

    def test_edit_retry_does_not_repeat_backend_write(self) -> None:
        first = self.writer.edit(role="po", actor="operator", reference="secretary-468", description="v2", request_id="same-edit")
        second = self.writer.edit(role="po", actor="operator", reference="secretary-468", description="v2", request_id="same-edit")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(len([call for call in self.client.calls if call[0] == "updateTask"]), 1)

    def test_archive_is_po_only_and_requires_reason(self) -> None:
        with self.assertRaisesRegex(TaskError, "not permitted") as raised:
            self.writer.archive(
                role="worker", actor="w", reference="secretary-468", reason="cleanup"
            )
        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

        with self.assertRaisesRegex(TaskError, "non-empty reason") as raised:
            self.writer.archive(
                role="po", actor="operator", reference="secretary-468", reason=" "
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

    def test_archive_refuses_live_work_or_active_claim(self) -> None:
        with self.assertRaisesRegex(TaskError, "active claim") as raised:
            self.writer.archive(
                role="po", actor="operator", reference="secretary-468", reason="cleanup"
            )
        self.assertEqual(raised.exception.code, "live_work")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

        self.client.metadata[12]["claim"] = ""
        self.client.tasks[0]["column_id"] = 4
        with self.assertRaisesRegex(TaskError, "live worker or reviewer") as raised:
            self.writer.archive(
                role="po", actor="operator", reference="secretary-468", reason="cleanup"
            )
        self.assertEqual(raised.exception.code, "live_work")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

    def test_archive_closes_card_and_writes_audit(self) -> None:
        self.client.metadata[12]["claim"] = ""

        result = self.writer.archive(
            role="po",
            actor="operator",
            reference="secretary-468",
            reason="backlog cleanup",
            request_id="archive-once",
        )

        self.assertEqual(result["action"], "archived")
        self.assertEqual(self.client.tasks[0]["is_active"], 0)
        self.assertEqual(
            [call[0] for call in self.client.calls if call[0] in {"createComment", "closeTask"}],
            ["createComment", "closeTask"],
        )
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            event = json.loads(events.readline())
        self.assertEqual(event["kind"], "archived")
        self.assertEqual(event["payload"].keys(), {"reason_sha256"})
        self.assertNotIn("secretary-468", [task["ref"] for task in self.writer.reader.list()])

    def test_archive_retry_after_lost_close_reply_does_not_close_twice(self) -> None:
        self.client.metadata[12]["claim"] = ""
        self.client.fail_close = True
        original_close = self.client.call

        def close_then_lose(method: str, **params: object) -> object:
            if method == "closeTask":
                self.client.fail_close = False
                original_close(method, **params)
                raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1)
            return original_close(method, **params)

        with mock.patch.object(self.client, "call", side_effect=close_then_lose):
            with self.assertRaisesRegex(TaskError, "audit repair") as raised:
                self.writer.archive(
                    role="po",
                    actor="operator",
                    reference="secretary-468",
                    reason="backlog cleanup",
                    request_id="archive-retry",
                )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        closes = len([call for call in self.client.calls if call[0] == "closeTask"])

        result = self.writer.archive(
            role="po",
            actor="operator",
            reference="secretary-468",
            reason="backlog cleanup",
            request_id="archive-retry",
        )

        self.assertEqual(result["action"], "archived")
        self.assertEqual(closes, len([call for call in self.client.calls if call[0] == "closeTask"]))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_archive_retry_after_failed_comment_recreates_reason_before_close(self) -> None:
        self.client.metadata[12]["claim"] = ""
        original_call = self.client.call
        failed_once = False

        def fail_first_comment(method: str, **params: object) -> object:
            nonlocal failed_once
            if method == "createComment" and not failed_once:
                failed_once = True
                self.client.calls.append((method, params))
                raise TaskError("backend_error", "Kanboard rejected the write", 1)
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=fail_first_comment):
            with self.assertRaisesRegex(TaskError, "audit repair") as raised:
                self.writer.archive(
                    role="po",
                    actor="operator",
                    reference="secretary-468",
                    reason="backlog cleanup",
                    request_id="archive-comment-retry",
                )
            self.assertEqual(raised.exception.code, "audit_pending")

            result = self.writer.archive(
                role="po",
                actor="operator",
                reference="secretary-468",
                reason="backlog cleanup",
                request_id="archive-comment-retry",
            )

        self.assertEqual(result["action"], "archived")
        self.assertEqual(self.client.tasks[0]["is_active"], 0)
        self.assertEqual(
            [comment["comment"] for comment in self.client.comments[12]],
            ["[archive]\nbacklog cleanup"],
        )
        self.assertLess(
            [call[0] for call in self.client.calls].index("createComment", 1),
            [call[0] for call in self.client.calls].index("closeTask"),
        )

    def test_archive_reconcile_without_missing_reason_does_not_close(self) -> None:
        self.client.metadata[12]["claim"] = ""
        original_call = self.client.call
        failed_once = False

        def fail_first_comment(method: str, **params: object) -> object:
            nonlocal failed_once
            if method == "createComment" and not failed_once:
                failed_once = True
                self.client.calls.append((method, params))
                raise TaskError("backend_error", "Kanboard rejected the write", 1)
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=fail_first_comment):
            with self.assertRaises(TaskError):
                self.writer.archive(
                    role="po",
                    actor="operator",
                    reference="secretary-468",
                    reason="backlog cleanup",
                    request_id="archive-reconcile-missing-reason",
                )

        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertNotEqual(self.client.tasks[0].get("is_active"), 0)
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

    def test_archive_refuses_dispatcher_record_after_claim_was_cleared(self) -> None:
        self.client.metadata[12]["claim"] = ""
        state_dir = Path(self.tmpdir.name) / "dispatcher"
        state_dir.mkdir()
        (state_dir / "production-state.json").write_text(
            json.dumps({
                "version": 1,
                "phase": "production",
                "records": {
                    "secretary-468": {
                        "worker": "worker-secretary-468",
                        "workspace": "/home/dev/orca/workspaces/secretary/468-archive",
                        "handle": "terminal-1",
                        "review_handle": "review-1",
                    }
                },
            }),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(TaskError, "live dispatcher work") as raised:
            self.writer.archive(
                role="po",
                actor="operator",
                reference="secretary-468",
                reason="cleanup",
                request_id="archive-live-dispatcher-record",
            )

        self.assertEqual(raised.exception.code, "live_work")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

    def test_pending_is_visible_and_reconciles_without_backend_retry(self) -> None:
        with mock.patch.object(self.writer.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "committed") as raised:
                self.writer.comment(role="worker", actor="w", reference="secretary-468", body="safe")
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        writes = len([call for call in self.client.calls if call[0] == "createComment"])
        self.assertEqual(self.writer.audit.reconcile(), (1, 0))
        self.assertEqual(writes, len([call for call in self.client.calls if call[0] == "createComment"]))

    def test_restore_comment_retry_after_lost_reply_does_not_duplicate_history(self) -> None:
        self.client.lose_comment_reply = True
        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.restore_comment(
                reference="secretary-468", body="[report:done]\\nrestored", occurrence=0,
                request_id="restore-comment-lost-reply",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(len(self.client.comments[12]), 1)

        self.client.lose_comment_reply = False
        self.writer.restore_comment(
            reference="secretary-468", body="[report:done]\\nrestored", occurrence=0,
            request_id="restore-comment-lost-reply",
        )

        self.assertEqual(len(self.client.comments[12]), 1)
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertNotIn("restore_body", events.read())

    def test_restore_comment_retry_uses_digest_occurrence_not_history_index(self) -> None:
        self.client.comments[12].append({"date_creation": "1720000020", "comment": "first"})
        self.client.lose_comment_reply = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.restore_comment(
                reference="secretary-468", body="second", occurrence=0,
                request_id="restore-second-lost-reply",
            )
        self.client.lose_comment_reply = False
        self.writer.restore_comment(
            reference="secretary-468", body="second", occurrence=0,
            request_id="restore-second-lost-reply",
        )
        self.assertEqual([comment["comment"] for comment in self.client.comments[12]], ["first", "second"])

        self.client.lose_comment_reply = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.restore_comment(
                reference="secretary-468", body="second", occurrence=1,
                request_id="restore-duplicate-lost-reply",
            )
        self.client.lose_comment_reply = False
        self.writer.restore_comment(
            reference="secretary-468", body="second", occurrence=1,
            request_id="restore-duplicate-lost-reply",
        )
        self.assertEqual(
            [comment["comment"] for comment in self.client.comments[12]],
            ["first", "second", "second"],
        )

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

    def test_restore_move_failure_keeps_pending_audit(self) -> None:
        self.client.fail_move = True
        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.restore_card(
                reference="secretary-468", metadata={"claim": "restored"}, target="in_progress"
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

    def test_reconcile_finishes_pending_restore_before_auditing_success(self) -> None:
        self.client.fail_move = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.restore_card(
                reference="secretary-468", metadata={"claim": "restored"}, target="in_progress"
            )
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], "ready")
        self.client.fail_move = False

        self.assertEqual(self.writer.reconcile(), (1, 0))
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], "in_progress")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_pending_create_repairs_orphaned_reference(self) -> None:
        self.client.fail_update = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.create(
                role="po", actor="operator", project="secretary", task_type="code",
                title="Restore", reference="secretary-restore", request_id="restore-create",
            )
        self.assertEqual(self.client.tasks[-1]["reference"], "")
        self.client.fail_update = False

        result = self.writer.create(
            role="po", actor="operator", project="secretary", task_type="code",
            title="Restore", reference="secretary-restore", request_id="restore-create",
        )
        self.assertEqual(result["task"]["ref"], "secretary-restore")
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

    def test_pending_ready_replay_finishes_cleanup_before_success_audit(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        self.client.metadata[12]["resolved_head"] = "codex-terra"
        self.client.metadata[12]["resolved_review_head"] = "codex-reviewer"
        self.client.fail_metadata = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.move(role="dispatcher", actor="d", reference="secretary-468", target="ready", reason="", request_id="ready-replay")

        moves = len([call for call in self.client.calls if call[0] == "moveTaskPosition"])
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.move(role="dispatcher", actor="d", reference="secretary-468", target="ready", reason="", request_id="ready-replay")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))

        self.client.fail_metadata = False
        result = self.writer.move(role="dispatcher", actor="d", reference="secretary-468", target="ready", reason="", request_id="ready-replay")

        self.assertEqual(result["task"]["state"], "ready")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertEqual(len(events.readlines()), 1)
        task = self.writer.reader.show("secretary-468")
        self.assertIsNone(task["claim"]["worker"])
        self.assertIsNone(task["routing"]["resolved_worker_head"])
        self.assertIsNone(task["routing"]["resolved_review_head"])
        self.assertEqual(task["retry"], {"same": 0, "switched": 0, "heads": []})

    def test_dispatcher_claim_stamps_metadata_moves_and_audits(self) -> None:
        self.client.metadata[12]["claim"] = ""
        result = self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="secretary-468-runtime",
            resolved_head="codex",
            resolved_review_head="codex-reviewer",
            request_id="claim-once",
        )

        self.assertEqual(result["action"], "claimed")
        self.assertEqual(result["task"]["state"], "in_progress")
        self.assertEqual(self.client.metadata[12]["claim"], "secretary-468-runtime")
        self.assertEqual(self.client.metadata[12]["resolved_head"], "codex")
        self.assertEqual(self.client.metadata[12]["resolved_review_head"], "codex-reviewer")
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            event = json.loads(events.readline())
        self.assertEqual(event["kind"], "claimed")
        self.assertEqual(event["payload"]["worker"], "secretary-468-runtime")

    def test_create_stores_codex_launch_mode_and_audits(self) -> None:
        result = self.writer.create(
            role="po",
            actor="operator",
            project="secretary",
            task_type="code",
            title="Launch mode",
            description="body",
            target="ready",
            reference="secretary-522",
            head="codex-extra",
            codex_launch_mode="tui",
            request_id="create-tui",
        )

        self.assertEqual(result["action"], "created")
        self.assertEqual(result["task"]["ref"], "secretary-522")
        self.assertEqual(result["task"]["state"], "ready")
        self.assertEqual(result["task"]["routing"]["head_override"], "codex-extra")
        self.assertEqual(result["task"]["routing"]["codex_launch_mode"], "tui")
        task_id = int(result["task"]["id"].removeprefix("task_kanboard_"))
        self.assertEqual(self.client.metadata[task_id]["codex_launch_mode"], "tui")
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            event = json.loads(events.readline())
        self.assertEqual(event["kind"], "created")
        self.assertEqual(event["payload"]["codex_launch_mode"], "tui")
        self.assertEqual(event["payload"]["head"], "codex-extra")
        self.assertIn("title_sha256", event["payload"])

    def test_pending_create_replay_restores_metadata_before_audit(self) -> None:
        self.client.fail_metadata = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.create(
                role="po",
                actor="operator",
                project="secretary",
                task_type="code",
                title="Launch mode",
                target="ready",
                reference="secretary-523",
                codex_launch_mode="tui",
                request_id="create-replay",
            )
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        create_writes = len([call for call in self.client.calls if call[0] == "createTask"])

        self.client.fail_metadata = False
        result = self.writer.create(
            role="po",
            actor="operator",
            project="secretary",
            task_type="code",
            title="Launch mode",
            target="ready",
            reference="secretary-523",
            codex_launch_mode="tui",
            request_id="create-replay",
        )

        self.assertEqual(result["task"]["routing"]["codex_launch_mode"], "tui")
        self.assertEqual(create_writes, len([call for call in self.client.calls if call[0] == "createTask"]))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_ready_reset_preserves_codex_launch_mode(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        self.client.metadata[12]["codex_launch_mode"] = "tui"

        result = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="retry",
            request_id="ready-preserves-mode",
        )

        self.assertEqual(result["task"]["routing"]["codex_launch_mode"], "tui")
        self.assertEqual(self.client.metadata[12]["codex_launch_mode"], "tui")

    def test_create_rejects_invalid_codex_launch_mode_without_write(self) -> None:
        with self.assertRaisesRegex(TaskError, "codex launch mode") as raised:
            self.writer.create(
                role="po",
                actor="operator",
                project="secretary",
                task_type="code",
                title="Launch mode",
                codex_launch_mode="shell",
            )

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertFalse(any(call[0] == "createTask" for call in self.client.calls))

    def test_worker_create_ready_is_forbidden_without_backend_write(self) -> None:
        with self.assertRaisesRegex(TaskError, "only ideas") as raised:
            self.writer.create(
                role="worker",
                actor="w",
                project="secretary",
                task_type="code",
                title="Continuation",
                target="ready",
            )

        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertFalse(any(call[0] == "createTask" for call in self.client.calls))

    def test_pending_claim_replay_finishes_move_before_success_audit(self) -> None:
        self.client.metadata[12]["claim"] = ""
        self.client.fail_move = True

        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
                resolved_head="codex",
                request_id="claim-replay",
            )

        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        task = self.writer.reader.show("secretary-468")
        self.assertEqual(task["state"], "ready")
        self.assertEqual(task["claim"]["worker"], "secretary-468-runtime")

        self.client.fail_move = False
        replayed = self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="secretary-468-runtime",
            resolved_head="codex",
            request_id="claim-replay",
        )

        self.assertEqual(replayed["task"]["state"], "in_progress")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertEqual(len(events.readlines()), 1)

    def test_reconcile_finishes_pending_claim_move(self) -> None:
        self.client.metadata[12]["claim"] = ""
        self.client.fail_move = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
                resolved_head="codex",
                request_id="claim-reconcile",
            )

        self.client.fail_move = False

        self.assertEqual(self.writer.reconcile(), (1, 0))
        task = self.writer.reader.show("secretary-468")
        self.assertEqual(task["state"], "in_progress")
        self.assertEqual(task["claim"]["worker"], "secretary-468-runtime")

    def test_claim_rejects_project_code_capacity_without_write(self) -> None:
        self.client.metadata[12]["claim"] = ""
        self.client.tasks.append(
            {
                "id": 14,
                "reference": "secretary-999",
                "title": "Other code",
                "column_id": 3,
                "position": 1,
                "swimlane_id": 4,
            }
        )
        self.client.metadata[14] = {
            "project": "secretary",
            "task_type": "code",
            "claim": "other-worker",
        }

        with self.assertRaisesRegex(TaskError, "one active code task") as raised:
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
            )

        self.assertEqual(raised.exception.code, "capacity_reached")
        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.client.calls))

    def test_reviewer_verdict_uses_review_marker(self) -> None:
        result = self.writer.verdict(
            role="reviewer",
            actor="r",
            reference="secretary-468",
            kind="green",
            body="ok",
            request_id="green",
        )

        self.assertEqual(result["action"], "verdict")
        comment = [call for call in self.client.calls if call[0] == "createComment"][-1]
        self.assertEqual(comment[1]["content"], "[review:green]\nok")

    def test_validate_to_in_progress_rework_is_dispatcher_only(self) -> None:
        self.client.tasks[0]["column_id"] = 4
        self.client.metadata[12]["resolved_review_head"] = "codex-reviewer"

        result = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="in_progress",
            reason="review:red",
            request_id="rework",
        )

        self.assertEqual(result["task"]["state"], "in_progress")
        self.assertEqual(self.client.metadata[12]["resolved_review_head"], "")

    def test_completed_ready_replay_does_not_reset_metadata_again(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        self.writer.move(role="dispatcher", actor="d", reference="secretary-468", target="ready", reason="", request_id="ready-done")
        metadata_writes = len([call for call in self.client.calls if call[0] == "saveTaskMetadata"])

        self.client.tasks[0]["column_id"] = 4
        self.client.metadata[12]["claim"] = "codex-terra"
        self.client.metadata[12]["resolved_head"] = "codex-terra"
        self.client.metadata[12]["resolved_review_head"] = "codex-reviewer"
        self.client.metadata[12]["retry_same"] = "1"
        second = self.writer.move(role="dispatcher", actor="d", reference="secretary-468", target="ready", reason="", request_id="ready-done")

        self.assertEqual(second["task"]["state"], "validate")
        self.assertEqual(metadata_writes, len([call for call in self.client.calls if call[0] == "saveTaskMetadata"]))
        self.assertEqual(self.client.metadata[12]["claim"], "codex-terra")
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertEqual(len(events.readlines()), 1)

    def test_pending_blocks_export_from_the_same_data_root(self) -> None:
        self.writer.audit.stage("pending", {"request_id": "pending", "event_id": "evt_pending"})
        with self.assertRaisesRegex(RuntimeError, "unresolved pending"):
            export_board(Path(self.tmpdir.name), command=["pipeline"])


_READ_METHODS = {
    "getProjectByName", "getActiveSwimlanes", "getTaskByReference", "getTaskMetadata",
    "getAllComments", "getTask",
}


class RoutingJournalTests(unittest.TestCase):
    """secretary-716: the routing record is journal-only and must survive everything the board
    forgets: the reviewer head cleared on the way out of Validate, the routing block reset on the
    way back to Ready."""

    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)
        self.audit = TaskAudit(self.tmpdir.name)

    def _run(self, role: str, head: str):
        return head_run_from_profile(
            role=role,
            head=head,
            head_source="role_default",
            profile={"adapter": "codex", "model": "gpt-5.6-terra", "effort": "extra", "resource": "openai-sub"},
            resources={"openai-sub": {"account": "openai-subscription"}},
        )

    def _payload(self, attempt: int, phase: str, *heads: tuple[str, str], outcome: str = "") -> dict:
        return routing_payload(
            attempt=attempt,
            attempt_id="att-1",
            phase=phase,
            heads=[self._run(role, head) for role, head in heads],
            outcome=outcome,
        )

    def test_routing_writes_the_journal_without_touching_the_board(self) -> None:
        self.writer.routing(
            role="dispatcher", actor="pilot", reference="secretary-468",
            payload=self._payload(1, "worker", ("worker", "codex")), request_id="routing-1",
        )

        events = self.audit.events("secretary-468", kind="routing")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["heads"][0]["head"], "codex")
        self.assertEqual(
            [call for call in self.client.calls if call[0] not in _READ_METHODS], [],
            "a routing record is telemetry; it must not mutate the card",
        )

    def test_repeated_routing_record_commits_once(self) -> None:
        for _ in range(2):
            self.writer.routing(
                role="dispatcher", actor="pilot", reference="secretary-468",
                payload=self._payload(1, "worker", ("worker", "codex")), request_id="routing-1",
            )

        self.assertEqual(len(self.audit.events("secretary-468", kind="routing")), 1)

    def test_only_the_dispatcher_may_write_routing(self) -> None:
        with self.assertRaisesRegex(TaskError, "not permitted"):
            self.writer.routing(
                role="worker", actor="w", reference="secretary-468",
                payload=self._payload(1, "worker", ("worker", "codex")),
            )

    def test_routing_rejects_an_unknown_phase_and_an_empty_head_list(self) -> None:
        with self.assertRaisesRegex(TaskError, "unknown routing phase"):
            self.writer.routing(
                role="dispatcher", actor="pilot", reference="secretary-468",
                payload={"attempt": 1, "phase": "guess", "heads": [{"role": "worker"}]},
            )
        with self.assertRaisesRegex(TaskError, "at least one head"):
            self.writer.routing(
                role="dispatcher", actor="pilot", reference="secretary-468",
                payload={"attempt": 1, "phase": "worker", "heads": []},
            )

    def test_attempts_rebuild_the_pairs_and_their_outcomes(self) -> None:
        for attempt, outcome in ((1, "red"), (2, "green")):
            self.writer.routing(
                role="dispatcher", actor="pilot", reference="secretary-468",
                payload=self._payload(attempt, "worker", ("worker", "codex")),
                request_id=f"routing-worker-{attempt}",
            )
            self.writer.routing(
                role="dispatcher", actor="pilot", reference="secretary-468",
                payload=self._payload(attempt, "review", ("reviewer", "codex-reviewer")),
                request_id=f"routing-review-{attempt}",
            )
            self.writer.routing(
                role="dispatcher", actor="pilot", reference="secretary-468",
                payload=self._payload(
                    attempt, "verdict", ("worker", "codex"), ("reviewer", "codex-reviewer"),
                    outcome=outcome,
                ),
                request_id=f"routing-verdict-{attempt}",
            )

        history = attempts(self.audit.events("secretary-468", kind="routing"))
        self.assertEqual([record.attempt for record in history], [1, 2])
        self.assertEqual([record.outcome for record in history], ["red", "green"])
        self.assertEqual([record.worker.head for record in history], ["codex", "codex"])
        self.assertEqual([record.reviewer.head for record in history], ["codex-reviewer"] * 2)

    def test_a_head_without_a_model_must_say_the_cli_resolved_it(self) -> None:
        """The blank-model guard: a record may only omit the model when it names the runtime that
        picked one, so `claude-default` can never be journalled as a silent empty string."""
        with self.assertRaisesRegex(ValueError, "unpinned model"):
            HeadRun(role="worker", head="claude-default", model_source="profile")

        unpinned = head_run_from_profile(
            role="reviewer",
            head="claude-default",
            head_source="card",
            profile={"adapter": "claude", "resource": "claude-sub"},
            resources={"claude-sub": {"account": "claude-subscription"}},
        )

        self.assertEqual((unpinned.model, unpinned.model_source), ("", "cli_default"))


class ReportDurabilityGateTests(unittest.TestCase):
    """`report --kind done` refuses to run from a dirty workspace (secretary-653).

    The gate lives in the worker's own session so it can commit and retry, instead of
    learning from the dispatcher post-factum that the card went to blocked."""

    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.workspace = Path(self.tmpdir.name) / "workspace"
        self.workspace.mkdir()
        for args in (
            ["init", "-q"],
            ["config", "user.email", "worker@example.invalid"],
            ["config", "user.name", "worker"],
        ):
            subprocess.run(["git", "-C", str(self.workspace), *args], check=True, capture_output=True)
        (self.workspace / "code.py").write_text("print(1)\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-qm", "work"], check=True, capture_output=True
        )
        self.writer = TaskWriter(
            self.client,  # type: ignore[arg-type]
            data_dir=str(Path(self.tmpdir.name) / "data"),
            workspace=str(self.workspace),
        )

    def _report(self, kind: str, body: str = "ready") -> dict:
        return self.writer.report(role="worker", actor="w", reference="secretary-468", kind=kind, body=body)

    def test_clean_workspace_reports_done(self) -> None:
        self.assertEqual(self._report("done")["action"], "reported")

    def test_dirty_workspace_is_refused_without_touching_the_board(self) -> None:
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")
        with self.assertRaises(TaskError) as caught:
            self._report("done")
        self.assertEqual(caught.exception.code, "uncommitted")
        self.assertNotEqual(caught.exception.exit_code, 0)
        self.assertIn("code.py", caught.exception.message)
        self.assertIn("commit", caught.exception.message)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_untracked_file_is_refused(self) -> None:
        (self.workspace / "scratch.py").write_text("print(3)\n", encoding="utf-8")
        with self.assertRaises(TaskError) as caught:
            self._report("done")
        self.assertEqual(caught.exception.code, "uncommitted")
        self.assertIn("scratch.py", caught.exception.message)

    def test_runtime_audit_tail_does_not_block_done(self) -> None:
        board = self.workspace / "secretary-data" / "board"
        board.mkdir(parents=True)
        (board / "events.ndjson").write_text("{}\n", encoding="utf-8")
        self.assertEqual(self._report("done")["action"], "reported")

    def test_blocked_report_is_not_gated(self) -> None:
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")
        self.assertEqual(self._report("blocked", body="stuck on the adapter")["action"], "reported")

    def test_non_git_workspace_is_not_gated(self) -> None:
        plain = Path(self.tmpdir.name) / "plain"
        plain.mkdir()
        writer = TaskWriter(
            self.client,  # type: ignore[arg-type]
            data_dir=str(Path(self.tmpdir.name) / "data"),
            workspace=str(plain),
        )
        result = writer.report(role="worker", actor="w", reference="secretary-468", kind="done", body="ok")
        self.assertEqual(result["action"], "reported")

    def test_cwd_is_the_default_workspace(self) -> None:
        writer = TaskWriter(self.client, data_dir=str(Path(self.tmpdir.name) / "data"))  # type: ignore[arg-type]
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")
        with mock.patch("secretary.tasks.Path.cwd", return_value=self.workspace):
            with self.assertRaises(TaskError) as caught:
                writer.report(role="worker", actor="w", reference="secretary-468", kind="done", body="ok")
        self.assertEqual(caught.exception.code, "uncommitted")
