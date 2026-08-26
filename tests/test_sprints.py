from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from secretary import sprints
from secretary.board import (
    Actor,
    BoardEventPending,
    EntityKind,
    RelatedRefs,
    SprintState,
    TransitionRequest,
)
from secretary.cli import main
from secretary.config import load_config
from secretary.data import normalize_sprint_entity
from secretary.product_issues import ProductIssueStore
from secretary.sprint_close import parse_close_decisions
from secretary.sprint_observer import (
    OBSERVER_FIELD,
    encode_observer,
    head_choice,
    none_choice,
)
from secretary.sprints import (
    BUDGET_EVENT_TYPES,
    BUDGET_UNCHARGED_EVENT_TYPES,
    BUDGET_UNCHARGED_INFRASTRUCTURE,
    SprintReader,
    SprintWriter,
    _close_archive_request_id,
    _close_step_request_id,
    active_sprint_projects,
    budget_thresholds,
    ensure_sprint_board,
    open_sprint_admission_error,
    open_sprint_limit,
    open_sprint_limit_invalid,
    refresh_active_sprint_projects,
    sprint_admission_lock,
)
from secretary.tasks import TaskAudit, TaskError, TaskReader, TaskWriter
from tests.fakes.board import BatchedCalls
from tests.head_registry import write_installed_pair
from tests.observer_identity import as_observer, bind_observer, unbound_observer
from tests.sprint_close_fixtures import DROP_REASON, KEEP_OPEN_REASON, close_decisions

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
HEAD_SNAPSHOT = "\n".join(
    [
        "resources:",
        "  openai-sub:",
        "    account: openai-subscription",
        "  claude-sub:",
        "    account: claude-subscription",
        "profiles:",
        "  codex-observer:",
        "    adapter: codex",
        "    resource: openai-sub",
        "  claude-observer:",
        "    adapter: claude",
        "    resource: claude-sub",
        "role_defaults:",
        "  new_card: codex-observer",
        "  reviewer: codex-observer",
        "  observer: codex-observer",
        "",
    ]
)


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


class SprintOwnershipTests(SprintFixture):
    """A sprint belongs to a Product, serves its open Issues and reserves projects."""

    def _assert_nothing_was_written(self) -> None:
        self.assertEqual(self._events(), [])
        self.assertFalse(
            any(
                method in {"createTask", "saveTaskMetadata", "createProject"}
                for method, _params in self.client.calls
            )
        )

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
                goal="second",
                reference="sprint:second",
                projects=["secretary-instance"],
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
        self.writer.close(role="po", actor="operator", reference=first, decisions=KEEP_THE_ISSUE_OPEN)

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
        waiting_at_gate = threading.Event()
        arrivals_lock = threading.Lock()
        arrivals = 0
        real_admission_lock = sprint_admission_lock

        @contextlib.contextmanager
        def observed_admission_lock(data_dir: str | Path):
            nonlocal arrivals
            with arrivals_lock:
                arrivals += 1
                if arrivals == 2:
                    waiting_at_gate.set()
            with real_admission_lock(data_dir):
                yield

        def deliver(name: str) -> None:
            writer = SprintWriter(  # type: ignore[arg-type]
                self.client,
                data_dir=self.tmp.name,
                instance=self.instance,
            )
            started.wait(timeout=5)
            try:
                outcomes[name] = writer.create(
                    role="po",
                    actor="operator",
                    goal="one delivery",
                    reference="sprint:once",
                    product="secretary",
                    issues=["issue:open"],
                    projects=["secretary"],
                    observer=head_choice("codex-observer"),
                    request_id="same-delivery",
                )
            except TaskError as exc:
                outcomes[name] = exc

        threads = [threading.Thread(target=deliver, args=(name,)) for name in ("first", "second")]
        with (
            sprint_admission_lock(self.tmp.name),
            mock.patch.object(sprints, "sprint_admission_lock", observed_admission_lock),
        ):
            for thread in threads:
                thread.start()
            # Both are inside `create` and waiting for the gate before it is released.
            started.wait(timeout=5)
            self.assertTrue(waiting_at_gate.wait(timeout=5), "both creates reached the admission gate")
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        self.assertEqual([type(value) for value in outcomes.values()], [dict, dict], outcomes)
        self.assertEqual(len({result["event_id"] for result in outcomes.values()}), 1, outcomes)
        self.assertEqual([event["kind"] for event in self._events()], ["created"])
        self.assertEqual([sprint["ref"] for sprint in SprintReader(self.client).list()], ["sprint:once"])  # type: ignore[arg-type]

    def _refuse_once(self, refused_method: str, field: str = ""):
        """Answer the first call of that method carrying `field` the way Kanboard refuses."""
        original = self.client.call
        refused: list[str] = []

        def refuse(method: str, **params: object) -> object:
            values = dict(params.get("values") or {}) if method == "saveTaskMetadata" else {}  # type: ignore[arg-type]
            if method == refused_method and (not field or field in values) and not refused:
                refused.append(method)
                return False
            return original(method, **params)

        return mock.patch.object(self.client, "call", side_effect=refuse)

    def _refuse_metadata(self, field: str):
        return self._refuse_once("saveTaskMetadata", field)

    def _reject_removal(self):
        """Answer every `removeTask` the way a backend that keeps the row does."""
        original = self.client.call

        def refuse(method: str, **params: object) -> object:
            if method == "removeTask":
                return False
            return original(method, **params)

        return mock.patch.object(self.client, "call", side_effect=refuse)

    def _stall_create(self, request_id: str, **kwargs) -> None:
        """Leave one admitted create staged, repairable by its own request id."""
        with self._refuse_metadata("sprint_goal"):
            with self.assertRaisesRegex(TaskError, "pending repair") as pending:
                self._create(goal="rejected metadata", request_id=request_id, **kwargs)
        self.assertEqual(pending.exception.code, "audit_pending")
        self.assertEqual(self._events(), [])

    def test_a_refused_metadata_write_leaves_no_row_and_stays_repairable(self) -> None:
        """Kanboard may refuse the metadata write that carries the whole ownership.

        Reporting `created` on it would leave an open sprint with no product, issues or
        reservations. The create takes its own row back instead, so the board keeps no
        unreferenced row, and the repeat with the same request id finishes that very
        operation.
        """
        self._stall_create("metadata-once")

        self.assertEqual(self._sprint_rows(), [])
        self.assertEqual(SprintReader(self.client).list(create=False), [])  # type: ignore[arg-type]

        repaired = self._create(goal="rejected metadata", request_id="metadata-once")

        self.assertEqual(repaired["action"], "created")
        self.assertEqual(repaired["sprint"]["status"], "open")
        self.assertEqual(repaired["sprint"]["product"], "secretary")
        self.assertEqual(repaired["sprint"]["issues"], ["issue:open"])
        self.assertEqual(repaired["sprint"]["reservations"], ["secretary"])
        self.assertEqual([event["kind"] for event in self._events()], ["created"])
        self.assertEqual(len(self._sprint_rows()), 1)

    def test_a_refused_reference_write_leaves_no_row_without_a_reference(self) -> None:
        """The reference is what publishes the sprint, and Kanboard may refuse it.

        A row that never got one is on no reader's board, so leaving it behind would be
        litter the repair of this same request would have to find again.
        """
        with self._refuse_once("updateTask"):
            with self.assertRaisesRegex(TaskError, "pending repair") as pending:
                self._create(goal="refused reference", reference="sprint:first", request_id="reference-once")

        self.assertEqual(pending.exception.code, "audit_pending")
        self.assertEqual(self._sprint_rows(), [])
        self.assertEqual(self._events(), [])

        repaired = self._create(
            goal="refused reference", reference="sprint:first", request_id="reference-once"
        )

        self.assertEqual(repaired["sprint"]["ref"], "sprint:first")
        self.assertEqual(len(self._sprint_rows()), 1)
        self.assertEqual([event["kind"] for event in self._events()], ["created"])

    def test_a_staged_create_is_resumed_before_any_live_check(self) -> None:
        """Ownership of the request id is settled before product and issue state.

        Between a transient refusal and the repeat that at-least-once delivery sends,
        the Product may legitimately move on. The repeat has to finish the operation it
        was admitted for, not fail on a check the original request already passed.
        """
        self._stall_create("resumed-after-change")
        issue = next(task for task in self.client.tasks if task["reference"] == "issue:open")
        issue["is_active"] = 0
        self.client.metadata[issue["id"]]["issue_closed_reason"] = "resolved"

        repaired = self._create(goal="rejected metadata", request_id="resumed-after-change")

        self.assertEqual(repaired["action"], "created")
        self.assertEqual(repaired["sprint"]["issues"], ["issue:open"])
        self.assertEqual([event["kind"] for event in self._events()], ["created"])

    def test_a_staged_create_that_lost_the_slot_publishes_nothing(self) -> None:
        """A staged create holds nothing, so another sprint may take the installation.

        The repeat is measured against that before it publishes: it is refused naming
        the sprint that won, and no second open sprint appears on the board.
        """
        self._stall_create("loser", reference="sprint:loser")
        winner = self._create(
            goal="winner",
            reference="sprint:winner",
            projects=["secretary-instance"],
        )["sprint"]["ref"]
        events = [event["event_id"] for event in self._events()]
        pending = [event["event_id"] for event in TaskAudit(self.tmp.name).pending_events()]

        with self.assertRaisesRegex(TaskError, winner) as raised:
            self._create(
                goal="rejected metadata",
                reference="sprint:loser",
                request_id="loser",
            )

        self.assertEqual(raised.exception.code, "sprint_conflict")
        self.assertEqual([sprint["ref"] for sprint in SprintReader(self.client).list()], [winner])  # type: ignore[arg-type]
        self.assertEqual(len(self._sprint_rows()), 1)
        self.assertEqual([event["kind"] for event in self._events()], ["created"])
        # The refusal is this request's answer, so its staged intent goes with it: nothing
        # is left for a repair that would only be refused again.
        self.assertEqual(self._transactions(), [])
        self.assertEqual([event["event_id"] for event in self._events()], events)
        self.assertEqual(
            [event["event_id"] for event in TaskAudit(self.tmp.name).pending_events()],
            pending,
        )

    def test_a_staged_create_never_takes_over_a_sprint_sharing_its_reference(self) -> None:
        """Compensation frees the reference too, so another request may take it.

        The repeat of the stalled create is not the owner of that sprint: it must be
        refused naming the reference, and leave the winner's goal, reservations and
        audit provenance untouched.
        """
        self._stall_create("shared-loser", reference="sprint:shared")
        self.assertEqual(self._sprint_rows(), [])

        winner = self._create(
            goal="second payload",
            reference="sprint:shared",
            projects=["secretary-instance"],
            request_id="shared-winner",
        )["sprint"]

        with self.assertRaisesRegex(TaskError, "sprint:shared") as raised:
            self._create(
                goal="rejected metadata",
                reference="sprint:shared",
                request_id="shared-loser",
            )

        self.assertEqual(raised.exception.code, "sprint_conflict")
        live = SprintReader(self.client).show("sprint:shared")  # type: ignore[arg-type]
        self.assertEqual(live["goal"], "second payload")
        self.assertEqual(live["reservations"], ["secretary-instance"])
        self.assertEqual(len(self._sprint_rows()), 1)
        self.assertEqual(
            [(event["kind"], event["request_id"], event["ref"]) for event in self._events()],
            [("created", "shared-winner", "sprint:shared")],
        )
        self.assertEqual(winner["ref"], "sprint:shared")

    def test_an_automatic_reference_clears_every_number_the_board_handed_out(self) -> None:
        """The counter used to be the row's own id, which forgets what it already gave away.

        Live on 2026-08-06 that handed a new sprint `sprint:804`, taken by a sprint closed in July,
        and `show` then resolved the new reference to the old row.
        """
        board = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        for task_id, reference, closed in ((80, "sprint:804", True), (81, "sprint:1153", False)):
            self.client.tasks.append(
                {
                    "id": task_id,
                    "project_id": board,
                    "reference": reference,
                    "title": "historical",
                    "description": "",
                    "column_id": board * 10,
                    "position": task_id,
                    "swimlane_id": 0,
                    "is_active": 0 if closed else 1,
                    "date_creation": "1720000000",
                    "date_modification": "1720000000",
                }
            )
            self.client.metadata[task_id] = {"sprint_status": "closed"}
            self.client.comments[task_id] = []

        created = self._create(goal="numbered above every reference")["sprint"]

        self.assertEqual(created["ref"], "sprint:1154")
        self.assertEqual(
            SprintReader(self.client).show("sprint:1154")["goal"],  # type: ignore[arg-type]
            "numbered above every reference",
        )

    def test_an_automatic_reference_that_is_claimed_is_refused_not_adopted(self) -> None:
        """Allocation is only as good as the enumeration it counted, so the claim decides.

        An enumeration that missed a row is simulated here by allocating a reference the board
        already holds: the create must refuse it loudly instead of publishing a second sprint under
        someone else's reference.
        """
        taken = self._create(goal="the sprint that holds it", reference="sprint:900")["sprint"]
        # Closing it frees the installation to open another sprint; the reference stays taken.
        self.writer.close(
            role="po",
            actor="operator",
            reference=taken["ref"],
            decisions=KEEP_THE_ISSUE_OPEN,
        )

        with mock.patch.object(sprints, "next_reference", return_value="sprint:900"):
            with self.assertRaisesRegex(TaskError, "sprint:900") as raised:
                self._create(goal="collides", request_id="collides")

        self.assertEqual(raised.exception.code, "sprint_conflict")
        self.assertEqual(SprintReader(self.client).show("sprint:900")["goal"], taken["goal"])  # type: ignore[arg-type]
        self.assertEqual(
            [event["ref"] for event in self._events() if event["kind"] == "created"],
            ["sprint:900"],
        )

    def test_a_restored_sprint_without_a_reference_is_refused_rather_than_given_a_row(self) -> None:
        """A restore adopts the row it finds under its reference, so it has to name one.

        It is the one create that may take over a row it did not write, because that row is the one
        it exported and is putting back. An invented reference there would let it adopt a row it has
        never seen, which is the silent adoption every rule in this area exists to prevent.
        """
        held = self._create(
            goal="already on the board",
            reference="sprint:900",
            request_id="held",
        )["sprint"]
        rows_before = [dict(row) for row in self._sprint_rows()]

        with self.assertRaisesRegex(TaskError, "must name its own reference") as raised:
            self.writer.restore_create(
                reference="",
                goal="restored without a reference",
                request_id="restore-no-reference",
            )

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self._sprint_rows(), rows_before)
        self.assertEqual(SprintReader(self.client).show("sprint:900")["goal"], held["goal"])  # type: ignore[arg-type]
        self.assertEqual([event["request_id"] for event in self._events()], ["held"])
        self.assertEqual(self._transactions(), [])

    def test_a_refused_create_whose_row_survives_is_answered_as_repairable(self) -> None:
        """A refusal is only an answer when the request is left holding nothing.

        Here the backend keeps the row of a stalled create, so the repeat that loses the
        slot cannot be told `sprint_conflict`: that would call the request over while its
        row and its staged intent are both still there. It is repairable under the same
        request id until the removal goes through.
        """
        with self._reject_removal(), self._refuse_metadata("sprint_goal"):
            with self.assertRaisesRegex(TaskError, "pending repair"):
                self._create(
                    goal="kept row",
                    reference="sprint:kept",
                    request_id="kept",
                )
        self.assertEqual(len(self._sprint_rows()), 1)
        staged = self._transactions()
        self.assertEqual(len(staged), 1)

        winner = self._create(
            goal="winner",
            reference="sprint:winner",
            projects=["secretary-instance"],
        )["sprint"]["ref"]

        with self._reject_removal(), self.assertRaisesRegex(TaskError, "pending repair") as pending:
            self._create(goal="kept row", reference="sprint:kept", request_id="kept")

        self.assertEqual(pending.exception.code, "audit_pending")
        # The refusal was not answered, so the repair the caller is told to retry is still
        # there, with the row it has to take back.
        self.assertEqual(self._transactions(), staged)
        self.assertEqual(len(self._sprint_rows()), 2)
        self.assertEqual([sprint["ref"] for sprint in SprintReader(self.client).list()], [winner])  # type: ignore[arg-type]
        self.assertEqual([event["kind"] for event in self._events()], ["created"])

        with self.assertRaisesRegex(TaskError, winner) as raised:
            self._create(goal="kept row", reference="sprint:kept", request_id="kept")

        self.assertEqual(raised.exception.code, "sprint_conflict")
        self.assertEqual(self._transactions(), [])
        self.assertEqual(len(self._sprint_rows()), 1)
        self.assertEqual([sprint["ref"] for sprint in SprintReader(self.client).list()], [winner])  # type: ignore[arg-type]
        self.assertEqual([event["kind"] for event in self._events()], ["created"])

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
                self.client,
                data_dir=self.tmp.name,
                instance=self.instance,
            )
            start.wait(timeout=5)
            try:
                outcomes[name] = writer.create(
                    role="po",
                    actor="operator",
                    goal=name,
                    reference=f"sprint:{name}",
                    product="secretary",
                    issues=["issue:open"],
                    projects=[project],
                    observer=head_choice("codex-observer"),
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

    def test_both_transitions_into_open_wait_for_the_admission_gate(self) -> None:
        """Both ways into `open` take the gate, so neither can slip past a holder."""
        ref = self._create(goal="gated", reference="sprint:gated")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)

        for name, call in (
            (
                "reopen",
                lambda: self.writer.reopen(
                    observer=head_choice("codex-observer"), role="po", actor="operator", reference=ref
                ),
            ),
            ("create", lambda: self._create(goal="second", reference="sprint:second")),
        ):
            done = threading.Event()
            worker = threading.Thread(target=lambda: (call(), done.set()))
            with sprint_admission_lock(self.tmp.name):
                worker.start()
                self.assertFalse(done.wait(timeout=0.3), name)
            worker.join(timeout=10)
            self.assertTrue(done.is_set(), name)
            self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)

    def test_a_sprint_without_ownership_gains_none_in_show_status_or_export(self) -> None:
        """The 13 sprints closed before ownership existed keep the fields they had."""
        board = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        self.writer.restore_create(reference="sprint:legacy", goal="legacy", request_id="legacy")
        row = next(task for task in self.client.tasks if task["reference"] == "sprint:legacy")
        self.assertEqual(
            [
                key
                for key in self.client.metadata[row["id"]]
                if key in {"sprint_product", "sprint_issues", "sprint_reservations"}
            ],
            [],
        )
        reader = SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

        views = [
            reader.show("sprint:legacy"),
            reader.status("sprint:legacy"),
            reader.export()[0],
            reader.list()[0],
            normalize_sprint_entity(reader.export()[0]),
        ]

        self.assertEqual(board, row["project_id"])
        for view in views:
            for field in ("product", "issues", "reservations"):
                self.assertNotIn(field, view)

    def test_recovery_reproduces_the_roots_a_closed_row_already_carries(self) -> None:
        """Canonicalization is a rule for declaring a sprint, not for reproducing one.

        The rows closed before it carry the spellings their creates wrote, and recovery
        has to bring them back unchanged; rewriting them here would make the restored
        entity differ from its own export.
        """
        legacy = self.writer.restore_create(
            reference="sprint:legacy",
            goal="legacy",
            repositories=["secretary", "."],
            request_id="legacy-create",
        )["sprint"]
        reader = SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

        self.assertEqual(legacy["repositories"], ["secretary", "."])
        for view in (reader.show(legacy["ref"]), reader.list()[0], reader.export()[0]):
            self.assertEqual(view["repositories"], ["secretary", "."])
        self.assertEqual(
            self.writer.close(
                role="po",
                actor="operator",
                reference=legacy["ref"],
            )["sprint"]["repositories"],
            ["secretary", "."],
        )

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
            reference="sprint:legacy",
            goal="legacy",
            request_id="legacy-create",
        )["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=legacy)

        with self.assertRaisesRegex(TaskError, "predates sprint ownership") as raised:
            self.writer.reopen(
                observer=head_choice("codex-observer"), role="po", actor="operator", reference=legacy
            )

        self.assertEqual(raised.exception.code, "validation")
        reread = SprintReader(self.client).show(legacy, include_cards=False)  # type: ignore[arg-type]
        self.assertEqual(reread["status"], "closed")
        for field in ("product", "issues", "reservations"):
            self.assertNotIn(field, reread)

    def test_reopen_rechecks_ownership_and_stays_idempotent(self) -> None:
        ref = self._create(goal="reopened", reference="sprint:reopened")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)

        first = self.writer.reopen(
            observer=head_choice("codex-observer"),
            role="po",
            actor="operator",
            reference=ref,
            request_id="reopen-once",
        )
        second = self.writer.reopen(
            observer=head_choice("codex-observer"),
            role="po",
            actor="operator",
            reference=ref,
            request_id="reopen-once",
        )

        self.assertEqual(first["sprint"]["status"], "open")
        self.assertEqual(first["event_id"], second["event_id"])

    def test_reopen_refuses_a_request_id_that_belongs_to_another_operation(self) -> None:
        """The transition into `open` owns its request id like every other one.

        A caller that passes the id of its own `close` back to `reopen` is delivering a
        second operation under one id. It has to be refused before a side effect instead
        of being replayed into a `reopened` answer the board never made.
        """
        ref = self._create(goal="reused id", reference="sprint:reused")["sprint"]["ref"]
        self.writer.close(
            role="po", actor="operator", reference=ref, request_id="reused-id", decisions=KEEP_THE_ISSUE_OPEN
        )
        self.client.calls.clear()

        with self.assertRaisesRegex(TaskError, "request id belongs to another operation") as raised:
            self.writer.reopen(
                observer=head_choice("codex-observer"),
                role="po",
                actor="operator",
                reference=ref,
                request_id="reused-id",
            )

        self.assertEqual(raised.exception.code, "validation")
        self.assertFalse(any(method == "saveTaskMetadata" for method, _params in self.client.calls))
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        self.assertEqual([event["kind"] for event in self._events()], ["created", "sprint.closed", "closed"])

    def test_a_refused_reopen_stays_repairable_and_reports_no_transition(self) -> None:
        ref = self._create(goal="refused reopen", reference="sprint:refused")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)

        with self._refuse_metadata("sprint_status"):
            with self.assertRaisesRegex(TaskError, "pending repair") as pending:
                self.writer.reopen(
                    observer=head_choice("codex-observer"),
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="reopen-repair",
                )

        self.assertEqual(pending.exception.code, "audit_pending")
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        self.assertEqual([event["kind"] for event in self._events()], ["created", "sprint.closed", "closed"])

        repaired = self.writer.reopen(
            observer=head_choice("codex-observer"),
            role="po",
            actor="operator",
            reference=ref,
            request_id="reopen-repair",
        )
        replay = self.writer.reopen(
            observer=head_choice("codex-observer"),
            role="po",
            actor="operator",
            reference=ref,
            request_id="reopen-repair",
        )

        self.assertEqual(repaired["action"], "reopened")
        self.assertEqual(repaired["sprint"]["status"], "open")
        self.assertEqual(repaired["event_id"], replay["event_id"])
        self.assertEqual(
            [event["kind"] for event in self._events()],
            ["created", "sprint.closed", "closed", "sprint.reopened", "reopened"],
        )

    def test_a_staged_reopen_is_resumed_before_any_live_check(self) -> None:
        """`reopen` settles its request id first for the same reason `create` does."""
        ref = self._create(goal="resumed reopen", reference="sprint:resumed")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)
        with self._refuse_metadata("sprint_status"):
            with self.assertRaisesRegex(TaskError, "pending repair"):
                self.writer.reopen(
                    observer=head_choice("codex-observer"),
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="reopen-resumed",
                )
        issue = next(task for task in self.client.tasks if task["reference"] == "issue:open")
        issue["is_active"] = 0
        self.client.metadata[issue["id"]]["issue_closed_reason"] = "resolved"

        repaired = self.writer.reopen(
            observer=head_choice("codex-observer"),
            role="po",
            actor="operator",
            reference=ref,
            request_id="reopen-resumed",
        )

        self.assertEqual(repaired["sprint"]["status"], "open")
        self.assertEqual(
            [event["kind"] for event in self._events()],
            ["created", "sprint.closed", "closed", "sprint.reopened", "reopened"],
        )

    def test_a_staged_reopen_that_lost_the_slot_publishes_nothing(self) -> None:
        ref = self._create(goal="reopen loser", reference="sprint:reopen-loser")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)
        with self._refuse_metadata("sprint_status"):
            with self.assertRaisesRegex(TaskError, "pending repair"):
                self.writer.reopen(
                    observer=head_choice("codex-observer"),
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="reopen-lost",
                )
        winner = self._create(
            goal="winner",
            reference="sprint:winner",
            projects=["secretary-instance"],
        )["sprint"]["ref"]
        # The stalled attempt already wrote its fresh observer; the row it refuses to reopen
        # has to come back carrying the value it carried before that attempt.
        closed = SprintReader(self.client, data_dir=self.tmp.name).show(ref, include_cards=False)  # type: ignore[arg-type]
        events = [event["event_id"] for event in self._events()]
        pending = [event["event_id"] for event in TaskAudit(self.tmp.name).pending_events()]

        with self.assertRaisesRegex(TaskError, winner) as raised:
            self.writer.reopen(
                observer=head_choice("codex-observer"),
                role="po",
                actor="operator",
                reference=ref,
                request_id="reopen-lost",
            )

        self.assertEqual(raised.exception.code, "sprint_conflict")
        self.assertEqual(
            [sprint["ref"] for sprint in SprintReader(self.client).list(statuses={"open"})],
            [winner],  # type: ignore[arg-type]
        )
        reopened = SprintReader(self.client, data_dir=self.tmp.name).show(ref, include_cards=False)  # type: ignore[arg-type]
        self.assertEqual(reopened["observer"], closed["observer"])
        self.assertEqual(reopened["status"], "closed")
        self.assertEqual(self._transactions(), [])
        self.assertEqual([event["event_id"] for event in self._events()], events)
        self.assertEqual(
            [event["event_id"] for event in TaskAudit(self.tmp.name).pending_events()],
            pending,
        )

    def test_a_refused_reopen_puts_back_the_observer_its_attempt_wrote(self) -> None:
        """A reopen that loses the slot leaves the row exactly as it found it.

        Its first attempt got as far as writing the fresh observer, so the refusal of the
        repeat has to undo that: the closed row keeps the value of the run that closed,
        and nothing of the refused request is left staged.
        """
        ref = self._create(goal="reopen rollback", reference="sprint:rollback")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)
        with self._refuse_metadata("sprint_status"):
            with self.assertRaisesRegex(TaskError, "pending repair"):
                self.writer.reopen(
                    observer=none_choice(),
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="reopen-rollback",
                )
        reader = SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.assertEqual(reader.show(ref, include_cards=False)["observer"], none_choice())
        self._create(goal="winner", reference="sprint:winner", projects=["secretary-instance"])
        events = [event["event_id"] for event in self._events()]

        with self.assertRaises(TaskError) as raised:
            self.writer.reopen(
                observer=none_choice(),
                role="po",
                actor="operator",
                reference=ref,
                request_id="reopen-rollback",
            )

        self.assertEqual(raised.exception.code, "sprint_conflict")
        restored = reader.show(ref, include_cards=False)
        self.assertEqual(restored["status"], "closed")
        self.assertEqual(restored["observer"], head_choice("codex-observer"))
        self.assertEqual(self._transactions(), [])
        self.assertEqual([event["event_id"] for event in self._events()], events)
        self.assertEqual([event["event_id"] for event in TaskAudit(self.tmp.name).pending_events()], [])

    def test_a_refused_reopen_that_cannot_put_the_observer_back_stays_repairable(self) -> None:
        """The rollback of a refused reopen is a backend write, and it can be rejected.

        Until it goes through, the row still carries the observer the stalled attempt
        wrote, so the repeat that lost the slot is repairable rather than refused: the
        same request id keeps the rollback that has not happened yet.
        """
        ref = self._create(goal="reopen kept", reference="sprint:kept")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)
        with self._refuse_metadata("sprint_status"):
            with self.assertRaisesRegex(TaskError, "pending repair"):
                self.writer.reopen(
                    observer=none_choice(),
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="reopen-kept",
                )
        reader = SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.assertEqual(reader.show(ref, include_cards=False)["observer"], none_choice())
        self._create(goal="winner", reference="sprint:winner", projects=["secretary-instance"])
        staged = self._transactions()
        self.assertEqual(len(staged), 1)
        events = [event["event_id"] for event in self._events()]

        with self._refuse_metadata(OBSERVER_FIELD):
            with self.assertRaisesRegex(TaskError, "pending repair") as pending:
                self.writer.reopen(
                    observer=none_choice(),
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="reopen-kept",
                )

        self.assertEqual(pending.exception.code, "audit_pending")
        kept = reader.show(ref, include_cards=False)
        self.assertEqual(kept["status"], "closed")
        self.assertEqual(kept["observer"], none_choice())
        self.assertEqual(self._transactions(), staged)
        self.assertEqual([event["event_id"] for event in self._events()], events)

        with self.assertRaises(TaskError) as raised:
            self.writer.reopen(
                observer=none_choice(),
                role="po",
                actor="operator",
                reference=ref,
                request_id="reopen-kept",
            )

        self.assertEqual(raised.exception.code, "sprint_conflict")
        restored = reader.show(ref, include_cards=False)
        self.assertEqual(restored["status"], "closed")
        self.assertEqual(restored["observer"], head_choice("codex-observer"))
        self.assertEqual(self._transactions(), [])
        self.assertEqual([event["event_id"] for event in self._events()], events)
        self.assertEqual([event["event_id"] for event in TaskAudit(self.tmp.name).pending_events()], [])

    def test_reopen_is_refused_when_its_only_issue_has_been_closed(self) -> None:
        ref = self._create(goal="issue closed later", reference="sprint:stale")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)
        issue = next(task for task in self.client.tasks if task["reference"] == "issue:open")
        issue["is_active"] = 0
        self.client.metadata[issue["id"]]["issue_closed_reason"] = "resolved"

        with self.assertRaisesRegex(TaskError, "is closed"):
            self.writer.reopen(
                observer=head_choice("codex-observer"), role="po", actor="operator", reference=ref
            )

    def test_board_layout_without_issues_column_fails_closed(self) -> None:
        self.client.columns[7][0] = {"id": 1, "title": "Backlog"}

        with self.assertRaises(TaskError) as raised:
            self._create(goal="legacy layout")

        self.assertEqual(raised.exception.code, "legacy_layout")
        self._assert_nothing_was_written()


