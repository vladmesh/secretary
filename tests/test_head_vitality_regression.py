"""Incident regression table for the head-vitality reducer (card S1-3).

Every historical destructive mistake the head-vitality plan lists is replayed here as a
tick-by-tick timeline through the S1-1 snapshot builders and the S1-2 ``reduce_vitality``
reducer, and each test asserts exactly the verdict the plan demands -- including *when* the
ladder reaches SuspectedStall/ConfirmedStall under ``DEFAULT_VITALITY_THRESHOLDS``. The
asymmetry-of-cost principle behind every row: a false "working" costs an idle hour, but a
false kill loses a live round, so a verdict that can stop a head must be earned by strong,
admitted evidence only.

The tests are named after their incident refs so the plan's regression table
(``docs/HEAD_VITALITY.md``, section "Regression table") can point at one test per incident.

Two classes at the bottom characterise the *legacy* decision path (what the watchdog did in
each incident). They are ``expectedFailure`` where today's behaviour still contradicts the
plan; S1-4 flips them to real assertions when the watchdog switches onto episodes.
"""

from __future__ import annotations

import unittest

from secretary.dispatch.head_vitality import (
    ProcessState,
    ProgressState,
    SnapshotSource,
    SourceAvailability,
    TurnState,
    VitalitySnapshot,
)
from secretary.dispatch.head_vitality_episode import (
    DEFAULT_VITALITY_THRESHOLDS,
    VitalityEpisode,
    VitalityVerdict,
    reduce_vitality,
)

RUN_ID = "run-secretary-1420"
THRESHOLDS = DEFAULT_VITALITY_THRESHOLDS  # suspect_after=300s, confirm_after=600s


def heartbeat(
    observed_at: float, *, process: ProcessState = ProcessState.RUNNING,
    run_id: str = RUN_ID, available: bool = True,
) -> VitalitySnapshot:
    """A pid-heartbeat snapshot with only the Process axis filled."""
    if not available:
        return VitalitySnapshot(
            run_id=run_id, source=SnapshotSource.PID_HEARTBEAT, observed_at=observed_at,
            availability=SourceAvailability.UNAVAILABLE,
            reason="pid heartbeat is inconclusive: unreadable",
        )
    return VitalitySnapshot(
        run_id=run_id, source=SnapshotSource.PID_HEARTBEAT, observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE, process=process,
    )


def provider(
    observed_at: float, *, progress: ProgressState = ProgressState.QUIET,
    cursor: str = "12:abc", run_id: str = RUN_ID, available: bool = True,
) -> VitalitySnapshot:
    """A provider-cursor snapshot with only the Progress axis filled."""
    if not available:
        return VitalitySnapshot(
            run_id=run_id, source=SnapshotSource.PROVIDER_CURSOR, observed_at=observed_at,
            availability=SourceAvailability.UNAVAILABLE,
            reason="provider cursor is not admitted",
        )
    return VitalitySnapshot(
        run_id=run_id, source=SnapshotSource.PROVIDER_CURSOR, observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE, progress=progress, cursor=cursor,
    )


def pane(
    observed_at: float, *, idle: bool = True, run_id: str = RUN_ID,
) -> VitalitySnapshot:
    """An advisory pane-readiness snapshot with only the Turn axis filled."""
    return VitalitySnapshot(
        run_id=run_id, source=SnapshotSource.PANE_ADVISORY, observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE,
        turn=TurnState.IDLE if idle else TurnState.ACTIVE,
    )


def replay(timeline: list[tuple[float, list[VitalitySnapshot]]]) -> list[VitalityEpisode]:
    """Fold a whole incident's tick-by-tick observation timeline into its episodes."""
    episodes: list[VitalityEpisode] = []
    previous: VitalityEpisode | None = None
    for now, snapshots in timeline:
        previous = reduce_vitality(previous, snapshots, float(now), THRESHOLDS)
        episodes.append(previous)
    return episodes


