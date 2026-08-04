from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import main
from secretary.data import export_board
from secretary.sprints import refresh_active_sprint_projects
from secretary.routing_journal import (
    HeadRun,
    attempts,
    head_run_from_profile,
    routing_payload,
)
from secretary.tasks import (
    KanboardClient,
    TaskAudit,
    TaskError,
    TaskReader,
    TaskWriter,
    standing_decision,
    _STATE_BY_COLUMN,
    _STATES,
    _TRANSITIONS,
)
from tests.observer_identity import as_observer, bind_observer, unbound_observer


@contextlib.contextmanager
def open_sprint(ref: str = "sprint:test", project: str = "secretary"):
    """Stand in for the open sprint every Ready card needs.

    These tests are about the create and audit path; the sprint link is a precondition of a
    create on the board, and the guard behind it is covered in tests/test_sprints.py.

    The caller is bound to the same sprint, because the observer creating a card linked to it is
    that sprint's own head; an unbound caller is refused before the create path is reached.
    """
    sprint = {"ref": ref, "status": "open", "repositories": [project], "reservations": [project]}
    with mock.patch("secretary.sprints.SprintReader.show", return_value=sprint), as_observer(ref):
        yield ref


# The sprint the assessment fixture's card belongs to.
SPRINT = "sprint:1031"


