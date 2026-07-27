"""Durable launch intent for the worker and reviewer heads (secretary-820).

The window these tests hold open is the one between "the host has a head running" and "the
dispatcher has a record that says so". A state write that refuses inside it used to leave a live
head nothing pointed at, and the next tick then read the card as headless and launched a second
one. Every test here drives a real restart path (a claim, a rework, a respawn) with the state
plane failing on one side of the host call or the other, and asks the same two questions: was a
head created that nobody can find, and did the recovery produce a second one.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.dispatcher import CutoverState, DispatcherRuntime, PilotSelector
from secretary.dispatcher_gate import GateResult
from secretary.dispatcher_launch import launch_intent_liveness
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_watchdog import initial_output_stall_seconds, pid_file_path
from secretary.tasks import TaskAudit, TaskReader, TaskWriter

from tests.test_dispatcher import FakeCatalog, FakeHost, FakeKanboard, FakeLegacyPause

REF = "secretary-510-pilot"
# Above the default pid_max, so `kill(pid, 0)` raises and the heartbeat reads as a head that died.
DEAD_PID = 999999


class LaunchIntentTests(unittest.TestCase):
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
        self.board = FakeKanboard()
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            TaskAudit(self.data_dir),
            CutoverState(self.data_dir),
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
            legacy_pause=FakeLegacyPause(),  # type: ignore[arg-type]
        )
        self.selector = PilotSelector.exact(REF)
        self.runtime.pause_old(self.selector, actor="operator", evidence="legacy hard pause")
        self.runtime.start_new_pilot(self.selector, actor="operator")

    # fixtures ---------------------------------------------------------------

    def tick(self) -> dict:
        return self.runtime.tick(self.selector)

    def record(self) -> DispatcherRecord | None:
        return self.runtime.state.records(self.runtime.state.load()).get(REF)

    def stored_intent(self) -> dict:
        """The intent as it is on disk, which is the only copy a next tick can read."""
        record = self.runtime.state.load().get("records", {}).get(REF) or {}
        return dict(record.get("launch_intent") or {})

    def fail_launch_intent_save(self):
        """A state plane that refuses exactly the write a launch intent needs, and nothing else.

        Failing every save instead would prove far less: the tick would die on some earlier write
        and never reach the launch at all.
        """
        real = self.runtime.state.save

        def save(payload: dict) -> None:
            records = payload.get("records") or {}
            if any(
                (record.get("launch_intent") or {}).get("role")
                for record in records.values()
                if isinstance(record, dict)
            ):
                raise OSError("dispatcher state is not writable")
            real(payload)

        return mock.patch.object(self.runtime.state, "save", save)

    @contextlib.contextmanager
    def state_dies_after(self, host_method: str):
        """The tick that started a head does not live to record it.

        Every state write after `host_method` returns refuses, which is what a process killed or a
        data plane lost mid-launch looks like from the record's side.
        """
        real_save = self.runtime.state.save
        real_call = getattr(self.host, host_method)
        launched = {"yet": False}

        def save(payload: dict) -> None:
            if launched["yet"]:
                raise OSError("dispatcher state is not writable")
            real_save(payload)

        def call(*args, **kwargs):
            result = real_call(*args, **kwargs)
            launched["yet"] = True
            return result

        with mock.patch.object(self.runtime.state, "save", save):
            with mock.patch.object(self.host, host_method, call):
                yield

    def report_done(self, request_id: str = "worker-done") -> None:
        self.writer.report(
            role="worker",
            actor="worker",
            reference=REF,
            kind="done",
            body="done",
            request_id=request_id,
        )

    def verdict(self, kind: str, body: str, request_id: str) -> None:
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference=REF, kind=kind,
            body=body, request_id=request_id,
        )

    def run_to_validate(self) -> None:
        self.tick()
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")

    def age_intent(self, seconds: float) -> None:
        """Push a stored intent back in time, so its grace window has run out."""
        payload = self.runtime.state.load()
        payload["records"][REF]["launch_intent"]["at"] -= seconds
        self.runtime.state.save(payload)

    # worker: before the host call -------------------------------------------

    def test_the_worker_launch_intent_is_on_disk_before_the_host_is_called(self) -> None:
        seen: list[dict] = []
        real = self.host.prepare_worker

        def spy(*args, **kwargs):
            seen.append(self.stored_intent())
            return real(*args, **kwargs)

        with mock.patch.object(self.host, "prepare_worker", spy):
            self.tick()

        intent = seen[0]
        self.assertEqual(intent["role"], "worker")
        self.assertEqual(intent["action"], "claim")
        self.assertEqual(intent["head"], "codex")
        self.assertEqual(intent["round"], 1)
        # Workspace and pid file are the head's own, both known before the head exists: without
        # them a tick that dies here could neither find the head nor read its liveness.
        self.assertEqual(intent["workspace"], self.host.restore_workspace({}, f"{REF}-pilot"))
        self.assertEqual(intent["pid_file"], pid_file_path("worker", REF))
        # And it is gone again once the host has answered and the launch is recorded in full.
        self.assertEqual(self.stored_intent(), {})

    def test_state_that_cannot_be_written_launches_no_worker_at_all(self) -> None:
        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-intent-unwritable")
        self.assertEqual(outcome["status"], "degraded")
        self.assertEqual(self.host.prepared, [], "no head may exist that no record can find")
        self.assertEqual(self.stored_intent(), {})

        # The card keeps its claim, and the very next tick brings up exactly one head.
        recovered = self.tick()

        self.assertEqual(recovered["step"], "claim")
        self.assertEqual(self.host.prepared, [REF])

    # worker: after the host call --------------------------------------------

    def test_a_worker_launch_that_outlived_its_tick_is_adopted_not_doubled(self) -> None:
        with self.state_dies_after("prepare_worker"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.host.prepared, [REF])
        intent = self.stored_intent()
        self.assertEqual((intent["role"], intent["action"]), ("worker", "claim"))

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.prepared, [REF], "the live head must not be launched twice")
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "claimed")
        self.assertEqual(record.workspace, self.host.restore_workspace({}, f"{REF}-pilot"))
        self.assertEqual(self.stored_intent(), {}, "an adopted intent is spent")

        # And the card carries on from the adopted head instead of being restarted around it.
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.host.prepared, [REF])

    def test_a_worker_intent_whose_head_died_is_relaunched_exactly_once(self) -> None:
        self.host.head_pid = DEAD_PID
        with self.state_dies_after("prepare_worker"):
            with self.assertRaises(OSError):
                self.tick()

        self.host.head_pid = os.getpid()
        relaunched = self.tick()

        self.assertEqual(relaunched["step"], "claim")
        # Whatever the lost tick left in that workspace is closed before the replacement opens.
        self.assertEqual(
            [call for call in self.host.calls if call in ("prepare_worker", "stop")],
            ["prepare_worker", "stop", "prepare_worker"],
        )
        self.assertEqual(self.stored_intent(), {})

    def test_an_intent_without_a_heartbeat_waits_out_its_grace_window(self) -> None:
        """A head that has been launched but has not written its pid is not a dead head."""
        self.host.head_pid = None
        with self.state_dies_after("prepare_worker"):
            with self.assertRaises(OSError):
                self.tick()

        pending = self.tick()

        self.assertEqual(pending["action"], "worker-launch-pending")
        self.assertEqual(self.host.prepared, [REF], "a head that is still starting is not replaced")
        self.assertNotIn("stop", self.host.calls)
        self.assertEqual(self.stored_intent()["role"], "worker")

        # Once the window has run out with no heartbeat, the launch counts as one that left
        # nothing running and the ordinary path relaunches.
        self.age_intent(initial_output_stall_seconds() + 60)
        self.host.head_pid = os.getpid()

        self.assertEqual(self.tick()["step"], "claim")
        self.assertEqual(self.host.prepared, [REF, REF])

    # worker: the restart paths, not only the first claim ---------------------

    def test_a_rework_launch_that_outlived_its_tick_is_adopted_not_doubled(self) -> None:
        self.run_to_validate()
        self.tick()  # reviewer up
        self.verdict("red", "needs work", "verdict-red")
        with self.state_dies_after("restart_worker"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.stored_intent()["action"], "review-red-rework")

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "claimed")

    def test_a_gate_red_rework_writes_its_intent_before_the_relaunch(self) -> None:
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]
        seen: list[dict] = []
        real = self.host.restart_worker

        def spy(*args, **kwargs):
            seen.append(self.stored_intent())
            return real(*args, **kwargs)

        with mock.patch.object(self.host, "restart_worker", spy):
            outcome = self.tick()

        self.assertEqual(outcome["action"], "gate-red-rework")
        self.assertEqual(seen[0]["action"], "gate-red-rework")
        self.assertEqual(seen[0]["role"], "worker")

    def test_a_respawn_that_outlived_its_tick_is_adopted_not_doubled(self) -> None:
        self.tick()
        # A worker pane Orca no longer knows about: the watchdog respawns it once.
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        with self.state_dies_after("restart_worker"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.stored_intent()["action"], "worker-respawn")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

        # The respawned head is alive, so it is adopted rather than respawned a second time, even
        # though the terminal inventory still cannot see it.
        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_a_stale_done_rework_writes_its_intent_before_the_relaunch(self) -> None:
        self.run_to_validate()
        self.tick()
        self.verdict("red", "needs work", "verdict-red")
        self.tick()  # rework head up on the rejected sha
        seen: list[dict] = []
        real = self.host.restart_worker

        def spy(*args, **kwargs):
            seen.append(self.stored_intent())
            return real(*args, **kwargs)

        self.report_done(request_id="worker-done-again")
        with mock.patch.object(self.host, "restart_worker", spy):
            outcome = self.tick()

        self.assertEqual(outcome["action"], "stale-done-rework")
        self.assertEqual(seen[0]["action"], "stale-done-rework")

    # reviewer ---------------------------------------------------------------

    def test_the_review_launch_intent_is_on_disk_before_the_host_is_called(self) -> None:
        self.run_to_validate()
        seen: list[dict] = []
        real = self.host.start_review

        def spy(*args, **kwargs):
            seen.append(self.stored_intent())
            return real(*args, **kwargs)

        with mock.patch.object(self.host, "start_review", spy):
            self.tick()

        intent = seen[0]
        self.assertEqual(intent["role"], "review")
        self.assertEqual(intent["action"], "review-started")
        self.assertEqual(intent["head"], "codex-reviewer")
        self.assertEqual(intent["workspace"], str(self.data_dir / "workspaces" / f"{REF}-pilot"))
        self.assertEqual(self.stored_intent(), {})

    def test_state_that_cannot_be_written_launches_no_reviewer_at_all(self) -> None:
        self.run_to_validate()

        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "review-launch-intent-unwritable")
        self.assertEqual(self.host.reviews, [])

        recovered = self.tick()

        self.assertEqual(recovered["step"], "review")
        self.assertEqual(self.host.reviews, [REF], "exactly one reviewer, on the retry")

    def test_a_review_launch_that_outlived_its_tick_is_adopted_not_doubled(self) -> None:
        self.run_to_validate()
        with self.state_dies_after("start_review"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.host.reviews, [REF])
        self.assertEqual(self.stored_intent()["role"], "review")

        adopted = self.tick()

        self.assertEqual(adopted["action"], "review-launch-adopted")
        self.assertEqual(self.host.reviews, [REF], "the live reviewer must not be doubled")
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "reviewing")
        # The merge gate refuses a verdict it cannot tie to a checkout, so the adopted reviewer
        # gets the commit its launch was pinned at.
        self.assertEqual(record.review_commit, self.host.commit)

        # The verdict of the adopted reviewer lands on the card it was launched for.
        self.verdict("green", "looks good", "verdict-green")
        self.assertEqual(self.tick()["to"], "done")
        self.assertEqual(self.host.reviews, [REF])

    def test_a_review_intent_whose_head_died_starts_exactly_one_replacement(self) -> None:
        self.run_to_validate()
        self.host.head_pid = DEAD_PID
        with self.state_dies_after("start_review"):
            with self.assertRaises(OSError):
                self.tick()

        self.host.head_pid = os.getpid()
        # The reviewer of the lost tick is gone, so the card is back to needing one.
        self.host.review_running_result = False
        restarted = self.tick()

        self.assertEqual(restarted["step"], "review")
        self.assertEqual(self.host.reviews, [REF, REF])
        self.assertEqual(self.stored_intent(), {})

    def test_a_review_intent_without_a_heartbeat_waits_out_its_grace_window(self) -> None:
        self.run_to_validate()
        self.host.head_pid = None
        with self.state_dies_after("start_review"):
            with self.assertRaises(OSError):
                self.tick()

        pending = self.tick()

        self.assertEqual(pending["action"], "review-launch-pending")
        self.assertEqual(self.host.reviews, [REF])

    # liveness ---------------------------------------------------------------

    def test_a_heartbeat_that_never_appears_reads_as_dead_only_past_the_window(self) -> None:
        intent = {"pid_file": str(self.data_dir / "nothing.pid"), "at": 1000.0}

        inside = launch_intent_liveness(intent, now=1000.0 + initial_output_stall_seconds() - 1)
        outside = launch_intent_liveness(intent, now=1000.0 + initial_output_stall_seconds() + 1)

        self.assertEqual((inside["alive"], inside["pid_known"]), (True, False))
        self.assertEqual((outside["alive"], outside["pid_known"]), (False, False))


if __name__ == "__main__":
    unittest.main()
