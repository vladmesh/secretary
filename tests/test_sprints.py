from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from secretary.cli import main
from secretary.data import normalize_sprint_entity
from secretary.sprints import (
    BUDGET_EVENT_TYPES,
    SprintReader,
    SprintWriter,
    active_sprint_repositories,
    budget_thresholds,
    ensure_sprint_board,
    sprint_admission_lock,
)
from secretary.tasks import TaskAudit, TaskError, TaskReader, TaskWriter


class SprintKanboard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.projects = {"Pipeline": 7}
        self.columns = {
            7: [
                {"id": 1, "title": "Ideas"}, {"id": 2, "title": "Ready"},
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


class ProductSprintKanboard(SprintKanboard):
    """The same two boards, with the Pipeline carrying Product and Issue records.

    A sprint now names the Product it belongs to and the Issues it serves, so the
    fixture holds one product with an open and a closed issue, plus a second product
    to prove a foreign issue is refused.
    """

    def __init__(self) -> None:
        super().__init__()
        self.columns[7] = [{"id": 1, "title": "Issues"}] + self.columns[7][1:]
        self._record(20, "product:secretary", "Secretary", {
            "record_type": "product", "product_id": "secretary",
            "product_projects": json.dumps(["secretary", "secretary-instance"]),
        })
        self._record(21, "product:other", "Other", {
            "record_type": "product", "product_id": "other",
            "product_projects": json.dumps(["other"]),
        })
        self._record(22, "issue:open", "Open issue", {
            "record_type": "issue", "issue_product": "secretary", "issue_kind": "feature",
            "issue_priority": "P1",
        })
        self._record(23, "issue:done", "Closed issue", {
            "record_type": "issue", "issue_product": "secretary", "issue_kind": "bug",
            "issue_priority": "P2", "issue_closed_reason": "resolved",
        }, closed=True)
        self._record(24, "issue:foreign", "Issue of another product", {
            "record_type": "issue", "issue_product": "other", "issue_kind": "bug",
            "issue_priority": "P1",
        })

    def _record(self, task_id: int, reference: str, title: str, metadata: dict, *, closed: bool = False) -> None:
        self.tasks.append({
            "id": task_id, "project_id": 7, "reference": reference, "title": title,
            "description": "", "column_id": 1, "position": task_id, "swimlane_id": 0,
            "is_active": 0 if closed else 1,
            "date_creation": "1720000000", "date_modification": "1720000000",
        })
        self.metadata[task_id] = dict(metadata)
        self.comments[task_id] = []

    def call(self, method: str, **params: object) -> object:
        if method == "getAllTasks":
            self.calls.append((method, params))
            status = params.get("status_id")
            return [
                task for task in self.tasks
                if task["project_id"] == params["project_id"]
                and (status == 2 or (int(task.get("is_active", 1) or 0) != 0) == (status == 1))
            ]
        return super().call(method, **params)


def _write_project_registry(root: Path, *projects: str) -> Path:
    instance = root / "instance"
    (instance / "projects").mkdir(parents=True, exist_ok=True)
    for project in projects:
        (instance / "projects" / f"{project}.yaml").write_text(f"id: {project}\n", encoding="utf-8")
    return instance


class SprintFixture(unittest.TestCase):
    """One Product/Issue Pipeline, one sprint board and a real project registry."""

    def setUp(self) -> None:
        self.client = ProductSprintKanboard()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = _write_project_registry(
            Path(self.tmp.name), "secretary", "secretary-instance", "other",
        )
        self.writer = SprintWriter(  # type: ignore[arg-type]
            self.client, data_dir=self.tmp.name, instance=self.instance,
        )

    def _create(self, **kwargs) -> dict:
        """Open a sprint that owns the fixture's product, open issue and project."""
        for field, value in (
            ("role", "po"), ("actor", "operator"), ("product", "secretary"),
            ("issues", ["issue:open"]), ("projects", ["secretary"]),
        ):
            kwargs.setdefault(field, value)
        return self.writer.create(**kwargs)


class SprintOwnershipTests(SprintFixture):
    """A sprint belongs to a Product, serves its open Issues and reserves projects."""

    def _events(self) -> list[dict]:
        return TaskAudit(self.tmp.name).events()

    def _assert_nothing_was_written(self) -> None:
        self.assertEqual(self._events(), [])
        self.assertFalse(any(
            method in {"createTask", "saveTaskMetadata", "createProject"}
            for method, _params in self.client.calls
        ))

    def test_create_requires_product_issue_and_reservation_before_any_write(self) -> None:
        for kwargs, message in (
            ({"product": ""}, "owning product"),
            ({"issues": []}, "at least one open issue"),
            ({"projects": []}, "at least one reserved project"),
            ({"product": "ghost"}, "was not found"),
            ({"issues": ["issue:missing"]}, "was not found"),
            ({"projects": ["unregistered"]}, "unknown registered project"),
        ):
            self.client.calls.clear()
            with self.assertRaisesRegex(TaskError, message):
                self._create(goal="rejected", **kwargs)
            self._assert_nothing_was_written()

    def test_foreign_and_closed_issues_are_refused_separately(self) -> None:
        with self.assertRaisesRegex(TaskError, "belongs to product 'other'") as foreign:
            self._create(goal="foreign issue", issues=["issue:foreign"])
        self.assertEqual(foreign.exception.code, "validation")

        with self.assertRaisesRegex(TaskError, "is closed") as closed:
            self._create(goal="closed issue", issues=["issue:done"])
        self.assertEqual(closed.exception.code, "validation")
        self._assert_nothing_was_written()

    def test_second_open_sprint_is_refused_and_names_the_open_one(self) -> None:
        first = self._create(goal="first", reference="sprint:first")["sprint"]["ref"]

        with self.assertRaisesRegex(TaskError, first) as raised:
            self._create(
                goal="second", reference="sprint:second", projects=["secretary-instance"],
            )

        self.assertEqual(raised.exception.code, "sprint_conflict")
        self.assertEqual([sprint["ref"] for sprint in SprintReader(self.client).list()], [first])  # type: ignore[arg-type]

    def test_a_reserved_project_is_a_resource_conflict_of_its_own(self) -> None:
        first = self._create(goal="first", reference="sprint:first")["sprint"]["ref"]

        with self.assertRaisesRegex(TaskError, "already reserved") as raised:
            self._create(goal="second", reference="sprint:second", projects=["secretary"])

        self.assertEqual(raised.exception.code, "resource_conflict")
        self.assertIn("secretary held by " + first, raised.exception.message)

    def test_a_closed_sprint_releases_its_reservation(self) -> None:
        first = self._create(goal="first", reference="sprint:first")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=first)

        second = self._create(goal="second", reference="sprint:second")["sprint"]

        self.assertEqual(second["reservations"], ["secretary"])

    def test_create_replay_returns_the_same_event_instead_of_conflicting_with_itself(self) -> None:
        first = self._create(goal="replayed", request_id="create-once")
        second = self._create(goal="replayed", request_id="create-once")

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["sprint"]["ref"], second["sprint"]["ref"])
        self.assertEqual([event["kind"] for event in self._events()], ["created"])

    def test_a_concurrent_repeat_of_one_request_is_replayed_not_refused(self) -> None:
        """At-least-once delivery may overlap with the request it repeats.

        Both callers are held at the admission gate before either can look at live
        state, so neither could have seen the other's sprint. The repeat has to come
        back with the first event instead of colliding with the sprint it opened.
        """
        ensure_sprint_board(self.client)  # type: ignore[arg-type]
        started = threading.Barrier(3)
        outcomes: dict[str, Any] = {}

        def deliver(name: str) -> None:
            writer = SprintWriter(  # type: ignore[arg-type]
                self.client, data_dir=self.tmp.name, instance=self.instance,
            )
            started.wait(timeout=5)
            try:
                outcomes[name] = writer.create(
                    role="po", actor="operator", goal="one delivery", reference="sprint:once",
                    product="secretary", issues=["issue:open"], projects=["secretary"],
                    request_id="same-delivery",
                )
            except TaskError as exc:
                outcomes[name] = exc

        threads = [threading.Thread(target=deliver, args=(name,)) for name in ("first", "second")]
        with sprint_admission_lock(self.tmp.name):
            for thread in threads:
                thread.start()
            # Both are inside `create` and waiting for the gate before it is released.
            started.wait(timeout=5)
            time.sleep(0.2)
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        self.assertEqual([type(value) for value in outcomes.values()], [dict, dict], outcomes)
        self.assertEqual(
            len({result["event_id"] for result in outcomes.values()}), 1, outcomes
        )
        self.assertEqual([event["kind"] for event in self._events()], ["created"])
        self.assertEqual([sprint["ref"] for sprint in SprintReader(self.client).list()], ["sprint:once"])  # type: ignore[arg-type]

    def _refuse_metadata(self, field: str):
        """Answer the first metadata write carrying `field` the way Kanboard refuses."""
        original = self.client.call
        refused: list[str] = []

        def refuse(method: str, **params: object) -> object:
            if method == "saveTaskMetadata" and field in dict(params["values"]) and not refused:  # type: ignore[arg-type]
                refused.append(method)
                return False
            return original(method, **params)

        return mock.patch.object(self.client, "call", side_effect=refuse)

    def _stall_create(self, request_id: str) -> dict:
        """Leave one admitted sprint in `opening`, repairable by its own request id."""
        with self._refuse_metadata("sprint_goal"):
            with self.assertRaisesRegex(TaskError, "pending repair") as pending:
                self._create(goal="rejected metadata", request_id=request_id)
        self.assertEqual(pending.exception.code, "audit_pending")
        self.assertEqual(self._events(), [])
        return SprintReader(self.client).list(create=False)[0]  # type: ignore[arg-type]

    def test_a_refused_metadata_write_leaves_the_sprint_opening_and_repairable(self) -> None:
        """Kanboard may refuse the metadata write that carries the whole ownership.

        Reporting `created` on it would leave an open sprint with no product, issues or
        reservations. The row stays `opening` instead: it is on the board with its own
        reference, no reader counts it as open, and the repeat with the same request id
        finishes that very operation.
        """
        stalled = self._stall_create("metadata-once")

        self.assertEqual(stalled["status"], "opening")
        self.assertTrue(stalled["ref"].startswith("sprint:"))
        for field in ("product", "issues", "reservations"):
            self.assertNotIn(field, stalled)
        self.assertEqual(SprintReader(self.client).list(statuses={"open"}, create=False), [])  # type: ignore[arg-type]

        repaired = self._create(goal="rejected metadata", request_id="metadata-once")

        self.assertEqual(repaired["action"], "created")
        self.assertEqual(repaired["sprint"]["ref"], stalled["ref"])
        self.assertEqual(repaired["sprint"]["status"], "open")
        self.assertEqual(repaired["sprint"]["product"], "secretary")
        self.assertEqual(repaired["sprint"]["issues"], ["issue:open"])
        self.assertEqual(repaired["sprint"]["reservations"], ["secretary"])
        self.assertEqual([event["kind"] for event in self._events()], ["created"])
        board = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        self.assertEqual(len([task for task in self.client.tasks if task["project_id"] == board]), 1)

    def test_an_opening_sprint_holds_admission_whatever_the_next_create_reserves(self) -> None:
        """The hold is the unfinished sprint itself, not the projects it happens to want.

        Between the refusal and the repair there must be no window for a second sprint:
        a fresh create is refused whether or not it collides on a project, and the error
        names the delivery that has to be finished first.
        """
        stalled = self._stall_create("held-open")

        for projects in (["secretary"], ["secretary-instance"]):
            with self.assertRaisesRegex(TaskError, "unfinished sprint") as raised:
                self._create(goal="second", reference="sprint:second", projects=projects)
            self.assertEqual(raised.exception.code, "sprint_conflict")
            self.assertIn(stalled["ref"], raised.exception.message)
        self.assertEqual(self._events(), [])

        repaired = self._create(goal="rejected metadata", request_id="held-open")

        self.assertEqual([sprint["ref"] for sprint in SprintReader(self.client).list()], [repaired["sprint"]["ref"]])  # type: ignore[arg-type]

    def test_an_opening_sprint_is_read_as_open_nowhere(self) -> None:
        stalled = self._stall_create("never-open")
        reader = SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        reference = stalled["ref"]

        self.assertEqual(reader.show(reference)["status"], "opening")
        self.assertEqual(reader.status(reference)["status"], "opening")
        self.assertEqual(reader.list(statuses={"open"}, create=False), [])
        self.assertEqual([sprint["ref"] for sprint in reader.list(statuses={"opening"})], [reference])
        self.assertEqual(reader.export()[0]["status"], "opening")
        # The sprint holds no repository for the card guard either: it has no cards yet.
        self.assertEqual(active_sprint_repositories(self.tmp.name), {})
        with self.assertRaisesRegex(TaskError, "has not finished opening") as raised:
            self.writer.comment(role="po", actor="operator", reference=reference, body="early")
        self.assertEqual(raised.exception.code, "validation")
        with self.assertRaisesRegex(TaskError, "has not finished opening"):
            self.writer.reopen(role="po", actor="operator", reference=reference)

    def test_a_repeated_create_records_exactly_one_audit_event(self) -> None:
        first = self._create(goal="repeated", request_id="repeat-once")
        results = [self._create(goal="repeated", request_id="repeat-once") for _ in range(3)]

        self.assertEqual({result["event_id"] for result in results}, {first["event_id"]})
        self.assertEqual([event["kind"] for event in self._events()], ["created"])

    def test_a_repeat_with_another_payload_is_refused_before_any_side_effect(self) -> None:
        self._create(goal="original", reference="sprint:original", request_id="claimed")
        self.client.calls.clear()

        with self.assertRaisesRegex(TaskError, "request id belongs to another operation") as raised:
            self._create(goal="different", reference="sprint:other", request_id="claimed")

        self.assertEqual(raised.exception.code, "validation")
        self.assertFalse(any(method == "createTask" for method, _params in self.client.calls))
        self.assertEqual([event["kind"] for event in self._events()], ["created"])

    def test_concurrent_creates_admit_exactly_one_open_sprint(self) -> None:
        """Two writers that check at the same time still open one sprint between them.

        The rules are reads of live state, so without a shared gate both would see an
        installation with no open sprint and both would create a row.
        """
        ensure_sprint_board(self.client)  # type: ignore[arg-type]
        start = threading.Barrier(2)
        outcomes: dict[str, Any] = {}

        def open_sprint(name: str, project: str) -> None:
            writer = SprintWriter(  # type: ignore[arg-type]
                self.client, data_dir=self.tmp.name, instance=self.instance,
            )
            start.wait(timeout=5)
            try:
                outcomes[name] = writer.create(
                    role="po", actor="operator", goal=name, reference=f"sprint:{name}",
                    product="secretary", issues=["issue:open"], projects=[project],
                )
            except TaskError as exc:
                outcomes[name] = exc

        threads = [
            threading.Thread(target=open_sprint, args=("left", "secretary")),
            threading.Thread(target=open_sprint, args=("right", "secretary-instance")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        refused = [value for value in outcomes.values() if isinstance(value, TaskError)]
        self.assertEqual(len(refused), 1, outcomes)
        self.assertEqual(refused[0].code, "sprint_conflict")
        open_sprints = SprintReader(self.client).list(statuses={"open"})  # type: ignore[arg-type]
        self.assertEqual(len(open_sprints), 1, [sprint["ref"] for sprint in open_sprints])

    def test_opening_a_sprint_waits_for_the_installation_admission_gate(self) -> None:
        """Both ways into `open` take the gate, so neither can slip past a holder."""
        ref = self._create(goal="gated", reference="sprint:gated")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref)

        for name, call in (
            ("reopen", lambda: self.writer.reopen(role="po", actor="operator", reference=ref)),
            ("create", lambda: self._create(goal="second", reference="sprint:second")),
        ):
            done = threading.Event()
            worker = threading.Thread(target=lambda: (call(), done.set()))
            with sprint_admission_lock(self.tmp.name):
                worker.start()
                self.assertFalse(done.wait(timeout=0.3), name)
            worker.join(timeout=10)
            self.assertTrue(done.is_set(), name)
            self.writer.close(role="po", actor="operator", reference=ref)

    def test_a_sprint_without_ownership_gains_none_in_show_status_or_export(self) -> None:
        """The 13 sprints closed before ownership existed keep the fields they had."""
        board = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        self.writer.restore_create(reference="sprint:legacy", goal="legacy", request_id="legacy")
        row = next(task for task in self.client.tasks if task["reference"] == "sprint:legacy")
        self.assertEqual(
            [key for key in self.client.metadata[row["id"]] if key in
             {"sprint_product", "sprint_issues", "sprint_reservations"}],
            [],
        )
        reader = SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

        views = [
            reader.show("sprint:legacy"), reader.status("sprint:legacy"),
            reader.export()[0], reader.list()[0],
            normalize_sprint_entity(reader.export()[0]),
        ]

        self.assertEqual(board, row["project_id"])
        for view in views:
            for field in ("product", "issues", "reservations"):
                self.assertNotIn(field, view)

    def test_show_status_and_export_carry_the_new_links(self) -> None:
        ref = self._create(goal="linked", projects=["secretary", "secretary-instance"])["sprint"]["ref"]
        reader = SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

        shown = reader.show(ref)
        status = reader.status(ref)
        exported = reader.export()[0]

        for view in (shown, status, exported):
            self.assertEqual(view["product"], "secretary")
            self.assertEqual(view["issues"], ["issue:open"])
            self.assertEqual(view["reservations"], ["secretary", "secretary-instance"])

    def test_reopen_of_a_legacy_sprint_fails_closed_without_filling_fields(self) -> None:
        legacy = self.writer.restore_create(
            reference="sprint:legacy", goal="legacy", request_id="legacy-create",
        )["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=legacy)

        with self.assertRaisesRegex(TaskError, "predates sprint ownership") as raised:
            self.writer.reopen(role="po", actor="operator", reference=legacy)

        self.assertEqual(raised.exception.code, "validation")
        reread = SprintReader(self.client).show(legacy, include_cards=False)  # type: ignore[arg-type]
        self.assertEqual(reread["status"], "closed")
        for field in ("product", "issues", "reservations"):
            self.assertNotIn(field, reread)

    def test_reopen_rechecks_ownership_and_stays_idempotent(self) -> None:
        ref = self._create(goal="reopened", reference="sprint:reopened")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref)

        first = self.writer.reopen(role="po", actor="operator", reference=ref, request_id="reopen-once")
        second = self.writer.reopen(role="po", actor="operator", reference=ref, request_id="reopen-once")

        self.assertEqual(first["sprint"]["status"], "open")
        self.assertEqual(first["event_id"], second["event_id"])

    def test_reopen_refuses_a_request_id_that_belongs_to_another_operation(self) -> None:
        """The transition into `open` owns its request id like every other one.

        A caller that passes the id of its own `close` back to `reopen` is delivering a
        second operation under one id. It has to be refused before a side effect instead
        of being replayed into a `reopened` answer the board never made.
        """
        ref = self._create(goal="reused id", reference="sprint:reused")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, request_id="reused-id")
        self.client.calls.clear()

        with self.assertRaisesRegex(TaskError, "request id belongs to another operation") as raised:
            self.writer.reopen(role="po", actor="operator", reference=ref, request_id="reused-id")

        self.assertEqual(raised.exception.code, "validation")
        self.assertFalse(any(method == "saveTaskMetadata" for method, _params in self.client.calls))
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        self.assertEqual([event["kind"] for event in self._events()], ["created", "closed"])

    def test_a_refused_reopen_stays_repairable_and_reports_no_transition(self) -> None:
        ref = self._create(goal="refused reopen", reference="sprint:refused")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref)

        with self._refuse_metadata("sprint_status"):
            with self.assertRaisesRegex(TaskError, "pending repair") as pending:
                self.writer.reopen(role="po", actor="operator", reference=ref, request_id="reopen-repair")

        self.assertEqual(pending.exception.code, "audit_pending")
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        self.assertEqual([event["kind"] for event in self._events()], ["created", "closed"])

        repaired = self.writer.reopen(role="po", actor="operator", reference=ref, request_id="reopen-repair")
        replay = self.writer.reopen(role="po", actor="operator", reference=ref, request_id="reopen-repair")

        self.assertEqual(repaired["action"], "reopened")
        self.assertEqual(repaired["sprint"]["status"], "open")
        self.assertEqual(repaired["event_id"], replay["event_id"])
        self.assertEqual([event["kind"] for event in self._events()], ["created", "closed", "reopened"])

    def test_reopen_is_refused_when_its_only_issue_has_been_closed(self) -> None:
        ref = self._create(goal="issue closed later", reference="sprint:stale")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref)
        issue = next(task for task in self.client.tasks if task["reference"] == "issue:open")
        issue["is_active"] = 0
        self.client.metadata[issue["id"]]["issue_closed_reason"] = "resolved"

        with self.assertRaisesRegex(TaskError, "is closed"):
            self.writer.reopen(role="po", actor="operator", reference=ref)

    def test_board_layout_without_issues_column_fails_closed(self) -> None:
        self.client.columns[7][0] = {"id": 1, "title": "Ideas"}

        with self.assertRaises(TaskError) as raised:
            self._create(goal="legacy layout")

        self.assertEqual(raised.exception.code, "legacy_layout")
        self._assert_nothing_was_written()


