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
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from secretary import dispatcher as secretary_dispatcher
from secretary.dispatcher import (
    CommandHostRuntime,
    CutoverState,
    DispatcherRuntime,
    PilotSelector,
)
from secretary.dispatcher_launcher import HeadLaunch
from secretary.dispatcher_tui import TuiDeliveryError
from secretary.dispatcher_gate import GateResult
from secretary.dispatcher_launch import launch_intent_liveness
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_types import HeadLaunchAborted, HostError
from secretary.dispatcher_watchdog import initial_output_stall_seconds, pid_file_path
from secretary.dispatcher_worker_lifecycle import WorkerContinuation, WorkerContinuationStage
from secretary.routing_journal import attempts as routing_attempts
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

    def refuse_audit(self, match: str):
        """A journal that refuses exactly the writes whose request id carries `match`.

        The refusal lands on `stage`, which is where the journal is written before the backend
        mutation it describes: nothing of that write happens at all.
        """
        real = self.writer.audit.stage

        def stage(request_id: str, event: dict) -> None:
            if match in request_id:
                raise OSError("audit journal is not writable")
            real(request_id, event)

        return mock.patch.object(self.writer.audit, "stage", stage)

    @contextlib.contextmanager
    def audit_dies_after(self, host_method: str):
        """The journal stops accepting writes the moment the head is up.

        The other half of `state_dies_after`: the launch itself succeeded, and what is refused is
        the telemetry the launch path writes after it.
        """
        real_stage = self.writer.audit.stage
        real_append = self.writer.audit.append
        real_call = getattr(self.host, host_method)
        launched = {"yet": False}

        def stage(request_id: str, event: dict) -> None:
            if launched["yet"]:
                raise OSError("audit journal is not writable")
            real_stage(request_id, event)

        def append(request_id: str, event: dict) -> str:
            if launched["yet"]:
                raise OSError("audit journal is not writable")
            return real_append(request_id, event)

        def call(*args, **kwargs):
            result = real_call(*args, **kwargs)
            launched["yet"] = True
            return result

        with mock.patch.object(self.writer.audit, "stage", stage):
            with mock.patch.object(self.writer.audit, "append", append):
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

    def test_a_stale_done_rework_after_an_unconfirmed_stop_starts_nothing(self) -> None:
        self.run_to_validate()
        self.tick()
        self.verdict("red", "needs work", "verdict-red")
        self.tick()  # rework head up on the rejected sha
        self.report_done(request_id="worker-done-again")
        self.host.calls.clear()
        self.host.fail_stop_head_reason = "orca terminal stop failed"

        outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-stop-unconfirmed")
        self.assertEqual(outcome["status"], "degraded")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.stored_intent(), {}, "no launch was even fixed on disk")

        # Once the host confirms the stop, the rework relaunch happens exactly once.
        self.host.fail_stop_head_reason = ""

        retried = self.tick()

        self.assertEqual(retried["action"], "stale-done-rework")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    # worker: the round a rework launch belongs to ---------------------------

    def rework_after_red_review(self) -> None:
        """Bring the card to the point where the next tick relaunches a worker for round 2."""
        self.run_to_validate()
        self.tick()  # reviewer up
        self.verdict("red", "needs work", "verdict-red")

    def test_an_uninterrupted_review_red_rework_opens_the_next_round(self) -> None:
        """The baseline the interrupted rework below has to end up matching."""
        self.rework_after_red_review()

        self.assertEqual(self.tick()["action"], "rework-started")
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2)

    def test_an_adopted_review_red_rework_lands_on_the_round_it_reserved(self) -> None:
        """Recovery resumes the rework's own round, not the one the red verdict closed.

        The round is reserved before the intent goes to disk precisely so a tick that dies between
        the relaunch and its record cannot collapse two rounds, with their routing and their
        verdicts, into one.
        """
        self.rework_after_red_review()
        with self.state_dies_after("restart_worker"):
            with self.assertRaises(OSError):
                self.tick()

        intent = self.stored_intent()
        self.assertEqual((intent["round"], intent["opens_round"]), (2, True))

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2)
        # The previous round's heads go with it: the round records the adopted worker as its own,
        # and the reviewer of the round that was rejected is not carried over.
        self.assertEqual(record.worker_run.get("role"), "worker")
        self.assertEqual(record.review_run, {})

    def test_an_adopted_gate_red_rework_lands_on_the_round_it_reserved(self) -> None:
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]
        with self.state_dies_after("restart_worker"):
            with self.assertRaises(OSError):
                self.tick()

        intent = self.stored_intent()
        self.assertEqual((intent["action"], intent["round"], intent["opens_round"]),
                         ("gate-red-rework", 2, True))

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2)

    def test_a_red_delivery_that_outlives_its_tick_replays_without_a_second_worker(self) -> None:
        """The durable resuming state masks the old done report on recovery."""
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.state_dies_after("resume_worker"):
            with self.assertRaises(OSError):
                self.tick()

        retained = self.record()
        assert retained is not None
        self.assertEqual(retained.worker_continuation.stage, WorkerContinuationStage.DELIVERY_PENDING)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-reused-worker")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.record().state, "claimed")  # type: ignore[union-attr]

    def test_a_done_report_after_interrupted_red_delivery_is_not_sent_again(self) -> None:
        """The worker may finish before recovery checkpoints its already-delivered prompt."""
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.state_dies_after("resume_worker"):
            with self.assertRaises(OSError):
                self.tick()

        self.report_done("worker-done-after-red-delivery")
        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-reused-worker")
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

    def test_an_unnamed_worker_is_swept_by_workspace_before_replacement(self) -> None:
        record = DispatcherRecord(
            worker="worker", workspace=str(self.data_dir / "workspace"), handle="", head="codex",
            review_head="codex-reviewer", attempt_id="attempt", comment_baseline=0,
            review_baseline=0, state="claimed", claimed_at=0.0,
        )

        outcome = self.runtime._stop_worker_confirmed(
            record, REF, step="gate", attempt_id="attempt"
        )

        self.assertIsNone(outcome)
        self.assertIn("stop_workspace", self.host.calls)

    def test_a_red_delivery_recovery_keeps_the_phase_it_persisted(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.tick()
        record = self.record()
        assert record is not None
        record.worker_continuation.begin_retention(time.time())
        record.worker_continuation.confirm_validation_move()
        record.worker_continuation.begin_delivery("merge-gate", time.time())
        payload = self.runtime.state.load()
        payload["records"][REF] = record.to_json()
        self.runtime.state.save(payload)
        recovered = self.tick()

        self.assertEqual(recovered["action"], "merge-gate-red-reused-worker")
        continuation = self.reader.show(REF)["comments"][-1]["body"]
        self.assertIn("merge-gate red continuation", continuation)

    def test_a_red_review_delivery_recovery_reuses_the_same_session(self) -> None:
        """A crash between the red verdict and its delivery replays into the same conversation."""
        self.host.fail_resume_worker_reason = ""
        self.tick()
        record = self.record()
        assert record is not None
        record.worker_continuation.begin_retention(time.time())
        record.worker_continuation.confirm_validation_move()
        record.worker_continuation.begin_delivery("review", time.time())
        payload = self.runtime.state.load()
        payload["records"][REF] = record.to_json()
        self.runtime.state.save(payload)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "review-red-reused-worker")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertIn("review red continuation", self.reader.show(REF)["comments"][-1]["body"])

    def test_a_freeze_crash_replays_the_validate_move_without_waking_the_worker(self) -> None:
        self.tick()
        self.report_done("worker-done-freeze-crash")
        real_move = self.writer.move

        def die_before_move(**kwargs):
            if kwargs.get("target") == "validate":
                raise OSError("dispatcher died before board move")
            return real_move(**kwargs)

        with mock.patch.object(self.writer, "move", die_before_move):
            with self.assertRaises(OSError):
                self.tick()

        retained = self.record()
        assert retained is not None
        self.assertEqual(
            retained.worker_continuation.stage,
            WorkerContinuationStage.VALIDATION_MOVE_PENDING,
        )
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")

        recovered = self.tick()

        self.assertEqual(recovered["to"], "validate")
        self.assertEqual(self.host.calls.count("retain_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

    def test_a_crash_after_validate_move_keeps_the_worker_frozen_for_review(self) -> None:
        self.tick()
        self.report_done("worker-done-after-validate-move")
        real_save = self.runtime.state.save

        def die_after_move(payload: dict) -> None:
            record = payload.get("records", {}).get(REF, {})
            if record.get("state") == "validate":
                raise OSError("dispatcher died after board move")
            real_save(payload)

        with mock.patch.object(self.runtime.state, "save", die_after_move):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.reader.show(REF)["state"], "validate")
        retained = self.record()
        assert retained is not None
        self.assertEqual(
            retained.worker_continuation.stage,
            WorkerContinuationStage.VALIDATION_MOVE_PENDING,
        )
        self.host.gate_results = [GateResult("green", "passed")]

        recovered = self.tick()

        self.assertEqual(recovered["action"], "review-started")
        # The worker stays suspended for the reviewer instead of being stopped: the checkout is
        # still untouched, and a red verdict has a conversation to hand the findings back to.
        self.assertNotIn("stop_head:worker", self.host.calls)
        self.assertLess(
            self.host.calls.index("confirm_worker_retained"), len(self.host.calls)
        )

    def fail_the_red_move(self):
        """The red intent reaches the disk and the board move behind it does not.

        A refusing move and a process that dies just before it leave the same thing on disk: an
        open red transition over a card the board still shows in Validate.
        """
        real_move = self.writer.move

        def move(**kwargs):
            if kwargs.get("target") == "in_progress":
                raise OSError("dispatcher died before the red board move")
            return real_move(**kwargs)

        return mock.patch.object(self.writer, "move", move)

    def assert_red_intent_open_in_validate(self, phase: str) -> None:
        self.assertEqual(self.reader.show(REF)["state"], "validate")
        stranded = self.record()
        assert stranded is not None
        self.assertEqual(
            stranded.worker_continuation.stage, WorkerContinuationStage.RED_TRANSITION_PENDING
        )
        self.assertEqual(stranded.worker_continuation.phase, phase)

    def test_a_red_gate_intent_before_the_board_move_outranks_a_fresh_green_gate(self) -> None:
        """The recorded red verdict is not up for re-decision by the next tick's rollup.

        Between the two ticks CI can turn green: the failing job is retried, a flake settles, or the
        rollup simply finishes. Reading the gate again there would start a reviewer over a card that
        already owes its worker a red round, and the red verdict would be gone from the record with
        nothing having delivered it.
        """
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.fail_the_red_move():
            with self.assertRaises(OSError):
                self.tick()

        self.assert_red_intent_open_in_validate("gate")
        self.host.gate_results = [GateResult("green", "passed")]

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-reused-worker")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("start_review"), 0)
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertIn("gate red continuation: reused", self.reader.show(REF)["comments"][-1]["body"])

    def test_a_red_review_intent_before_the_board_move_is_finished_as_that_intent(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.rework_after_red_review()

        with self.fail_the_red_move():
            with self.assertRaises(OSError):
                self.tick()

        self.assert_red_intent_open_in_validate("review")

        recovered = self.tick()

        self.assertEqual(recovered["action"], "review-red-reused-worker")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("start_review"), 1, "no second reviewer is spawned")
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertIn(
            "review red continuation: reused", self.reader.show(REF)["comments"][-1]["body"]
        )

    def test_a_red_gate_intent_without_a_session_still_moves_and_replaces_once(self) -> None:
        """Nothing to reuse is not a lesser transition: the replacement is owed just the same."""
        self.without_a_retained_session()
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.fail_the_red_move():
            with self.assertRaises(OSError):
                self.tick()

        self.assert_red_intent_open_in_validate("gate")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.host.gate_results = [GateResult("green", "passed")]

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-rework")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("start_review"), 0)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_a_red_review_intent_without_a_session_still_moves_and_replaces_once(self) -> None:
        self.without_a_retained_session()
        self.rework_after_red_review()

        with self.fail_the_red_move():
            with self.assertRaises(OSError):
                self.tick()

        self.assert_red_intent_open_in_validate("review")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "rework-started")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("start_review"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def die_after_red_move(self):
        """A tick that moves the card back to In progress and never records why.

        The red intent is written before the move, so the only saves this refuses are the delivery
        boundary and everything after it.
        """
        real_save = self.runtime.state.save

        def save(payload: dict) -> None:
            if self.reader.show(REF)["state"] == "in_progress":
                raise OSError("dispatcher died after red board move")
            real_save(payload)

        return mock.patch.object(self.runtime.state, "save", save)

    def test_a_crash_after_the_red_gate_move_delivers_the_continuation_it_intended(self) -> None:
        """The Validate handoff of the closed round is never replayed by its own done report."""
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.die_after_red_move():
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        retained = self.record()
        assert retained is not None
        self.assertEqual(
            retained.worker_continuation.stage,
            WorkerContinuationStage.RED_TRANSITION_PENDING,
        )
        self.assertEqual(retained.worker_continuation.phase, "gate")

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-reused-worker")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.host.calls.count("resume_worker"), 1)

    def test_a_crash_after_the_red_review_move_delivers_the_continuation_it_intended(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.tick()  # reviewer up
        self.verdict("red", "needs work", "verdict-red")

        with self.die_after_red_move():
            with self.assertRaises(OSError):
                self.tick()

        retained = self.record()
        assert retained is not None
        self.assertEqual(
            retained.worker_continuation.stage,
            WorkerContinuationStage.RED_TRANSITION_PENDING,
        )
        self.assertEqual(retained.worker_continuation.phase, "review")

        recovered = self.tick()

        self.assertEqual(recovered["action"], "review-red-reused-worker")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.host.calls.count("resume_worker"), 1)

    def die_before_finishing_the_round(self):
        """The delivery is checkpointed and the tick dies before the round it opened is recorded."""

        def die(*_args, **_kwargs):
            raise OSError("dispatcher died after the delivery checkpoint")

        return mock.patch.object(self.runtime, "_finish_retained_worker_resume", die)

    def test_a_confirmed_gate_red_delivery_is_finished_by_the_next_tick(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.die_before_finishing_the_round():
            with self.assertRaises(OSError):
                self.tick()

        confirmed = self.record()
        assert confirmed is not None
        self.assertEqual(
            confirmed.worker_continuation.stage, WorkerContinuationStage.DELIVERY_CONFIRMED
        )
        self.assertEqual(confirmed.attempt_round, 1)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-reused-worker")
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2)
        self.assertEqual(record.worker_continuation.stage, WorkerContinuationStage.NONE)
        self.assertIn("gate red continuation: reused", self.reader.show(REF)["comments"][-1]["body"])

    def test_a_confirmed_review_red_delivery_is_finished_by_the_next_tick(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.rework_after_red_review()

        with self.die_before_finishing_the_round():
            with self.assertRaises(OSError):
                self.tick()

        confirmed = self.record()
        assert confirmed is not None
        self.assertEqual(
            confirmed.worker_continuation.stage, WorkerContinuationStage.DELIVERY_CONFIRMED
        )

        recovered = self.tick()

        self.assertEqual(recovered["action"], "review-red-reused-worker")
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2)
        self.assertIn(
            "review red continuation: reused", self.reader.show(REF)["comments"][-1]["body"]
        )

    def test_a_session_that_lost_its_suspension_during_review_is_replaced_once(self) -> None:
        """The suspension confirmed before the reviewer started is not evidence at delivery time."""
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.tick()  # reviewer up over a confirmed suspended worker
        self.host.retained_worker_alive = False
        self.verdict("red", "needs work", "verdict-red")

        outcome = self.tick()

        self.assertEqual(outcome["action"], "rework-started")
        self.assertEqual(self.host.calls.count("resume_worker"), 0)
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertIn(
            "review red continuation: replacement", self.reader.show(REF)["comments"][-1]["body"]
        )

    def test_a_session_that_lost_its_suspension_before_the_red_gate_is_replaced_once(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]
        self.host.retained_worker_alive = False

        outcome = self.tick()

        self.assertEqual(outcome["action"], "gate-red-rework")
        self.assertEqual(self.host.calls.count("resume_worker"), 0)
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertIn(
            "gate red continuation: replacement", self.reader.show(REF)["comments"][-1]["body"]
        )

    def lose_the_suspension_after_the_send(self):
        """The dispatcher dies between the send and its checkpoint, and the head wakes meanwhile.

        Terminal recovery and an operator both do this: the record's `delivery_pending` says a
        prompt went out to a session that was suspended one tick ago, which is not the same fact as
        that session being suspended now.
        """
        return self.state_dies_after("resume_worker")

    def test_a_pending_delivery_that_lost_its_suspension_is_replaced_once(self) -> None:
        """Recovery asks the heartbeat again instead of resuming on the dead tick's answer."""
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]
        with self.lose_the_suspension_after_the_send():
            with self.assertRaises(OSError):
                self.tick()
        pending = self.record()
        assert pending is not None
        self.assertEqual(pending.worker_continuation.stage, WorkerContinuationStage.DELIVERY_PENDING)
        confirmations = self.host.calls.count("confirm_worker_retained")
        self.host.retained_worker_alive = False

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-rework")
        self.assertGreater(
            self.host.calls.count("confirm_worker_retained"), confirmations,
            "the suspension is confirmed again at the boundary, not inherited from the dead tick",
        )
        self.assertEqual(self.host.calls.count("resume_worker"), 1, "the woken head is not typed into")
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertIn(
            "gate red continuation: replacement", self.reader.show(REF)["comments"][-1]["body"]
        )

    def test_a_pending_review_delivery_that_lost_its_suspension_is_replaced_once(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.rework_after_red_review()
        with self.lose_the_suspension_after_the_send():
            with self.assertRaises(OSError):
                self.tick()
        confirmations = self.host.calls.count("confirm_worker_retained")
        self.host.retained_worker_alive = False

        recovered = self.tick()

        self.assertEqual(recovered["action"], "rework-started")
        self.assertGreater(self.host.calls.count("confirm_worker_retained"), confirmations)
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertIn(
            "review red continuation: replacement", self.reader.show(REF)["comments"][-1]["body"]
        )

    def without_a_retained_session(self) -> None:
        """A round whose worker has no conversation to keep: a one-shot head or a lost pane."""
        self.host.fail_retain_worker_reason = "worker session has no addressable pane to retain"

    def test_a_gate_red_crash_without_a_session_replaces_instead_of_replaying_validate(self) -> None:
        """The red intent is durable even when there is nothing to reuse.

        Without it the record would still say Validate, still name the report that closed the round,
        and the next tick would hand that report to the gate a second time while the card sat In
        progress waiting for a worker nobody launched.
        """
        self.without_a_retained_session()
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.die_after_red_move():
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        stranded = self.record()
        assert stranded is not None
        self.assertEqual(
            stranded.worker_continuation.stage, WorkerContinuationStage.RED_TRANSITION_PENDING
        )
        self.assertFalse(stranded.worker_continuation.retained)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-rework")
        self.assertNotIn("to", recovered, "the closed round's report is never a new completion")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_a_review_red_crash_without_a_session_replaces_instead_of_replaying_validate(self) -> None:
        self.without_a_retained_session()
        self.rework_after_red_review()

        with self.die_after_red_move():
            with self.assertRaises(OSError):
                self.tick()

        stranded = self.record()
        assert stranded is not None
        self.assertEqual(
            stranded.worker_continuation.stage, WorkerContinuationStage.RED_TRANSITION_PENDING
        )
        self.assertFalse(stranded.worker_continuation.retained)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "rework-started")
        self.assertNotIn("to", recovered)
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def assert_replacement_still_owed(self) -> None:
        """The board moved for a red verdict and no head was launched: something must still owe one.

        The launch intent was to take the transition over, and the write that would have made that
        handover durable refused. What is left on disk is the only thing a next tick can read.
        """
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.stored_intent(), {})
        stranded = self.record()
        assert stranded is not None
        self.assertIn(
            stranded.worker_continuation.stage,
            {
                WorkerContinuationStage.RED_TRANSITION_PENDING,
                WorkerContinuationStage.DELIVERY_PENDING,
            },
        )
        self.assertFalse(
            stranded.worker_continuation.retained, "the old session was stopped, not kept"
        )

    def assert_one_replacement_on_the_rework_round(self, action: str) -> None:
        recovered = self.tick()

        self.assertEqual(recovered["action"], action)
        self.assertNotIn("to", recovered, "the closed round's report is never a new completion")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual((record.state, record.attempt_round), ("claimed", 2))
        self.assertIn("red continuation: replacement", self.reader.show(REF)["comments"][-1]["body"])

    def test_a_gate_red_replacement_that_cannot_write_its_intent_keeps_its_transition(self) -> None:
        """A refused handover is not a completed one.

        The intent write is where the red transition is meant to change hands. When it fails the
        transition has gone nowhere, and dropping it from the record would leave the card In
        progress with nothing durable owing it a worker: no rework round, no replacement, and no
        continuation entry on the card.
        """
        self.without_a_retained_session()
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-intent-unwritable")
        self.assert_replacement_still_owed()
        self.assert_one_replacement_on_the_rework_round("gate-red-rework")

    def test_a_review_red_replacement_that_cannot_write_its_intent_keeps_its_transition(self) -> None:
        self.without_a_retained_session()
        self.rework_after_red_review()

        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-intent-unwritable")
        self.assert_replacement_still_owed()
        self.assert_one_replacement_on_the_rework_round("rework-started")

    def test_a_refused_gate_continuation_that_cannot_write_its_intent_keeps_its_transition(self) -> None:
        """The same window, entered from the other fallback: the session refused the continuation."""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-intent-unwritable")
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        self.assert_replacement_still_owed()
        self.assert_one_replacement_on_the_rework_round("gate-red-rework")

    def test_a_refused_review_continuation_that_cannot_write_its_intent_keeps_its_transition(self) -> None:
        self.rework_after_red_review()

        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-intent-unwritable")
        self.assert_replacement_still_owed()
        self.assert_one_replacement_on_the_rework_round("rework-started")

    def test_a_dead_rework_intent_relaunches_inside_the_round_it_reserved(self) -> None:
        """The reservation outlives the head the rework never got.

        The round is the one thing the intent knows and the record does not: the record still
        carries the round the red verdict closed. A dead rework head that gave its reservation back
        would put the relaunch, its routing and its next verdict inside the rejected round.
        """
        self.rework_after_red_review()
        self.host.head_pid = DEAD_PID
        with self.state_dies_after("restart_worker"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual((self.stored_intent()["round"], self.stored_intent()["opens_round"]), (2, True))

        self.host.head_pid = os.getpid()
        # Nothing of that launch is running and the pane inventory cannot see one either, so the
        # ordinary path owns the card again and the wait watchdog respawns its head.
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        self.tick()

        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2, "the rework runs in the round it reserved")
        self.assertEqual(self.stored_intent(), {})
        self.assertEqual(self.host.calls.count("restart_worker"), 2, "one head, once the first died")
        rounds = [
            event["payload"]["attempt"]
            for event in self.runtime.audit.events(REF, kind="routing")
            if event["payload"].get("phase") == "worker"
        ]
        self.assertEqual(rounds[-1], 2, "the respawned head is recorded by the rework's round")

    def test_a_respawn_adoption_stays_inside_its_round(self) -> None:
        """A respawn continues the round it interrupted, so its intent reserves nothing."""
        self.tick()
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        with self.state_dies_after("restart_worker"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertFalse(self.stored_intent()["opens_round"])

        self.tick()

        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 1)

    # worker: a journal that refuses instead of a state plane ------------------

    def test_an_audit_that_refuses_the_claim_launches_no_worker_at_all(self) -> None:
        with self.refuse_audit("-claim-"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.host.prepared, [], "no head may exist that no record can find")
        self.assertEqual(self.stored_intent(), {})
        self.assertIsNone(self.record())

        recovered = self.tick()

        self.assertEqual(recovered["step"], "claim")
        self.assertEqual(self.host.prepared, [REF], "exactly one head, on the retry")

    def test_an_audit_that_refuses_after_the_claim_leaves_the_head_on_the_record(self) -> None:
        """The record's own save is past by then, so the head is findable either way.

        What the refused write costs is the round's routing event, and that is why the intent is
        not spent yet: the next tick adopts the same head and writes it.
        """
        with self.audit_dies_after("prepare_worker"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.host.prepared, [REF])
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "claimed")
        self.assertTrue(record.handle, "the launched head must be on the record")
        self.assertEqual(self.stored_intent()["action"], "claim")

        # The next tick reads a card that already has its head, and starts nothing beside it.
        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.prepared, [REF])
        attempt = self.routing_history()[-1]
        assert attempt.worker is not None
        self.assertEqual(attempt.worker.head, "codex", "the round gets the head that ran it")
        self.assertEqual(self.stored_intent(), {})

    def test_an_audit_that_refuses_after_a_rework_recovers_that_rework_once(self) -> None:
        self.rework_after_red_review()
        with self.audit_dies_after("restart_worker"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.stored_intent()["action"], "review-red-rework")

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual((record.state, record.attempt_round), ("claimed", 2))

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

    # reviewer: a journal that refuses instead of a state plane ---------------

    def test_an_audit_that_refuses_the_launch_request_starts_no_reviewer(self) -> None:
        """The launch request comment is the reviewer's own pre-launch journal write."""
        self.run_to_validate()

        with self.refuse_audit("start-intent"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.host.reviews, [])
        self.assertEqual(self.stored_intent(), {})

        recovered = self.tick()

        self.assertEqual(recovered["step"], "review")
        self.assertEqual(self.host.reviews, [REF], "exactly one reviewer, on the retry")

    def test_an_audit_that_refuses_after_the_review_launch_adopts_that_reviewer(self) -> None:
        """The reviewer's routing write is before the record's save, so the intent is what survives."""
        self.run_to_validate()
        with self.audit_dies_after("start_review"):
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

        # And the adopted reviewer's verdict still lands on the card it was launched for.
        self.verdict("green", "looks good", "verdict-green")
        self.assertEqual(self.tick()["to"], "done")

    # an adopted head belongs to the round's routing history ------------------

    def routing_history(self) -> list:
        return routing_attempts(TaskAudit(self.data_dir).events(REF, kind="routing"))

    def test_an_adopted_worker_is_recorded_as_the_round_that_ran_it(self) -> None:
        """The head an interrupted tick launched is a head that ran, so the round has to name it.

        The tick that started it died before writing its routing event, and nothing after adoption
        writes one either: without this the round's verdict names only the reviewer, and the
        history reads as a round nobody worked.
        """
        with self.state_dies_after("prepare_worker"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.tick()["action"], "worker-launch-adopted")

        attempt = self.routing_history()[-1]
        self.assertEqual(attempt.attempt, 1)
        assert attempt.worker is not None
        # The launch snapshot the interrupted tick fixed on disk, not a fresh read of a registry
        # that may have moved since.
        self.assertEqual((attempt.worker.role, attempt.worker.head), ("worker", "codex"))

        # And the verdict the round ends on carries that worker beside its reviewer.
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")
        self.verdict("green", "looks good", "verdict-green")
        self.assertEqual(self.tick()["to"], "done")

        attempt = self.routing_history()[-1]
        assert attempt.worker is not None and attempt.reviewer is not None
        self.assertEqual(attempt.outcome, "green")
        self.assertEqual((attempt.worker.head, attempt.reviewer.head), ("codex", "codex-reviewer"))

    def test_an_adopted_reviewer_is_recorded_as_the_head_that_judged_the_round(self) -> None:
        self.run_to_validate()
        with self.state_dies_after("start_review"):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.tick()["action"], "review-launch-adopted")

        attempt = self.routing_history()[-1]
        assert attempt.reviewer is not None
        self.assertEqual((attempt.reviewer.role, attempt.reviewer.head), ("reviewer", "codex-reviewer"))

        self.verdict("green", "looks good", "verdict-green")
        self.assertEqual(self.tick()["to"], "done")

        attempt = self.routing_history()[-1]
        assert attempt.worker is not None and attempt.reviewer is not None
        self.assertEqual(attempt.outcome, "green")
        self.assertEqual(attempt.reviewer.head, "codex-reviewer")

    def test_a_journal_that_refuses_an_adopted_head_keeps_the_intent_for_the_next_tick(self) -> None:
        """The routing write is the last thing adoption owes that head, and it can refuse.

        Spending the intent on a round whose history is missing would leave the head with no record
        of its launch at all; keeping it costs one more adoption instead.
        """
        with self.state_dies_after("prepare_worker"):
            with self.assertRaises(OSError):
                self.tick()

        with self.refuse_audit("routing-worker"):
            deferred = self.tick()

        self.assertEqual(deferred["action"], "worker-launch-adopt-deferred")
        self.assertEqual(deferred["status"], "degraded")
        self.assertEqual(self.stored_intent()["role"], "worker", "the intent outlives the refusal")
        self.assertEqual(self.host.prepared, [REF])

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.prepared, [REF], "the retry adopts, it does not relaunch")
        attempt = self.routing_history()[-1]
        assert attempt.worker is not None
        self.assertEqual(attempt.worker.head, "codex")

    # an adopted head has a lifecycle, not only a pid -------------------------

    def adopt_worker(self) -> None:
        """Leave the card with a live worker head that no pane handle points at."""
        with self.state_dies_after("prepare_worker"):
            with self.assertRaises(OSError):
                self.tick()
        self.assertEqual(self.tick()["action"], "worker-launch-adopted")
        record = self.record()
        assert record is not None
        self.assertEqual(record.handle, "", "an adopted head never had a pane recorded")
        self.assertEqual(record.worker_pid_file, pid_file_path("worker", REF))

    def adopt_reviewer(self) -> None:
        self.run_to_validate()
        with self.state_dies_after("start_review"):
            with self.assertRaises(OSError):
                self.tick()
        self.assertEqual(self.tick()["action"], "review-launch-adopted")
        record = self.record()
        assert record is not None
        self.assertEqual(record.review_handle, "")
        self.assertEqual(record.review_pid_file, pid_file_path("review", REF))

    def head_alive(self, kind: str) -> bool:
        return Path(pid_file_path(kind, REF)).exists()

    def test_an_adopted_worker_is_stopped_before_the_reviewer_takes_the_checkout(self) -> None:
        """The freeze goes by heartbeat when there is no pane to close.

        Without it the adopted worker keeps editing the tree the reviewer was launched to judge,
        and the verdict describes a checkout that no longer exists.
        """
        self.adopt_worker()
        self.report_done()

        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")

        self.assertIn("stop_head:worker", self.host.calls)
        self.assertFalse(self.head_alive("worker"), "the adopted worker must be stopped")
        record = self.record()
        assert record is not None
        self.assertEqual((record.worker_pid_file, record.handle), ("", ""))

    def test_an_adopted_reviewer_is_stopped_by_the_red_verdict_it_returns(self) -> None:
        self.adopt_reviewer()
        self.verdict("red", "needs work", "verdict-red")

        rework = self.tick()

        self.assertEqual(rework["action"], "rework-started")
        self.assertIn("stop_review", self.host.calls)
        self.assertFalse(self.head_alive("review"), "the adopted reviewer must be stopped")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual(record.review_pid_file, "")

    def test_an_adopted_reviewer_is_stopped_before_its_watchdog_respawns_it(self) -> None:
        self.adopt_reviewer()
        # The pane inventory cannot see a head that was adopted without a handle, which is exactly
        # the reading that used to respawn a reviewer beside a live one.
        self.host.review_status_result = {
            "known": True, "live": False, "reason": "missing-terminal"
        }

        respawned = self.tick()

        self.assertEqual(respawned["action"], "review-respawned")
        self.assertFalse(
            self.head_alive("review") and self.host.head_pid is None,
            "the adopted reviewer is stopped before its replacement",
        )
        self.assertIn("stop_review", self.host.calls)
        self.assertEqual(self.host.reviews, [REF, REF], "one replacement, not one beside a live head")

    def test_a_reviewer_with_no_heartbeat_is_stopped_through_its_workspace(self) -> None:
        """A raw command override writes no heartbeat, so nothing names that reviewer.

        Past the grace window the launch counts as one that left nothing running, and the
        replacement used to open beside a reviewer that was in fact still up. The workspace is the
        only identity left, so that is what the stop goes through.
        """
        self.run_to_validate()
        self.host.head_pid = None
        with self.state_dies_after("start_review"):
            with self.assertRaises(OSError):
                self.tick()
        self.age_intent(initial_output_stall_seconds() + 60)
        self.host.calls.clear()

        self.host.head_pid = os.getpid()
        self.host.review_running_result = False
        restarted = self.tick()

        self.assertEqual(restarted["step"], "review")
        self.assertIn("stop_workspace", self.host.calls)
        self.assertLess(
            self.host.calls.index("stop_workspace"),
            self.host.calls.index("start_review"),
            "whatever the lost launch left is stopped before the replacement opens",
        )
        self.assertEqual(self.host.reviews, [REF, REF])

    # a bring-up that failed with its terminal already open -------------------

    def test_a_worker_bring_up_that_left_a_terminal_keeps_its_intent(self) -> None:
        """`HeadLaunchAborted` is not "no head exists", so the card is neither blocked nor dropped."""
        self.host.fail_prepare_error = HeadLaunchAborted(
            "prompt delivery failed; head terminal stop failed: orca refused",
            handle="term:leftover",
            workspace=str(self.data_dir / "workspaces" / f"{REF}-pilot"),
            pid_file=pid_file_path("worker", REF),
        )

        outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-aborted")
        self.assertEqual(outcome["status"], "degraded")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress", "the card is not blocked")
        intent = self.stored_intent()
        self.assertEqual((intent["role"], intent["handle"], intent["aborted"]),
                         ("worker", "term:leftover", True))

        # And the head that terminal is running is adopted, pane included, rather than doubled.
        self.host.fail_prepare_error = None
        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("prepare_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual(record.handle, "term:leftover")

    def test_a_reviewer_whose_worker_will_not_freeze_keeps_its_intent(self) -> None:
        """The reviewer pane is up and the worker would not go: neither head may be forgotten."""
        self.run_to_validate()
        self.host.fail_freeze_worker_reason = "orca refused to close the worker pane"

        outcome = self.tick()

        self.assertEqual(outcome["action"], "review-launch-aborted")
        self.assertEqual(self.reader.show(REF)["state"], "validate", "the card is not blocked")
        self.assertIsNotNone(self.record(), "the record is the only pointer to that reviewer")
        intent = self.stored_intent()
        self.assertEqual((intent["role"], intent["aborted"]), ("review", True))
        self.assertTrue(intent["handle"])

        # Recovery retries the freeze; while it keeps failing, no second reviewer is started.
        stuck = self.tick()

        self.assertEqual(stuck["action"], "worker-stop-unconfirmed")
        self.assertEqual(self.host.reviews, [REF])

        self.host.fail_freeze_worker_reason = ""
        adopted = self.tick()

        self.assertEqual(adopted["action"], "review-launch-adopted")
        self.assertEqual(self.host.reviews, [REF])
        record = self.record()
        assert record is not None
        self.assertEqual((record.handle, record.worker_pid_file), ("", ""))
        self.assertFalse(self.head_alive("worker"), "the freeze is what recovery had to finish")

    # a failure raised after the head is up, over an intent the tick still holds -----------------

    def pane_identity_fails(self):
        """Orca answers the bring-up and then refuses the inventory the pane leaf is read from.

        The narrow window the record used to lose a head in: the launch succeeded, and the tick
        failed on the next thing it did with it. Anything that treats that as a launch that did not
        happen blocks the card and drops the record over a worker that is running.
        """
        return mock.patch.object(
            self.host, "pane_leaf", mock.Mock(side_effect=HostError("orca terminal list failed"))
        )

    def test_a_claim_whose_pane_identity_fails_keeps_the_worker_it_launched(self) -> None:
        with self.pane_identity_fails():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-aborted")
        self.assertEqual(self.host.prepared, [REF])
        self.assertEqual(self.reader.show(REF)["state"], "in_progress", "the card is not blocked")
        self.assertIsNotNone(self.record(), "the record is the only pointer to that worker")
        intent = self.stored_intent()
        self.assertEqual((intent["role"], intent["aborted"]), ("worker", True))
        self.assertTrue(intent["handle"], "the pane the launch did report is kept")

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.prepared, [REF], "the live head must not be launched twice")

    def test_a_rework_whose_pane_identity_fails_keeps_the_worker_it_launched(self) -> None:
        self.rework_after_red_review()

        with self.pane_identity_fails():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-aborted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertIsNotNone(self.record())
        self.assertEqual(self.stored_intent()["action"], "review-red-rework")

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual((record.state, record.attempt_round), ("claimed", 2))

    def test_a_respawn_whose_pane_identity_fails_keeps_the_worker_it_launched(self) -> None:
        self.tick()
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}

        with self.pane_identity_fails():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-aborted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertIsNotNone(self.record(), "a respawned head may not lose its record")
        self.assertTrue(self.head_alive("worker"))
        self.assertEqual(self.stored_intent()["action"], "worker-respawn")

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_a_gate_red_rework_whose_pane_identity_fails_keeps_its_worker(self) -> None:
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.pane_identity_fails():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-aborted")
        self.assertIsNotNone(self.record())
        self.assertEqual(self.stored_intent()["action"], "gate-red-rework")

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_a_review_bring_up_that_fails_over_its_own_heartbeat_keeps_its_intent(self) -> None:
        """An ordinary failure is only believed while the heartbeat agrees with it.

        A reviewer bring-up that got as far as writing a heartbeat left a process behind, whatever
        the host called the failure. Blocking the card and dropping the record there strands it.
        """
        self.run_to_validate()

        def failing_review(task: dict, record: Any):
            self.host.calls.append("start_review")
            self.host.reviews.append(task["ref"])
            self.host._write_head_pid("review", task["ref"])
            raise HostError("orca terminal rename failed")

        with mock.patch.object(self.host, "start_review", failing_review):
            outcome = self.tick()

        self.assertEqual(outcome["action"], "review-launch-aborted")
        self.assertEqual(self.reader.show(REF)["state"], "validate", "the card is not blocked")
        self.assertIsNotNone(self.record(), "the record is the only pointer to that reviewer")
        self.assertEqual(self.stored_intent()["role"], "review")
        self.assertTrue(self.head_alive("review"))

        adopted = self.tick()

        self.assertEqual(adopted["action"], "review-launch-adopted")
        self.assertEqual(self.host.reviews, [REF], "no second reviewer beside the live one")

    def test_a_bring_up_that_could_not_hold_its_workspace_leaves_no_intent(self) -> None:
        """The intent names a workspace before the host answers, so the host must land on it.

        A worktree created somewhere else is refused by `_create_workspace` and reaches the tick as
        an ordinary bring-up failure: nothing is running, so nothing may be adopted against a path
        that would send every later review, stop and teardown to the wrong checkout.
        """
        self.host.fail_prepare_reason = "orca placed the worker workspace at /elsewhere, not /intended"

        outcome = self.tick()

        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(self.stored_intent(), {})
        self.assertIsNone(self.record())

        self.host.fail_prepare_reason = ""
        self.host.calls.clear()

        # And the retry after the operator requeues it launches exactly one head, on the path the
        # fresh intent names.
        self.writer.move(
            role="po", actor="operator", reference=REF, target="ready",
            reason="requeued", request_id="requeue-after-workspace-mismatch",
        )
        recovered = self.tick()

        self.assertEqual(recovered["step"], "claim")
        self.assertEqual(self.host.prepared, [REF])
        record = self.record()
        assert record is not None
        self.assertEqual(record.workspace, self.host.restore_workspace({}, f"{REF}-pilot"))

    # a stop the host would not confirm ---------------------------------------

    def test_a_worker_respawn_after_an_unconfirmed_stop_starts_nothing(self) -> None:
        self.tick()
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        self.host.fail_stop_head_reason = "orca terminal stop failed"

        outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-stop-unconfirmed")
        self.assertEqual(outcome["status"], "degraded")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.stored_intent(), {}, "no launch was even fixed on disk")

        # Once the stop goes through, the respawn happens exactly once.
        self.host.fail_stop_head_reason = ""
        respawned = self.tick()

        self.assertEqual(respawned["action"], "worker-respawned")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_a_reviewer_respawn_after_an_unconfirmed_stop_starts_nothing(self) -> None:
        self.run_to_validate()
        self.tick()  # reviewer up
        self.host.review_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        self.host.fail_stop_review_reason = "orca refused to close the reviewer pane"

        outcome = self.tick()

        self.assertEqual(outcome["action"], "review-stop-unconfirmed")
        self.assertEqual(self.host.reviews, [REF], "no reviewer opens beside one that may be live")

        self.host.fail_stop_review_reason = ""
        respawned = self.tick()

        self.assertEqual(respawned["action"], "review-respawned")
        self.assertEqual(self.host.reviews, [REF, REF])

    def test_a_red_verdict_whose_reviewer_will_not_stop_leaves_the_card_in_validate(self) -> None:
        self.run_to_validate()
        self.tick()
        self.verdict("red", "needs work", "verdict-red")
        self.host.fail_stop_review_reason = "orca refused to close the reviewer pane"

        outcome = self.tick()

        self.assertEqual(outcome["action"], "review-stop-unconfirmed")
        self.assertEqual(self.reader.show(REF)["state"], "validate", "the bounce did not happen")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

        self.host.fail_stop_review_reason = ""
        rework = self.tick()

        self.assertEqual(rework["action"], "rework-started")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    # liveness ---------------------------------------------------------------

    def test_a_heartbeat_that_never_appears_reads_as_dead_only_past_the_window(self) -> None:
        intent = {"pid_file": str(self.data_dir / "nothing.pid"), "at": 1000.0}

        inside = launch_intent_liveness(intent, now=1000.0 + initial_output_stall_seconds() - 1)
        outside = launch_intent_liveness(intent, now=1000.0 + initial_output_stall_seconds() + 1)

        self.assertEqual((inside["alive"], inside["pid_known"]), (True, False))
        self.assertEqual((outside["alive"], outside["pid_known"]), (False, False))


