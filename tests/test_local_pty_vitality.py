"""secretary-1719: a working local-pty worker, retained and then continued, is not read as stalled.

secretary-1703's worker (run a2c07855…, sprint:1459) went `retained` at 01:11:15Z, was continued at
01:15:46Z, and read `suspected_stall` at 01:16:31Z and `confirmed_stall` at 01:21:40Z. Its
supervisor journal shows about a hundred `provider.progressed` events a minute over the same
span, and it reported done at 01:22Z. The watchdog refused the destructive step with
`pid-only-ceiling-unelapsed`: the confirmation was earned with no progress source answering.

The cause: `command_terminal_status` read the provider cursor only for a head it found in Orca's
pane inventory. A local-pty head is never there, so its status was always the lost-pane shape,
with a pid and child processes and no `provider_progress`. The episode aged on the pid alone.
Child processes held it healthy only inside their ceiling, measured from a head-own reference
that never moved. A continuation did not cause the stall. It only made it visible, because the
freeze kept the quiet the pid-only episode had already built up.

These tests go through the production status function (only the Orca inventory and the /proc
probe are stubbed), the production snapshot builder and the production reducer, tick by tick.
"""

from __future__ import annotations

import unittest
from unittest import mock

from secretary.dispatch import review as dispatcher_review
from secretary.dispatch.head_vitality import snapshots_from_status
from secretary.dispatch.head_vitality_episode import (
    DEFAULT_VITALITY_THRESHOLDS,
    VitalityEpisode,
    VitalityVerdict,
    reduce_vitality,
)
from secretary.dispatch.state import DispatcherRecord
from secretary.dispatch.worker_lifecycle import head_run_binding
from secretary.runtime.head import HeadRun, HeadSpec, TaskRef
from secretary.runtime.head_runtimes import LOCAL_PTY_RUNTIME, ORCA_LEGACY_RUNTIME

REF = "secretary-9719"
SUSPECT = DEFAULT_VITALITY_THRESHOLDS.suspect_after
CONFIRM = DEFAULT_VITALITY_THRESHOLDS.confirm_after
TICK = 60.0


class _Host:
    """The one host read the status function makes besides the stubbed inventory and /proc."""

    mode = "real"

    def __init__(self, run: dict) -> None:
        self.run = run
        self.cursor = "0:0"

    def provider_progress(self, _task, _record, _kind) -> dict[str, str]:
        run_id, fingerprint = head_run_binding(self.run)
        return {
            "state": "observed",
            "admission": "accepted",
            "source": "claude-session",
            "source_fingerprint": "e" * 32,
            "cursor": self.cursor,
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
        }


class ResumedLocalPtyWorkerVitalityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runs: dict[str, dict] = {}

    def record(self, runtime: str = LOCAL_PTY_RUNTIME) -> DispatcherRecord:
        run = HeadRun(
            run_id=f"run-{runtime}",
            spec=HeadSpec(profile_id="claude-opus-high-local-pty", adapter="claude", runtime=runtime),
            workspace="/tmp/secretary-9719",
            task_ref=TaskRef.card(REF),
            role="worker",
            pid_file="/tmp/secretary-9719/worker.pid",
        ).to_json()
        return DispatcherRecord(
            worker=f"{REF}-worker",
            workspace="/tmp/secretary-9719",
            handle="",
            head="claude-opus-high-local-pty",
            review_head="claude-opus-high-local-pty",
            attempt_id="attempt-9719",
            comment_baseline=0,
            review_baseline=0,
            state="in_progress",
            claimed_at=0.0,
            worker_head_run=run,
        )

    def status(self, host: _Host, record: DispatcherRecord, *, stopped: bool) -> dict:
        """What the real `command_terminal_status` answers for a head with no Orca pane."""
        heartbeat = {"known": True, "alive": True, "match": True, "state": "live-match", "stopped": stopped}
        with (
            mock.patch.object(dispatcher_review, "worktree_panes", return_value=[]),
            mock.patch.object(dispatcher_review, "_head_run_process_status", return_value=heartbeat),
        ):
            return dispatcher_review.command_terminal_status(host, {"ref": REF}, record, kind="worker")

    def run_1703(self, record: DispatcherRecord, *, works_after_continuation: bool):
        """Work, report and go quiet, be retained and parked, then be continued.

        Returns, for every tick after the continuation, its verdict and the quiet the head was
        awake for by then: from its last output to the tick that froze the clocks, plus the time
        since the first tick that saw it running again.
        """
        host = _Host(record.worker_head_run)
        episode: VitalityEpisode | None = None
        now = 0.0
        output = 0

        def tick(*, stopped: bool = False, retained: bool = False) -> VitalityEpisode:
            nonlocal episode
            status = self.status(host, record, stopped=stopped)
            previous_cursor = (episode.evidence_cursors if episode else {}).get("provider_cursor", "")
            snapshots = snapshots_from_status(
                status,
                run_id=record.worker_head_run["run_id"],
                previous_cursor=previous_cursor,
                observed_at=now,
            )
            episode = reduce_vitality(episode, snapshots, now, retained=retained)
            return episode

        # The round: twenty minutes of work, the head writing to its transcript on every tick.
        for _ in range(20):
            output += 1000
            host.cursor = f"{output}:{now}"
            last_output_at = now
            tick()
            now += TICK
        # Done reported; the gate runs. Quiet, then retention (SIGSTOP) for fifteen minutes.
        now += TICK
        tick()
        frozen_at = now + TICK
        for _ in range(15):
            now += TICK
            self.assertIs(tick(stopped=True, retained=True).verdict, VitalityVerdict.RETAINED)
        # Continued (SIGCONT and the continuation prompt) between two ticks.
        awake_at = now + TICK
        ticks: list[tuple[VitalityVerdict, float]] = []
        for _ in range(30):
            now += TICK
            if works_after_continuation:
                output += 1000
                host.cursor = f"{output}:{now}"
            verdict = tick().verdict
            ticks.append((verdict, (frozen_at - last_output_at) + (now - awake_at)))
        return ticks

    def test_a_working_head_continued_after_retention_stays_healthy(self) -> None:
        ticks = self.run_1703(self.record(), works_after_continuation=True)
        self.assertEqual({verdict for verdict, _ in ticks}, {VitalityVerdict.HEALTHY_ACTIVE}, ticks)

    def test_a_silent_head_continued_after_retention_still_climbs_the_ladder(self) -> None:
        # The fix may not blind the ladder: a head that took the continuation and then did
        # nothing is suspected and then confirmed on the unchanged thresholds, counting only the
        # quiet it was awake for.
        ticks = self.run_1703(self.record(), works_after_continuation=False)
        for verdict, quiet in ticks:
            if quiet >= SUSPECT + CONFIRM:
                expected = VitalityVerdict.CONFIRMED_STALL
            elif quiet >= SUSPECT:
                expected = VitalityVerdict.SUSPECTED_STALL
            else:
                expected = VitalityVerdict.HEALTHY_QUIET
            self.assertIs(verdict, expected, f"{quiet:.0f}s of awake quiet")
        seen = [verdict for verdict, _ in ticks]
        self.assertIn(VitalityVerdict.SUSPECTED_STALL, seen)
        self.assertIs(seen[-1], VitalityVerdict.CONFIRMED_STALL)

    def test_the_local_pty_status_carries_the_provider_cursor(self) -> None:
        record = self.record()
        status = self.status(_Host(record.worker_head_run), record, stopped=False)
        self.assertEqual(status["reason"], "pid")
        self.assertEqual(status["provider_progress"]["state"], "observed")

    def test_an_orca_head_that_lost_its_pane_keeps_the_provider_less_shape(self) -> None:
        # secretary-1543's shape is unchanged: its darkness is what the episode records.
        record = self.record(ORCA_LEGACY_RUNTIME)
        status = self.status(_Host(record.worker_head_run), record, stopped=False)
        self.assertEqual(status["reason"], "pid")
        self.assertNotIn("provider_progress", status)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