class IssueB5195041CodexTranscriptBlindnessTests(unittest.TestCase):
    """issue:b5195041abbc3ec28243 (board 951), secretary-1420, 2026-08-11.

    Three ``idle ~380s no worker report`` episodes (20:44 nudge, 20:51 respawn, 20:58
    Blocked) while the respawned worker's Codex transcript stayed active until 20:58:45 --
    the head was blocked while working. The legacy idle metric read the screen/report, not
    the provider transcript. The fix the plan demands: idleness is the absence of provider-
    transcript advancement, and pane-idle alone never grounds a destructive verdict.
    """

    def test_screen_idle_with_an_advancing_transcript_is_healthy_active(self) -> None:
        """/proc Running + provider Advancing + pane idle => HealthyActive.

        The pane says "between turns"; the transcript says "the work moved seconds ago".
        The strong channel outranks the advisory one, so nothing may nudge, respawn or
        block this head.
        """
        # 20:44:00 through 20:51:00, ticks every minute; the transcript moves every tick.
        timeline = [
            (
                now,
                [
                    heartbeat(now),
                    provider(now, progress=ProgressState.ADVANCING, cursor=f"{index}:rollout"),
                    pane(now, idle=True),
                ],
            )
            for index, now in enumerate(range(0, 421, 60), start=1)
        ]
        episodes = replay(timeline)

        self.assertEqual(
            [episode.verdict for episode in episodes],
            [VitalityVerdict.HEALTHY_ACTIVE] * len(episodes),
        )

    def test_pane_idle_alone_never_ages_into_suspicion(self) -> None:
        """Advisory idle across hours never leaves Unverifiable without a strong source.

        This is the exact blindness that produced the ~380s idle readings: a screen/report
        reading of idleness was charged as stall time. With no pid answer and no provider
        answer, an episode must stay Unverifiable no matter how long the pane reads idle --
        there is no strong quiet to age.
        """
        timeline = [(now, [pane(now, idle=True)]) for now in range(0, 3_600_1, 3_600)]
        episodes = replay(timeline)

        self.assertTrue(episodes)
        for episode in episodes:
            self.assertEqual(episode.verdict, VitalityVerdict.UNVERIFIABLE)
        self.assertTrue(all(episode.suspected_since == 0.0 for episode in episodes))

    def test_no_destructive_verdict_while_the_transcript_was_alive_until_20_58_45(self) -> None:
        """Replay of 20:44-20:59: the transcript advances through the whole window, so the
        episode never reaches any verdict a recovery policy may act on destructively.

        In reality the dispatcher nudged at 20:44, respawned at 20:51 and blocked at 20:58;
        the episode the plan demands stays HealthyActive throughout, because the provider
        cursor moved until 20:58:45.
        """
        base = 20 * 3600 + 44 * 60  # 20:44:00 in wall-clock seconds of the hour
        block_at = 20 * 3600 + 58 * 60
        last_advance = base + 14 * 60 + 45  # transcript active until 20:58:45

        def timeline() -> list[tuple[float, list[VitalitySnapshot]]]:
            steps = []
            for now in range(base, block_at + 1, 60):
                advancing = now <= last_advance
                steps.append((
                    float(now),
                    [
                        heartbeat(float(now)),
                        provider(
                            float(now),
                            progress=(
                                ProgressState.ADVANCING if advancing else ProgressState.QUIET
                            ),
                            cursor="12:alive" if advancing else "12:frozen",
                        ),
                        pane(float(now), idle=True),
                    ],
                ))
            return steps

        episodes = replay(timeline())

        for index, episode in enumerate(episodes):
            with self.subTest(tick=index):
                self.assertIn(
                    episode.verdict,
                    {VitalityVerdict.HEALTHY_ACTIVE, VitalityVerdict.HEALTHY_QUIET},
                    f"tick {index}: a working head was declared {episode.verdict.value}",
                )


