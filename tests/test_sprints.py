from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock

from secretary.cli import main
from secretary.sprints import BUDGET_EVENT_TYPES, SprintReader, SprintWriter, budget_thresholds, ensure_sprint_board
from secretary.tasks import TaskAudit, TaskError, TaskReader, TaskWriter


class SprintKanboard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.projects = {"Pipeline": 7}
        self.columns = {
            7: [
                {"id": 1, "title": "Идеи"}, {"id": 2, "title": "Ready"},
                {"id": 3, "title": "In progress"}, {"id": 4, "title": "Validate"},
                {"id": 5, "title": "Blocked"}, {"id": 6, "title": "Done"},
            ]
        }
        self.tasks = [{
            "id": 12, "project_id": 7, "reference": "secretary-12", "title": "existing",
            "description": "", "column_id": 2, "position": 1, "swimlane_id": 0,
            "date_creation": "1720000000", "date_modification": "1720000000",
        }]
        self.metadata = {12: {"project": "secretary", "task_type": "code"}}
        self.comments = {12: []}

    def call(self, method: str, **params: object) -> object:
        self.calls.append((method, params))
        if method == "getProjectByName":
            project_id = self.projects.get(str(params["name"]))
            return {"id": project_id} if project_id else None
        if method == "createProject":
            project_id = max(self.projects.values()) + 1
            self.projects[str(params["name"])] = project_id
            self.columns[project_id] = [{"id": project_id * 10, "title": "Backlog"}]
            return project_id
        if method == "getColumns":
            return self.columns[int(params["project_id"])]
        if method == "getActiveSwimlanes":
            return []
        if method == "getAllTasks":
            return [
                task for task in self.tasks
                if task["project_id"] == params["project_id"] and int(task.get("is_active", 1)) != 0
            ]
        if method == "getTaskByReference":
            return next((task for task in self.tasks if task["project_id"] == params["project_id"] and task["reference"] == params["reference"]), None)
        if method == "getTaskMetadata":
            return self.metadata[int(params["task_id"])]
        if method == "getAllComments":
            return self.comments[int(params["task_id"])]
        if method == "createTask":
            task_id = max(int(task["id"]) for task in self.tasks) + 1
            task = {
                "id": task_id, "project_id": int(params["project_id"]), "reference": "",
                "title": params["title"], "description": params.get("description", ""),
                "column_id": params["column_id"], "position": len(self.tasks) + 1,
                "swimlane_id": params.get("swimlane_id", 0), "date_creation": "1720000001",
                "date_modification": "1720000001",
            }
            self.tasks.append(task)
            self.metadata[task_id] = {}
            self.comments[task_id] = []
            return task_id
        if method == "updateTask":
            task = next(task for task in self.tasks if task["id"] == params["id"])
            for field in ("reference", "title", "description"):
                if field in params:
                    task[field] = params[field]
            task["date_modification"] = "1720000002"
            return True
        if method == "saveTaskMetadata":
            self.metadata[int(params["task_id"])].update(params["values"])
            return True
        if method == "createComment":
            self.comments[int(params["task_id"])].append({"date_creation": "1720000003", "comment": params["content"]})
            return 1
        raise AssertionError(method)


class SprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SprintKanboard()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.writer = SprintWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

    def test_board_creation_is_idempotent(self) -> None:
        first = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        second = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        self.assertEqual(first, second)
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createProject"]), 1)

    def test_create_has_only_contract_fields_and_rejects_duplicate_reference(self) -> None:
        created = self.writer.create(
            role="po", actor="operator", goal="Ship sprint entity", definition_of_done="tests pass",
            repositories=["secretary", "secretary"], reference="sprint:entity", request_id="create",
        )
        sprint = created["sprint"]
        self.assertEqual(sprint["repositories"], ["secretary"])
        self.assertEqual(sprint["status"], "open")
        self.assertEqual(sprint["budget"]["total"], 0)
        self.assertEqual(sprint["budget"]["by_type"], {event: 0 for event in BUDGET_EVENT_TYPES})
        self.assertFalse(sprint["budget"]["signal_reached"])
        self.assertIsNone(sprint["current_task"])
        self.assertNotIn("title", sprint)
        with self.assertRaisesRegex(TaskError, "already exists") as raised:
            self.writer.create(role="po", actor="operator", goal="another", reference="sprint:entity")
        self.assertEqual(raised.exception.code, "validation")

    def test_missing_metadata_reads_as_empty_contract_values(self) -> None:
        sprint_board = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        self.client.tasks.append({
            "id": 13, "project_id": sprint_board, "reference": "sprint:legacy", "title": "legacy",
            "description": "", "column_id": sprint_board * 10, "position": 1, "swimlane_id": 0,
            "date_creation": "1720000000", "date_modification": "1720000000",
        })
        self.client.metadata[13] = {}
        self.client.comments[13] = []
        sprint = SprintReader(self.client).show("sprint:legacy")  # type: ignore[arg-type]
        self.assertEqual(sprint["goal"], "")
        self.assertEqual(sprint["definition_of_done"], "")
        self.assertEqual(sprint["repositories"], [])
        self.assertEqual(sprint["budget"]["total"], 0)
        self.assertEqual(sprint["budget"]["by_type"], {event: 0 for event in BUDGET_EVENT_TYPES})
        self.assertIsNone(sprint["current_task"])

    def test_budget_is_validated_and_retry_is_one_event(self) -> None:
        ref = self.writer.create(role="po", actor="operator", goal="budget")["sprint"]["ref"]
        with self.assertRaisesRegex(TaskError, "unknown budget"):
            self.writer.record_budget(role="po", actor="operator", reference=ref, event_type="green")
        first = self.writer.record_budget(role="po", actor="operator", reference=ref, event_type="red_ci", request_id="budget-once")
        second = self.writer.record_budget(role="po", actor="operator", reference=ref, event_type="red_ci", request_id="budget-once")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(second["sprint"]["budget"]["total"], 1)
        self.assertEqual(second["sprint"]["budget"]["by_type"]["red_ci"], 1)
        events = TaskAudit(self.tmp.name).events(reference=ref)
        self.assertEqual([event["kind"] for event in events], ["created", "budget_recorded"])

    def test_budget_thresholds_reject_hard_limit_below_signal(self) -> None:
        with self.assertRaisesRegex(TaskError, "hard threshold"):
            budget_thresholds({"sprint_budget": {"signal": 3, "hard": 2}})

    def test_task_link_is_live_metadata_and_closed_sprint_rejects_writes(self) -> None:
        ref = self.writer.create(role="po", actor="operator", goal="link")["sprint"]["ref"]
        task_writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        task_writer.create(
            role="po", actor="operator", project="secretary", task_type="code", title="linked",
            sprint=ref, request_id="linked-card",
        )
        self.assertEqual(TaskReader(self.client).list(sprint=ref)[0]["sprint"], ref)  # type: ignore[arg-type]
        shown = SprintReader(self.client).show(ref)  # type: ignore[arg-type]
        self.assertEqual([card["ref"] for card in shown["cards"]], ["secretary-14"])
        self.writer.close(role="po", actor="operator", reference=ref)
        with self.assertRaisesRegex(TaskError, "closed"):
            self.writer.comment(role="worker", actor="worker", reference=ref, body="late")
        with self.assertRaisesRegex(TaskError, "closed"):
            task_writer.create(role="po", actor="operator", project="secretary", task_type="code", title="late", sprint=ref)

    def test_cli_create_and_list_return_stable_json(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("secretary.sprint_commands.KanboardClient", return_value=self.client), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main([
                "sprint", "create", "--role", "po", "--data-dir", self.tmp.name,
                "--goal", "CLI sprint", "--repository", "secretary", "--request-id", "cli-create",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        result = json.loads(output.getvalue())
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["sprint"]["repositories"], ["secretary"])

    def test_resume_requires_all_fields_and_staleness_uses_card_audit(self) -> None:
        ref = self.writer.create(role="po", actor="operator", goal="resume") ["sprint"]["ref"]
        with self.assertRaisesRegex(TaskError, "missing required fields"):
            self.writer.resume(role="po", actor="operator", reference=ref, entry={"selected_step": "x"})
        entry = {
            "selected_step": "implement", "selected_why": "needed", "rejected_alternatives": "wait",
            "current_task": "next card", "dod_state": "tests pending", "next_safe_step": "run tests",
            "recorded_at": "2000-01-01T00:00:00Z",
        }
        self.writer.resume(role="po", actor="operator", reference=ref, entry=entry, request_id="resume")
        fresh = SprintReader(self.client, data_dir=self.tmp.name).show(ref)  # type: ignore[arg-type]
        self.assertTrue(fresh["resume_freshness"]["fresh"])
        task_writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        task = task_writer.create(
            role="po", actor="operator", project="secretary", task_type="code", title="linked",
            sprint=ref, request_id="resume-card",
        )["task"]
        task_writer.comment(role="po", actor="operator", reference=task["ref"], body="meaningful", request_id="later")
        stale = SprintReader(self.client, data_dir=self.tmp.name).show(ref)  # type: ignore[arg-type]
        self.assertFalse(stale["resume_freshness"]["fresh"])
        self.assertEqual(stale["resume_freshness"]["error"], "resume_stale")


if __name__ == "__main__":
    unittest.main()