class FakeSprintReader:
    """The sprint board as the task writer's reservation guard reads it: one open sprint."""

    def __init__(self, sprint: dict[str, object]) -> None:
        self.sprint = sprint

    def list(self, **kwargs: object) -> list[dict[str, object]]:
        return [self.sprint]

    def show(self, reference: str, **kwargs: object) -> dict[str, object]:
        if reference != self.sprint["ref"]:
            raise TaskError("not_found", f"no sprint {reference}", 3)
        return self.sprint


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
            return [{"id": 1, "title": "Issues"}, {"id": 2, "title": "Ready"}]
        if method == "getActiveSwimlanes":
            return [{"id": 4, "name": "Secretary"}]
        if method == "getAllTasks":
            status = params.get("status_id")
            if status not in {0, 1}:
                return []
            return [
                task for task in self.tasks
                if (int(task.get("is_active", task.get("status", 1)) or 0) != 0) == (status == 1)
            ]
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

    def test_show_prefers_live_duplicate_reference(self) -> None:
        archived = {
            "id": 14, "reference": "secretary-784", "title": "Archived", "column_id": 6,
            "position": 1, "swimlane_id": 4, "is_active": 0,
        }
        live = {
            "id": 15, "reference": "secretary-784", "title": "Live", "column_id": 2,
            "position": 1, "swimlane_id": 4, "is_active": 1,
        }
        self.client.tasks.extend([archived, live])
        self.client.metadata.update({14: {}, 15: {"project": "secretary"}})

        task = self.reader.show("secretary-784")

        self.assertEqual(task["id"], "task_kanboard_15")
        self.assertEqual(task["title"], "Live")
        self.assertEqual(
            [params["status_id"] for method, params in self.client.calls if method == "getAllTasks"],
            [1],
        )

    def test_show_returns_archived_reference_when_no_live_duplicate_exists(self) -> None:
        self.client.tasks[1]["is_active"] = 0

        task = self.reader.show("old-1")

        self.assertEqual(task["id"], "task_kanboard_13")

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
                {"id": 1, "title": "Issues"}, {"id": 2, "title": "Ready"},
                {"id": 3, "title": "In progress"}, {"id": 4, "title": "Validate"},
                {"id": 7, "title": "Assessment"},
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
                "reference": params.get("reference", ""),
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
        with self.assertRaisesRegex(TaskError, "Ready or Blocked") as raised:
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

    def test_restore_placement_uses_live_duplicate_reference(self) -> None:
        archived = self.client.tasks[0]
        archived.update({"column_id": 3, "position": 1, "is_active": 0})
        live = {
            "id": 15, "reference": "secretary-468", "title": "Live", "description": "",
            "column_id": 3, "position": 2, "swimlane_id": 4, "is_active": 1,
            "date_creation": "1720000000", "date_modification": "1720000000",
        }
        self.client.tasks.append(live)
        self.client.metadata[15] = {"project": "secretary", "task_type": "code"}
        self.client.comments[15] = []

        self.writer.restore_card(
            reference="secretary-468", metadata={"claim": "restored"}, target="in_progress", position=1
        )

        moves = [params for method, params in self.client.calls if method == "moveTaskPosition"]
        self.assertEqual(moves[-1]["task_id"], 15)

    def test_pending_create_repairs_legacy_orphaned_reference_by_recorded_id(self) -> None:
        self.client.fail_metadata = True
        with open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.create(
                    role="observer", actor="observer", project="secretary", task_type="code",
                    title="Restore", reference="secretary-restore", request_id="restore-create",
                    sprint=sprint,
                )
            # A staged event left by the pre-atomic create path has the id but not the ref.
            self.client.tasks[-1]["reference"] = ""
            pending = self.writer.audit.pending_event("restore-create")
            assert pending is not None
            pending["backend"].pop("reference_assignment")
            self.writer.audit.stage("restore-create", pending)
            self.assertEqual(self.client.tasks[-1]["reference"], "")
            self.client.fail_metadata = False

            result = self.writer.create(
                role="observer", actor="observer", project="secretary", task_type="code",
                title="Restore", reference="secretary-restore", request_id="restore-create",
                sprint=sprint,
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
        with open_sprint() as sprint:
            result = self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Launch mode",
                description="body",
                target="ready",
                reference="secretary-522",
                head="codex-extra",
                codex_launch_mode="tui",
                request_id="create-tui",
                sprint=sprint,
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

    def test_auto_reference_uses_board_wide_project_high_water_mark(self) -> None:
        # The new Kanboard row will be 14, which is already a historical reference.
        self.client.tasks[0]["reference"] = "secretary-14"
        self.client.tasks.append(
            {
                "id": 10, "reference": "secretary-1158", "title": "Archived", "column_id": 6,
                "position": 1, "swimlane_id": 4, "is_active": 0,
            }
        )
        self.client.tasks.extend([
            {"id": 9, "reference": "secretary-nope", "title": "Malformed", "column_id": 2, "position": 2, "swimlane_id": 4},
            {"id": 8, "reference": "other-999", "title": "Other project", "column_id": 2, "position": 3, "swimlane_id": 4},
        ])
        self.client.metadata.update({8: {}, 9: {}, 10: {}})
        self.client.comments.update({8: [], 9: [], 10: []})

        with mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True), open_sprint() as sprint:
            result = self.writer.create(
                role="observer", actor="observer", project="secretary", task_type="code",
                title="Auto reference", request_id="auto-reference", sprint=sprint,
            )

        self.assertEqual(result["task"]["ref"], "secretary-1159")
        self.assertNotEqual(result["task"]["ref"], "secretary-14")
        self.assertEqual(
            [params["status_id"] for method, params in self.client.calls if method == "getAllTasks"][:2],
            [1, 0],
        )

    def test_auto_reference_serializes_concurrent_creates(self) -> None:
        first_create_started = threading.Event()
        release_first_create = threading.Event()
        second_reached_board = threading.Event()
        original_call = self.client.call
        first_create = True
        results: list[dict[str, object]] = []
        failures: list[BaseException] = []

        def paused_first_create(method: str, **params: object) -> object:
            nonlocal first_create
            if method == "createTask" and first_create:
                first_create = False
                first_create_started.set()
                if not release_first_create.wait(2):
                    raise AssertionError("first create was not released")
            elif method == "getProjectByName" and first_create_started.is_set():
                second_reached_board.set()
            return original_call(method, **params)

        def create(writer: TaskWriter, request_id: str, sprint: str) -> None:
            try:
                results.append(writer.create(
                    role="po", actor="operator", project="secretary", task_type="code",
                    title=request_id, target="ready", request_id=request_id, sprint=sprint,
                    sprint_override=True, sprint_override_reason="concurrent allocation test",
                ))
            except BaseException as exc:  # Preserve thread failures for the assertion below.
                failures.append(exc)

        with mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True), \
             open_sprint() as sprint, \
             mock.patch.object(self.client, "call", side_effect=paused_first_create):
            first = threading.Thread(target=create, args=(self.writer, "first-auto-reference", sprint))
            first.start()
            self.assertTrue(first_create_started.wait(2))
            second = threading.Thread(
                target=create,
                args=(TaskWriter(self.client, data_dir=self.tmpdir.name), "second-auto-reference", sprint),
            )
            second.start()
            self.assertFalse(second_reached_board.wait(0.2))
            release_first_create.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(sorted(result["task"]["ref"] for result in results), ["secretary-469", "secretary-470"])

    def test_auto_reference_enumeration_failure_writes_no_card(self) -> None:
        original_call = self.client.call

        def invalid_task_list(method: str, **params: object) -> object:
            if method == "getAllTasks":
                return {"unexpected": "shape"}
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=invalid_task_list), \
             mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True), \
             open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "invalid task list") as raised:
                self.writer.create(
                    role="observer", actor="observer", project="secretary", task_type="code",
                    title="No fallback", request_id="auto-reference-failure", sprint=sprint,
                )

        self.assertEqual(raised.exception.code, "backend_error")
        self.assertFalse(any(method == "createTask" for method, _params in self.client.calls))

    def test_auto_reference_refuses_null_or_false_enumeration(self) -> None:
        original_call = self.client.call

        for reply in (None, False):
            with self.subTest(reply=reply), \
                 mock.patch.object(
                     self.client,
                     "call",
                     side_effect=lambda method, **params: reply if method == "getAllTasks" else original_call(method, **params),
                 ), \
                 mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True), \
                 open_sprint() as sprint:
                with self.assertRaisesRegex(TaskError, "invalid task list") as raised:
                    self.writer.create(
                        role="observer", actor="observer", project="secretary", task_type="code",
                        title="No fallback", request_id=f"null-reference-{reply}", sprint=sprint,
                    )

            self.assertEqual(raised.exception.code, "backend_error")
            self.assertFalse(any(method == "createTask" for method, _params in self.client.calls))

    def test_create_passes_reference_to_atomic_backend_write(self) -> None:
        with mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True), open_sprint() as sprint:
            result = self.writer.create(
                role="observer", actor="observer", project="secretary", task_type="code",
                title="Atomic reference", request_id="atomic-reference", sprint=sprint,
            )

        created = [params for method, params in self.client.calls if method == "createTask"]
        self.assertEqual(created[-1]["reference"], result["task"]["ref"])
        self.assertFalse(any(method == "updateTask" for method, _params in self.client.calls))

    def test_pending_atomic_create_recovers_after_backend_id_audit_crash(self) -> None:
        original_stage = self.writer.audit.stage
        stages = 0

        def lose_backend_id_stage(request_id: str, event: dict[str, object]) -> None:
            nonlocal stages
            stages += 1
            if stages == 3:
                raise OSError("lost after create")
            original_stage(request_id, event)  # type: ignore[arg-type]

        with mock.patch.object(self.writer.audit, "stage", side_effect=lose_backend_id_stage), open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.create(
                    role="observer", actor="observer", project="secretary", task_type="code",
                    title="Crash safe", request_id="atomic-create-crash", sprint=sprint,
                )

        self.assertEqual(self.client.tasks[-1]["reference"], "secretary-469")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        with open_sprint() as sprint:
            result = self.writer.create(
                role="observer", actor="observer", project="secretary", task_type="code",
                title="Crash safe", request_id="atomic-create-crash", sprint=sprint,
            )

        self.assertEqual(result["task"]["ref"], "secretary-469")
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 1)
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_sigint_before_atomic_create_does_not_adopt_later_unrelated_reference(self) -> None:
        original_call = self.client.call

        def interrupt_create(method: str, **params: object) -> object:
            if method == "createTask":
                raise KeyboardInterrupt()
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=interrupt_create), open_sprint() as sprint:
            with self.assertRaises(KeyboardInterrupt):
                self.writer.create(
                    role="observer", actor="observer", project="secretary", task_type="code",
                    title="Interrupted before create", description="never reached board",
                    request_id="sigint-before-create", sprint=sprint,
                )

        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.client.tasks.append({
            "id": 99, "reference": "secretary-469", "title": "Later unrelated", "description": "different",
            "column_id": 2, "position": 1, "swimlane_id": 4, "is_active": 1,
        })
        self.client.metadata[99] = {}
        self.client.comments[99] = []

        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertEqual(self.client.metadata[99], {})
        self.assertFalse(any(method == "updateTask" for method, _params in self.client.calls))

    def test_backend_ignoring_atomic_reference_leaves_pending_create_unrepaired(self) -> None:
        original_call = self.client.call

        def ignore_reference(method: str, **params: object) -> object:
            if method == "createTask":
                params.pop("reference", None)
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=ignore_reference), open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.create(
                    role="observer", actor="observer", project="secretary", task_type="code",
                    title="Reference must persist", request_id="ignored-atomic-reference", sprint=sprint,
                )

        self.assertEqual(self.client.tasks[-1]["reference"], "")
        self.assertEqual(self.client.metadata[int(self.client.tasks[-1]["id"])], {})
        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertFalse(any(method == "updateTask" for method, _params in self.client.calls))

    def test_pending_create_does_not_repair_a_different_task_with_its_reference(self) -> None:
        self.client.fail_metadata = True
        with open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.create(
                    role="observer", actor="observer", project="secretary", task_type="code",
                    title="Interrupted", reference="secretary-interrupted", request_id="interrupted-create",
                    sprint=sprint,
                )
        intended = self.client.tasks[-1]
        intended["reference"] = ""
        self.client.tasks.append({
            "id": 99, "reference": "secretary-interrupted", "title": "Different", "column_id": 2,
            "position": 1, "swimlane_id": 4, "is_active": 1,
        })
        self.client.metadata[99] = {}
        self.client.comments[99] = []
        self.client.fail_metadata = False

        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertEqual(self.client.metadata[int(intended["id"])], {})
        self.assertEqual(self.client.metadata[99], {})
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

    def test_explicit_reference_collision_is_still_refused(self) -> None:
        with open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "task reference already exists") as raised:
                self.writer.create(
                    role="observer", actor="observer", project="secretary", task_type="code",
                    title="Duplicate", reference="secretary-468", request_id="explicit-collision",
                    sprint=sprint,
                )

        self.assertEqual(raised.exception.code, "validation")
        self.assertFalse(any(method == "createTask" for method, _params in self.client.calls))

    def test_pending_create_replay_restores_metadata_before_audit(self) -> None:
        self.client.fail_metadata = True
        with open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.create(
                    role="observer",
                    actor="observer",
                    project="secretary",
                    task_type="code",
                    title="Launch mode",
                    target="ready",
                    reference="secretary-523",
                    codex_launch_mode="tui",
                    request_id="create-replay",
                    sprint=sprint,
                )
            self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
            create_writes = len([call for call in self.client.calls if call[0] == "createTask"])

            self.client.fail_metadata = False
            result = self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Launch mode",
                target="ready",
                reference="secretary-523",
                codex_launch_mode="tui",
                request_id="create-replay",
                sprint=sprint,
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
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Launch mode",
                codex_launch_mode="shell",
            )

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertFalse(any(call[0] == "createTask" for call in self.client.calls))

    def test_worker_create_ready_is_forbidden_without_backend_write(self) -> None:
        with self.assertRaisesRegex(TaskError, "only proposals in Issues") as raised:
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

    def test_claim_counts_a_parked_card_as_an_active_code_task(self) -> None:
        """A parked card holds a retained worker and its checkout: a second writer in the same
        project is as wrong there as it is in Validate."""
        self.client.metadata[12]["claim"] = ""
        self.client.tasks.append(
            {
                "id": 14,
                "reference": "secretary-999",
                "title": "Parked code",
                "column_id": 7,  # Assessment
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

    def test_archive_refuses_a_parked_card(self) -> None:
        """Assessment is a wait, not a resting place: the worker and workspace are still owned."""
        self.client.metadata[12]["claim"] = ""
        self.client.tasks[0]["column_id"] = 7

        with self.assertRaisesRegex(TaskError, "live worker or reviewer") as raised:
            self.writer.archive(
                role="po", actor="operator", reference="secretary-468", reason="cleanup"
            )

        self.assertEqual(raised.exception.code, "live_work")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

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


class AssessmentStateTests(unittest.TestCase):
    """secretary-1025/1031: the durable wait between a reviewer verdict and the observer's decision.

    These pin the model: who may move a card in and out of the column, that the column
    round-trips through the state map, and that a card only leaves it on a decision somebody
    recorded.
    """

    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)

    def reserve_project(
        self, *, card_sprint: str = SPRINT, project: str = "secretary", data_dir: str = "",
    ) -> None:
        """Put the card in an open sprint that reserves its project.

        That reservation is what entitles an observer to decide about the card, so the tests of
        the decision path set it up as the board would have it: the guard index and the live
        sprint row both naming the sprint the card is linked to.

        The caller is bound to the card's own sprint, which is the head that would be deciding
        here. A test about a caller from elsewhere binds its own.
        """
        bind_observer(self, card_sprint)
        self.client.metadata[12]["sprint_ref"] = card_sprint
        reader = FakeSprintReader({"ref": SPRINT, "status": "open", "reservations": [project]})
        patcher = mock.patch("secretary.sprints.SprintReader", return_value=reader)
        patcher.start()
        self.addCleanup(patcher.stop)
        refresh_active_sprint_projects(data_dir or self.tmpdir.name, reader)

    def test_column_order_and_state_map(self) -> None:
        self.assertEqual(
            _STATES,
            ("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done"),
        )
        self.assertEqual(_STATE_BY_COLUMN["Assessment"], "assessment")
        self.assertEqual(
            list(_STATE_BY_COLUMN),
            ["Issues", "Ready", "In progress", "Validate", "Assessment", "Blocked", "Done"],
        )

    def test_dispatcher_transitions_are_exact(self) -> None:
        """Pinned exactly: a later card must widen this table deliberately, not by accident."""
        self.assertEqual(
            _TRANSITIONS["dispatcher"],
            {
                ("in_progress", "validate"), ("in_progress", "blocked"),
                ("in_progress", "ready"), ("validate", "in_progress"),
                ("validate", "blocked"), ("validate", "done"),
                ("validate", "assessment"), ("assessment", "in_progress"),
                ("assessment", "done"), ("assessment", "blocked"),
            },
        )

    def test_worker_and_reviewer_stay_out_of_assessment(self) -> None:
        self.assertEqual(_TRANSITIONS["worker"], set())
        self.assertEqual(_TRANSITIONS["reviewer"], set())
        for role in ("po", "observer"):
            self.assertIn(("validate", "assessment"), _TRANSITIONS[role])
        self.assertIn(("assessment", "ready"), _TRANSITIONS["po"])
        self.assertEqual(
            {edge for edge in _TRANSITIONS["steward"] if "assessment" in edge},
            {("assessment", "blocked")},
        )

    def test_the_observer_takes_no_exit_out_of_assessment(self) -> None:
        """The observer decides; the dispatcher performs. A board move by the observer would be a
        release with nothing merged, so the authority matrix has no exit for it at all."""
        self.assertEqual(
            {edge for edge in _TRANSITIONS["observer"] if edge[0] == "assessment"}, set()
        )
        self.assertIn(("validate", "assessment"), _TRANSITIONS["observer"])

    def _park(self, request_id: str = "into-assessment") -> None:
        self.client.tasks[0]["column_id"] = 4  # Validate
        entered = self.writer.move(
            role="dispatcher", actor="d", reference="secretary-468",
            target="assessment", reason="", request_id=request_id,
        )
        self.assertEqual(entered["task"]["state"], "assessment")
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

    def _decide(self, kind: str, request_id: str = "") -> dict:
        if not self.client.metadata[12].get("sprint_ref"):
            self.reserve_project()
        return self.writer.decide(
            role="observer", actor="observer", reference="secretary-468", kind=kind,
            body="the round converged", request_id=request_id or f"decision-{kind}",
        )

    def test_dispatcher_moves_a_card_into_and_out_of_assessment(self) -> None:
        self._park()
        self._decide("rework")

        left = self.writer.move(
            role="dispatcher", actor="d", reference="secretary-468",
            target="in_progress", reason="", decision="rework", request_id="out-of-assessment",
        )
        self.assertEqual(left["task"]["state"], "in_progress")
        self.assertEqual(self.client.tasks[0]["column_id"], 3)

    def test_a_release_with_no_recorded_decision_is_refused(self) -> None:
        """The seam's whole point: nothing acts on a parked card that nobody decided about."""
        self._park()

        with self.assertRaisesRegex(TaskError, "recorded decision") as raised:
            self.writer.move(
                role="dispatcher", actor="d", reference="secretary-468", target="done",
                reason="", request_id="undecided-release",
            )

        self.assertEqual(raised.exception.code, "decision_required")
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

    def test_a_move_naming_a_decision_nobody_recorded_is_refused(self) -> None:
        """Carrying the word is not deciding: the audit is what the refusal reads."""
        self._park()

        with self.assertRaisesRegex(TaskError, "no release decision is recorded"):
            self.writer.move(
                role="dispatcher", actor="d", reference="secretary-468", target="done",
                reason="", decision="release", request_id="claimed-release",
            )

        self.assertEqual(self.client.tasks[0]["column_id"], 7)

    def test_a_decision_from_an_earlier_parking_does_not_release_a_later_one(self) -> None:
        """A decision is about the round it was written for, not about every later round."""
        self._park()
        self._decide("release")
        self.writer.move(
            role="dispatcher", actor="d", reference="secretary-468", target="done",
            reason="", decision="release", request_id="first-release",
        )
        self._park(request_id="parked-again")

        with self.assertRaisesRegex(TaskError, "no release decision is recorded"):
            self.writer.move(
                role="dispatcher", actor="d", reference="secretary-468", target="done",
                reason="", decision="release", request_id="replayed-release",
            )

    def test_a_decision_is_recorded_on_the_card_and_in_the_audit(self) -> None:
        self._park()

        decided = self._decide("reslice")

        self.assertEqual(decided["action"], "decided")
        comment = decided["task"]["comments"][-1]
        self.assertEqual(comment["marker"], "decision:reslice")
        self.assertIn("the round converged", comment["body"])
        event = TaskAudit(Path(self.tmpdir.name)).events("secretary-468", kind="decided")[-1]
        self.assertEqual(event["payload"]["decision"], "reslice")
        self.assertEqual(event["actor"], {"role": "observer", "id": "observer"})

    def test_a_decision_needs_a_parked_card_a_reason_and_a_permitted_role(self) -> None:
        self._park()
        with self.assertRaisesRegex(TaskError, "non-empty reason"):
            self.writer.decide(
                role="observer", actor="observer", reference="secretary-468",
                kind="release", body="  ", request_id="empty-reason",
            )
        with self.assertRaisesRegex(TaskError, "decision must be one of"):
            self.writer.decide(
                role="observer", actor="observer", reference="secretary-468",
                kind="merge", body="ship it", request_id="unknown-kind",
            )
        with self.assertRaisesRegex(TaskError, "role is not permitted"):
            self.writer.decide(
                role="worker", actor="w", reference="secretary-468",
                kind="release", body="ship it", request_id="worker-decision",
            )
        # The card leaves the column. Its project stays reserved by the observer's own sprint, so
        # what refuses this is the state and not the reservation.
        self.reserve_project()
        self.client.tasks[0]["column_id"] = 4
        with self.assertRaisesRegex(TaskError, "only recorded on a card in Assessment"):
            self.writer.decide(
                role="observer", actor="observer", reference="secretary-468",
                kind="release", body="ship it", request_id="unparked-decision",
            )

    def test_a_blocked_escalation_out_of_assessment_needs_no_decision(self) -> None:
        """Blocked stays reachable without one: it is what rescues a card nobody decided about."""
        self._park()

        escalated = self.writer.move(
            role="dispatcher", actor="d", reference="secretary-468", target="blocked",
            reason="the release could not land", request_id="parked-card-blocked",
        )

        self.assertEqual(escalated["task"]["state"], "blocked")

    def test_only_the_observer_decides(self) -> None:
        """One authority for the decision. A PO that has to intervene overrides visibly."""
        self._park()

        with self.assertRaisesRegex(TaskError, "role is not permitted"):
            self.writer.decide(
                role="po", actor="operator", reference="secretary-468",
                kind="release", body="ship it", request_id="po-decision",
            )

    def test_a_decision_moves_the_card_where_that_decision_goes(self) -> None:
        """A recorded release paired with a move back to In progress is a rework nobody decided."""
        self._park()
        self._decide("release")

        with self.assertRaisesRegex(TaskError, "release decision moves the card to done") as raised:
            self.writer.move(
                role="dispatcher", actor="d", reference="secretary-468", target="in_progress",
                reason="", decision="release", request_id="release-to-in-progress",
            )

        self.assertEqual(raised.exception.code, "decision_mismatch")
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

    def test_the_undecided_exits_from_assessment_are_closed(self) -> None:
        """Ready, Validate and Issues all leave the column with nothing decided, and Ready also
        clears the claim, which is what would let a second worker start on a reviewed checkout."""
        self._park()

        for target in ("ready", "validate", "issues"):
            with self.assertRaises(TaskError) as raised:
                self.writer.move(
                    role="dispatcher", actor="d", reference="secretary-468", target=target,
                    reason="", request_id=f"dispatcher-bypass-{target}",
                )
            self.assertIn(raised.exception.code, {"decision_required", "transition_forbidden"})
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

    def test_the_observer_may_not_perform_its_own_decision(self) -> None:
        """The observer records the decision; the dispatcher performs it.

        A matching decision is checkable, but a board move is not a release: the card would read
        Done with nothing merged, In progress with no worker relaunched. So every
        decision-carrying exit is refused to the observer, on its own sprint's card and with the
        decision standing on the card.
        """
        self._park()

        for kind, target in (("release", "done"), ("rework", "in_progress"), ("reslice", "blocked")):
            self._decide(kind, request_id=f"decision-performed-by-{kind}")
            with self.assertRaises(TaskError) as raised:
                self.writer.move(
                    role="observer", actor="observer", reference="secretary-468", target=target,
                    reason="", decision=kind, request_id=f"observer-performs-{kind}",
                )
            self.assertEqual(raised.exception.code, "transition_forbidden")
            self.assertIn("task decide", str(raised.exception))
            self.assertEqual(self.client.tasks[0]["column_id"], 7)

        # And the dispatcher performs the decision that is standing, from the same state.
        performed = self.writer.move(
            role="dispatcher", actor="d", reference="secretary-468", target="blocked",
            reason="", decision="reslice", request_id="dispatcher-performs-reslice",
        )
        self.assertEqual(performed["task"]["state"], "blocked")

    def test_a_decision_needs_an_open_sprint_to_hold_the_project(self) -> None:
        """A decision is refused where no open sprint holds the card's project, the reservation
        `move` already checks. What it does not do is say who the caller is: see the test below.
        """
        self._park()
        # A bound caller, so what is being tested is the reservation and not the identity: this
        # observer is somebody's head, and the card it reaches for is held by no open sprint.
        bind_observer(self, SPRINT)

        with self.assertRaisesRegex(TaskError, "role is not permitted") as unheld:
            self.writer.decide(
                role="observer", actor="observer", reference="secretary-468",
                kind="release", body="ship it", request_id="decision-without-a-sprint",
            )
        self.assertEqual(unheld.exception.code, "role_forbidden")

        # A card linked to another sprint than the one holding its project is refused too, and
        # refused as the reservation it crosses.
        self.reserve_project(card_sprint="sprint:1030")
        with self.assertRaises(TaskError) as other:
            self.writer.decide(
                role="observer", actor="observer", reference="secretary-468",
                kind="release", body="ship it", request_id="decision-from-another-sprint",
            )
        self.assertEqual(other.exception.code, "sprint_write_forbidden")
        self.assertEqual(standing_decision(TaskAudit(Path(self.tmpdir.name)).events("secretary-468")), "")

    def test_the_decision_guard_also_places_the_caller(self) -> None:
        """The other half of the guard: which sprint's observer is writing.

        Every observer process still runs as `--role observer --actor observer`, so the actor id
        places nobody. The sprint its head was launched for does: the card's own observer decides,
        and a head of another sprint is refused as the identity failure it is.
        """
        self._park()
        self.reserve_project()

        decided = self.writer.decide(
            role="observer", actor="observer", reference="secretary-468", kind="release",
            body="deciding from this card's own head", request_id="decision-from-its-own-head",
        )

        self.assertEqual(decided["action"], "decided")
        event = TaskAudit(Path(self.tmpdir.name)).events("secretary-468", kind="decided")[-1]
        self.assertEqual(event["actor"], {"role": "observer", "id": "observer"})

        self._park(request_id="park-again")
        with as_observer("sprint:2000"):
            with self.assertRaises(TaskError) as stranger:
                self.writer.decide(
                    role="observer", actor="observer", reference="secretary-468",
                    kind="release", body="deciding about a sprint I do not observe",
                    request_id="decision-from-another-head",
                )
        self.assertEqual(stranger.exception.code, "observer_sprint_mismatch")
        denial = TaskAudit(Path(self.tmpdir.name)).events("secretary-468", kind="sprint_guard_denied")[-1]
        self.assertEqual(denial["payload"]["code"], "observer_sprint_mismatch")
        self.assertEqual(denial["payload"]["sprint"], "sprint:2000")

        with self.assertRaises(TaskError) as unbound:
            with unbound_observer():
                self.writer.decide(
                    role="observer", actor="observer", reference="secretary-468",
                    kind="release", body="deciding from a head nobody bound",
                    request_id="decision-from-an-unbound-head",
                )
        self.assertEqual(unbound.exception.code, "observer_identity_unbound")

    def test_a_po_override_still_takes_a_parked_card_back_to_ready(self) -> None:
        """The escape hatch stays open, and it is recorded as the override it is."""
        self._park()

        requeued = self.writer.move(
            role="po", actor="operator", reference="secretary-468", target="ready",
            reason="taking this one back by hand", request_id="po-requeue",
        )

        self.assertEqual(requeued["task"]["state"], "ready")

    def test_a_po_override_takes_a_parked_card_to_the_decided_targets_too(self) -> None:
        """The escape hatch is the whole exit, not the two thirds of it that need nothing decided.

        A seam stuck with no observer to release it is exactly when an operator has to finish or
        return a parked card by hand, and Done and In progress are where it would send it. Only
        the dispatcher is held to a recorded decision, because only the dispatcher performs one.
        """
        self.reserve_project()
        for target, request_id in (("done", "po-release"), ("in_progress", "po-return")):
            self._park(request_id=f"{request_id}-park")

            moved = self.writer.move(
                role="po", actor="operator", reference="secretary-468", target=target,
                reason="finishing this one by hand", sprint_override=True,
                sprint_override_reason="no observer is coming back for it",
                request_id=request_id,
            )

            self.assertEqual(moved["task"]["state"], target)

    def test_a_po_move_out_of_assessment_still_checks_a_decision_it_names(self) -> None:
        """Not being held to a decision is not licence to invent one: a decision the PO passes is
        read against the card and its destination like anybody else's."""
        self._park()

        with self.assertRaisesRegex(TaskError, "no release decision is recorded"):
            self.writer.move(
                role="po", actor="operator", reference="secretary-468", target="done",
                reason="", decision="release", request_id="po-claimed-release",
            )
        self._decide("release")
        with self.assertRaises(TaskError) as mismatched:
            self.writer.move(
                role="po", actor="operator", reference="secretary-468", target="in_progress",
                reason="", decision="release", sprint_override=True,
                sprint_override_reason="stepping in on a reserved project",
                request_id="po-mismatched-release",
            )

        self.assertEqual(mismatched.exception.code, "decision_mismatch")
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

    def test_worker_may_not_move_a_card_out_of_assessment(self) -> None:
        self.client.tasks[0]["column_id"] = 7
        with self.assertRaisesRegex(TaskError, "may not move") as raised:
            self.writer.move(
                role="worker", actor="w", reference="secretary-468", target="done", reason="",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))

    def test_steward_escalates_an_assessment_card_with_a_reason(self) -> None:
        self.client.tasks[0]["column_id"] = 7
        with self.assertRaisesRegex(TaskError, "non-empty reason"):
            self.writer.move(
                role="steward", actor="s", reference="secretary-468", target="blocked", reason="",
            )
        escalated = self.writer.move(
            role="steward", actor="s", reference="secretary-468", target="blocked",
            reason="the observer never came back", request_id="assessment-escalation",
        )
        self.assertEqual(escalated["task"]["state"], "blocked")
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], "blocked")

    def _move_cli(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("secretary.task_commands.KanboardClient", return_value=self.client), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main([
                "task", "move", "--ref", "secretary-468",
                "--data-dir", str(Path(self.tmpdir.name) / "data"), *arguments,
            ])
        return code, output.getvalue(), errors.getvalue()

    def test_cli_move_target_assessment_moves_the_card(self) -> None:
        """Criterion 3 spells this `--target`; `--to` is the same argument under another name."""
        self.client.tasks[0]["column_id"] = 4  # Validate
        code, output, errors = self._move_cli(
            "--role", "dispatcher", "--target", "assessment", "--request-id", "cli-target",
        )

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["action"], "moved")
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

        # The way back out is the decision path, through the CLI as well: the writer checks
        # `--decision` against the audit, so the recorded decision has to come first.
        code, output, errors = self._move_cli(
            "--role", "dispatcher", "--to", "done", "--request-id", "cli-to-undecided",
        )
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(errors)["error"]["code"], "decision_required")

        # The CLI writes its audit and its sprint guard index under its own data dir, so both the
        # decision and the reservation that authorizes it have to be set up there.
        self.reserve_project(data_dir=str(Path(self.tmpdir.name) / "data"))
        reason = Path(self.tmpdir.name) / "reason.md"
        reason.write_text("ship it", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("secretary.task_commands.KanboardClient", return_value=self.client), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            decided = main([
                "task", "decide", "--ref", "secretary-468", "--role", "observer",
                "--kind", "release", "--reason-file", str(reason),
                "--data-dir", str(Path(self.tmpdir.name) / "data"), "--request-id", "cli-decision",
            ])
        self.assertEqual((decided, errors.getvalue()), (0, ""))
        self.assertEqual(json.loads(output.getvalue())["action"], "decided")
        code, output, errors = self._move_cli(
            "--role", "dispatcher", "--to", "done", "--decision", "release",
            "--request-id", "cli-to",
        )
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.client.tasks[0]["column_id"], 6)

    def test_cli_move_target_assessment_is_refused_for_a_forbidden_role(self) -> None:
        self.client.tasks[0]["column_id"] = 4
        code, output, errors = self._move_cli(
            "--role", "worker", "--target", "assessment", "--request-id", "cli-forbidden",
        )

        self.assertEqual((code, output), (3, ""))
        self.assertEqual(json.loads(errors)["error"]["code"], "transition_forbidden")
        self.assertEqual(self.client.tasks[0]["column_id"], 4)

    def test_cli_choice_lists_accept_assessment_where_a_state_is_legal(self) -> None:
        """`list --state` and `move --to` take it; `create --state` still cannot open a card there."""
        choices = _task_state_choices()
        self.assertIn("assessment", choices[("list", "state")])
        self.assertIn("assessment", choices[("move", "to")])
        self.assertEqual(choices[("create", "state")], ("issues", "ready"))
        # One argument, two spellings: `--target` is not a second option with its own dest.
        self.assertEqual(sorted(_move_target_option_strings()), ["--target", "--to"])


