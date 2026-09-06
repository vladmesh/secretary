"""The sprint half of the transport-independent layer: one create, two reads, and no second rule.

Hermetic in the same strong sense the other two suites are: no live Kanboard, no network, no
dispatcher tick and no real head. The board is the Product/Issue fake the sprint tests already use,
the head registry is an installed pair this file writes, and the dispatcher's production state is a
file. Everything the layer concludes it concludes from the evidence a real installation leaves.

Two properties are the ones that must fail rather than be believed, and they have their own cases:
a repeat of a request creates nothing (`IdempotencyTests`), and a role nobody pinned reaches the
entity as a field that was never written (`ExecutorPinTests`).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from secretary.config import validate
from secretary.sprint_observer import EXECUTOR_PINNED, EXECUTOR_UNSET, REVIEWER_FIELD, WORKER_FIELD
from secretary.sprints import SPRINT_BOARD_NAME
from secretary.webproto import sprint_reads as sprint_reads_module
from secretary.webproto.boundary import GUARDED, operations
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
from tests.fakes.sprints import ProductSprintKanboard
from tests.head_registry import write_installed_pair
from triggered_agents.runtime.head.identity import publish_heartbeat

OBSERVER_PROFILE = "codex-observer"
WORKER_PROFILE = "claude-worker"
REVIEWER_PROFILE = "codex-reviewer"

#: A registry of exactly three profiles, each pinning a different model and effort so that the
#: catalogue has something to reflect. `retired-observer` is deliberately not in it: it is what the
#: tests declare when they want a profile this installation does not have.
HEAD_SNAPSHOT = yaml.safe_dump(
    {
        "resources": {
            "claude-sub": {"account": "claude-subscription"},
            "openai-sub": {"account": "openai-subscription"},
        },
        "profiles": {
            OBSERVER_PROFILE: {
                "adapter": "codex",
                "resource": "openai-sub",
                "model": "gpt-5.6-terra",
                "effort": "high",
            },
            WORKER_PROFILE: {"adapter": "claude", "resource": "claude-sub", "model": "opus"},
            REVIEWER_PROFILE: {
                "adapter": "codex",
                "resource": "openai-sub",
                "model": "gpt-5.6-sol",
                "effort": "medium",
            },
        },
        "role_defaults": {
            "new_card": WORKER_PROFILE,
            "reviewer": REVIEWER_PROFILE,
            "observer": OBSERVER_PROFILE,
        },
    },
    sort_keys=False,
)


class SprintProtocolFixture(unittest.TestCase):
    """One instance, one Product/Issue board, one installed head registry, one data plane."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data_dir = self.tmp / "data"
        for relative in ("board", "dispatcher", "sprints"):
            (self.data_dir / relative).mkdir(parents=True)
        self.board = ProductSprintKanboard()
        self.instance = self._instance()
        self._production({})
        self.clock = 1788652800.0

    # -- fixture pieces ------------------------------------------------------------------------

    def _instance(self) -> Path:
        instance_dir = self.tmp / "instance"
        (instance_dir / "projects").mkdir(parents=True)
        (instance_dir / "instance.yaml").write_text(
            "version: 1\nname: test\n"
            f"data_dir: {self.data_dir}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        for project in ("secretary", "secretary-instance", "other"):
            repo = self.tmp / "repos" / project
            repo.mkdir(parents=True)
            (instance_dir / "projects" / f"{project}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "id": project,
                        "repo": str(repo),
                        "enabled": True,
                        "adapter": "secretary",
                        "default_branch": "main",
                    }
                ),
                encoding="utf-8",
            )
        write_installed_pair(instance_dir, HEAD_SNAPSHOT)
        return instance_dir

    def _production(self, observers: dict[str, Any]) -> None:
        (self.data_dir / "dispatcher" / "production-state.json").write_text(
            json.dumps({"phase": "production", "records": {}, "observers": observers}),
            encoding="utf-8",
        )

    # -- the layers under test -----------------------------------------------------------------

    def ops(self, **kwargs) -> SprintOperationLayer:
        options: dict[str, Any] = {
            "data_dir": self.data_dir,
            "board_client": self.board,
            "clock": lambda: self.clock,
        }
        options.update(kwargs)
        return SprintOperationLayer(self.instance, **options)

    def reads(self, **kwargs) -> SprintReadLayer:
        options: dict[str, Any] = {
            "data_dir": self.data_dir,
            "board_client": self.board,
            "clock": lambda: self.clock,
        }
        options.update(kwargs)
        return SprintReadLayer(self.instance, **options)

    def create(self, **kwargs) -> dict[str, Any]:
        """Open the fixture's sprint: its product, its open issue, one registered project."""
        request = {
            "request_id": "req-1",
            "actor": "operator",
            "product": "secretary",
            "goal": "Give webproto a sprint create",
            "definition_of_done": "the operation exists and is tested",
            "issues": ["issue:open"],
            "projects": ["secretary"],
            "observer": OBSERVER_PROFILE,
        }
        request.update(kwargs)
        return self.ops().sprint_create(**request)

    # -- what the board holds ------------------------------------------------------------------

    def sprint_rows(self) -> list[dict[str, Any]]:
        board = self.board.projects.get(SPRINT_BOARD_NAME)
        if board is None:
            return []
        return [
            task
            for task in self.board.tasks
            if task["project_id"] == board and str(task.get("reference") or "").startswith("sprint:")
        ]

    def metadata_of(self, reference: str) -> dict[str, str]:
        row = next(task for task in self.sprint_rows() if task["reference"] == reference)
        return self.board.metadata[int(row["id"])]

    def reference_of(self, document: dict[str, Any]) -> str:
        return str(document["sprint"]["ref"])


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
        """
        broken = mock.patch.object(
            SprintRequestStore,
            "record_reference",
            side_effect=RunStoreError("the request index is unwritable"),
        )
        with broken, self.assertRaises(OperationPending) as pending:
            self.create()
        self.assertEqual(pending.exception.code, "backend_unavailable")
        self.assertEqual(pending.exception.data["reason"], PENDING_REASON)
        action = pending.exception.data["action"]
        self.assertTrue(action["repeat_request"])
        self.assertEqual(action["request_id"], "req-1")
        self.assertEqual(action["operation"], "sprint_create")
        created = self.sprint_rows()
        self.assertEqual(len(created), 1)

        repeat = self.create()
        self.assertEqual(self.reference_of(repeat), str(created[0]["reference"]))
        self.assertEqual(len(self.sprint_rows()), 1)

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

    def test_the_sprint_modules_import_no_transport(self) -> None:
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
        written = [
            method
            for method, _params in self.board.calls[before:]
            if method
            in {"createTask", "updateTask", "saveTaskMetadata", "createProject", "createComment", "removeTask"}
        ]
        self.assertEqual(written, [])


if __name__ == "__main__":
    unittest.main()
