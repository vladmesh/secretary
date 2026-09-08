from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

from secretary.product_issues import ProductIssueStore
from secretary.sprint_observer import (
    head_choice,
)
from secretary.sprints import (
    SPRINT_BOARD_NAME,
    SprintReader,
    SprintWriter,
    ensure_sprint_board,
)
from secretary.tasks import TaskAudit, TaskReader
from tests.fakes.board import BatchedCalls
from tests.head_registry import write_installed_pair
from tests.observer_identity import bind_observer
from tests.sprint_close_fixtures import DROP_REASON, KEEP_OPEN_REASON
from tests.sprint_contract import KANBOARD_ONLY_BY_METHOD

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
        repo = root / "project-repos" / project
        repo.mkdir(parents=True, exist_ok=True)
        (instance / "projects" / f"{project}.yaml").write_text(
            f"id: {project}\nrepo: {repo}\nenabled: true\nadapter: secretary\ndefault_branch: main\n",
            encoding="utf-8",
        )
    # A config that validates, because the reads of this installation are reached through
    # `secretary.webproto`, and every operation of that layer resolves the instance before it reads
    # anything. A fixture instance without one is not a smaller installation, it is one no protocol
    # operation can be run against.
    (instance / "instance.yaml").write_text(
        "version: 1\nname: test\n"
        f"data_dir: {root}\n"
        "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
        encoding="utf-8",
    )
    _write_head_registry(instance)
    return instance


# Opening a sprint resolves its declared observer against this installation's head snapshot, the
# same file the dispatcher launches from, so the fixture instance owns a real one. `retired-observer`
# is deliberately absent: it is the profile the tests declare when they want an unknown one.
HEAD_SNAPSHOT = "resources:\n  openai-sub:\n    account: openai-subscription\n  claude-sub:\n    account: claude-subscription\nprofiles:\n  codex-observer:\n    adapter: codex\n    resource: openai-sub\n  claude-observer:\n    adapter: claude\n    resource: claude-sub\nrole_defaults:\n  new_card: codex-observer\n  reviewer: codex-observer\n  observer: codex-observer\n"


def _write_head_registry(instance: Path) -> Path:
    return write_installed_pair(instance, HEAD_SNAPSHOT)


class SprintBackendFixture:
    """One factory and exclusion policy shared by every portable sprint suite."""

    BACKEND = "kanboard"
    KANBOARD_ONLY: ClassVar[dict[str, str]] = KANBOARD_ONLY_BY_METHOD

    def make_sprint_client(self) -> ProductSprintKanboard:
        return ProductSprintKanboard()

    def skip_kanboard_only(self) -> None:
        reason = self.KANBOARD_ONLY.get(self._testMethodName)  # type: ignore[attr-defined]
        if reason and self.BACKEND != "kanboard":
            self.skipTest(f"Kanboard-only: {reason}")  # type: ignore[attr-defined]