class SprintTests(SprintFixture):
    def test_board_creation_is_idempotent(self) -> None:
        first = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        second = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        self.assertEqual(first, second)
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createProject"]), 1)

    def test_read_only_sprint_list_does_not_create_a_board_or_claim_resume_freshness(self) -> None:
        reader = SprintReader(self.client)  # type: ignore[arg-type]
        self.assertEqual(reader.list(create=False), [])
        self.assertFalse(any(call[0] == "createProject" for call in self.client.calls))

        created = self._create(goal="list")
        listed = reader.list()
        self.assertEqual(listed[0]["ref"], created["sprint"]["ref"])
        self.assertNotIn("resume_freshness", listed[0])

    def test_create_has_only_contract_fields_and_rejects_duplicate_reference(self) -> None:
        created = self._create(
            goal="Ship sprint entity", definition_of_done="tests pass",
            repositories=["secretary", "secretary"], projects=["secretary", "secretary"],
            reference="sprint:entity", request_id="create",
        )
        sprint = created["sprint"]
        self.assertEqual(sprint["repositories"], ["secretary"])
        self.assertEqual(sprint["product"], "secretary")
        self.assertEqual(sprint["issues"], ["issue:open"])
        self.assertEqual(sprint["reservations"], ["secretary"])
        self.assertEqual(sprint["status"], "open")
        self.assertEqual(sprint["budget"]["total"], 0)
        self.assertEqual(sprint["budget"]["by_type"], {event: 0 for event in BUDGET_EVENT_TYPES})
        self.assertFalse(sprint["budget"]["signal_reached"])
        self.assertIsNone(sprint["current_task"])
        self.assertNotIn("title", sprint)
        # The reference is only reachable once the installation is free to open a sprint.
        self.writer.close(role="po", actor="operator", reference=sprint["ref"])
        with self.assertRaisesRegex(TaskError, "already exists") as raised:
            self._create(goal="another", reference="sprint:entity")
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

        ref = self._create(goal="export")["sprint"]["ref"]
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
        ref = self._create(goal="restore")["sprint"]["ref"]
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
        ref = self._create(goal="budget")["sprint"]["ref"]
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
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client, data_dir=self.tmp.name, instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = writer.create(
            role="po", actor="operator", goal="hard limit", product="secretary",
            issues=["issue:open"], projects=["secretary"],
        )["sprint"]["ref"]

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
        ref = self._create(goal="link")["sprint"]["ref"]
        task_writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        task_writer.create(
            role="po", actor="operator", project="secretary", task_type="code", title="linked", target="ready",
            sprint=ref, request_id="linked-card",
        )
        self.assertEqual(TaskReader(self.client).list(sprint=ref)[0]["sprint"], ref)  # type: ignore[arg-type]
        shown = SprintReader(self.client).show(ref)  # type: ignore[arg-type]
        self.assertEqual([card["ref"] for card in shown["cards"]], ["secretary-26"])
        self.writer.close(role="po", actor="operator", reference=ref)
        with self.assertRaisesRegex(TaskError, "closed"):
            self.writer.comment(role="worker", actor="worker", reference=ref, body="late")
        with self.assertRaisesRegex(TaskError, "closed"):
            task_writer.create(role="po", actor="operator", project="secretary", task_type="code", title="late", target="ready", sprint=ref)

    def test_cli_create_and_list_return_stable_json(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("secretary.sprint_commands.KanboardClient", return_value=self.client), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main([
                "sprint", "create", "--role", "po", "--data-dir", self.tmp.name,
                "--instance", str(self.instance), "--goal", "CLI sprint",
                "--repository", "secretary", "--product", "secretary", "--issue", "issue:open",
                "--project", "secretary", "--request-id", "cli-create",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        result = json.loads(output.getvalue())
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["sprint"]["repositories"], ["secretary"])
        self.assertEqual(result["sprint"]["product"], "secretary")
        self.assertEqual(result["sprint"]["issues"], ["issue:open"])
        self.assertEqual(result["sprint"]["reservations"], ["secretary"])

    def test_cli_observer_can_set_current_task(self) -> None:
        ref = self._create(goal="observer current task")["sprint"]["ref"]
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(
            role="po", actor="operator", project="secretary", task_type="code", title="linked", target="ready",
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
        ref = self._create(goal="resume") ["sprint"]["ref"]
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
            role="po", actor="operator", project="secretary", task_type="code", title="linked", target="ready",
            sprint=ref, request_id="resume-card",
        )["task"]
        task_writer.comment(role="po", actor="operator", reference=task["ref"], body="meaningful", request_id="later")
        stale = SprintReader(self.client, data_dir=self.tmp.name).show(ref)  # type: ignore[arg-type]
        self.assertFalse(stale["resume_freshness"]["fresh"])
        self.assertEqual(stale["resume_freshness"]["error"], "resume_stale")

    def test_naive_resume_timestamp_is_rejected_and_legacy_data_fails_closed(self) -> None:
        ref = self._create(goal="naive resume") ["sprint"]["ref"]
        entry = {
            "selected_step": "implement", "selected_why": "needed", "rejected_alternatives": "wait",
            "current_task": "next card", "dod_state": "tests pending", "next_safe_step": "run tests",
            "recorded_at": "2026-07-29T12:00:00",
        }
        with self.assertRaisesRegex(TaskError, "must include a timezone"):
            self.writer.resume(role="observer", actor="observer", reference=ref, entry=entry)

        sprint = next(item for item in self.client.tasks if item["reference"] == ref)
        self.client.metadata[int(sprint["id"])] ["sprint_resume"] = json.dumps(entry)
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(
            role="po", actor="operator", project="secretary", task_type="code", title="linked", target="ready",
            sprint=ref, request_id="naive-card",
        )["task"]
        TaskWriter(self.client, data_dir=self.tmp.name).comment(
            role="po", actor="operator", reference=task["ref"], body="meaningful", request_id="naive-event",
        )

        shown = SprintReader(self.client, data_dir=self.tmp.name).show(ref)  # type: ignore[arg-type]

        self.assertFalse(shown["resume_freshness"]["fresh"])
        self.assertEqual(shown["resume_freshness"]["error"], "resume_stale")
        self.assertIsNone(shown["resume_freshness"]["lag_seconds"])

    def test_observer_can_record_a_complete_resume_entry(self) -> None:
        ref = self._create(goal="observer resume")["sprint"]["ref"]
        entry = {
            "selected_step": "implement", "selected_why": "needed", "rejected_alternatives": "wait",
            "current_task": "secretary-14", "dod_state": "tests pending", "next_safe_step": "run tests",
        }

        result = self.writer.resume(
            role="observer", actor="observer-head", reference=ref, entry=entry,
            request_id="observer-resume", delivery_id="delivery-1", through_event="evt-card-1",
        )

        self.assertEqual(result["action"], "resume_recorded")
        self.assertEqual(result["sprint"]["resume"]["selected_step"], "implement")
        self.assertNotIn("delivery_id", result["sprint"]["resume"])
        resume_event = TaskAudit(self.tmp.name).events(reference=ref)[-1]
        self.assertEqual(resume_event["payload"]["delivery_id"], "delivery-1")
        self.assertEqual(resume_event["payload"]["through_event"], "evt-card-1")
        with self.assertRaisesRegex(TaskError, "requires both"):
            self.writer.resume(
                role="observer", actor="observer-head", reference=ref, entry=entry,
                delivery_id="delivery-2",
            )
        with self.assertRaisesRegex(TaskError, "only an observer"):
            self.writer.resume(
                role="po", actor="operator", reference=ref, entry=entry,
                delivery_id="delivery-2", through_event="evt-card-2",
            )

    def test_resume_freshness_ignores_denied_and_failed_card_events(self) -> None:
        ref = self._create(goal="event predicate")["sprint"]["ref"]
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="po", actor="operator", project="secretary", task_type="code", title="linked", target="ready",
            sprint=ref, request_id="predicate-card",
        )["task"]
        entry = {
            "selected_step": "wait", "selected_why": "no durable transition", "rejected_alternatives": "act",
            "current_task": task["ref"], "dod_state": "open", "next_safe_step": "wait",
        }
        self.writer.resume(role="observer", actor="observer", reference=ref, entry=entry)
        audit = TaskAudit(self.tmp.name)
        baseline = SprintReader(self.client, data_dir=self.tmp.name).show(ref)["resume_freshness"]  # type: ignore[arg-type]
        for request_id, kind, outcome in (
            ("predicate-denied", "sprint_guard_denied", "denied"),
            ("predicate-guard-success", "sprint_guard_denied", "success"),
            ("predicate-failed", "commented", "failed"),
            ("predicate-missing-outcome", "commented", ""),
        ):
            audit.append(request_id, {
                "event_id": "evt_" + request_id,
                "request_id": request_id,
                "ref": task["ref"],
                "kind": kind,
                "outcome": outcome,
                "occurred_at": "2099-01-01T00:00:00Z",
            })

        freshness = SprintReader(self.client, data_dir=self.tmp.name).show(ref)["resume_freshness"]  # type: ignore[arg-type]

        self.assertTrue(freshness["fresh"])
        self.assertEqual(freshness["last_event_at"], baseline["last_event_at"])


class SprintSingleWriterGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SprintKanboard()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sprints = SprintWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        # The guard is about card writes against an open sprint, not about opening one,
        # and this board still has the legacy Ideas layout that ownership cannot read.
        self.ref = self.sprints.restore_create(
            reference="sprint:guard", goal="single writer", repositories=["secretary", "other"],
            request_id="seed-guard-sprint",
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
        """A create that could not commit its audit is finished by its own request id."""
        create = dict(
            goal="recovered", repositories=["recovered"], reference="sprint:recovered",
            request_id="recover-sprint-index",
        )
        with mock.patch.object(self.sprints.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "pending repair") as pending:
                self.sprints.restore_create(**create)
        self.assertEqual(pending.exception.code, "audit_pending")

        repaired = self.sprints.restore_create(**create)

        self.assertEqual(repaired["sprint"]["ref"], "sprint:recovered")
        self.assertEqual(
            len([task for task in self.client.tasks if task["reference"] == "sprint:recovered"]), 1
        )
        self.assertEqual(
            len([
                event for event in TaskAudit(self.tmp.name).events()
                if event["request_id"] == "recover-sprint-index"
            ]),
            1,
        )
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
        # Two open sprints are no longer reachable through `create`; recovery can still
        # rebuild a backend that holds them, and the guard has to keep working there.
        other_ref = self.sprints.restore_create(
            reference="sprint:overlap", goal="overlap", repositories=["secretary"],
            request_id="seed-overlap-sprint",
        )["sprint"]["ref"]
        card = self.tasks.create(
            role="observer", actor="second-observer", project="secretary", task_type="code",
            title="second sprint", sprint=other_ref,
        )["task"]

        self.assertEqual(card["sprint"], other_ref)


if __name__ == "__main__":
    unittest.main()
