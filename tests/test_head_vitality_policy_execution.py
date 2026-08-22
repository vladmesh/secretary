"""Dispatcher-side execution tests for the S1-5 recovery policy.

The policy module is pure; these tests pin what the dispatcher actually *does* with its
intents. The Suspended execution is exercised two ways:

  * through the real runtime fixture for the tick outcomes, the durable comments and the
    "zero destructive host calls" invariants -- including the window-expiry arm aged through
    the persisted rung state;
  * against a REAL stopped child process with a real heartbeat file (the pattern the S1-1
    tests use: ``SIGSTOP`` -> /proc state `T`) for the identity fence itself: a matching
    process is resumed; a heartbeat naming a foreign live process sends nothing at all.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

import secretary.dispatcher as secretary_dispatcher
from secretary.dispatcher_heartbeat import heartbeat_identity
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_watchdog import (
    head_process_status,
    suspension_response_window_seconds,
)
from tests.test_dispatcher import CARD_REF, DispatcherRuntimeFixture
from tests.test_head_vitality_wait_decisions import (
    RUNNING_STATUS,
    STOPPED_STATUS,
)
from triggered_agents.runtime.head import with_pid_heartbeat


class WindowExpiryTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The second rung's bound: past the response window the operator hears, nobody stops."""

    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.tick()  # claim + launch; the worker heartbeat binds a live pid
        self.host.worker_status_result = dict(STOPPED_STATUS)
        # The first suspended tick spends the SIGCONT and opens the response window.
        sent = self.tick()
        self.assertEqual(sent["action"], "worker-sigcont-sent")

    def _age_window(self, seconds: float) -> None:
        """Move the persisted span stamps back so the next decision sees an expired window.

        Both clocks age together -- the reducer's freeze stamp and the policy's span key --
        which is exactly what an operator clock-rewind of a longer-standing suspension does:
        the head has been parked longer, and its rung state belongs to that same span.
        """
        payload = self.runtime.production_state.load()
        record = payload["records"][CARD_REF]
        episode = record["worker_vitality_episode"]
        for name in ("stall_frozen_since", "recovery_span_started_at"):
            if episode.get(name):
                episode[name] -= seconds
        record["worker_vitality_episode"] = episode
        self.runtime.production_state.save(payload)

    def test_inside_the_window_the_head_is_observed_and_untouched(self) -> None:
        outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-suspension-observed")
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertNotIn("stop_head:worker", self.host.calls)

    def test_an_expired_window_escalates_to_the_operator_without_stopping_anything(self) -> None:
        self._age_window(suspension_response_window_seconds() + 60.0)

        outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-suspension-escalated")
        self.assertEqual(outcome["status"], "degraded")
        comments = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict)
            and "past the response window" in str(comment.get("body") or "")
        ]
        self.assertEqual(len(comments), 1, comments)
        self.assertIn("NOT stopped or replaced", comments[0])
        record = self._pilot_record()
        # Zero destructive steps: no respawn, no stop, no workspace teardown.
        self.assertEqual(record["worker_respawns"], 0)
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertNotIn("stop_head:worker", self.host.calls)
        self.assertNotIn("stop_workspace", self.host.calls)
        self.assertNotIn("stop", self.host.calls)
        # The escalation holds for further ticks of the same span instead of repeating.
        again = self.tick()
        self.assertEqual(again["action"], "worker-suspension-observed")
        comments_after = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict)
            and "past the response window" in str(comment.get("body") or "")
        ]
        self.assertEqual(len(comments_after), 1, "the escalation is idempotent per span")

    def test_recovery_after_sigcont_resumes_the_verdict_and_clears_the_rung(self) -> None:
        self.host.worker_status_result = dict(RUNNING_STATUS)
        resumed = self.tick()

        self.assertEqual(resumed["action"], "waiting-worker-report")
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "healthy_quiet")
        self.assertEqual(episode["recovery_rung"], 0)
        self.assertEqual(episode["recovery_span_started_at"], 0.0)


class RealStoppedChildTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The identity fence itself, against a real SIGSTOPed process behind a real heartbeat."""

    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self._processes: list[subprocess.Popen] = []
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        # A stopped process cannot act on a cleanup SIGTERM; resume before killing.
        for proc in self._processes:
            try:
                proc.send_signal(signal.SIGCONT)
            except ProcessLookupError:
                continue
            try:
                proc.terminate()
            except ProcessLookupError:
                continue
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def _live_heartbeat(
        self, name: str, *, run_id: str = "run-real"
    ) -> tuple[str, subprocess.Popen, dict[str, str]]:
        """Launch a real sleeping process behind a real heartbeat file, like a launch does."""
        pid_file = str(Path(self.data_dir) / name)
        identity = heartbeat_identity(
            run_id=run_id, role="worker", task="card:s1-5-real",
        )
        wrapped = with_pid_heartbeat("sleep 30", pid_file, identity=identity)
        proc = subprocess.Popen(["/bin/sh", "-lc", wrapped])
        self._processes.append(proc)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (
            Path(pid_file).exists()
            and head_process_status(pid_file, expected=identity)["state"] == "live-match"
        ):
            time.sleep(0.01)
        return pid_file, proc, identity

    @staticmethod
    def _parked(pid: int) -> bool:
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
                return any(line.startswith("State:") and "\tT" in line for line in handle)
        except OSError:
            return False

    def _wait_parked(self, pid: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._parked(pid):
                return
            time.sleep(0.01)
        self.fail(f"process {pid} never parked in `T`")

    def _wait_resumed(self, pid: int) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not self._parked(pid):
                return True
            time.sleep(0.01)
        return False

    def _record_for(self, pid_file: str, run_id: str) -> DispatcherRecord:
        """A minimal record pointing the worker role at this real heartbeat."""
        return DispatcherRecord(
            worker="pilot", review_head="", attempt_id="", comment_baseline=0,
            review_baseline=0, state="claimed", claimed_at=0.0,
            workspace="", handle="", head="",
            worker_pid_file=pid_file,
            worker_head_run={"run_id": run_id},
        )

    def test_a_matching_stopped_process_is_resumed_by_the_fenced_helper(self) -> None:
        pid_file, proc, _identity = self._live_heartbeat("matched.pid")
        record = self._record_for(pid_file, "run-real")

        proc.send_signal(signal.SIGSTOP)
        self._wait_parked(proc.pid)

        sent = self.runtime._sigcont_head({"ref": "s1-5-real"}, record, kind="worker")

        self.assertTrue(sent)
        self.assertTrue(
            self._wait_resumed(proc.pid),
            "the SIGCONT must have resumed the matched process",
        )
        self.assertFalse(self._parked(proc.pid))

    def test_a_matching_process_is_signalled_once_per_call_and_only_via_sigcont(self) -> None:
        """The helper sends SIGCONT and nothing else: no TERM/KILL can leak from this path.

        Both delivery branches are audited (``killpg`` for a head in its own group,
        ``kill`` for one sharing ours -- this fixture's shell child is the latter).
        """
        pid_file, proc, _identity = self._live_heartbeat("once.pid")
        record = self._record_for(pid_file, "run-real")
        proc.send_signal(signal.SIGSTOP)
        self._wait_parked(proc.pid)

        real_kill = os.kill
        real_killpg = os.killpg
        signalled: list[signal.Signals] = []

        def audit_kill(pid, number):
            # Signal 0 is the heartbeat classifier's existence probe, not a delivery.
            if number == 0:
                return real_kill(pid, number)
            if number != signal.SIGCONT:
                raise AssertionError(
                    f"the recovery path tried to send {number!r}, not SIGCONT"
                )
            signalled.append(number)
            return real_kill(pid, number)

        def audit_killpg(group, number):
            signalled.append(number)
            return real_killpg(group, number)
        with mock.patch.object(secretary_dispatcher.os, "kill", side_effect=audit_kill), \
                mock.patch.object(secretary_dispatcher.os, "killpg", side_effect=audit_killpg):
            self.assertTrue(
                self.runtime._sigcont_head({"ref": "s1-5-real"}, record, kind="worker"),
            )
        self.assertEqual(
            signalled, [signal.SIGCONT],
            f"only SIGCONT may leave the recovery path, saw {signalled}",
        )

    def test_a_foreign_identity_is_never_signalled(self) -> None:
        # The heartbeat names run-real; the record claims another run entirely.
        pid_file, proc, _identity = self._live_heartbeat("foreign.pid", run_id="run-real")
        foreign_record = self._record_for(pid_file, "run-someone-else")
        proc.send_signal(signal.SIGSTOP)
        self._wait_parked(proc.pid)

        sent = self.runtime._sigcont_head({"ref": "s1-5-real"}, foreign_record, kind="worker")

        self.assertFalse(sent, "a mismatched identity must never be resumed")
        self.assertTrue(
            self._parked(proc.pid),
            "the foreign process stayed parked",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
