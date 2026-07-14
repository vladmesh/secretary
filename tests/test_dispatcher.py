from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from secretary.dispatcher import (
    CutoverState,
    DispatcherRuntime,
    PilotSelector,
)
from secretary.tasks import TaskAudit, TaskReader, TaskWriter


class FakeKanboard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.columns = [
            {"id": 1, "title": "Идеи"},
            {"id": 2, "title": "Ready"},
            {"id": 3, "title": "In progress"},
            {"id": 4, "title": "Validate"},
            {"id": 5, "title": "Blocked"},
            {"id": 6, "title": "Done"},
        ]
        self.tasks = [
            {
                "id": 12,
                "reference": "secretary-510-pilot",
                "title": "Pilot",
                "description": "pilot spec",
                "column_id": 2,
                "position": 1,
                "swimlane_id": 4,
                "date_creation": 1720000000,
                "date_modification": 1720000000,
            },
            {
                "id": 13,
                "reference": "secretary-510-neighbor",
                "title": "Neighbor",
                "description": "do not claim",
                "column_id": 2,
                "position": 2,
                "swimlane_id": 4,
                "date_creation": 1720000000,
                "date_modification": 1720000000,
            },
        ]
        self.metadata = {
            12: {"project": "secretary", "task_type": "code", "slug": "pilot"},
            13: {"project": "secretary", "task_type": "code", "slug": "neighbor"},
        }
        self.comments: dict[int, list[dict]] = {12: [], 13: []}
        self.now = 1720000000

    def call(self, method: str, **params: object) -> object:
        self.calls.append((method, params))
        if method == "getProjectByName":
            return {"id": 7}
        if method == "getColumns":
            return self.columns
        if method == "getActiveSwimlanes":
            return [{"id": 4, "name": "Secretary"}]
        if method == "getAllTasks":
            return self.tasks
        if method == "getTaskByReference":
            return next((task for task in self.tasks if task["reference"] == params["reference"]), None)
        if method == "getTaskMetadata":
            return self.metadata[int(params["task_id"])]
        if method == "saveTaskMetadata":
            self.metadata[int(params["task_id"])].update(params["values"])
            return True
        if method == "moveTaskPosition":
            task = next(task for task in self.tasks if int(task["id"]) == int(params["task_id"]))
            task["column_id"] = params["column_id"]
            self.now += 1
            task["date_modification"] = self.now
            return True
        if method == "createComment":
            self.now += 1
            self.comments[int(params["task_id"])].append(
                {"date_creation": self.now, "comment": params["content"]}
            )
            return len(self.comments[int(params["task_id"])])
        if method == "getAllComments":
            return self.comments[int(params["task_id"])]
        raise AssertionError(method)


class FakeCatalog:
    def worker_head(self, task: dict) -> str:
        return "codex"

    def review_head(self, task: dict) -> str:
        return "codex-reviewer"


class FakeHost:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.prepared: list[str] = []
        self.reviews: list[str] = []
        self.stopped: list[str] = []
        self.completed: list[str] = []

    def prepare_worker(self, task: dict, worker_id: str, head: str) -> dict[str, str]:
        workspace = self.root / worker_id
        workspace.mkdir(parents=True, exist_ok=True)
        self.prepared.append(task["ref"])
        return {"workspace": str(workspace), "handle": f"term:{worker_id}"}

    def start_review(self, task: dict, record) -> str:
        self.reviews.append(task["ref"])
        return f"review:{task['ref']}"

    def restore_workspace(self, task: dict, worker: str) -> str:
        return str(self.root / worker)

    def complete_green(self, task: dict, record) -> None:
        self.completed.append(task["ref"])

    def stop(self, record) -> None:
        self.stopped.append(record.worker)


class DispatcherRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)
        self.board = FakeKanboard()
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]
        self.host = FakeHost(self.data_dir / "workspaces")
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            TaskAudit(self.data_dir),
            CutoverState(self.data_dir),
            FakeCatalog(),  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
        )
        self.selector = PilotSelector.exact("secretary-510-pilot")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def start_pilot(self) -> None:
        self.runtime.pause_old(self.selector, actor="operator", evidence="legacy hard pause")
        started = self.runtime.start_new_pilot(self.selector, actor="operator")
        self.assertEqual(started["status"], "ok")

    def test_tick_fails_closed_without_cutover_guard(self) -> None:
        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.board.calls))

    def test_full_pilot_lifecycle_ignores_neighbor_ready_card(self) -> None:
        self.start_pilot()

        claimed = self.runtime.tick(self.selector)
        self.assertEqual(claimed["step"], "claim")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.reader.show("secretary-510-neighbor")["state"], "ready")

        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="PR: https://github.com/vladmesh/secretary/pull/1",
            request_id="worker-done",
        )
        advanced = self.runtime.tick(self.selector)
        self.assertEqual(advanced["to"], "validate")

        review_started = self.runtime.tick(self.selector)
        self.assertEqual(review_started["action"], "review-started")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="green",
            request_id="review-green",
        )
        done = self.runtime.tick(self.selector)

        self.assertEqual(done["to"], "done")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "done")
        neighbor = self.reader.show("secretary-510-neighbor")
        self.assertEqual(neighbor["state"], "ready")
        self.assertIsNone(neighbor["claim"]["worker"])
        self.assertEqual(self.host.completed, ["secretary-510-pilot"])

    def test_rollback_after_claim_preserves_board_state_and_claim(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)

        result = self.runtime.rollback(self.selector, actor="operator", reason="pilot red")

        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(result["phase"], "rolled_back")
        self.assertEqual(task["state"], "in_progress")
        self.assertEqual(task["claim"]["worker"], "secretary-510-pilot-pilot")
        self.assertEqual(len(task["comments"]), 1)

    def test_validate_adoption_restores_workspace_from_claim(self) -> None:
        self.start_pilot()
        self.board.tasks[0]["column_id"] = 4
        self.board.metadata[12]["claim"] = "secretary-510-pilot-pilot"

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "review-started")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

    def test_rollback_after_worker_report_preserves_validate_card_and_comments(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done body",
            request_id="worker-report",
        )
        self.runtime.tick(self.selector)

        self.runtime.rollback(self.selector, actor="operator", reason="pilot red")

        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "validate")
        self.assertEqual(task["claim"]["worker"], "secretary-510-pilot-pilot")
        self.assertEqual(
            [comment["marker"] for comment in task["comments"]],
            ["dispatcher", "report:done", "dispatcher"],
        )
