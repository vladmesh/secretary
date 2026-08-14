"""The sprint's declared observer: representation, reader and fence."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from secretary.dispatcher import DispatcherRuntime
from secretary.dispatcher_observer import (
    ObserverRecord,
    load_observers,
    observer_alive,
    observer_decision,
    put_observers,
)
from secretary.dispatcher_observer_fence import (
    REASON_DEAD,
    REASON_NOT_ADOPTED,
    REASON_NO_RECORD,
    fenced_task,
    observer_fence,
)
from secretary.sprint_observer import (
    ObserverMetadataError,
    REASON_HISTORICAL,
    REASON_MALFORMED,
    REASON_MISSING,
    REASON_UNKNOWN_PROFILE,
    encode_observer,
    executable_observer,
    head_choice,
    historical_recovered,
    historical_unknown,
    none_choice,
    observer_choice,
    parse_observer,
)
from secretary.dispatcher_production import _reconcile_production
from secretary.sprints import SprintWriter
from secretary.tasks import TaskAudit, TaskError, TaskReader, TaskWriter

from tests.test_dispatcher import (
    FakeCatalog,
    FakeHost,
    FakeKanboard,
    TwoOpenSprintAdmission,
)
from tests.test_dispatcher_observer import DEAD_PID, install_skill_registry
from tests.test_sprints import SprintFixture
from tests.sprint_close_fixtures import close_decisions


def mark_observer_heartbeat_dead(record: ObserverRecord) -> None:
    path = Path(record.pid_file)
    heartbeat = json.loads(path.read_text(encoding="utf-8"))
    heartbeat.update({
        "pid": DEAD_PID,
        "boot_id": "dead-process",
        "proc_starttime_ticks": "0",
    })
    path.write_text(json.dumps(heartbeat), encoding="utf-8")


class ObserverValueTests(unittest.TestCase):
    """Four tagged forms, and nothing that resembles one."""

    def test_the_four_forms_round_trip_byte_for_byte(self) -> None:
        for value in (
            head_choice("claude-observer"),
            none_choice(),
            historical_recovered("codex-observer", "evt_1"),
            historical_unknown(),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_observer(encode_observer(value)), value)

    def test_absent_null_empty_default_and_inherited_are_not_values(self) -> None:
        for raw in (
            None, "", "null", "{}", "default", "inherited",
            {"kind": "default"}, {"kind": "inherited"},
            {"kind": "head"}, {"kind": "head", "profile": ""},
            {"kind": "none", "profile": "codex-observer"},
            # An extra key is a shape nobody audited, not the form it resembles.
            {"kind": "head", "profile": "codex-observer", "note": "why"},
            {"kind": "historical", "profile": None, "source": "observer_lifecycle_audit"},
            {"kind": "historical", "profile": "codex-observer", "source": "migration_unknown"},
            {"kind": "historical", "profile": "x", "source": "observer_lifecycle_audit",
             "event_id": ""},
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_observer(raw))

    def test_a_historical_value_is_never_executable(self) -> None:
        for value in (historical_recovered("codex-observer", "evt_1"), historical_unknown()):
            with self.subTest(value=value):
                with self.assertRaises(ObserverMetadataError) as raised:
                    executable_observer({"ref": "sprint:1", "observer": value})
                self.assertEqual(raised.exception.reason, REASON_HISTORICAL)

    def test_missing_and_malformed_are_told_apart(self) -> None:
        with self.assertRaises(ObserverMetadataError) as missing:
            executable_observer({"ref": "sprint:1"})
        self.assertEqual(missing.exception.reason, REASON_MISSING)
        with self.assertRaises(ObserverMetadataError) as malformed:
            executable_observer({"ref": "sprint:1", "observer": None})
        self.assertEqual(malformed.exception.reason, REASON_MALFORMED)

    def test_the_operator_spells_none_or_a_profile(self) -> None:
        self.assertEqual(observer_choice("none"), none_choice())
        self.assertEqual(observer_choice(" claude-observer "), head_choice("claude-observer"))
        self.assertIsNone(observer_choice("  "))


class SprintDeclarationTests(SprintFixture):
    """Opening and reopening a sprint state its observer, or do not happen."""

    def _metadata(self, reference: str) -> dict:
        row = next(item for item in self.client.tasks if item["reference"] == reference)
        return self.client.metadata[int(row["id"])]

    def test_create_without_an_observer_is_refused_before_any_write(self) -> None:
        with self.assertRaisesRegex(TaskError, "requires an explicit observer"):
            self.writer.create(
                role="po", actor="operator", goal="no observer", product="secretary",
                issues=["issue:open"], projects=["secretary"],
            )

        self.assertEqual(TaskAudit(self.tmp.name).events(), [])

    def test_create_records_none_as_a_value_of_its_own(self) -> None:
        result = self._create(goal="unobserved", observer=none_choice())

        self.assertEqual(result["sprint"]["observer"], none_choice())
        self.assertEqual(
            self._metadata(result["sprint"]["ref"])["sprint_observer"],
            encode_observer(none_choice()),
        )

    def test_create_refuses_provenance_and_every_absent_spelling(self) -> None:
        for observer in (
            historical_unknown(),
            historical_recovered("codex-observer", "evt_1"),
        ):
            with self.subTest(observer=observer):
                with self.assertRaisesRegex(TaskError, "migration provenance"):
                    self._create(goal="provenance", observer=observer)
        for observer in ({"kind": "default"}, {"kind": "head", "profile": ""}, ""):
            with self.subTest(observer=observer):
                with self.assertRaises(TaskError):
                    self._create(goal="not a value", observer=observer)

    def test_create_refuses_a_head_the_registry_does_not_have(self) -> None:
        """A sprint may not be opened on a head that does not exist.

        The fence would stop its projects on the very first tick, and an operator would be reading
        a critical outcome about a sprint they had just opened instead of a validation error.
        """
        with self.assertRaisesRegex(TaskError, "not a profile of this installation"):
            self._create(goal="ghost head", observer=head_choice("retired-observer"))

        self.assertEqual(TaskAudit(self.tmp.name).events(), [])

    def test_reopen_refuses_a_head_the_registry_does_not_have(self) -> None:
        reference = self._create(goal="reopen onto a ghost")["sprint"]["ref"]
        self.writer.close(
            role="po", actor="operator", reference=reference, request_id="close",
            decisions=close_decisions(self.writer, reference),
        )

        with self.assertRaisesRegex(TaskError, "not a profile of this installation"):
            self.writer.reopen(
                role="po", actor="operator", reference=reference,
                observer=head_choice("retired-observer"), request_id="reopen",
            )

        self.assertEqual(
            self.writer.reader.show(reference, include_cards=False)["status"], "closed"
        )

    def test_an_unreadable_registry_refuses_rather_than_accepting_the_declaration(self) -> None:
        (self.instance / "heads" / "heads.yaml").write_text("profiles: []\n", encoding="utf-8")

        with self.assertRaisesRegex(TaskError, "head registry"):
            self._create(goal="no registry", observer=head_choice("codex-observer"))

    def test_none_needs_no_profile(self) -> None:
        (self.instance / "heads" / "heads.yaml").unlink()

        result = self._create(goal="unobserved", observer=none_choice())

        self.assertEqual(result["sprint"]["observer"], none_choice())

    def test_the_observer_lands_before_the_reference_publishes_the_row(self) -> None:
        self._create(goal="ordered", reference="sprint:ordered")

        order = [
            method for method, params in self.client.calls
            if (method == "saveTaskMetadata" and "sprint_observer" in dict(params["values"]))
            or (method == "updateTask" and params.get("reference") == "sprint:ordered")
        ]
        self.assertEqual(order[:2], ["saveTaskMetadata", "updateTask"])

    def test_reopen_requires_a_fresh_choice_and_never_inherits_the_closed_one(self) -> None:
        reference = self._create(goal="reopened", observer=head_choice("codex-observer"))["sprint"]["ref"]
        self.writer.close(
            role="po", actor="operator", reference=reference, request_id="close",
            decisions=close_decisions(self.writer, reference),
        )

        with self.assertRaisesRegex(TaskError, "requires an explicit observer"):
            self.writer.reopen(role="po", actor="operator", reference=reference)

        reopened = self.writer.reopen(
            role="po", actor="operator", reference=reference, observer=none_choice(),
            request_id="reopen",
        )
        self.assertEqual(reopened["sprint"]["observer"], none_choice())
        self.assertEqual(reopened["sprint"]["status"], "open")

    def test_reopen_writes_the_choice_while_the_sprint_is_still_closed(self) -> None:
        reference = self._create(goal="ordered reopen")["sprint"]["ref"]
        self.writer.close(
            role="po", actor="operator", reference=reference, request_id="close",
            decisions=close_decisions(self.writer, reference),
        )
        self.client.calls.clear()

        self.writer.reopen(
            role="po", actor="operator", reference=reference,
            observer=head_choice("claude-observer"), request_id="reopen",
        )

        written = [
            sorted(dict(params["values"]))
            for method, params in self.client.calls
            if method == "saveTaskMetadata"
            and {"sprint_observer", "sprint_status"} & set(dict(params["values"]))
        ]
        self.assertEqual(written, [["sprint_observer"], ["sprint_status"]])

    def test_a_reopen_repeated_with_another_observer_is_refused(self) -> None:
        reference = self._create(goal="one reopen")["sprint"]["ref"]
        self.writer.close(
            role="po", actor="operator", reference=reference, request_id="close",
            decisions=close_decisions(self.writer, reference),
        )
        first = self.writer.reopen(
            role="po", actor="operator", reference=reference,
            observer=head_choice("codex-observer"), request_id="reopen-once",
        )

        replay = self.writer.reopen(
            role="po", actor="operator", reference=reference,
            observer=head_choice("codex-observer"), request_id="reopen-once",
        )
        self.assertEqual(replay["event_id"], first["event_id"])

        with self.assertRaises(TaskError):
            self.writer.reopen(
                role="po", actor="operator", reference=reference,
                observer=none_choice(), request_id="reopen-once",
            )


class ObserverFenceFixture(unittest.TestCase):
    """A production runtime over one open sprint with a declared observer."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        install_skill_registry(self.data_dir)
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
                "SECRETARY_ROLE_SKILLS_MANIFEST": str(self.data_dir / "registry" / "manifest.toml"),
                "SECRETARY_INSTANCE": str(self.data_dir / "registry" / "instance"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        (self.data_dir / "bodies").mkdir(parents=True, exist_ok=True)
        self.board = FakeKanboard()
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.catalog.profiles["claude-observer"] = {
            "adapter": "claude", "model": "opus", "resource": "claude-sub",
        }
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.runtime = DispatcherRuntime(
            TaskReader(self.board),  # type: ignore[arg-type]
            TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir),  # type: ignore[arg-type]
            TaskAudit(self.data_dir),
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
        )

    def declare(self, observer, *, reference: str = "sprint:1", **metadata) -> None:
        values = {"sprint_reservations": '["secretary"]', **metadata}
        if observer is not None:
            values["sprint_observer"] = observer
        self.board.add_sprint(reference, status="open", **values)

    def fence(self) -> dict:
        return observer_fence(self.runtime, self.runtime.production_state.load())


