"""Observer-head lifecycle: the production tick against the sprint board (secretary-793)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.dispatcher import CommandHostRuntime, CutoverState, DispatcherRuntime
from secretary.dispatcher_observer import (
    EVENT_DEFERRED,
    EVENT_LAUNCHED,
    EVENT_RELAUNCHED,
    EVENT_STOPPED,
    OBSERVER_HEAD_FALLBACK,
    ObserverRecord,
    load_observers,
    observer_request_id,
    render_observer_prompt,
    stop_observer_head,
)
from secretary.dispatcher_types import HostError
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
        self.assertNotIn("sprint:2", self.observers())

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


if __name__ == "__main__":
    unittest.main()