class TwoOpenSprintFixture(SprintFixture):
    """Three pairwise disjoint sprint candidates, of which the setting admits two.

    The fixture gains a third product, issue and project, because the count refusal is
    only reachable once three sprints can be pairwise disjoint on everything else.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client._record(
            25,
            "product:third",
            "Third",
            {
                "record_type": "product",
                "product_id": "third",
                "product_projects": json.dumps(["third"]),
            },
        )
        self.client._record(
            26,
            "issue:third",
            "Third issue",
            {
                "record_type": "issue",
                "issue_product": "third",
                "issue_kind": "feature",
                "issue_priority": "P1",
            },
        )
        (self.instance / "projects" / "third.yaml").write_text("id: third\n", encoding="utf-8")
        self.roots = Path(self.tmp.name) / "repos"

    def _limit(self, value: object) -> None:
        (self.instance / "instance.yaml").write_text(
            f"open_sprint_limit: {value}\n",
            encoding="utf-8",
        )

    def _first(self, **kwargs) -> str:
        kwargs.setdefault("repositories", [str(self.roots / "secretary")])
        return self._create(goal="first", reference="sprint:first", **kwargs)["sprint"]["ref"]

    def _second(self, **kwargs) -> dict:
        """A sprint disjoint from `_first` on product, reservation and repository."""
        for field, value in (
            ("goal", "second"),
            ("reference", "sprint:second"),
            ("product", "other"),
            ("issues", ["issue:foreign"]),
            ("projects", ["other"]),
            ("repositories", [str(self.roots / "other")]),
            ("observer", none_choice()),
        ):
            kwargs.setdefault(field, value)
        return self._create(**kwargs)

    def _third(self, **kwargs) -> dict:
        for field, value in (
            ("goal", "third"),
            ("reference", "sprint:third"),
            ("product", "third"),
            ("issues", ["issue:third"]),
            ("projects", ["third"]),
            ("repositories", [str(self.roots / "third")]),
            ("observer", none_choice()),
        ):
            kwargs.setdefault(field, value)
        return self._create(**kwargs)

    def _open_refs(self) -> list[str]:
        return [
            sprint["ref"]
            for sprint in SprintReader(self.client).list(statuses={"open"}, create=False)  # type: ignore[arg-type]
        ]

    def _assert_refusal_left_nothing(self, call, code: str, message: str) -> None:
        """Prove a refusal is only an answer: no row, no staged intent, no audit event."""
        rows = len(self._sprint_rows())
        transactions = self._transactions()
        events = [event["event_id"] for event in self._events()]
        audit = TaskAudit(self.tmp.name)
        pending = [event["event_id"] for event in audit.pending_events()]

        with self.assertRaisesRegex(TaskError, message) as raised:
            call()

        self.assertEqual(raised.exception.code, code)
        self.assertEqual(len(self._sprint_rows()), rows)
        self.assertEqual(self._transactions(), transactions)
        self.assertEqual([event["event_id"] for event in self._events()], events)
        self.assertEqual([event["event_id"] for event in audit.pending_events()], pending)


class TwoOpenSprintAdmissionTests(TwoOpenSprintFixture):
    """The opt-in limit of two open sprints, and the disjointness that makes it safe."""

    def test_the_setting_reader_never_widens_the_limit(self) -> None:
        self.assertEqual(open_sprint_limit(None), 1)
        self.assertEqual(open_sprint_limit({}), 1)
        self.assertEqual(open_sprint_limit({"open_sprint_limit": 2}), 2)
        for value in (0, 3, -1, 1.5, "", "2", True, False, None, [2], {"limit": 2}):
            with self.subTest(value=value):
                config = {"open_sprint_limit": value}
                self.assertEqual(open_sprint_limit(config), 1)
                self.assertTrue(open_sprint_limit_invalid(config))
        self.assertFalse(open_sprint_limit_invalid({}))
        self.assertFalse(open_sprint_limit_invalid({"open_sprint_limit": 1}))
        self.assertFalse(open_sprint_limit_invalid({"open_sprint_limit": 2}))

    def test_an_absent_or_singleton_setting_keeps_the_installation_a_singleton(self) -> None:
        """The default and an explicit 1 are the behaviour every installation has today."""
        for setting in (None, 1):
            with self.subTest(setting=setting):
                self.setUp()
                if setting is not None:
                    self._limit(setting)
                first = self._first()

                self._assert_refusal_left_nothing(
                    self._second,
                    "sprint_conflict",
                    f"installation already has an open sprint: {first}; close it before opening another",
                )
                self.assertEqual(self._open_refs(), [first])

    def test_an_invalid_setting_fails_closed_and_is_reported(self) -> None:
        """No unreadable value may widen the limit, and doctor has to name it."""
        for setting in ("0", "3", "-1", "1.5", '""', "true", "two"):
            with self.subTest(setting=setting):
                self.setUp()
                self._limit(setting)
                first = self._first()

                self._assert_refusal_left_nothing(
                    self._second,
                    "sprint_conflict",
                    "installation already has an open sprint",
                )
                self.assertEqual(self._open_refs(), [first])
                self.assertTrue(open_sprint_limit_invalid(load_config(self.instance / "instance.yaml")))

    def test_a_disjoint_second_sprint_is_admitted_under_the_pilot_limit(self) -> None:
        self._limit(2)
        first = self._first()

        second = self._second()["sprint"]

        self.assertEqual(second["product"], "other")
        self.assertEqual(sorted(self._open_refs()), sorted([first, second["ref"]]))

    def test_a_second_sprint_of_the_same_product_is_refused(self) -> None:
        self._limit(2)
        first = self._first()

        self._assert_refusal_left_nothing(
            lambda: self._second(product="secretary", issues=["issue:open"]),
            "resource_conflict",
            f"product secretary is already the product of open sprint {first}",
        )
        self.assertEqual(self._open_refs(), [first])

    def test_a_shared_reservation_is_refused_before_the_count(self) -> None:
        """The reservation clash reads the same at either limit, and names the holder."""
        self._limit(2)
        first = self._first()

        self._assert_refusal_left_nothing(
            lambda: self._second(projects=["secretary"]),
            "resource_conflict",
            f"secretary held by {first}",
        )
        self.assertEqual(self._open_refs(), [first])

    def test_an_overlapping_repository_root_names_the_tree_not_the_count(self) -> None:
        """Nesting is overlap, a sibling prefix is not, and symlinks are resolved first."""
        self._limit(2)
        (self.roots / "secretary").mkdir(parents=True)
        link = Path(self.tmp.name) / "linked-secretary"
        link.symlink_to(self.roots / "secretary")
        first = self._first()

        for repositories in (
            [str(self.roots / "secretary")],
            [str(self.roots / "secretary" / "nested")],
            [str(self.roots / "secretary") + "/./nested/.."],
            [str(link)],
        ):
            with self.subTest(repositories=repositories):
                self._assert_refusal_left_nothing(
                    lambda repositories=repositories: self._second(repositories=repositories),
                    "resource_conflict",
                    f"held by open sprint {first}",
                )

        sibling = self._second(repositories=[str(self.roots / "secretary-instance")])["sprint"]

        self.assertEqual(sorted(self._open_refs()), sorted([first, sibling["ref"]]))

    def _stored_repositories(self, reference: str, values: list[str]) -> None:
        """Put values on an open row that no create would write, as a legacy row carries."""
        row = next(task for task in self._sprint_rows() if task["reference"] == reference)
        self.client.call(
            "saveTaskMetadata",
            task_id=row["id"],
            values={"sprint_repositories": json.dumps(values)},
        )

    def test_a_declared_root_is_canonicalized_where_it_is_declared(self) -> None:
        """The reviewer's sequence, which used to admit an overlapping pair.

        A sprint that declared `.` persisted the literal `.`, and the next admission
        resolved it against its own working directory.  Run from a second tree, the
        first sprint's stored root pointed at that second tree, the two read as
        disjoint, and both were admitted although they shared one working tree.
        """
        self._limit(2)
        work_a, work_b = self.roots / "work-a", self.roots / "work-b"
        for path in (work_a, work_b):
            path.mkdir(parents=True)

        with contextlib.chdir(work_a):
            first = self._first(repositories=["."])

        self.assertEqual(
            SprintReader(self.client).show(first)["repositories"],
            [str(work_a)],  # type: ignore[arg-type]
        )

        with contextlib.chdir(work_b):
            self._assert_refusal_left_nothing(
                lambda: self._second(repositories=[str(work_a)]),
                "resource_conflict",
                f"overlaps {work_a}, held by open sprint {first}",
            )
        self.assertEqual(self._open_refs(), [first])

    def test_a_root_this_host_cannot_resolve_is_refused_before_anything_is_written(self) -> None:
        """A declaration nobody can canonicalize is an answer, not a sprint."""
        with mock.patch.object(Path, "resolve", side_effect=OSError("too many levels")):
            self._assert_refusal_left_nothing(
                lambda: self._first(repositories=["/loop"]),
                "validation",
                "repository root '/loop' cannot be resolved on this host",
            )
        self.assertEqual(self._open_refs(), [])

    def test_a_stored_root_that_is_not_absolute_is_refused_rather_than_resolved(self) -> None:
        """Admission never resolves a stored root: it would answer against its own cwd.

        Both sides are judged, because a relative root proves nothing about the tree it
        names whichever of the two sprints happens to carry it.
        """
        self._limit(2)
        first = self._first()
        self._stored_repositories(first, ["."])

        self._assert_refusal_left_nothing(
            self._second,
            "resource_conflict",
            f"open sprint {first} declares repository root '.', which is not an absolute path",
        )

        self._stored_repositories(first, [str(self.roots / "secretary")])
        second = self._second()["sprint"]["ref"]
        self.writer.close(
            role="po",
            actor="operator",
            reference=second,
            decisions=close_decisions(self.writer, second),
        )
        self._stored_repositories(second, ["../elsewhere"])

        self._assert_refusal_left_nothing(
            lambda: self.writer.reopen(
                role="po",
                actor="operator",
                reference=second,
                observer=none_choice(),
            ),
            "resource_conflict",
            "this sprint declares repository root '../elsewhere', which is not an absolute path",
        )
        self.assertEqual(self._open_refs(), [first])

    def test_a_sole_sprint_may_not_be_reopened_under_a_root_nobody_can_place(self) -> None:
        """The candidate's own roots are judged whether or not another sprint is open.

        Nothing else being open is what makes this reachable: with the pairwise scan as
        the only check, a row is excluded from its own comparison, the loop over the
        other open sprints has nothing to run, and a relative root reaches `open`.
        """
        self._limit(2)
        first = self._first()
        self.writer.close(role="po", actor="operator", reference=first, decisions=KEEP_THE_ISSUE_OPEN)
        self._stored_repositories(first, ["."])
        self.assertEqual(self._open_refs(), [])

        self._assert_refusal_left_nothing(
            lambda: self.writer.reopen(
                role="po",
                actor="operator",
                reference=first,
                observer=none_choice(),
            ),
            "resource_conflict",
            "this sprint declares repository root '.', which is not an absolute path",
        )
        self.assertEqual(self._open_refs(), [])
        self.assertEqual(
            SprintReader(self.client).show(first, include_cards=False)["status"],
            "closed",  # type: ignore[arg-type]
        )

    def test_a_disjoint_second_sprint_may_declare_its_own_observer_head(self) -> None:
        """An observer call is bound to its sprint, so both open sprints may run a head.

        The ceiling this replaces refused the second head because nothing scoped an
        observer call to the sprint it was about.  That binding exists now, and the
        declaration is judged on the disjointness rules alone.
        """
        self._limit(2)
        first = self._first()

        second = self._second(observer=head_choice("claude-observer"))["sprint"]

        self.assertEqual(second["observer"], head_choice("claude-observer"))
        self.assertEqual(sorted(self._open_refs()), sorted([first, second["ref"]]))

    def test_a_third_sprint_is_refused_however_disjoint_it_is(self) -> None:
        self._limit(2)
        first = self._first()
        second = self._second()["sprint"]["ref"]

        self._assert_refusal_left_nothing(
            self._third,
            "sprint_conflict",
            "installation already holds its limit of 2 open sprints: " + ", ".join(sorted([first, second])),
        )
        self.assertEqual(sorted(self._open_refs()), sorted([first, second]))

    def test_a_third_sprint_that_collides_is_told_the_resource_not_the_count(self) -> None:
        """At capacity too, the refusal names the holder the caller has to close.

        The count names every open sprint and distinguishes none of them, so a caller
        acting on it can close the wrong one and be refused again.
        """
        self._limit(2)
        first = self._first()
        second = self._second()["sprint"]["ref"]

        for name, candidate, code, message in (
            (
                "reservation",
                lambda: self._third(projects=["other"]),
                "resource_conflict",
                f"other held by {second}",
            ),
            (
                "product",
                lambda: self._third(product="other", issues=["issue:foreign"]),
                "resource_conflict",
                f"product other is already the product of open sprint {second}",
            ),
            (
                "repository",
                lambda: self._third(repositories=[str(self.roots / "other")]),
                "resource_conflict",
                f"overlaps {self.roots / 'other'}, held by open sprint {second}",
            ),
        ):
            with self.subTest(collision=name):
                self._assert_refusal_left_nothing(candidate, code, message)

        # A declared head is not a collision of its own: a disjoint third sprint is told the
        # count, which is the only thing left standing in its way.
        self._assert_refusal_left_nothing(
            lambda: self._third(observer=head_choice("claude-observer")),
            "sprint_conflict",
            "installation already holds its limit of 2 open sprints",
        )
        self.assertEqual(sorted(self._open_refs()), sorted([first, second]))

    def test_a_pair_that_cannot_be_proven_disjoint_is_refused_in_either_order(self) -> None:
        """Both orderings of the pair refuse, whichever row carries the opaque value.

        A one-way comparison would admit the pair whenever the opaque row happened to be
        the one already open, which the repository check got wrong once.
        """
        for attribute, opaque in (
            ("product", {"product": "", "repositories": [str(self.roots / "opaque")]}),
            ("repository", {"product": "opaque", "repositories": ["."]}),
        ):
            for refs in (("sprint:a", "sprint:b"), ("sprint:b", "sprint:a")):
                with self.subTest(attribute=attribute, opaque_ref=refs[0]):
                    rows = [
                        {"ref": refs[0], "reservations": [], **opaque},
                        {
                            "ref": refs[1],
                            "reservations": [],
                            "product": "plain",
                            "repositories": [str(self.roots / "plain")],
                        },
                    ]
                    self.assertIsNotNone(open_sprint_admission_error(rows, limit=2))

    def test_reopen_obeys_the_same_rules_excluding_only_its_own_row(self) -> None:
        self._limit(2)
        first = self._first()
        second = self._second()["sprint"]["ref"]
        self.writer.close(
            role="po",
            actor="operator",
            reference=second,
            decisions=close_decisions(self.writer, second),
        )

        # Its own reservation, product and repository are not collisions of its own row.
        reopened = self.writer.reopen(
            role="po",
            actor="operator",
            reference=second,
            observer=none_choice(),
        )
        self.assertEqual(reopened["sprint"]["status"], "open")

        self.writer.close(
            role="po",
            actor="operator",
            reference=second,
            decisions=close_decisions(self.writer, second),
        )
        # A reopen may declare its own head beside the one the other sprint already runs.
        with_head = self.writer.reopen(
            role="po",
            actor="operator",
            reference=second,
            observer=head_choice("claude-observer"),
        )
        self.assertEqual(with_head["sprint"]["observer"], head_choice("claude-observer"))

        self.writer.close(
            role="po",
            actor="operator",
            reference=second,
            decisions=close_decisions(self.writer, second),
        )
        third = self._third(projects=["other"])["sprint"]["ref"]
        # At its limit too, the reservation it collides on is named ahead of the count.
        self._assert_refusal_left_nothing(
            lambda: self.writer.reopen(
                role="po",
                actor="operator",
                reference=second,
                observer=none_choice(),
            ),
            "resource_conflict",
            f"other held by {third}",
        )
        self.assertEqual(sorted(self._open_refs()), sorted([first, third]))

        # With room again, the reservation the third sprint took is what refuses it.
        self.writer.close(role="po", actor="operator", reference=first, decisions=KEEP_THE_ISSUE_OPEN)
        self._assert_refusal_left_nothing(
            lambda: self.writer.reopen(
                role="po",
                actor="operator",
                reference=second,
                observer=none_choice(),
            ),
            "resource_conflict",
            f"other held by {third}",
        )
        self.assertEqual(self._open_refs(), [third])

    def test_concurrent_creates_admit_at_most_the_limit(self) -> None:
        """Three disjoint creates at once still leave exactly two open sprints."""
        self._limit(2)
        ensure_sprint_board(self.client)  # type: ignore[arg-type]
        start = threading.Barrier(3)
        outcomes: dict[str, Any] = {}
        candidates = {
            "first": ("secretary", "issue:open", "secretary"),
            "second": ("other", "issue:foreign", "other"),
            "third": ("third", "issue:third", "third"),
        }

        def open_sprint(name: str) -> None:
            product, issue, project = candidates[name]
            writer = SprintWriter(  # type: ignore[arg-type]
                self.client,
                data_dir=self.tmp.name,
                instance=self.instance,
            )
            start.wait(timeout=5)
            try:
                outcomes[name] = writer.create(
                    role="po",
                    actor="operator",
                    goal=name,
                    reference=f"sprint:{name}",
                    product=product,
                    issues=[issue],
                    projects=[project],
                    repositories=[str(self.roots / project)],
                    observer=none_choice(),
                )
            except TaskError as exc:
                outcomes[name] = exc

        threads = [threading.Thread(target=open_sprint, args=(name,)) for name in candidates]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        refused = [value for value in outcomes.values() if isinstance(value, TaskError)]
        self.assertEqual(len(refused), 1, outcomes)
        self.assertEqual(refused[0].code, "sprint_conflict")
        self.assertIn("its limit of 2 open sprints", refused[0].message)
        self.assertEqual(len(self._open_refs()), 2, self._open_refs())

    def test_an_open_sprint_without_a_product_cannot_be_proven_disjoint(self) -> None:
        """A restored legacy row is opaque, so it holds the installation on its own."""
        self._limit(2)
        self.writer.restore_create(
            reference="sprint:legacy",
            goal="legacy",
            request_id="legacy",
            status="open",
        )

        self._assert_refusal_left_nothing(
            self._second,
            "resource_conflict",
            "open sprint sprint:legacy declares no product",
        )
        self.assertEqual(self._open_refs(), ["sprint:legacy"])


class TwoOpenSprintIsolationTests(TwoOpenSprintFixture):
    """What the entity itself keeps apart once two sprints are open at the same time.

    The pair is opened through admission under the pilot setting, not written onto the
    board, so every fact below is one a real installation could reach.  The budget, the
    hard stop, the close and the reserved-project index are each read for both sprints
    after a write that names one of them.
    """

    def setUp(self) -> None:
        super().setUp()
        self._limit(2)

    def _pair(self) -> tuple[str, str]:
        first = self._first()
        second = self._second()["sprint"]["ref"]
        self.assertEqual(sorted(self._open_refs()), sorted([first, second]))
        return first, second

    def _writer(self, **thresholds: int) -> SprintWriter:
        return SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds=thresholds or None,
        )

    def _budget_of(self, reference: str, writer: SprintWriter | None = None) -> dict:
        reader = (writer or self.writer).reader
        return reader.show(reference, include_cards=False)["budget"]

    def _status_of(self, reference: str) -> str:
        return self.writer.reader.show(reference, include_cards=False)["status"]

    def _charge(self, writer: SprintWriter, reference: str, event_type: str, request_id: str) -> None:
        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=reference,
            event_type=event_type,
            request_id=request_id,
            source_event_id="evt-" + request_id,
        )

    def test_a_charge_moves_the_counters_of_the_sprint_it_names_only(self) -> None:
        first, second = self._pair()

        self._charge(self.writer, first, "red_ci", "charge-first")

        self.assertEqual(self._budget_of(first)["total"], 1)
        self.assertEqual(self._budget_of(first)["by_type"]["red_ci"], 1)
        self.assertEqual(self._budget_of(second)["total"], 0)
        self.assertEqual(
            self._budget_of(second)["by_type"],
            {event: 0 for event in BUDGET_EVENT_TYPES},
        )
        # The charge is an event of its own sprint, and the other sprint has none.
        self.assertEqual(
            [event["kind"] for event in TaskAudit(self.tmp.name).events(reference=first)],
            ["created", "budget_recorded"],
        )
        self.assertEqual(
            [event["kind"] for event in TaskAudit(self.tmp.name).events(reference=second)],
            ["created"],
        )

    def test_each_sprint_reaches_its_signal_threshold_on_its_own_counters(self) -> None:
        """Two charges to one sprint are not two charges to the installation."""
        writer = self._writer(signal=2, hard=4)
        first, second = self._pair()

        self._charge(writer, first, "red_ci", "signal-first-1")
        self.assertFalse(self._budget_of(first, writer)["signal_reached"])

        self._charge(writer, first, "blocked", "signal-first-2")

        self.assertTrue(self._budget_of(first, writer)["signal_reached"])
        self.assertFalse(self._budget_of(second, writer)["signal_reached"])
        self.assertEqual(self._budget_of(second, writer)["total"], 0)

        # And the second sprint's own signal is reached by its own two charges, no sooner.
        self._charge(writer, second, "red_ci", "signal-second-1")
        self.assertFalse(self._budget_of(second, writer)["signal_reached"])
        self._charge(writer, second, "red_ci", "signal-second-2")
        self.assertTrue(self._budget_of(second, writer)["signal_reached"])
        self.assertEqual(self._status_of(first), "open")
        self.assertEqual(self._status_of(second), "open")

    def test_a_hard_stop_stops_the_sprint_that_reached_it_and_not_the_other(self) -> None:
        writer = self._writer(signal=1, hard=2)
        first, second = self._pair()

        self._charge(writer, first, "blocked", "hard-first-1")
        self._charge(writer, first, "blocked", "hard-first-2")

        self.assertEqual(self._status_of(first), "stopped")
        self.assertEqual(self._status_of(second), "open")
        self.assertEqual(self._budget_of(second, writer)["total"], 0)
        self.assertFalse(self._budget_of(second, writer)["hard_reached"])
        self.assertEqual(
            [
                event["ref"]
                for event in TaskAudit(self.tmp.name).events()
                if event["kind"] == "budget_hard_stopped"
            ],
            [first],
        )

        # The other sprint still charges, and stops on its own second event, not on the first.
        self._charge(writer, second, "red_ci", "hard-second-1")
        self.assertEqual(self._status_of(second), "open")
        self._charge(writer, second, "red_ci", "hard-second-2")

        self.assertEqual(self._status_of(second), "stopped")
        self.assertEqual(
            sorted(
                event["ref"]
                for event in TaskAudit(self.tmp.name).events()
                if event["kind"] == "budget_hard_stopped"
            ),
            sorted([first, second]),
        )

    def test_closing_either_sprint_leaves_the_other_open(self) -> None:
        """Both orders, because closing the older one is not the only close that happens."""
        for closed_first in (True, False):
            with self.subTest(closes="first" if closed_first else "second"):
                self.setUp()
                first, second = self._pair()
                closing, remaining = (first, second) if closed_first else (second, first)

                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=closing,
                    decisions=close_decisions(self.writer, closing),
                )

                self.assertEqual(self._status_of(closing), "closed")
                self.assertEqual(self._status_of(remaining), "open")
                self.assertEqual(self._open_refs(), [remaining])
                # The sprint left open is still a sprint that writes: its budget still moves.
                self._charge(self.writer, remaining, "red_ci", "after-close")
                self.assertEqual(self._budget_of(remaining)["total"], 1)

    def test_closing_one_sprint_releases_its_reservations_and_holds_the_others(self) -> None:
        for closed_first in (True, False):
            with self.subTest(closes="first" if closed_first else "second"):
                self.setUp()
                first, second = self._pair()
                self.assertEqual(
                    active_sprint_projects(self.tmp.name),
                    {"secretary": [first], "other": [second]},
                )
                closing, remaining = (first, second) if closed_first else (second, first)
                released = "secretary" if closed_first else "other"
                held = "other" if closed_first else "secretary"

                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=closing,
                    decisions=close_decisions(self.writer, closing),
                )

                self.assertEqual(active_sprint_projects(self.tmp.name), {held: [remaining]})
                # The released project is free for a new sprint; the held one is still refused.
                self._assert_refusal_left_nothing(
                    lambda: self._third(projects=[held]),
                    "resource_conflict",
                    f"{held} held by {remaining}",
                )
                third = self._third(projects=[released])["sprint"]["ref"]
                self.assertEqual(
                    active_sprint_projects(self.tmp.name),
                    {held: [remaining], released: [third]},
                )

    def test_a_card_of_the_remaining_sprints_project_is_still_guarded_after_the_close(self) -> None:
        """The index is what the card guard reads, so the release is checked through it."""
        first, second = self._pair()
        tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

        self.writer.close(role="po", actor="operator", reference=first, decisions=KEEP_THE_ISSUE_OPEN)

        # `secretary` was released with its sprint, so an unrelated role may write there again.
        created = tasks.create(
            role="retro",
            actor="retro",
            project="secretary",
            task_type="research",
            title="finding",
            target="issues",
            request_id="released-project",
        )
        self.assertEqual(created["task"]["project"], "secretary")

        with self.assertRaisesRegex(TaskError, second) as denied:
            tasks.create(
                role="retro",
                actor="retro",
                project="other",
                task_type="research",
                title="finding",
                target="issues",
                request_id="held-project",
            )
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")

    def _second_sprint_card(self, second: str) -> dict:
        """One Ready card of the second sprint, written by that sprint's own head."""
        with as_observer(second):
            return TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
                role="observer",
                actor="observer",
                project="other",
                task_type="code",
                title="the other sprint's work",
                target="ready",
                sprint=second,
                request_id="second-sprint-card",
            )["task"]

    def _denials(self) -> list[dict]:
        return [event for event in self._events() if event["kind"] == "sprint_guard_denied"]

    def test_an_observer_of_one_sprint_writes_nothing_of_the_other(self) -> None:
        """The identity half of the guard, across two sprints that share nothing.

        Product, reservations and repository roots are disjoint, so nothing but the caller's own
        binding stands between the first sprint's head and the second sprint's work. Card and
        entity are both refused, and each refusal is in the audit as an identity failure rather
        than as a role that is not permitted.
        """
        first, second = self._pair()
        card = self._second_sprint_card(second)
        tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        before = len(self._denials())
        entry = {
            "selected_step": "implement",
            "selected_why": "needed",
            "rejected_alternatives": "wait",
            "current_task": card["ref"],
            "dod_state": "open",
            "next_safe_step": "run tests",
        }

        with as_observer(first):
            calls = (
                (
                    "decide",
                    lambda: tasks.decide(
                        role="observer",
                        actor="observer",
                        reference=card["ref"],
                        kind="release",
                        body="releasing a card of a sprint I do not observe",
                        request_id="cross-sprint-decision",
                    ),
                ),
                (
                    "move",
                    lambda: tasks.move(
                        role="observer",
                        actor="observer",
                        reference=card["ref"],
                        target="blocked",
                        reason="blocking a card of a sprint I do not observe",
                        request_id="cross-sprint-move",
                    ),
                ),
                (
                    "resume",
                    lambda: self.writer.resume(
                        role="observer",
                        actor="observer",
                        reference=second,
                        entry=entry,
                        request_id="cross-sprint-resume",
                    ),
                ),
                # The card this points at is linked to the target sprint, which is what its own
                # head would pass: the link is a constraint on the value, not on the caller.
                (
                    "current-task",
                    lambda: self.writer.set_current_task(
                        role="observer",
                        actor="observer",
                        reference=second,
                        task_reference=card["ref"],
                        request_id="cross-sprint-current-task",
                    ),
                ),
            )
            for name, call in calls:
                with self.subTest(call=name), self.assertRaises(TaskError) as refused:
                    call()
                self.assertEqual(refused.exception.code, "observer_sprint_mismatch")

        denials = self._denials()[before:]
        self.assertEqual(
            [event["payload"]["code"] for event in denials],
            ["observer_sprint_mismatch"] * 4,
        )
        self.assertEqual({event["payload"]["sprint"] for event in denials}, {first})
        self.assertEqual([event["outcome"] for event in denials], ["denied"] * 4)
        self.assertEqual(
            [event["ref"] for event in denials],
            [card["ref"], card["ref"], second, second],
        )
        # Nothing moved: the card is where its own sprint left it, and the entity has neither a
        # resume entry nor a current task somebody else chose.
        self.assertEqual(TaskReader(self.client).show(card["ref"])["state"], "ready")  # type: ignore[arg-type]
        other = self.writer.reader.show(second, include_cards=False)
        self.assertIsNone(other["resume"])
        self.assertIsNone(other["current_task"])

    def test_a_head_nobody_bound_writes_nothing_at_all(self) -> None:
        """Fail-closed: an unbound caller cannot prove which sprint it is, so it is not one."""
        first, second = self._pair()
        card = self._second_sprint_card(second)
        tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        before = len(self._denials())

        with unbound_observer():
            with self.assertRaises(TaskError) as moved:
                tasks.move(
                    role="observer",
                    actor="observer",
                    reference=card["ref"],
                    target="blocked",
                    reason="from a head nobody bound",
                    request_id="unbound-move",
                )
            with self.assertRaises(TaskError) as resumed:
                self.writer.resume(
                    role="observer",
                    actor="observer",
                    reference=first,
                    entry={
                        "selected_step": "implement",
                        "selected_why": "needed",
                        "rejected_alternatives": "wait",
                        "current_task": card["ref"],
                        "dod_state": "open",
                        "next_safe_step": "run tests",
                    },
                    request_id="unbound-resume",
                )
            with self.assertRaises(TaskError) as pointed:
                self.writer.set_current_task(
                    role="observer",
                    actor="observer",
                    reference=second,
                    task_reference=card["ref"],
                    request_id="unbound-current-task",
                )

        self.assertEqual(moved.exception.code, "observer_identity_unbound")
        self.assertEqual(resumed.exception.code, "observer_identity_unbound")
        self.assertEqual(pointed.exception.code, "observer_identity_unbound")
        self.assertEqual(
            [event["payload"]["code"] for event in self._denials()[before:]],
            ["observer_identity_unbound"] * 3,
        )

    def test_a_bound_head_still_writes_its_own_sprint(self) -> None:
        """The other side of the same guard: nothing changes for the sprint's own observer."""
        first, second = self._pair()
        card = self._second_sprint_card(second)

        with as_observer(second):
            blocked = TaskWriter(self.client, data_dir=self.tmp.name).move(  # type: ignore[arg-type]
                role="observer",
                actor="observer",
                reference=card["ref"],
                target="blocked",
                reason="its own head blocking its own card",
                request_id="own-sprint-move",
            )
            recorded = self.writer.resume(
                role="observer",
                actor="observer",
                reference=second,
                entry={
                    "selected_step": "implement",
                    "selected_why": "needed",
                    "rejected_alternatives": "wait",
                    "current_task": card["ref"],
                    "dod_state": "open",
                    "next_safe_step": "run tests",
                },
                request_id="own-sprint-resume",
            )
            pointed = self.writer.set_current_task(
                role="observer",
                actor="observer",
                reference=second,
                task_reference=card["ref"],
                request_id="own-sprint-current-task",
            )

        self.assertEqual(blocked["task"]["state"], "blocked")
        self.assertEqual(recorded["sprint"]["resume"]["selected_step"], "implement")
        self.assertEqual(pointed["sprint"]["current_task"], card["ref"])
        self.assertEqual(self._denials(), [])
        self.assertEqual(first, "sprint:first")


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
            goal="Ship sprint entity",
            definition_of_done="tests pass",
            repositories=["secretary", "secretary"],
            projects=["secretary", "secretary"],
            reference="sprint:entity",
            request_id="create",
        )
        sprint = created["sprint"]
        # A declaration is canonicalized where it is written, so the row persists the
        # absolute root rather than the spelling the caller happened to use.
        self.assertEqual(sprint["repositories"], [str(Path("secretary").resolve())])
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
        self.writer.close(role="po", actor="operator", reference=sprint["ref"], decisions=KEEP_THE_ISSUE_OPEN)
        with self.assertRaisesRegex(TaskError, "already exists") as raised:
            self._create(goal="another", reference="sprint:entity")
        self.assertEqual(raised.exception.code, "validation")

    def test_missing_metadata_reads_as_empty_contract_values(self) -> None:
        sprint_board = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        self.client.tasks.append(
            {
                "id": 13,
                "project_id": sprint_board,
                "reference": "sprint:legacy",
                "title": "legacy",
                "description": "",
                "column_id": sprint_board * 10,
                "position": 1,
                "swimlane_id": 0,
                "date_creation": "1720000000",
                "date_modification": "1720000000",
            }
        )
        self.client.metadata[13] = {}
        self.client.comments[13] = []
        sprint = SprintReader(self.client).show("sprint:legacy")  # type: ignore[arg-type]
        self.assertEqual(sprint["goal"], "")
        self.assertEqual(sprint["definition_of_done"], "")
        self.assertEqual(sprint["repositories"], [])
        self.assertEqual(sprint["budget"]["total"], 0)
        self.assertEqual(sprint["budget"]["by_type"], {event: 0 for event in BUDGET_EVENT_TYPES})
        self.assertIsNone(sprint["current_task"])

    def test_metadata_less_sprint_still_closes_through_the_host(self) -> None:
        sprint_board = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        self.client.tasks.append(
            {
                "id": 13,
                "project_id": sprint_board,
                "reference": "sprint:legacy-close",
                "title": "legacy",
                "description": "",
                "column_id": sprint_board * 10,
                "position": 1,
                "swimlane_id": 0,
                "date_creation": "1720000000",
                "date_modification": "1720000000",
            }
        )
        self.client.metadata[13] = {}
        self.client.comments[13] = []

        closed = self.writer.close(
            role="po",
            actor="operator",
            reference="sprint:legacy-close",
            request_id="legacy-close",
        )

        self.assertEqual(closed["sprint"]["status"], "closed")
        typed = self.writer.audit.committed_event("legacy-close:typed-close")
        assert typed is not None
        self.assertEqual(typed["transition"], {"source": "open", "target": "closed"})

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
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)

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
        first = self.writer.record_budget(
            role="po", actor="operator", reference=ref, event_type="red_ci", request_id="budget-once"
        )
        second = self.writer.record_budget(
            role="po", actor="operator", reference=ref, event_type="red_ci", request_id="budget-once"
        )
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(second["sprint"]["budget"]["total"], 1)
        self.assertEqual(second["sprint"]["budget"]["by_type"]["red_ci"], 1)
        events = TaskAudit(self.tmp.name).events(reference=ref)
        self.assertEqual([event["kind"] for event in events], ["created", "budget_recorded"])

    def _budget_of(self, reference: str) -> dict:
        return self.writer.reader.show(reference, include_cards=False)["budget"]

    def test_infrastructure_outcome_is_counted_apart_and_spends_nothing(self) -> None:
        """An infrastructure bring-up is visible in the sprint and moves no threshold."""
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = self._create(goal="infrastructure outcomes")["sprint"]["ref"]

        for index in range(3):
            writer.record_budget(
                role="dispatcher",
                actor="dispatcher",
                reference=ref,
                event_type=BUDGET_UNCHARGED_INFRASTRUCTURE,
                request_id=f"infra-{index}",
                source_event_id=f"evt-infra-{index}",
            )

        budget = writer.reader.show(ref, include_cards=False)["budget"]
        self.assertEqual(budget["uncharged"], {BUDGET_UNCHARGED_INFRASTRUCTURE: 3})
        self.assertEqual(budget["total"], 0)
        self.assertEqual(budget["by_type"], {event: 0 for event in BUDGET_EVENT_TYPES})
        self.assertFalse(budget["signal_reached"])
        self.assertFalse(budget["hard_reached"])
        # Three of them under a hard limit of one: the sprint is still open, and no hard stop
        # was written.
        self.assertEqual(writer.reader.show(ref, include_cards=False)["status"], "open")
        self.assertEqual(
            [event["kind"] for event in TaskAudit(self.tmp.name).events(reference=ref)],
            ["created"] + ["budget_recorded"] * 3,
        )
        self.assertEqual(
            writer.reader.status(ref)["budget"]["uncharged"],
            {BUDGET_UNCHARGED_INFRASTRUCTURE: 3},
        )

    def test_a_task_class_block_still_charges_beside_an_infrastructure_one(self) -> None:
        ref = self._create(goal="both classes")["sprint"]["ref"]

        self.writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type=BUDGET_UNCHARGED_INFRASTRUCTURE,
            request_id="infra-one",
        )
        for index, event_type in enumerate(BUDGET_EVENT_TYPES):
            self.writer.record_budget(
                role="dispatcher",
                actor="dispatcher",
                reference=ref,
                event_type=event_type,
                request_id=f"charge-{index}",
            )

        budget = self._budget_of(ref)
        self.assertEqual(budget["by_type"], {event: 1 for event in BUDGET_EVENT_TYPES})
        self.assertEqual(budget["total"], len(BUDGET_EVENT_TYPES))
        self.assertEqual(budget["uncharged"], {BUDGET_UNCHARGED_INFRASTRUCTURE: 1})

    def test_a_sprint_stored_without_uncharged_counts_reads_them_as_zero(self) -> None:
        """A sprint written before this quantity existed reads back, and reads back as zero."""
        ref = self._create(goal="stored before")["sprint"]["ref"]
        stored = self.writer.reader.show(ref, include_cards=False)
        task_id = int(str(stored["id"]).removeprefix("sprint_kanboard_"))
        self.client.call(
            "saveTaskMetadata",
            task_id=task_id,
            values={
                "sprint_budget": json.dumps({"by_type": {"blocked": 2, "red_ci": 1}}),
            },
        )
        self.assertNotIn(
            "sprint_budget_uncharged",
            self.client.call("getTaskMetadata", task_id=task_id),
        )

        budget = self._budget_of(ref)
        self.assertEqual(budget["total"], 3)
        self.assertEqual(budget["by_type"]["blocked"], 2)
        self.assertEqual(
            budget["uncharged"],
            {event: 0 for event in BUDGET_UNCHARGED_EVENT_TYPES},
        )

        # And the next infrastructure outcome starts from that zero rather than failing on it.
        self.writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type=BUDGET_UNCHARGED_INFRASTRUCTURE,
            request_id="infra-after-legacy",
        )
        self.assertEqual(self._budget_of(ref)["uncharged"][BUDGET_UNCHARGED_INFRASTRUCTURE], 1)
        self.assertEqual(self._budget_of(ref)["total"], 3)

    def test_a_hard_stop_keeps_the_uncharged_counts_it_did_not_compute(self) -> None:
        """The hard-stop edge rewrites the charged budget; the counts beside it survive it."""
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = self._create(goal="hard stop with infrastructure")["sprint"]["ref"]
        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type=BUDGET_UNCHARGED_INFRASTRUCTURE,
            request_id="infra-before-stop",
        )

        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="hard-stop",
            source_event_id="evt-card-blocked",
        )

        sprint = writer.reader.show(ref, include_cards=False)
        self.assertEqual(sprint["status"], "stopped")
        self.assertEqual(sprint["budget"]["by_type"]["blocked"], 1)
        self.assertEqual(sprint["budget"]["total"], 1)
        self.assertEqual(sprint["budget"]["uncharged"], {BUDGET_UNCHARGED_INFRASTRUCTURE: 1})

    def test_hard_budget_stop_has_its_own_durable_event(self) -> None:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = writer.create(
            role="po",
            actor="operator",
            goal="hard limit",
            product="secretary",
            issues=["issue:open"],
            projects=["secretary"],
            observer=head_choice("codex-observer"),
        )["sprint"]["ref"]

        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="hard-stop",
            source_event_id="evt-card-blocked",
        )
        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="hard-stop",
            source_event_id="evt-card-blocked",
        )

        events = TaskAudit(self.tmp.name).events(reference=ref)
        self.assertEqual(
            [event["kind"] for event in events],
            ["created", "sprint.stopped", "budget_recorded", "budget_hard_stopped"],
        )
        self.assertEqual(events[-1]["payload"]["reason"], "budget_hard_limit")
        self.assertEqual(events[-1]["payload"]["source_event_id"], "evt-card-blocked")

    def test_hard_stop_stages_typed_then_persists_budget_and_state_together(self) -> None:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = writer.create(
            role="po",
            actor="operator",
            goal="atomic hard stop",
            product="secretary",
            issues=["issue:open"],
            projects=["secretary"],
            observer=head_choice("codex-observer"),
        )["sprint"]["ref"]
        self.client.calls.clear()
        staged: list[dict | None] = []
        original = self.client.call

        def call(method: str, **params: object) -> object:
            if method == "saveTaskMetadata" and "sprint_status" in params.get("values", {}):
                staged.append(writer.audit.pending_event("atomic-hard:typed-hard-stop"))
            return original(method, **params)

        with mock.patch.object(self.client, "call", side_effect=call):
            result = writer.record_budget(
                role="dispatcher",
                actor="dispatcher",
                reference=ref,
                event_type="blocked",
                request_id="atomic-hard",
            )

        self.assertEqual(result["sprint"]["status"], "stopped")
        self.assertEqual(len(staged), 1)
        assert staged[0] is not None
        self.assertEqual(staged[0]["transition"], {"source": "open", "target": "stopped"})
        status_writes = [
            params["values"]
            for method, params in self.client.calls
            if method == "saveTaskMetadata" and "sprint_status" in params["values"]
        ]
        self.assertEqual(len(status_writes), 1)
        self.assertEqual(set(status_writes[0]), {"sprint_budget", "sprint_status"})
        self.assertEqual(
            [event["kind"] for event in self._events()],
            [
                "created",
                "sprint.stopped",
                "budget_recorded",
                "budget_hard_stopped",
            ],
        )

    def test_hard_stop_post_effect_failure_retries_its_typed_owner_before_charge(self) -> None:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = writer.create(
            role="po",
            actor="operator",
            goal="recover hard stop",
            product="secretary",
            issues=["issue:open"],
            projects=["secretary"],
            observer=head_choice("codex-observer"),
        )["sprint"]["ref"]
        original_append = writer.audit.append

        def fail_typed(request_id: str, event: dict) -> str:
            if request_id == "recover-hard:typed-hard-stop":
                raise OSError("disk full")
            return original_append(request_id, event)

        with mock.patch.object(writer.audit, "append", side_effect=fail_typed):
            with self.assertRaisesRegex(TaskError, "pending repair") as raised:
                writer.record_budget(
                    role="dispatcher",
                    actor="dispatcher",
                    reference=ref,
                    event_type="blocked",
                    request_id="recover-hard",
                )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(SprintReader(self.client).show(ref)["status"], "stopped")  # type: ignore[arg-type]
        self.assertIsNotNone(writer.audit.pending_event("recover-hard:typed-hard-stop"))
        self.assertIsNotNone(writer.audit.pending_event("recover-hard"))
        writes = len([method for method, _params in self.client.calls if method == "saveTaskMetadata"])

        result = writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="recover-hard",
        )

        self.assertEqual(result["sprint"]["status"], "stopped")
        self.assertIsNone(writer.audit.pending_event("recover-hard:typed-hard-stop"))
        self.assertIsNone(writer.audit.pending_event("recover-hard"))
        self.assertEqual(
            writes, len([method for method, _params in self.client.calls if method == "saveTaskMetadata"])
        )

    def test_hard_stop_pre_effect_failure_discards_its_generic_charge(self) -> None:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = writer.create(
            role="po",
            actor="operator",
            goal="failed hard stop",
            product="secretary",
            issues=["issue:open"],
            projects=["secretary"],
            observer=head_choice("codex-observer"),
        )["sprint"]["ref"]
        original_read = writer._host().read

        def fail_sprint_read(kind: object, reference: str) -> object:
            if reference == ref:
                raise RuntimeError("Kanboard connection reset")
            return original_read(kind, reference)

        with mock.patch("secretary.board.kanboard.KanboardBoardHost.read", side_effect=fail_sprint_read):
            with self.assertRaisesRegex(TaskError, "Kanboard connection reset") as raised:
                writer.record_budget(
                    role="dispatcher",
                    actor="dispatcher",
                    reference=ref,
                    event_type="blocked",
                    request_id="failed-hard",
                )

        self.assertEqual(raised.exception.code, "validation")
        self.assertIsNone(writer.audit.pending_event("failed-hard"))
        self.assertIsNone(writer.audit.pending_event("failed-hard:typed-hard-stop"))
        self.assertEqual(SprintReader(self.client).show(ref)["status"], "open")  # type: ignore[arg-type]
        writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="close-after-failed-hard",
            decisions=KEEP_THE_ISSUE_OPEN,
        )
        self.assertEqual(TaskWriter(self.client, data_dir=self.tmp.name).reconcile(), (0, 0))  # type: ignore[arg-type]

    def test_hard_stop_replay_keeps_its_stored_related_refs_after_card_archive(self) -> None:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = writer.create(
            role="po",
            actor="operator",
            goal="stable hard replay",
            product="secretary",
            issues=["issue:open"],
            projects=["secretary"],
            observer=head_choice("codex-observer"),
        )["sprint"]["ref"]
        bind_observer(self, ref)
        card = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
            request_id="replay-linked-card",
        )["task"]
        first = writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="stable-hard",
        )
        TaskWriter(self.client, data_dir=self.tmp.name).archive(  # type: ignore[arg-type]
            role="po",
            actor="operator",
            reference=card["ref"],
            reason="ordinary archive",
            request_id="archive-linked-card",
        )

        replay = writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="stable-hard",
        )

        self.assertEqual(replay["event_id"], first["event_id"])
        self.assertEqual(replay["sprint"]["status"], "stopped")

    def test_close_of_a_stopped_sprint_uses_the_host_lifecycle_edge(self) -> None:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = writer.create(
            role="po",
            actor="operator",
            goal="close stopped",
            product="secretary",
            issues=["issue:open"],
            projects=["secretary"],
            observer=head_choice("codex-observer"),
        )["sprint"]["ref"]
        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="stop-before-close",
        )

        first = writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="close-stopped",
            decisions=KEEP_THE_ISSUE_OPEN,
        )
        replay = writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="close-stopped",
            decisions=KEEP_THE_ISSUE_OPEN,
        )

        self.assertEqual(first["event_id"], replay["event_id"])
        self.assertEqual(first["sprint"]["status"], "closed")
        typed = writer.audit.committed_event("close-stopped:typed-close")
        assert typed is not None
        self.assertEqual(typed["transition"], {"source": "stopped", "target": "closed"})

    def test_reconcile_never_publishes_a_pending_hard_charge_before_its_typed_stop(self) -> None:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = writer.create(
            role="po",
            actor="operator",
            goal="reconcile hard stop",
            product="secretary",
            issues=["issue:open"],
            projects=["secretary"],
            observer=head_choice("codex-observer"),
        )["sprint"]["ref"]
        original_append = writer.audit.append

        def fail_typed(request_id: str, event: dict) -> str:
            if request_id == "reconcile-hard:typed-hard-stop":
                raise OSError("disk full")
            return original_append(request_id, event)

        with mock.patch.object(writer.audit, "append", side_effect=fail_typed):
            with self.assertRaises(TaskError):
                writer.record_budget(
                    role="dispatcher",
                    actor="dispatcher",
                    reference=ref,
                    event_type="blocked",
                    request_id="reconcile-hard",
                )

        self.assertEqual(TaskWriter(self.client, data_dir=self.tmp.name).reconcile(), (2, 0))  # type: ignore[arg-type]
        kinds = [event["kind"] for event in TaskAudit(self.tmp.name).events(reference=ref)]
        self.assertLess(kinds.index("sprint.stopped"), kinds.index("budget_recorded"))
        self.assertEqual(SprintReader(self.client).show(ref)["status"], "stopped")  # type: ignore[arg-type]

    def test_reopen_refuses_to_open_when_observer_read_back_is_not_proven(self) -> None:
        ref = self._create(goal="observer read back", reference="sprint:observer-readback")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)
        original = self.client.call
        stale = [False]

        def readback_failure(method: str, **params: object) -> object:
            if method == "saveTaskMetadata" and OBSERVER_FIELD in params.get("values", {}):
                stale[0] = True
                return original(method, **params)
            if method == "getTaskMetadata" and stale[0]:
                stale[0] = False
                return {OBSERVER_FIELD: ""}
            return original(method, **params)

        with mock.patch.object(self.client, "call", side_effect=readback_failure):
            with self.assertRaisesRegex(TaskError, "pending repair") as raised:
                self.writer.reopen(
                    role="po",
                    actor="operator",
                    reference=ref,
                    observer=head_choice("codex-observer"),
                    request_id="observer-readback",
                )

        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        self.assertIsNone(self.writer.audit.event("observer-readback:typed-reopen"))

    def test_lifecycle_events_carry_the_checked_edge_and_available_links(self) -> None:
        ref = self._create(goal="typed lifecycle", reference="sprint:typed-lifecycle")["sprint"]["ref"]
        self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="typed-close",
            decisions=KEEP_THE_ISSUE_OPEN,
        )
        self.writer.reopen(
            role="po",
            actor="operator",
            reference=ref,
            observer=head_choice("codex-observer"),
            request_id="typed-reopen",
        )

        events = {
            event["kind"]: event
            for event in TaskAudit(self.tmp.name).events(reference=ref)
            if event.get("record_type") == "board.protocol_event"
        }
        self.assertEqual(events["sprint.closed"]["subject"], {"kind": "sprint", "ref": ref})
        self.assertEqual(events["sprint.closed"]["actor"], {"role": "po", "id": "operator"})
        self.assertEqual(events["sprint.closed"]["reason"], "Sprint closed")
        self.assertEqual(events["sprint.closed"]["transition"], {"source": "open", "target": "closed"})
        self.assertEqual(events["sprint.closed"]["related_refs"], ["product:secretary", "issue:open"])
        self.assertEqual(events["sprint.reopened"]["transition"], {"source": "closed", "target": "open"})
        self.assertEqual(
            events["sprint.reopened"]["data"],
            {
                "observer": encode_observer(head_choice("codex-observer")),
                "request_related_refs": [],
            },
        )

    def test_host_sprint_replay_rejects_changed_related_refs_when_committed_or_pending(self) -> None:
        actor = Actor("po", "operator")

        committed_ref = self._create(goal="committed related refs")["sprint"]["ref"]
        host = self.writer._host()
        committed = TransitionRequest(
            EntityKind.SPRINT,
            committed_ref,
            SprintState.CLOSED,
            actor,
            "Sprint closed",
            RelatedRefs(("head-run:first",)),
            "committed-related-refs",
        )
        host.transition(committed)
        with self.assertRaises(ValueError):
            host.transition(
                TransitionRequest(
                    EntityKind.SPRINT,
                    committed_ref,
                    SprintState.CLOSED,
                    actor,
                    "Sprint closed",
                    RelatedRefs(("head-run:changed",)),
                    "committed-related-refs",
                )
            )

        pending_ref = self._create(
            goal="pending related refs",
            reference="sprint:pending-related-refs",
        )["sprint"]["ref"]
        pending = TransitionRequest(
            EntityKind.SPRINT,
            pending_ref,
            SprintState.CLOSED,
            actor,
            "Sprint closed",
            RelatedRefs(("head-run:first",)),
            "pending-related-refs",
        )
        with mock.patch.object(host.canon.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaises(BoardEventPending):
                host.transition(pending)
        with self.assertRaises(ValueError):
            host.transition(
                TransitionRequest(
                    EntityKind.SPRINT,
                    pending_ref,
                    SprintState.CLOSED,
                    actor,
                    "Sprint closed",
                    RelatedRefs(("head-run:changed",)),
                    "pending-related-refs",
                )
            )
        self.assertEqual(host.transition(pending).entity.state, SprintState.CLOSED)

    def test_pending_typed_close_recovers_without_repeating_the_status_write(self) -> None:
        ref = self._create(goal="recover typed close", reference="sprint:recover-typed-close")["sprint"][
            "ref"
        ]
        with mock.patch.object(self.writer.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "pending repair") as raised:
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="recover-typed-close",
                    decisions=KEEP_THE_ISSUE_OPEN,
                )

        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        pending = self.writer.audit.pending_event("recover-typed-close:typed-close")
        assert pending is not None
        self.assertEqual(pending["transition"], {"source": "open", "target": "closed"})
        writes = sum(
            values == {"sprint_status": "closed"}
            for method, params in self.client.calls
            if method == "saveTaskMetadata"
            for values in [params["values"]]
        )
        with self.assertRaisesRegex(TaskError, "typed Sprint lifecycle occurrence") as refused:
            self.writer.record_budget(
                role="po",
                actor="operator",
                reference=ref,
                event_type="blocked",
                request_id="recover-typed-close:typed-close",
            )
        self.assertEqual(refused.exception.code, "validation")
        self.assertEqual(self.writer.audit.pending_event("recover-typed-close:typed-close"), pending)

        self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="recover-typed-close",
            decisions=KEEP_THE_ISSUE_OPEN,
        )

        self.assertEqual(self.writer.audit.pending_event("recover-typed-close:typed-close"), None)
        self.assertEqual(
            writes,
            sum(
                values == {"sprint_status": "closed"}
                for method, params in self.client.calls
                if method == "saveTaskMetadata"
                for values in [params["values"]]
            ),
        )
        self.writer.reopen(
            role="po",
            actor="operator",
            reference=ref,
            observer=head_choice("codex-observer"),
            request_id="recover-typed-close-reopen",
        )
        self.assertEqual(TaskWriter(self.client, data_dir=self.tmp.name).reconcile(), (0, 0))  # type: ignore[arg-type]

    def test_budget_thresholds_reject_hard_limit_below_signal(self) -> None:
        with self.assertRaisesRegex(TaskError, "hard threshold"):
            budget_thresholds({"sprint_budget": {"signal": 3, "hard": 2}})

    def test_task_link_is_live_metadata_and_closed_sprint_rejects_writes(self) -> None:
        ref = self._create(goal="link")["sprint"]["ref"]
        task_writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        task_writer.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
            request_id="linked-card",
        )
        self.assertEqual(TaskReader(self.client).list(sprint=ref)[0]["sprint"], ref)  # type: ignore[arg-type]
        shown = SprintReader(self.client).show(ref)  # type: ignore[arg-type]
        self.assertEqual([card["ref"] for card in shown["cards"]], ["secretary-13"])
        self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            decisions=drop_cards("secretary-13"),
        )
        with self.assertRaisesRegex(TaskError, "closed"):
            self.writer.comment(role="worker", actor="worker", reference=ref, body="late")
        with self.assertRaisesRegex(TaskError, "closed"):
            task_writer.create(
                role="po",
                actor="operator",
                project="secretary",
                task_type="code",
                title="late",
                target="ready",
                sprint=ref,
            )

    def test_task_creation_requires_an_open_reserved_sprint_and_rejects_priority_before_writes(self) -> None:
        ref = self._create(goal="binding")["sprint"]["ref"]
        writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

        # An unlinked card in a project the sprint reserves is answered by the reservation
        # guard, not by the admission rule; the admission rule is what a project outside every
        # reservation still meets.
        for kwargs, code in (
            ({"project": "other"}, "validation"),
            ({}, "sprint_write_forbidden"),
            ({"sprint": ref, "project": "other"}, "sprint_project_unreserved"),
            ({"sprint": ref, "priority": "P1"}, "validation"),
        ):
            before = len(self.client.calls)
            arguments = {
                "role": "po",
                "actor": "operator",
                "project": "secretary",
                "task_type": "code",
                "title": "rejected",
                "target": "ready",
                **kwargs,
            }
            with self.assertRaises(TaskError) as raised:
                writer.create(**arguments)
            self.assertEqual(raised.exception.code, code)
            self.assertFalse(
                any(
                    method in {"createTask", "updateTask", "saveTaskMetadata"}
                    for method, _params in self.client.calls[before:]
                )
            )

    def test_close_archives_only_its_done_tasks_and_leaves_issues_and_unlinked_cards(self) -> None:
        ref = self._create(goal="close")["sprint"]["ref"]
        writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        done = writer.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="done",
            target="ready",
            sprint=ref,
            request_id="close-done",
        )["task"]
        open_task = writer.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="open",
            target="ready",
            sprint=ref,
            request_id="close-open",
        )["task"]
        writer.claim(
            role="dispatcher",
            actor="dispatcher",
            reference=done["ref"],
            worker="worker",
            request_id="close-claim",
        )
        writer.move(
            role="dispatcher",
            actor="dispatcher",
            reference=done["ref"],
            target="validate",
            reason="",
            request_id="close-validate",
        )
        writer.move(
            role="dispatcher",
            actor="dispatcher",
            reference=done["ref"],
            target="done",
            reason="",
            request_id="close-done-move",
        )
        done_row = next(task for task in self.client.tasks if task["reference"] == done["ref"])
        self.assertIsNone(TaskReader(self.client).show(done["ref"])["claim"]["worker"])  # type: ignore[arg-type]
        # Even malformed metadata cannot turn an Issue into a close target.
        self.client.metadata[22]["sprint_ref"] = ref

        decisions = drop_cards(open_task["ref"])
        first = self.writer.close(
            role="po", actor="operator", reference=ref, request_id="close-once", decisions=decisions
        )
        second = self.writer.close(
            role="po", actor="operator", reference=ref, request_id="close-once", decisions=decisions
        )

        self.assertEqual(first["archived_tasks"], [done["ref"]])
        self.assertEqual(first["remaining_tasks"], [open_task["ref"]])
        # The card that was not done is not left behind either: it is taken off the closed
        # contract by the disposition its caller gave, and only Product and Issue records and
        # cards of no sprint are untouched by a close.
        self.assertEqual(first["disposed_tasks"], [open_task["ref"]])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(
            next(task for task in self.client.tasks if task["reference"] == done["ref"])["is_active"], 0
        )
        open_row = next(task for task in self.client.tasks if task["reference"] == open_task["ref"])
        self.assertEqual(open_row["is_active"], 0)
        self.assertNotEqual(
            next(task for task in self.client.tasks if task["reference"] == "secretary-12").get(
                "is_active", 1
            ),
            0,
        )
        self.assertNotEqual(
            next(task for task in self.client.tasks if task["reference"] == "issue:open").get("is_active", 1),
            0,
        )
        close_calls = [params["task_id"] for method, params in self.client.calls if method == "closeTask"]
        self.assertEqual(close_calls, [done_row["id"], open_row["id"]])
        self.assertEqual(
            len(
                [
                    event
                    for event in TaskAudit(self.tmp.name).events(reference=ref)
                    if event["kind"] == "closed"
                ]
            ),
            1,
        )

    def test_close_repairs_an_archive_that_lost_its_backend_reply(self) -> None:
        ref = self._create(goal="repair")["sprint"]["ref"]
        writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        done = writer.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="done",
            target="ready",
            sprint=ref,
            request_id="repair-done",
        )["task"]
        writer.claim(
            role="dispatcher",
            actor="dispatcher",
            reference=done["ref"],
            worker="worker",
            request_id="repair-claim",
        )
        writer.move(
            role="dispatcher",
            actor="dispatcher",
            reference=done["ref"],
            target="validate",
            reason="",
            request_id="repair-validate",
        )
        writer.move(
            role="dispatcher",
            actor="dispatcher",
            reference=done["ref"],
            target="done",
            reason="",
            request_id="repair-done-move",
        )
        done_row = next(task for task in self.client.tasks if task["reference"] == done["ref"])
        original_call = self.client.call
        lost = False

        def close_then_lose(method: str, **params: object) -> object:
            nonlocal lost
            result = original_call(method, **params)
            if method == "closeTask" and not lost:
                lost = True
                raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1)
            return result

        with mock.patch.object(self.client, "call", side_effect=close_then_lose):
            with self.assertRaisesRegex(TaskError, "pending repair"):
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="repair-close",
                    decisions=KEEP_THE_ISSUE_OPEN,
                )
            repaired = self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                request_id="repair-close",
                decisions=KEEP_THE_ISSUE_OPEN,
            )

        self.assertEqual(repaired["archived_tasks"], [done["ref"]])
        self.assertEqual(repaired["remaining_tasks"], [])
        self.assertEqual(len([params for method, params in self.client.calls if method == "closeTask"]), 1)
        self.assertEqual(
            len(
                [
                    event
                    for event in TaskAudit(self.tmp.name).events(reference=ref)
                    if event["kind"] == "closed"
                ]
            ),
            1,
        )

    def test_close_propagates_a_terminal_archive_refusal_without_leaving_a_transaction(self) -> None:
        ref = self._create(goal="terminal refusal")["sprint"]["ref"]
        writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        done = writer.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="done",
            target="ready",
            sprint=ref,
            request_id="terminal-done",
        )["task"]
        done_row = next(task for task in self.client.tasks if task["reference"] == done["ref"])
        done_row["column_id"] = 6

        with mock.patch.object(TaskWriter, "archive", side_effect=TaskError("live_work", "live worker", 3)):
            with self.assertRaises(TaskError) as raised:
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="terminal-close",
                    decisions=KEEP_THE_ISSUE_OPEN,
                )

        self.assertEqual(raised.exception.code, "live_work")
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(
            [event["kind"] for event in TaskAudit(self.tmp.name).events(reference=ref)],
            ["created"],
        )

    def test_cli_create_and_list_return_stable_json(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.sprint_commands.KanboardClient.for_instance", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "sprint",
                    "create",
                    "--role",
                    "po",
                    "--data-dir",
                    self.tmp.name,
                    "--instance",
                    str(self.instance),
                    "--goal",
                    "CLI sprint",
                    "--repository",
                    "secretary",
                    "--product",
                    "secretary",
                    "--issue",
                    "issue:open",
                    "--project",
                    "secretary",
                    "--request-id",
                    "cli-create",
                    "--observer",
                    "codex-observer",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        result = json.loads(output.getvalue())
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["sprint"]["repositories"], [str(Path("secretary").resolve())])
        self.assertEqual(result["sprint"]["product"], "secretary")
        self.assertEqual(result["sprint"]["issues"], ["issue:open"])
        self.assertEqual(result["sprint"]["reservations"], ["secretary"])

    def test_cli_observer_can_set_current_task(self) -> None:
        ref = self._create(goal="observer current task")["sprint"]["ref"]
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
        )["task"]
        output, errors = io.StringIO(), io.StringIO()

        with (
            mock.patch("secretary.sprint_commands.KanboardClient.for_instance", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "sprint",
                    "current-task",
                    "--ref",
                    ref,
                    "--role",
                    "observer",
                    "--actor",
                    "observer",
                    "--task",
                    task["ref"],
                    "--data-dir",
                    self.tmp.name,
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["sprint"]["current_task"], task["ref"])

    def test_resume_requires_all_fields_and_staleness_uses_card_audit(self) -> None:
        ref = self._create(goal="resume")["sprint"]["ref"]
        with self.assertRaisesRegex(TaskError, "missing required fields"):
            self.writer.resume(role="po", actor="operator", reference=ref, entry={"selected_step": "x"})
        entry = {
            "selected_step": "implement",
            "selected_why": "needed",
            "rejected_alternatives": "wait",
            "current_task": "next card",
            "dod_state": "tests pending",
            "next_safe_step": "run tests",
            "recorded_at": "2000-01-01T00:00:00Z",
        }
        self.writer.resume(role="po", actor="operator", reference=ref, entry=entry, request_id="resume")
        fresh = SprintReader(self.client, data_dir=self.tmp.name).show(ref)  # type: ignore[arg-type]
        self.assertTrue(fresh["resume_freshness"]["fresh"])
        task_writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        task = task_writer.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
            request_id="resume-card",
        )["task"]
        TaskAudit(self.tmp.name).append(
            "later",
            {
                "event_id": "evt_later",
                "request_id": "later",
                "ref": task["ref"],
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"to": "assessment"},
                "occurred_at": "2099-01-01T00:00:00Z",
            },
        )
        stale = SprintReader(self.client, data_dir=self.tmp.name).show(ref)  # type: ignore[arg-type]
        self.assertFalse(stale["resume_freshness"]["fresh"])
        self.assertEqual(stale["resume_freshness"]["error"], "resume_stale")

    def test_naive_resume_timestamp_is_rejected_and_legacy_data_fails_closed(self) -> None:
        ref = self._create(goal="naive resume")["sprint"]["ref"]
        entry = {
            "selected_step": "implement",
            "selected_why": "needed",
            "rejected_alternatives": "wait",
            "current_task": "next card",
            "dod_state": "tests pending",
            "next_safe_step": "run tests",
            "recorded_at": "2026-07-29T12:00:00",
        }
        with self.assertRaisesRegex(TaskError, "must include a timezone"):
            self.writer.resume(role="observer", actor="observer", reference=ref, entry=entry)

        sprint = next(item for item in self.client.tasks if item["reference"] == ref)
        self.client.metadata[int(sprint["id"])]["sprint_resume"] = json.dumps(entry)
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
            request_id="naive-card",
        )["task"]
        TaskAudit(self.tmp.name).append(
            "naive-event",
            {
                "event_id": "evt_naive_event",
                "request_id": "naive-event",
                "ref": task["ref"],
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"to": "assessment"},
                "occurred_at": "2099-01-01T00:00:00Z",
            },
        )

        shown = SprintReader(self.client, data_dir=self.tmp.name).show(ref)  # type: ignore[arg-type]

        self.assertFalse(shown["resume_freshness"]["fresh"])
        self.assertEqual(shown["resume_freshness"]["error"], "resume_stale")
        self.assertIsNone(shown["resume_freshness"]["lag_seconds"])

    def test_observer_can_record_a_complete_resume_entry(self) -> None:
        ref = self._create(goal="observer resume")["sprint"]["ref"]
        entry = {
            "selected_step": "implement",
            "selected_why": "needed",
            "rejected_alternatives": "wait",
            "current_task": "secretary-14",
            "dod_state": "tests pending",
            "next_safe_step": "run tests",
        }

        result = self.writer.resume(
            role="observer",
            actor="observer-head",
            reference=ref,
            entry=entry,
            request_id="observer-resume",
            delivery_id="delivery-1",
            through_event="evt-card-1",
        )

        self.assertEqual(result["action"], "resume_recorded")
        self.assertEqual(result["sprint"]["resume"]["selected_step"], "implement")
        self.assertNotIn("delivery_id", result["sprint"]["resume"])
        resume_event = TaskAudit(self.tmp.name).events(reference=ref)[-1]
        self.assertEqual(resume_event["payload"]["delivery_id"], "delivery-1")
        self.assertEqual(resume_event["payload"]["through_event"], "evt-card-1")
        with self.assertRaisesRegex(TaskError, "requires both"):
            self.writer.resume(
                role="observer",
                actor="observer-head",
                reference=ref,
                entry=entry,
                delivery_id="delivery-2",
            )
        with self.assertRaisesRegex(TaskError, "only an observer"):
            self.writer.resume(
                role="po",
                actor="operator",
                reference=ref,
                entry=entry,
                delivery_id="delivery-2",
                through_event="evt-card-2",
            )

    def test_resume_freshness_ignores_denied_and_failed_card_events(self) -> None:
        ref = self._create(goal="event predicate")["sprint"]["ref"]
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
            request_id="predicate-card",
        )["task"]
        entry = {
            "selected_step": "wait",
            "selected_why": "no durable transition",
            "rejected_alternatives": "act",
            "current_task": task["ref"],
            "dod_state": "open",
            "next_safe_step": "wait",
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
            audit.append(
                request_id,
                {
                    "event_id": "evt_" + request_id,
                    "request_id": request_id,
                    "ref": task["ref"],
                    "kind": kind,
                    "outcome": outcome,
                    "occurred_at": "2099-01-01T00:00:00Z",
                },
            )

        freshness = SprintReader(self.client, data_dir=self.tmp.name).show(ref)["resume_freshness"]  # type: ignore[arg-type]

        self.assertTrue(freshness["fresh"])
        self.assertEqual(freshness["last_event_at"], baseline["last_event_at"])


class SprintAuditTraversalTests(SprintFixture):
    """A mass sprint summary costs one audit traversal, not one per sprint."""

    @contextlib.contextmanager
    def _traversals(self):
        """Count the committed-audit traversals a block performs."""
        counter: dict[str, int] = {"count": 0}
        original = TaskAudit.events

        def counting(audit: TaskAudit, *args: Any, **kwargs: Any) -> list[dict]:
            counter["count"] += 1
            return original(audit, *args, **kwargs)

        with mock.patch.object(TaskAudit, "events", counting):
            yield counter

    def _resumed(self, goal: str) -> str:
        """An open sprint holding a recorded resume, ready to be judged for freshness."""
        ref = self._create(goal=goal)["sprint"]["ref"]
        self.writer.resume(
            role="po",
            actor="operator",
            reference=ref,
            request_id=f"resume-{goal}",
            entry={
                "selected_step": "implement",
                "selected_why": "needed",
                "rejected_alternatives": "wait",
                "current_task": "next card",
                "dod_state": "tests pending",
                "next_safe_step": "run tests",
                "recorded_at": "2000-01-01T00:00:00Z",
            },
        )
        return ref

    def _entry(self, recorded_at: str = "2000-01-01T00:00:00Z") -> dict[str, str]:
        return {
            "selected_step": "implement",
            "selected_why": "needed",
            "rejected_alternatives": "wait",
            "current_task": "next card",
            "dod_state": "tests pending",
            "next_safe_step": "run tests",
            "recorded_at": recorded_at,
        }

    def _terminal(self, reference: str, *, status: str, recorded_at: str = "2000-01-01T00:00:00Z") -> str:
        """A closed or stopped sprint carrying a resume, seeded straight onto the board.

        A terminal sprint holds no reservation, so a live board accumulates them beside the
        open one; the writer cannot open a second sprint over the fixture's project.  The real
        `close`, hard-budget stop and restore transitions are traced separately, over rows this
        fixture's writer produces itself.
        """
        board = ensure_sprint_board(self.client)  # type: ignore[arg-type]
        task_id = max(int(task["id"]) for task in self.client.tasks) + 1
        self.client.tasks.append(
            {
                "id": task_id,
                "project_id": board,
                "reference": reference,
                "title": reference,
                "description": "",
                "column_id": self.client.columns[board][0]["id"],
                "position": task_id,
                "swimlane_id": 0,
                "is_active": 1,
                "date_creation": "1720000000",
                "date_modification": "1720000000",
            }
        )
        self.client.metadata[task_id] = {
            "sprint_goal": "seeded",
            "sprint_definition_of_done": "done",
            "sprint_repositories": json.dumps(["secretary"]),
            "sprint_status": status,
            "sprint_current_task": "",
            "sprint_resume": json.dumps(self._entry(recorded_at)),
        }
        self.client.comments[task_id] = []
        return reference

    def _sprint_event(self, reference: str, request_id: str, occurred_at: str) -> None:
        """A significant sprint-scoped event: it would age the resume of an open sprint."""
        TaskAudit(self.tmp.name).append(
            request_id,
            {
                "event_id": f"evt_{request_id.replace('-', '_')}",
                "request_id": request_id,
                "ref": reference,
                "kind": "budget_recorded",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"event_type": "red_ci"},
                "occurred_at": occurred_at,
            },
        )

    def _reader(self) -> SprintReader:
        return SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

    @contextlib.contextmanager
    def _round_trips(self):
        """Count the board round trips a block performs, a batched read counting as the one it is."""
        trips: list[str] = []
        client = type(self.client)
        original_call, original_batch = client.call, client.call_batch
        batching = False

        def counting_call(fake, method: str, **params: Any) -> Any:
            if not batching:
                trips.append(method)
            return original_call(fake, method, **params)

        def counting_batch(fake, calls: Any) -> Any:
            nonlocal batching
            calls = list(calls)
            trips.append("batch:" + ",".join(sorted({method for method, _ in calls})))
            batching = True
            try:
                return original_batch(fake, calls)
            finally:
                batching = False

        with (
            mock.patch.object(client, "call", counting_call),
            mock.patch.object(client, "call_batch", counting_batch),
        ):
            yield trips

    def test_mass_sprint_status_costs_the_same_board_round_trips_for_one_and_for_many(self) -> None:
        """The shape of the reads: what a mass status asks the board for, and how often.

        A per-sprint `status` read that sprint's metadata, its comments and the whole Pipeline
        listing - every card's metadata included - once per sprint. This pins the call structure
        that replaced it, over the fake, where a batch stands for the calls it carries. What that
        costs on the wire is a separate question this cannot answer, because the fake answers a
        batch by looping: `tests/test_board_batch_transport.py` counts the actual posts.
        """
        open_ref = self._resumed("round trips")
        TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="linked",
            target="ready",
            sprint=open_ref,
            request_id="round-trip-card",
        )

        with self._round_trips() as single:
            self._reader().statuses()

        for index in range(4):
            self._terminal(f"sprint:round-trip-{index}", status="closed")

        with self._round_trips() as many:
            self._reader().statuses()

        self.assertEqual(
            single,
            [
                "getProjectByName",  # the sprint board
                "getAllTasks",  # its rows
                "batch:getTaskMetadata",
                "getProjectByName",  # the Pipeline, read once for every sprint together
                "getColumns",
                "getActiveSwimlanes",
                "getAllTasks",
                "batch:getTaskMetadata",
            ],
        )
        self.assertEqual(many, single)

    def test_mass_sprint_status_reads_the_audit_once_for_one_and_for_many_sprints(self) -> None:
        open_ref = self._resumed("audit traversal")
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="linked",
            target="ready",
            sprint=open_ref,
            request_id="traversal-card",
        )["task"]
        TaskAudit(self.tmp.name).append(
            "traversal-later",
            {
                "event_id": "evt_traversal_later",
                "request_id": "traversal-later",
                "ref": task["ref"],
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"to": "assessment"},
                "occurred_at": "2099-01-01T00:00:00Z",
            },
        )

        with self._traversals() as single:
            one = self._reader().statuses()

        for index in range(4):
            self._terminal(f"sprint:seeded-{index}", status="closed" if index % 2 else "stopped")

        with self._traversals() as many:
            all_sprints = self._reader().statuses()

        self.assertEqual(len(one), 1)
        self.assertEqual(len(all_sprints), 5)
        self.assertEqual(single["count"], 1)
        # The cost is the traversal, not the sprint count: five summaries read the journal once.
        self.assertEqual(many["count"], 1)
        for summary in (one[0], next(item for item in all_sprints if item["ref"] == open_ref)):
            # The open sprint still sees the significant later linked-card event.
            self.assertFalse(summary["resume_freshness"]["fresh"])
            self.assertEqual(summary["resume_freshness"]["error"], "resume_stale")
            self.assertEqual(summary["resume_freshness"]["last_event_at"], "2099-01-01T00:00:00Z")

    def test_sprint_list_reads_no_audit_and_a_single_status_reads_it_once(self) -> None:
        ref = self._resumed("single status")

        with self._traversals() as listing:
            listed = self._reader().list()
        with self._traversals() as single:
            summary = self._reader().status(ref)

        self.assertEqual([sprint["ref"] for sprint in listed], [ref])
        self.assertEqual(listing["count"], 0)
        self.assertEqual(single["count"], 1)
        self.assertTrue(summary["resume_freshness"]["fresh"])

    def test_closed_and_stopped_summaries_keep_the_documented_freshness_shape(self) -> None:
        closed = self._terminal("sprint:closed-summary", status="closed")
        stopped = self._terminal("sprint:stopped-summary", status="stopped")
        # Events that would age an open sprint's resume.  A terminal sprint is judged by its own
        # frozen record, so they are never read: the whole summary costs no traversal at all.
        self._sprint_event(closed, "closed-later", "2099-01-01T00:00:00Z")
        self._sprint_event(stopped, "stopped-later", "2099-01-01T00:00:00Z")

        with self._traversals() as traversals:
            summaries = {item["ref"]: item for item in self._reader().statuses()}

        self.assertEqual(traversals["count"], 0)
        self.assertEqual(summaries[closed]["status"], "closed")
        self.assertEqual(summaries[stopped]["status"], "stopped")
        self.assertEqual(summaries[stopped]["stop_reason"], "budget_hard_limit")
        self.assertIsNone(summaries[closed]["stop_reason"])
        for ref in (closed, stopped):
            freshness = summaries[ref]["resume_freshness"]
            self.assertEqual(
                sorted(freshness),
                ["error", "fresh", "lag_seconds", "last_event_at", "recorded_at", "threshold_seconds"],
            )
            self.assertEqual(freshness["recorded_at"], "2000-01-01T00:00:00Z")
            self.assertIsNone(freshness["last_event_at"])
            self.assertEqual(freshness["lag_seconds"], 0)
            self.assertTrue(freshness["fresh"])
            self.assertIsNone(freshness["error"])

    def test_an_all_terminal_installation_summarises_without_touching_the_audit(self) -> None:
        """The acceptance case: only closed and stopped rows, every one with a valid resume."""
        refs = [
            self._terminal(f"sprint:terminal-{index}", status="closed" if index % 2 else "stopped")
            for index in range(6)
        ]
        for index, ref in enumerate(refs):
            self._sprint_event(ref, f"terminal-later-{index}", "2099-01-01T00:00:00Z")

        with self._traversals() as mass:
            summaries = {item["ref"]: item for item in self._reader().statuses()}
        with self._traversals() as single:
            direct = self._reader().status(refs[0])

        self.assertEqual(sorted(summaries), sorted(refs))
        self.assertEqual(mass["count"], 0)
        self.assertEqual(single["count"], 0)
        for freshness in [item["resume_freshness"] for item in summaries.values()] + [
            direct["resume_freshness"]
        ]:
            self.assertTrue(freshness["fresh"])
            self.assertIsNone(freshness["error"])
            self.assertIsNone(freshness["last_event_at"])

    def test_every_terminal_transition_freezes_freshness_on_the_sprint_record(self) -> None:
        """Close, hard-budget stop and restore all reach the same read-time rule."""
        closing = self._resumed("close transition")
        card = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="linked",
            target="ready",
            sprint=closing,
            request_id="close-card",
        )["task"]
        TaskAudit(self.tmp.name).append(
            "close-later",
            {
                "event_id": "evt_close_later",
                "request_id": "close-later",
                "ref": card["ref"],
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"to": "assessment"},
                "occurred_at": "2099-01-01T00:00:00Z",
            },
        )

        with self._traversals() as while_open:
            open_summary = self._reader().status(closing)
        self.writer.close(
            role="po",
            actor="operator",
            reference=closing,
            request_id="close-it",
            decisions=drop_cards(card["ref"]),
        )
        with self._traversals() as after_close:
            closed_summary = self._reader().status(closing)

        # Open: one traversal and the later card event is seen.  Closed: the same record, no read.
        self.assertEqual(while_open["count"], 1)
        self.assertEqual(open_summary["resume_freshness"]["error"], "resume_stale")
        self.assertEqual(open_summary["resume_freshness"]["last_event_at"], "2099-01-01T00:00:00Z")
        self.assertEqual(after_close["count"], 0)
        self.assertEqual(closed_summary["status"], "closed")
        self.assertTrue(closed_summary["resume_freshness"]["fresh"])
        self.assertEqual(closed_summary["resume_freshness"]["recorded_at"], "2000-01-01T00:00:00Z")
        # The record cannot move after the transition, which is what makes it the durable source.
        with self.assertRaisesRegex(TaskError, "sprint is closed"):
            self.writer.resume(
                role="po",
                actor="operator",
                reference=closing,
                request_id="resume-after-close",
                entry=self._entry("2030-01-01T00:00:00Z"),
            )

        stopping = self._resumed("stop transition")
        budget_writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        budget_writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=stopping,
            event_type="blocked",
            request_id="hard-stop",
            source_event_id="evt-card-blocked",
        )

        with self._traversals() as after_stop:
            stopped_summary = self._reader().status(stopping)

        self.assertEqual(stopped_summary["status"], "stopped")
        self.assertEqual(stopped_summary["stop_reason"], "budget_hard_limit")
        # The stop itself writes a significant sprint event, and it still ages nothing.
        self.assertEqual(after_stop["count"], 0)
        self.assertTrue(stopped_summary["resume_freshness"]["fresh"])
        self.assertEqual(stopped_summary["resume_freshness"]["recorded_at"], "2000-01-01T00:00:00Z")

        restored = self.writer.restore_create(
            reference="sprint:restored",
            goal="restored",
            status="closed",
            request_id="restore-create",
        )["sprint"]["ref"]
        self.writer.restore(
            reference=restored,
            values={"sprint_resume": json.dumps(self._entry("2001-01-01T00:00:00Z"))},
            request_id="restore-resume",
        )
        self._sprint_event(restored, "restored-later", "2099-01-01T00:00:00Z")

        with self._traversals() as after_restore:
            restored_summary = self._reader().status(restored)

        self.assertEqual(restored_summary["status"], "closed")
        self.assertEqual(after_restore["count"], 0)
        self.assertTrue(restored_summary["resume_freshness"]["fresh"])
        self.assertEqual(restored_summary["resume_freshness"]["recorded_at"], "2001-01-01T00:00:00Z")

    def test_a_reopened_sprint_is_judged_against_the_audit_again(self) -> None:
        """The rule reads the sprint's state, so it is not sticky once the sprint reopens."""
        ref = self._resumed("reopen transition")
        self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="close-before-reopen",
            decisions=KEEP_THE_ISSUE_OPEN,
        )
        self._sprint_event(ref, "reopen-later", "2099-01-01T00:00:00Z")

        with self._traversals() as closed:
            closed_summary = self._reader().status(ref)
        self.writer.reopen(
            role="po",
            actor="operator",
            reference=ref,
            request_id="reopen-it",
            observer=head_choice("codex-observer"),
        )
        with self._traversals() as reopened:
            reopened_summary = self._reader().status(ref)

        self.assertEqual(closed["count"], 0)
        self.assertTrue(closed_summary["resume_freshness"]["fresh"])
        self.assertEqual(reopened["count"], 1)
        self.assertEqual(reopened_summary["resume_freshness"]["error"], "resume_stale")
        self.assertEqual(reopened_summary["resume_freshness"]["last_event_at"], "2099-01-01T00:00:00Z")

    def test_an_unusable_terminal_resume_still_fails_closed_without_the_audit(self) -> None:
        """A legacy record the writer would refuse today keeps its documented answer."""
        naive = self._terminal("sprint:legacy-terminal", status="closed", recorded_at="2026-07-29T12:00:00")
        empty = self._terminal("sprint:missing-terminal", status="stopped")
        empty_id = next(int(task["id"]) for task in self.client.tasks if task.get("reference") == empty)
        self.client.metadata[empty_id].pop("sprint_resume")

        with self._traversals() as traversals:
            summaries = {item["ref"]: item for item in self._reader().statuses()}

        self.assertEqual(traversals["count"], 0)
        self.assertFalse(summaries[naive]["resume_freshness"]["fresh"])
        self.assertEqual(summaries[naive]["resume_freshness"]["error"], "resume_stale")
        # No event to trail, so the lag is the zero the helper already answers for that case; the
        # unusable timestamp still fails closed on its own.
        self.assertEqual(summaries[naive]["resume_freshness"]["lag_seconds"], 0)
        self.assertEqual(summaries[naive]["resume_freshness"]["recorded_at"], "2026-07-29T12:00:00")
        self.assertEqual(summaries[empty]["resume_freshness"]["error"], "resume_missing")
        self.assertIsNone(summaries[empty]["resume_freshness"]["recorded_at"])

    def test_a_missing_resume_still_answers_without_reading_the_audit(self) -> None:
        ref = self._create(goal="no resume")["sprint"]["ref"]

        with self._traversals() as traversals:
            summary = self._reader().status(ref)

        self.assertEqual(traversals["count"], 0)
        self.assertEqual(summary["resume_freshness"]["error"], "resume_missing")
        self.assertFalse(summary["resume_freshness"]["fresh"])


class SprintSingleWriterGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SprintKanboard()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sprints = SprintWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        # The guard is about card writes against an open sprint, not about opening one, so the
        # sprint is seeded through the restore route instead of `create`.
        self.ref = self.sprints.restore_create(
            reference="sprint:guard",
            goal="single writer",
            repositories=["secretary", "other"],
            request_id="seed-guard-sprint",
        )["sprint"]["ref"]
        sprint = next(task for task in self.client.tasks if task["reference"] == self.ref)
        self.client.metadata[int(sprint["id"])]["sprint_reservations"] = json.dumps(["secretary", "other"])
        # The reservations landed on the board behind the writer's back, so the index is
        # re-seeded from it the way a live installation seeds it.
        refresh_active_sprint_projects(self.tmp.name, SprintReader(self.client))  # type: ignore[arg-type]
        bind_observer(self, self.ref)

    def test_observer_must_link_to_its_open_sprint_and_other_roles_are_denied(self) -> None:
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="owned",
            sprint=self.ref,
            request_id="observer-create",
        )["task"]
        self.assertEqual(card["sprint"], self.ref)
        with self.assertRaisesRegex(TaskError, self.ref) as missing:
            self.tasks.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="unlinked",
                request_id="observer-unlinked",
            )
        self.assertEqual(missing.exception.code, "sprint_write_forbidden")
        with self.assertRaisesRegex(TaskError, self.ref) as retro:
            self.tasks.create(
                role="retro",
                actor="retro",
                project="secretary",
                task_type="research",
                title="finding",
                target="issues",
                request_id="retro-denied",
            )
        self.assertEqual(retro.exception.code, "sprint_write_forbidden")
        denied = [
            event for event in TaskAudit(self.tmp.name).events() if event["kind"] == "sprint_guard_denied"
        ]
        self.assertEqual(len(denied), 2)
        self.assertEqual(denied[0]["payload"]["sprint"], self.ref)

    def test_po_override_requires_reason_and_is_audited_once(self) -> None:
        with self.assertRaisesRegex(TaskError, "non-empty reason") as missing:
            self.tasks.create(
                role="po",
                actor="operator",
                project="secretary",
                task_type="code",
                title="urgent",
                sprint_override=True,
                request_id="override-empty",
            )
        self.assertEqual(missing.exception.code, "validation")
        first = self.tasks.create(
            role="po",
            actor="operator",
            project="secretary",
            task_type="code",
            title="urgent",
            sprint_override=True,
            sprint_override_reason="production incident",
            request_id="override-once",
        )
        second = self.tasks.create(
            role="po",
            actor="operator",
            project="secretary",
            task_type="code",
            title="urgent",
            sprint_override=True,
            sprint_override_reason="production incident",
            request_id="override-once",
        )
        self.assertEqual(first["event_id"], second["event_id"])
        event = next(
            event for event in TaskAudit(self.tmp.name).events() if event["request_id"] == "override-once"
        )
        self.assertEqual(event["payload"]["sprint_override_reason"], "production incident")

    def test_linked_task_still_obeys_the_held_project_guard(self) -> None:
        for role, actor, request_id in (
            ("po", "operator", "linked-po-denied"),
            ("steward", "steward", "linked-steward-denied"),
        ):
            with self.assertRaises(TaskError) as raised:
                self.tasks.create(
                    role=role,
                    actor=actor,
                    project="secretary",
                    task_type="code",
                    title="guarded",
                    sprint=self.ref,
                    request_id=request_id,
                )
            self.assertEqual(raised.exception.code, "sprint_write_forbidden")

        created = self.tasks.create(
            role="po",
            actor="operator",
            project="secretary",
            task_type="code",
            title="overridden",
            sprint=self.ref,
            sprint_override=True,
            sprint_override_reason="production incident",
            request_id="linked-po-override",
        )
        event = next(
            event
            for event in TaskAudit(self.tmp.name).events()
            if event["request_id"] == "linked-po-override"
        )
        self.assertEqual(created["task"]["sprint"], self.ref)
        self.assertEqual(event["payload"]["sprint_override_reason"], "production incident")
        denied = [
            event for event in TaskAudit(self.tmp.name).events() if event["kind"] == "sprint_guard_denied"
        ]
        self.assertEqual(
            [event["payload"]["operation_request_id"] for event in denied],
            ["linked-po-denied", "linked-steward-denied"],
        )

    def test_po_cannot_edit_a_held_card_without_an_audited_override(self) -> None:
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="owned",
            sprint=self.ref,
        )["task"]
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.edit(role="po", actor="operator", reference=card["ref"], description="outside edit")
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")
        edited = self.tasks.edit(
            role="po",
            actor="operator",
            reference=card["ref"],
            description="incident edit",
            sprint_override=True,
            sprint_override_reason="production incident",
        )
        self.assertEqual(edited["task"]["description"], "incident edit")
        event = TaskAudit(self.tmp.name).events()[-1]
        self.assertEqual(event["payload"]["sprint_override_reason"], "production incident")

    def test_override_retry_reuses_the_denied_request_id_for_the_write(self) -> None:
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="owned",
            sprint=self.ref,
        )["task"]
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.move(
                role="po",
                actor="operator",
                reference=card["ref"],
                target="blocked",
                reason="",
                request_id="po-override-retry",
            )
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")

        moved = self.tasks.move(
            role="po",
            actor="operator",
            reference=card["ref"],
            target="blocked",
            reason="",
            sprint_override=True,
            sprint_override_reason="production incident",
            request_id="po-override-retry",
        )

        self.assertEqual(moved["task"]["state"], "blocked")
        events = TaskAudit(self.tmp.name).events()
        denial = next(event for event in events if event["kind"] == "sprint_guard_denied")
        self.assertEqual(denial["payload"]["operation_request_id"], "po-override-retry")
        success = next(event for event in events if event["request_id"] == "po-override-retry")
        self.assertEqual(success["record_type"], "board.protocol_event")
        self.assertEqual(success["transition"], {"source": "ready", "target": "blocked"})
        # The typed event describes the lifecycle edge; the authority the PO used to make it is
        # its own generic record, and the journal still answers who overrode the sprint and why.
        grant = next(event for event in events if event["kind"] == "sprint_guard_override")
        self.assertEqual(grant["payload"]["sprint_override_reason"], "production incident")
        self.assertEqual(grant["payload"]["operation_request_id"], "po-override-retry")
        self.assertEqual(grant["payload"]["sprint"], self.ref)

    def test_every_move_path_through_the_sprint_guard_records_its_decision(self) -> None:
        """Ordinary, refused and granted-override moves all pass through the one guard.

        A first-attempt override never produces a denial, so before this it was the one decision
        the guard made that left nothing behind at all.
        """
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="owned",
            sprint=self.ref,
        )["task"]

        ordinary = self.tasks.move(
            role="dispatcher",
            actor="d",
            reference=card["ref"],
            target="in_progress",
            reason="",
            request_id="guard-ordinary-move",
        )
        with self.assertRaisesRegex(TaskError, self.ref) as refused:
            self.tasks.move(
                role="po",
                actor="operator",
                reference=card["ref"],
                target="blocked",
                reason="",
                request_id="guard-refused-move",
            )
        granted = self.tasks.move(
            role="po",
            actor="operator",
            reference=card["ref"],
            target="blocked",
            reason="",
            sprint_override=True,
            sprint_override_reason="production incident",
            request_id="guard-granted-move",
        )

        self.assertEqual(ordinary["task"]["state"], "in_progress")
        self.assertEqual(refused.exception.code, "sprint_write_forbidden")
        self.assertEqual(granted["task"]["state"], "blocked")
        decisions = {
            str(event["payload"].get("operation_request_id")): event["kind"]
            for event in TaskAudit(self.tmp.name).events()
            if event["kind"] in {"sprint_guard_denied", "sprint_guard_override"}
        }
        self.assertEqual(
            decisions,
            {
                "guard-refused-move": "sprint_guard_denied",
                "guard-granted-move": "sprint_guard_override",
            },
        )

    def test_a_granted_override_is_recorded_once_and_survives_reconcile(self) -> None:
        """The grant is idempotent under retry and is a repairable pending record like the denial."""
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="owned",
            sprint=self.ref,
        )["task"]

        def override_move() -> dict[str, object]:
            return self.tasks.move(
                role="po",
                actor="operator",
                reference=card["ref"],
                target="blocked",
                reason="",
                sprint_override=True,
                sprint_override_reason="production incident",
                request_id="override-recorded-once",
            )

        with mock.patch.object(self.tasks.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "audit repair") as pending:
                override_move()
        self.assertEqual(pending.exception.code, "audit_pending")
        self.assertEqual(self.tasks.reader.show(card["ref"])["state"], "ready")

        self.assertEqual(self.tasks.reconcile(), (1, 0))
        first = override_move()
        second = override_move()

        self.assertEqual(first["event_id"], second["event_id"])
        grants = [
            event for event in TaskAudit(self.tmp.name).events() if event["kind"] == "sprint_guard_override"
        ]
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["payload"]["sprint_override_reason"], "production incident")

    def test_denied_create_request_can_succeed_after_sprint_closes(self) -> None:
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.create(
                role="retro",
                actor="retro",
                project="secretary",
                task_type="research",
                title="finding",
                target="issues",
                request_id="retro-after-close",
            )
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")
        self.sprints.close(role="po", actor="operator", reference=self.ref)

        created = self.tasks.create(
            role="retro",
            actor="retro",
            project="secretary",
            task_type="research",
            title="finding",
            target="issues",
            request_id="retro-after-close",
        )

        self.assertEqual(created["task"]["project"], "secretary")
        self.assertEqual(created["task"]["state"], "issues")

    def test_dispatcher_cycle_and_observer_move_are_allowed(self) -> None:
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="cycle",
            sprint=self.ref,
        )["task"]
        self.assertEqual(card["state"], "ready")
        self.tasks.claim(role="dispatcher", actor="dispatcher", reference=card["ref"], worker="worker")
        result = self.tasks.move(
            role="dispatcher", actor="dispatcher", reference=card["ref"], target="validate", reason=""
        )
        self.assertEqual(result["task"]["state"], "validate")

    def test_close_releases_every_repository_and_unheld_projects_skip_sprint_board(self) -> None:
        """A project no open sprint holds is never looked up on the sprint board.

        Such a card is still refused, but by the admission rule (every Ready card needs its own
        open sprint), not by another sprint's hold — and that is what changes on close.
        """
        self.client.calls.clear()
        with self.assertRaises(TaskError) as unheld:
            self.tasks.create(role="po", actor="operator", project="unheld", task_type="code", title="normal")
        self.assertEqual(unheld.exception.code, "validation")
        self.assertFalse(
            any(
                method == "getProjectByName" and params.get("name") == "Secretary sprints"
                for method, params in self.client.calls
            )
        )

        with self.assertRaises(TaskError) as held:
            self.tasks.create(
                role="po", actor="operator", project="other", task_type="code", title="cross repo"
            )
        self.assertEqual(held.exception.code, "sprint_write_forbidden")

        self.sprints.close(role="po", actor="operator", reference=self.ref)

        with self.assertRaises(TaskError) as released:
            self.tasks.create(
                role="po", actor="operator", project="other", task_type="code", title="released"
            )
        self.assertEqual(released.exception.code, "validation")

    def test_unavailable_sprint_board_fails_closed(self) -> None:
        original = self.client.call

        def unavailable(method: str, **params: object) -> object:
            if method == "getTaskByReference" and params.get("reference") == self.ref:
                raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1)
            return original(method, **params)

        with mock.patch.object(self.client, "call", side_effect=unavailable):
            with self.assertRaisesRegex(TaskError, "cannot verify sprint") as raised:
                self.tasks.create(
                    role="po", actor="operator", project="secretary", task_type="code", title="blocked"
                )
        self.assertEqual(raised.exception.code, "sprint_guard_unavailable")

    def test_missing_index_bootstraps_from_live_open_sprints(self) -> None:
        (Path(self.tmp.name) / "sprints" / "active-repositories.json").unlink()
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.create(
                role="po", actor="operator", project="secretary", task_type="code", title="blocked"
            )
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")

    def test_pending_sprint_recovery_rebuilds_its_project_index(self) -> None:
        """A create that could not commit its audit is finished by its own request id."""
        create = dict(
            goal="recovered",
            repositories=["recovered"],
            reference="sprint:recovered",
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
            len(
                [
                    event
                    for event in TaskAudit(self.tmp.name).events()
                    if event["request_id"] == "recover-sprint-index"
                ]
            ),
            1,
        )
        # `restore_create` reproduces the row; the reservations arrive with the second
        # write recovery always makes, and that is the write the index follows.
        self.sprints.restore(
            reference="sprint:recovered",
            values={"sprint_reservations": json.dumps(["recovered"])},
            request_id="recover-sprint-reservations",
        )

        with self.assertRaisesRegex(TaskError, "sprint:recovered") as denied:
            self.tasks.create(
                role="po",
                actor="operator",
                project="recovered",
                task_type="code",
                title="blocked",
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
            reference="sprint:overlap",
            goal="overlap",
            repositories=["secretary"],
            request_id="seed-overlap-sprint",
        )["sprint"]["ref"]
        other = next(task for task in self.client.tasks if task["reference"] == other_ref)
        self.client.metadata[int(other["id"])]["sprint_reservations"] = json.dumps(["secretary"])
        # The second sprint's own head, bound to it: the write is about its card, not about the
        # sprint this fixture opened.
        with as_observer(other_ref):
            card = self.tasks.create(
                role="observer",
                actor="second-observer",
                project="secretary",
                task_type="code",
                title="second sprint",
                sprint=other_ref,
            )["task"]

        self.assertEqual(card["sprint"], other_ref)