def _move_target_option_strings() -> list[str]:
    """Every flag `task move` accepts for the destination state."""
    from secretary.task_commands import add_task_subcommands

    parser = argparse.ArgumentParser()
    add_task_subcommands(parser.add_subparsers(dest="command"))
    task = parser._subparsers._group_actions[0].choices["task"]  # type: ignore[union-attr]
    move = task._subparsers._group_actions[0].choices["move"]  # type: ignore[union-attr]
    return [
        option for action in move._actions if action.dest == "to" for option in action.option_strings
    ]


def _task_state_choices() -> dict[tuple[str, str], tuple[str, ...]]:
    """{(task subcommand, argument dest): its choices} for every state-valued task argument."""
    from secretary.task_commands import add_task_subcommands

    parser = argparse.ArgumentParser()
    add_task_subcommands(parser.add_subparsers(dest="command"))
    task = parser._subparsers._group_actions[0].choices["task"]  # type: ignore[union-attr]
    found: dict[tuple[str, str], tuple[str, ...]] = {}
    for name, sub in task._subparsers._group_actions[0].choices.items():  # type: ignore[union-attr]
        for action in sub._actions:
            if action.dest in {"state", "to"} and action.choices:
                found[(name, action.dest)] = tuple(action.choices)
    return found


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
        classification = "external_fact" if kind == "blocked" else ""
        return self.writer.report(
            role="worker", actor="w", reference="secretary-468", kind=kind, body=body,
            classification=classification,
        )

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