class HostLaunchContourTests(unittest.TestCase):
    """The host half of the contour: what a bring-up promises, and what a stop confirms.

    Everything above this reads the host's answers. These read the answers themselves, because the
    two ambiguous outcomes only exist down here: a workspace Orca put somewhere else than the
    intent already names, and a failure raised after the head's terminal was created.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        self.host = CommandHostRuntime(FakeCatalog(), self.data_dir, mode="real")  # type: ignore[arg-type]
        self.json_calls: list[list[str]] = []

    def run_json(self, answers: dict[str, Any]):
        """Stand in for the Orca CLI: the first word after `orca` picks the answer."""

        def call(command: list[str]) -> dict:
            self.json_calls.append(command)
            for key, answer in answers.items():
                if all(word in command for word in key.split()):
                    if isinstance(answer, Exception):
                        raise answer
                    return answer
            return {"ok": True}

        return mock.patch.object(self.host, "_run_json", call)

    def pid_file(self, contents: str | None) -> str:
        path = self.data_dir / "head.pid"
        if contents is not None:
            path.write_text(contents, encoding="utf-8")
        return str(path)

    # the workspace the intent already names ---------------------------------

    def test_a_worker_workspace_orca_places_elsewhere_is_refused(self) -> None:
        """The observer's invariant, applied to the worker (secretary-820).

        The launch intent recorded `expected` before this call, and a tick that dies right after it
        can only find the head through that path. A worktree somewhere else is a deferred bring-up
        with a readable reason, not a head every later stop and review would address in the wrong
        checkout.
        """
        repo = self.data_dir / "repo"
        repo.mkdir()
        self.host.catalog.binding = lambda project: {"repo": str(repo)}  # type: ignore[assignment]
        answers = {"worktree create": {"worktree": {"path": str(self.data_dir / "elsewhere")}}}

        with mock.patch.object(self.host, "_run", lambda *a, **k: None):
            with self.run_json(answers):
                with self.assertRaisesRegex(HostError, "not "):
                    self.host._create_workspace(
                        "secretary", "w1", "main", expected=str(self.data_dir / "intended")
                    )

                answers["worktree create"]["worktree"]["path"] = str(self.data_dir / "intended")

                self.assertEqual(
                    self.host._create_workspace(
                        "secretary", "w1", "main", expected=str(self.data_dir / "intended")
                    ),
                    str(self.data_dir / "intended"),
                )

    # a failure raised after the terminal exists -----------------------------

    def test_a_pane_that_closes_leaves_nothing_of_the_bring_up(self) -> None:
        """Confirmed gone, so the caller may treat it as a launch that did not happen."""
        with mock.patch.object(secretary_dispatcher, "_close_tui_terminal_strict", lambda *a, **k: None):
            self.host._close_launched_pane("term:1", self.pid_file(str(DEAD_PID)))

    def test_a_pane_that_will_not_close_and_a_head_nothing_can_read_is_ambiguous(self) -> None:
        """No heartbeat and a refused close: the head cannot be reported as gone."""
        refuse = mock.Mock(side_effect=HostError("tab_not_found"))

        with mock.patch.object(secretary_dispatcher, "_close_tui_terminal_strict", refuse):
            with self.assertRaisesRegex(HostError, "head terminal close failed"):
                self.host._close_launched_pane("term:1", self.pid_file(None))

    def test_a_refused_close_over_a_head_that_is_provably_gone_is_not_ambiguous(self) -> None:
        """Orca answers `tab_not_found` for every pane it never gave a UI tab, which is every pane
        a dispatcher-launched head gets on a headless serve. The heartbeat, not the refusal, is
        what says whether the head is still there."""
        refuse = mock.Mock(side_effect=HostError("tab_not_found"))

        with mock.patch.object(secretary_dispatcher, "_close_tui_terminal_strict", refuse):
            self.host._close_launched_pane("term:1", self.pid_file(str(DEAD_PID)))

    @contextlib.contextmanager
    def delivery_fails(self, close: Exception | None):
        """A bring-up whose head takes its terminal but never its prompt."""
        launch = HeadLaunch("run-worker", prompt_after_start=True)

        class Catalog:
            def head_launch(self, *args: Any, **kwargs: Any) -> HeadLaunch:
                return launch

        with mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir)}):
            with mock.patch.object(self.host, "catalog", Catalog()):
                with mock.patch.object(self.host, "_create_terminal", lambda *a, **k: "term:1"):
                    with mock.patch.object(self.host, "_launched", lambda *a, **k: "launched"):
                        with mock.patch.object(
                            secretary_dispatcher, "_deliver_tui_prompt",
                            mock.Mock(side_effect=TuiDeliveryError("the head never took the prompt")),
                        ):
                            with mock.patch.object(
                                secretary_dispatcher, "_close_tui_terminal_strict",
                                mock.Mock(side_effect=close) if close else mock.Mock(),
                            ):
                                yield

    def launch_worker(self):
        return self.host._launch(
            str(self.data_dir), "title", "codex", "TASK.md",
            role="worker", env_name="SECRETARY_UNSET_COMMAND",
            task={"ref": REF, "project": "secretary"},
        )

    def test_a_delivery_failure_over_a_pane_that_stays_up_aborts_the_launch(self) -> None:
        """The whole point of `HeadLaunchAborted`: the caller keeps its intent instead of blocking."""
        with self.delivery_fails(close=HostError("orca refused")):
            with self.assertRaises(HeadLaunchAborted) as caught:
                self.launch_worker()
            expected_pid_file = pid_file_path("worker", REF)

        self.assertEqual(caught.exception.handle, "term:1")
        self.assertEqual(caught.exception.pid_file, expected_pid_file)

    def test_a_delivery_failure_whose_pane_goes_stays_an_ordinary_failure(self) -> None:
        """The other half: nothing is left running, so the caller may block the card as before."""
        with self.delivery_fails(close=None):
            with self.assertRaises(HostError) as caught:
                self.launch_worker()

        self.assertNotIsInstance(caught.exception, HeadLaunchAborted)

    # a split whose label will not stick -------------------------------------

    def split_answers(self, rename: Exception) -> dict[str, Any]:
        return {
            "terminal split": {"split": {"handle": "term:review"}},
            "terminal rename": rename,
        }

    def test_a_split_whose_pane_will_not_close_is_ambiguous(self) -> None:
        """The reviewer's pane exists from the split on, so a failed rename is not "no head".

        The cleanup decides which failure this is, and a close the host will not confirm leaves a
        reviewer running: it goes back as `HeadLaunchAborted` with that pane, so the caller keeps
        its launch intent instead of blocking the card and dropping the record over a live head.
        """
        answers = self.split_answers(HostError("orca rename failed"))
        refuse = mock.Mock(side_effect=HostError("tab_not_found"))

        with self.run_json(answers):
            with mock.patch.object(secretary_dispatcher, "_close_tui_terminal_strict", refuse):
                with self.assertRaises(HeadLaunchAborted) as caught:
                    self.host._split_pane(
                        "term:worker", "title", "run-review",
                        workspace=str(self.data_dir), pid_file=self.pid_file(None),
                    )

        self.assertEqual(caught.exception.handle, "term:review")
        self.assertEqual(caught.exception.workspace, str(self.data_dir))

    def test_a_split_whose_pane_is_confirmed_gone_stays_an_ordinary_failure(self) -> None:
        """The other half: the head is provably not there, so the caller may block the card."""
        answers = self.split_answers(HostError("orca rename failed"))
        refuse = mock.Mock(side_effect=HostError("tab_not_found"))

        with self.run_json(answers):
            with mock.patch.object(secretary_dispatcher, "_close_tui_terminal_strict", refuse):
                with self.assertRaises(HostError) as caught:
                    self.host._split_pane(
                        "term:worker", "title", "run-review",
                        workspace=str(self.data_dir), pid_file=self.pid_file(str(DEAD_PID)),
                    )

        self.assertNotIsInstance(caught.exception, HeadLaunchAborted)

    # a stop that is not confirmed -------------------------------------------

    def test_a_head_that_ignores_every_signal_is_not_reported_as_stopped(self) -> None:
        pid_file = self.pid_file(str(os.getpid()))

        with mock.patch.object(self.host, "_signal_head", lambda *a: None):
            with mock.patch.object(secretary_dispatcher, "HEAD_STOP_GRACE_SECONDS", 0.05):
                with self.assertRaisesRegex(HostError, "still running after stop"):
                    self.host._confirm_head_process_gone(pid_file)

    def test_a_head_with_no_pane_is_stopped_through_its_heartbeat(self) -> None:
        """The shape every adopted head has: no handle, only a pid."""
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(head.kill)
        record = DispatcherRecord(
            worker="w1", workspace=str(self.data_dir), handle="", head="codex",
            review_head="codex-reviewer", attempt_id="a1", comment_baseline=0, review_baseline=0,
            state="claimed", claimed_at=0.0, worker_pid_file=self.pid_file(str(head.pid)),
        )

        self.host.stop_head(record, "worker")

        self.assertIsNotNone(head.poll(), "the head must actually be gone")
        self.assertFalse(Path(record.worker_pid_file).exists())

    def test_a_stopped_head_is_woken_before_its_graceful_stop(self) -> None:
        """SIGTERM is pending while stopped, so handoff must SIGCONT first."""
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(head.kill)
        record = DispatcherRecord(
            worker="w1", workspace=str(self.data_dir), handle="", head="codex",
            review_head="codex-reviewer", attempt_id="a1", comment_baseline=0, review_baseline=0,
            state="claimed", claimed_at=0.0, worker_pid_file=self.pid_file(str(head.pid)),
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.RETAINED,
                retained_at=time.time(),
            ),
        )
        os.kill(head.pid, signal.SIGSTOP)

        self.host.stop_head(record, "worker")

        self.assertIsNotNone(head.poll(), "a retained head must exit without SIGKILL grace")

    def test_retention_stops_the_head_process_group(self) -> None:
        """A helper started by a worker is frozen with the worker, not left writing alone."""
        child_file = self.data_dir / "child.pid"
        head = subprocess.Popen([
            "setsid", "sh", "-c", f"sleep 30 & echo $! > {child_file}; wait",
        ])
        def reap_group() -> None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(head.pid), signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                head.wait(timeout=1)
        self.addCleanup(reap_group)
        for _ in range(50):
            if child_file.exists():
                break
            time.sleep(0.01)
        child = int(child_file.read_text(encoding="utf-8"))
        self.addCleanup(lambda: os.kill(child, signal.SIGKILL))
        record = DispatcherRecord(
            worker="w1", workspace=str(self.data_dir), handle="term:worker", head="codex",
            review_head="codex-reviewer", attempt_id="a1", comment_baseline=0, review_baseline=0,
            state="claimed", claimed_at=0.0, worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "codex", "codex_mode": "tui", "head": "codex"},
        )

        self.host.retain_worker(record)

        status = ""
        for _ in range(50):
            status = Path(f"/proc/{child}/status").read_text(encoding="utf-8")
            if "State:\tT" in status:
                break
            time.sleep(0.01)
        self.assertIn("State:\tT", status)

    def test_claude_retained_worker_rewrites_task_before_delivering_rework(self) -> None:
        """Claude's interactive pane is reusable just like Codex TUI."""
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(head.kill)
        record = DispatcherRecord(
            worker="w1", workspace=str(self.data_dir), handle="term:worker", head="claude-opus",
            review_head="codex-reviewer", attempt_id="a1", comment_baseline=0, review_baseline=3,
            state="claimed", claimed_at=0.0, worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "claude", "head": "claude-opus"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="gate",
                retained_at=time.time(),
                sent_at=time.time(),
            ),
        )
        os.kill(head.pid, signal.SIGSTOP)
        time.sleep(0.05)
        calls: list[list[str]] = []
        task_at_delivery: list[str] = []

        def run_json(command: list[str]) -> dict:
            calls.append(command)
            if command[2] == "send":
                task_at_delivery.append((self.data_dir / "TASK.md").read_text())
            if command[2] == "read":
                return {"terminal": {"tail": ["✻ Thinking… (esc to interrupt)"]}}
            return {}

        with mock.patch.object(self.host, "_run_json", run_json), \
             mock.patch("secretary.dispatcher_tui.latest_claude_user_turn_for", return_value=1.0):
            self.host.resume_worker({"ref": REF, "project": "secretary", "workspace": {}}, record)

        self.assertIn("worker-report-done-secretary-510-pilot-3", task_at_delivery[0])
        self.assertTrue(any(command[2] == "send" for command in calls))

    def test_a_running_retained_claude_replays_delivery_after_a_crash_before_send(self) -> None:
        """SIGCONT alone is not a delivered continuation.

        The first call models a dispatcher dying in that boundary. The recovery call sees a
        running but idle provider, waits for its TUI to settle, sends the prompt and confirms the
        turn rather than treating process liveness as delivery evidence.
        """
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(head.kill)
        record = DispatcherRecord(
            worker="w1", workspace=str(self.data_dir), handle="term:worker", head="claude-opus",
            review_head="codex-reviewer", attempt_id="a1", comment_baseline=0, review_baseline=3,
            state="claimed", claimed_at=0.0, worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "claude", "head": "claude-opus"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="gate",
                retained_at=time.time(),
                sent_at=time.time(),
            ),
        )
        os.kill(head.pid, signal.SIGSTOP)
        time.sleep(0.05)
        real_signal = self.host._signal_head

        class DispatcherDied(BaseException):
            pass

        def die_after_continuing(pid_file: str, signal_number: int) -> None:
            real_signal(pid_file, signal_number)
            raise DispatcherDied()

        with mock.patch.object(self.host, "_signal_head", die_after_continuing):
            with self.assertRaises(DispatcherDied):
                self.host.resume_worker({"ref": REF, "project": "secretary", "workspace": {}}, record)

        calls: list[list[str]] = []
        sent = False

        def run_json(command: list[str]) -> dict:
            nonlocal sent
            calls.append(command)
            if command[2] == "send":
                sent = True
            if command[2] == "read":
                return {"terminal": {"tail": ["✻ Thinking… (esc to interrupt)"] if sent else [""]}}
            return {}

        def latest_turn(_workspace: Path, _since: float) -> float | None:
            return 1.0 if sent else None

        with mock.patch.object(self.host, "_run_json", run_json), \
             mock.patch(
                 "secretary.dispatcher_tui.latest_claude_user_turn_for",
                 side_effect=latest_turn,
             ):
            self.host.resume_worker({"ref": REF, "project": "secretary", "workspace": {}}, record)

        self.assertFalse(secretary_dispatcher._head_process_status(record.worker_pid_file).get("stopped"))
        self.assertTrue(any(command[2] == "wait" for command in calls))
        self.assertTrue(any(command[2] == "send" for command in calls))

    def test_a_running_retained_claude_recovers_from_its_durable_user_turn(self) -> None:
        """A Claude JSONL user record proves delivery after a crash without terminal guessing."""
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(head.kill)
        sent_at = time.time() - 1
        record = DispatcherRecord(
            worker="w1", workspace=str(self.data_dir), handle="term:worker", head="claude-opus",
            review_head="codex-reviewer", attempt_id="a1", comment_baseline=0, review_baseline=3,
            state="claimed", claimed_at=0.0, worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "claude", "head": "claude-opus"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="gate",
                retained_at=sent_at,
                sent_at=sent_at,
            ),
        )
        projects = self.data_dir / "claude-projects"
        session = projects / str(self.data_dir.resolve()).replace("/", "-") / "session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            json.dumps({"type": "user", "timestamp": "2099-01-02T03:04:05Z"}) + "\n",
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        with mock.patch.dict(os.environ, {"SECRETARY_CLAUDE_PROJECTS": str(projects)}), \
             mock.patch.object(self.host, "_run_json", lambda command: calls.append(command) or {}):
            self.host.resume_worker({"ref": REF, "project": "secretary", "workspace": {}}, record)

        self.assertEqual(calls, [])

    def test_a_confirmed_retained_continuation_is_not_delivered_twice_on_recovery(self) -> None:
        """A crash after checkpointing delivery must leave the active worker alone."""
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(head.kill)
        record = DispatcherRecord(
            worker="w1", workspace=str(self.data_dir), handle="term:worker", head="claude-opus",
            review_head="codex-reviewer", attempt_id="a1", comment_baseline=0, review_baseline=3,
            state="claimed", claimed_at=0.0, worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "claude", "head": "claude-opus"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_CONFIRMED,
                phase="gate",
                retained_at=time.time(),
                sent_at=time.time(),
            ),
        )
        calls: list[list[str]] = []

        with mock.patch.object(self.host, "_run_json", lambda command: calls.append(command) or {}):
            self.host.resume_worker({"ref": REF, "project": "secretary", "workspace": {}}, record)

        self.assertEqual(calls, [])

    def test_dead_or_missing_retained_worker_refuses_continuation(self) -> None:
        record = DispatcherRecord(
            worker="w1", workspace=str(self.data_dir / "missing"), handle="term:worker", head="claude-opus",
            review_head="codex-reviewer", attempt_id="a1", comment_baseline=0, review_baseline=0,
            state="claimed", claimed_at=0.0, worker_pid_file=self.pid_file(str(DEAD_PID)),
            worker_run={"adapter": "claude"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="gate",
                retained_at=time.time(),
                sent_at=time.time(),
            ),
        )

        with self.assertRaisesRegex(HostError, "session exited"):
            self.host.resume_worker({"ref": REF, "project": "secretary", "workspace": {}}, record)

    def test_a_stopped_retained_worker_with_no_workspace_refuses_continuation(self) -> None:
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(head.kill)
        record = DispatcherRecord(
            worker="w1", workspace=str(self.data_dir / "missing"), handle="term:worker", head="claude-opus",
            review_head="codex-reviewer", attempt_id="a1", comment_baseline=0, review_baseline=0,
            state="claimed", claimed_at=0.0, worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "claude"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="gate",
                retained_at=time.time(),
                sent_at=time.time(),
            ),
        )
        os.kill(head.pid, signal.SIGSTOP)
        time.sleep(0.05)

        with self.assertRaisesRegex(HostError, "workspace is missing"):
            self.host.resume_worker({"ref": REF, "project": "secretary", "workspace": {}}, record)

    def test_a_head_nothing_names_cannot_be_reported_as_stopped(self) -> None:
        record = DispatcherRecord(
            worker="w1", workspace=str(self.data_dir), handle="", head="codex",
            review_head="codex-reviewer", attempt_id="a1", comment_baseline=0, review_baseline=0,
            state="claimed", claimed_at=0.0,
        )

        with self.assertRaisesRegex(HostError, "neither a pane handle nor a pid heartbeat"):
            self.host.stop_head(record, "worker")