class SprintReservedProjectGuardTests(unittest.TestCase):
    """The guards compare a card's project against reservations, not repository paths.

    A live sprint's `repositories` are filesystem paths and its `reservations` are project
    ids, so a fixture where the two lists differ is what tells the two key spaces apart.
    """

    def setUp(self) -> None:
        self.client = SprintKanboard()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sprints = SprintWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.ref = self.sprints.restore_create(
            reference="sprint:reserved",
            goal="reserved projects",
            repositories=["/home/dev/secretary"],
            request_id="seed-reserved-sprint",
        )["sprint"]["ref"]
        sprint = next(task for task in self.client.tasks if task["reference"] == self.ref)
        self.client.metadata[int(sprint["id"])]["sprint_reservations"] = json.dumps(["secretary"])
        refresh_active_sprint_projects(self.tmp.name, SprintReader(self.client))  # type: ignore[arg-type]
        bind_observer(self, self.ref)

    def _card(self, title: str = "owned") -> dict:
        return self.tasks.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title=title,
            sprint=self.ref,
        )["task"]

    def test_index_is_keyed_by_reserved_project(self) -> None:
        self.assertEqual(active_sprint_projects(self.tmp.name), {"secretary": [self.ref]})

    def test_a_stale_repository_keyed_index_is_rebuilt_before_it_answers(self) -> None:
        path = Path(self.tmp.name) / "sprints" / "active-repositories.json"
        path.write_text(
            json.dumps({"version": 1, "repositories": {"/home/dev/secretary": [self.ref]}}),
            encoding="utf-8",
        )
        self.assertEqual(active_sprint_projects(self.tmp.name), {})

        with self.assertRaises(TaskError) as denied:
            self.tasks.create(
                role="retro",
                actor="retro",
                project="secretary",
                task_type="research",
                title="finding",
                target="issues",
            )

        self.assertEqual(denied.exception.code, "sprint_write_forbidden")
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            {
                "version": 2,
                "projects": {"secretary": [self.ref]},
            },
        )

    def test_observer_moves_and_edits_a_card_of_its_reserved_project(self) -> None:
        card = self._card()

        moved = self.tasks.move(
            role="observer",
            actor="observer",
            reference=card["ref"],
            target="blocked",
            reason="waiting on review",
        )
        edited = self.tasks.edit(
            role="observer",
            actor="observer",
            reference=card["ref"],
            description="revised spec",
        )

        self.assertEqual(moved["task"]["state"], "blocked")
        self.assertEqual(edited["task"]["description"], "revised spec")

    def test_observer_may_not_move_a_card_of_an_unreserved_project(self) -> None:
        outside = self.tasks.create(
            role="retro",
            actor="retro",
            project="other",
            task_type="research",
            title="outside",
            target="issues",
        )["task"]

        with self.assertRaises(TaskError) as denied:
            self.tasks.move(
                role="observer",
                actor="observer",
                reference=outside["ref"],
                target="ready",
                reason="",
            )

        self.assertEqual(denied.exception.code, "role_forbidden")

    def test_the_reservation_guard_denies_an_unauthorized_write(self) -> None:
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.create(
                role="retro",
                actor="retro",
                project="secretary",
                task_type="research",
                title="finding",
                target="issues",
                request_id="retro-denied",
            )

        self.assertEqual(denied.exception.code, "sprint_write_forbidden")
        events = [
            event for event in TaskAudit(self.tmp.name).events() if event["kind"] == "sprint_guard_denied"
        ]
        self.assertEqual([event["payload"]["sprint"] for event in events], [self.ref])

    def test_a_project_no_sprint_reserves_is_unaffected(self) -> None:
        created = self.tasks.create(
            role="retro",
            actor="retro",
            project="other",
            task_type="research",
            title="finding",
            target="issues",
        )

        self.assertEqual(created["task"]["project"], "other")
        self.assertEqual(active_sprint_projects(self.tmp.name), {"secretary": [self.ref]})


