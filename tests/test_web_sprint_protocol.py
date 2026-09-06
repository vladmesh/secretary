"""The sprint half of the transport-independent layer: one create, two reads, and no second rule.

Hermetic in the same strong sense the other two suites are: no live Kanboard, no network, no
dispatcher tick and no real head. The board is the Product/Issue fake the sprint tests already use,
the head registry is an installed pair, and the dispatcher's production state is a file. Everything
the layer concludes it concludes from the evidence a real installation leaves. The installation
itself is built by `tests/webproto_sprint_fixtures.py`, which the transport suite drives the same
layer through, so the two cannot end up testing two different installations.

Two properties are the ones that must fail rather than be believed, and they have their own cases:
a repeat of a request creates nothing (`IdempotencyTests`), and a role nobody pinned reaches the
entity as a field that was never written (`ExecutorPinTests`).
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from secretary.cli import main
from secretary.config import validate
from secretary.sprint_observer import EXECUTOR_PINNED, EXECUTOR_UNSET, REVIEWER_FIELD, WORKER_FIELD
from secretary.sprints import SPRINT_BOARD_NAME
from secretary.tasks import TaskError
from secretary.webproto import sprint_reads as sprint_reads_module
from secretary.webproto import sprint_requests, store_io
from secretary.webproto.boundary import GUARDED, operations
from secretary.webproto.commands import _EXIT_BY_CODE
from secretary.webproto.errors import (
    OperationPending,
    OwnerConflict,
    ReadError,
    RuntimeUnavailable,
    TaskNotFound,
    ValidationRefused,
)
from secretary.webproto.runs import RunStoreError
from secretary.webproto.sprint_ops import PENDING_REASON, SprintOperationLayer
from secretary.webproto.sprint_reads import (
    OBSERVER_NOT_STARTED,
    OBSERVER_RUNNING,
    OBSERVER_UNAVAILABLE,
    SprintReadLayer,
)
from secretary.webproto.sprint_requests import SprintRequestStore
from tests.webproto_sprint_fixtures import (
    OBSERVER_PROFILE,
    REVIEWER_PROFILE,
    WORKER_PROFILE,
    SprintProtocolFixture,
)
from triggered_agents.runtime.head.identity import publish_heartbeat

#: What a call to the board writes with. Reads of this layer make none of these, and the sprint
#: board a fresh installation does not have is one of the things they do not create.
_WRITE_METHODS = {
    "createTask",
    "updateTask",
    "saveTaskMetadata",
    "createProject",
    "createComment",
    "removeTask",
}


class CreateTests(SprintProtocolFixture):
    def test_a_sprint_opens_with_what_it_was_opened_with(self) -> None:
        document = self.create()
        self.assertEqual(document["kind"], "sprint_created")
        self.assertTrue(document["created"])
        self.assertEqual(document["request_id"], "req-1")
        value = document["sprint"]["sprint"]["value"]
        self.assertEqual(value["goal"], "Give webproto a sprint create")
        self.assertEqual(value["definition_of_done"], "the operation exists and is tested")
        self.assertEqual(value["product"], "secretary")
        self.assertEqual(value["issues"], ["issue:open"])
        self.assertEqual(value["reservations"], ["secretary"])
        self.assertEqual(value["status"], "open")
        self.assertEqual(
            document["sprint"]["observer"]["declared"]["profile"], OBSERVER_PROFILE
        )

    def test_the_create_leaves_the_audit_and_the_reservation_index_the_writer_leaves(self) -> None:
        """The existing audit and reservations are kept because the existing writer keeps them."""
        from secretary.sprints import SPRINT_CREATED, active_sprint_projects
        from secretary.tasks import TaskAudit

        reference = self.reference_of(self.create())
        kinds = [str(event.get("kind") or "") for event in TaskAudit(self.data_dir).events()]
        self.assertIn(SPRINT_CREATED, kinds)
        self.assertEqual(active_sprint_projects(self.data_dir), {"secretary": [reference]})

    def test_a_sprint_on_a_project_another_open_sprint_holds_is_an_owner_conflict(self) -> None:
        self.create()
        with self.assertRaises(OwnerConflict) as refused:
            self.create(request_id="req-2", projects=["secretary"], issues=["issue:open"])
        self.assertIn("already reserved by an open sprint", str(refused.exception))
        self.assertEqual(len(self.sprint_rows()), 1)

    def test_an_unregistered_project_is_a_typed_refusal_that_writes_nothing(self) -> None:
        with self.assertRaises(ValidationRefused) as refused:
            self.create(projects=["not-a-project"])
        self.assertIn("unknown registered project", str(refused.exception))
        self.assertEqual(self.sprint_rows(), [])

    def test_a_closed_issue_and_a_foreign_issue_are_both_refused(self) -> None:
        for issues, expected in (
            (["issue:done"], "closed"),
            (["issue:foreign"], "belongs to product"),
        ):
            with self.subTest(issues=issues):
                with self.assertRaises(ValidationRefused) as refused:
                    self.create(request_id=f"req-{issues[0]}", issues=issues)
                self.assertIn(expected, str(refused.exception))
        self.assertEqual(self.sprint_rows(), [])

    def test_an_issue_the_board_does_not_hold_is_a_typed_not_found(self) -> None:
        with self.assertRaises(TaskNotFound):
            self.create(issues=["issue:missing"])
        self.assertEqual(self.sprint_rows(), [])

    def test_an_observer_profile_the_registry_does_not_have_is_refused(self) -> None:
        with self.assertRaises(ValidationRefused) as refused:
            self.create(observer="retired-observer")
        self.assertIn("head registry", str(refused.exception))
        self.assertEqual(self.sprint_rows(), [])

    def test_an_executor_profile_the_registry_does_not_have_is_refused_for_either_role(self) -> None:
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                with self.assertRaises(ValidationRefused) as refused:
                    self.create(request_id=f"req-{role}", **{role: "retired-observer"})
                self.assertIn(f"sprint {role} names head profile", str(refused.exception))
        self.assertEqual(self.sprint_rows(), [])

    def test_a_request_without_an_id_is_refused_before_anything_is_read(self) -> None:
        with self.assertRaises(ValidationRefused):
            self.create(request_id="")
        self.assertEqual(self.sprint_rows(), [])


class ExecutorPinTests(SprintProtocolFixture):
    """Criterion 2: the difference between "not pinned" and "pinned" survives the whole operation."""

    def test_a_sprint_opens_with_the_pins_it_was_given(self) -> None:
        document = self.create(worker=WORKER_PROFILE, reviewer=REVIEWER_PROFILE)
        executors = document["sprint"]["sprint"]["value"]["executors"]
        self.assertEqual(executors["worker"], {"state": EXECUTOR_PINNED, "profile": WORKER_PROFILE})
        self.assertEqual(
            executors["reviewer"], {"state": EXECUTOR_PINNED, "profile": REVIEWER_PROFILE}
        )
        stored = self.metadata_of(self.reference_of(document))
        self.assertEqual(stored[WORKER_FIELD], WORKER_PROFILE)
        self.assertEqual(stored[REVIEWER_FIELD], REVIEWER_PROFILE)

    def test_a_role_nobody_pinned_reaches_the_entity_as_no_field_at_all(self) -> None:
        """Not an empty string and not a `role_defaults` value: a field that was never written."""
        document = self.create(worker=WORKER_PROFILE)
        stored = self.metadata_of(self.reference_of(document))
        self.assertEqual(stored[WORKER_FIELD], WORKER_PROFILE)
        self.assertNotIn(REVIEWER_FIELD, stored)
        executors = document["sprint"]["sprint"]["value"]["executors"]
        self.assertEqual(executors["reviewer"], {"state": EXECUTOR_UNSET})

    def test_neither_the_empty_string_nor_none_is_a_way_to_pin_nothing(self) -> None:
        for spelling in ("", "none", " "):
            with self.subTest(spelling=spelling), self.assertRaises(ValidationRefused):
                self.create(request_id=f"req-{spelling!r}", worker=spelling)
        self.assertEqual(self.sprint_rows(), [])

    def test_pinning_a_role_is_a_different_request_from_not_pinning_it(self) -> None:
        """The two must not collide in the request fingerprint, or a retry would answer the other."""
        self.create(worker=WORKER_PROFILE)
        with self.assertRaises(ValidationRefused) as refused:
            self.create()
        self.assertIn("different inputs", str(refused.exception))


class IdempotencyTests(SprintProtocolFixture):
    """Criteria 3 and 4: a request id owns the sprint, and a half-done create is resumed."""

    def test_a_repeat_returns_the_same_sprint_and_creates_no_second_one(self) -> None:
        first = self.create()
        second = self.create()
        self.assertEqual(self.reference_of(second), self.reference_of(first))
        self.assertFalse(second["created"])
        self.assertEqual(len(self.sprint_rows()), 1)

    def test_a_repeat_calls_no_writer_at_all(self) -> None:
        """The shortcut is the point: a repeat cannot create a second observer if it never writes."""
        reference = self.reference_of(self.create())
        with mock.patch("secretary.sprints.SprintWriter.create") as never:
            repeat = self.create()
        never.assert_not_called()
        self.assertEqual(self.reference_of(repeat), reference)

    def test_the_same_id_over_different_inputs_is_refused_rather_than_answered(self) -> None:
        self.create()
        with self.assertRaises(ValidationRefused) as refused:
            self.create(goal="a different sprint entirely")
        self.assertIn("different inputs", str(refused.exception))
        self.assertEqual(len(self.sprint_rows()), 1)

    def test_a_repeat_after_a_partial_failure_uses_the_sprint_that_exists(self) -> None:
        """The entity is created and the step after it fails: the repeat must not open a second.

        The failing step here is this layer's own -- recording which sprint the request produced --
        which is exactly the window criterion 4 names. The repeat therefore arrives with a claimed
        request that names no sprint, hands the same id down to the writer, and gets back the
        sprint the first attempt already created rather than a new one.

        The failure is injected at the *filesystem*, not at `record_reference`. An earlier version
        of this test patched that method to raise `RunStoreError`, which is the exception the
        operation already catches, so by construction it could never see what the real write does:
        `write_text_atomic` raises a bare `RuntimeError`, which the boundary deliberately does not
        translate, and a full disk here escaped as that raw exception instead of the typed answer
        below. The seam this now goes through
        (:func:`secretary.webproto.store_io.write_document`) is what makes the two agree.
        """
        real = store_io.write_text_atomic

        def refuse_the_reference(path, payload):
            # The claim write carries no reference yet; the write that records which sprint this
            # request produced does. So this fails exactly the second one, with the message the
            # atomic writer really raises when the filesystem refuses it.
            if '"reference": "sprint:' in payload:
                raise RuntimeError(f"could not write export file {path}: [Errno 28] No space left on device")
            return real(path, payload)

        with (
            mock.patch.object(store_io, "write_text_atomic", refuse_the_reference),
            self.assertRaises(OperationPending) as pending,
        ):
            self.create()
        created = self.assert_pending_after_create(pending.exception, cause="No space left on device")

        repeat = self.create()
        self.assertEqual(self.reference_of(repeat), str(created["reference"]))
        self.assertEqual(len(self.sprint_rows()), 1)

    def test_a_lock_the_filesystem_refuses_after_the_row_exists_is_the_same_answer(self) -> None:
        """The second primitive of the same region, and the one that made this round happen.

        `_fsutil.file_lock` does `mkdir`, `open("a+")` and `flock`, and every one of them raises a
        bare `OSError`. The boundary turned that into `backend_unavailable` with no request id and
        no action, so a caller learned neither that a sprint already existed nor that repeating the
        same request was the safe move. It is not caught here by adding `OSError` to a list: the
        whole post-create region answers this way, which is why the third primitive to arrive in it
        cannot open a third hole.
        """
        real = sprint_requests.file_lock
        entries = []

        def refuse_the_second_lock(path):
            # Call one is `claim`, before the writer. Call two is `record_reference`, after the row
            # exists -- and it is the one a full disk would refuse while creating the lock file.
            entries.append(path)
            if len(entries) >= 2:
                raise OSError(28, "No space left on device")
            return real(path)

        with (
            mock.patch.object(sprint_requests, "file_lock", refuse_the_second_lock),
            self.assertRaises(OperationPending) as pending,
        ):
            self.create()
        self.assertEqual(len(entries), 2, "the failure must land on the post-create lock")
        created = self.assert_pending_after_create(pending.exception, cause="No space left on device")
        self.assertIsInstance(pending.exception.__cause__, OSError)

        repeat = self.create()
        self.assertEqual(self.reference_of(repeat), str(created["reference"]))
        self.assertEqual(len(self.sprint_rows()), 1)

    def test_any_failure_after_the_row_exists_is_that_answer_including_one_nobody_listed(self) -> None:
        """The region, not the vocabulary: an exception type no list names is answered the same.

        Deliberately a defect-shaped failure (`TypeError`) raised from the step *after* the request
        index -- building the document -- because that is the case a list of durable-source
        vocabularies would miss and the case that must not lose the durable fact. The cause is not
        swallowed: it is chained, so a traceback still names it.
        """
        with (
            mock.patch.object(
                SprintOperationLayer,
                "_document",
                side_effect=TypeError("a defect in the document builder"),
            ),
            self.assertRaises(OperationPending) as pending,
        ):
            self.create()
        self.assert_pending_after_create(pending.exception, cause="a defect in the document builder")
        self.assertIsInstance(pending.exception.__cause__, TypeError)

    def test_a_writer_that_stalls_mid_create_is_reported_as_repeatable_not_as_a_refusal(self) -> None:
        """A create that stopped between its row and its reference is not "it did not happen"."""
        real = self.board.call

        def refuse_the_reference(method: str, **params: object) -> object:
            if method == "updateTask" and "reference" in params:
                return False
            return real(method, **params)

        with (
            mock.patch.object(self.board, "call", refuse_the_reference),
            self.assertRaises(OperationPending) as pending,
        ):
            self.create()
        self.assertEqual(pending.exception.data["action"]["request_id"], "req-1")

        # The repeat resumes the same request and this installation ends with one sprint.
        repeat = self.create()
        self.assertEqual(len(self.sprint_rows()), 1)
        self.assertEqual(self.reference_of(repeat), str(self.sprint_rows()[0]["reference"]))

    def test_the_request_index_names_the_sprint_and_never_a_second_one(self) -> None:
        reference = self.reference_of(self.create())
        record = SprintRequestStore(self.data_dir).by_request("req-1")
        self.assertEqual(record.reference, reference)
        self.assertEqual(record.operation, "sprint_create")
        # A recorded reference is final: a second one could only come from a second sprint.
        SprintRequestStore(self.data_dir).record_reference("req-1", "sprint:999")
        self.assertEqual(SprintRequestStore(self.data_dir).by_request("req-1").reference, reference)


class OptionsTests(SprintProtocolFixture):
    """Criterion 5: what a sprint can be built from, read from the sources that own it."""

    def test_the_catalogue_offers_products_open_issues_projects_and_heads(self) -> None:
        options = self.reads().sprint_options()
        self.assertEqual(options["kind"], "sprint_options")
        self.assertEqual([item["id"] for item in options["products"]["items"]], ["other", "secretary"])
        refs = [item["ref"] for item in options["issues"]["items"]]
        self.assertEqual(refs, ["issue:foreign", "issue:open"])
        self.assertNotIn("issue:done", refs, "a closed issue is refused, so it is never offered")
        self.assertEqual(
            {item["ref"]: item["product"] for item in options["issues"]["items"]},
            {"issue:open": "secretary", "issue:foreign": "other"},
        )
        self.assertEqual(
            [item["id"] for item in options["projects"]["items"]],
            ["other", "secretary", "secretary-instance"],
        )

    def test_a_project_an_open_sprint_holds_is_marked_as_held(self) -> None:
        reference = self.reference_of(self.create())
        held = {
            item["id"]: item["reserved_by"] for item in self.reads().sprint_options()["projects"]["items"]
        }
        self.assertEqual(held["secretary"], [reference])
        self.assertEqual(held["other"], [])

    def test_the_profile_catalogue_is_the_installed_registry_and_not_a_constant(self) -> None:
        heads = self.reads().sprint_options()["heads"]
        by_id = {item["id"]: item for item in heads["items"]}
        self.assertEqual(sorted(by_id), sorted([WORKER_PROFILE, REVIEWER_PROFILE, OBSERVER_PROFILE]))
        self.assertEqual(by_id[OBSERVER_PROFILE]["model"], "gpt-5.6-terra")
        self.assertEqual(by_id[OBSERVER_PROFILE]["effort"], "high")
        self.assertEqual(by_id[WORKER_PROFILE]["model"], "opus")
        self.assertIsNone(by_id[WORKER_PROFILE]["effort"], "a profile that pins no effort says so")
        # Every entry can be chosen without knowing an identifier, and carries the identifier.
        self.assertEqual(by_id[REVIEWER_PROFILE]["label"], "codex · gpt-5.6-sol · medium effort")
        self.assertEqual(by_id[REVIEWER_PROFILE]["id"], REVIEWER_PROFILE)
        self.assertEqual(heads["observer"]["default"], OBSERVER_PROFILE)
        self.assertEqual(heads["role_defaults"]["reviewer"], REVIEWER_PROFILE)
        self.assertEqual(by_id[OBSERVER_PROFILE]["role_default_for"], ["observer"])

    def test_a_profile_the_registry_does_not_have_is_not_offered_and_is_not_creatable(self) -> None:
        """The catalogue and the refusal read the same file, so they cannot disagree."""
        offered = {item["id"] for item in self.reads().sprint_options()["heads"]["items"]}
        self.assertNotIn("retired-observer", offered)
        with self.assertRaises(ValidationRefused):
            self.create(observer="retired-observer")

    def test_every_offered_profile_is_marked_for_the_observer_role_it_may_take(self) -> None:
        items = self.reads().sprint_options()["heads"]["items"]
        self.assertTrue(all(item["observer"] for item in items))
        self.assertTrue(all(item["observer_reason"] is None for item in items))
        # And the mark is the create's own check, not a restatement: a profile the registry loses
        # is marked ineligible by the same call that refuses it.
        with mock.patch.object(
            sprint_reads_module, "installed_head_profiles", return_value={OBSERVER_PROFILE}
        ):
            marked = {
                item["id"]: item["observer"]
                for item in self.reads().sprint_options()["heads"]["items"]
            }
        self.assertEqual(marked, {OBSERVER_PROFILE: True, WORKER_PROFILE: False, REVIEWER_PROFILE: False})

    def test_an_unreadable_head_registry_blanks_its_own_section_only(self) -> None:
        (self.instance / "heads" / "heads.yaml").write_text("{", encoding="utf-8")
        options = self.reads().sprint_options()
        self.assertEqual(options["heads"]["source"]["state"], "unavailable")
        self.assertEqual(options["heads"]["items"], [])
        self.assertEqual(options["products"]["source"]["state"], "available")
        self.assertTrue(options["products"]["items"])


class SprintStateTests(SprintProtocolFixture):
    """Criterion 6: enough of one sprint to watch it, and three launch states told apart."""

    def _observer_record(self, reference: str, *, alive: bool) -> None:
        run_id = "obs-run-1"
        pid_file = self.data_dir / "dispatcher" / "observer.pid"
        publish_heartbeat(
            str(pid_file),
            {"run_id": run_id, "role": "observer", "task": f"sprint:{reference}"},
        )
        if not alive:
            # A record whose heartbeat cannot be read at all, and which is long past the grace
            # window a just-launched head gets: a head that is not there.
            pid_file.write_text("{}", encoding="utf-8")
        self._production(
            {
                reference: {
                    "sprint": reference,
                    "head": OBSERVER_PROFILE,
                    "state": "working",
                    "pid_file": str(pid_file),
                    "head_run": {"run_id": run_id},
                    "launched_at": 1.0,
                    "launches": 1,
                    "bound": True,
                }
            }
        )

    def test_a_saved_sprint_whose_observer_is_not_up_yet_says_so(self) -> None:
        reference = self.reference_of(self.create())
        document = self.reads().sprint_state(reference)
        self.assertEqual(document["kind"], "sprint")
        self.assertEqual(document["observer"]["launch"]["state"], OBSERVER_NOT_STARTED)
        self.assertIsNone(document["observer"]["launch"]["record"])
        self.assertEqual(document["observer"]["launch"]["source"]["state"], "available")
        self.assertEqual(document["observer"]["declared"]["profile"], OBSERVER_PROFILE)

    def test_an_observer_that_is_really_working_is_read_from_the_dispatcher_state(self) -> None:
        reference = self.reference_of(self.create())
        self._observer_record(reference, alive=True)
        launch = self.reads().sprint_state(reference)["observer"]["launch"]
        self.assertEqual(launch["state"], OBSERVER_RUNNING)
        self.assertEqual(launch["record"]["head"], OBSERVER_PROFILE)
        self.assertTrue(launch["record"]["alive"])

    def test_a_dispatcher_state_nobody_can_read_is_unavailable_and_never_not_started(self) -> None:
        reference = self.reference_of(self.create())
        (self.data_dir / "dispatcher" / "production-state.json").write_text("{", encoding="utf-8")
        document = self.reads().sprint_state(reference)
        launch = document["observer"]["launch"]
        self.assertEqual(launch["state"], OBSERVER_UNAVAILABLE)
        self.assertEqual(launch["source"]["state"], "unavailable")
        # The sprint's own fields are a different source and survive.
        self.assertEqual(document["sprint"]["source"]["state"], "available")
        self.assertEqual(document["sprint"]["value"]["goal"], "Give webproto a sprint create")

    def test_a_head_the_dispatcher_holds_that_is_not_alive_is_neither_running_nor_missing(self) -> None:
        reference = self.reference_of(self.create())
        self._observer_record(reference, alive=False)
        launch = self.reads().sprint_state(reference)["observer"]["launch"]
        self.assertEqual(launch["state"], sprint_reads_module.OBSERVER_STOPPED)
        self.assertFalse(launch["record"]["alive"])

    def test_a_watched_sprint_carries_its_goal_dod_links_pins_status_card_and_resume(self) -> None:
        reference = self.reference_of(self.create(worker=WORKER_PROFILE))
        value = self.reads().sprint_state(reference)["sprint"]["value"]
        self.assertEqual(value["goal"], "Give webproto a sprint create")
        self.assertEqual(value["definition_of_done"], "the operation exists and is tested")
        self.assertEqual(value["reservations"], ["secretary"])
        self.assertEqual(value["issues"], ["issue:open"])
        self.assertEqual(value["status"], "open")
        self.assertIsNone(value["current_task"])
        self.assertIsNone(value["resume"], "a sprint nobody has resumed carries no resume entry")
        self.assertEqual(value["executors"]["worker"]["profile"], WORKER_PROFILE)
        self.assertEqual(value["executors"]["reviewer"]["state"], EXECUTOR_UNSET)

    def test_the_last_resume_entry_of_a_sprint_is_on_the_page(self) -> None:
        from secretary.sprints import SprintWriter
        from tests.observer_identity import bind_observer

        reference = self.reference_of(self.create())
        bind_observer(self, reference)
        SprintWriter(
            self.board, data_dir=self.data_dir, instance=self.instance
        ).resume(
            role="observer",
            actor="observer",
            reference=reference,
            entry={
                "recorded_at": "2026-09-06T10:00:00Z",
                "selected_step": "the create operation",
                "selected_why": "the layer owes the transport a contract",
                "rejected_alternatives": "a second scheduler",
                "current_task": "secretary-1569",
                "dod_state": "DoD 4 in progress",
                "next_safe_step": "write the transport",
            },
        )
        resume = self.reads().sprint_state(reference)["sprint"]["value"]["resume"]
        self.assertEqual(resume["selected_step"], "the create operation")
        self.assertEqual(resume["next_safe_step"], "write the transport")

    def test_a_reference_no_sprint_holds_is_a_typed_not_found(self) -> None:
        self.create()
        with self.assertRaises(TaskNotFound):
            self.reads().sprint_state("sprint:404")

    def test_a_read_creates_no_sprint_board(self) -> None:
        """A read writes nothing, and the board this installation has never had is one of them."""
        with self.assertRaises(TaskNotFound):
            self.reads().sprint_state("sprint:1")
        self.assertNotIn(SPRINT_BOARD_NAME, self.board.projects)
        self.assertFalse(any(method == "createProject" for method, _ in self.board.calls))


class LayerPropertyTests(SprintProtocolFixture):
    """Criterion 7: the properties of the layer, checked rather than described."""

    def test_every_document_validates_against_the_published_schema(self) -> None:
        created = self.create(worker=WORKER_PROFILE)
        reference = self.reference_of(created)
        for document in (created, self.reads().sprint_state(reference), self.reads().sprint_options()):
            with self.subTest(kind=document["kind"]):
                self.assertEqual(validate(document, "web-sprint", document["kind"]), [])
                json.dumps(document)

    def test_the_post_create_region_is_one_place_with_one_exit(self) -> None:
        """The invariant, as structure rather than as a promise every future step must remember.

        Two rounds of this card fixed a post-create failure at the primitive that happened to raise
        it and watched the next primitive open the same hole. So what is checked here is the shape:
        `sprint_create` ends by handing the whole post-create region to `_after_create`, and that
        region is a single `try` with a single `except Exception` that raises the pending answer. A
        step added to it later is covered by being inside it, and there is no list to keep in step.
        """
        import ast

        source = (
            Path(__file__).resolve().parents[1] / "src" / "secretary" / "webproto" / "sprint_ops.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        layer = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "SprintOperationLayer"
        )
        methods = {node.name: node for node in layer.body if isinstance(node, ast.FunctionDef)}

        # `sprint_create` hands over and does nothing after the hand-over.
        last = methods["sprint_create"].body[-1]
        self.assertIsInstance(last, ast.Return)
        self.assertIsInstance(last.value, ast.Call)
        self.assertEqual(getattr(last.value.func, "attr", ""), "_after_create")

        # And the region has exactly one exit for every failure in it.
        body = [
            node
            for node in methods["_after_create"].body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        ]
        guarded = [node for node in body if isinstance(node, ast.Try)]
        self.assertEqual(len(guarded), 1)
        self.assertIs(guarded[0], body[-1], "nothing runs after the region")
        handlers = guarded[0].handlers
        self.assertEqual(len(handlers), 1)
        self.assertEqual(getattr(handlers[0].type, "id", ""), "Exception")
        raised = [node for node in ast.walk(handlers[0]) if isinstance(node, ast.Raise)]
        self.assertEqual(len(raised), 1)
        self.assertEqual(getattr(raised[0].exc.func, "id", ""), "OperationPending")
        # Chained, so the primitive that failed is still named in a traceback.
        self.assertIsNotNone(raised[0].cause)

    def test_the_sprint_modules_import_no_transport(self) -> None:
        """The promise as it is meant: this layer speaks no transport to its caller.

        Direct imports, deliberately. A transitive scan would be a different and false claim: the
        layer's access to the board is `KanboardClient`, the board is an HTTP service, and
        `secretary.tasks` has imported `urllib` since long before this card -- as `reads.py`,
        `admission.py`, `ops.py` and `run_events.py` all show. What the promise means, and what is
        checked here and in the refusal tests above, is that nothing of the transport reaches the
        caller: no HTTP, socket, framework or rendering in this layer's own surface, and failures
        that leave it are typed `webproto.errors` codes rather than status numbers.
        """
        import ast

        forbidden = frozenset(
            """
            http httpx requests urllib urllib3 socket socketserver ssl asyncio aiohttp flask
            fastapi starlette uvicorn django jinja2 tornado werkzeug wsgiref html cgi bottle sanic
            quart argparse sys
            """.split()
        )
        root = Path(__file__).resolve().parents[1] / "src" / "secretary" / "webproto"
        offenders: list[str] = []
        for name in ("sprint_ops.py", "sprint_reads.py", "sprint_requests.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"), filename=name)
            for node in ast.walk(tree):
                imported = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = [node.module]
                offenders.extend(
                    f"{name}: {value}" for value in imported if value.split(".")[0] in forbidden
                )
        self.assertEqual(offenders, [])

    def test_every_operation_of_both_layers_is_guarded_by_being_public(self) -> None:
        for layer in (SprintOperationLayer, SprintReadLayer):
            for name in operations(layer):
                with self.subTest(layer=layer.__name__, operation=name):
                    self.assertTrue(getattr(getattr(layer, name), GUARDED, False))

    def test_an_unreadable_source_becomes_a_protocol_code_and_never_escapes(self) -> None:
        """The boundary is what holds this, and it holds it for an operation added today."""
        reference = self.reference_of(self.create())
        with mock.patch.object(
            SprintRequestStore, "by_request", side_effect=RunStoreError("unreadable")
        ), self.assertRaises(ReadError) as refused:
            self.create(request_id="req-2")
        self.assertEqual(refused.exception.code, "backend_unavailable")
        with mock.patch(
            "secretary.webproto.sprint_reads.observer_snapshot", side_effect=RunStoreError("x")
        ), self.assertRaises(ReadError) as read_refused:
            self.reads().sprint_state(reference)
        self.assertEqual(read_refused.exception.code, "backend_unavailable")

    def test_a_refusal_with_nothing_to_add_carries_no_data(self) -> None:
        self.assertEqual(
            RuntimeUnavailable("plain").to_json(), {"code": "backend_unavailable", "message": "plain"}
        )
        self.assertEqual(
            OperationPending("held", data={"reason": PENDING_REASON}).to_json()["data"],
            {"reason": PENDING_REASON},
        )

    def test_the_reads_write_nothing_to_the_board(self) -> None:
        self.create()
        before = len(self.board.calls)
        self.reads().sprint_options()
        self.reads().sprint_state(self.reference_of(self.create()))
        self.reads().sprint_list()
        written = [
            method for method, _params in self.board.calls[before:] if method in _WRITE_METHODS
        ]
        self.assertEqual(written, [])




class SprintListTests(SprintProtocolFixture):
    """The listing: every sprint at once, and no sprint described as something it is not.

    Two defects of the live installation are pinned here as cases rather than as prose. Both were
    reproduced on the owner's board on 2026-09-06, on roughly sixty closed sprints: a finished
    sprint reported a `current_task` with nothing saying it was historical, and reported its
    observer as `not_started` -- a sprint that ended described as one waiting for its head to come
    up.
    """

    def _entry(self, document: dict, reference: str) -> dict:
        return next(item for item in document["sprints"]["items"] if item["ref"] == reference)

    def test_the_listing_answers_every_sprint_with_what_it_is_doing(self) -> None:
        open_sprint = self.reference_of(self.create())
        self.add_sprint_row("sprint:1001", status="closed", current_task="secretary-1435")

        document = self.reads().sprint_list()

        self.assertEqual(document["kind"], "sprint_list")
        self.assertEqual(validate(document, "web-sprint", document["kind"]), [])
        self.assertEqual(
            sorted(item["ref"] for item in document["sprints"]["items"]),
            [open_sprint, "sprint:1001"],
        )
        entry = self._entry(document, open_sprint)
        self.assertEqual(entry["goal"], "Give webproto a sprint create")
        self.assertEqual(entry["status"], "open")
        for section in ("current_task", "decision", "cards", "degraded_cards", "checks", "waiting"):
            self.assertIn("source", entry[section], f"{section} carries no availability")
        self.assertEqual(entry["observer"]["declared"]["profile"], OBSERVER_PROFILE)
        self.assertEqual(entry["observer"]["launch"]["state"], OBSERVER_NOT_STARTED)

    def test_the_status_filter_selects_and_an_unknown_status_is_refused(self) -> None:
        self.reference_of(self.create())
        self.add_sprint_row("sprint:1001", status="closed", current_task="secretary-1435")

        closed = self.reads().sprint_list(statuses=["closed"])
        self.assertEqual([item["ref"] for item in closed["sprints"]["items"]], ["sprint:1001"])
        self.assertEqual(closed["filter"]["statuses"], ["closed"])

        with self.assertRaises(ValidationRefused):
            self.reads().sprint_list(statuses=["retired"])

    def test_a_closed_sprint_is_never_presented_as_working(self) -> None:
        """Criterion 3, in the two shapes the live board has it in."""
        self.add_sprint_row("sprint:1001", status="closed", current_task="secretary-1435")
        self.add_sprint_row("sprint:1030", status="closed")

        document = self.reads().sprint_list()

        ended = self._entry(document, "sprint:1001")
        # The card is kept -- it is where the sprint got to -- and it is qualified.
        self.assertEqual(ended["current_task"]["ref"], "secretary-1435")
        self.assertFalse(ended["current_task"]["live"])
        self.assertIn("not work in progress", ended["current_task"]["reason"])
        self.assertEqual(ended["waiting"]["state"], sprint_reads_module.WAITING_ENDED)
        self.assertEqual(ended["checks"]["state"], sprint_reads_module.CHECKS_NOT_APPLICABLE)
        # The second defect: a sprint that ended is not one whose observer has not come up.
        self.assertEqual(ended["observer"]["launch"]["state"], sprint_reads_module.OBSERVER_ENDED)
        self.assertNotEqual(ended["observer"]["launch"]["state"], OBSERVER_NOT_STARTED)

        without = self._entry(document, "sprint:1030")
        self.assertIsNone(without["current_task"]["ref"])
        self.assertFalse(without["current_task"]["live"])
        self.assertEqual(without["observer"]["launch"]["state"], sprint_reads_module.OBSERVER_ENDED)

    def test_a_watched_closed_sprint_answers_the_way_the_listing_does(self) -> None:
        """Criterion 2: one question, one answer, whichever operation is asked."""
        self.add_sprint_row("sprint:1001", status="closed", current_task="secretary-1435")

        watched = self.reads().sprint_state("sprint:1001")
        listed = self._entry(self.reads().sprint_list(), "sprint:1001")

        self.assertEqual(validate(watched, "web-sprint", watched["kind"]), [])
        for section in ("current_task", "decision", "cards", "degraded_cards", "checks", "waiting"):
            self.assertEqual(watched["work"][section], listed[section], section)
        self.assertEqual(watched["observer"], listed["observer"])
        # And the sprint's own fields are still there beside the work.
        self.assertEqual(watched["sprint"]["value"]["current_task"], "secretary-1435")

    def test_the_checks_of_a_current_card_are_the_dispatchers_own_gate_record(self) -> None:
        reference = self.reference_of(self.create())
        card = self._card(reference)
        self._current_task(reference, card)
        self._production(
            {},
            {
                card: {
                    "state": "validate",
                    "gate_state": "green",
                    # The receipt's own field names: the candidate it is bound to, and the base it
                    # was validated against. Reporting the base as the attested candidate would name
                    # the wrong commit, which is why the test states both.
                    "gate_attestation": {"validated_sha": "abc123", "base_sha": "def456"},
                }
            },
        )

        checks = self._entry(self.reads().sprint_list(), reference)["checks"]

        self.assertEqual(checks["state"], sprint_reads_module.CHECKS_GREEN)
        self.assertEqual(checks["card"], card)
        self.assertEqual(checks["gate"]["attested_sha"], "abc123")
        self.assertEqual(checks["gate"]["base_sha"], "def456")
        self.assertIn("abc123", checks["reason"])

    def test_a_card_whose_gate_has_not_passed_is_not_green_and_says_why(self) -> None:
        reference = self.reference_of(self.create())
        card = self._card(reference)
        self._current_task(reference, card)
        self._production({}, {card: {"state": "rework", "gate_state": "", "gate_transport_error": ""}})

        entry = self._entry(self.reads().sprint_list(), reference)

        self.assertEqual(entry["checks"]["state"], sprint_reads_module.CHECKS_NOT_GREEN)
        self.assertIn("has not passed", entry["checks"]["reason"])
        self.assertEqual(entry["waiting"]["state"], sprint_reads_module.WAITING_WORKING)
        self.assertIn("rework", entry["waiting"]["reason"])

    def test_a_current_card_no_record_names_is_unknown_and_never_not_green(self) -> None:
        """The distinction criterion 2 is about: nobody said, which is not "it has not passed"."""
        reference = self.reference_of(self.create())
        card = self._card(reference)
        self._current_task(reference, card)

        entry = self._entry(self.reads().sprint_list(), reference)

        self.assertEqual(entry["checks"]["state"], sprint_reads_module.CHECKS_UNKNOWN)
        self.assertIsNone(entry["checks"]["gate"])
        self.assertIn("no record", entry["checks"]["reason"])
        self.assertEqual(entry["waiting"]["state"], sprint_reads_module.WAITING_WAITING)

    def test_an_unreadable_pipeline_board_marks_its_own_sections_and_no_others(self) -> None:
        reference = self.reference_of(self.create())
        del self.board.projects["Pipeline"]

        document = self.reads().sprint_list()
        entry = self._entry(document, reference)

        self.assertEqual(document["cards"]["source"]["state"], "unavailable")
        self.assertIsNone(entry["cards"]["states"], "an empty grouping would claim the sprint has none")
        self.assertEqual(entry["decision"]["freshness"]["source"]["state"], "unavailable")
        self.assertIsNone(entry["decision"]["freshness"]["value"])
        # The sprint's own fields, and the dispatcher's, are other sources and stand.
        self.assertEqual(document["sprints"]["source"]["state"], "available")
        self.assertEqual(entry["goal"], "Give webproto a sprint create")
        self.assertEqual(entry["current_task"]["source"]["state"], "available")
        self.assertEqual(entry["observer"]["launch"]["state"], OBSERVER_NOT_STARTED)

    def test_an_unreadable_production_state_marks_only_what_it_feeds(self) -> None:
        reference = self.reference_of(self.create())
        card = self._card(reference)
        self._current_task(reference, card)
        (self.data_dir / "dispatcher" / "production-state.json").write_text("{", encoding="utf-8")

        document = self.reads().sprint_list()
        entry = self._entry(document, reference)

        self.assertEqual(document["liveness"]["source"]["state"], "unavailable")
        self.assertEqual(entry["checks"]["state"], sprint_reads_module.CHECKS_UNKNOWN)
        self.assertEqual(entry["waiting"]["state"], sprint_reads_module.WAITING_UNKNOWN)
        self.assertIsNone(entry["degraded_cards"]["items"])
        self.assertEqual(entry["observer"]["launch"]["state"], OBSERVER_UNAVAILABLE)
        # And the board's answers are untouched.
        self.assertEqual(entry["current_task"]["ref"], card)
        self.assertEqual(entry["cards"]["source"]["state"], "available")

    def test_an_unreadable_sprint_board_marks_the_listing_and_still_says_the_rest(self) -> None:
        self.create()
        original = self.board.call

        def refuse(method: str, **params: Any) -> Any:
            if method == "getAllTasks" and params.get("project_id") == self.board.projects[SPRINT_BOARD_NAME]:
                raise TaskError("backend_error", "the sprint board is unavailable", 1)
            return original(method, **params)

        self.board.call = refuse  # type: ignore[method-assign]
        document = self.reads().sprint_list()

        self.assertEqual(document["sprints"]["source"]["state"], "unavailable")
        self.assertEqual(document["sprints"]["items"], [])
        self.assertEqual(document["cards"]["source"]["state"], "available")
        self.assertEqual(document["liveness"]["source"]["state"], "available")

    def test_the_listing_creates_no_board_and_starts_nothing(self) -> None:
        document = self.reads().sprint_list()

        self.assertEqual(document["sprints"]["items"], [])
        self.assertNotIn(SPRINT_BOARD_NAME, self.board.projects)
        self.assertFalse(
            [method for method, _ in self.board.calls if method in _WRITE_METHODS],
            "a read of the listing wrote to the board",
        )

    # -- fixture pieces ------------------------------------------------------------------------

    def _card(self, sprint: str) -> str:
        """One Pipeline card of this sprint, created the way the observer creates one."""
        from secretary.tasks import TaskWriter
        from tests.observer_identity import bind_observer

        bind_observer(self, sprint)
        return str(
            TaskWriter(self.board, data_dir=self.data_dir).create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="the current card",
                sprint=sprint,
            )["task"]["ref"]
        )

    def _current_task(self, sprint: str, card: str) -> None:
        from secretary.sprints import SprintWriter

        SprintWriter(self.board, data_dir=self.data_dir, instance=self.instance).set_current_task(
            role="observer", actor="observer", reference=sprint, task_reference=card
        )


class SprintReadCommandTests(SprintProtocolFixture):
    """`secretary sprint list` and `secretary sprint status` as clients of the two operations.

    Criterion 6: the commands keep being what an operator types, and stop being a second reader.
    What is checked here is exactly that -- the document the command prints is the document the
    operation returned, and a typed refusal reaches the exit status `secretary web-read` maps it to.
    """

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch(
                "secretary.webproto.sprint_reads.KanboardClient.for_instance",
                return_value=self.board,
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main([*argv, "--instance", str(self.instance), "--data-dir", str(self.data_dir)])
        return code, output.getvalue(), errors.getvalue()

    def test_sprint_list_prints_the_document_the_operation_answered(self) -> None:
        reference = self.reference_of(self.create())

        code, output, errors = self._run(["sprint", "list"])

        self.assertEqual(code, 0, errors)
        document = json.loads(output)
        self.assertEqual(document["kind"], "sprint_list")
        self.assertEqual([item["ref"] for item in document["sprints"]["items"]], [reference])

    def test_sprint_list_passes_its_filter_through_and_decides_nothing(self) -> None:
        self.create()
        self.add_sprint_row("sprint:1001", status="closed", current_task="secretary-1435")

        code, output, _errors = self._run(["sprint", "list", "--status", "closed"])

        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual([item["ref"] for item in document["sprints"]["items"]], ["sprint:1001"])
        self.assertFalse(document["sprints"]["items"][0]["current_task"]["live"])

    def test_sprint_status_prints_the_watched_sprint_document(self) -> None:
        reference = self.reference_of(self.create())

        code, output, errors = self._run(["sprint", "status", "--ref", reference])

        self.assertEqual(code, 0, errors)
        document = json.loads(output)
        self.assertEqual(document["kind"], "sprint")
        self.assertEqual(document["ref"], reference)
        self.assertIn("waiting", document["work"])
        self.assertEqual(document["observer"]["launch"]["state"], OBSERVER_NOT_STARTED)

    def test_the_command_prints_whatever_the_operation_returns(self) -> None:
        """Clientship, checked rather than described: no shaping of its own on the way out."""
        answered = {"kind": "sprint_list", "sprints": {"items": []}}
        with mock.patch.object(SprintReadLayer, "sprint_list", return_value=answered):
            code, output, _errors = self._run(["sprint", "list"])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), answered)

    def test_a_sprint_nobody_holds_is_the_exit_status_web_read_uses(self) -> None:
        self.create()

        code, output, errors = self._run(["sprint", "status", "--ref", "sprint:404"])

        self.assertEqual(code, _EXIT_BY_CODE["not_found"])
        self.assertEqual(output, "")
        self.assertEqual(json.loads(errors)["error"]["code"], "not_found")

    def test_an_installation_that_does_not_validate_is_the_backend_status(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["sprint", "list", "--instance", str(self.tmp / "not-an-instance")])

        self.assertEqual(code, _EXIT_BY_CODE["backend_unavailable"])
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")


if __name__ == "__main__":
    unittest.main()