class ProductionLaunchIntentTests(unittest.TestCase):
    """The same contour under the production tick, where records outlive one card's cycle.

    Two things only exist here: the reconciliation pass, which removes the records of cards the
    board has taken out of the active cycle, and the pipeline freeze. Both used to walk past a head
    that had no pane handle — the shape every head adopted from a launch intent has.
    """

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
        self.runtime.state.save({
            "version": 1,
            "phase": "cutover_committed",
            "pilot_ref": REF,
            "old_owner_paused": True,
            "records": {},
        })

    # fixtures ---------------------------------------------------------------

    def tick(self) -> dict:
        return self.runtime.production_tick()

    def actions(self, result: dict) -> list[dict]:
        return [action for action in result.get("actions") or []]

    def records(self) -> dict:
        payload = self.runtime.production_state.load()
        return payload.get("records") or {}

    def stored_intent(self) -> dict:
        return dict((self.records().get(REF) or {}).get("launch_intent") or {})

    @contextlib.contextmanager
    def state_dies_after(self, host_method: str):
        real_save = self.runtime.production_state.save
        real_call = getattr(self.host, host_method)
        launched = {"yet": False}

        def save(payload: dict) -> None:
            if launched["yet"]:
                raise OSError("production state is not writable")
            real_save(payload)

        def call(*args, **kwargs):
            result = real_call(*args, **kwargs)
            launched["yet"] = True
            return result

        with mock.patch.object(self.runtime.production_state, "save", save):
            with mock.patch.object(self.host, host_method, call):
                yield

    def leave_a_post_launch_intent(self) -> None:
        """Claim the card and lose the tick right after the worker head came up."""
        with self.state_dies_after("prepare_worker"):
            with self.assertRaises(OSError):
                self.tick()
        self.assertEqual(self.host.prepared, [REF])
        self.assertEqual(self.stored_intent().get("role"), "worker")

    def leave_a_post_launch_review_intent(self) -> None:
        """Take the card to validate and lose the tick right after the reviewer pane came up."""
        self.tick()
        self.report_done()
        self.tick()
        with self.state_dies_after("start_review"):
            with self.assertRaises(OSError):
                self.tick()
        self.assertEqual(self.host.reviews, [REF])
        self.assertEqual(self.stored_intent().get("role"), "review")

    def leave_a_post_launch_rework_intent(self) -> None:
        """Lose the tick right after a red verdict's rework head came up, round 2 reserved."""
        self.leave_a_post_launch_review_intent()
        self.tick()  # the reviewer of the lost tick is adopted
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference=REF, kind="red",
            body="needs work", request_id="verdict-red",
        )
        with self.state_dies_after("restart_worker"):
            with self.assertRaises(OSError):
                self.tick()
        intent = self.stored_intent()
        self.assertEqual((intent["action"], intent["round"], intent["opens_round"]),
                         ("review-red-rework", 2, True))

    def report_done(self, request_id: str = "worker-done") -> None:
        self.writer.report(
            role="worker",
            actor="worker",
            reference=REF,
            kind="done",
            body="done",
            request_id=request_id,
        )

    def move_card(self, target: str, reason: str, request_id: str) -> None:
        self.writer.move(
            role="po",
            actor="operator",
            reference=REF,
            target=target,
            reason=reason,
            request_id=request_id,
        )

    def head_alive(self, kind: str) -> bool:
        return Path(pid_file_path(kind, REF)).exists()

    # reconciliation ---------------------------------------------------------

    def test_a_card_moved_out_of_the_cycle_takes_its_unresolved_head_with_it(self) -> None:
        """The record is the only pointer to that head, so it cannot be dropped over one.

        `_tick_task` never sees this card again — the board has taken it out of the active cycle —
        so reconciliation is the last chance to settle the launch. Removing the record first would
        strand a live worker in the workspace, and a requeue would put a second one in beside it.
        """
        self.leave_a_post_launch_intent()
        self.move_card("blocked", "PO parked it mid-launch", "move-to-blocked")
        self.host.calls.clear()

        result = self.tick()

        reconciled = [a for a in self.actions(result) if a["step"] == "production-reconcile"]
        self.assertEqual([a["action"] for a in reconciled], ["record-removed"])
        self.assertEqual(reconciled[0]["stopped_launch"], "claim")
        self.assertIn("stop_workspace", self.host.calls)
        self.assertFalse(self.head_alive("worker"), "the head of the unresolved launch is gone")
        self.assertNotIn(REF, self.records())

        # And the requeue that follows starts one head, not one beside a survivor. The neighbour
        # card is parked first: one code task per project may be active, and it took the slot the
        # moment this card left the cycle.
        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-neighbor",
            target="issues",
            reason="park the neighbour",
            request_id="park-neighbor",
        )
        self.move_card("ready", "back to the queue", "move-back-to-ready")
        for _ in range(3):
            self.tick()

        self.assertEqual(
            self.host.prepared.count(REF), 2, "one head for the first claim, one for the requeue"
        )

    def test_ready_card_settles_its_unresolved_launch_before_reclaim(self) -> None:
        self.leave_a_post_launch_intent()
        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-neighbor",
            target="issues",
            reason="make the requeue claimable",
            request_id="park-neighbor-for-ready-intent",
        )
        self.move_card("ready", "requeue during bring-up", "ready-with-live-intent")
        self.host.calls.clear()

        result = self.tick()

        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertIn("stop_workspace", self.host.calls)
        self.assertEqual(self.host.prepared.count(REF), 2)
        self.assertEqual(self.stored_intent(), {})

    def test_workspace_only_adopted_record_is_stopped_before_removal(self) -> None:
        self.tick()
        payload = self.runtime.production_state.load()
        record = payload["records"][REF]
        record["handle"] = ""
        record["worker_leaf"] = ""
        record["worker_pid_file"] = ""
        record["review_handle"] = ""
        record["review_leaf"] = ""
        record["review_pid_file"] = ""
        self.runtime.production_state.save(payload)
        self.move_card("blocked", "park the adopted card", "park-workspace-only-record")
        self.host.calls.clear()

        result = self.tick()

        actions = [a for a in self.actions(result) if a["step"] == "production-reconcile"]
        self.assertEqual([a["action"] for a in actions], ["record-removed"])
        self.assertIn("stop_workspace", self.host.calls)
        self.assertNotIn(REF, self.records())

    def test_settled_ready_workspace_is_not_stopped_again_on_later_ticks(self) -> None:
        self.tick()
        payload = self.runtime.production_state.load()
        record = payload["records"][REF]
        record["handle"] = ""
        record["worker_leaf"] = ""
        record["worker_pid_file"] = ""
        record["review_handle"] = ""
        record["review_leaf"] = ""
        record["review_pid_file"] = ""
        self.runtime.production_state.save(payload)
        self.move_card("ready", "park behind the other ready card", "park-workspace-record")
        self.runtime.pause.save({"mode": "drain"})
        self.host.calls.clear()

        self.tick()
        self.tick()

        self.assertEqual(self.host.calls.count("stop_workspace"), 1)
        record = self.records()[REF]
        self.assertTrue(record["workspace_settled"])
        restored = DispatcherRecord.from_json(record)
        self.assertFalse(restored.owns_head())
        self.assertFalse(restored.needs_settling())

    def test_role_identity_without_workspace_is_stopped_before_removal(self) -> None:
        self.tick()
        payload = self.runtime.production_state.load()
        record = payload["records"][REF]
        record["workspace"] = ""
        record["handle"] = ""
        record["worker_leaf"] = ""
        record["worker_pid_file"] = ""
        record["review_handle"] = ""
        record["review_leaf"] = ""
        record["review_pid_file"] = pid_file_path("review", REF)
        self.runtime.production_state.save(payload)
        self.host._write_head_pid("review", REF)
        self.move_card("blocked", "park the identity-only record", "park-identity-only-record")
        self.host.calls.clear()

        result = self.tick()

        actions = [a for a in self.actions(result) if a["step"] == "production-reconcile"]
        self.assertEqual([a["action"] for a in actions], ["record-removed"])
        self.assertIn("stop_head:review", self.host.calls)
        self.assertNotIn(REF, self.records())

    def test_a_stop_the_host_refuses_keeps_the_record_and_its_intent(self) -> None:
        self.leave_a_post_launch_intent()
        self.move_card("blocked", "PO parked it mid-launch", "move-to-blocked")
        self.host.fail_stop_workspace_reason = "orca terminal stop failed"

        result = self.tick()

        reconciled = [a for a in self.actions(result) if a["step"] == "production-reconcile"]
        self.assertEqual([a["action"] for a in reconciled], ["launch-intent-stop-unconfirmed"])
        self.assertEqual(reconciled[0]["status"], "degraded")
        self.assertEqual(self.stored_intent().get("role"), "worker", "the pointer survives")

        # The next tick retries the same stop, and only then lets the record go.
        self.host.fail_stop_workspace_reason = ""

        retried = self.tick()

        actions = [a["action"] for a in self.actions(retried) if a["step"] == "production-reconcile"]
        self.assertEqual(actions, ["record-removed"])
        self.assertNotIn(REF, self.records())

    # a claim that moved under a launch nothing has resolved ------------------

    def claim_moved_to(self, worker: str) -> None:
        """Someone else's claim on the card this record was launched for."""
        self.board.metadata[12]["claim"] = worker

    def test_a_claim_that_moved_under_a_live_launch_stops_its_head_first(self) -> None:
        """The mismatch drops the record, and the intent on it is the only pointer to that head.

        It runs ahead of `_tick_task`, so nothing else will settle the launch: blocking the card
        and removing the record over a live worker leaves it in the checkout, and the requeue that
        follows opens a second one beside it.
        """
        self.leave_a_post_launch_intent()
        self.claim_moved_to("someone-else")
        self.host.calls.clear()

        result = self.tick()

        mismatch = [a for a in self.actions(result) if a.get("step") == "production-recovery"]
        self.assertEqual([a["status"] for a in mismatch], ["blocked"])
        self.assertIn("stop_workspace", self.host.calls)
        self.assertFalse(self.head_alive("worker"), "the head of the unresolved launch is gone")
        self.assertNotIn(REF, self.records())
        self.assertEqual(self.reader.show(REF)["state"], "blocked")

    def test_a_mismatch_over_a_head_that_will_not_stop_keeps_the_record(self) -> None:
        self.leave_a_post_launch_intent()
        self.claim_moved_to("someone-else")
        self.host.fail_stop_workspace_reason = "orca terminal stop failed"

        result = self.tick()

        mismatch = [a for a in self.actions(result) if a.get("step") == "production-recovery"]
        self.assertEqual([a["action"] for a in mismatch], ["launch-intent-stop-unconfirmed"])
        self.assertEqual(mismatch[0]["status"], "degraded")
        self.assertEqual(self.stored_intent().get("role"), "worker", "the pointer survives")
        self.assertTrue(self.head_alive("worker"))
        self.assertEqual(
            self.reader.show(REF)["state"], "in_progress", "the card is not blocked over a live head"
        )

        # Once the host confirms the stop, the mismatch is resolved the ordinary way.
        self.host.fail_stop_workspace_reason = ""

        retried = self.tick()

        mismatch = [a for a in self.actions(retried) if a.get("step") == "production-recovery"]
        self.assertEqual([a["status"] for a in mismatch], ["blocked"])
        self.assertFalse(self.head_alive("worker"))
        self.assertNotIn(REF, self.records())

    # freeze and resume ------------------------------------------------------

    def test_a_freeze_stops_an_adopted_worker_and_resume_brings_back_one_head(self) -> None:
        self.leave_a_post_launch_intent()
        self.assertEqual(
            [a["action"] for a in self.actions(self.tick()) if a.get("pilot_ref") == REF],
            ["worker-launch-adopted"],
        )

        paused = self.runtime.pause_pipeline(
            mode="freeze", actor="operator", reason="maintenance"
        )

        self.assertEqual(paused["stopped_worker"], [REF])
        self.assertFalse(self.head_alive("worker"), "a freeze must reach a head with no handle")

        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertTrue((self.records()[REF] or {})["handle"])

    def test_a_freeze_stops_the_worker_of_a_launch_nothing_has_resolved_yet(self) -> None:
        """Between the host call and the record's save the intent is the only pointer to that head.

        A freeze that looked only at the handle and the stored pid file would find neither, write an
        empty `stopped_worker`, and declare the pipeline stopped over a worker still editing the
        checkout.
        """
        self.leave_a_post_launch_intent()
        self.assertFalse((self.records()[REF] or {})["handle"], "no handle was ever recorded")
        self.assertTrue(self.head_alive("worker"))

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_worker"], [REF])
        self.assertFalse(self.head_alive("worker"), "the head of the unresolved launch is stopped")
        self.assertEqual(self.stored_intent(), {}, "a confirmed stop spends the intent")

        # And the resume puts back one head, from the record the freeze left behind.
        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.host.calls.count("prepare_worker"), 1, "the claim is not redone")

    def test_a_freeze_that_cannot_stop_an_unresolved_launch_keeps_its_intent(self) -> None:
        self.leave_a_post_launch_intent()
        self.host.fail_stop_workspace_reason = "orca terminal stop failed"

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_worker"], [], "an unconfirmed stop is not a stop")
        self.assertTrue(self.head_alive("worker"))
        self.assertEqual(self.stored_intent().get("role"), "worker", "the only pointer survives")

        # The resume launches nothing beside it, and the tick's own recovery still owns that head.
        self.runtime.resume_pipeline(actor="operator")
        self.host.fail_stop_workspace_reason = ""

        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(
            [a["action"] for a in self.actions(self.tick()) if a.get("pilot_ref") == REF],
            ["worker-launch-adopted"],
        )
        self.assertEqual(self.host.prepared, [REF], "still exactly one head")

    def test_a_freeze_stops_the_reviewer_of_a_launch_nothing_has_resolved_yet(self) -> None:
        """The reviewer's window is wider: neither handle nor pid file is on the record yet."""
        self.leave_a_post_launch_review_intent()
        record = self.records()[REF] or {}
        self.assertEqual((record["review_handle"], record["review_pid_file"]), ("", ""))

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_reviewer"], [REF])
        self.assertFalse(self.head_alive("review"), "the reviewer of the lost tick is stopped")
        self.assertEqual(self.stored_intent(), {})

        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.reviews, [REF, REF], "one reviewer at a time, one after resume")

    def test_a_freeze_that_cannot_stop_an_unresolved_reviewer_keeps_its_intent(self) -> None:
        self.leave_a_post_launch_review_intent()
        # The reviewer of that launch wrote its heartbeat, so the intent's identity is its own pane
        # and the stop goes through the reviewer's lifecycle rather than the whole workspace.
        self.host.fail_stop_review_reason = "orca terminal stop failed"

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_reviewer"], [])
        self.assertTrue(self.head_alive("review"))
        self.assertEqual(self.stored_intent().get("role"), "review")

        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.reviews, [REF], "no second reviewer over a head still running")

    def test_a_freeze_that_stops_a_rework_launch_keeps_the_round_it_reserved(self) -> None:
        self.leave_a_post_launch_rework_intent()

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_worker"], [REF])
        self.assertEqual((self.records()[REF] or {})["attempt_round"], 2)

        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual((self.records()[REF] or {})["attempt_round"], 2)

    def test_a_freeze_that_cannot_stop_an_adopted_worker_does_not_relaunch_it(self) -> None:
        self.leave_a_post_launch_intent()
        self.tick()
        self.host.fail_stop_workspace_reason = "orca terminal stop failed"

        paused = self.runtime.pause_pipeline(
            mode="freeze", actor="operator", reason="maintenance"
        )

        self.assertEqual(paused["stopped_worker"], [], "an unconfirmed stop is not a stop")
        self.assertTrue(self.head_alive("worker"))

        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.calls.count("restart_worker"), 0)


if __name__ == "__main__":
    unittest.main()