class Issue3e7abdf9BusyReadAsUnavailableTests(unittest.TestCase):
    """issue:3e7abdf91b8cd8a16254 (board 997), secretary-1423, 2026-08-12.

    ``orca terminal wait --for tui-idle`` timed out on a WORKING head; the timeout was read
    as ``transport-refused-wait-for-readiness`` and the retained live worker was replaced,
    losing the round's context. Busy is readiness, not unavailability -- and readiness is
    never liveness evidence either way.
    """

    def test_busy_readiness_with_a_running_process_and_advancing_provider_is_healthy_active(
        self,
    ) -> None:
        """The secretary-1423 shape itself: pane busy, process alive, transcript moving."""
        reduced = reduce_vitality(
            None,
            [heartbeat(1000.0), provider(1000.0, progress=ProgressState.ADVANCING,
                                         cursor="13:def"),
             pane(1000.0, idle=False)],
            now=1000.0, thresholds=THRESHOLDS,
        )

        # A busy pane on a provably advancing head is health, not a transport refusal.
        self.assertEqual(reduced.verdict, VitalityVerdict.HEALTHY_ACTIVE)
        self.assertEqual(reduced.activity_epoch, 1)

    def test_readiness_source_unavailable_is_not_dead_and_not_stall_evidence(self) -> None:
        """A refused readiness probe (stage=none) freezes the Turn axis only.

        Whatever the delivery layer concluded from the refused wait, the reducer may see
        only: the probe answered nothing. Process Running plus provider Quiet below every
        threshold stays HealthyQuiet, and it can never reach Dead or ConfirmedStall from a
        readiness failure alone.
        """
        first = reduce_vitality(
            None, [heartbeat(100.0), provider(100.0)], now=100.0, thresholds=THRESHOLDS,
        )
        second = reduce_vitality(
            first,
            [
                heartbeat(400.0),
                provider(400.0),
                # The refused probe: unavailable advisory, no turn opinion.
                VitalitySnapshot(
                    run_id=RUN_ID, source=SnapshotSource.PANE_ADVISORY, observed_at=400.0,
                    availability=SourceAvailability.UNAVAILABLE,
                    reason="pane readiness did not answer",
                ),
            ],
            now=400.0, thresholds=THRESHOLDS,
        )

        # 300s of quiet is exactly suspect_after: the ladder runs on strong quiet alone,
        # the dark advisory contributes nothing, and suspicion is measured from the strong
        # reference (100s), never from the tick that saw the probe fail.
        self.assertEqual(second.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(second.suspected_since, 100.0 + THRESHOLDS.suspect_after)
        self.assertNotEqual(second.verdict, VitalityVerdict.DEAD)
        self.assertIn("pane_advisory", second.unavailable_since)

    def test_running_pid_with_unknown_provider_is_unverifiable_or_quiet_never_destructive(
        self,
    ) -> None:
        """Provider unknown (unadmitted journal): the verdict may not become Dead or Stall."""
        shapes = [
            # Provider went dark entirely.
            [heartbeat(1000.0), provider(1000.0, available=False)],
            # Provider answered about the process but was never admitted.
            [heartbeat(1000.0)],
            # Only the busy pane answered alongside the pid.
            [heartbeat(1000.0), pane(1000.0, idle=False)],
        ]
        for batch in shapes:
            with self.subTest(sources=[snapshot.source.value for snapshot in batch]):
                reduced = reduce_vitality(None, batch, now=1000.0, thresholds=THRESHOLDS)
                self.assertNotEqual(reduced.verdict, VitalityVerdict.DEAD)
                self.assertNotEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)
                self.assertNotEqual(reduced.verdict, VitalityVerdict.SUSPECTED_STALL)


