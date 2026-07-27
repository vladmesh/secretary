"""Observer-head lifecycle: the production tick against the sprint board (secretary-793)."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from secretary import dispatcher as dispatcher_module
from secretary.dispatcher import CommandHostRuntime, CutoverState, DispatcherRuntime
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
    put_observers,
    observer_request_id,
    render_observer_prompt,
    stop_observer_head,
)
from secretary.sprints import SprintReader
from secretary.dispatcher_types import HostError
from secretary.dispatcher_production import _budget_event_type
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


class ObserverLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
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
            reason="operator stop", request_id="blocked-card",
        )
        result = self.runtime.production_tick()
        sprint = self.runtime.sprints.show("sprint:1")
        self.assertEqual(sprint["status"], "stopped")
        self.assertEqual(sprint["budget"]["by_type"]["blocked"], 1)
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
        prompt = render_observer_prompt({
            "ref": "sprint:9",
            "goal": "make the pipeline autonomous",
            "definition_of_done": "an operator sleeps through a sprint",
            "repositories": ["secretary", "codegen"],
            "current_task": "secretary-800",
            "budget": {"total": 3},
        })

        self.assertIn("sprint:9", prompt)
        self.assertIn("make the pipeline autonomous", prompt)
        self.assertIn("an operator sleeps through a sprint", prompt)
        self.assertIn("- secretary", prompt)
        self.assertIn("- codegen", prompt)
        self.assertIn("secretary-800", prompt)
        self.assertIn("run-sprint", prompt)

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


class RealHostStopObserverTests(unittest.TestCase):
    """The real host, not the fake: a refused close has to reach the lifecycle."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.host = CommandHostRuntime(FakeCatalog(), self.root / "data", mode="real")  # type: ignore[arg-type]
        self.record = ObserverRecord(sprint="sprint:1", head="observer", handle="term-1")
        self.calls: list[list[str]] = []

    def _run_json(self, args: list[str]) -> dict[str, object]:
        self.calls.append(args)
        return {}

    def _run_json_refusing(self, args: list[str]) -> dict[str, object]:
        self.calls.append(args)
        raise HostError("orca terminal close failed: pane is busy")

    def test_close_is_requested_for_the_observer_pane(self) -> None:
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        ):
            self.host.stop_observer(self.record)

        self.assertEqual(
            self.calls, [["orca", "terminal", "close", "--terminal", "term-1", "--json"]]
        )

    def test_a_refused_close_raises_instead_of_reporting_success(self) -> None:
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json_refusing(args)
        ):
            with self.assertRaises(HostError):
                self.host.stop_observer(self.record)

    def test_a_refused_close_keeps_the_record_and_marks_stop_pending(self) -> None:
        runtime = mock.Mock()
        runtime.host = self.host
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json_refusing(args)
        ):
            self.assertFalse(stop_observer_head(runtime, self.record))

    def test_a_head_with_no_handle_is_stopped_through_its_workspace(self) -> None:
        """A head adopted from a launch intent: the handle died with the tick that opened it, and
        the observer workspace is the only pointer left to its terminals."""
        adopted = ObserverRecord(
            sprint="sprint:1", head="observer", workspace="/ws/observers/sprint-1",
            head_possible=True,
        )

        def run_json(args: list[str]) -> dict[str, object]:
            self.calls.append(args)
            if args[1] == "terminal" and args[2] == "list":
                return {"terminals": [{"handle": "term-9"}, {"handle": "term-10"}]}
            return {}

        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            self.host.stop_observer(adopted)

        self.assertEqual(
            self.calls[1:],
            [
                ["orca", "terminal", "close", "--terminal", "term-9", "--json"],
                ["orca", "terminal", "close", "--terminal", "term-10", "--json"],
            ],
        )

    def test_a_workspace_with_no_terminals_is_already_stopped(self) -> None:
        adopted = ObserverRecord(
            sprint="sprint:1", head="observer", workspace="/ws/observers/sprint-1",
            head_possible=True,
        )
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        ):
            self.host.stop_observer(adopted)

        self.assertEqual([args[2] for args in self.calls], ["list"])

    def test_an_unreadable_terminal_list_is_not_an_empty_one(self) -> None:
        """Orca down while the handle is gone: the stop must fail, not report success.

        Reporting success here drops the record of a head that may well still be running, and the
        next time the sprint opens the tick puts a second observer beside it.
        """
        adopted = ObserverRecord(
            sprint="sprint:1", head="observer", workspace="/ws/observers/sprint-1",
            head_possible=True,
        )
        runtime = mock.Mock()
        runtime.host = self.host

        def run_json(args: list[str]) -> dict[str, object]:
            self.calls.append(args)
            raise HostError("orca terminal list failed: daemon is unreachable")

        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            self.assertFalse(stop_observer_head(runtime, adopted))

        self.assertTrue(adopted.head_possible)
        self.assertEqual(adopted.workspace, "/ws/observers/sprint-1")

    def test_a_terminal_list_that_is_not_a_list_is_not_an_empty_one(self) -> None:
        adopted = ObserverRecord(
            sprint="sprint:1", head="observer", workspace="/ws/observers/sprint-1",
            head_possible=True,
        )
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: {"terminals": "nope"}
        ):
            with self.assertRaises(HostError):
                self.host.stop_observer(adopted)

    def test_the_pid_file_is_named_before_the_head_exists(self) -> None:
        self.assertEqual(
            self.host.observer_pid_file("sprint:1"), observer_pid_file("sprint:1")
        )


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
        self.closes: list[str] = []
        self.close_refused = False
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
        if args[:3] == ["orca", "terminal", "close"]:
            self.closes.append(args[4])
            if self.close_refused:
                raise HostError("orca terminal close failed: pane is busy")
            return {}
        return {}

    def _prepare(self) -> None:
        self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")

    def test_a_closed_terminal_reports_a_bring_up_with_nothing_left_running(self) -> None:
        with self.assertRaises(ObserverLaunchAborted) as caught:
            self._prepare()

        self.assertEqual(self.closes, ["term-obs"])
        self.assertEqual(caught.exception.handle, "")

    def test_a_terminal_that_will_not_close_hands_its_handle_back(self) -> None:
        self.close_refused = True

        with self.assertRaises(ObserverLaunchAborted) as caught:
            self._prepare()

        self.assertEqual(self.closes, ["term-obs"])
        self.assertEqual(caught.exception.handle, "term-obs")
        self.assertTrue(caught.exception.workspace)
        self.assertTrue(caught.exception.pid_file)
        self.assertIn("close failed", str(caught.exception))


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