class ObserverFenceTests(ObserverFenceFixture):
    def test_a_declared_none_passes_without_a_launch_or_a_probe(self) -> None:
        self.declare(encode_observer(none_choice()))

        result = self.runtime.production_tick()

        self.assertEqual(self.fence()["sprints"], set())
        self.assertEqual(self.host.observers, [])
        self.assertEqual(
            [action["action"] for action in result["actions"] if action["step"] == "observer-reconcile"],
            ["observer-none"],
        )

    def _tick_twice_with_an_active_card(self, declaration: str | None) -> list[dict]:
        """Two ticks over one in-progress card linked to the sprint. The second is the evidence.

        The first tick of any declared sprint fences on `observer_not_launched`, because the head
        it declares is brought up later in that same tick. The second tick is where a working
        declaration has an adopted head and a broken one still has none.
        """
        self.declare(declaration)
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.board.tasks[0]["column_id"] = 3
        self.runtime.production_tick()
        return self.runtime.production_tick()["actions"]

    def test_a_sprint_redeclared_as_none_gives_its_head_back(self) -> None:
        self.declare(encode_observer(head_choice("claude-observer")))
        self.runtime.production_tick()
        row = next(item for item in self.board.sprints if item["reference"] == "sprint:1")
        self.board.metadata[int(row["id"])]["sprint_observer"] = encode_observer(none_choice())

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in result["actions"] if action["step"] == "observer-reconcile"],
            ["observer-stopped"],
        )
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(load_observers(self.runtime.production_state.load()), {})
        self.assertEqual(self.fence()["sprints"], set())

    def test_a_corrupt_declaration_fences_before_any_card_moves(self) -> None:

        actions = self._tick_twice_with_an_active_card("{not json")

        fenced = [action for action in actions if action["step"] == "observer-fence"]
        self.assertEqual([action["observer_reason"] for action in fenced], [REASON_MALFORMED])
        self.assertEqual(fenced[0]["status"], "critical")
        # The active card of that sprint was not advanced, no head came up, and the card was not
        # moved on the board.
        self.assertEqual([action for action in actions if action["step"] == "advance"], [])
        self.assertEqual(self.host.observers, [])
        self.assertEqual([method for method, _ in self.board.calls if method == "moveTaskPosition"], [])

    def test_a_card_fenced_only_by_its_project_keeps_its_dispatcher_record(self) -> None:
        """Dropping out of the active set is not the same as leaving the cycle.

        The card here is not linked to the sprint at all: it sits in a project the sprint
        reserved, and it has left the active cycle. Reconciliation reads such a record as orphaned
        and settles its heads, which is exactly the mutation the fence exists to prevent.
        """
        self.declare("{not json")
        # An unlinked card of the reserved project, out of the cycle, with a live record.
        self.board.tasks[1]["column_id"] = 5
        payload = self.runtime.production_state.load()
        payload["records"] = {
            "secretary-510-neighbor": {
                "worker": "w1", "workspace": "/tmp/w1", "handle": "term_w", "head": "codex",
                "review_head": "codex-reviewer", "attempt_id": "att-1", "comment_baseline": 0,
                "review_baseline": 0, "state": "adopted", "claimed_at": time.time(),
            }
        }
        self.runtime.production_state.save(payload)

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in result["actions"] if action["step"] == "observer-fence"],
            ["observer-fenced"],
        )
        self.assertEqual([action for action in result["actions"] if action["step"] == "advance"], [])
        self.assertIn("secretary-510-neighbor", self.runtime.production_state.load()["records"])

    def test_the_same_card_advances_once_the_declared_observer_is_adopted(self) -> None:
        """The control for the fence: without it the pass above proves nothing."""

        actions = self._tick_twice_with_an_active_card(encode_observer(head_choice("claude-observer")))

        self.assertEqual(
            [action["action"] for action in actions if action["step"] == "observer-fence"],
            ["observer-fence-cleared"],
        )
        self.assertEqual(
            [action["action"] for action in actions if action["step"] == "advance"],
            ["waiting-worker-report"],
        )

    def test_a_missing_declaration_is_corruption(self) -> None:
        self.declare(None)

        fence = self.fence()

        self.assertEqual(fence["sprints"], {"sprint:1"})
        self.assertEqual(fence["outcomes"][0]["observer_reason"], REASON_MISSING)

    def test_an_unknown_profile_fences_and_never_falls_back_to_the_role_default(self) -> None:
        self.declare(encode_observer(head_choice("retired-observer")))

        result = self.runtime.production_tick()

        fenced = [action for action in result["actions"] if action["step"] == "observer-fence"]
        self.assertEqual([action["observer_reason"] for action in fenced], [REASON_UNKNOWN_PROFILE])
        self.assertEqual(self.host.observers, [])
        reconcile = [
            action for action in result["actions"] if action["step"] == "observer-reconcile"
        ]
        self.assertEqual([action["action"] for action in reconcile], ["observer-declaration-invalid"])

    def test_a_declared_head_launches_on_its_own_profile_and_clears_on_a_later_tick(self) -> None:
        self.declare(encode_observer(head_choice("claude-observer")))

        first = self.runtime.production_tick()
        raised = [action for action in first["actions"] if action["step"] == "observer-fence"]
        self.assertEqual([action["observer_reason"] for action in raised], [REASON_NO_RECORD])
        self.assertEqual(
            load_observers(self.runtime.production_state.load())["sprint:1"].head, "claude-observer"
        )

        second = self.runtime.production_tick()

        cleared = [action for action in second["actions"] if action["step"] == "observer-fence"]
        self.assertEqual([action["action"] for action in cleared], ["observer-fence-cleared"])
        self.assertEqual(self.fence()["sprints"], set())

    def test_a_dead_declared_head_fences_the_sprint_again(self) -> None:
        self.declare(encode_observer(head_choice("claude-observer")))
        self.runtime.production_tick()
        self.runtime.production_tick()
        record = load_observers(self.runtime.production_state.load())["sprint:1"]
        mark_observer_heartbeat_dead(record)

        fence = self.fence()

        self.assertEqual(fence["sprints"], {"sprint:1"})
        self.assertEqual(fence["outcomes"][0]["observer_reason"], REASON_DEAD)

    def test_fencing_is_project_local(self) -> None:
        self.declare(encode_observer(head_choice("claude-observer")))
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.board.metadata[13]["project"] = "other"

        fence = self.fence()

        self.assertEqual(fence["projects"], {"secretary"})
        self.assertTrue(fenced_task(fence, {"ref": "secretary-510-pilot", "project": "secretary"}))
        self.assertFalse(fenced_task(fence, {"ref": "secretary-510-neighbor", "project": "other"}))

    def test_a_fence_that_cannot_stage_its_outcome_stops_the_whole_tick(self) -> None:
        """An empty fence is not "nothing may be decided", it is "everything may move".

        The staging write is a separate path from the production-state guard, so a full volume or
        a permissions change can take the audit while the state stays writable. The tick has to end
        there rather than fall back to a fence that permits every card.
        """
        self.declare("{not json")
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.board.tasks[0]["column_id"] = 3

        with mock.patch(
            "secretary.dispatcher_observer_fence.stage_event", side_effect=OSError("disk full"),
        ):
            result = self.runtime.production_tick()

        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["action"], "observer-fence-unavailable")
        self.assertEqual(result["actions"], [])
        self.assertIn("disk full", result["reason"])
        self.assertEqual(result["errors"][0]["code"], "unexpected_error")
        # Nothing ran after the fence: no advance, no claim, no observer launch, no board move.
        self.assertEqual([method for method, _ in self.board.calls if method == "moveTaskPosition"], [])
        self.assertEqual(self.host.observers, [])
        telemetry = self.runtime.production_state.load()["tick_telemetry"]
        self.assertFalse(telemetry["last"]["healthy"])

    def test_one_failed_card_list_does_not_let_reconciliation_settle_a_fenced_record(self) -> None:
        """Backend reads fail independently, so the fence's own read can fail and the rest recover.

        An empty ref set there would hand reconciliation a fenced project's record as an orphan and
        it would stop its heads and remove it — the exact mutation the fence exists to prevent.
        """
        self.declare("{not json")
        # An unlinked card of the reserved project, out of the active cycle, with a live record:
        # what reconciliation settles when nothing tells it the card is fenced.
        self.board.tasks[1]["column_id"] = 5
        payload = self.runtime.production_state.load()
        payload["records"] = {
            "secretary-510-neighbor": {
                "worker": "w1", "workspace": "/tmp/w1", "handle": "term_w", "head": "codex",
                "review_head": "codex-reviewer", "attempt_id": "att-1", "comment_baseline": 0,
                "review_baseline": 0, "state": "adopted", "claimed_at": time.time(),
            }
        }
        self.runtime.production_state.save(payload)
        real_list = self.runtime.reader.list
        failures = [TaskError("backend_error", "transient", 1)]

        def list_once_broken(*args, **kwargs):
            if failures:
                raise failures.pop()
            return real_list(*args, **kwargs)

        with mock.patch.object(self.runtime.reader, "list", side_effect=list_once_broken):
            result = self.runtime.production_tick()

        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["action"], "observer-fence-unavailable")
        self.assertIn(
            "secretary-510-neighbor", self.runtime.production_state.load()["records"]
        )
        self.assertEqual(
            [action for action in result["actions"] if action.get("step") == "production-reconcile"],
            [],
        )

    def test_reconciliation_classifies_the_card_it_reads_against_the_fence(self) -> None:
        """The second line: a card absent from the fence's inventory is still fenced by its sprint."""
        self.declare("{not json")
        self.board.tasks[1]["column_id"] = 5
        self.board.metadata[13]["sprint_ref"] = "sprint:1"
        payload = self.runtime.production_state.load()
        payload["records"] = {
            "secretary-510-neighbor": {
                "worker": "w1", "workspace": "/tmp/w1", "handle": "term_w", "head": "codex",
                "review_head": "codex-reviewer", "attempt_id": "att-1", "comment_baseline": 0,
                "review_baseline": 0, "state": "adopted", "claimed_at": time.time(),
            }
        }
        self.runtime.production_state.save(payload)
        fence = observer_fence(self.runtime, self.runtime.production_state.load())
        fence["refs"] = set()  # the inventory misses it; the sprint link still holds

        outcomes = _reconcile_production(
            self.runtime, self.runtime.production_state.records(payload), payload,
            set(), fenced_refs=set(), fence=fence,
        )

        self.assertEqual(outcomes, [])

    def test_a_launched_head_that_has_not_written_its_pid_keeps_the_fence_up(self) -> None:
        """A pid that is merely not written yet is not confirmed adoption.

        The lifecycle grace window reads it as alive so a head that has just started is not
        relaunched. Releasing another role's cards on that is different: the terminal may have
        died before it ever reached the observer prompt.
        """
        self.declare(encode_observer(head_choice("claude-observer")))
        self.runtime.production_tick()
        record = load_observers(self.runtime.production_state.load())["sprint:1"]
        Path(record.pid_file).unlink()
        self.assertEqual(
            observer_alive(record), {"alive": True, "reason": "pid-not-written-yet", "pid_known": False}
        )

        fence = self.fence()

        self.assertEqual(fence["sprints"], {"sprint:1"})
        self.assertEqual(fence["outcomes"][0]["observer_reason"], REASON_NOT_ADOPTED)

    def test_a_record_that_names_no_head_is_a_mismatch(self) -> None:
        self.declare(encode_observer(head_choice("claude-observer")))
        payload = self.runtime.production_state.load()
        put_observers(payload, {
            "sprint:1": ObserverRecord(
                sprint="sprint:1", head="", handle="term_x", state="running",
                launches=1, launched_at=time.time(),
            )
        })
        self.runtime.production_state.save(payload)

        fence = observer_fence(self.runtime, self.runtime.production_state.load())

        self.assertEqual(fence["outcomes"][0]["observer_reason"], "observer_head_mismatch")

    def test_the_card_of_an_unadopted_observer_does_not_advance(self) -> None:
        actions = self._tick_twice_with_an_active_card(
            encode_observer(head_choice("claude-observer"))
        )
        self.assertEqual(
            [action["action"] for action in actions if action["step"] == "advance"],
            ["waiting-worker-report"],
        )
        record = load_observers(self.runtime.production_state.load())["sprint:1"]
        Path(record.pid_file).unlink()

        blind = self.runtime.production_tick()["actions"]

        self.assertEqual([action for action in blind if action["step"] == "advance"], [])
        self.assertEqual(
            [action["observer_reason"] for action in blind if action["step"] == "observer-fence"],
            [REASON_NOT_ADOPTED],
        )

    def test_a_fenced_sprint_makes_the_tick_report_unhealthy(self) -> None:
        """A stopped project with a healthy-looking tick is how the last outage stayed invisible."""
        self.declare(encode_observer(head_choice("retired-observer")))

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "degraded")
        telemetry = self.runtime.production_state.load()["tick_telemetry"]
        self.assertFalse(telemetry["last"]["healthy"])
        self.assertIn(
            "sprint:1", [row["ref"] for row in telemetry["last"]["degradations"]],
        )

    def test_a_fence_writes_its_reason_durably_once_per_reason(self) -> None:
        self.declare(encode_observer(head_choice("retired-observer")))

        self.runtime.production_tick()
        self.runtime.production_tick()

        raised = [
            event for event in self.runtime.audit.events("sprint:1")
            if event["kind"] == "observer_fence_raised"
        ]
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0]["outcome"], "critical")
        self.assertEqual(raised[0]["payload"]["observer_reason"], REASON_UNKNOWN_PROFILE)

    def test_an_unreadable_sprint_board_fences_what_it_last_saw(self) -> None:
        """The Pipeline board can answer while the sprint board cannot: fail closed, not open."""
        self.declare(encode_observer(head_choice("claude-observer")))
        payload = self.runtime.production_state.load()
        observer_fence(self.runtime, payload)  # one sighted pass, to take the snapshot
        self.runtime.production_state.save(payload)
        self.board.metadata[13]["project"] = "other"

        with mock.patch.object(
            self.runtime.sprints, "list", side_effect=TaskError("backend_error", "down", 1)
        ):
            fence = self.fence()

        self.assertEqual(fence["sprints"], {"sprint:1"})
        self.assertEqual(fence["projects"], {"secretary"})
        self.assertEqual(fence["outcomes"][0]["action"], "sprint_board_unavailable")
        self.assertEqual(fence["outcomes"][0]["status"], "critical")
        # Project-local even when blind: the reserved project stops, another project does not.
        self.assertTrue(fenced_task(fence, {"ref": "secretary-510-pilot", "project": "secretary"}))
        self.assertFalse(fenced_task(fence, {"ref": "secretary-510-neighbor", "project": "other"}))

    def test_a_blind_tick_still_fences_a_sprint_it_never_saw(self) -> None:
        """A sprint opened since the last snapshot is caught through its cards' own link."""
        self.declare(encode_observer(head_choice("claude-observer")))
        self.board.metadata[12]["sprint_ref"] = "sprint:1"

        with mock.patch.object(
            self.runtime.sprints, "list", side_effect=TaskError("backend_error", "down", 1)
        ):
            fence = self.fence()

        self.assertIn("secretary-510-pilot", fence["refs"])
        self.assertTrue(fenced_task(fence, {"ref": "secretary-510-pilot", "project": "secretary"}))
        self.assertFalse(fenced_task(fence, {"ref": "secretary-510-neighbor", "project": "other"}))

    def test_an_unreadable_sprint_board_does_not_advance_the_sprints_cards(self) -> None:
        actions = self._tick_twice_with_an_active_card(
            encode_observer(head_choice("claude-observer"))
        )
        self.assertEqual(
            [action["action"] for action in actions if action["step"] == "advance"],
            ["waiting-worker-report"],
        )

        with mock.patch.object(
            self.runtime.sprints, "list", side_effect=TaskError("backend_error", "down", 1)
        ):
            blind = self.runtime.production_tick()["actions"]

        self.assertEqual([action for action in blind if action["step"] == "advance"], [])
        self.assertEqual(
            [action["action"] for action in blind if action["step"] == "observer-fence"],
            ["sprint_board_unavailable"],
        )

    def test_the_decision_never_reads_the_role_default(self) -> None:
        self.declare(encode_observer(head_choice("claude-observer")))
        sprint = self.runtime.sprints.show("sprint:1", include_cards=False)
        self.catalog.role_defaults["observer"] = "codex-observer"

        self.assertEqual(observer_decision(self.runtime, sprint)["head"], "claude-observer")