class BlockedContractTests(unittest.TestCase):
    """Why a card is blocked, and what the observer did about it (secretary-1034).

    Both halves are recorded rather than left in prose: the worker names the kind of blocker
    it hit, and the observer's move out of Blocked carries the reason it moved.
    """

    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # A workspace outside git, so the done report's durability gate is not what these
        # tests are measuring.
        workspace = Path(self.tmpdir.name) / "workspace"
        workspace.mkdir()
        self.writer = TaskWriter(  # type: ignore[arg-type]
            self.client, data_dir=self.tmpdir.name, workspace=str(workspace),
        )

    def _events(self, kind: str) -> list[dict]:
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            return [event for event in map(json.loads, events) if event["kind"] == kind]

    def _reserve(self) -> None:
        bind_observer(self, SPRINT)
        self.client.metadata[12]["sprint_ref"] = SPRINT
        reader = FakeSprintReader({"ref": SPRINT, "status": "open", "reservations": ["secretary"]})
        patcher = mock.patch("secretary.sprints.SprintReader", return_value=reader)
        patcher.start()
        self.addCleanup(patcher.stop)
        refresh_active_sprint_projects(self.tmpdir.name, reader)

    def test_a_blocked_report_without_a_classification_is_refused(self) -> None:
        with self.assertRaisesRegex(TaskError, "require --classification") as raised:
            self.writer.report(
                role="worker", actor="w", reference="secretary-468", kind="blocked",
                body="the upstream API is down", request_id="blocked-unclassified",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(self.client.calls, [])

    def test_an_unknown_classification_is_refused(self) -> None:
        with self.assertRaisesRegex(TaskError, "require --classification"):
            self.writer.report(
                role="worker", actor="w", reference="secretary-468", kind="blocked",
                body="stuck", classification="something_else", request_id="blocked-unknown",
            )
        self.assertEqual(self.client.calls, [])

    def test_each_classification_reaches_the_audit_and_the_card(self) -> None:
        for index, classification in enumerate(("external_fact", "wrong_task_definition")):
            with self.subTest(classification=classification):
                result = self.writer.report(
                    role="worker", actor="w", reference="secretary-468", kind="blocked",
                    body="stuck on the adapter", classification=classification,
                    request_id=f"blocked-{classification}",
                )
                self.assertEqual(result["action"], "reported")
                payload = self._events("reported")[index]["payload"]
                self.assertEqual(payload["marker"], "report:blocked")
                self.assertEqual(payload["classification"], classification)
                self.assertEqual(
                    payload["body_sha256"],
                    hashlib.sha256(b"stuck on the adapter").hexdigest(),
                )
                comment = self.client.comments[12][-1]["comment"]
                self.assertTrue(comment.startswith("[report:blocked]\n"))
                self.assertIn(f"classification: {classification}", comment)
                self.assertIn("stuck on the adapter", comment)

    def test_a_blocked_report_is_a_single_backend_write(self) -> None:
        """Two writes could disagree; the comment and the audit event cannot."""
        self.writer.report(
            role="worker", actor="w", reference="secretary-468", kind="blocked",
            body="stuck", classification="external_fact", request_id="blocked-one-write",
        )
        written = [method for method, _ in self.client.calls if method.startswith(("create", "save", "move", "update"))]
        self.assertEqual(written, ["createComment"])

    def test_a_done_report_carries_no_classification(self) -> None:
        result = self.writer.report(
            role="worker", actor="w", reference="secretary-468", kind="done", body="ready",
            request_id="done-no-classification",
        )
        self.assertEqual(result["action"], "reported")
        self.assertNotIn("classification", self._events("reported")[0]["payload"])
        self.assertNotIn("classification:", self.client.comments[12][-1]["comment"])
        with self.assertRaisesRegex(TaskError, "no classification") as raised:
            self.writer.report(
                role="worker", actor="w", reference="secretary-468", kind="done", body="ready",
                classification="external_fact", request_id="done-with-classification",
            )
        self.assertEqual(raised.exception.code, "validation")

    def test_the_cli_refuses_an_unclassified_blocked_report(self) -> None:
        body = Path(self.tmpdir.name) / "report.md"
        body.write_text("the upstream API is down\n", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("secretary.task_commands.KanboardClient", return_value=self.client), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main([
                "task", "report", "--role", "worker", "--ref", "secretary-468",
                "--kind", "blocked", "--data-dir", str(Path(self.tmpdir.name) / "cli"),
                "--body-file", str(body), "--request-id", "cli-blocked-unclassified",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "validation")

    def test_the_cli_records_a_classified_blocked_report(self) -> None:
        body = Path(self.tmpdir.name) / "report.md"
        body.write_text("the card contradicts itself\n", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("secretary.task_commands.KanboardClient", return_value=self.client), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main([
                "task", "report", "--role", "worker", "--ref", "secretary-468",
                "--kind", "blocked", "--classification", "wrong_task_definition",
                "--data-dir", str(Path(self.tmpdir.name) / "cli"),
                "--body-file", str(body), "--request-id", "cli-blocked-classified",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["action"], "reported")
        comment = self.client.comments[12][-1]["comment"]
        self.assertIn("classification: wrong_task_definition", comment)

    def test_an_observer_moving_a_card_out_of_blocked_must_say_why(self) -> None:
        self._reserve()
        self.client.tasks[0]["column_id"] = 5  # Blocked

        with self.assertRaisesRegex(TaskError, "out of Blocked requires a non-empty reason") as raised:
            self.writer.move(
                role="observer", actor="observer", reference="secretary-468", target="ready",
                reason="   ", request_id="observer-silent-disposition",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(self.client.tasks[0]["column_id"], 5)

        reason = "the upstream fix landed, the card is workable again"
        moved = self.writer.move(
            role="observer", actor="observer", reference="secretary-468", target="ready",
            reason=reason, request_id="observer-disposition",
        )
        self.assertEqual(moved["task"]["state"], "ready")
        payload = self._events("moved")[-1]["payload"]
        self.assertEqual((payload["from"], payload["to"]), ("blocked", "ready"))
        self.assertEqual(payload["reason_sha256"], hashlib.sha256(reason.encode()).hexdigest())
        self.assertIn(reason, self.client.comments[12][-1]["comment"])

        # Every exit is guarded, not just the requeue to Ready.
        self.client.tasks[0]["column_id"] = 5
        with self.assertRaisesRegex(TaskError, "out of Blocked requires a non-empty reason"):
            self.writer.move(
                role="observer", actor="observer", reference="secretary-468",
                target="in_progress", reason="", request_id="observer-silent-resume",
            )

    def test_the_observer_may_still_move_a_card_into_blocked_without_a_reason(self) -> None:
        """Only the exit is guarded here. The entry paths are unchanged."""
        self._reserve()
        self.client.tasks[0]["column_id"] = 3  # In progress
        moved = self.writer.move(
            role="observer", actor="observer", reference="secretary-468", target="blocked",
            reason="", request_id="observer-into-blocked",
        )
        self.assertEqual(moved["task"]["state"], "blocked")

    def test_the_record_of_a_block_survives_the_card_leaving_blocked(self) -> None:
        """The classification is history, not card state: nothing on the card to go stale."""
        self._reserve()
        self.writer.report(
            role="worker", actor="w", reference="secretary-468", kind="blocked",
            body="the upstream API is down", classification="external_fact",
            request_id="blocked-before-requeue",
        )
        self.client.tasks[0]["column_id"] = 5  # Blocked
        requeued = self.writer.move(
            role="observer", actor="observer", reference="secretary-468", target="ready",
            reason="the upstream fix landed", request_id="observer-requeue",
        )
        self.assertEqual(requeued["task"]["state"], "ready")
        self.assertNotIn("blocked_classification", requeued["task"])
        self.assertNotIn("blocked_classification", self.client.metadata[12])
        self.assertEqual(self._events("reported")[0]["payload"]["classification"], "external_fact")

    def test_the_steward_requirement_is_untouched(self) -> None:
        self.client.tasks[0]["column_id"] = 3  # In progress
        with self.assertRaisesRegex(TaskError, "this steward transition requires a non-empty reason"):
            self.writer.move(
                role="steward", actor="s", reference="secretary-468", target="blocked", reason="",
            )
        escalated = self.writer.move(
            role="steward", actor="s", reference="secretary-468", target="blocked",
            reason="the head went silent", request_id="steward-escalation",
        )
        self.assertEqual(escalated["task"]["state"], "blocked")
        # And its own exit out of Blocked keeps the shape it had: Ready needs nothing, Done does.
        self.assertEqual(
            self.writer.move(
                role="steward", actor="s", reference="secretary-468", target="ready", reason="",
                request_id="steward-requeue",
            )["task"]["state"],
            "ready",
        )


class RequestIdOwnershipTests(unittest.TestCase):
    """A request id owns the operation it committed (secretary-1060).

    A retained worker reused the previous round's report id while submitting the next
    round's body. The committed event was replayed, no comment was appended, and the
    caller was told the report succeeded, so the dispatcher waited for a marker that
    could never arrive. The id has to be refused, not replayed.
    """

    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # Outside git, so the done report's durability gate is not what these tests measure.
        workspace = Path(self.tmpdir.name) / "workspace"
        workspace.mkdir()
        self.writer = TaskWriter(  # type: ignore[arg-type]
            self.client, data_dir=self.tmpdir.name, workspace=str(workspace),
        )

    def _events(self, request_id: str = "") -> list[dict]:
        try:
            with open(self.writer.audit.events_path, encoding="utf-8") as events:
                recorded = [json.loads(line) for line in events if line.strip()]
        except FileNotFoundError:
            return []
        if not request_id:
            return recorded
        return [event for event in recorded if event["request_id"] == request_id]

    def _comments(self, task_id: int = 12) -> list[str]:
        return [str(comment["comment"]) for comment in self.client.comments[task_id]]

    def _report(self, **overrides: object) -> dict:
        call = {
            "role": "worker", "actor": "w", "reference": "secretary-468", "kind": "done",
            "body": "first round", "request_id": "round-1",
        }
        call.update(overrides)
        return self.writer.report(**call)  # type: ignore[arg-type]

    def test_a_reused_report_id_with_another_body_is_refused(self) -> None:
        """The live shape of issue:df7d0778b26357e60046."""
        first = self._report()
        self.client.calls.clear()

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._report(body="third round, a different report entirely")

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["event_id"], first["event_id"])
        self.assertEqual(len(self._comments()), 1)

    def test_the_same_report_under_the_same_id_stays_idempotent(self) -> None:
        first = self._report()
        second = self._report()

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(second["action"], "reported")
        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(len(self._comments()), 1)

    def test_structured_output_tells_a_replay_from_an_accepted_write(self) -> None:
        self.assertIs(self._report()["replayed"], False)
        self.assertIs(self._report()["replayed"], True)

    def test_a_reused_report_id_with_another_kind_is_refused(self) -> None:
        self._report()

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report(kind="blocked", classification="external_fact")

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["payload"]["marker"], "report:done")
        self.assertEqual(len(self._comments()), 1)

    def test_a_reused_report_id_with_another_classification_is_refused(self) -> None:
        self._report(kind="blocked", body="stuck", classification="external_fact")

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report(kind="blocked", body="stuck", classification="wrong_task_definition")

        self.assertEqual(
            self._events("round-1")[0]["payload"]["classification"], "external_fact"
        )
        self.assertEqual(len(self._comments()), 1)

    def test_a_reused_report_id_on_another_card_is_refused(self) -> None:
        self._report()

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report(reference="old-1")

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["ref"], "secretary-468")
        self.assertEqual(self._comments(13), [])

    def test_a_reused_id_from_another_write_is_refused(self) -> None:
        """The claim is over the operation, not only over the report vocabulary."""
        self.writer.comment(
            role="worker", actor="w", reference="secretary-468", body="a note",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report()

        self.assertEqual(self._events("round-1")[0]["kind"], "commented")
        self.assertEqual(len(self._comments()), 1)

    def _stage_pending_report(self, body: str) -> dict:
        """A report staged by a crashed attempt: written, never appended."""
        event = {
            "event_id": "evt_staged", "schema_version": 1, "occurred_at": "2026-08-03T00:00:00Z",
            "actor": {"role": "worker", "id": "w"}, "kind": "reported", "outcome": "success",
            "task_id": "task_kanboard_12", "ref": "secretary-468",
            "backend": {"kind": "kanboard", "task_id": 12, "revision": "pending"},
            "request_id": "round-1",
            "payload": {"marker": "report:done", "body_sha256": hashlib.sha256(body.encode()).hexdigest()},
        }
        self.writer.audit.stage("round-1", event)
        return event

    def test_a_pending_report_is_owned_by_its_id_too(self) -> None:
        self._stage_pending_report("first round")

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._report(body="third round, a different report entirely")

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self._events(), [])
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(self._comments(), [])

    def test_a_pending_report_still_commits_under_its_own_payload(self) -> None:
        staged = self._stage_pending_report("first round")

        result = self._report()

        self.assertEqual(result["event_id"], staged["event_id"])
        self.assertIs(result["replayed"], True)
        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        # The crashed attempt already wrote the comment; recovery writes no second one.
        self.assertEqual(self._comments(), [])

    def test_a_reused_verdict_id_with_another_verdict_is_refused(self) -> None:
        self.writer.verdict(
            role="reviewer", actor="r", reference="secretary-468", kind="green", body="ok",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.verdict(
                role="reviewer", actor="r", reference="secretary-468", kind="red",
                body="the gate is red", request_id="round-1",
            )

        self.assertEqual(self._events("round-1")[0]["payload"]["marker"], "review:green")
        self.assertEqual(len(self._comments()), 1)

    def test_the_cli_refuses_a_reused_report_id_with_exit_code_two(self) -> None:
        data_dir = str(Path(self.tmpdir.name) / "cli")
        body = Path(self.tmpdir.name) / "report.md"
        body.write_text("first round\n", encoding="utf-8")
        argv = [
            "task", "report", "--role", "worker", "--ref", "secretary-468", "--kind", "done",
            "--data-dir", data_dir, "--body-file", str(body), "--request-id", "cli-round-1",
        ]
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("secretary.task_commands.KanboardClient", return_value=self.client), \
             mock.patch("secretary.tasks.workspace_dirt", return_value=[]), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(main(argv), 0)
            body.write_text("third round, a different report entirely\n", encoding="utf-8")
            code = main(argv)

        self.assertEqual(code, 2)
        self.assertIs(json.loads(output.getvalue().splitlines()[0])["replayed"], False)
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "validation")
        self.assertEqual(len(self._comments()), 1)

    def test_every_write_has_to_declare_what_its_id_claims(self) -> None:
        """A new caller cannot inherit the blind replay by forgetting one keyword."""
        identity = inspect.signature(TaskWriter._write).parameters["identity"]
        self.assertIs(identity.default, inspect.Parameter.empty)

    def _routing_payload(self, attempt: int, head: str = "codex-terra") -> dict:
        return routing_payload(
            attempt=attempt,
            attempt_id="att-1",
            phase="worker",
            heads=[
                head_run_from_profile(
                    role="worker", head=head, head_source="role_default",
                    profile={"adapter": "codex", "model": "gpt-5.6-terra", "effort": "extra"},
                    resources={},
                )
            ],
        )

    def _routing(self, attempt: int, **overrides: object) -> dict:
        call = {
            "role": "dispatcher", "actor": "pilot", "reference": "secretary-468",
            "payload": self._routing_payload(attempt), "request_id": "round-1",
        }
        call.update(overrides)
        return self.writer.routing(**call)  # type: ignore[arg-type]

    def test_a_reused_routing_id_with_another_record_is_refused(self) -> None:
        """A journal-only write is caller-supplied end to end, so it owns its id too."""
        first = self._routing(1)

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._routing(2)

        self.assertEqual(raised.exception.exit_code, 2)
        recorded = self._events("round-1")
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["event_id"], first["event_id"])
        self.assertEqual(recorded[0]["payload"]["attempt"], 1)

    def test_a_staged_routing_record_is_owned_by_its_id_too(self) -> None:
        staged = {
            "event_id": "evt_staged_routing", "schema_version": 1,
            "occurred_at": "2026-08-03T00:00:00Z", "actor": {"role": "dispatcher", "id": "pilot"},
            "kind": "routing", "outcome": "success", "task_id": "task_kanboard_12",
            "ref": "secretary-468",
            "backend": {"kind": "kanboard", "task_id": 12, "revision": "pending"},
            "request_id": "round-1", "payload": self._routing_payload(1),
        }
        self.writer.audit.stage("round-1", staged)

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._routing(2)

        self.assertEqual(self._events(), [])
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

        # The record its own id claims still commits.
        self.assertEqual(self._routing(1)["event_id"], "evt_staged_routing")
        self.assertEqual(self._events("round-1")[0]["payload"]["attempt"], 1)

    def test_a_reused_edit_id_with_another_spec_is_refused(self) -> None:
        self.writer.edit(
            role="po", actor="operator", reference="secretary-468", description="first spec",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.edit(
                role="po", actor="operator", reference="secretary-468", description="second spec",
                request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self.client.tasks[0]["description"], "first spec")

    def test_an_edit_retried_after_it_landed_stays_idempotent(self) -> None:
        """The `_was` digests are of text the edit replaced, so a retry must not compare them."""
        first = self.writer.edit(
            role="po", actor="operator", reference="secretary-468", description="one spec",
            head="codex-terra", request_id="round-1",
        )
        second = self.writer.edit(
            role="po", actor="operator", reference="secretary-468", description="one spec",
            head="codex-terra", request_id="round-1",
        )

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertIs(second["replayed"], True)
        self.assertEqual(len([call for call in self.client.calls if call[0] == "updateTask"]), 1)

    def test_a_reused_claim_id_with_another_worker_is_refused(self) -> None:
        self.client.metadata[12]["claim"] = ""
        self.writer.claim(
            role="dispatcher", actor="d", reference="secretary-468", worker="worker-a",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.claim(
                role="dispatcher", actor="d", reference="secretary-468", worker="worker-b",
                request_id="round-1",
            )

        self.assertEqual(self._events("round-1")[0]["payload"]["worker"], "worker-a")
        self.assertEqual(self.client.metadata[12]["claim"], "worker-a")

    def test_a_reused_move_id_with_another_destination_is_refused(self) -> None:
        self.client.tasks[0]["column_id"] = 3  # In progress
        self.writer.move(
            role="dispatcher", actor="d", reference="secretary-468", target="ready",
            reason="requeue", request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.move(
                role="dispatcher", actor="d", reference="secretary-468", target="blocked",
                reason="requeue", request_id="round-1",
            )
        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.move(
                role="dispatcher", actor="d", reference="secretary-468", target="ready",
                reason="a different reason entirely", request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["payload"]["to"], "ready")

    def test_a_move_retried_after_it_landed_stays_idempotent(self) -> None:
        """`from` is the column the move left, so a retry must not compare it."""
        self.client.tasks[0]["column_id"] = 3  # In progress
        call = {
            "role": "dispatcher", "actor": "d", "reference": "secretary-468", "target": "ready",
            "reason": "requeue", "request_id": "round-1",
        }
        first = self.writer.move(**call)  # type: ignore[arg-type]
        second = self.writer.move(**call)  # type: ignore[arg-type]

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertIs(second["replayed"], True)
        self.assertEqual(self._events("round-1")[0]["payload"]["from"], "in_progress")

    def test_a_reused_restore_id_with_another_placement_is_refused(self) -> None:
        self.writer.restore_card(
            reference="secretary-468", metadata={"project": "secretary"}, target="ready",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.restore_card(
                reference="secretary-468", metadata={"project": "secretary"}, target="blocked",
                request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["payload"]["target"], "ready")

    def test_a_reused_restore_comment_id_with_another_body_is_refused(self) -> None:
        self.writer.restore_comment(
            reference="secretary-468", body="the original comment", occurrence=0,
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.restore_comment(
                reference="secretary-468", body="another comment entirely", occurrence=0,
                request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._comments(), ["the original comment"])

    def _create(self, **overrides: object) -> dict:
        call = {
            "role": "observer", "actor": "observer", "project": "secretary", "task_type": "code",
            "title": "First card", "target": "ready", "request_id": "create-1",
        }
        call.update(overrides)
        with open_sprint() as sprint:
            return self.writer.create(sprint=sprint, **call)  # type: ignore[arg-type]

    def test_a_reused_create_id_with_another_card_is_refused(self) -> None:
        created = self._create()
        cards = len(self.client.tasks)

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._create(title="A different card entirely")

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(len(self.client.tasks), cards)
        self.assertEqual(len(self._events("create-1")), 1)
        self.assertEqual(self._events("create-1")[0]["event_id"], created["event_id"])

    def test_the_same_create_under_the_same_id_stays_idempotent(self) -> None:
        first = self._create()
        cards = len(self.client.tasks)

        second = self._create()

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["task"]["ref"], second["task"]["ref"])
        self.assertIs(first["replayed"], False)
        self.assertIs(second["replayed"], True)
        self.assertEqual(len(self.client.tasks), cards)
        self.assertEqual(len(self._events("create-1")), 1)


class AuditCommittedIndexTests(unittest.TestCase):
    """committed_event читает журнал инкрементально, а не целиком на каждый вызов.

    append()/stage() зовут committed_event на каждое событие, а тот разбирал весь
    events.ndjson с начала. На восстановлении 745 карточек (~30k событий) это давало
    квадратичный прогон: ~8 МБ JSON перечитывались на каждую запись, борд при этом
    отвечал за 5-10 мс. Индекс обязан оставаться согласованным с файлом, который
    дописывает другой процесс.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.audit = TaskAudit(self.tmpdir.name)
        Path(self.audit.board_dir).mkdir(parents=True, exist_ok=True)

    def _append_raw(self, payload: dict, terminated: bool = True) -> None:
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with open(self.audit.events_path, "a", encoding="utf-8") as events:
            events.write(line + ("\n" if terminated else ""))

    def test_missing_file_reads_as_no_event(self) -> None:
        self.assertIsNone(self.audit.committed_event("nope"))

    def test_picks_up_events_appended_after_the_first_read(self) -> None:
        self._append_raw({"request_id": "one", "event_id": "e1"})
        self.assertEqual(self.audit.committed_event("one")["event_id"], "e1")
        self.assertIsNone(self.audit.committed_event("two"))

        self._append_raw({"request_id": "two", "event_id": "e2"})
        self.assertEqual(self.audit.committed_event("two")["event_id"], "e2")
        self.assertEqual(self.audit.committed_event("one")["event_id"], "e1")

    def test_earliest_duplicate_wins(self) -> None:
        self._append_raw({"request_id": "dup", "event_id": "first"})
        self._append_raw({"request_id": "dup", "event_id": "second"})
        self.assertEqual(self.audit.committed_event("dup")["event_id"], "first")

    def test_half_written_line_is_not_consumed_until_terminated(self) -> None:
        self._append_raw({"request_id": "done", "event_id": "e1"})
        self._append_raw({"request_id": "torn", "event_id": "e2"}, terminated=False)

        self.assertEqual(self.audit.committed_event("done")["event_id"], "e1")
        self.assertIsNone(self.audit.committed_event("torn"))

        with open(self.audit.events_path, "a", encoding="utf-8") as events:
            events.write("\n")
        self.assertEqual(self.audit.committed_event("torn")["event_id"], "e2")

    def test_rebuilds_when_the_journal_is_replaced(self) -> None:
        self._append_raw({"request_id": "old", "event_id": "e1"})
        self.assertIsNotNone(self.audit.committed_event("old"))

        with open(self.audit.events_path, "w", encoding="utf-8") as events:
            events.write(json.dumps({"request_id": "new", "event_id": "e2"}) + "\n")

        self.assertIsNone(self.audit.committed_event("old"))
        self.assertEqual(self.audit.committed_event("new")["event_id"], "e2")

    def test_garbage_lines_are_skipped(self) -> None:
        with open(self.audit.events_path, "a", encoding="utf-8") as events:
            events.write("not json\n")
            events.write("[1,2,3]\n")
            events.write("\n")
        self._append_raw({"request_id": "good", "event_id": "e1"})
        self.assertEqual(self.audit.committed_event("good")["event_id"], "e1")

    def test_warm_index_answers_misses_without_reparsing_the_journal(self) -> None:
        """Суть фикса: на прогретом индексе промах не разбирает журнал заново.

        Именно этот путь исполнялся на каждой записи (append -> committed_event ->
        промах -> запись) и стоил полного json-разбора всего файла.
        """
        for index in range(20):
            self._append_raw({"request_id": f"r{index}", "event_id": f"e{index}"})
        self.assertIsNotNone(self.audit.committed_event("r0"))  # прогреваем индекс

        parsed: list[int] = []
        real_loads = json.loads

        def counting_loads(payload, *args, **kwargs):  # type: ignore[no-untyped-def]
            parsed.append(1)
            return real_loads(payload, *args, **kwargs)

        with mock.patch("secretary.tasks.json.loads", counting_loads):
            for index in range(20):
                self.assertIsNone(self.audit.committed_event(f"missing-{index}"))

        self.assertEqual(parsed, [])
