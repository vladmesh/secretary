from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from unittest import mock

from secretary.tasks import (
    TaskError,
)
from tests.fakes.board import BatchedCalls
from tests.observer_identity import as_observer

CARD_STATES = ("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done")


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


class FakeKanboard(BatchedCalls):
    def __init__(self) -> None:
        self.instance_dir = Path(tempfile.gettempdir())
        self.calls: list[tuple[str, dict]] = []
        self.tasks = [
            {
                "id": "12",
                "reference": "secretary-468",
                "title": "Readonly task protocol",
                "description": "",
                "column_id": "2",
                "position": "3",
                "swimlane_id": "4",
                "date_creation": "1720000000",
                "date_modification": "1720000010",
            },
            {"id": 13, "reference": "old-1", "title": "Old", "column_id": 1, "position": "bad"},
        ]
        self.metadata = {
            12: {
                "project": "secretary",
                "task_type": "code",
                "claim": "codex-terra",
                "head": "codex-terra",
                "retry_same": "2",
                "retry_switch": "bad",
                "retry_heads": "codex-terra,claude-opus",
                "steward_report": "1",
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
                task
                for task in self.tasks
                if (int(task.get("is_active", task.get("status", 1)) or 0) != 0) == (status == 1)
            ]
        if method == "getTaskByReference":
            return next((task for task in self.tasks if task["reference"] == params["reference"]), None)
        if method == "getTaskMetadata":
            return self.metadata[int(params["task_id"])]
        if method == "getAllComments":
            return [{"date_creation": 1720000020, "comment": "[report:done]\nReady for review"}]
        raise AssertionError(method)


class WriteKanboard(FakeKanboard):
    fail_comments = False
    lose_comment_reply = False
    fail_metadata = False
    fail_move = False
    fail_update = False
    fail_close = False
    # Two faults that can only happen after a column move has already been applied: the transport
    # dropping the very next round trip, and another writer moving the card onward before anyone
    # reads it back.  Both are what a live JSON-RPC board does and the in-memory client cannot.
    fail_read_after_move = False
    race_column_after_move: int | None = None

    def __init__(self) -> None:
        super().__init__()
        self.comments: dict[int, list[dict[str, object]]] = {int(task["id"]): [] for task in self.tasks}
        # Свимлейны борда, а не константа ответа: их создают по требованию, как живой Kanboard.
        self.swimlanes: list[dict[str, object]] = [{"id": 4, "name": "Secretary", "position": 1}]
        self._unavailable_next_call = False

    def call(self, method: str, **params: object) -> object:
        if self._unavailable_next_call:
            self._unavailable_next_call = False
            raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1)
        if method == "getActiveSwimlanes":
            self.calls.append((method, params))
            return [dict(lane) for lane in self.swimlanes]
        if method == "addSwimlane":
            self.calls.append((method, params))
            # Kanboard отвечает на дубликат имени false, а не идентификатором существующего.
            if any(lane["name"] == params["name"] for lane in self.swimlanes):
                return False
            identifier = max((int(lane["id"]) for lane in self.swimlanes), default=0) + 1
            self.swimlanes.append(
                {"id": identifier, "name": params["name"], "position": len(self.swimlanes) + 1}
            )
            return identifier
        if method == "getColumns":
            return [
                {"id": 1, "title": "Issues"},
                {"id": 2, "title": "Ready"},
                {"id": 3, "title": "In progress"},
                {"id": 4, "title": "Validate"},
                {"id": 7, "title": "Assessment"},
                {"id": 5, "title": "Blocked"},
                {"id": 6, "title": "Done"},
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
            self.tasks.append(
                {
                    "id": task_id,
                    "reference": params.get("reference", ""),
                    "title": params["title"],
                    "description": params.get("description", ""),
                    "column_id": params["column_id"],
                    "position": len(self.tasks) + 1,
                    "swimlane_id": params.get("swimlane_id") or 0,
                    "date_creation": "1720000200",
                    "date_modification": "1720000200",
                }
            )
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
                    candidate
                    for candidate in self.tasks
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
            # The move is applied and its reply is on the wire.  Whatever happens next happens
            # to a card that has already changed column.
            if self.race_column_after_move is not None:
                task["column_id"] = self.race_column_after_move
            if self.fail_read_after_move:
                self._unavailable_next_call = True
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