class ObserverRecordFenceStateTests(ObserverFenceFixture):
    def test_a_stale_fence_of_a_closed_sprint_is_dropped(self) -> None:
        self.declare(encode_observer(head_choice("claude-observer")))
        payload = self.runtime.production_state.load()
        observer_fence(self.runtime, payload)
        self.assertIn("sprint:1", payload["observer_fence"])
        self.board.metadata[100]["sprint_status"] = "closed"

        outcomes = observer_fence(self.runtime, payload)["outcomes"]

        self.assertEqual([outcome["action"] for outcome in outcomes], ["observer-fence-cleared"])
        self.assertNotIn("observer_fence", payload)

    def test_a_head_that_is_not_the_declared_one_fences(self) -> None:
        self.declare(encode_observer(head_choice("claude-observer")))
        payload = self.runtime.production_state.load()
        put_observers(payload, {
            "sprint:1": ObserverRecord(
                sprint="sprint:1", head="codex-observer", handle="term_x",
                state="running", launches=1, launched_at=time.time(),
            )
        })
        self.runtime.production_state.save(payload)

        fence = observer_fence(self.runtime, self.runtime.production_state.load())

        self.assertEqual(fence["outcomes"][0]["observer_reason"], "observer_head_mismatch")


class TwoOpenSprintFenceTests(ObserverFenceFixture, TwoOpenSprintAdmission):
    """The fence with two sprints open at once: it holds one sprint's work, not the tick's.

    The pair is opened through `SprintWriter.create` under the pilot setting, so the rows the
    fence reads are rows admission produced. A broken declaration is then written over the
    persisted value of an already-open sprint, which is how a live installation reaches one: no
    create would admit it.
    """

    HELD = "the sprint holding this project has no working declared observer"

    def open_pair(self, *, observer=None, second_observer=None) -> None:
        self.sprint_writer = self.admit_two_open_sprints(
            observer=observer or head_choice("claude-observer"),
            second_observer=second_observer,
        )
        self.link_pair_cards()

    def in_flight_pair(self) -> None:
        """The pair with its head adopted and one card of each sprint in flight.

        The first tick fences `sprint:1` on its unlaunched head, so the card claimed there is
        the other sprint's; the second tick, with the head adopted, claims `sprint:1`'s own.
        """
        self.open_pair()
        self.assertEqual(self._claim(self.runtime.production_tick()), "secretary-510-neighbor")
        self.assertEqual(self._claim(self.runtime.production_tick()), "secretary-510-pilot")

    def _fence_steps(self, result: dict) -> list[dict]:
        return [action for action in result["actions"] if action["step"] == "observer-fence"]

    def _claim(self, result: dict) -> str:
        claims = [action for action in result["actions"] if action["step"] == "claim"]
        return claims[0]["pilot_ref"] if claims else ""

    def _skipped(self, result: dict) -> list[dict]:
        for action in result["actions"]:
            if action.get("step") in {"claim", "production-claim"}:
                return list(action.get("skipped_ready") or [])
        return []

    def assert_only_the_first_sprint_is_held(self, result: dict, reason: str) -> None:
        """Its cards neither advance nor are claimed; the other sprint's do both."""
        self.assertEqual(
            [(action["sprint"], action["observer_reason"]) for action in self._fence_steps(result)],
            [(self.FIRST, reason)],
        )
        self.assertEqual(
            [action["pilot_ref"] for action in result["actions"] if action["step"] == "advance"],
            ["secretary-510-neighbor"],
        )
        self.assertEqual(self._claim(result), "third-1")
        self.assertEqual(self._skipped(result), [{"ref": "fourth-1", "reason": self.HELD}])
        self.assertEqual(self.runtime.reader.show("fourth-1")["state"], "ready")
        self.assertEqual(self.runtime.reader.show("third-1")["state"], "in_progress")
        self.assertEqual(self.runtime.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_a_dead_declared_head_holds_its_own_sprint_and_leaves_the_other_running(self) -> None:
        self.in_flight_pair()
        record = load_observers(self.runtime.production_state.load())[self.FIRST]
        mark_observer_heartbeat_dead(record)

        # Read before the tick: the tick relaunches the dead head, so this is the state the
        # cards are judged against while it is still dead.
        fence = self.fence()
        result = self.runtime.production_tick()

        self.assert_only_the_first_sprint_is_held(result, REASON_DEAD)
        self.assertEqual(fence["sprints"], {self.FIRST})
        self.assertEqual(fence["projects"], {"secretary", "fourth"})
        self.assertEqual(fence["refs"], {"secretary-510-pilot", "fourth-1"})
        self.assertTrue(fenced_task(
            fence, {"ref": "secretary-510-pilot", "sprint": self.FIRST, "project": "secretary"},
        ))
        self.assertFalse(fenced_task(
            fence, {"ref": "secretary-510-neighbor", "sprint": self.SECOND, "project": "other"},
        ))

    def test_an_unresolvable_profile_holds_its_own_sprint_only(self) -> None:
        """The declared head left the registry after the sprint was opened on it."""
        self.in_flight_pair()
        self.rewrite_observer(self.FIRST, encode_observer(head_choice("retired-observer")))

        result = self.runtime.production_tick()

        self.assert_only_the_first_sprint_is_held(result, REASON_UNKNOWN_PROFILE)
        self.assertEqual(
            [
                action["action"] for action in result["actions"]
                if action["step"] == "observer-reconcile" and action.get("sprint") == self.FIRST
            ],
            ["observer-declaration-invalid"],
        )

    def test_a_corrupt_declaration_holds_its_own_sprint_only(self) -> None:
        self.in_flight_pair()
        self.rewrite_observer(self.FIRST, "{not json")

        result = self.runtime.production_tick()

        self.assert_only_the_first_sprint_is_held(result, REASON_MALFORMED)

    def test_the_fence_state_and_its_durable_events_name_the_fenced_sprint_only(self) -> None:
        self.in_flight_pair()
        self.rewrite_observer(self.FIRST, "{not json")

        self.runtime.production_tick()

        payload = self.runtime.production_state.load()
        self.assertEqual(set(payload["observer_fence"]), {self.FIRST})
        self.assertEqual(payload["observer_fence"][self.FIRST]["reason"], REASON_MALFORMED)
        raised = [
            event for event in self.runtime.audit.events()
            if event["kind"] == "observer_fence_raised"
        ]
        # One sprint, however many reasons it has been fenced for: the first tick fenced it on
        # its unlaunched head, this one on the declaration that was broken since.
        self.assertEqual({event["ref"] for event in raised}, {self.FIRST})
        self.assertEqual(raised[-1]["payload"]["observer_reason"], REASON_MALFORMED)

    def test_the_snapshot_holds_each_open_sprints_own_reservations(self) -> None:
        """Keyed by sprint, so a blind pass fences each sprint on what that sprint holds.

        Every open sprint is in it, fenced or not: the snapshot is what a pass that cannot
        read the sprint board falls back on, and a pass that could not check any declaration
        fences all of them. What names the fenced sprint alone is the fence state above.
        """
        self.open_pair()
        self.rewrite_observer(self.FIRST, "{not json")

        self.runtime.production_tick()

        self.assertEqual(
            self.runtime.production_state.load()["observer_fence_snapshot"],
            {self.FIRST: ["fourth", "secretary"], self.SECOND: ["other", "third"]},
        )

    def test_an_unreadable_sprint_board_fences_both_sprints_by_their_own_reservations(self) -> None:
        """The blind path is installation-wide by design, and still project-local per sprint."""
        self.open_pair()
        payload = self.runtime.production_state.load()
        observer_fence(self.runtime, payload)  # one sighted pass, to take the snapshot
        self.runtime.production_state.save(payload)

        with mock.patch.object(
            self.runtime.sprints, "list", side_effect=TaskError("backend_error", "down", 1)
        ):
            fence = self.fence()

        self.assertEqual(fence["sprints"], {self.FIRST, self.SECOND})
        self.assertEqual(fence["projects"], {"secretary", "fourth", "other", "third"})
        self.assertEqual(fence["outcomes"][0]["action"], "sprint_board_unavailable")
        # The blind outcome names the sprints, which is what an operator has to act on. It
        # used to name the fenced cards there, because the linked-card index is keyed by card.
        self.assertEqual(fence["outcomes"][0]["sprints"], [self.FIRST, self.SECOND])
        self.assertFalse(fenced_task(fence, {"ref": "loose-1", "sprint": "", "project": "loose"}))

    def _orphan_records(self) -> None:
        """Both cards out of the active cycle with a live record behind them."""
        self.board.tasks[0]["column_id"] = 5
        self.board.tasks[1]["column_id"] = 5
        payload = self.runtime.production_state.load()
        payload["records"] = {
            reference: {
                "worker": "w", "workspace": "/tmp/w", "handle": "term_" + reference, "head": "codex",
                "review_head": "codex-reviewer", "attempt_id": "att-" + reference,
                "comment_baseline": 0, "review_baseline": 0, "state": "adopted",
                "claimed_at": time.time(),
            }
            for reference in ("secretary-510-pilot", "secretary-510-neighbor")
        }
        self.runtime.production_state.save(payload)

    def test_reconciliation_settles_the_unfenced_sprints_record_and_holds_the_fenced_one(self) -> None:
        """Both records are orphaned by the same tick; only the fenced sprint's survives it."""
        self.open_pair()
        self.rewrite_observer(self.FIRST, "{not json")
        self._orphan_records()

        result = self.runtime.production_tick()

        reconciled = [
            action for action in result["actions"] if action["step"] == "production-reconcile"
        ]
        self.assertEqual(
            [(action["ref"], action["action"]) for action in reconciled],
            [("secretary-510-neighbor", "record-removed")],
        )
        records = set(self.runtime.production_state.load()["records"])
        self.assertIn("secretary-510-pilot", records)
        self.assertNotIn("secretary-510-neighbor", records)

    def test_the_same_pair_with_no_fence_settles_both_records(self) -> None:
        """The control: without the fence the pass above would have removed both."""
        self.open_pair(observer=none_choice())
        self._orphan_records()

        result = self.runtime.production_tick()

        self.assertEqual(
            sorted(
                action["ref"] for action in result["actions"]
                if action["step"] == "production-reconcile"
            ),
            ["secretary-510-neighbor", "secretary-510-pilot"],
        )
        records = set(self.runtime.production_state.load()["records"])
        self.assertNotIn("secretary-510-pilot", records)
        self.assertNotIn("secretary-510-neighbor", records)

    def test_one_sprints_broken_head_does_not_fence_the_other_sprints_head(self) -> None:
        """Both sprints declaring a head: the fence is still one sprint's, not the pair's.

        The scenario admission used to refuse. Each sprint is judged on its own declaration and
        its own record, so the sprint whose head is fine keeps its projects and its cards.
        """
        self.open_pair(second_observer=head_choice("codex-observer"))
        self.runtime.production_tick()  # both heads launched and adopted
        self.rewrite_observer(self.FIRST, "{not json")

        fence = self.fence()

        self.assertEqual(fence["sprints"], {self.FIRST})
        self.assertEqual(fence["projects"], {"secretary", "fourth"})
        self.assertEqual(fence["refs"], {"secretary-510-pilot", "fourth-1"})
        self.assertEqual(fence["outcomes"][0]["observer_reason"], REASON_MALFORMED)
        self.assertFalse(fenced_task(
            fence, {"ref": "secretary-510-neighbor", "sprint": self.SECOND, "project": "other"},
        ))
        self.assertFalse(fenced_task(
            fence, {"ref": "third-1", "sprint": self.SECOND, "project": "third"},
        ))
        # The second sprint's head is not what the fence was about, and it is still alive.
        self.assertTrue(observer_alive(load_observers(self.runtime.production_state.load())[
            self.SECOND
        ])["alive"])


if __name__ == "__main__":
    unittest.main()
