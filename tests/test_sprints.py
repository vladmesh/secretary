from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
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
        if method == "moveTaskPosition":
            task = next(task for task in self.tasks if task["id"] == params["task_id"])
            task["column_id"] = params["column_id"]
            task["swimlane_id"] = params["swimlane_id"]
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

    def test_read_only_sprint_list_does_not_create_a_board_or_claim_resume_freshness(self) -> None:
        reader = SprintReader(self.client)  # type: ignore[arg-type]
        self.assertEqual(reader.list(create=False), [])
        self.assertFalse(any(call[0] == "createProject" for call in self.client.calls))

        created = self.writer.create(role="po", actor="operator", goal="list")
        listed = reader.list()
        self.assertEqual(listed[0]["ref"], created["sprint"]["ref"])
        self.assertNotIn("resume_freshness", listed[0])

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

    def test_export_reads_records_without_the_board_or_the_linked_cards(self) -> None:
        reader = SprintReader(self.client)  # type: ignore[arg-type]
        self.assertEqual(reader.export(), [])
        self.assertFalse(any(call[0] == "createProject" for call in self.client.calls))

        ref = self.writer.create(role="po", actor="operator", goal="export")["sprint"]["ref"]
        self.writer.comment(role="po", actor="operator", reference=ref, body="note")
        self.client.calls.clear()
        exported = reader.export()

        self.assertEqual([sprint["ref"] for sprint in exported], [ref])
        self.assertEqual([comment["body"] for comment in exported[0]["comments"]], ["[po]\nnote"])
        self.assertNotIn("resume_freshness", exported[0])
        self.assertNotIn("cards", exported[0])
        self.assertFalse(
            any(call[0] == "getProjectByName" and call[1]["name"] == "Pipeline" for call in self.client.calls)
        )

    def test_restore_rewrites_a_closed_entity_and_refuses_foreign_fields(self) -> None:
        ref = self.writer.create(role="po", actor="operator", goal="restore")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref)

        with self.assertRaisesRegex(TaskError, "unknown sprint fields"):
            self.writer.restore(reference=ref, values={"claim": "worker"})

        result = self.writer.restore(
            reference=ref,
            values={"sprint_goal": "rewritten", "sprint_current_task": "secretary-12"},
            request_id="restore-once",
        )
        replay = self.writer.restore(
            reference=ref,
            values={"sprint_goal": "rewritten", "sprint_current_task": "secretary-12"},
            request_id="restore-once",
        )

        self.assertEqual(result["sprint"]["goal"], "rewritten")
        self.assertEqual(result["sprint"]["status"], "closed")
        self.assertEqual(result["sprint"]["current_task"], "secretary-12")
        self.assertEqual(result["event_id"], replay["event_id"])

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

    def test_hard_budget_stop_has_its_own_durable_event(self) -> None:
        writer = SprintWriter(self.client, data_dir=self.tmp.name, thresholds={"signal": 1, "hard": 1})  # type: ignore[arg-type]
        ref = writer.create(role="po", actor="operator", goal="hard limit")["sprint"]["ref"]

        writer.record_budget(
            role="dispatcher", actor="dispatcher", reference=ref, event_type="blocked",
            request_id="hard-stop", source_event_id="evt-card-blocked",
        )
        writer.record_budget(
            role="dispatcher", actor="dispatcher", reference=ref, event_type="blocked",
            request_id="hard-stop", source_event_id="evt-card-blocked",
        )

        events = TaskAudit(self.tmp.name).events(reference=ref)
        self.assertEqual([event["kind"] for event in events], ["created", "budget_recorded", "budget_hard_stopped"])
        self.assertEqual(events[-1]["payload"]["reason"], "budget_hard_limit")
        self.assertEqual(events[-1]["payload"]["source_event_id"], "evt-card-blocked")

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

    def test_cli_observer_can_set_current_task(self) -> None:
        ref = self.writer.create(role="po", actor="operator", goal="observer current task")["sprint"]["ref"]
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(
            role="po", actor="operator", project="secretary", task_type="code", title="linked",
            sprint=ref,
        )["task"]
        output, errors = io.StringIO(), io.StringIO()

        with mock.patch("secretary.sprint_commands.KanboardClient", return_value=self.client), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main([
                "sprint", "current-task", "--ref", ref, "--role", "observer", "--actor", "observer",
                "--task", task["ref"], "--data-dir", self.tmp.name,
            ])

        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["sprint"]["current_task"], task["ref"])

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

    def test_observer_can_record_a_complete_resume_entry(self) -> None:
        ref = self.writer.create(role="po", actor="operator", goal="observer resume")["sprint"]["ref"]
        entry = {
            "selected_step": "implement", "selected_why": "needed", "rejected_alternatives": "wait",
            "current_task": "secretary-14", "dod_state": "tests pending", "next_safe_step": "run tests",
        }

        result = self.writer.resume(
            role="observer", actor="observer-head", reference=ref, entry=entry, request_id="observer-resume",
        )

        self.assertEqual(result["action"], "resume_recorded")
        self.assertEqual(result["sprint"]["resume"]["selected_step"], "implement")


class SprintSingleWriterGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SprintKanboard()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sprints = SprintWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.ref = self.sprints.create(
            role="po", actor="operator", goal="single writer", repositories=["secretary", "other"],
        )["sprint"]["ref"]

    def test_observer_must_link_to_its_open_sprint_and_other_roles_are_denied(self) -> None:
        card = self.tasks.create(
            role="observer", actor="observer", project="secretary", task_type="code", title="owned",
            sprint=self.ref, request_id="observer-create",
        )["task"]
        self.assertEqual(card["sprint"], self.ref)
        with self.assertRaisesRegex(TaskError, self.ref) as missing:
            self.tasks.create(
                role="observer", actor="observer", project="secretary", task_type="code", title="unlinked",
                request_id="observer-unlinked",
            )
        self.assertEqual(missing.exception.code, "sprint_write_forbidden")
        with self.assertRaisesRegex(TaskError, self.ref) as retro:
            self.tasks.create(
                role="retro", actor="retro", project="secretary", task_type="research", title="finding",
                request_id="retro-denied",
            )
        self.assertEqual(retro.exception.code, "sprint_write_forbidden")
        denied = [event for event in TaskAudit(self.tmp.name).events() if event["kind"] == "sprint_guard_denied"]
        self.assertEqual(len(denied), 2)
        self.assertEqual(denied[0]["payload"]["sprint"], self.ref)

    def test_po_override_requires_reason_and_is_audited_once(self) -> None:
        with self.assertRaisesRegex(TaskError, "non-empty reason") as missing:
            self.tasks.create(
                role="po", actor="operator", project="secretary", task_type="code", title="urgent",
                sprint_override=True, request_id="override-empty",
            )
        self.assertEqual(missing.exception.code, "validation")
        first = self.tasks.create(
            role="po", actor="operator", project="secretary", task_type="code", title="urgent",
            sprint_override=True, sprint_override_reason="production incident", request_id="override-once",
        )
        second = self.tasks.create(
            role="po", actor="operator", project="secretary", task_type="code", title="urgent",
            sprint_override=True, sprint_override_reason="production incident", request_id="override-once",
        )
        self.assertEqual(first["event_id"], second["event_id"])
        event = next(event for event in TaskAudit(self.tmp.name).events() if event["request_id"] == "override-once")
        self.assertEqual(event["payload"]["sprint_override_reason"], "production incident")

    def test_po_cannot_edit_a_held_card_without_an_audited_override(self) -> None:
        card = self.tasks.create(
            role="observer", actor="observer", project="secretary", task_type="code", title="owned",
            sprint=self.ref,
        )["task"]
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.edit(role="po", actor="operator", reference=card["ref"], description="outside edit")
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")
        edited = self.tasks.edit(
            role="po", actor="operator", reference=card["ref"], description="incident edit",
            sprint_override=True, sprint_override_reason="production incident",
        )
        self.assertEqual(edited["task"]["description"], "incident edit")
        event = TaskAudit(self.tmp.name).events()[-1]
        self.assertEqual(event["payload"]["sprint_override_reason"], "production incident")

    def test_override_retry_reuses_the_denied_request_id_for_the_write(self) -> None:
        card = self.tasks.create(
            role="observer", actor="observer", project="secretary", task_type="code", title="owned",
            sprint=self.ref,
        )["task"]
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.move(
                role="po", actor="operator", reference=card["ref"], target="ready", reason="",
                request_id="po-override-retry",
            )
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")

        moved = self.tasks.move(
            role="po", actor="operator", reference=card["ref"], target="ready", reason="",
            sprint_override=True, sprint_override_reason="production incident", request_id="po-override-retry",
        )

        self.assertEqual(moved["task"]["state"], "ready")
        events = TaskAudit(self.tmp.name).events()
        denial = next(event for event in events if event["kind"] == "sprint_guard_denied")
        self.assertEqual(denial["payload"]["operation_request_id"], "po-override-retry")
        success = next(event for event in events if event["request_id"] == "po-override-retry")
        self.assertEqual(success["kind"], "moved")
        self.assertEqual(success["payload"]["sprint_override_reason"], "production incident")

    def test_denied_create_request_can_succeed_after_sprint_closes(self) -> None:
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.create(
                role="retro", actor="retro", project="secretary", task_type="research", title="finding",
                request_id="retro-after-close",
            )
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")
        self.sprints.close(role="po", actor="operator", reference=self.ref)

        created = self.tasks.create(
            role="retro", actor="retro", project="secretary", task_type="research", title="finding",
            request_id="retro-after-close",
        )

        self.assertEqual(created["task"]["project"], "secretary")
        self.assertEqual(created["task"]["state"], "ideas")

    def test_dispatcher_cycle_and_observer_move_are_allowed(self) -> None:
        card = self.tasks.create(
            role="observer", actor="observer", project="secretary", task_type="code", title="cycle",
            sprint=self.ref,
        )["task"]
        self.tasks.move(role="observer", actor="observer", reference=card["ref"], target="ready", reason="")
        self.tasks.claim(role="dispatcher", actor="dispatcher", reference=card["ref"], worker="worker")
        result = self.tasks.move(role="dispatcher", actor="dispatcher", reference=card["ref"], target="validate", reason="")
        self.assertEqual(result["task"]["state"], "validate")

    def test_close_releases_every_repository_and_unheld_projects_skip_sprint_board(self) -> None:
        self.client.calls.clear()
        card = self.tasks.create(
            role="po", actor="operator", project="unheld", task_type="code", title="normal",
        )["task"]
        self.assertEqual(card["project"], "unheld")
        self.assertFalse(any(method == "getProjectByName" and params.get("name") == "Secretary sprints" for method, params in self.client.calls))
        with self.assertRaises(TaskError):
            self.tasks.create(role="po", actor="operator", project="other", task_type="code", title="cross repo")
        self.sprints.close(role="po", actor="operator", reference=self.ref)
        released = self.tasks.create(role="po", actor="operator", project="other", task_type="code", title="released")
        self.assertEqual(released["task"]["project"], "other")

    def test_unavailable_sprint_board_fails_closed(self) -> None:
        original = self.client.call

        def unavailable(method: str, **params: object) -> object:
            if method == "getTaskByReference" and params.get("reference") == self.ref:
                raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1)
            return original(method, **params)

        with mock.patch.object(self.client, "call", side_effect=unavailable):
            with self.assertRaisesRegex(TaskError, "cannot verify sprint") as raised:
                self.tasks.create(role="po", actor="operator", project="secretary", task_type="code", title="blocked")
        self.assertEqual(raised.exception.code, "sprint_guard_unavailable")

    def test_missing_index_bootstraps_from_live_open_sprints(self) -> None:
        (Path(self.tmp.name) / "sprints" / "active-repositories.json").unlink()
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.create(role="po", actor="operator", project="secretary", task_type="code", title="blocked")
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")

    def test_pending_sprint_recovery_rebuilds_its_repository_index(self) -> None:
        with mock.patch.object(self.sprints.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.sprints.create(
                    role="po", actor="operator", goal="recovered", repositories=["recovered"],
                    reference="sprint:recovered", request_id="recover-sprint-index",
                )

        self.assertEqual(self.tasks.reconcile(), (1, 0))
        with self.assertRaisesRegex(TaskError, "sprint:recovered") as denied:
            self.tasks.create(
                role="po", actor="operator", project="recovered", task_type="code", title="blocked",
            )
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")

    def test_guard_read_avoids_sprint_comment_history(self) -> None:
        self.client.calls.clear()

        sprint = SprintReader(self.client).show(self.ref, include_cards=False)  # type: ignore[arg-type]

        self.assertNotIn("comments", sprint)
        self.assertFalse(any(method == "getAllComments" for method, _params in self.client.calls))

    def test_observer_can_write_when_another_open_sprint_shares_the_repository(self) -> None:
        other_ref = self.sprints.create(
            role="po", actor="operator", goal="overlap", repositories=["secretary"],
        )["sprint"]["ref"]
        card = self.tasks.create(
            role="observer", actor="second-observer", project="secretary", task_type="code",
            title="second sprint", sprint=other_ref,
        )["task"]

        self.assertEqual(card["sprint"], other_ref)


if __name__ == "__main__":
    unittest.main()