class SprintCloseDecisionTests(SprintFixture):
    """A close decides every issue the sprint declared and every card it still holds."""

    def setUp(self) -> None:
        super().setUp()
        # A second open issue of the same product, so a close can decide two issues
        # differently and the refusal has more than one ref to be silent about.
        self.client._record(
            30,
            "issue:second",
            "Second issue",
            {
                "record_type": "issue",
                "issue_product": "secretary",
                "issue_kind": "bug",
                "issue_priority": "P1",
            },
        )
        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

    def _open(self, **kwargs) -> str:
        kwargs.setdefault("issues", ["issue:open", "issue:second"])
        return self._create(goal="decided close", **kwargs)["sprint"]["ref"]

    def _card(self, sprint: str, title: str, request_id: str) -> str:
        return self.tasks.create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title=title,
            target="ready",
            sprint=sprint,
            request_id=request_id,
        )["task"]["ref"]

    def _store(self):
        from secretary.product_issues import ProductIssueStore

        return ProductIssueStore(self.client, data_dir=self.tmp.name, instance=self.instance)

    def _writes(self, since: int) -> list[tuple[str, dict]]:
        return [
            (method, params)
            for method, params in self.client.calls[since:]
            if method
            in {
                "createTask",
                "updateTask",
                "saveTaskMetadata",
                "createComment",
                "closeTask",
                "moveTaskPosition",
            }
        ]

    def test_a_close_short_of_an_issue_verdict_refuses_and_writes_nothing(self) -> None:
        ref = self._open()
        before = len(self.client.calls)
        events = len(self._events())

        with self.assertRaises(TaskError) as raised:
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                decisions={"issues": [{"ref": "issue:open", "verdict": "resolved", "reason": "landed"}]},
            )

        self.assertEqual(raised.exception.code, "validation")
        self.assertIn("issue:second", raised.exception.message)
        self.assertNotIn("issue:open,", raised.exception.message)
        self.assertEqual(self._writes(before), [])
        self.assertEqual(len(self._events()), events)
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")  # type: ignore[arg-type]

    def test_a_verdict_for_an_issue_the_sprint_never_declared_is_refused(self) -> None:
        ref = self._open(issues=["issue:open"])
        before = len(self.client.calls)

        with self.assertRaisesRegex(TaskError, "did not declare"):
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                decisions={
                    "issues": [
                        {"ref": "issue:open", "verdict": "open", "reason": "unfinished"},
                        {"ref": "issue:second", "verdict": "resolved", "reason": "not this sprint's"},
                    ]
                },
            )

        self.assertEqual(self._writes(before), [])

    def test_a_closing_verdict_closes_the_issue_and_a_kept_one_stays_open_with_its_basis(self) -> None:
        ref = self._open()

        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="decided-close",
            decisions={
                "issues": [
                    {"ref": "issue:open", "verdict": "resolved", "reason": "the fix landed in this sprint"},
                    {"ref": "issue:second", "verdict": "open", "reason": "only half of it was reached"},
                ]
            },
        )

        store = self._store()
        closed = store.show_issue("issue:open")
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["close_reason"], "resolved")
        self.assertFalse(store.show_issue("issue:second")["closed"])
        # Closed with its reason where an operator reads issues, and gone from the open list.
        self.assertEqual(
            sorted(
                item["ref"]
                for item in store.list_issues(product="secretary", include_closed=True)
                if item["closed"]
            ),
            ["issue:done", "issue:open"],
        )
        self.assertNotIn("issue:open", [item["ref"] for item in store.list_issues(product="secretary")])
        self.assertEqual(result["closed_issues"], ["issue:open"])
        # The basis of the issue left open is on the close itself, which is where the audit
        # keeps it: nothing about the issue record says why the sprint let it stand.
        recorded = next(
            event for event in self._events() if event["kind"] == "closed" and event["ref"] == ref
        )
        self.assertEqual(
            recorded["payload"]["decisions"]["issues"],
            [
                {"ref": "issue:open", "verdict": "resolved", "reason": "the fix landed in this sprint"},
                {"ref": "issue:second", "verdict": "open", "reason": "only half of it was reached"},
            ],
        )

    def test_a_close_short_of_a_disposition_names_the_cards_and_their_states(self) -> None:
        ref = self._open(issues=["issue:open"])
        ready = self._card(ref, "still ready", "undisposed-ready")
        blocked = self._card(ref, "still blocked", "undisposed-blocked")
        with as_observer(ref):
            self.tasks.move(
                role="observer",
                actor="observer",
                reference=blocked,
                target="blocked",
                reason="waiting on an answer",
                request_id="undisposed-block",
            )
        before = len(self.client.calls)

        with self.assertRaises(TaskError) as raised:
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                decisions=KEEP_THE_ISSUE_OPEN,
            )

        self.assertEqual(raised.exception.code, "validation")
        self.assertIn(f"{ready} (ready)", raised.exception.message)
        self.assertIn(f"{blocked} (blocked)", raised.exception.message)
        self.assertEqual(self._writes(before), [])
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")  # type: ignore[arg-type]

    def test_dispositions_take_every_card_into_a_recorded_end(self) -> None:
        ref = self._open(issues=["issue:open"])
        landed = self._card(ref, "landed after all", "disposed-done")
        dropped = self._card(ref, "will not be done", "disposed-drop")

        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="disposing-close",
            decisions={
                "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
                "cards": [
                    {"ref": landed, "verdict": "done", "reason": "merged in the last hour of the sprint"},
                    {"ref": dropped, "verdict": "drop", "reason": "superseded by the next sprint's cut"},
                ],
            },
        )

        self.assertEqual(result["disposed_tasks"], sorted([landed, dropped]))
        # No card of the sprint is left in a working state on the closed contract.
        self.assertEqual(TaskReader(self.client).list(sprint=ref), [])  # type: ignore[arg-type]
        for reference in (landed, dropped):
            row = next(task for task in self.client.tasks if task["reference"] == reference)
            self.assertEqual(row["is_active"], 0)
        landed_row = next(task for task in self.client.tasks if task["reference"] == landed)
        self.assertIn(
            f"[po]\ndone when sprint {ref} closed: merged in the last hour of the sprint",
            [comment["comment"] for comment in self.client.comments[landed_row["id"]]],
        )
        self.assertIn(
            f"archived when sprint {ref} closed: merged in the last hour of the sprint",
            "\n".join(comment["comment"] for comment in self.client.comments[landed_row["id"]]),
        )

    def test_a_disposition_for_a_card_that_is_not_open_work_is_refused(self) -> None:
        ref = self._open(issues=["issue:open"])
        before = len(self.client.calls)

        with self.assertRaisesRegex(TaskError, "not open work of this sprint"):
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                decisions={
                    "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
                    "cards": [{"ref": "product:secretary", "verdict": "drop", "reason": "no"}],
                },
            )

        self.assertEqual(self._writes(before), [])

    def test_no_verdict_makes_a_product_or_issue_record_a_close_target(self) -> None:
        ref = self._open(issues=["issue:open"])
        # Even malformed metadata cannot enrol a typed record in the close.
        self.client.metadata[20]["sprint_ref"] = ref
        self.client.metadata[22]["sprint_ref"] = ref

        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="typed-records-close",
            decisions=KEEP_THE_ISSUE_OPEN,
        )

        self.assertEqual(result["disposed_tasks"], [])
        self.assertEqual(result["archived_tasks"], [])
        self.assertEqual(result["remaining_tasks"], [])
        for reference in ("product:secretary", "issue:open"):
            row = next(task for task in self.client.tasks if task["reference"] == reference)
            self.assertNotEqual(row.get("is_active", 1), 0)

    def test_an_interrupted_close_continues_without_repeating_what_it_did(self) -> None:
        ref = self._open()
        first = self._card(ref, "first drop", "retry-first")
        second = self._card(ref, "second drop", "retry-second")
        with as_observer(ref):
            self.tasks.move(
                role="observer",
                actor="observer",
                reference=first,
                target="blocked",
                reason="stuck",
                request_id="retry-block",
            )
        decisions = {
            "issues": [
                {"ref": "issue:open", "verdict": "resolved", "reason": "done by the sprint"},
                {"ref": "issue:second", "verdict": "open", "reason": "carried forward"},
            ],
            "cards": [
                {"ref": first, "verdict": "drop", "reason": "not finished"},
                {"ref": second, "verdict": "drop", "reason": "not started"},
            ],
        }
        real_archive = TaskWriter.archive

        def failing_archive(self_writer, **kwargs):
            if kwargs["reference"] == max(first, second):
                raise OSError("disk full")
            return real_archive(self_writer, **kwargs)

        before = len(self.client.calls)
        with mock.patch.object(TaskWriter, "archive", failing_archive):
            with self.assertRaisesRegex(TaskError, "pending repair"):
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="interrupted-close",
                    decisions=decisions,
                )

        # The interrupted run got as far as the issue and one of the two cards.
        self.assertTrue(self._store().show_issue("issue:open")["closed"])
        self.assertEqual(
            [
                task["reference"]
                for task in self.client.tasks
                if task["reference"] in {first, second} and int(task.get("is_active", 1) or 0) == 0
            ],
            [min(first, second)],
        )

        self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="interrupted-close",
            decisions=decisions,
        )

        closes = [
            event
            for event in self._events()
            if event.get("kind") == "issue.closed" and event.get("ref") == "issue:open"
        ]
        self.assertEqual(len(closes), 1)
        self.assertTrue(self._store().show_issue("issue:open")["closed"])
        rows = {
            reference: next(task for task in self.client.tasks if task["reference"] == reference)["id"]
            for reference in (first, second)
        }
        issue_row = next(task for task in self.client.tasks if task["reference"] == "issue:open")["id"]
        archived = [
            params["task_id"] for method, params in self.client.calls[before:] if method == "closeTask"
        ]
        # One archival per card and one close for the issue, however often the close is run.
        self.assertEqual(sorted(archived), sorted([issue_row, *rows.values()]))
        moved = [
            params["task_id"] for method, params in self.client.calls[before:] if method == "moveTaskPosition"
        ]
        # Only the blocked card needs a move at all, and the retry does not repeat it.
        self.assertEqual(moved, [rows[first]])
        self.assertEqual(TaskReader(self.client).list(sprint=ref), [])  # type: ignore[arg-type]
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})

    def test_no_successor_is_admitted_while_a_close_is_still_disposing_of_a_card(self) -> None:
        """A concurrent create waits for the whole terminal phase, not for part of it.

        The close publishes `closed` last, so a successor is already refused by the sprint's
        own reservation while the dispositions run.  The admission gate is what makes the
        concurrent attempt wait for the answer instead of racing the status it depends on:
        it is admitted after this close is complete, and then it is admitted cleanly.
        """
        ref = self._open(issues=["issue:open"])
        card = self._card(ref, "still open work", "raced-card")
        with as_observer(ref):
            self.tasks.move(
                role="observer",
                actor="observer",
                reference=card,
                target="blocked",
                reason="waiting on an answer",
                request_id="raced-block",
            )
        disposing = threading.Event()
        successor: dict[str, Any] = {}

        def open_successor() -> None:
            disposing.wait(5)
            try:
                successor["ref"] = self.writer.create(
                    role="po",
                    actor="operator",
                    goal="the successor sprint",
                    product="secretary",
                    issues=["issue:second"],
                    projects=["secretary"],
                    observer=head_choice("codex-observer"),
                    request_id="successor-create",
                )["sprint"]["ref"]
            except TaskError as exc:
                successor["error"] = exc

        thread = threading.Thread(target=open_successor)
        real_move = TaskWriter.move

        def racing_move(self_writer, **kwargs):
            disposing.set()
            # The successor create is trying to be admitted right here. It has to still be
            # trying when this disposition is written.
            thread.join(0.5)
            successor["waiting_at_the_disposition"] = thread.is_alive()
            return real_move(self_writer, **kwargs)

        thread.start()
        self.addCleanup(thread.join, 5)
        with mock.patch.object(TaskWriter, "move", racing_move):
            result = self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                request_id="raced-close",
                decisions=drop_cards(card),
            )
        thread.join(5)

        self.assertTrue(successor["waiting_at_the_disposition"])
        self.assertEqual(result["disposed_tasks"], [card])
        # The close finished the card it had, and only then was the successor admitted.
        self.assertEqual(TaskReader(self.client).list(sprint=ref), [])  # type: ignore[arg-type]
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        self.assertNotIn("error", successor)
        self.assertNotEqual(successor["ref"], ref)

    def test_an_issue_closed_from_elsewhere_leaves_the_close_recoverable(self) -> None:
        """A close never adopts somebody else's verdict as its own, and never walls up.

        `issue close` does not ask whether an open sprint declared the issue, so a close can
        find one of its verdicts already performed with another reason.  A `resolved` decision
        on an issue closed as `duplicate` is not this sprint's decision, so the preflight
        refuses it before the transaction and names what the issue carries; the closer
        confirms what actually happened, and that close goes through.
        """
        ref = self._open()
        card = self._card(ref, "still open work", "conflict-card")
        self._store().close_issue(
            reference="issue:second",
            reason="duplicate",
            actor="another-po",
            request_id="somebody-elses-close",
        )
        stated = {
            "issues": [
                {"ref": "issue:open", "verdict": "resolved", "reason": "the fix landed"},
                {"ref": "issue:second", "verdict": "resolved", "reason": "this one landed too"},
            ],
            "cards": [{"ref": card, "verdict": "drop", "reason": "not finished"}],
        }
        before = len(self.client.calls)

        with self.assertRaises(TaskError) as raised:
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                request_id="refused-close",
                decisions=stated,
            )

        self.assertEqual(raised.exception.code, "validation")
        self.assertIn("issue:second (duplicate)", raised.exception.message)
        self.assertIn("already_closed", raised.exception.message)
        self.assertEqual(self._writes(before), [])
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")  # type: ignore[arg-type]
        # Neither does a confirmation of something that did not happen go through.
        for wrong in ({"verdict": "already_closed", "actual": "wont_do"}, {"verdict": "open"}):
            with self.assertRaises(TaskError) as refused:
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    decisions={
                        "issues": [
                            {"ref": "issue:open", "verdict": "resolved", "reason": "the fix landed"},
                            {"ref": "issue:second", "reason": "somebody else got there", **wrong},
                        ],
                        "cards": list(stated["cards"]),
                    },
                )
            self.assertEqual(refused.exception.code, "validation")
        self.assertEqual(self._writes(before), [])

        confirmed = dict(stated)
        confirmed["issues"] = [
            {"ref": "issue:open", "verdict": "resolved", "reason": "the fix landed"},
            {
                "ref": "issue:second",
                "verdict": "already_closed",
                "actual": "duplicate",
                "reason": "another PO closed it as a duplicate while this sprint ran",
            },
        ]
        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="confirmed-close",
            decisions=confirmed,
        )

        # The confirmed issue is not closed again, and it keeps the reason it actually carries.
        self.assertEqual(result["closed_issues"], ["issue:open"])
        self.assertEqual(self._store().show_issue("issue:second")["close_reason"], "duplicate")
        self.assertEqual(
            [item for item in result["issue_decisions"] if item["ref"] == "issue:second"],
            [confirmed["issues"][1]],
        )
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        self.assertEqual(TaskReader(self.client).list(sprint=ref), [])  # type: ignore[arg-type]
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})

    def test_an_issue_closed_from_elsewhere_mid_close_stops_and_is_amended(self) -> None:
        """A conflict the preflight could not see stops the close and is answered by its retry.

        The close is already past its first verdict when somebody else closes the second issue.
        It does not finish on their reason and it does not become unresolvable: it records the
        conflict and refuses, the retry of the same request id may amend exactly that ref to
        the confirmation of what happened - and nothing else - and then it completes.
        """
        ref = self._open()
        real_close_issue = ProductIssueStore.close_issue

        def closing_the_other_issue_too(self_store, **kwargs):
            answer = real_close_issue(self_store, **kwargs)
            if kwargs["reference"] == "issue:open":
                real_close_issue(
                    self_store,
                    reference="issue:second",
                    reason="wont_do",
                    actor="another-po",
                    request_id="a-close-that-raced-this-one",
                )
            return answer

        stated = {
            "issues": [
                {"ref": "issue:open", "verdict": "resolved", "reason": "the fix landed"},
                {"ref": "issue:second", "verdict": "invalid", "reason": "it was never a bug"},
            ]
        }

        with mock.patch.object(ProductIssueStore, "close_issue", closing_the_other_issue_too):
            with self.assertRaises(TaskError) as raised:
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="raced-issue-close",
                    decisions=stated,
                )

        self.assertEqual(raised.exception.code, "close_conflict")
        self.assertIn("issue:second was closed as wont_do by somebody else", raised.exception.message)
        self.assertIn("already_closed", raised.exception.message)
        # The sprint is not closed on a verdict nobody stated, and the close is still there.
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")  # type: ignore[arg-type]
        self.assertEqual(self.writer.transactions.status()["pending"], 1)
        # The amendment is the only difference a retry may carry.
        with self.assertRaisesRegex(TaskError, "staged with other decisions"):
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                request_id="raced-issue-close",
                decisions={
                    "issues": [
                        {"ref": "issue:open", "verdict": "invalid", "reason": "restated"},
                        {
                            "ref": "issue:second",
                            "verdict": "already_closed",
                            "actual": "wont_do",
                            "reason": "another PO got there first",
                        },
                    ]
                },
            )
        with self.assertRaisesRegex(TaskError, "staged with other decisions"):
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                request_id="raced-issue-close",
                decisions={
                    "issues": [
                        {"ref": "issue:open", "verdict": "resolved", "reason": "the fix landed"},
                        {"ref": "issue:second", "verdict": "open", "reason": "leaving it open instead"},
                    ]
                },
            )

        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="raced-issue-close",
            decisions={
                "issues": [
                    {"ref": "issue:open", "verdict": "resolved", "reason": "the fix landed"},
                    {
                        "ref": "issue:second",
                        "verdict": "already_closed",
                        "actual": "wont_do",
                        "reason": "another PO got there first",
                    },
                ]
            },
        )

        self.assertEqual(result["closed_issues"], ["issue:open"])
        self.assertEqual(self._store().show_issue("issue:second")["close_reason"], "wont_do")
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})
        closes = [
            event
            for event in self._events()
            if event.get("kind") == "issue.closed" and event.get("ref") == "issue:open"
        ]
        self.assertEqual(len(closes), 1)

    def test_a_card_moved_by_somebody_else_stops_the_close_and_is_confirmed(self) -> None:
        """A card in the disposition's target state is not proof this close moved it there."""
        ref = self._open(issues=["issue:open"])
        card = self._card(ref, "still open work", "raced-move-card")
        with as_observer(ref):
            self.tasks.move(
                role="observer",
                actor="observer",
                reference=card,
                target="blocked",
                reason="waiting on an answer",
                request_id="raced-move-block",
            )
        stated = drop_cards(card)
        moved_by_somebody_else = TaskWriter.move
        move_id = _close_step_request_id("raced-card-close", "dispose-move", card)

        def move_it_first(self_writer, **kwargs):
            if kwargs.get("request_id") == move_id:
                with as_observer(ref):
                    moved_by_somebody_else(
                        self.tasks,
                        role="observer",
                        actor="observer",
                        reference=card,
                        target="ready",
                        reason="picked it back up",
                        request_id="somebody-elses-move",
                    )
                raise OSError("disk full")
            return moved_by_somebody_else(self_writer, **kwargs)

        with mock.patch.object(TaskWriter, "move", move_it_first):
            with self.assertRaisesRegex(TaskError, "pending repair"):
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="raced-card-close",
                    decisions=stated,
                )

        with self.assertRaises(TaskError) as raised:
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                request_id="raced-card-close",
                decisions=stated,
            )

        self.assertEqual(raised.exception.code, "close_conflict")
        self.assertIn(f"card {card} was moved to ready by somebody else", raised.exception.message)
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")  # type: ignore[arg-type]

        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="raced-card-close",
            decisions={
                "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
                "cards": [
                    {
                        "ref": card,
                        "verdict": "already_moved",
                        "actual": "ready",
                        "reason": "its own head put it back in Ready before the close got to it",
                    }
                ],
            },
        )

        self.assertEqual(result["disposed_tasks"], [card])
        self.assertEqual(TaskReader(self.client).list(sprint=ref), [])  # type: ignore[arg-type]
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})

    def test_a_step_whose_event_did_not_commit_is_repaired_not_assumed(self) -> None:
        """The reviewer's window: the backend effect landed, the journal write did not.

        The card is in the disposition's target state and the step is not done, because being
        in that state was never what made it done. The retry drives the same derived request id
        again, and the close does not finish while that id still has a pending event.
        """
        ref = self._open(issues=["issue:open"])
        card = self._card(ref, "still open work", "half-written-card")
        with as_observer(ref):
            self.tasks.move(
                role="observer",
                actor="observer",
                reference=card,
                target="blocked",
                reason="waiting on an answer",
                request_id="half-written-block",
            )
        move_id = _close_step_request_id("half-written-close", "dispose-move", card)
        audit = TaskAudit(self.tmp.name)
        real_append = TaskAudit.append

        def failing_append(self_audit, request_id, event):
            if request_id == move_id:
                raise OSError("disk full")
            return real_append(self_audit, request_id, event)

        with mock.patch.object(TaskAudit, "append", failing_append):
            with self.assertRaisesRegex(TaskError, "pending repair"):
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="half-written-close",
                    decisions=drop_cards(card),
                )

        # The backend effect landed and the event did not: exactly the window that used to be
        # read as "already in the target state, nothing to do".
        self.assertEqual(TaskReader(self.client).show(card)["state"], "ready")  # type: ignore[arg-type]
        self.assertIsNotNone(audit.pending_event(move_id))

        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="half-written-close",
            decisions=drop_cards(card),
        )

        self.assertEqual(result["disposed_tasks"], [card])
        self.assertIsNone(audit.pending_event(move_id))
        self.assertIsNotNone(audit.committed_event(move_id))
        self.assertEqual(TaskReader(self.client).list(sprint=ref), [])  # type: ignore[arg-type]
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})

    def test_no_step_of_a_close_ends_on_anything_but_its_own_committed_event(self) -> None:
        """One classification, three kinds of step: issue verdict, archive, disposition."""
        for step, request_id_of in (
            ("issue", lambda ref, card: _close_step_request_id(ref, "issue", "issue:open")),
            ("archive", lambda ref, card: _close_archive_request_id(ref, card[0])),
            ("dispose", lambda ref, card: _close_step_request_id(ref, "dispose-archive", card[1])),
        ):
            with self.subTest(step=step):
                self.setUp()
                sprint = self._open(issues=["issue:open"])
                done = self._card(sprint, "already done", f"{step}-done")
                self.tasks.claim(
                    role="dispatcher",
                    actor="dispatcher",
                    reference=done,
                    worker="worker",
                    request_id=f"{step}-claim",
                )
                for target in ("validate", "done"):
                    self.tasks.move(
                        role="dispatcher",
                        actor="dispatcher",
                        reference=done,
                        target=target,
                        reason="",
                        request_id=f"{step}-move-{target}",
                    )
                open_card = self._card(sprint, "still open work", f"{step}-open")
                decisions = {
                    "issues": [{"ref": "issue:open", "verdict": "resolved", "reason": "landed"}],
                    "cards": [{"ref": open_card, "verdict": "drop", "reason": "not finished"}],
                }
                step_id = request_id_of(f"{step}-audit-close", (done, open_card))
                audit = TaskAudit(self.tmp.name)
                real_append = TaskAudit.append

                def failing_append(self_audit, request_id, event, _id=step_id):
                    if request_id == _id:
                        raise OSError("disk full")
                    return real_append(self_audit, request_id, event)

                with mock.patch.object(TaskAudit, "append", failing_append):
                    with self.assertRaisesRegex(TaskError, "pending repair"):
                        self.writer.close(
                            role="po",
                            actor="operator",
                            reference=sprint,
                            request_id=f"{step}-audit-close",
                            decisions=decisions,
                        )

                self.assertIsNotNone(audit.pending_event(step_id))

                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=sprint,
                    request_id=f"{step}-audit-close",
                    decisions=decisions,
                )

                self.assertIsNone(audit.pending_event(step_id))
                self.assertIsNotNone(audit.committed_event(step_id))
                self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})

    def test_a_successor_is_refused_until_an_interrupted_close_is_finished(self) -> None:
        """An interrupted close keeps its sprint open, and an open sprint keeps its projects.

        The status is published last, so a close that fails on a disposition leaves the row
        open — and the reservation rule that has always refused a second sprint on a reserved
        project is what holds the successor off until the retry has finished the dispositions.
        """
        ref = self._open(issues=["issue:open"])
        card = self._card(ref, "still open work", "successor-card")
        with as_observer(ref):
            self.tasks.move(
                role="observer",
                actor="observer",
                reference=card,
                target="blocked",
                reason="waiting on an answer",
                request_id="successor-block",
            )

        def open_successor(request_id: str) -> dict:
            return self.writer.create(
                role="po",
                actor="operator",
                goal="the successor sprint",
                product="secretary",
                issues=["issue:second"],
                projects=["secretary"],
                observer=head_choice("codex-observer"),
                request_id=request_id,
            )

        with mock.patch.object(TaskWriter, "move", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "pending repair"):
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="unfinished-close",
                    decisions=drop_cards(card),
                )

        # Nothing was published: the sprint is still open, so it still holds its project.
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")  # type: ignore[arg-type]
        with self.assertRaises(TaskError) as raised:
            open_successor("successor-too-early")
        self.assertIn(ref, raised.exception.message)

        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="unfinished-close",
            decisions=drop_cards(card),
        )

        self.assertEqual(result["disposed_tasks"], [card])
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        self.assertEqual(TaskReader(self.client).list(sprint=ref), [])  # type: ignore[arg-type]
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})
        # And only now is the successor admitted on that project.
        self.assertNotEqual(open_successor("successor-after")["sprint"]["ref"], ref)

    def test_every_step_of_the_terminal_phase_is_retried_where_it_stopped(self) -> None:
        """Success, a failure at each step and the retry after each one, in one order."""
        failures = [
            ("issue", "close_issue", {"closed_issues": [], "status": "open"}),
            ("archive", "archive", {"archived_tasks": [], "status": "open"}),
            ("dispose", "move", {"disposed_tasks": [], "status": "open"}),
        ]
        from secretary.product_issues import ProductIssueStore

        for step, method, expected in failures:
            with self.subTest(step=step):
                self.setUp()
                ref = self._open(issues=["issue:open"])
                done = self._card(ref, "already done", f"{step}-done")
                self.tasks.claim(
                    role="dispatcher",
                    actor="dispatcher",
                    reference=done,
                    worker="worker",
                    request_id=f"{step}-claim",
                )
                for target in ("validate", "done"):
                    self.tasks.move(
                        role="dispatcher",
                        actor="dispatcher",
                        reference=done,
                        target=target,
                        reason="",
                        request_id=f"{step}-move-{target}",
                    )
                blocked = self._card(ref, "still open work", f"{step}-open")
                with as_observer(ref):
                    self.tasks.move(
                        role="observer",
                        actor="observer",
                        reference=blocked,
                        target="blocked",
                        reason="waiting",
                        request_id=f"{step}-block",
                    )
                decisions = {
                    "issues": [{"ref": "issue:open", "verdict": "resolved", "reason": "landed"}],
                    "cards": [{"ref": blocked, "verdict": "drop", "reason": "not finished"}],
                }
                target_class = ProductIssueStore if method == "close_issue" else TaskWriter
                with mock.patch.object(target_class, method, side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(TaskError, "pending repair"):
                        self.writer.close(
                            role="po",
                            actor="operator",
                            reference=ref,
                            request_id=f"{step}-close",
                            decisions=decisions,
                        )

                # Whatever failed, the sprint is still open and the transaction is still there.
                self.assertEqual(
                    SprintReader(self.client).show(ref, include_cards=False)["status"],  # type: ignore[arg-type]
                    expected["status"],
                )
                self.assertEqual(self.writer.transactions.status()["pending"], 1)

                result = self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id=f"{step}-close",
                    decisions=decisions,
                )

                self.assertEqual(result["closed_issues"], ["issue:open"])
                self.assertEqual(result["archived_tasks"], [done])
                self.assertEqual(result["disposed_tasks"], [blocked])
                self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
                self.assertEqual(TaskReader(self.client).list(sprint=ref), [])  # type: ignore[arg-type]
                self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})
                closes = [
                    event
                    for event in self._events()
                    if event.get("kind") == "issue.closed" and event.get("ref") == "issue:open"
                ]
                self.assertEqual(len(closes), 1)

    def test_a_retry_that_states_other_decisions_is_refused(self) -> None:
        ref = self._open(issues=["issue:open"])
        card = self._card(ref, "disposed once", "restated-card")
        decisions = {
            "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
            "cards": [{"ref": card, "verdict": "drop", "reason": "not finished"}],
        }
        with mock.patch.object(TaskWriter, "archive", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "pending repair"):
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="restated-close",
                    decisions=decisions,
                )

        with self.assertRaisesRegex(TaskError, "staged with other decisions") as raised:
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                request_id="restated-close",
                decisions={
                    "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
                    "cards": [{"ref": card, "verdict": "done", "reason": "landed after all"}],
                },
            )
        self.assertEqual(raised.exception.code, "validation")

        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="restated-close",
            decisions=decisions,
        )
        self.assertEqual(result["disposed_tasks"], [card])

    def test_cli_close_reads_its_decisions_from_a_file(self) -> None:
        ref = self._open(issues=["issue:open"])
        card = self._card(ref, "cli card", "cli-disposed")
        path = Path(self.tmp.name) / "decisions.yaml"
        path.write_text(
            "issues:\n"
            "  - ref: issue:open\n"
            "    verdict: wont_do\n"
            "    reason: the product moved on\n"
            "cards:\n"
            f"  - ref: {card}\n"
            "    verdict: drop\n"
            "    reason: nobody will pick this up\n",
            encoding="utf-8",
        )
        output, errors = io.StringIO(), io.StringIO()

        with (
            mock.patch("secretary.sprint_commands.KanboardClient.for_instance", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "sprint",
                    "close",
                    "--ref",
                    ref,
                    "--role",
                    "po",
                    "--actor",
                    "operator",
                    "--data-dir",
                    self.tmp.name,
                    "--instance",
                    str(self.instance),
                    "--decisions-file",
                    str(path),
                    "--request-id",
                    "cli-close",
                ]
            )

        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["closed_issues"], ["issue:open"])
        self.assertEqual(result["disposed_tasks"], [card])
        self.assertEqual(self._store().show_issue("issue:open")["close_reason"], "wont_do")

    def test_cli_close_without_the_file_refuses_before_it_writes(self) -> None:
        ref = self._open(issues=["issue:open"])
        output, errors = io.StringIO(), io.StringIO()

        with (
            mock.patch("secretary.sprint_commands.KanboardClient.for_instance", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "sprint",
                    "close",
                    "--ref",
                    ref,
                    "--role",
                    "po",
                    "--actor",
                    "operator",
                    "--data-dir",
                    self.tmp.name,
                    "--instance",
                    str(self.instance),
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "validation")
        self.assertIn("issue:open", json.loads(errors.getvalue())["error"]["message"])
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")  # type: ignore[arg-type]


class CloseDecisionFileTests(unittest.TestCase):
    """The decisions file is read strictly, and every refusal is a validation refusal."""

    def _refusal(self, text: str) -> TaskError:
        with self.assertRaises(TaskError) as raised:
            parse_close_decisions(text)
        self.assertEqual(raised.exception.code, "validation")
        return raised.exception

    def test_a_well_formed_file_normalizes_to_its_two_sections(self) -> None:
        parsed = parse_close_decisions(
            "issues:\n"
            "  - ref: issue:abc\n"
            "    verdict: duplicate\n"
            "    reason: 'the same as issue:def '\n"
            "cards:\n"
            "  - ref: secretary-1\n"
            "    verdict: done\n"
            "    reason: merged\n"
        )

        self.assertEqual(
            parsed,
            {
                "issues": [{"ref": "issue:abc", "verdict": "duplicate", "reason": "the same as issue:def"}],
                "cards": [{"ref": "secretary-1", "verdict": "done", "reason": "merged"}],
            },
        )

    def test_an_empty_file_decides_nothing_rather_than_failing_to_parse(self) -> None:
        self.assertEqual(parse_close_decisions(""), {"issues": [], "cards": []})

    def test_every_malformed_decision_is_refused_by_name(self) -> None:
        cases = [
            ("issues: [{ref: issue:a, verdict: nonsense, reason: why}]", "needs a verdict"),
            ("issues: [{ref: issue:a, verdict: resolved, reason: '  '}]", "non-empty reason"),
            ("issues: [{ref: issue:a, verdict: resolved}]", "non-empty reason"),
            ("issues: [{verdict: resolved, reason: why}]", "needs a ref"),
            (
                "cards: [{ref: c-1, verdict: drop, reason: a}, {ref: c-1, verdict: done, reason: b}]",
                "more than one decision",
            ),
            ("cards: [{ref: c-1, verdict: archived, reason: a}]", "needs a verdict"),
            ("cards: [{ref: c-1, verdict: drop, reason: a, decision: b}]", "unknown field"),
            ("verdicts: []", "unknown section"),
            ("- issue:a", "must be a mapping"),
            ("issues: 3", "must be a mapping"),
            ("issues: [\n", "not valid YAML"),
            # YAML types its scalars, so a key can be an integer sitting next to string ones.
            # That is a refusal by name, not a TypeError out of the parser.
            ("1: x\nunknown: x", "non-string key"),
            ("issues: [{1: x, unknown: x}]", "non-string key"),
            # A confirmation has to name the fact it confirms, and only a confirmation may.
            ("issues: [{ref: issue:a, verdict: already_closed, reason: why}]", "must name it in 'actual'"),
            (
                "issues: [{ref: issue:a, verdict: already_closed, reason: why, actual: ready}]",
                "must name it in 'actual'",
            ),
            (
                "cards: [{ref: c-1, verdict: already_moved, reason: why, actual: blocked}]",
                "must name it in 'actual'",
            ),
            (
                "issues: [{ref: issue:a, verdict: resolved, reason: why, actual: duplicate}]",
                "only a already_closed decision carries",
            ),
            (
                "cards: [{ref: c-1, verdict: drop, reason: why, actual: ready}]",
                "only a already_moved decision carries",
            ),
        ]
        for text, message in cases:
            with self.subTest(text=text):
                self.assertIn(message, self._refusal(text).message)


if __name__ == "__main__":
    unittest.main()
