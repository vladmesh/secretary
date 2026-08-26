"""VitalityEpisode reducer: the plan's invariants a-h, each with a named test.

The reducer is pure, so every test drives it directly with hand-built snapshots and an explicit
``now`` -- the same way the shadow wiring feeds it, minus the I/O.
"""

from __future__ import annotations

import json
import unittest

from secretary.dispatch.head_vitality import (
    HeadVitalityError,
    ProcessState,
    ProgressState,
    SnapshotSource,
    SourceAvailability,
    TurnState,
    VitalitySnapshot,
)
from secretary.dispatch.head_vitality_episode import (
    DEFAULT_VITALITY_THRESHOLDS,
    EPISODE_VERSION,
    VitalityEpisode,
    VitalityThresholds,
    VitalityVerdict,
    reduce_vitality,
)

RUN_ID = "run-1"
THRESHOLDS = VitalityThresholds(suspect_after=300.0, confirm_after=600.0)


def heartbeat(
    observed_at: float,
    *,
    process: ProcessState = ProcessState.RUNNING,
    run_id: str = RUN_ID,
    available: bool = True,
) -> VitalitySnapshot:
    """A pid-heartbeat snapshot with only the Process axis filled."""
    if not available:
        return VitalitySnapshot(
            run_id=run_id,
            source=SnapshotSource.PID_HEARTBEAT,
            observed_at=observed_at,
            availability=SourceAvailability.UNAVAILABLE,
            reason="pid heartbeat is inconclusive: not-yet-written",
        )
    return VitalitySnapshot(
        run_id=run_id,
        source=SnapshotSource.PID_HEARTBEAT,
        observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE,
        process=process,
    )


def provider(
    observed_at: float,
    *,
    progress: ProgressState = ProgressState.QUIET,
    run_id: str = RUN_ID,
    cursor: str = "12:abc",
    available: bool = True,
) -> VitalitySnapshot:
    """A provider-cursor snapshot with only the Progress axis filled."""
    if not available:
        return VitalitySnapshot(
            run_id=run_id,
            source=SnapshotSource.PROVIDER_CURSOR,
            observed_at=observed_at,
            availability=SourceAvailability.UNAVAILABLE,
            reason="provider cursor is not admitted",
        )
    return VitalitySnapshot(
        run_id=run_id,
        source=SnapshotSource.PROVIDER_CURSOR,
        observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE,
        progress=progress,
        cursor=cursor,
    )


def pane(observed_at: float, *, idle: bool = True, run_id: str = RUN_ID) -> VitalitySnapshot:
    """An advisory pane snapshot with only the Turn axis filled."""
    return VitalitySnapshot(
        run_id=run_id,
        source=SnapshotSource.PANE_ADVISORY,
        observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE,
        turn=TurnState.IDLE if idle else TurnState.ACTIVE,
    )


def episode(**overrides: object) -> VitalityEpisode:
    """A baseline episode with the fields a continuation test needs to name."""
    fields: dict[str, object] = {
        "run_id": RUN_ID,
        "started_at": 0.0,
        "verdict": VitalityVerdict.HEALTHY_QUIET,
        "updated_at": 0.0,
    }
    fields.update(overrides)
    return VitalityEpisode(**fields)  # type: ignore[arg-type]