class SprintFixture(SprintBackendFixture, unittest.TestCase):
    """Backend-neutral fixture boundary for the shared sprint contract.

    The Kanboard fake is an implementation detail of ``make_sprint_client``.  A future SQL
    contract class overrides that factory and, where necessary, the small arrangement and
    observation methods below.  Shared test bodies speak only through SprintReader,
    SprintWriter, TaskReader/TaskWriter, or these helpers.  They never need the fake's row,
    metadata, comment, RPC-log, or transaction-file representation.
    """

    def setUp(self) -> None:
        self.skip_kanboard_only()
        self.client = self.make_sprint_client()
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

    def sprint_reader(self) -> SprintReader:
        return SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

    def sprint(self, reference: str, *, include_cards: bool = False) -> dict[str, Any]:
        return self.sprint_reader().show(reference, include_cards=include_cards)

    def sprints(self) -> list[dict[str, Any]]:
        return self.sprint_reader().list()

    def sprint_record_count(self, reference: str | None = None) -> int:
        """Count persisted sprint records through the client boundary, active and archived."""
        project = self.client.call("getProjectByName", name=SPRINT_BOARD_NAME)
        if not isinstance(project, dict) or not project.get("id"):
            return 0
        rows = [
            row
            for status_id in (1, 0)
            for row in self.client.call("getAllTasks", project_id=int(project["id"]), status_id=status_id)
        ]
        if reference is None:
            return len(rows)
        return sum(row.get("reference") == reference for row in rows)

    def transaction_state(self) -> dict[str, int | bool]:
        """Observe recovery ownership without knowing the Kanboard journal's filenames."""
        return self.writer.transactions.status()

    def arrange_metadata(self, reference: str, **values: object) -> None:
        """Arrange legacy/corrupt persisted values at the public client boundary."""
        project = self.client.call("getProjectByName", name=SPRINT_BOARD_NAME)
        if not isinstance(project, dict) or not project.get("id"):
            self.fail(f"sprint board is not visible while arranging {reference}")
        row = self.client.call("getTaskByReference", project_id=int(project["id"]), reference=reference)
        if not isinstance(row, dict):
            self.fail(f"sprint record is not visible while arranging {reference}")
        result = self.client.call("saveTaskMetadata", task_id=int(row["id"]), values=values)
        if result is not True:
            self.fail(f"backend refused fixture metadata for {reference}")

    def arrange_historical_sprint(self, reference: str, *, status: str = "closed") -> dict[str, Any]:
        return self.writer.restore_create(
            reference=reference,
            goal="historical",
            status=status,
            request_id=f"fixture-{reference}",
            observer=head_choice("codex-observer"),
        )["sprint"]

    def arrange_issue_closed(self, reference: str = "issue:open") -> None:
        self.product_issue_store().close_issue(
            reference=reference,
            reason="resolved",
            actor="fixture",
            request_id=f"fixture-close-{reference}",
        )

    def product_issue_store(self) -> ProductIssueStore:
        """The public ownership contract used to arrange Product and Issue records."""
        return ProductIssueStore(  # type: ignore[arg-type]
            self.client, data_dir=self.tmp.name, instance=self.instance
        )

    def arrange_product(self, product_id: str, *, projects: list[str]) -> dict[str, Any]:
        """Arrange fixed ownership identities inside the backend factory seam.

        Product/Issue's public create contract allocates Issue references, while sprint fixtures
        intentionally exercise stable named ownership. A future SQL fixture overrides this method
        to seed its disposable ownership tables; portable suites never assume Pipeline rows.
        """
        reference = f"product:{product_id}"
        task_id = max(int(row["id"]) for row in self.client.tasks) + 1
        self.client._record(
            task_id,
            reference,
            product_id.title(),
            {
                "record_type": "product",
                "product_id": product_id,
                "product_projects": json.dumps(projects),
            },
        )
        return self.product_issue_store().show_product(product_id)

    def arrange_issue(self, reference: str, *, product: str) -> dict[str, Any]:
        task_id = max(int(row["id"]) for row in self.client.tasks) + 1
        self.client._record(
            task_id,
            reference,
            reference,
            {
                "record_type": "issue",
                "issue_product": product,
                "issue_kind": "feature",
                "issue_priority": "P1",
            },
        )
        return self.product_issue_store().show_issue(reference)

    def arrange_record_active(self, reference: str, *, active: bool) -> None:
        project = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(project, dict) or not project.get("id"):
            self.fail("Pipeline is not visible")
        row = self.client.call("getTaskByReference", project_id=int(project["id"]), reference=reference)
        if not isinstance(row, dict):
            self.fail(f"record is not visible while arranging {reference}")
        if not active:
            result = self.client.call("closeTask", task_id=int(row["id"]))
            if result is not True:
                self.fail(f"backend refused fixture archive for {reference}")

    def arrange_pipeline_metadata(self, reference: str, **values: object) -> None:
        project = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(project, dict) or not project.get("id"):
            self.fail("Pipeline is not visible")
        row = self.client.call("getTaskByReference", project_id=int(project["id"]), reference=reference)
        if not isinstance(row, dict):
            self.fail(f"record is not visible while arranging {reference}")
        result = self.client.call("saveTaskMetadata", task_id=int(row["id"]), values=values)
        if result is not True:
            self.fail(f"backend refused fixture metadata for {reference}")

    def task(self, reference: str) -> dict[str, Any]:
        return TaskReader(self.client).show(reference)  # type: ignore[arg-type]

    def record_is_active(self, reference: str) -> bool:
        project = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(project, dict) or not project.get("id"):
            self.fail("Pipeline is not visible")
        for status_id, active in ((1, True), (0, False)):
            rows = self.client.call("getAllTasks", project_id=int(project["id"]), status_id=status_id)
            if any(row.get("reference") == reference for row in rows):
                return active
        self.fail(f"record is not visible: {reference}")

    def record_comments(self, reference: str) -> list[str]:
        project = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(project, dict) or not project.get("id"):
            self.fail("Pipeline is not visible")
        row = self.client.call("getTaskByReference", project_id=int(project["id"]), reference=reference)
        if not isinstance(row, dict):
            self.fail(f"record is not visible: {reference}")
        comments = self.client.call("getAllComments", task_id=int(row["id"]))
        return [str(comment["comment"]) for comment in comments]

    @contextlib.contextmanager
    def named_failure(
        self,
        boundary: str,
        *,
        result: object = False,
        error: Exception | None = None,
    ) -> Iterator[None]:
        """Fail one semantic SprintWriter boundary without naming an RPC in a test body."""
        methods = {
            "record_create": "createTask",
            "record_metadata": "saveTaskMetadata",
            "record_reference": "updateTask",
            "record_remove": "removeTask",
            "record_comment": "createComment",
            "record_archive": "closeTask",
        }
        method = methods[boundary]
        original = self.client.call
        armed = True

        def call(method_name: str, **params: object) -> object:
            nonlocal armed
            if armed and method_name == method:
                armed = False
                if error is not None:
                    raise error
                return result
            return original(method_name, **params)

        self.client.call = call  # type: ignore[method-assign]
        try:
            yield
        finally:
            self.client.call = original  # type: ignore[method-assign]

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
        """Kanboard-only raw-row observation retained for named recovery cases."""
        board = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        return [
            row
            for status_id in (1, 0)
            for row in self.client.call("getAllTasks", project_id=board, status_id=status_id)
        ]

    def _transactions(self) -> list[str]:
        directory = Path(self.tmp.name) / "board" / "product-issue-transactions"
        return sorted(path.name for path in directory.glob("v1-*.json")) if directory.is_dir() else []
