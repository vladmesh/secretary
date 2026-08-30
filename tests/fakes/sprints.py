from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from secretary.sprint_observer import (
    head_choice,
)
from secretary.sprints import (
    SprintWriter,
    ensure_sprint_board,
)
from secretary.tasks import TaskAudit
from tests.fakes.board import BatchedCalls
from tests.head_registry import write_installed_pair
from tests.observer_identity import bind_observer
from tests.sprint_close_fixtures import DROP_REASON, KEEP_OPEN_REASON

# A close states a verdict on every issue its sprint declared, and every sprint this fixture
# opens declares `issue:open`. The tests below are about the rest of the close, so they give
# the verdict that writes nothing: the issue stays open, with the basis on the close record.
KEEP_THE_ISSUE_OPEN = {
    "issues": [{"ref": "issue:open", "verdict": "open", "reason": KEEP_OPEN_REASON}],
    "cards": [],
}


def drop_cards(*refs: str) -> dict:
    """Keep the fixture's issue open and take the named cards off the closing contract."""
    return {
        "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
        "cards": [{"ref": ref, "verdict": "drop", "reason": DROP_REASON} for ref in refs],
    }


class SprintKanboard(BatchedCalls):
    def __init__(self) -> None:
        self.instance_dir = Path(tempfile.gettempdir())
        self.calls: list[tuple[str, dict]] = []
        self.projects = {"Pipeline": 7}
        self.columns = {
            7: [
                {"id": 1, "title": "Issues"},
                {"id": 2, "title": "Ready"},
                {"id": 3, "title": "In progress"},
                {"id": 4, "title": "Validate"},
                {"id": 5, "title": "Blocked"},
                {"id": 6, "title": "Done"},
            ]
        }
        self.tasks = [
            {
                "id": 12,
                "project_id": 7,
                "reference": "secretary-12",
                "title": "existing",
                "description": "",
                "column_id": 2,
                "position": 1,
                "swimlane_id": 0,
                "date_creation": "1720000000",
                "date_modification": "1720000000",
            }
        ]
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
            status = params.get("status_id")
            if status not in {0, 1}:
                return []
            return [
                task
                for task in self.tasks
                if task["project_id"] == params["project_id"]
                and (int(task.get("is_active", 1) or 0) != 0) == (status == 1)
            ]
        if method == "getTaskByReference":
            return next(
                (
                    task
                    for task in self.tasks
                    if task["project_id"] == params["project_id"] and task["reference"] == params["reference"]
                ),
                None,
            )
        if method == "getTaskMetadata":
            return self.metadata[int(params["task_id"])]
        if method == "getAllComments":
            return self.comments[int(params["task_id"])]
        if method == "createTask":
            task_id = max(int(task["id"]) for task in self.tasks) + 1
            task = {
                "id": task_id,
                "project_id": int(params["project_id"]),
                "reference": params.get("reference", ""),
                "title": params["title"],
                "description": params.get("description", ""),
                "column_id": params["column_id"],
                "position": len(self.tasks) + 1,
                "swimlane_id": params.get("swimlane_id", 0),
                "date_creation": "1720000001",
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
        if method == "removeTask":
            remaining = [task for task in self.tasks if task["id"] != int(params["task_id"])]
            if len(remaining) == len(self.tasks):
                return False
            self.tasks = remaining
            return True
        if method == "createComment":
            self.comments[int(params["task_id"])].append(
                {"date_creation": "1720000003", "comment": params["content"]}
            )
            return 1
        if method == "closeTask":
            task = next(task for task in self.tasks if task["id"] == int(params["task_id"]))
            task["is_active"] = 0
            return True
        raise AssertionError(method)


class ProductSprintKanboard(SprintKanboard):
    """The same two boards, with the Pipeline carrying Product and Issue records.

    A sprint now names the Product it belongs to and the Issues it serves, so the
    fixture holds one product with an open and a closed issue, plus a second product
    to prove a foreign issue is refused.
    """

    def __init__(self) -> None:
        super().__init__()
        self.columns[7] = [{"id": 1, "title": "Issues"}] + self.columns[7][1:]
        self._record(
            20,
            "product:secretary",
            "Secretary",
            {
                "record_type": "product",
                "product_id": "secretary",
                "product_projects": json.dumps(["secretary", "secretary-instance"]),
            },
        )
        self._record(
            21,
            "product:other",
            "Other",
            {
                "record_type": "product",
                "product_id": "other",
                "product_projects": json.dumps(["other"]),
            },
        )
        self._record(
            22,
            "issue:open",
            "Open issue",
            {
                "record_type": "issue",
                "issue_product": "secretary",
                "issue_kind": "feature",
                "issue_priority": "P1",
            },
        )
        self._record(
            23,
            "issue:done",
            "Closed issue",
            {
                "record_type": "issue",
                "issue_product": "secretary",
                "issue_kind": "bug",
                "issue_priority": "P2",
                "issue_closed_reason": "resolved",
            },
            closed=True,
        )
        self._record(
            24,
            "issue:foreign",
            "Issue of another product",
            {
                "record_type": "issue",
                "issue_product": "other",
                "issue_kind": "bug",
                "issue_priority": "P1",
            },
        )

    def _record(
        self, task_id: int, reference: str, title: str, metadata: dict, *, closed: bool = False
    ) -> None:
        self.tasks.append(
            {
                "id": task_id,
                "project_id": 7,
                "reference": reference,
                "title": title,
                "description": "",
                "column_id": 1,
                "position": task_id,
                "swimlane_id": 0,
                "is_active": 0 if closed else 1,
                "date_creation": "1720000000",
                "date_modification": "1720000000",
            }
        )
        self.metadata[task_id] = dict(metadata)
        self.comments[task_id] = []

    def call(self, method: str, **params: object) -> object:
        if method == "getAllTasks":
            self.calls.append((method, params))
            status = params.get("status_id")
            if status not in {0, 1}:
                return []
            return [
                task
                for task in self.tasks
                if task["project_id"] == params["project_id"]
                and (int(task.get("is_active", 1) or 0) != 0) == (status == 1)
            ]
        return super().call(method, **params)


def _write_project_registry(root: Path, *projects: str) -> Path:
    instance = root / "instance"
    (instance / "projects").mkdir(parents=True, exist_ok=True)
    for project in projects:
        (instance / "projects" / f"{project}.yaml").write_text(f"id: {project}\n", encoding="utf-8")
    _write_head_registry(instance)
    return instance


# Opening a sprint resolves its declared observer against this installation's head snapshot, the
# same file the dispatcher launches from, so the fixture instance owns a real one. `retired-observer`
# is deliberately absent: it is the profile the tests declare when they want an unknown one.
HEAD_SNAPSHOT = "resources:\n  openai-sub:\n    account: openai-subscription\n  claude-sub:\n    account: claude-subscription\nprofiles:\n  codex-observer:\n    adapter: codex\n    resource: openai-sub\n  claude-observer:\n    adapter: claude\n    resource: claude-sub\nrole_defaults:\n  new_card: codex-observer\n  reviewer: codex-observer\n  observer: codex-observer\n"


def _write_head_registry(instance: Path) -> Path:
    return write_installed_pair(instance, HEAD_SNAPSHOT)


class SprintFixture(unittest.TestCase):
    """One Product/Issue Pipeline, one sprint board and a real project registry."""

    def setUp(self) -> None:
        self.client = ProductSprintKanboard()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = _write_project_registry(
            Path(self.tmp.name),
            "secretary",
            "secretary-instance",
            "other",
        )
        self.writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
        )

    def _create(self, **kwargs) -> dict:
        """Open a sprint that owns the fixture's product, open issue and project."""
        for field, value in (
            ("role", "po"),
            ("actor", "operator"),
            ("product", "secretary"),
            ("issues", ["issue:open"]),
            ("projects", ["secretary"]),
            ("observer", head_choice("codex-observer")),
        ):
            kwargs.setdefault(field, value)
        created = self.writer.create(**kwargs)
        # The calls that follow act as this sprint's observer head, which is bound to it the way
        # the dispatcher binds a head it launches.
        bind_observer(self, str(created["sprint"]["ref"]))
        return created

    def _events(self) -> list[dict]:
        return TaskAudit(self.tmp.name).events()

    def _sprint_rows(self) -> list[dict]:
        board = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        return [task for task in self.client.tasks if task["project_id"] == board]

    def _transactions(self) -> list[str]:
        directory = Path(self.tmp.name) / "board" / "product-issue-transactions"
        return sorted(path.name for path in directory.glob("v1-*.json")) if directory.is_dir() else []
