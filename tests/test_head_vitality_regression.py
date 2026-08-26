"""Incident regression table for the head-vitality reducer (card S1-3).

Every historical destructive mistake the head-vitality plan lists is replayed here as a
tick-by-tick timeline through the S1-1 snapshot builders (``from_pid_heartbeat``,
``from_provider_cursor``, ``from_pane_readiness``) fed with the producer payload dicts the
wait tick actually carries -- ``dispatcher_watchdog.head_process_status`` classifications,
admitted/unadmitted ``provider_progress_for_run`` evidence, pane readiness ``{"idle": bool}``
-- and folded by the S1-2 ``reduce_vitality`` reducer. Each test asserts exactly the verdict
the plan demands, including *when* the ladder reaches SuspectedStall/ConfirmedStall under
``DEFAULT_VITALITY_THRESHOLDS``. The asymmetry-of-cost principle behind every row: a false
"working" costs an idle hour, but a false kill loses a live round, so a verdict that can
stop a head must be earned by strong, admitted evidence only.

The tests are named after their incident refs so the plan's regression table
(``docs/HEAD_VITALITY.md``, section "Regression table") can point at one test per incident.
"""

from __future__ import annotations

import unittest

from secretary.dispatch.head_vitality import (
    SourceAvailability,
    VitalitySnapshot,
)
from secretary.dispatch.head_vitality_episode import (
    DEFAULT_VITALITY_THRESHOLDS,
    VitalityEpisode,
    VitalityVerdict,
    reduce_vitality,
)

RUN_ID = "run-secretary-1420"
RUN_FINGERPRINT = "a" * 32
THRESHOLDS = DEFAULT_VITALITY_THRESHOLDS  # suspect_after=300s, confirm_after=600s


def pid_status(
    *,
    alive: bool = True,
    match: bool = True,
    stopped: bool = False,
) -> dict[str, object]:
    """A ``head_process_status`` classification, as the watchdog spells it."""
    if not alive:
        return {"known": True, "alive": False, "match": False, "state": "dead", "record": {"pid": 1}}
    if not match:
        return {
            "known": True,
            "alive": True,
            "match": False,
            "state": "identity-mismatch",
            "stopped": stopped,
        }
    return {"known": True, "alive": True, "match": True, "state": "live-match", "stopped": stopped}


def provider_evidence(
    cursor: str,
    *,
    admitted: bool = True,
    bound_run: str = RUN_ID,
) -> dict[str, object]:
    """An admitted exact-run ``provider_progress_for_run`` answer, as the wire spells it."""
    if not admitted:
        return {
            "state": "unavailable",
            "source": "codex-session",
            "reason": "Codex provider source has no bound v1 baseline",
        }
    return {
        "state": "observed",
        "admission": "accepted",
        "source": "codex-session",
        "source_fingerprint": "e" * 32,
        "cursor": cursor,
        "head_run_id": bound_run,
        "head_run_fingerprint": RUN_FINGERPRINT,
    }


def heartbeat(
    observed_at: float,
    *,
    alive: bool = True,
    match: bool = True,
    stopped: bool = False,
    run_id: str = RUN_ID,
    status: dict[str, object] | None = None,
) -> VitalitySnapshot:
    """One S1-1 pid snapshot built through the real builder from a producer payload."""
    return VitalitySnapshot.from_pid_heartbeat(
        status if status is not None else pid_status(alive=alive, match=match, stopped=stopped),
        run_id=run_id,
        observed_at=observed_at,
    )


def provider(
    observed_at: float,
    *,
    cursor: str = "12:abc",
    previous_cursor: str = "12:abc",
    baseline: bool = False,
    admitted: bool = True,
    run_id: str = RUN_ID,
    evidence: dict[str, object] | None = None,
) -> VitalitySnapshot:
    """One S1-1 provider snapshot built through the real builder from a producer payload.

    The builder's own rule is reproduced here, not bypassed: Advancing requires this
    snapshot's cursor to differ from ``previous_cursor`` (this run's last recorded cursor,
    as the wiring feeds it back from the episode), Quiet means it re-read the same value,
    and ``baseline=True`` is the first-ever observation (no earlier cursor to compare
    against, so the builder answers Unknown). Callers replaying an advancing transcript
    therefore give each tick its own new cursor AND the prior tick's cursor.
    """
    return VitalitySnapshot.from_provider_cursor(
        evidence if evidence is not None else provider_evidence(cursor, admitted=admitted),
        run_id=run_id,
        previous_cursor="" if baseline else previous_cursor,
        observed_at=observed_at,
    )