class Issue8f86ed63BusyMasksStallTests(unittest.TestCase):
    """issue:8f86ed6341b187bdd4b6 (board 1010), secretary-1428, 2026-08-13.

    Eleven ``review-red-worker-busy`` cycles in an hour, ``busy_attempts=11``,
    ``progress_at=0``: the rollout had not moved since 06:50, the composer held a stale
    line, and `tui-idle` answered busy forever -- so the dispatcher deferred forever. Busy
    is a readiness state, never proof of liveness; the provider transcript is what says
    whether the work moved.
    """

    def test_pid_running_and_provider_quiet_over_the_hour_climbs_to_confirmed(self) -> None:
        """The required verdict: SuspectedStall then ConfirmedStall, timed from progress.

        Ticks arrive every 5 minutes across the incident hour; the rollout froze at the
        baseline cursor. Assert the exact crossing points under the default thresholds:
        quiet measured from the last progress crosses suspect_after at +300s and
        confirm_after at +900s.
        """
        reference = 100.0  # last provider progress before the wedge
        timeline = []
        for offset in range(0, 1201, 300):
            now = reference + offset
            timeline.append((
                float(now),
                [
                    heartbeat(float(now)),
                    provider(float(now), progress=ProgressState.QUIET, cursor="12:frozen"),
                    pane(float(now), idle=False),
                ],
            ))
        episodes = replay(timeline)

        by_offset = {
            index * 300: episode.verdict for index, episode in enumerate(episodes)
        }
        self.assertEqual(by_offset[0], VitalityVerdict.HEALTHY_QUIET)
        self.assertEqual(by_offset[300], VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(by_offset[600], VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(by_offset[900], VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(by_offset[1200], VitalityVerdict.CONFIRMED_STALL)

        suspected = episodes[1]
        confirmed = episodes[3]
        # Phase onsets are computed from the quiet reference plus thresholds, not from ticks.
        self.assertEqual(suspected.suspected_since, reference + THRESHOLDS.suspect_after)
        self.assertEqual(confirmed.confirmed_since,
                         reference + THRESHOLDS.suspect_after + THRESHOLDS.confirm_after)

    def test_the_busy_pane_does_not_mask_the_stall(self) -> None:
        """The composer's stale line keeps `tui-idle` busy forever; the episode stalls anyway.

        The advisory active reading appears in basis as corroboration of what the pane said
        and nowhere else: it contributes zero weight against the strong quiet evidence.
        """
        previous = VitalityEpisode(run_id=RUN_ID, started_at=100.0, updated_at=100.0)
        reduced = reduce_vitality(
            previous,
            [heartbeat(1000.0), provider(1000.0), pane(1000.0, idle=False)],
            now=1000.0, thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertFalse(
            any(token.startswith("advisory:") and "convict" in token
                for token in reduced.basis)
        )
        # The busy pane is recorded as corroboration only.
        self.assertIn("advisory:active@pane_advisory", reduced.basis)


class IssueFe04011bStoppedWorkerSixHourCeilingTests(unittest.TestCase):
    """issue:fe04011b3723df8d5c2c (board 1156), codegen-orchestrator-1197, 2026-08-21.

    Worker and child sat in ``T (stopped)`` for 27 minutes; the terminal went silent at
    10:44, processes resumed losslessly after ``kill -CONT`` -- yet every dispatcher tick
    wrote ``gate-pending status: ok errors: []`` with ``worker_idle_since=0`` and only the
    six-hour gate ceiling applied. The watchdogs counted time and never asked whether the
    process they waited on was alive.
    """

    def test_proc_state_t_is_suspended_within_one_tick(self) -> None:
        """One tick seeing /proc state `T` yields Suspended -- immediately, not after hours."""
        reduced = reduce_vitality(
            None, [heartbeat(100.0, process=ProcessState.SUSPENDED)], now=100.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.SUSPENDED)
        self.assertEqual(reduced.stall_frozen_since, 100.0)

    def test_suspension_freezes_the_stall_timers_for_the_whole_27_minute_stop(self) -> None:
        """The 10:44-11:11 stop feeds no threshold: quiet references shift past the freeze.

        Timeline: progress at 100s, suspended from 160s (one minute later) until 1780s
        (27 minutes stopped), then SIGCONT resumes the work. The quiet the head is finally
        charged with starts when it woke, not when it was parked.
        """
        frozen_at = 160.0
        resumed_at = frozen_at + 27 * 60  # 27 minutes in `T`
        previous = reduce_vitality(
            None, [heartbeat(100.0), provider(100.0)], now=100.0, thresholds=THRESHOLDS,
        )
        parked = reduce_vitality(
            previous,
            [heartbeat(frozen_at, process=ProcessState.SUSPENDED), provider(frozen_at)],
            now=frozen_at, thresholds=THRESHOLDS,
        )
        self.assertEqual(parked.verdict, VitalityVerdict.SUSPENDED)
        self.assertEqual(parked.stall_frozen_since, frozen_at)

        # Every intermediate tick during the stop re-reads Suspended; none ages a stall.
        for now in range(int(frozen_at) + 300, int(resumed_at), 300):
            parked = reduce_vitality(
                parked,
                [heartbeat(float(now), process=ProcessState.SUSPENDED),
                 provider(float(now))],
                now=float(now), thresholds=THRESHOLDS,
            )
            with self.subTest(suspended_tick=now):
                self.assertEqual(parked.verdict, VitalityVerdict.SUSPENDED)

        woke = reduce_vitality(
            parked, [heartbeat(resumed_at), provider(resumed_at)],
            now=resumed_at, thresholds=THRESHOLDS,
        )

        self.assertEqual(woke.stall_frozen_since, 0.0)
        self.assertNotEqual(woke.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertNotEqual(woke.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(woke.verdict, VitalityVerdict.HEALTHY_QUIET)

    def test_a_suspended_head_is_never_confirmed_stall_and_never_dead(self) -> None:
        """/proc `T` cannot be read as a stall conclusion nor as a gone process, ever."""
        batches = [
            [heartbeat(1000.0, process=ProcessState.SUSPENDED)],
            [heartbeat(1000.0, process=ProcessState.SUSPENDED), provider(1000.0)],
            [heartbeat(1000.0, process=ProcessState.SUSPENDED), provider(1000.0),
             pane(1000.0, idle=True)],
        ]
        for batch in batches:
            with self.subTest(sources=[snapshot.source.value for snapshot in batch]):
                reduced = reduce_vitality(None, batch, now=1000.0, thresholds=THRESHOLDS)
                self.assertEqual(reduced.verdict, VitalityVerdict.SUSPENDED)


class CodegenOrchestrator1194DeterministicSplitFailureTests(unittest.TestCase):
    """codegen-orchestrator-1194 (board card, sprint 1148), 2026-08-21.

    The reviewer spawn failed for 49 minutes: 45 identical deterministic
    ``terminal_split_source_not_found`` refusals against a live terminal, and the
    dispatcher retried forever. This is a deterministic-failure class, not a vitality
    question: the vitality reducer must NOT classify it, and a later recovery-policy card
    (S1-5) owns escalating authoritative deterministic reasons fast.
    """

    def test_a_deterministic_split_refusal_keeps_the_episode_unverifiable(self) -> None:
        """The refusal arrives as an unavailable source with its bounded reason attached.

        The reducer's honest answer is Unverifiable: the observation channel could not see
        the head, so nothing may be concluded about vitality -- and in particular the
        episode must never launder into Dead, Suspended or a stall verdict off a launch
        failure. The escalation belongs to the recovery policy (S1-5 TODO).
        """
        split_refusal = VitalitySnapshot(
            run_id=RUN_ID, source=SnapshotSource.PANE_ADVISORY, observed_at=1000.0,
            availability=SourceAvailability.UNAVAILABLE,
            reason="terminal_split_source_not_found",
        )
        previous = reduce_vitality(
            None, [split_refusal], now=1000.0, thresholds=THRESHOLDS,
        )
        # 45 identical refusals over 65 minutes change nothing: same inputs, same verdict,
        # and no stall timer ever starts.
        for attempt in range(2, 47):
            previous = reduce_vitality(
                previous, [split_refusal.__class__(
                    run_id=RUN_ID, source=SnapshotSource.PANE_ADVISORY,
                    observed_at=1000.0 + attempt * 60,
                    availability=SourceAvailability.UNAVAILABLE,
                    reason="terminal_split_source_not_found",
                )],
                now=1000.0 + attempt * 60, thresholds=THRESHOLDS,
            )

        self.assertEqual(previous.verdict, VitalityVerdict.UNVERIFIABLE)
        self.assertEqual(previous.suspected_since, 0.0)
        self.assertEqual(previous.confirmed_since, 0.0)
        self.assertIn(SnapshotSource.PANE_ADVISORY.value, previous.unavailable_since)

    @unittest.expectedFailure
    def test_todo_S1_5_a_deterministic_reason_must_escalate_fast(self) -> None:
        """TODO(S1-5, recovery policy): ``terminal_split_source_not_found`` is an
        authoritative deterministic class per the plan ("Быстро перескакивать ранги могут
        только авторитетные детерминированные классы"), so the policy must skip the retry
        ladder and escalate within the same attempt instead of re-sending the same command
        45 times. Written as expectedFailure: no policy consumes episodes yet, so the
        contract below has nothing to assert itself against."""
        # The policy input would be the persisted episode plus the deterministic reason;
        # the assertion below names the contract S1-5 owes.
        self.fail("S1-5: no recovery-policy seam exists yet to escalate "
                  "'terminal_split_source_not_found' inside one attempt")


class Issue06dcf6cbUmbrellaLivenessContractTests(unittest.TestCase):
    """issue:06dcf6cb6aacbc38da5f (board 656): the umbrella liveness contract.

    Silence -> neutral ping -> response window -> replacement; and explicitly: the
    existence of a child process is NOT proof of liveness. A pid that answers `Running`
    while no strong progress has been seen long enough is exactly the ConfirmedStall case;
    a bare heartbeat must not keep a wedged head alive forever.
    """

    def test_pid_only_running_with_no_progress_for_hours_confirms_the_stall(self) -> None:
        """The decision S1-2 left open, settled here: pid-only Quiet DOES age into a stall.

        Reading the plan strictly: `Unavailable != no progress` forbids a *dark* source
        from voting for stall, and an unavailable source must not be invented. But issue
        656's own text settles the available-pid case -- the heartbeat IS answering, it
        proves Running and nothing more, and "a process exists" is precisely the weak
        evidence the umbrella refuses to accept as liveness. So a Running pid with no
        strong progress evidence ages HealthyQuiet -> SuspectedStall -> ConfirmedStall
        from the episode's quiet reference: the provider source is treated as simply
        absent (it votes for nothing, neither progress nor stall), while the pid's silence
        about progress stops being free once it outlives both thresholds.
        """
        started = 100.0
        timeline = []
        for offset in (0, 150, 299, 301, 899, 901):
            now = started + offset
            timeline.append((float(now), [heartbeat(float(now))]))
        episodes = replay(timeline)

        self.assertEqual(episodes[0].verdict, VitalityVerdict.HEALTHY_QUIET)
        self.assertEqual(episodes[1].verdict, VitalityVerdict.HEALTHY_QUIET)
        self.assertEqual(episodes[2].verdict, VitalityVerdict.HEALTHY_QUIET)
        self.assertEqual(episodes[3].verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(episodes[4].verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(episodes[5].verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(episodes[5].confirmed_since,
                         started + THRESHOLDS.suspect_after + THRESHOLDS.confirm_after)