class IdentityTests(unittest.TestCase):
    """Invariant (a): snapshots from another run never touch an episode."""

    def test_a_foreign_run_snapshot_is_dropped_and_named_in_basis(self) -> None:
        """A misrouted reading must not inject its verdict into this run's history."""
        previous = episode(verdict=VitalityVerdict.SUSPECTED_STALL, suspected_since=300.0)
        reduced = reduce_vitality(
            previous,
            [
                heartbeat(400.0),
                provider(400.0),
                # Another run's advancement, stale or misrouted into this batch.
                provider(999.0, progress=ProgressState.ADVANCING, run_id="run-other"),
            ],
            now=400.0,
            thresholds=THRESHOLDS,
        )

        # The foreign advancement must not end this episode's suspicion.
        self.assertEqual(reduced.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(reduced.activity_epoch, 0)
        self.assertEqual(reduced.last_progress_at, 0.0)
        self.assertTrue(any("dropped-foreign-run" in token for token in reduced.basis))

    def test_a_wholesale_identity_change_starts_a_fresh_episode(self) -> None:
        previous = episode(verdict=VitalityVerdict.CONFIRMED_STALL, confirmed_since=900.0)
        reduced = reduce_vitality(
            previous,
            [heartbeat(1000.0, run_id="run-respawned")],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        # A respawned head is not the old stall's continuation.
        self.assertEqual(reduced.run_id, "run-respawned")
        self.assertNotEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(reduced.last_progress_at, 0.0)
        self.assertTrue(any("identity-changed" in token for token in reduced.basis))

    def test_a_mixed_batch_keeps_the_episode_run_and_drops_the_rest(self) -> None:
        reduced = reduce_vitality(
            None,
            [heartbeat(1000.0), provider(1000.0, run_id="run-other")],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.run_id, RUN_ID)
        self.assertTrue(any("dropped-foreign-run" in token for token in reduced.basis))


class ProcessAxisTests(unittest.TestCase):
    """Invariant (b): Dead and Suspended outrank the other axes."""

    def test_dead_wins_regardless_of_advancing_progress_and_busy_pane(self) -> None:
        reduced = reduce_vitality(
            None,
            [
                heartbeat(1000.0, process=ProcessState.DEAD),
                provider(1000.0, progress=ProgressState.ADVANCING),
                pane(1000.0, idle=False),
            ],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.DEAD)

    def test_suspended_is_its_own_verdict_and_never_a_stall(self) -> None:
        previous = episode(verdict=VitalityVerdict.SUSPECTED_STALL, suspected_since=300.0)
        reduced = reduce_vitality(
            previous,
            [heartbeat(1000.0, process=ProcessState.SUSPENDED), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.SUSPENDED)
        self.assertNotEqual(reduced.verdict, VitalityVerdict.SUSPECTED_STALL)

    def test_suspension_freezes_the_stall_clocks(self) -> None:
        """The secretary-1061 shape: a worker parked in T for 27 minutes must not be read as
        six hours of quiet. The freeze is stamped, and resumed time is shifted past it."""
        previous = episode(last_progress_at=100.0)
        frozen = reduce_vitality(
            previous,
            [heartbeat(120.0, process=ProcessState.SUSPENDED), provider(120.0)],
            now=120.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(frozen.stall_frozen_since, 120.0)

        # The process resumes at 3000.0 -- 2880s of suspended time that feeds no threshold.
        resumed = reduce_vitality(
            frozen,
            [heartbeat(3000.0), provider(3000.0)],
            now=3000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(resumed.stall_frozen_since, 0.0)
        self.assertEqual(resumed.last_progress_at, 100.0 + 2880.0)
        # 3000 - (100+2880) = 20s of real quiet: far below suspicion.
        self.assertEqual(resumed.verdict, VitalityVerdict.HEALTHY_QUIET)


class ProgressTests(unittest.TestCase):
    """Invariant (c): advancement from any strong source ends a stall episode immediately."""

    def test_advancement_ends_a_confirmed_episode_and_resets_the_clocks(self) -> None:
        previous = episode(
            verdict=VitalityVerdict.CONFIRMED_STALL,
            suspected_since=300.0,
            confirmed_since=900.0,
            last_progress_at=0.0,
        )
        reduced = reduce_vitality(
            previous,
            [provider(1000.0, progress=ProgressState.ADVANCING), heartbeat(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.HEALTHY_ACTIVE)
        self.assertEqual(reduced.suspected_since, 0.0)
        self.assertEqual(reduced.confirmed_since, 0.0)
        self.assertEqual(reduced.last_progress_at, 1000.0)
        self.assertEqual(reduced.last_progress_source, SnapshotSource.PROVIDER_CURSOR.value)
        self.assertEqual(reduced.activity_epoch, 1)

    def test_advancement_from_a_first_observation_counts_as_progress_too(self) -> None:
        # The first provider reading carries no comparison, but its cursor is evidence the
        # source answered; only a *later* reduction can call it advancement. Here the pid
        # heartbeat alone is present, so the verdict rests at healthy without inventing quiet.
        reduced = reduce_vitality(
            None,
            [heartbeat(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.HEALTHY_QUIET)
        self.assertEqual(reduced.last_progress_at, 0.0)


class UnavailableTests(unittest.TestCase):
    """Invariant (d): a dark source freezes evidence; all-strong-dark is Unverifiable."""

    def test_an_unavailable_source_does_not_advance_stall_timers(self) -> None:
        previous = episode(last_progress_at=0.0)
        first = reduce_vitality(
            previous,
            [heartbeat(100.0), provider(100.0, available=False)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        # The provider stays dark for a long time.
        second = reduce_vitality(
            first,
            [heartbeat(1000.0), provider(2000.0, available=False)],
            now=2000.0,
            thresholds=THRESHOLDS,
        )

        # The pid heartbeat answers "alive, no progress source" -- which is not quiet evidence,
        # so the verdict never ages toward a stall while the only progress channel is dark.
        self.assertEqual(second.verdict, VitalityVerdict.HEALTHY_QUIET)
        self.assertIn(SnapshotSource.PROVIDER_CURSOR.value, second.unavailable_since)
        self.assertEqual(second.unavailable_since[SnapshotSource.PROVIDER_CURSOR.value], 100.0)

    def test_a_returning_source_leaves_the_unavailable_map(self) -> None:
        first = reduce_vitality(
            episode(),
            [heartbeat(100.0), provider(100.0, available=False)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        second = reduce_vitality(
            first,
            [heartbeat(200.0), provider(200.0)],
            now=200.0,
            thresholds=THRESHOLDS,
        )

        self.assertNotIn(SnapshotSource.PROVIDER_CURSOR.value, second.unavailable_since)
        self.assertEqual(second.evidence_cursors.get(SnapshotSource.PROVIDER_CURSOR.value), "12:abc")

    def test_all_strong_sources_dark_is_unverifiable(self) -> None:
        reduced = reduce_vitality(
            None,
            [heartbeat(1000.0, available=False), pane(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.UNVERIFIABLE)

    def test_a_confirmed_episode_is_not_laundered_by_its_observers_going_blind(self) -> None:
        confirmed = episode(verdict=VitalityVerdict.CONFIRMED_STALL, confirmed_since=900.0)
        reduced = reduce_vitality(
            confirmed,
            [heartbeat(1000.0, available=False), provider(1000.0, available=False)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)

    def test_a_confirmation_earned_then_blind_keeps_its_phase_onset(self) -> None:
        """Confirmation is stamped the tick it is earned; when the observers then go dark the
        preserved verdict must still carry that onset, so a later policy can audit how long the
        phase has run."""
        previous = episode(last_progress_at=0.0)
        confirmed = reduce_vitality(
            previous,
            [heartbeat(1000.0), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(confirmed.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(confirmed.confirmed_since, 900.0)

        # Every strong source goes dark; the confirmation survives, with its onset.
        blind = reduce_vitality(
            confirmed,
            [heartbeat(2000.0, available=False), provider(2000.0, available=False)],
            now=2000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(blind.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(blind.confirmed_since, 900.0)

    def test_a_confirmation_survives_a_dark_provider_while_the_pid_keeps_answering(self) -> None:
        """The witnessed-freeze arm must never launder an earned confirmation.

        Invariant (g) has no provider-shaped exception: the confirmation was earned on real
        admitted quiet, and a provider going dark afterwards while the pid keeps answering
        says nothing that could end it. The freeze arm must therefore preserve the verdict
        AND its onset -- not demote the phase to HealthyQuiet with dangling stamps (the
        S1-3 review's MAJOR 1), and not keep aging a phase that is already terminal.
        """
        previous = episode(last_progress_at=0.0)
        confirmed = reduce_vitality(
            previous,
            [heartbeat(1000.0), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(confirmed.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(confirmed.confirmed_since, 900.0)
        self.assertEqual(confirmed.suspected_since, 300.0)

        # The provider goes dark mid-episode; the pid keeps answering Running.
        dark = reduce_vitality(
            confirmed,
            [heartbeat(1300.0), provider(1300.0, available=False)],
            now=1300.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(dark.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(dark.confirmed_since, 900.0)
        self.assertEqual(dark.suspected_since, 300.0)
        self.assertIn(
            "preserved-confirmation:provider-dark-pid-alive",
            dark.basis,
        )
        # Much later ticks change nothing: no aging past a terminal phase.
        later = reduce_vitality(
            dark,
            [heartbeat(900_000.0), provider(900_000.0, available=False)],
            now=900_000.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(later.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(later.confirmed_since, 900.0)

        # And only the four authorised endings still move it: real progress first.
        resumed = reduce_vitality(
            dark,
            [heartbeat(900_100.0), provider(900_100.0, progress=ProgressState.ADVANCING, cursor="13:def")],
            now=900_100.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(resumed.verdict, VitalityVerdict.HEALTHY_ACTIVE)
        self.assertEqual(resumed.confirmed_since, 0.0)

    def test_a_suspicion_survives_a_dark_provider_while_the_pid_keeps_answering(self) -> None:
        """The freeze arm preserves an earned SuspectedStall instead of demoting it.

        The S1-3 review follow-up: an earned ``SuspectedStall`` whose provider then went
        dark was rewritten to ``HealthyQuiet`` with ``suspected_since`` left stamped -- a
        dangling onset on a phase the verdict no longer claimed. The rule a dark source
        obeys everywhere else applies here too: it never advances AND never rewinds a
        phase. Mutation-resistant by construction: flipping the preserved verdict back to
        HealthyQuiet fails both the verdict assertion and its onset's.
        """
        previous = episode(last_progress_at=0.0)
        suspected = reduce_vitality(
            previous,
            [heartbeat(400.0), provider(400.0)],
            now=400.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(suspected.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(suspected.suspected_since, 300.0)

        # The provider goes dark mid-suspicion; the pid keeps answering Running.
        dark = reduce_vitality(
            suspected,
            [heartbeat(500.0), provider(500.0, available=False)],
            now=500.0,
            thresholds=THRESHOLDS,
        )

        # The suspicion stands frozen: same phase, same onset, no aging past it.
        self.assertEqual(dark.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(dark.suspected_since, 300.0)
        self.assertEqual(dark.confirmed_since, 0.0)
        self.assertIn("preserved-suspicion:provider-dark-pid-alive", dark.basis)

        # Much later ticks change nothing: freezing never advances the ladder either --
        # confirmation must be EARNED on strong quiet evidence, not inherited from a
        # channel outage.
        later = reduce_vitality(
            dark,
            [heartbeat(900_000.0), provider(900_000.0, available=False)],
            now=900_000.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(later.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(later.suspected_since, 300.0)
        self.assertEqual(later.confirmed_since, 0.0)

        # And only the four authorised endings still move it: real progress first.
        resumed = reduce_vitality(
            later,
            [heartbeat(900_100.0), provider(900_100.0, progress=ProgressState.ADVANCING, cursor="13:def")],
            now=900_100.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(resumed.verdict, VitalityVerdict.HEALTHY_ACTIVE)
        self.assertEqual(resumed.suspected_since, 0.0)


class AdvisoryTests(unittest.TestCase):
    """Invariant (e): pane evidence corroborates and never convicts."""

    def test_advisory_idle_alone_stays_unverifiable_and_never_suspected(self) -> None:
        reduced = reduce_vitality(
            None,
            [pane(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.UNVERIFIABLE)

    def test_advisory_idle_for_hours_cannot_reach_a_stall_verdict(self) -> None:
        first = reduce_vitality(None, [pane(0.0)], now=0.0, thresholds=THRESHOLDS)
        reduced = reduce_vitality(first, [pane(100_000.0)], now=100_000.0, thresholds=THRESHOLDS)

        self.assertEqual(reduced.verdict, VitalityVerdict.UNVERIFIABLE)
        self.assertTrue(any(token.startswith("advisory:") for token in reduced.basis))

    def test_advisory_corroboration_appears_in_basis_alongside_strong_quiet(self) -> None:
        reduced = reduce_vitality(
            None,
            [heartbeat(1000.0), provider(1000.0), pane(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertTrue(any(token.startswith("advisory:") for token in reduced.basis))
        self.assertEqual(reduced.verdict, VitalityVerdict.HEALTHY_QUIET)


class QuietLadderTests(unittest.TestCase):
    """Invariant (f): quiet accumulates from the progress reference, not from ticks."""

    def test_quiet_climbs_healthy_then_suspected_then_confirmed(self) -> None:
        previous = episode(last_progress_at=0.0)
        quiet = reduce_vitality(
            previous,
            [heartbeat(100.0), provider(100.0)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(quiet.verdict, VitalityVerdict.HEALTHY_QUIET)

        suspected = reduce_vitality(
            quiet,
            [heartbeat(400.0), provider(400.0)],
            now=400.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(suspected.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(suspected.suspected_since, 300.0)

        confirmed = reduce_vitality(
            suspected,
            [heartbeat(1000.0), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(confirmed.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(confirmed.confirmed_since, 900.0)

    def test_timers_are_measured_from_last_progress_not_from_the_last_tick(self) -> None:
        """Ticks are irregular: a 10-minute gap between ticks must not shrink the quiet the
        episode charges, and a burst of ticks must not stretch it either."""
        previous = episode(last_progress_at=0.0)
        burst = previous
        for tick in (10.0, 20.0, 30.0, 40.0):
            burst = reduce_vitality(
                burst,
                [heartbeat(tick), provider(tick)],
                now=tick,
                thresholds=THRESHOLDS,
            )
        self.assertEqual(burst.verdict, VitalityVerdict.HEALTHY_QUIET)

        # One long-silent stretch lands exactly on the threshold the wall clock says.
        crossed = reduce_vitality(
            burst,
            [heartbeat(300.0), provider(300.0)],
            now=300.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(crossed.verdict, VitalityVerdict.SUSPECTED_STALL)

    def test_fresh_advancement_restarts_the_quiet_reference(self) -> None:
        previous = episode(last_progress_at=0.0)
        quiet = reduce_vitality(
            previous,
            [heartbeat(100.0), provider(100.0)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        advanced = reduce_vitality(
            quiet,
            [heartbeat(200.0), provider(200.0, progress=ProgressState.ADVANCING, cursor="13:def")],
            now=200.0,
            thresholds=THRESHOLDS,
        )
        # Quiet measured from the new progress, not from the episode start.
        still_healthy = reduce_vitality(
            advanced,
            [heartbeat(400.0), provider(400.0)],
            now=400.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(advanced.last_progress_at, 200.0)
        self.assertEqual(still_healthy.verdict, VitalityVerdict.HEALTHY_QUIET)


class HysteresisTests(unittest.TestCase):
    """Invariant (g): confirmation is sticky, and the verdict always carries basis and run."""

    def test_one_quiet_tick_does_not_bounce_a_confirmed_episode_back(self) -> None:
        confirmed = episode(
            verdict=VitalityVerdict.CONFIRMED_STALL,
            suspected_since=300.0,
            confirmed_since=900.0,
            last_progress_at=0.0,
        )
        reduced = reduce_vitality(
            confirmed,
            [heartbeat(1000.0), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)

    def test_only_progress_suspension_death_or_identity_ends_a_confirmation(self) -> None:
        confirmed = episode(
            verdict=VitalityVerdict.CONFIRMED_STALL,
            suspected_since=300.0,
            confirmed_since=900.0,
            last_progress_at=0.0,
        )
        ends = [
            ("progress", [heartbeat(1000.0), provider(1000.0, progress=ProgressState.ADVANCING)]),
            ("suspended", [heartbeat(1000.0, process=ProcessState.SUSPENDED), provider(1000.0)]),
            ("dead", [heartbeat(1000.0, process=ProcessState.DEAD), provider(1000.0)]),
            ("identity", [heartbeat(1000.0, run_id="run-respawned")]),
        ]
        for name, batch in ends:
            with self.subTest(ended_by=name):
                reduced = reduce_vitality(confirmed, batch, now=1000.0, thresholds=THRESHOLDS)
                self.assertNotEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)

    def test_every_verdict_carries_its_run_and_a_basis(self) -> None:
        batches = [
            [heartbeat(1000.0), provider(1000.0)],
            [heartbeat(1000.0, process=ProcessState.DEAD)],
            [heartbeat(1000.0, available=False)],
        ]
        for batch in batches:
            reduced = reduce_vitality(None, batch, now=1000.0, thresholds=THRESHOLDS)
            with self.subTest(verdict=reduced.verdict):
                self.assertEqual(reduced.run_id, RUN_ID)
                self.assertTrue(reduced.basis)


class PurityTests(unittest.TestCase):
    """Invariant (h): deterministic and pure -- no I/O, no clock, same inputs, same output."""

    def test_same_inputs_reduce_to_the_same_episode(self) -> None:
        batch = [heartbeat(100.0), provider(100.0)]
        previous = episode(last_progress_at=0.0)
        self.assertEqual(
            reduce_vitality(previous, batch, now=100.0, thresholds=THRESHOLDS),
            reduce_vitality(previous, batch, now=100.0, thresholds=THRESHOLDS),
        )

    def test_the_previous_episode_is_not_mutated_by_a_reduction(self) -> None:
        previous = episode(
            verdict=VitalityVerdict.SUSPECTED_STALL,
            suspected_since=300.0,
            last_progress_at=0.0,
        )
        frozen_previous = replace_episode(previous)
        reduce_vitality(
            previous,
            [heartbeat(1000.0), provider(1000.0, progress=ProgressState.ADVANCING)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(previous, frozen_previous)

    def test_snapshot_order_in_a_batch_does_not_change_the_verdict(self) -> None:
        first = reduce_vitality(
            None,
            [heartbeat(1000.0), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )
        second = reduce_vitality(
            None,
            [provider(1000.0), heartbeat(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(first, second)

    def test_snapshot_order_does_not_change_the_written_basis(self) -> None:
        """The whole episode, basis included, is a function of the batch -- not of the order
        the channels happened to answer in."""
        previous = episode(verdict=VitalityVerdict.SUSPECTED_STALL, suspected_since=300.0)
        forward = reduce_vitality(
            previous,
            [heartbeat(400.0), provider(400.0), pane(400.0), provider(401.0, run_id="run-other")],
            now=400.0,
            thresholds=THRESHOLDS,
        )
        backward = reduce_vitality(
            previous,
            [provider(401.0, run_id="run-other"), pane(400.0), provider(400.0), heartbeat(400.0)],
            now=400.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(forward, backward)
        self.assertEqual(forward.basis, backward.basis)

    def test_two_dark_sources_write_their_unavailable_basis_in_source_order(self) -> None:
        """The review follow-up: with both strong sources dark in one tick the ``unavailable``
        tokens were emitted in batch order, so a reversed batch wrote a different basis for the
        same facts. The sources are walked sorted, and this pins the episode -- basis included --
        as equal under reversal."""
        batch = [
            provider(1000.0, available=False),
            heartbeat(1000.0, available=False),
            pane(1000.0),
        ]
        forward = reduce_vitality(None, batch, now=1000.0, thresholds=THRESHOLDS)
        backward = reduce_vitality(None, list(reversed(batch)), now=1000.0, thresholds=THRESHOLDS)

        unavailable_tokens = [token for token in forward.basis if token.startswith("unavailable@")]
        # Both dark sources are named, and named in sorted-source order regardless of the order
        # the channels failed in.
        self.assertEqual(
            unavailable_tokens,
            ["unavailable@pid_heartbeat", "unavailable@provider_cursor"],
        )
        self.assertEqual(forward.verdict, VitalityVerdict.UNVERIFIABLE)
        self.assertEqual(forward, backward)
        self.assertEqual(forward.basis, backward.basis)

    def test_no_snapshots_leaves_the_previous_episode_untouched(self) -> None:
        previous = episode(verdict=VitalityVerdict.SUSPECTED_STALL, suspected_since=300.0)

        reduced = reduce_vitality(previous, [], now=1000.0, thresholds=THRESHOLDS)

        self.assertEqual(reduced, previous)


class SerialisationTests(unittest.TestCase):
    def test_a_full_episode_round_trips_through_json_text(self) -> None:
        full = VitalityEpisode(
            run_id=RUN_ID,
            verdict=VitalityVerdict.CONFIRMED_STALL,
            started_at=0.0,
            suspected_since=300.0,
            confirmed_since=900.0,
            last_progress_at=100.0,
            last_progress_source=SnapshotSource.PROVIDER_CURSOR.value,
            evidence_cursors={SnapshotSource.PROVIDER_CURSOR.value: "14:def"},
            unavailable_since={SnapshotSource.PANE_ADVISORY.value: 500.0},
            basis=("quiet:1000s@provider_cursor", "confirmed-stall"),
            reason="strong quiet for 1000s with no advancement",
            recovery_rung=0,
            activity_epoch=3,
            updated_at=1000.0,
            stall_frozen_since=0.0,
        )

        restored = VitalityEpisode.from_json(json.loads(json.dumps(full.to_json())))

        self.assertEqual(restored, full)

    def test_a_fresh_episode_round_trips_with_its_empty_maps(self) -> None:
        fresh = VitalityEpisode(run_id=RUN_ID, started_at=5.0, updated_at=5.0)

        restored = VitalityEpisode.from_json(json.loads(json.dumps(fresh.to_json())))

        self.assertEqual(restored, fresh)
        self.assertEqual(restored.evidence_cursors, {})
        self.assertEqual(restored.unavailable_since, {})

    def test_from_json_refuses_payloads_that_change_meaning(self) -> None:
        base = VitalityEpisode(run_id=RUN_ID, started_at=1.0, updated_at=1.0).to_json()
        cases = [
            {**base, "version": EPISODE_VERSION + 1},
            {**base, "version": True},
            {**base, "version": 1.0},
            "not-an-object",
            {**base, "verdict": "totally-fine"},
            {**base, "run_id": ""},
            {**base, "recovery_rung": "zero"},
            {**base, "recovery_rung": -1},
            {**base, "activity_epoch": -1},
            {**base, "evidence_cursors": {"provider_cursor": 12}},
            {**base, "unavailable_since": {"provider_cursor": "recently"}},
            {**base, "basis": "quiet"},
            {**base, "basis": ["quiet", 7]},
            {**base, "updated_at": None},
            {**base, "updated_at": float("nan")},
            {**base, "suspected_since": "300"},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(HeadVitalityError):
                VitalityEpisode.from_json(payload)


class ThresholdTests(unittest.TestCase):
    def test_thresholds_must_be_positive_finite_numbers(self) -> None:
        for bad in (0, -5, float("nan"), float("inf"), "300", True):
            with self.subTest(suspect_after=bad), self.assertRaises(HeadVitalityError):
                VitalityThresholds(suspect_after=bad, confirm_after=600.0)

    def test_defaults_align_with_the_watchdogs_idle_ceiling(self) -> None:
        from secretary.dispatcher_watchdog import IDLE_STALL_DEFAULT

        self.assertEqual(DEFAULT_VITALITY_THRESHOLDS.suspect_after, float(IDLE_STALL_DEFAULT))
        self.assertEqual(DEFAULT_VITALITY_THRESHOLDS.confirm_after, 2.0 * float(IDLE_STALL_DEFAULT))
        self.assertEqual(
            (DEFAULT_VITALITY_THRESHOLDS.suspect_after, DEFAULT_VITALITY_THRESHOLDS.confirm_after),
            (300.0, 600.0),
        )


def replace_episode(value: VitalityEpisode) -> VitalityEpisode:
    """A structural copy used to prove reductions never mutate their input."""
    return VitalityEpisode.from_json(json.loads(json.dumps(value.to_json())))


if __name__ == "__main__":
    unittest.main()
