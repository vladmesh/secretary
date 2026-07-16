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
                "codex_launch_mode": "tui",
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

    def __init__(self) -> None:
        super().__init__()
        self.comments: dict[int, list[dict[str, object]]] = {
            int(task["id"]): [] for task in self.tasks
        }

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
            if "reference" in params:
                task["reference"] = params["reference"]
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

    def test_restore_comment_retry_after_lost_reply_does_not_duplicate_history(self) -> None:
        self.client.lose_comment_reply = True
        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.restore_comment(
                reference="secretary-468", body="[report:done]\\nrestored", index=0,
                request_id="restore-comment-lost-reply",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(len(self.client.comments[12]), 1)

        self.client.lose_comment_reply = False
        self.writer.restore_comment(
            reference="secretary-468", body="[report:done]\\nrestored", index=0,
            request_id="restore-comment-lost-reply",
        )

        self.assertEqual(len(self.client.comments[12]), 1)
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertNotIn("restore_body", events.read())

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