def pane(
    observed_at: float,
    *,
    idle: bool = True,
    run_id: str = RUN_ID,
    refused: bool = False,
    reason: str = "",
) -> VitalitySnapshot:
    """One S1-1 advisory snapshot built through the real builder from a producer payload.

    ``reason`` carries the bounded refusal diagnostic the producer put on the wire for an
    unavailable reading, so a replay can assert what S1-5's policy will key on.
    """
    return VitalitySnapshot.from_pane_readiness(
        {"reason": reason} if refused else {"idle": idle},
        run_id=run_id,
        observed_at=observed_at,
    )


def replay(timeline: list[tuple[float, list[VitalitySnapshot]]]) -> list[VitalityEpisode]:
    """Fold a whole incident's tick-by-tick observation timeline into its episodes.

    The caller owns the per-source cursor memory exactly as the wait tick does: snapshots
    are compared against the previous episode's recorded cursor, which is what makes one
    reading Advancing and the next Quiet. Timelines that only ever show movement may leave
    that to ``provider(advancing=True)``; timelines replaying a freeze thread the cursor.
    """
    episodes: list[VitalityEpisode] = []
    previous: VitalityEpisode | None = None
    for now, snapshots in timeline:
        previous = reduce_vitality(previous, snapshots, float(now), THRESHOLDS)
        episodes.append(previous)
    return episodes


def frozen_cursor_timeline(
    ticks: list[float],
    *,
    stopped_cursor: str = "12:frozen",
    busy: bool = True,
) -> list[tuple[float, list[VitalitySnapshot]]]:
    """The 8f86ed63 shape: live pid + admitted cursor frozen at one value + busy pane.

    The first tick baselines the cursor; every later tick re-reads the SAME value, so the
    reducer sees admitted Quiet exactly as it would from real rollout evidence.
    """
    steps: list[tuple[float, list[VitalitySnapshot]]] = []
    for index, now in enumerate(ticks):
        steps.append(
            (
                float(now),
                [
                    heartbeat(float(now)),
                    provider(
                        float(now),
                        cursor=stopped_cursor,
                        previous_cursor="" if index == 0 else stopped_cursor,
                    ),
                    pane(float(now), idle=not busy),
                ],
            )
        )
    return steps


