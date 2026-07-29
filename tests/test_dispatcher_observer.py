"""Observer-head lifecycle: the production tick against the sprint board (secretary-793)."""

from __future__ import annotations

import os
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from secretary import dispatcher as dispatcher_module
from secretary.dispatcher import (
    CommandHostRuntime,
    CutoverState,
    DispatcherRuntime,
    InstanceCatalog,
)
from secretary.dispatcher_launcher import HeadLaunch
from secretary.dispatcher_tui import TuiDeliveryError
from secretary.dispatcher_observer import (
    EVENT_DEFERRED,
    EVENT_LAUNCHED,
    EVENT_RELAUNCHED,
    EVENT_STOPPED,
    OBSERVER_HEAD_FALLBACK,
    ObserverLaunchAborted,
    ObserverRecord,
    load_observers,
    observer_pid_file,
    observer_queue_finished,
    put_observers,
    observer_request_id,
    render_observer_prompt,
    stop_observer_head,
)
from secretary.sprints import SprintReader
from secretary.dispatcher_types import HostError
from secretary.dispatcher_production import _budget_event_type, _production_claim_ready, _reconcile_sprint_budget
from secretary.dispatcher_watchdog import initial_output_stall_seconds
from secretary.head_health import HeadReadiness
from secretary.head_registry import canonical_heads
from secretary.role_env import ROLE_ALLOWLIST, ROLE_REQUIRED, runtime_env
from secretary.status import _observers as status_observers
from secretary.tasks import TaskAudit, TaskError, TaskReader, TaskWriter

from tests.test_dispatcher import FakeCatalog, FakeHost, FakeKanboard, FakeLegacyPause


# A pid that is real but not this process: `kill(pid, 0)` raises, so the watchdog reads the head as
# dead. Pid 2 is the kernel's kthreadd on Linux, hence a live pid nobody can be launched as; 999999
# is above the default pid_max and is reliably free.
DEAD_PID = 999999


def install_skill_registry(root: Path, *, delivered: bool = True) -> Path:
    """A role-skill registry of this test's own, pointed at by SECRETARY_ROLE_SKILLS_MANIFEST.

    The launch gate reads the shell's skill directory, and the shells of the live installation are
    not a fixture: a test that let the tick look at them would pass or fail on whether somebody had
    run `role-skills sync` on this machine. The empty instance beside it does the same job for the
    other half of the registry: the overlay of the live installation is not a fixture either.
    Returns the observer skill's path in the fake shell, which `delivered=False` leaves absent.
    """
    manifest = root / "registry" / "manifest.toml"
    shell_root = root / "registry" / "codex-shell"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        '[roles.observer]\nskills = ["observe-sprint"]\n\n'
        '[targets.codex-test]\nshell = "codex"\n'
        f'root = "{shell_root}"\nroles = ["observer"]\n',
        encoding="utf-8",
    )
    (root / "registry" / "instance").mkdir(parents=True, exist_ok=True)
    skill = shell_root / "observe-sprint" / "SKILL.md"
    if delivered:
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("# canonical observer skill\n", encoding="utf-8")
    return skill


class ObserverLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        self.observer_skill = install_skill_registry(self.data_dir)
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
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.audit = TaskAudit(self.data_dir)
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            self.audit,
            CutoverState(self.data_dir),
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
            legacy_pause=FakeLegacyPause(),  # type: ignore[arg-type]
        )
        self.runtime.state.save({
            "version": 1,
            "phase": "cutover_committed",
            "pilot_ref": "secretary-510-pilot",
            "old_owner_paused": True,
            "records": {},
        })

    # helpers -----------------------------------------------------------------

    def open_sprint(self, reference: str = "sprint:1", **metadata: object) -> None:
        self.board.add_sprint(reference, status="open", **metadata)

    def close_sprint(self, reference: str = "sprint:1") -> None:
        sprint = next(item for item in self.board.sprints if item["reference"] == reference)
        self.board.metadata[int(sprint["id"])]["sprint_status"] = "closed"

    def observers(self) -> dict:
        return load_observers(self.runtime.production_state.load())

    def actions(self, result: dict, step: str = "observer-reconcile") -> list[dict]:
        return [action for action in result["actions"] if action.get("step") == step]

    def expire_launch_grace(self, reference: str = "sprint:1") -> None:
        """Age a launch intent past the window in which a missing pid still counts as alive."""
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        observers[reference].launched_at = time.time() - initial_output_stall_seconds() - 1
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

    def kill_observer(self, reference: str = "sprint:1") -> None:
        record = self.observers()[reference]
        Path(record.pid_file).write_text(str(DEAD_PID), encoding="utf-8")

    # lifecycle ---------------------------------------------------------------

    def test_open_sprint_gets_one_observer_head(self) -> None:
        self.open_sprint()

        result = self.runtime.production_tick()

        actions = self.actions(result)
        self.assertEqual([action["action"] for action in actions], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.head, "codex-observer")
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.state, "running")
        self.assertTrue(record.workspace)
        self.assertTrue(record.handle)

    def test_second_tick_does_not_launch_a_second_head(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        before = self.observers()["sprint:1"]

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].to_json(), before.to_json())

    def test_dead_pid_is_relaunched_and_distinguishable_from_the_first_launch(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-relaunched"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 2)
        self.assertEqual(record.last_action, "relaunched")
        # The pane the dead head left behind is closed before the new one opens.
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        kinds = [event["kind"] for event in self.audit.events("sprint:1")]
        self.assertEqual(kinds, [EVENT_LAUNCHED, EVENT_RELAUNCHED])

    def test_finished_observer_queue_is_relaunched_and_visible_as_recovering(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.host.observer_status_result = {
            "last_activity": time.time() - 2,
            "queue_finished": True,
        }

        with mock.patch.dict(os.environ, {"SECRETARY_OBSERVER_IDLE_SECONDS": "1"}):
            result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-idle-relaunched")
        self.assertIn("completed its queue", action["reason"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "idle-recovering")
        self.assertTrue(record.idle_since)
        self.assertIn("threshold 1s", record.idle_reason)
        status = status_observers(self.runtime.production_state.load())[0]
        self.assertEqual(status["state"], "idle-recovering")
        self.assertIsNotNone(status["idle_since"])
        self.assertIn("completed its queue", status["idle_reason"])
        sprint_status = self.runtime.sprints.status("sprint:1", observer=status)
        self.assertEqual(sprint_status["observer"]["state"], "idle-recovering")

        self.host.observer_status_result = {"last_activity": time.time(), "queue_finished": False}
        resumed = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(resumed)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "running")

    def test_finished_observer_queue_is_visible_during_the_idle_grace_period(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.host.observer_status_result = {
            "last_activity": time.time() - 2,
            "queue_finished": True,
        }

        with mock.patch.dict(os.environ, {"SECRETARY_OBSERVER_IDLE_SECONDS": "3600"}):
            result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-idle-grace")
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "idle-grace")
        self.assertTrue(record.idle_since)
        self.assertIn("3600s idle threshold", record.idle_reason)
        status = status_observers(self.runtime.production_state.load())[0]
        self.assertEqual(status["state"], "idle-grace")
        self.assertIn("automatic relaunch waits", status["idle_reason"])
        sprint_status = self.runtime.sprints.status("sprint:1", observer=status)
        self.assertEqual(sprint_status["observer"]["state"], "idle-grace")

    def test_active_card_keeps_a_finished_observer_queue_in_waiting_state(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.board.metadata[100]["sprint_current_task"] = "secretary-510-pilot"
        self.runtime.production_tick()
        self.host.observer_status_result = {
            "last_activity": time.time() - 2,
            "queue_finished": True,
        }

        with mock.patch.dict(os.environ, {"SECRETARY_OBSERVER_IDLE_SECONDS": "1"}):
            result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-waiting"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "waiting")

    def test_closed_sprint_stops_the_head_and_drops_the_record(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-stopped"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers(), {})
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_STOPPED],
        )

    def test_vanished_sprint_stops_the_head(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.board.sprints.clear()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-stopped"])
        self.assertEqual(self.observers(), {})

    def test_a_reappeared_sprint_reference_is_a_second_audited_lifecycle(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.board.sprints.clear()
        self.runtime.production_tick()

        self.open_sprint()
        result = self.runtime.production_tick()
        self.board.sprints.clear()
        self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        events = self.audit.events("sprint:1")
        self.assertEqual(
            [event["kind"] for event in events],
            [EVENT_LAUNCHED, EVENT_STOPPED, EVENT_LAUNCHED, EVENT_STOPPED],
        )
        # The second lifecycle is its own request, not a retry of the first one.
        self.assertEqual(len({event["request_id"] for event in events}), 4)

    def test_no_open_sprint_changes_nothing(self) -> None:
        before = len(self.host.calls)

        result = self.runtime.production_tick()

        self.assertEqual(self.actions(result), [])
        self.assertNotIn("observers", self.runtime.production_state.load())
        self.assertNotIn("prepare_observer", self.host.calls[before:])
        self.assertNotIn("stop_observer", self.host.calls[before:])

    def test_budget_is_charged_from_card_events_once_and_hard_limit_stops_observer(self) -> None:
        self.catalog.instance = {"sprint_budget": {"signal": 1, "hard": 2}}
        self.runtime.sprints = SprintReader(self.board, data_dir=self.data_dir, thresholds={"signal": 1, "hard": 2})  # type: ignore[arg-type]
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot", kind="red",
            body="fix it", request_id="red-review",
        )
        first = self.runtime.production_tick()
        self.assertEqual(self.runtime.sprints.show("sprint:1")["budget"]["total"], 1)
        self.assertTrue(self.runtime.sprints.show("sprint:1")["budget"]["signal_reached"])
        self.assertEqual(len([row for row in first["actions"] if row.get("step") == "sprint-budget"]), 1)
        repeated = self.runtime.production_tick()
        self.assertEqual(self.runtime.sprints.show("sprint:1")["budget"]["total"], 1)
        self.assertEqual([row for row in repeated["actions"] if row.get("step") == "sprint-budget"], [])
        self.writer.move(
            role="po", actor="operator", reference="secretary-510-pilot", target="blocked",
            reason="operator stop", sprint_override=True,
            sprint_override_reason="operator stop", request_id="blocked-card",
        )
        result = self.runtime.production_tick()
        sprint = self.runtime.sprints.show("sprint:1")
        self.assertEqual(sprint["status"], "stopped")
        self.assertEqual(sprint["budget"]["by_type"]["blocked"], 1)
        hard_stop_events = [
            event for event in self.audit.events("sprint:1")
            if event.get("kind") == "budget_hard_stopped"
        ]
        self.assertEqual(len(hard_stop_events), 1)
        self.assertEqual(hard_stop_events[0]["payload"]["reason"], "budget_hard_limit")
        self.assertIn("observer-stopped", [row.get("action") for row in self.actions(result)])

    def test_budget_event_classification_excludes_green_card_cycle(self) -> None:
        cases = {
            "red_review": {"kind": "verdict", "payload": {"marker": "review:red"}},
            "blocked": {"kind": "moved", "payload": {"to": "blocked"}},
            "red_ci": {"kind": "moved", "payload": {"to": "in_progress"}, "request_id": "gate-red"},
            "preempt": {"kind": "moved", "payload": {"from": "validate", "to": "ready"}},
            "recreated_task": {"kind": "created", "payload": {"budget_event": "recreated_task"}},
            "hotfix": {"kind": "created", "payload": {"budget_event": "hotfix"}},
        }
        self.assertEqual({name: _budget_event_type(event) for name, event in cases.items()}, {name: name for name in cases})
        self.assertIsNone(_budget_event_type({"kind": "verdict", "payload": {"marker": "review:green"}}))
        self.assertIsNone(_budget_event_type({"kind": "moved", "payload": {"to": "done"}}))

    def test_full_green_card_cycle_does_not_charge_the_sprint_budget(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"

        self.writer.claim(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            worker="worker", request_id="green-claim",
        )
        self.writer.move(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            target="validate", reason="worker completed", request_id="green-validate",
        )
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot",
            kind="green", body="looks good", request_id="green-verdict",
        )
        self.writer.move(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            target="done", reason="review passed", request_id="green-done",
        )

        result = self.runtime.production_tick()

        self.assertEqual(self.runtime.sprints.show("sprint:1")["budget"]["total"], 0)
        self.assertEqual([row for row in result["actions"] if row.get("step") == "sprint-budget"], [])

    def test_unlinked_historical_budget_events_are_not_reread_on_every_tick(self) -> None:
        for index in range(20):
            task_id = 1000 + index
            reference = f"secretary-historical-{index}"
            self.board.tasks.append({
                "id": task_id, "reference": reference, "title": reference, "description": "",
                "column_id": 2, "position": task_id, "swimlane_id": 4,
                "date_creation": 1720000000, "date_modification": 1720000000,
            })
            self.board.metadata[task_id] = {"project": "secretary", "task_type": "code"}
            self.board.comments[task_id] = []
            self.audit.append(f"historical-red-{index}", {
                "event_id": f"evt_historical_red_{index}", "request_id": f"historical-red-{index}",
                "ref": reference, "kind": "verdict", "occurred_at": "2026-07-27T00:00:00Z",
                "payload": {"marker": "review:red"},
            })

        self.board.calls.clear()
        self.assertEqual(_reconcile_sprint_budget(self.runtime), [])
        first_reads = [
            params for method, params in self.board.calls
            if method == "getTaskByReference" and params.get("project_id") == 7
        ]
        self.assertEqual(len(first_reads), 20)
        self.assertEqual(
            len([event for event in self.audit.events() if event.get("kind") == "budget_unlinked"]), 20,
        )

        self.board.calls.clear()
        self.assertEqual(_reconcile_sprint_budget(self.runtime), [])
        repeated_reads = [
            params for method, params in self.board.calls
            if method == "getTaskByReference" and params.get("project_id") == 7
        ]
        self.assertEqual(repeated_reads, [])

    def test_unreadable_linked_sprint_skips_only_its_ready_cards_and_is_cached(self) -> None:
        ready = [
            {"ref": "broken-1", "sprint": "sprint:broken", "type": "code", "project": "one"},
            {"ref": "broken-2", "sprint": "sprint:broken", "type": "code", "project": "two"},
            {"ref": "claimable", "sprint": None, "type": "code", "project": "three"},
        ]
        with mock.patch("secretary.dispatcher_production._production_tasks", side_effect=[[], ready]), \
             mock.patch.object(
                 self.runtime.sprints, "show", side_effect=TaskError("backend_error", "sprint board is down", 1)
             ) as show, \
             mock.patch.object(self.runtime, "_claim", return_value={"action": "claimed"}) as claim:
            result = _production_claim_ready(self.runtime, {}, {})

        self.assertEqual(show.call_count, 1)
        self.assertEqual(claim.call_args.args[0]["ref"], "claimable")
        self.assertEqual([item["ref"] for item in result["skipped_ready"]], ["broken-1", "broken-2"])

    def test_unreadable_sprint_board_never_stops_a_live_head(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        def explode(**_kwargs):
            raise TaskError("backend_error", "kanboard is down", 1)

        with mock.patch.object(self.runtime.sprints, "list", explode):
            result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["sprint-board-unavailable"]
        )
        self.assertEqual(self.host.stopped_observers, [])
        self.assertIn("sprint:1", self.observers())

    def test_a_refused_stop_keeps_the_record_and_the_next_tick_retries(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-stop-failed"]
        )
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")
        # The head is still alive, so nothing may claim it was stopped.
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_LAUNCHED])

        self.host.fail_stop_observer_reason = ""
        retry = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(retry)], ["observer-stopped"]
        )
        self.assertEqual(self.observers(), {})
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_STOPPED],
        )

    def test_a_refused_stop_never_yields_a_second_head_when_the_sprint_reopens(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"
        self.runtime.production_tick()

        self.board.metadata[
            int(next(item for item in self.board.sprints if item["reference"] == "sprint:1")["id"])
        ]["sprint_status"] = "open"
        self.host.fail_stop_observer_reason = ""
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "running")

    def test_a_refused_stop_before_a_relaunch_defers_instead_of_doubling_the_head(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-launch-deferred"]
        )
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.handle, "observer:sprint:1")
        self.assertIn("could not be stopped", record.deferred_reason)

    def test_a_bring_up_that_left_its_terminal_up_keeps_the_handle_on_the_record(self) -> None:
        self.open_sprint()
        pid_file = self.data_dir / "observers" / "aborted.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        # The abandoned head is a live process: only the record's own mark tells it apart from a
        # working observer.
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(pid_file),
        )

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-launch-deferred"]
        )
        record = self.observers()["sprint:1"]
        self.assertEqual(record.handle, "observer:sprint:1")
        self.assertTrue(record.abandoned_handle)
        self.assertEqual(record.launches, 0)
        self.assertIn("terminal close failed", record.deferred_reason)
        # Nothing was launched, so no launch event may be in the log.
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_DEFERRED])

    def test_an_abandoned_terminal_that_will_not_close_never_yields_a_second_head(self) -> None:
        self.open_sprint()
        pid_file = self.data_dir / "observers" / "aborted.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(pid_file),
        )
        self.runtime.production_tick()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        result = self.runtime.production_tick()

        # Not "observer-live": the pid behind that handle belongs to a head that never got its
        # sprint. The tick retries the close and brings nothing new up until it succeeds.
        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-launch-deferred"]
        )
        self.assertEqual(self.host.calls.count("prepare_observer"), 1)
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertTrue(record.abandoned_handle)
        self.assertEqual(record.handle, "observer:sprint:1")
        self.assertIn("could not be stopped", record.deferred_reason)

    def test_a_closed_abandoned_terminal_frees_the_sprint_for_a_launch(self) -> None:
        self.open_sprint()
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(self.data_dir / "observers" / "aborted.pid"),
        )
        self.runtime.production_tick()
        self.host.fail_observer_error = None

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertFalse(record.abandoned_handle)
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.state, "running")
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")][-1], EVENT_LAUNCHED)

    def test_a_freeze_takes_an_abandoned_terminal_down_with_the_rest(self) -> None:
        self.open_sprint()
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(self.data_dir / "observers" / "aborted.pid"),
        )
        self.runtime.production_tick()

        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        record = self.observers()["sprint:1"]
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(record.handle, "")
        self.assertFalse(record.abandoned_handle)

    # readiness ---------------------------------------------------------------

    def test_unready_resource_defers_the_launch_and_keeps_the_sprint(self) -> None:
        self.open_sprint()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unauthenticated", "resource authentication failed", 1.0
        )

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launch-deferred")
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "deferred")
        self.assertEqual(record.launches, 0)
        self.assertIn("unauthenticated", record.deferred_reason)
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_DEFERRED])

    # role skill delivery ------------------------------------------------------

    def test_an_undelivered_skill_defers_the_launch_instead_of_a_blind_head(self) -> None:
        self.observer_skill.unlink()
        self.open_sprint()

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launch-deferred")
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "deferred")
        self.assertEqual(record.launches, 0)
        self.assertIn("observe-sprint", record.deferred_reason)
        self.assertIn(str(self.observer_skill), record.deferred_reason)
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_DEFERRED])
        # The same reason has to be readable from outside, or the sprint just looks headless.
        self.assertIn("observe-sprint", status_observers(self.runtime.production_state.load())[0]["deferred_reason"])

    def test_a_delivered_skill_launches_the_head_on_the_next_tick(self) -> None:
        self.observer_skill.unlink()
        self.open_sprint()
        self.runtime.production_tick()

        install_skill_registry(self.data_dir)
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.observers()["sprint:1"].deferred_reason, "")

    def test_a_shell_without_an_observer_target_defers_the_launch(self) -> None:
        """The skill exists and is delivered somewhere, just not to the shell of this head."""
        manifest = self.data_dir / "registry" / "manifest.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace('shell = "codex"', 'shell = "claude"'),
            encoding="utf-8",
        )
        self.open_sprint()

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launch-deferred")
        self.assertEqual(self.host.observers, [])
        self.assertIn("no codex target", self.observers()["sprint:1"].deferred_reason)

    def test_the_launched_prompt_points_at_the_delivered_skill(self) -> None:
        self.open_sprint()

        self.runtime.production_tick()

        prompt = (Path(self.observers()["sprint:1"].workspace) / "SPRINT.md").read_text(encoding="utf-8")
        self.assertIn(str(self.observer_skill), prompt)

    def test_deferred_launch_retries_on_the_next_tick(self) -> None:
        self.open_sprint()
        unready = HeadReadiness("openai-sub", "unavailable", "resource provider is unavailable", 1.0)
        self.runtime.head_readiness = lambda _head: unready
        self.runtime.production_tick()

        del self.runtime.head_readiness
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "running")
        self.assertEqual(record.deferred_reason, "")
        self.assertEqual(record.launches, 1)

    def test_a_deferral_streak_records_one_event(self) -> None:
        self.open_sprint()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unavailable", "resource provider is unavailable", 1.0
        )

        self.runtime.production_tick()
        self.runtime.production_tick()

        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_DEFERRED])

    def test_failed_bring_up_keeps_the_record_intact(self) -> None:
        self.open_sprint()
        self.host.fail_observer_reason = "orca refused the terminal"

        result = self.runtime.production_tick()

        self.assertEqual(self.actions(result)[0]["action"], "observer-launch-deferred")
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 0)
        self.assertIn("orca refused the terminal", record.deferred_reason)

    # audit durability --------------------------------------------------------

    def broken_append(self):
        """Audit storage that takes a staged event but refuses to commit it."""
        def explode(_request_id, _event):
            raise OSError("audit log is not writable")

        return mock.patch.object(self.audit, "append", explode)

    def broken_stage(self):
        """Audit storage that cannot even take the staged event."""
        def explode(_request_id, _event):
            raise OSError("pending audit directory is not writable")

        return mock.patch.object(self.audit, "stage", explode)

    def test_a_refused_audit_append_still_leaves_the_launched_head_on_the_books(self) -> None:
        self.open_sprint()

        with self.broken_append():
            result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launched")
        self.assertEqual(action["audit"], "pending")
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.state, "running")
        self.assertTrue(record.handle)
        # The event is not lost: it waits on disk for the repair pass.
        pending = self.audit.pending_events()
        self.assertEqual([event["kind"] for event in pending], [EVENT_LAUNCHED])
        self.assertEqual(pending[0]["payload"]["workspace"], record.workspace)
        self.audit.reconcile()
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_LAUNCHED])

    def test_a_refused_audit_append_does_not_yield_a_second_head(self) -> None:
        self.open_sprint()
        with self.broken_append():
            self.runtime.production_tick()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_an_unwritable_audit_defers_the_launch_instead_of_opening_a_head(self) -> None:
        self.open_sprint()

        with self.broken_stage():
            result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-launch-deferred"]
        )
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 0)
        self.assertIn("could not be staged", record.deferred_reason)

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    # crash-safe launch intent ------------------------------------------------

    def failing_state_save(self, *, after: int = 0):
        """Production state that stops accepting writes after `after` of them landed.

        `after=0` is a data plane that is down before the tick touches the host; `after=1` lets
        the launch intent land and then kills every write after it, which is a tick that dies
        between opening the head and recording that it did.
        """
        real = self.runtime.production_state.save
        landed = {"count": 0}

        def save(payload):
            if landed["count"] >= after:
                raise OSError("production state is not writable")
            landed["count"] += 1
            real(payload)

        return mock.patch.object(self.runtime.production_state, "save", save)

    def test_the_launch_intent_is_on_disk_before_the_host_is_called(self) -> None:
        self.open_sprint()
        seen: list = []
        real = self.host.prepare_observer

        def spy(sprint, head, *, prompt):
            seen.append(load_observers(self.runtime.production_state.load()).get("sprint:1"))
            return real(sprint, head, prompt=prompt)

        with mock.patch.object(self.host, "prepare_observer", spy):
            self.runtime.production_tick()

        intent = seen[0]
        self.assertIsNotNone(intent)
        self.assertEqual(intent.state, "launching")
        self.assertEqual(intent.pending_launch, 1)
        self.assertEqual(intent.launches, 0)
        # Workspace and pid file are the head's own, known before the head exists.
        self.assertEqual(intent.workspace, self.host.observer_workspace("sprint:1"))
        self.assertEqual(intent.pid_file, self.host.observer_pid_file("sprint:1"))

    def test_state_that_cannot_be_written_launches_no_head_at_all(self) -> None:
        self.open_sprint()

        with self.failing_state_save():
            with self.assertRaises(OSError):
                self.runtime.production_tick()

        self.assertEqual(self.host.observers, [])
        self.assertEqual(self.observers(), {})

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].launches, 1)

    def test_a_launch_intent_that_outlived_its_tick_is_adopted_not_doubled(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()

        intent = self.observers()["sprint:1"]
        self.assertEqual((intent.state, intent.pending_launch, intent.handle), ("launching", 1, ""))
        self.assertEqual(self.host.observers, ["sprint:1"])

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-adopted"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "running")
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.pending_launch, 0)
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_LAUNCHED])

        live = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(live)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_no_tick_after_a_refused_confirmation_ever_opens_a_second_head(self) -> None:
        """The invariant the worker and reviewer contours were ported from (secretary-820).

        The head is up and the write that would have confirmed it refused. Every tick after that,
        not only the first, has to resolve the intent to the head that is already running.
        """
        self.open_sprint()
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()

        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "launching")

        actions = [
            action["action"]
            for _ in range(3)
            for action in self.actions(self.runtime.production_tick())
        ]

        self.assertEqual(actions, ["observer-adopted", "observer-live", "observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_an_adopted_head_is_stopped_through_its_workspace(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()
        self.runtime.production_tick()
        self.close_sprint()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-stopped"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers(), {})

    def test_a_freeze_takes_an_adopted_head_down_too(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()
        self.runtime.production_tick()

        status = self.runtime.pause_pipeline(
            mode="freeze", actor="operator", reason="host maintenance"
        )

        self.assertEqual(status["stopped_observer"], ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "stopped-by-pause")

    def test_an_intent_whose_head_died_before_the_next_tick_is_a_relaunch(self) -> None:
        self.open_sprint()
        # The heartbeat writes a pid nobody is running under, so the head of the lost tick is dead.
        self.host.observer_pid = DEAD_PID
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()

        self.host.observer_pid = os.getpid()
        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-relaunched"]
        )
        # Whatever the lost tick opened is closed before the replacement head opens.
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 2)
        self.assertEqual(record.state, "running")
        # The attempt the log already carries is spent, so the new head gets its own line.
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_RELAUNCHED],
        )

    def test_a_tick_killed_before_the_host_answered_retries_the_same_attempt(self) -> None:
        self.open_sprint()

        with mock.patch.object(self.host, "prepare_observer", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.production_tick()

        self.assertEqual(self.host.observers, [])
        intent = self.observers()["sprint:1"]
        self.assertEqual((intent.state, intent.pending_launch), ("launching", 1))
        # The event of that attempt never made it out of pending, so nothing was observed to launch.
        self.assertEqual([event["kind"] for event in self.audit.pending_events()], [EVENT_LAUNCHED])

        self.expire_launch_grace()
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].launches, 1)
        self.assertEqual(self.audit.pending_events(), [])
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_LAUNCHED])

    def test_an_intent_without_a_pid_yet_waits_out_its_grace_window(self) -> None:
        """A head that has been launched but has not written its pid is not a dead head.

        Its own grace window says so, so the tick leaves the intent alone: no stop, no second
        prepare_observer. Cutting a head that is still coming up would break the one continuous
        session the sprint is supposed to have.
        """
        self.open_sprint()
        with mock.patch.object(self.host, "prepare_observer", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.production_tick()
        self.host.calls.clear()

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-launch-pending"]
        )
        self.assertNotIn("prepare_observer", self.host.calls)
        self.assertNotIn("stop_observer", self.host.calls)
        intent = self.observers()["sprint:1"]
        self.assertEqual((intent.state, intent.pending_launch, intent.launches), ("launching", 1, 0))
        self.assertEqual([event["kind"] for event in self.audit.pending_events()], [EVENT_LAUNCHED])
        self.assertEqual(self.audit.events("sprint:1"), [])

    def test_a_waiting_intent_whose_head_appears_is_adopted(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()
        pid_file = Path(self.observers()["sprint:1"].pid_file)
        written = pid_file.read_text(encoding="utf-8")
        pid_file.unlink()

        waiting = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(waiting)], ["observer-launch-pending"]
        )

        pid_file.write_text(written, encoding="utf-8")
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-adopted"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_a_refused_audit_append_still_drops_the_stopped_record(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()

        with self.broken_append():
            result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-stopped")
        self.assertEqual(action["audit"], "pending")
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        # The terminal is gone, so no record may keep pointing at it.
        self.assertEqual(self.observers(), {})
        self.assertEqual([event["kind"] for event in self.audit.pending_events()], [EVENT_STOPPED])
        self.audit.reconcile()
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_STOPPED],
        )

    def test_an_unwritable_audit_parks_the_stop_instead_of_performing_it(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()

        with self.broken_stage():
            result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-stop-failed"]
        )
        self.assertEqual(self.host.stopped_observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")

        retry = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(retry)], ["observer-stopped"])
        self.assertEqual(self.observers(), {})

    def test_the_repair_pass_commits_a_staged_event_of_a_sprint_that_is_gone(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.board.sprints.clear()
        with self.broken_append():
            self.runtime.production_tick()

        repaired, unresolved = self.writer.reconcile()

        self.assertEqual((repaired, unresolved), (1, 0))
        self.assertEqual(self.audit.pending_events(), [])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_STOPPED],
        )

    def test_a_refused_audit_append_still_records_the_freeze_stop(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        with self.broken_append():
            status = self.runtime.pause_pipeline(
                mode="freeze", actor="operator", reason="host maintenance"
            )

        self.assertEqual(status["stopped_observer"], ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stopped-by-pause")
        self.assertEqual(record.handle, "")
        self.assertEqual([event["kind"] for event in self.audit.pending_events()], [EVENT_STOPPED])

    def test_an_unwritable_audit_keeps_the_freeze_stop_pending(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        with self.broken_stage():
            status = self.runtime.pause_pipeline(
                mode="freeze", actor="operator", reason="host maintenance"
            )

        self.assertEqual(status["stopped_observer"], [])
        self.assertEqual(self.host.stopped_observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "pause-stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")

        result = self.runtime.production_tick()

        self.assertEqual(
            [row["action"] for row in result["observer_stops"]], ["observer-stopped-by-pause"]
        )
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])

    # pause -------------------------------------------------------------------

    def test_freeze_stops_the_observer_and_records_the_reason(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        status = self.runtime.pause_pipeline(
            mode="freeze", actor="operator", reason="host maintenance"
        )

        self.assertEqual(status["stopped_observer"], ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stopped-by-pause")
        self.assertEqual(record.handle, "")
        self.assertIn("host maintenance", record.stopped_reason)
        self.assertGreater(record.paused_at, 0)

    def test_a_refused_freeze_stop_is_reported_and_retried_by_the_frozen_tick(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        status = self.runtime.pause_pipeline(
            mode="freeze", actor="operator", reason="host maintenance"
        )

        self.assertEqual(status["stopped_observer"], [])
        self.assertTrue(any("sprint:1" in warning for warning in status["warnings"]))
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "pause-stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_LAUNCHED])

        self.host.fail_stop_observer_reason = ""
        result = self.runtime.production_tick()

        self.assertEqual(
            [row["action"] for row in result["observer_stops"]], ["observer-stopped-by-pause"]
        )
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stopped-by-pause")
        self.assertEqual(record.handle, "")
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_STOPPED],
        )

    def test_a_head_that_survived_a_refused_freeze_stop_is_not_relaunched_on_resume(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        resumed = self.runtime.resume_pipeline(actor="operator")
        result = self.runtime.production_tick()

        self.assertEqual(resumed["observers_resumed"], ["sprint:1"])
        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.paused_at, 0.0)

    def test_resume_brings_the_observer_back_on_the_next_tick(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        resumed = self.runtime.resume_pipeline(actor="operator")
        result = self.runtime.production_tick()

        self.assertEqual(resumed["observers_resumed"], ["sprint:1"])
        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-relaunched"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "running")
        self.assertEqual(record.launches, 2)
        self.assertEqual(record.paused_at, 0.0)

    def test_drain_leaves_a_live_observer_alone_and_launches_none(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")

        result = self.runtime.production_tick()

        actions = {action["sprint"]: action["action"] for action in self.actions(result)}
        self.assertEqual(actions["sprint:1"], "observer-live")
        self.assertEqual(actions["sprint:2"], "observer-launch-skipped")
        self.assertEqual(self.host.stopped_observers, [])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_a_sprint_opened_during_drain_is_still_visible_from_outside(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")

        self.runtime.production_tick()

        record = self.observers()["sprint:2"]
        self.assertEqual(record.head, "codex-observer")
        self.assertEqual(record.state, "deferred")
        self.assertEqual(record.launches, 0)
        self.assertEqual(record.deferred_reason, "pipeline is draining")
        self.assertTrue(record.last_action_at)
        observed = {row["sprint"]: row for row in self.runtime.production_observe()["observers"]}
        status = {row["sprint"]: row for row in status_observers(self.runtime.production_state.load())}
        for row in (observed["sprint:2"], status["sprint:2"]):
            self.assertEqual(row["head"], "codex-observer")
            self.assertFalse(row["alive"])
            self.assertIn("draining", row["deferred_reason"])
            self.assertTrue(row["last_action_at"])

    def test_the_drained_sprint_is_launched_from_its_record_after_resume(self) -> None:
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")
        self.runtime.production_tick()
        generation = self.observers()["sprint:2"].generation

        self.runtime.resume_pipeline(actor="operator")
        result = self.runtime.production_tick()

        actions = {action["sprint"]: action["action"] for action in self.actions(result)}
        self.assertEqual(actions["sprint:2"], "observer-launched")
        record = self.observers()["sprint:2"]
        self.assertEqual(record.generation, generation)
        self.assertEqual(record.state, "running")
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.deferred_reason, "")
        self.assertEqual(self.host.observers, ["sprint:2"])
        kinds = [event["kind"] for event in self.audit.events("sprint:2")]
        self.assertEqual(kinds, [EVENT_DEFERRED, EVENT_LAUNCHED])

    def test_repeated_drain_ticks_write_one_deferral_event(self) -> None:
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")

        self.runtime.production_tick()
        before = self.observers()["sprint:2"]
        self.runtime.production_tick()

        self.assertEqual(self.observers()["sprint:2"].generation, before.generation)
        self.assertEqual(self.observers()["sprint:2"].launches, 0)
        self.assertEqual(self.host.observers, [])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:2")], [EVENT_DEFERRED]
        )

    def test_frozen_tick_launches_nothing(self) -> None:
        self.open_sprint()
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.host.observers, [])

    # isolation ---------------------------------------------------------------

    def test_a_live_observer_does_not_block_a_card_claim(self) -> None:
        self.open_sprint()

        result = self.runtime.production_tick()

        claim = [action for action in result["actions"] if action.get("step") == "claim"]
        self.assertEqual(claim[0]["pilot_ref"], "secretary-510-pilot")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        # The observer holds no card record and does not occupy the per-project claim gate.
        records = self.runtime.production_state.records(self.runtime.production_state.load())
        self.assertEqual(sorted(records), ["secretary-510-pilot"])
        self.assertNotIn("sprint:1", records)

    def test_the_observer_workspace_is_not_a_card_workspace(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        record = self.observers()["sprint:1"]
        card = self.runtime.production_state.records(self.runtime.production_state.load())
        self.assertNotEqual(record.workspace, card["secretary-510-pilot"].workspace)
        self.assertNotEqual(record.handle, card["secretary-510-pilot"].handle)

    # observability -----------------------------------------------------------

    def test_observer_state_is_visible_without_a_transcript(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        observed = self.runtime.production_observe()["observers"]
        status = status_observers(self.runtime.production_state.load())

        self.assertEqual(observed[0]["sprint"], "sprint:1")
        self.assertEqual(observed[0]["head"], "codex-observer")
        self.assertTrue(observed[0]["alive"])
        self.assertEqual(status[0]["state"], "running")
        self.assertEqual(status[0]["launches"], 1)
        self.assertIsNotNone(status[0]["last_action_at"])
        self.assertIsNone(status[0]["deferred_reason"])

    def test_status_reports_the_deferred_reason(self) -> None:
        self.open_sprint()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unavailable", "resource provider is unavailable", 1.0
        )
        self.runtime.production_tick()

        status = status_observers(self.runtime.production_state.load())

        self.assertEqual(status[0]["state"], "deferred")
        self.assertFalse(status[0]["alive"])
        self.assertIn("unavailable", status[0]["deferred_reason"])


class ObserverConfigurationTests(unittest.TestCase):
    def test_codex_completed_queue_requires_an_empty_composer(self) -> None:
        self.assertTrue(observer_queue_finished("Worked for 2h 00m 46s\n› "))
        self.assertFalse(observer_queue_finished("Worked for 2h 00m 46s"))
        self.assertFalse(observer_queue_finished("Worked for 2h 00m 46s\n› continue"))

    def test_the_observer_head_comes_from_its_own_role_default(self) -> None:
        canonical = canonical_heads(Path(__file__).resolve().parents[1])
        role_defaults = canonical["role_defaults"]

        self.assertIn("observer", role_defaults)
        self.assertNotEqual(role_defaults["observer"], role_defaults["new_card"])
        self.assertIn(role_defaults["observer"], canonical["profiles"])
        self.assertIn(OBSERVER_HEAD_FALLBACK, canonical["profiles"])

    def test_a_registry_without_the_key_falls_back_to_the_named_profile(self) -> None:
        catalog = FakeCatalog()
        catalog.role_defaults.pop("observer")

        self.assertEqual(catalog.observer_head(), OBSERVER_HEAD_FALLBACK)

    def test_the_observer_runs_with_the_role_scoped_environment(self) -> None:
        self.assertIn("observer", ROLE_ALLOWLIST)
        self.assertEqual(ROLE_ALLOWLIST["observer"], ROLE_ALLOWLIST["worker"])
        self.assertIn("observer", ROLE_REQUIRED)

        env_file = Path(tempfile.mkdtemp()) / "runtime.env"
        env_file.write_text(
            "KANBOARD_URL=http://board\nKANBOARD_API_USER=u\nKANBOARD_API_TOKEN=t\n"
            "UNRELATED_SECRET_TOKEN=leak\n",
            encoding="utf-8",
        )

        env = runtime_env("observer", base_env={"PATH": "/usr/bin"}, env_file=env_file)

        self.assertEqual(env["BOARD_ROLE"], "observer")
        self.assertEqual(env["KANBOARD_URL"], "http://board")
        self.assertNotIn("UNRELATED_SECRET_TOKEN", env)

    def test_the_prompt_is_rendered_from_the_live_sprint(self) -> None:
        prompt = render_observer_prompt(
            {
                "ref": "sprint:9",
                "goal": "make the pipeline autonomous",
                "definition_of_done": "an operator sleeps through a sprint",
                "repositories": ["secretary", "codegen"],
                "status": "open",
                "current_task": "secretary-800",
                "budget": {"total": 3},
            },
            skill_path="/shell/skills/observe-sprint/SKILL.md",
        )

        self.assertIn("sprint:9", prompt)
        self.assertIn("make the pipeline autonomous", prompt)
        self.assertIn("an operator sleeps through a sprint", prompt)
        self.assertIn("- secretary", prompt)
        self.assertIn("- codegen", prompt)
        self.assertIn("secretary-800", prompt)
        self.assertIn("/shell/skills/observe-sprint/SKILL.md", prompt)

    def test_the_prompt_points_at_the_skill_instead_of_restating_it(self) -> None:
        """The launch document carries data and one pointer; the instructions live in the skill."""
        prompt = render_observer_prompt(
            {"ref": "sprint:9", "goal": "goal", "definition_of_done": "dod"},
            skill_path="/shell/skills/observe-sprint/SKILL.md",
        )
        canonical = (
            Path(__file__).resolve().parents[1]
            / "skills" / "roles" / "observer" / "observe-sprint" / "SKILL.md"
        ).read_text(encoding="utf-8")

        # Every heading the skill owns is a section the prompt must not carry a second copy of.
        headings = [line for line in canonical.splitlines() if line.startswith("## ")]
        self.assertTrue(headings)
        self.assertEqual([heading for heading in headings if heading in prompt], [])
        self.assertNotIn("sprint resume", prompt)
        self.assertNotIn("task create", prompt)

    def test_request_ids_are_stable_per_launch_generation(self) -> None:
        self.assertEqual(
            observer_request_id("launch", "sprint:1", "gen-a", 1),
            observer_request_id("launch", "sprint:1", "gen-a", 1),
        )
        self.assertNotEqual(
            observer_request_id("launch", "sprint:1", "gen-a", 1),
            observer_request_id("launch", "sprint:1", "gen-a", 2),
        )
        # Two lifecycles of the same sprint reference are two different requests.
        self.assertNotEqual(
            observer_request_id("launch", "sprint:1", "gen-a", 1),
            observer_request_id("launch", "sprint:1", "gen-b", 1),
        )

    def test_each_record_gets_its_own_generation(self) -> None:
        self.assertNotEqual(
            ObserverRecord(sprint="sprint:1").generation,
            ObserverRecord(sprint="sprint:1").generation,
        )


class ObserverTerminalStatusTests(unittest.TestCase):
    def test_real_host_reads_the_completed_queue_and_output_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            host = CommandHostRuntime(FakeCatalog(), Path(root), mode="real")
            record = ObserverRecord(
                sprint="sprint:1", workspace="/workspace", handle="observer:sprint:1"
            )
            calls: list[list[str]] = []

            def run_json(args: list[str]) -> dict:
                calls.append(args)
                if args[1:3] == ["terminal", "list"]:
                    return {
                        "terminals": [
                            {
                                "handle": "observer:sprint:1",
                                "connected": True,
                                "lastOutputAt": 1_753_456_789_123,
                            }
                        ]
                    }
                if args[1:3] == ["terminal", "read"]:
                    return {"terminal": {"tail": ["Worked for 2h 00m 46s", "› "]}}
                raise AssertionError(args)

            with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
                status = host.observer_status(record)

        self.assertEqual(status, {"last_activity": 1_753_456_789.123, "queue_finished": True})
        self.assertEqual([args[1:3] for args in calls], [["terminal", "list"], ["terminal", "read"]])


class RealHostStopObserverTests(unittest.TestCase):
    """The real host, not the fake: a refused stop has to reach the lifecycle."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.host = CommandHostRuntime(FakeCatalog(), self.root / "data", mode="real")  # type: ignore[arg-type]
        self.record = ObserverRecord(
            sprint="sprint:1", head="observer", handle="term-1",
            workspace="/ws/observers/sprint-1", head_possible=True,
        )
        self.calls: list[list[str]] = []

    def _run_json(self, args: list[str]) -> dict[str, object]:
        self.calls.append(args)
        return {}

    def _refusing(self, step: list[str], message: str):
        def run_json(args: list[str]) -> dict[str, object]:
            self.calls.append(args)
            if args[1:3] == step:
                raise HostError(message)
            return {}

        return run_json

    def test_the_head_and_the_workspace_it_was_given_are_both_stopped(self) -> None:
        """What the bring-up registered, the stop gives back: Orca is left with neither a terminal
        of this observer nor a worktree for it."""
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        ):
            self.host.stop_observer(self.record)

        self.assertEqual(
            self.calls,
            [
                [
                    "orca", "worktree", "show",
                    "--worktree", "path:/ws/observers/sprint-1", "--json",
                ],
                [
                    "orca", "terminal", "stop",
                    "--worktree", "path:/ws/observers/sprint-1", "--json",
                ],
                [
                    "orca", "worktree", "rm",
                    "--worktree", "path:/ws/observers/sprint-1", "--force", "--json",
                ],
            ],
        )

    def test_a_head_with_no_handle_is_stopped_through_its_workspace(self) -> None:
        """A head adopted from a launch intent: the handle died with the tick that opened it, and
        the observer workspace is the only pointer left to its terminals."""
        adopted = ObserverRecord(
            sprint="sprint:1", head="observer", workspace="/ws/observers/sprint-1",
            head_possible=True,
        )
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        ):
            self.host.stop_observer(adopted)

        self.assertEqual(
            [args[1:3] for args in self.calls],
            [["worktree", "show"], ["terminal", "stop"], ["worktree", "rm"]],
        )

    def test_a_record_without_a_workspace_still_closes_its_pane(self) -> None:
        """Records written before the launch intent named a workspace: the handle is all there is."""
        legacy = ObserverRecord(sprint="sprint:1", head="observer", handle="term-1")
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        ):
            self.host.stop_observer(legacy)

        self.assertEqual(
            self.calls, [["orca", "terminal", "close", "--terminal", "term-1", "--json"]]
        )

    def test_a_refused_stop_raises_instead_of_reporting_success(self) -> None:
        run_json = self._refusing(["terminal", "stop"], "orca terminal stop failed: pane is busy")
        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            with self.assertRaises(HostError):
                self.host.stop_observer(self.record)

    def test_a_refused_stop_keeps_the_record_and_marks_stop_pending(self) -> None:
        runtime = mock.Mock()
        runtime.host = self.host
        run_json = self._refusing(["terminal", "stop"], "orca terminal stop failed: pane is busy")
        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            self.assertFalse(stop_observer_head(runtime, self.record))

        self.assertTrue(self.record.head_possible)
        self.assertEqual(self.record.workspace, "/ws/observers/sprint-1")

    def test_a_terminal_that_is_stopped_but_a_worktree_that_will_not_go_is_a_failed_stop(
        self,
    ) -> None:
        """Otherwise the record is dropped while the worktree it named is still registered, and
        nothing is left pointing at it to clean it up."""
        runtime = mock.Mock()
        runtime.host = self.host
        run_json = self._refusing(["worktree", "rm"], "orca worktree rm failed: worktree is busy")
        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            self.assertFalse(stop_observer_head(runtime, self.record))

        self.assertEqual(self.record.workspace, "/ws/observers/sprint-1")

    def test_a_workspace_orca_does_not_know_is_a_head_that_is_already_gone(self) -> None:
        """What makes the retry of a half-finished stop terminate."""
        run_json = self._refusing(
            ["worktree", "show"], "orca worktree show failed: selector_not_found"
        )
        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            self.host.stop_observer(self.record)

        self.assertEqual([args[1:3] for args in self.calls], [["worktree", "show"]])

    def test_an_unreadable_answer_is_not_an_absent_workspace(self) -> None:
        """Orca down must not read as "nothing is running": that is how a live head loses its
        record, and the next time the sprint opens a second head is put beside it."""
        runtime = mock.Mock()
        runtime.host = self.host
        run_json = self._refusing(
            ["worktree", "show"], "orca worktree show failed: daemon is unreachable"
        )
        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            self.assertFalse(stop_observer_head(runtime, self.record))

        self.assertTrue(self.record.head_possible)
        self.assertEqual(self.record.workspace, "/ws/observers/sprint-1")

    def test_the_pid_file_is_named_before_the_head_exists(self) -> None:
        self.assertEqual(
            self.host.observer_pid_file("sprint:1"), observer_pid_file("sprint:1")
        )


class RealHostObserverWorkspaceTests(unittest.TestCase):
    """The real host on the bring-up path: how the observer workspace becomes known to Orca.

    A directory made with `mkdir` is not a worktree selector, so the live bring-up used to die on
    `selector_not_found`. The shape of the commands is what these tests pin, not the fake's answer.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        env = mock.patch.dict(
            os.environ, {"SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.root / "workspaces")}
        )
        env.start()
        self.addCleanup(env.stop)
        self.host = CommandHostRuntime(_ObserverCatalog(), self.root / "data", mode="real")  # type: ignore[arg-type]
        self.calls: list[list[str]] = []
        self.shell: list[list[str]] = []
        self.registered = False
        self.created_path: str | None = None
        run_json = mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        )
        run_json.start()
        self.addCleanup(run_json.stop)
        run = mock.patch.object(
            CommandHostRuntime, "_run", lambda _self, args, label, **kwargs: self._run(args)
        )
        run.start()
        self.addCleanup(run.stop)

    @property
    def workspace(self) -> str:
        return self.host.observer_workspace("sprint:1")

    def _run(self, args: list[str]) -> mock.Mock:
        self.shell.append(args)
        if args[:2] == ["git", "-C"] and "init" in args:
            Path(args[2], ".git").mkdir(parents=True, exist_ok=True)
        return mock.Mock(stdout="", stderr="", returncode=0)

    def _run_json(self, args: list[str]) -> dict[str, object]:
        self.calls.append(args)
        if args[1:3] == ["worktree", "show"] and not self.registered:
            raise HostError("orca worktree show failed: selector_not_found")
        if args[1:3] == ["worktree", "create"]:
            path = self.created_path if self.created_path is not None else self.workspace
            Path(path).mkdir(parents=True, exist_ok=True)
            return {"worktree": {"path": path}}
        if args[1:3] == ["terminal", "create"]:
            return {"handle": "term-obs"}
        return {}

    def _prepare(self) -> dict[str, object]:
        return self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")

    def test_the_workspace_is_registered_with_orca_before_the_terminal_is_asked_for(self) -> None:
        launched = self._prepare()

        repo = self.root / "data" / "dispatcher" / "observer-root" / "observers"
        self.assertEqual(
            [args[1:3] for args in self.calls],
            [
                ["worktree", "show"],
                ["repo", "add"],
                ["worktree", "create"],
                ["terminal", "create"],
            ],
        )
        self.assertEqual(
            self.calls[2],
            [
                "orca", "worktree", "create",
                "--repo", f"path:{repo}",
                "--name", Path(self.workspace).name,
                "--base-branch", "observers",
                "--setup", "skip",
                "--no-parent",
                "--json",
            ],
        )
        self.assertIn("--worktree", self.calls[3])
        self.assertEqual(
            self.calls[3][self.calls[3].index("--worktree") + 1], f"path:{self.workspace}"
        )
        self.assertEqual(launched["workspace"], self.workspace)
        self.assertEqual(launched["handle"], "term-obs")

    def test_the_observer_repo_is_its_own_and_not_a_checkout_of_the_project(self) -> None:
        """The observer reads the board and writes no code. Its workspace is cut from an empty
        standalone repo, so there is nothing there to commit the project from."""
        self._prepare()

        repo = self.root / "data" / "dispatcher" / "observer-root" / "observers"
        self.assertEqual([args[2] for args in self.shell], [str(repo), str(repo)])
        self.assertIn("init", self.shell[0])
        self.assertIn("--allow-empty", self.shell[1])
        project_repo = str(FakeCatalog().binding("secretary")["repo"])
        self.assertNotIn(project_repo, [args[2] for args in self.shell])
        self.assertNotIn(f"path:{project_repo}", self.calls[2])

    def test_a_workspace_orca_already_knows_is_reused_by_a_relaunch(self) -> None:
        self.registered = True
        Path(self.workspace).mkdir(parents=True, exist_ok=True)

        self._prepare()

        self.assertEqual(
            [args[1:3] for args in self.calls],
            [["worktree", "show"], ["terminal", "create"]],
        )
        self.assertEqual(self.shell, [])

    def test_a_directory_orca_never_learned_about_is_cleared_not_worked_around(self) -> None:
        """The state the live defect left behind: `worktree create` would otherwise place the
        workspace beside it, at a path no record points at."""
        stale = Path(self.workspace)
        stale.mkdir(parents=True, exist_ok=True)
        (stale / "SPRINT.md").write_text("stale\n", encoding="utf-8")

        self._prepare()

        self.assertEqual(
            (stale / "SPRINT.md").read_text(encoding="utf-8").splitlines()[0], "# Sprint"
        )

    def test_a_workspace_placed_somewhere_else_fails_the_bring_up(self) -> None:
        """The launch intent already names the workspace, and a tick that dies now can only find
        the head through it."""
        self.created_path = str(self.root / "workspaces" / "observers" / "elsewhere")

        with self.assertRaises(HostError) as caught:
            self._prepare()

        self.assertIn("elsewhere", str(caught.exception))
        self.assertNotIn(["terminal", "create"], [args[1:3] for args in self.calls])

    def test_a_workspace_orca_refuses_to_create_leaves_no_terminal(self) -> None:
        def run_json(args: list[str]) -> dict[str, object]:
            if args[1:3] == ["worktree", "create"]:
                raise HostError("orca worktree create failed: repo is unavailable")
            return self._run_json(args)

        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            with self.assertRaises(HostError):
                self._prepare()

        self.assertNotIn(["terminal", "create"], [args[1:3] for args in self.calls])


class RealHostObserverTeardownTests(unittest.TestCase):
    """The lifecycle over the real host seam: what a closed sprint gives back to Orca.

    The bring-up registers the observer workspace before it asks for a terminal, so a failure after
    that point leaves a registration behind with no head at all. The record has to keep pointing at
    it, or the sprint closes and the worktree stays in Orca with nothing left to remove it.
    """

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
                "SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.data_dir / "workspaces"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        (self.data_dir / "bodies").mkdir(parents=True, exist_ok=True)
        self.board = FakeKanboard()
        self.catalog = _ObserverCatalog(instance_dir=self.data_dir)
        self.host = CommandHostRuntime(self.catalog, self.data_dir / "host", mode="real")  # type: ignore[arg-type]
        self.audit = TaskAudit(self.data_dir)
        self.runtime = DispatcherRuntime(
            TaskReader(self.board),  # type: ignore[arg-type]
            TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir),  # type: ignore[arg-type]
            self.audit,
            CutoverState(self.data_dir),
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
            legacy_pause=FakeLegacyPause(),  # type: ignore[arg-type]
        )
        self.runtime.state.save({
            "version": 1,
            "phase": "cutover_committed",
            "pilot_ref": "secretary-510-pilot",
            "old_owner_paused": True,
            "records": {},
        })
        self.calls: list[list[str]] = []
        self.registered = False
        self.terminal_create_fails = False
        self.worktree_create_fails = False
        run_json = mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        )
        run_json.start()
        self.addCleanup(run_json.stop)
        run = mock.patch.object(
            CommandHostRuntime, "_run", lambda _self, args, label, **kwargs: self._run(args)
        )
        run.start()
        self.addCleanup(run.stop)

    @property
    def workspace(self) -> str:
        return self.host.observer_workspace("sprint:1")

    def _run(self, args: list[str]) -> mock.Mock:
        if args[:2] == ["git", "-C"] and "init" in args:
            Path(args[2], ".git").mkdir(parents=True, exist_ok=True)
        return mock.Mock(stdout="", stderr="", returncode=0)

    def _run_json(self, args: list[str]) -> dict[str, object]:
        self.calls.append(args)
        step = args[1:3]
        if step == ["worktree", "show"] and not self.registered:
            raise HostError("orca worktree show failed: selector_not_found")
        if step == ["worktree", "create"]:
            if not any("observer" in arg for arg in args):
                # A card worktree of the same tick's pipeline pass, not the observer's.
                return {"worktree": {"path": str(self.data_dir / "workspaces" / args[6])}}
            if self.worktree_create_fails:
                raise HostError("orca worktree create failed: repo is unavailable")
            Path(self.workspace).mkdir(parents=True, exist_ok=True)
            self.registered = True
            return {"worktree": {"path": self.workspace}}
        if step == ["worktree", "rm"]:
            self.registered = False
            return {}
        if step == ["terminal", "create"]:
            if self.terminal_create_fails:
                raise HostError("orca terminal create failed: selector_not_found")
            return {"handle": "term-obs"}
        return {}

    def observer_calls(self) -> list[list[str]]:
        """The tick's calls about the observer. A tick also runs the card pipeline, whose own
        worktrees and terminals are not what these tests are about."""
        return [args for args in self.calls if any("observer" in arg for arg in args)]

    def steps(self) -> list[list[str]]:
        return [args[1:3] for args in self.observer_calls()]

    def observers(self) -> dict:
        return load_observers(self.runtime.production_state.load())

    def actions(self, result: dict) -> list[str]:
        return [
            action["action"]
            for action in result["actions"]
            if action.get("step") == "observer-reconcile"
        ]

    def close_sprint(self) -> None:
        sprint = next(item for item in self.board.sprints if item["reference"] == "sprint:1")
        self.board.metadata[int(sprint["id"])]["sprint_status"] = "closed"

    def test_a_bring_up_that_dies_after_the_worktree_still_gives_it_back_on_closure(self) -> None:
        self.board.add_sprint("sprint:1", status="open")
        self.terminal_create_fails = True

        deferred = self.runtime.production_tick()

        self.assertEqual(self.actions(deferred), ["observer-launch-deferred"])
        record = self.observers()["sprint:1"]
        self.assertFalse(record.head_possible)
        self.assertTrue(record.workspace_live)
        self.assertTrue(self.registered)

        self.close_sprint()
        self.calls.clear()
        stopped = self.runtime.production_tick()

        self.assertEqual(self.actions(stopped), ["observer-stopped"])
        self.assertEqual(
            self.observer_calls(),
            [
                ["orca", "worktree", "show", "--worktree", f"path:{self.workspace}", "--json"],
                ["orca", "terminal", "stop", "--worktree", f"path:{self.workspace}", "--json"],
                [
                    "orca", "worktree", "rm",
                    "--worktree", f"path:{self.workspace}", "--force", "--json",
                ],
            ],
        )
        self.assertFalse(self.registered)
        self.assertEqual(self.observers(), {})

    def test_a_bring_up_that_never_registered_a_workspace_leaves_nothing_to_remove(self) -> None:
        """The other side of it: the stop asks Orca and takes its answer, rather than removing a
        worktree on the strength of a path the record computed before the host was ever called."""
        self.board.add_sprint("sprint:1", status="open")
        self.worktree_create_fails = True

        self.runtime.production_tick()
        self.close_sprint()
        self.calls.clear()
        stopped = self.runtime.production_tick()

        self.assertEqual(self.actions(stopped), ["observer-stopped"])
        self.assertEqual(self.steps(), [["worktree", "show"]])
        self.assertEqual(self.observers(), {})

    def test_a_worktree_that_will_not_go_keeps_the_closed_sprint_on_the_books(self) -> None:
        """A refused teardown of a workspace with no head behind it is still a failed stop: the
        record survives as `stop-pending` and the next tick comes back to it."""
        self.board.add_sprint("sprint:1", status="open")
        self.terminal_create_fails = True
        self.runtime.production_tick()
        self.close_sprint()
        refusing = self._run_json

        def run_json(args: list[str]) -> dict[str, object]:
            if args[1:3] == ["worktree", "rm"]:
                self.calls.append(args)
                raise HostError("orca worktree rm failed: worktree is busy")
            return refusing(args)

        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            failed = self.runtime.production_tick()

        self.assertEqual(self.actions(failed), ["observer-stop-failed"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stop-pending")
        self.assertTrue(record.workspace_live)
        self.assertTrue(self.registered)

        retried = self.runtime.production_tick()

        self.assertEqual(self.actions(retried), ["observer-stopped"])
        self.assertFalse(self.registered)
        self.assertEqual(self.observers(), {})


class RealHostTuiObserverLaunchTests(unittest.TestCase):
    """The real host on the TUI bring-up path: what a failed prompt delivery hands back."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        env = mock.patch.dict(
            os.environ, {"SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.root / "workspaces")}
        )
        env.start()
        self.addCleanup(env.stop)
        self.host = CommandHostRuntime(_TuiCatalog(), self.root / "data", mode="real")  # type: ignore[arg-type]
        self.stops: list[str] = []
        self.stop_refused = False
        delivery = mock.patch.object(
            dispatcher_module,
            "_deliver_tui_prompt",
            mock.Mock(side_effect=TuiDeliveryError("TUI prompt was not delivered")),
        )
        delivery.start()
        self.addCleanup(delivery.stop)
        run_json = mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        )
        run_json.start()
        self.addCleanup(run_json.stop)

    def _run_json(self, args: list[str]) -> dict[str, object]:
        if args[:3] == ["orca", "terminal", "create"]:
            return {"handle": "term-obs"}
        if args[:3] == ["orca", "terminal", "stop"]:
            self.stops.append(args[4])
            if self.stop_refused:
                raise HostError("orca terminal stop failed: pane is busy")
            return {}
        return {}

    def _prepare(self) -> None:
        self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")

    def test_a_stopped_terminal_reports_a_bring_up_with_nothing_left_running(self) -> None:
        with self.assertRaises(ObserverLaunchAborted) as caught:
            self._prepare()

        self.assertEqual(self.stops, [f"path:{self.host.observer_workspace('sprint:1')}"])
        self.assertEqual(caught.exception.handle, "")

    def test_a_terminal_that_will_not_stop_hands_its_handle_back(self) -> None:
        self.stop_refused = True

        with self.assertRaises(ObserverLaunchAborted) as caught:
            self._prepare()

        self.assertEqual(caught.exception.handle, "term-obs")
        self.assertTrue(caught.exception.workspace)
        self.assertTrue(caught.exception.pid_file)
        self.assertIn("stop failed", str(caught.exception))


class ObserverCodexTrustTests(unittest.TestCase):
    """The bring-up answers codex's trust question for the observer workspace.

    A real `CommandHostRuntime` over a real `InstanceCatalog`: the launcher renders the command and
    prepares the workspace, only `orca` is replaced. The git worktree is a real one, cut from the
    real observer repo the runtime creates, because what codex asks about is the repository root
    that worktree hangs off and no fake reproduces that shape.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.codex_home = self.root / "codex-home"
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.root / "workspaces"),
                "TA_CODEX_HOME": str(self.codex_home),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        catalog = object.__new__(InstanceCatalog)
        catalog._heads = canonical_heads(Path(__file__).resolve().parents[1])  # type: ignore[attr-defined]
        self.host = CommandHostRuntime(catalog, self.root / "data", mode="real")  # type: ignore[arg-type]
        self.commands: list[str] = []
        self.registered = False
        delivery = mock.patch.object(dispatcher_module, "_deliver_tui_prompt", mock.Mock())
        delivery.start()
        self.addCleanup(delivery.stop)
        run_json = mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        )
        run_json.start()
        self.addCleanup(run_json.stop)

    def _run_json(self, args: list[str]) -> dict[str, object]:
        step = args[1:3]
        if step == ["worktree", "show"]:
            if self.registered:
                return {}
            raise HostError("orca worktree show failed: selector_not_found")
        if step == ["worktree", "create"]:
            repo = args[args.index("--repo") + 1].split(":", 1)[1]
            workspace = self.host.observer_workspace("sprint:1")
            # What `orca worktree create` leaves behind: a git worktree of that repo at that path.
            self.host._run(  # type: ignore[attr-defined]
                ["git", "-C", repo, "worktree", "add", "--quiet", "--detach", workspace],
                "worktree add",
            )
            self.registered = True
            return {"worktree": {"path": workspace}}
        if step == ["terminal", "create"]:
            self.commands.append(args[args.index("--command") + 1])
            return {"handle": "term-obs"}
        return {}

    def test_the_bring_up_trusts_the_repository_root_of_the_observer_workspace(self) -> None:
        self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")

        trusted = tomllib.loads((self.codex_home / "config.toml").read_text(encoding="utf-8"))
        workspace = Path(self.host.observer_workspace("sprint:1")).resolve()
        repo_root = (self.root / "data" / "dispatcher" / "observer-root" / "observers").resolve()
        self.assertEqual(trusted["projects"][str(repo_root)]["trust_level"], "trusted")
        self.assertEqual(trusted["projects"][str(workspace)]["trust_level"], "trusted")
        command = self.commands[0]
        self.assertIn(f"CODEX_HOME={self.codex_home} codex", command)
        self.assertNotIn("codex exec", command)
        self.assertIn("--role observer", command)

    def test_a_second_bring_up_leaves_the_recorded_trust_alone(self) -> None:
        self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")
        first = (self.codex_home / "config.toml").read_text(encoding="utf-8")

        self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")

        self.assertEqual((self.codex_home / "config.toml").read_text(encoding="utf-8"), first)

    def test_worker_and_reviewer_launches_never_touch_the_codex_config(self) -> None:
        """Trust is written for the observer alone.

        Worker and reviewer workspaces are worktrees of repositories the codex runtime already
        trusts, so their bring-up has no reason to rewrite the runtime's own `config.toml` and
        must not: it is installation state, shared by every codex head on the host.
        """
        workspace = self.root / "worker-workspace"
        workspace.mkdir()

        for head, role in (("codex", "worker"), ("codex-reviewer", "reviewer")):
            with self.subTest(role=role):
                launch = self.host.catalog.head_launch(
                    head, "TASK.md", workspace=str(workspace), role=role
                )
                self.assertIn(f"CODEX_HOME={self.codex_home} codex exec", launch.command)
                self.assertIn(f"--role {role}", launch.command)
                self.assertFalse(self.codex_home.exists())


class _ObserverCatalog(FakeCatalog):
    """An observer profile whose head takes its prompt on the command line."""

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        codex_mode: str | None = None,
        launch_prompt: str | None = None,
    ):
        return HeadLaunch(f"run-{role}")


class _TuiCatalog(FakeCatalog):
    """An observer profile whose head takes its prompt after the terminal is already up."""

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        codex_mode: str | None = None,
        launch_prompt: str | None = None,
    ):
        return HeadLaunch(f"run-{role}", prompt_after_start=True)


if __name__ == "__main__":
    unittest.main()