def advancing_cursor_timeline(
    ticks: list[float],
    *,
    idle: bool = True,
) -> list[tuple[float, list[VitalitySnapshot]]]:
    """The b5195041 shape: live pid + transcript advancing every tick + screen idle.

    Each tick's rollout cursor differs from the previous one, which is exactly the
    comparison the builder makes against this run's last recorded cursor.
    """
    steps: list[tuple[float, list[VitalitySnapshot]]] = []
    for index, now in enumerate(ticks):
        cursor = f"{index + 1}:rollout"
        steps.append(
            (
                float(now),
                [
                    heartbeat(float(now)),
                    provider(float(now), cursor=cursor, previous_cursor=f"{index}:rollout"),
                    pane(float(now), idle=idle),
                ],
            )
        )
    return steps


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
        ticks = list(range(0, 421, 60))
        episodes = replay(advancing_cursor_timeline(ticks))

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
        """Replay of the incident's own wall clock, 20:44-20:59, one tick per minute.

        In reality the dispatcher nudged at 20:44, respawned at 20:51 and blocked at 20:58;
        the episode the plan demands stays healthy throughout, because the provider cursor
        moved until 20:58:45 -- five seconds before the block.
        """
        base = 20 * 3600 + 44 * 60  # 20:44:00 in wall-clock seconds of the hour
        block_at = 20 * 3600 + 58 * 60
        last_advance = base + 14 * 60 + 45  # transcript active until 20:58:45

        steps: list[tuple[float, list[VitalitySnapshot]]] = []
        for index, now in enumerate(range(base, block_at + 1, 60)):
            advancing = now <= last_advance
            batch = [heartbeat(float(now))]
            if advancing:
                batch.append(
                    provider(
                        float(now),
                        cursor=f"{index + 1}:rollout",
                        previous_cursor=f"{index}:rollout",
                    )
                )
            else:
                # After the last advance the same cursor re-reads: admitted quiet.
                batch.append(provider(float(now), cursor="15:final", previous_cursor="15:final"))
            batch.append(pane(float(now), idle=True))
            steps.append((float(now), batch))

        episodes = replay(steps)

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
            [
                heartbeat(1000.0),
                provider(1000.0, cursor="13:def", previous_cursor="12:abc"),
                pane(1000.0, idle=False),
            ],
            now=1000.0,
            thresholds=THRESHOLDS,
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
            None,
            [heartbeat(100.0), provider(100.0, cursor="13:def", baseline=True)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        second = reduce_vitality(
            first,
            [
                heartbeat(400.0),
                # The rollout re-reads its own recorded value: admitted quiet, not silence.
                provider(400.0, cursor="13:def", previous_cursor="13:def"),
                # The refused probe: unavailable advisory, no turn opinion.
                pane(400.0, refused=True),
            ],
            now=400.0,
            thresholds=THRESHOLDS,
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
            # Provider went dark entirely (unadmitted evidence on the wire).
            [heartbeat(1000.0), provider(1000.0, admitted=False)],
            # Only the busy pane answered alongside the pid.
            [heartbeat(1000.0), pane(1000.0, idle=False)],
        ]
        for batch in shapes:
            with self.subTest(sources=[snapshot.source.value for snapshot in batch]):
                reduced = reduce_vitality(None, batch, now=1000.0, thresholds=THRESHOLDS)
                self.assertNotEqual(reduced.verdict, VitalityVerdict.DEAD)
                self.assertNotEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)


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

        Ticks arrive every 5 minutes across the incident hour; the rollout froze at its
        06:50 cursor. Assert the exact crossing points under the default thresholds: quiet
        measured from the first (baselining) observation crosses suspect_after at +300s and
        confirm_after at +900s.
        """
        reference = 100.0
        ticks = [reference + offset for offset in range(0, 1201, 300)]
        episodes = replay(frozen_cursor_timeline(ticks))

        by_offset = {index * 300: episode.verdict for index, episode in enumerate(episodes)}
        self.assertEqual(by_offset[0], VitalityVerdict.HEALTHY_QUIET)
        self.assertEqual(by_offset[300], VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(by_offset[600], VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(by_offset[900], VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(by_offset[1200], VitalityVerdict.CONFIRMED_STALL)

        suspected = episodes[1]
        confirmed = episodes[3]
        # Phase onsets are computed from the quiet reference plus thresholds, not from ticks.
        self.assertEqual(suspected.suspected_since, reference + THRESHOLDS.suspect_after)
        self.assertEqual(
            confirmed.confirmed_since, reference + THRESHOLDS.suspect_after + THRESHOLDS.confirm_after
        )

    def test_the_busy_pane_does_not_mask_the_stall(self) -> None:
        """The composer's stale line keeps `tui-idle` busy forever; the episode stalls anyway.

        The advisory active reading appears in basis as corroboration of what the pane said
        and nowhere else: it contributes zero weight against the strong quiet evidence.
        """
        previous = VitalityEpisode(
            run_id=RUN_ID,
            started_at=100.0,
            updated_at=100.0,
            evidence_cursors={"provider_cursor": "12:frozen"},
        )
        reduced = reduce_vitality(
            previous,
            [
                heartbeat(1000.0),
                # The rollout re-reads its 06:50 value: admitted quiet against the
                # cursor this episode already recorded.
                provider(1000.0, cursor="12:frozen", previous_cursor="12:frozen"),
                pane(1000.0, idle=False),
            ],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertFalse(any(token.startswith("advisory:") and "convict" in token for token in reduced.basis))
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
            None,
            [heartbeat(100.0, stopped=True)],
            now=100.0,
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
            None,
            [heartbeat(100.0), provider(100.0, baseline=True)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        parked = reduce_vitality(
            previous,
            [heartbeat(frozen_at, stopped=True), provider(frozen_at, cursor="12:abc")],
            now=frozen_at,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(parked.verdict, VitalityVerdict.SUSPENDED)
        self.assertEqual(parked.stall_frozen_since, frozen_at)

        # Every intermediate tick during the stop re-reads Suspended; none ages a stall.
        for now in range(int(frozen_at) + 300, int(resumed_at), 300):
            parked = reduce_vitality(
                parked,
                [heartbeat(float(now), stopped=True), provider(float(now), cursor="12:abc")],
                now=float(now),
                thresholds=THRESHOLDS,
            )
            with self.subTest(suspended_tick=now):
                self.assertEqual(parked.verdict, VitalityVerdict.SUSPENDED)

        woke = reduce_vitality(
            parked,
            [heartbeat(resumed_at), provider(resumed_at, cursor="12:abc")],
            now=resumed_at,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(woke.stall_frozen_since, 0.0)
        self.assertNotEqual(woke.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertNotEqual(woke.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(woke.verdict, VitalityVerdict.HEALTHY_QUIET)

    def test_a_suspended_head_is_never_confirmed_stall_and_never_dead(self) -> None:
        """/proc `T` cannot be read as a stall conclusion nor as a gone process, ever."""
        batches = [
            [heartbeat(1000.0, stopped=True)],
            [heartbeat(1000.0, stopped=True), provider(1000.0)],
            [heartbeat(1000.0, stopped=True), provider(1000.0), pane(1000.0, idle=True)],
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
        previous = reduce_vitality(
            None,
            [pane(1000.0, refused=True, reason="terminal_split_source_not_found")],
            now=1000.0,
            thresholds=THRESHOLDS,
        )
        # 45 identical refusals over the loop's 46 minutes (one per minute, attempts 2..46)
        # change nothing: same inputs, same verdict, no stall timer ever starts. The real
        # incident ran 49 minutes (06:53-07:42); the loop here is the bounded replay of it.
        for attempt in range(2, 47):
            previous = reduce_vitality(
                previous,
                [pane(1000.0 + attempt * 60, refused=True, reason="terminal_split_source_not_found")],
                now=1000.0 + attempt * 60,
                thresholds=THRESHOLDS,
            )

        self.assertEqual(previous.verdict, VitalityVerdict.UNVERIFIABLE)
        self.assertEqual(previous.suspected_since, 0.0)
        self.assertEqual(previous.confirmed_since, 0.0)
        self.assertIn("pane_advisory", previous.unavailable_since)

    def test_the_deterministic_reason_travels_on_the_snapshot(self) -> None:
        """S1-5 keys its fast escalation on the reason string; the snapshot must carry it.

        The S1-3 review follow-up: a refusal's own diagnostic was dropped at the builder,
        leaving every dark pane reading with the same generic words, so a recovery policy
        could not tell an authoritative deterministic class from an ordinary outage. The
        producer's bounded reason now rides along on the unavailable snapshot.
        """
        snapshot = VitalitySnapshot.from_pane_readiness(
            {"reason": "terminal_split_source_not_found"},
            run_id=RUN_ID,
            observed_at=1000.0,
        )

        self.assertIs(snapshot.availability, SourceAvailability.UNAVAILABLE)
        self.assertIn("terminal_split_source_not_found", snapshot.reason)

    def test_a_deterministic_reason_escalates_fast_through_the_policy(self) -> None:
        """The S1-5 contract, flipped: N identical authoritative refusals escalate at once.

        The 1194 replay (45 refusals over 46 minutes) is fed through the real reduction and
        the real policy exactly as the dispatcher wires them: each tick's unavailable pane
        snapshot carries the reason on the episode, and the policy counts identical
        authoritative sightings. At the limit it returns ``escalate_operator`` -- inside one
        attempt, minutes into the loop instead of 49 minutes in -- while a heuristic reason
        repeated just as often earns only observation. The reducer's own honesty is asserted
        first (Unverifiable throughout): escalation is a POLICY conclusion about a launch
        fact, never a vitality verdict about the head.
        """
        from secretary.dispatch.head_vitality_policy import (
            DeterministicReasonClass,
            RecoveryIntent,
            apply_rung_state,
            decide_recovery,
            deterministic_reason_class,
        )

        # The allowlist classifies the incident's own token, and only authoritative tokens:
        self.assertIs(
            deterministic_reason_class("pane readiness did not answer: terminal_split_source_not_found"),
            DeterministicReasonClass.SPLIT_SOURCE_NOT_FOUND,
        )
        self.assertIsNone(
            deterministic_reason_class(
                "pane readiness did not answer: transport timeout",
            )
        )

        previous = reduce_vitality(
            None,
            [pane(1000.0, refused=True, reason="terminal_split_source_not_found")],
            now=1000.0,
            thresholds=THRESHOLDS,
        )
        escalated: list[bool] = []
        for attempt in range(2, 47):
            previous = reduce_vitality(
                previous,
                [pane(1000.0 + attempt * 60, refused=True, reason="terminal_split_source_not_found")],
                now=1000.0 + attempt * 60,
                thresholds=THRESHOLDS,
            )
            decision = decide_recovery(previous, previous, now=1000.0 + attempt * 60)
            escalated.append(decision.intent is RecoveryIntent.ESCALATE_OPERATOR)
            # The dispatcher persists the policy's count each tick; the replay does too,
            # or the count would restart at 1 forever and the limit could never fire.
            previous = apply_rung_state(previous, decision)
            if any(escalated):
                break

        # The reducer stays honest for the whole loop...
        self.assertEqual(previous.verdict, VitalityVerdict.UNVERIFIABLE)
        # ...and the policy escalates within the FIRST THREE sightings (limit 3), minutes
        # into the incident instead of 49 minutes after it.
        self.assertTrue(any(escalated))
        first_escalation = escalated.index(True) + 1
        self.assertLessEqual(first_escalation, 3, f"escalated at sighting {first_escalation}")

    def test_a_heuristic_reason_repeated_is_never_evidence(self) -> None:
        """The distinction the card names in code: heuristic repetition is not authority.

        The same dark-pane shape with an ordinary outage diagnostic, fed through the same
        reduction and policy for far more than the deterministic limit, never escalates:
        unavailability freezes evidence instead of spending it, and its repetition buys no
        rung.
        """
        from secretary.dispatch.head_vitality_policy import (
            RecoveryIntent,
            apply_rung_state,
            decide_recovery,
        )

        previous = reduce_vitality(
            None,
            [pane(1000.0, refused=True, reason="transport timeout")],
            now=1000.0,
            thresholds=THRESHOLDS,
        )
        for attempt in range(2, 47):
            previous = reduce_vitality(
                previous,
                [pane(1000.0 + attempt * 60, refused=True, reason="transport timeout")],
                now=1000.0 + attempt * 60,
                thresholds=THRESHOLDS,
            )
            decision = decide_recovery(previous, previous, now=1000.0 + attempt * 60)
            self.assertIsNot(decision.intent, RecoveryIntent.ESCALATE_OPERATOR)
            previous = apply_rung_state(previous, decision)

        self.assertEqual(previous.verdict, VitalityVerdict.UNVERIFIABLE)
        self.assertEqual(previous.deterministic_refusals, 0)


class Issue06dcf6cbUmbrellaLivenessContractTests(unittest.TestCase):
    """issue:06dcf6cb6aacbc38da5f (board 656): the umbrella liveness contract.

    Silence -> neutral ping -> response window -> replacement; and explicitly: the
    existence of a child process is NOT proof of liveness. A pid that answers `Running`
    while no strong progress has been seen long enough is exactly the ConfirmedStall case;
    a bare heartbeat must not keep a wedged head alive forever.

    Note for the switch (S1-4): today's wait tick always carries a provider snapshot --
    an unadmitted one still witnesses the source -- so the pid-only aging below is the
    policy/builder-reachable arm of the decision (see docs "Pid-only evidence ages"),
    exercised here through ``from_pid_heartbeat`` alone.
    """

    def test_pid_only_running_with_no_progress_for_hours_confirms_the_stall(self) -> None:
        """The decision S1-2 left open, settled here: pid-only Running DOES age into a stall.

        Reading the plan strictly: `Unavailable != no progress` forbids a *dark* source
        from voting for stall, and an unavailable source must not be invented. But issue
        656's own text settles the available-pid case -- the heartbeat IS answering, it
        proves Running and nothing more, and "a process exists" is precisely the weak
        evidence the umbrella refuses to accept as liveness. So a Running pid with no
        progress source that has ever answered ages HealthyQuiet -> SuspectedStall ->
        ConfirmedStall from the episode's start: the absent provider votes for nothing
        (neither progress nor stall), while the pid's silence about progress stops being
        free once it outlives both thresholds.
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
        self.assertEqual(
            episodes[5].confirmed_since, started + THRESHOLDS.suspect_after + THRESHOLDS.confirm_after
        )
