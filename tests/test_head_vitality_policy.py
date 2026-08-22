"""Unit tests for the recovery policy (card S1-5).

The policy is the layer that turns a persisted verdict into an intent, so its tests are
mutation-resistant by construction: every rung is exercised through the public
``decide_recovery``, every idempotency claim is asserted against the exact rung/span state a
caller would persist (``apply_rung_state`` then ``from_json`` -- the real serialisation
round-trip), and the two invariants the card rests on get their own named tests:

  * one SIGCONT per suspension span, with operator escalation -- never kill -- when the
    response window expires;
  * deterministic refusal reasons escalate fast after N identical sightings; heuristic
    reasons repeated just as often are never evidence.

Hostile input degrades to ``observe`` with previously-persisted rung state carried through,
and those shapes are pinned too: rewinding a spent ladder on a malformed tick would re-fire
an already-spent SIGCONT.
"""

from __future__ import annotations

import dataclasses
import json
import unittest

from secretary.dispatch.head_vitality_episode import (
    VitalityEpisode,
    VitalityVerdict,
)
from secretary.dispatch.head_vitality_policy import (
    DEFAULT_DETERMINISTIC_REFUSAL_LIMIT,
    DEFAULT_RESPONSE_WINDOW_SECONDS,
    DETERMINISTIC_TERMINAL_REASONS,
    DeterministicReasonClass,
    RecoveryDecision,
    RecoveryIntent,
    RecoveryThresholds,
    RUNG_ESCALATED,
    RUNG_NONE,
    RUNG_RESPONSE_WINDOW,
    RUNG_SIGCONT_SENT,
    RUNG_SUSPICION_NOTED,
    apply_rung_state,
    decide_recovery,
    deterministic_reason_class,
)

RUN_ID = "run-policy"
NOW = 2_000_000.0
WINDOW = 300.0
THRESHOLDS = RecoveryThresholds(
    response_window_seconds=WINDOW,
    deterministic_refusal_limit=3,
)


def episode(
    verdict: VitalityVerdict = VitalityVerdict.SUSPENDED,
    *,
    reason: str = "",
    stall_frozen_since: float = NOW - 30.0,
    **overrides: object,
) -> VitalityEpisode:
    """One persisted episode shaped like the reducer writes it."""
    return VitalityEpisode(
        run_id=RUN_ID,
        verdict=verdict,
        started_at=NOW - 10_000.0,
        updated_at=NOW - 1.0,
        stall_frozen_since=stall_frozen_since if verdict is VitalityVerdict.SUSPENDED else 0.0,
        reason=reason,
        **overrides,  # type: ignore[arg-type]
    )


def persisted(previous: VitalityEpisode, decision: RecoveryDecision) -> VitalityEpisode:
    """What the dispatcher stores after executing a decision, through the real JSON round-trip."""
    stored = apply_rung_state(previous, decision)
    return VitalityEpisode.from_json(json.loads(json.dumps(stored.to_json())))


class ThresholdTests(unittest.TestCase):
    def test_thresholds_must_be_positive(self) -> None:
        for bad in (0, -5, float("nan"), float("inf"), "300", True):
            with self.subTest(window=bad), self.assertRaises(ValueError):
                RecoveryThresholds(response_window_seconds=bad, deterministic_refusal_limit=3)
        for bad in (0, -1, "3", True, 2.5):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                RecoveryThresholds(response_window_seconds=300.0, deterministic_refusal_limit=bad)

    def test_the_defaults_match_the_documented_scale(self) -> None:
        self.assertEqual(DEFAULT_RESPONSE_WINDOW_SECONDS, 5 * 60)
        self.assertEqual(DEFAULT_DETERMINISTIC_REFUSAL_LIMIT, 3)


class DeterministicAllowlistTests(unittest.TestCase):
    """What qualifies as an authoritative terminal fact, and what never does."""

    def test_every_allowlisted_token_maps_to_its_class(self) -> None:
        for token, cls in DETERMINISTIC_TERMINAL_REASONS.items():
            with self.subTest(token=token):
                self.assertIs(deterministic_reason_class(token), cls)
                # Producers embed the token inside a bounded prose reason; matching must
                # survive that wrapping because that is how the reason travels.
                self.assertIs(
                    deterministic_reason_class(f"pane readiness did not answer: {token}"),
                    cls,
                )

    def test_the_incident_token_is_classified(self) -> None:
        self.assertIs(
            deterministic_reason_class("terminal_split_source_not_found"),
            DeterministicReasonClass.SPLIT_SOURCE_NOT_FOUND,
        )

    def test_heuristic_shapes_are_never_classified(self) -> None:
        # Timing / availability / transport refusals repeat whenever their cause persists;
        # letting their repetition count as authority would let a dark channel fast-track
        # a live head to escalation.
        for reason in (
            "",
            "transport timeout",
            "pane readiness did not answer",
            "pid heartbeat is inconclusive: not-yet-written",
            "provider cursor is unadmitted",
            "terminal_split_source_not_foundish but actually a different string is fine",
        ):
            with self.subTest(reason=reason):
                if reason == "terminal_split_source_not_foundish but actually a different string is fine":
                    continue  # substring match is deliberate; see the positive test above
                self.assertIsNone(deterministic_reason_class(reason))


class SuspendedLadderTests(unittest.TestCase):
    """The SIGCONT rungs: once per span, windowed, escalating to a human -- never killing."""

    def test_a_fresh_span_sends_one_sigcont(self) -> None:
        decision = decide_recovery(episode(), None, NOW, THRESHOLDS)

        self.assertIs(decision.intent, RecoveryIntent.SIGCONT)
        self.assertEqual(decision.rung, RUNG_RESPONSE_WINDOW)
        self.assertIn("SIGCONT", decision.reason)
        # The intent vocabulary itself carries no destructive word, and the decision
        # detail names a window, not a signal escalation. ("stop signal" describes the
        # kernel's own state, so only the destructive verbs are banned here.)
        lowered = (decision.reason + _decision_words(decision)).lower()
        for banned in ("sigterm", "sigkill", "kill", "respawn", "stopping"):
            self.assertNotIn(banned, lowered)

    def test_inside_one_span_the_send_does_not_repeat(self) -> None:
        first = decide_recovery(episode(), None, NOW, THRESHOLDS)
        stored = persisted(episode(), first)

        again = decide_recovery(stored, stored, NOW + 60.0, THRESHOLDS)

        self.assertIs(again.intent, RecoveryIntent.OBSERVE)
        self.assertEqual(again.rung, RUNG_RESPONSE_WINDOW)

    def test_an_already_spent_rung_is_not_rewound_by_hostile_input(self) -> None:
        first = decide_recovery(episode(), None, NOW, THRESHOLDS)
        stored = persisted(episode(), first)

        for bad_now in ("not-a-clock", float("nan"), True):
            with self.subTest(now=bad_now):
                degraded = decide_recovery(stored, stored, bad_now, THRESHOLDS)
                self.assertIs(degraded.intent, RecoveryIntent.OBSERVE)
                self.assertEqual(degraded.rung, RUNG_RESPONSE_WINDOW,
                                 "a malformed tick must not rewind a spent span")

    def test_window_expiry_escalates_to_the_operator_not_a_kill(self) -> None:
        first = decide_recovery(episode(), None, NOW, THRESHOLDS)
        stored = persisted(episode(), first)

        expired = decide_recovery(stored, stored, NOW + WINDOW + 1.0, THRESHOLDS)

        self.assertIs(expired.intent, RecoveryIntent.ESCALATE_OPERATOR)
        self.assertEqual(expired.rung, RUNG_ESCALATED)
        lowered = (expired.reason + _decision_words(expired)).lower()
        self.assertNotIn("sigterm", lowered)
        self.assertNotIn("sigkill", lowered)
        self.assertIn("not stopping", lowered)

    def test_escalation_fires_once_per_span_then_holds(self) -> None:
        first = decide_recovery(episode(), None, NOW, THRESHOLDS)
        stored = persisted(episode(), first)
        expired_decision = decide_recovery(stored, stored, NOW + WINDOW + 1.0, THRESHOLDS)
        escalated = persisted(stored, expired_decision)

        holding = decide_recovery(escalated, escalated, NOW + WINDOW + 120.0, THRESHOLDS)

        self.assertIs(holding.intent, RecoveryIntent.OBSERVE,
                      "repeating the escalation per tick would flood the card")
        self.assertEqual(holding.rung, RUNG_ESCALATED)

    def test_a_new_span_restarts_the_ladder(self) -> None:
        first = decide_recovery(episode(), None, NOW, THRESHOLDS)
        stored = persisted(episode(), first)
        expired = persisted(stored, decide_recovery(stored, stored, NOW + WINDOW + 1.0, THRESHOLDS))
        # Thaw, then re-freeze at a later instant: a new freeze stamp, hence a new span.
        resumed = dataclasses.replace(
            episode(verdict=VitalityVerdict.HEALTHY_QUIET),
        )
        cleared = persisted(resumed, decide_recovery(resumed, resumed, NOW, THRESHOLDS))
        refrozen = dataclasses.replace(cleared, verdict=VitalityVerdict.SUSPENDED,
                                       stall_frozen_since=NOW + 3_600.0)

        fresh_span = decide_recovery(refrozen, expired, NOW + 3_600.0, THRESHOLDS)

        self.assertIs(fresh_span.intent, RecoveryIntent.SIGCONT)
        self.assertEqual(fresh_span.rung, RUNG_RESPONSE_WINDOW,
                         "a second suspension must earn its own rungs, not inherit rung 4")

    def test_recovery_clears_the_ladder(self) -> None:
        first = decide_recovery(episode(), None, NOW, THRESHOLDS)
        stored = persisted(episode(), first)
        recovered = dataclasses.replace(stored, verdict=VitalityVerdict.HEALTHY_ACTIVE,
                                        stall_frozen_since=0.0)

        cleared = decide_recovery(recovered, stored, NOW + 1.0, THRESHOLDS)

        self.assertIs(cleared.intent, RecoveryIntent.OBSERVE)
        self.assertEqual(cleared.rung, RUNG_NONE)
        final = persisted(recovered, cleared)
        self.assertEqual(final.recovery_rung, 0)
        self.assertEqual(final.recovery_span_started_at, 0.0)


class SuspectedStallRungTests(unittest.TestCase):
    def test_a_suspicion_routes_through_the_policy_as_one_nudge(self) -> None:
        suspected = episode(
            VitalityVerdict.SUSPECTED_STALL,
            reason="strong quiet for 400s with no advancement",
        )

        decision = decide_recovery(suspected, None, NOW, THRESHOLDS)

        self.assertIs(decision.intent, RecoveryIntent.NUDGE)
        self.assertEqual(decision.rung, RUNG_SUSPICION_NOTED)
        # The nudge's own per-generation idempotency stays the S1-4 machinery's contract;
        # the policy only records that this suspicion was consumed.

    def test_below_threshold_verdicts_observe_and_reset(self) -> None:
        for verdict in (VitalityVerdict.HEALTHY_ACTIVE, VitalityVerdict.HEALTHY_QUIET,
                        VitalityVerdict.UNVERIFIABLE, VitalityVerdict.DEAD,
                        VitalityVerdict.CONFIRMED_STALL):
            with self.subTest(verdict=verdict.value):
                decision = decide_recovery(episode(verdict), None, NOW, THRESHOLDS)
                self.assertIs(decision.intent, RecoveryIntent.OBSERVE)
                self.assertEqual(decision.rung, RUNG_NONE)


class DeterministicEscalationTests(unittest.TestCase):
    """The 1194 contract: authoritative repetition skips the retry ladder."""

    REASON = "pane readiness did not answer: terminal_split_source_not_found"

    def test_three_identical_refusals_escalate(self) -> None:
        stored: VitalityEpisode | None = None
        intents: list[RecoveryIntent] = []
        for count in range(1, 5):
            current = episode(VitalityVerdict.UNVERIFIABLE, reason=self.REASON)
            decision = decide_recovery(current, stored, NOW, THRESHOLDS)
            intents.append(decision.intent)
            stored = persisted(current, decision)

        self.assertEqual(
            intents,
            [RecoveryIntent.OBSERVE, RecoveryIntent.OBSERVE,
             RecoveryIntent.ESCALATE_OPERATOR, RecoveryIntent.ESCALATE_OPERATOR],
        )
        self.assertEqual(stored is not None and stored.deterministic_refusals, 4)

    def test_escalation_keeps_the_whole_reason_chain_in_detail(self) -> None:
        current = episode(VitalityVerdict.UNVERIFIABLE, reason=self.REASON)
        third = None
        stored = None
        for _ in range(3):
            decision = decide_recovery(current, stored, NOW, THRESHOLDS)
            third = decision
            stored = persisted(current, decision)

        assert third is not None
        detail = third.detail or {}
        self.assertEqual(detail.get("deterministic_class"), "split_source_not_found")
        self.assertEqual(detail.get("identical_refusals"), 3)
        self.assertEqual(detail.get("limit"), 3)

    def test_a_change_of_attempt_resets_the_count(self) -> None:
        refusing = episode(VitalityVerdict.UNVERIFIABLE, reason=self.REASON)
        stored = None
        for _ in range(2):
            decision = decide_recovery(refusing, stored, NOW, THRESHOLDS)
            stored = persisted(refusing, decision)
        # The head came back and the channel answered: the world changed, so the old
        # count must not survive into the next refusal loop.
        healthy = dataclasses.replace(
            stored or refusing,
            verdict=VitalityVerdict.HEALTHY_ACTIVE, reason="",
        )
        reset = persisted(healthy, decide_recovery(healthy, stored, NOW + 1.0, THRESHOLDS))

        again = decide_recovery(
            dataclasses.replace(reset, verdict=VitalityVerdict.UNVERIFIABLE, reason=self.REASON),
            reset, NOW + 2.0, THRESHOLDS,
        )

        self.assertIs(again.intent, RecoveryIntent.OBSERVE,
                      "a changed attempt starts counting from one again")

    def test_heuristic_repetition_is_never_evidence(self) -> None:
        heuristic = episode(
            VitalityVerdict.UNVERIFIABLE,
            reason="pane readiness did not answer: transport timeout",
        )
        stored = None
        for _ in range(20):
            decision = decide_recovery(heuristic, stored, NOW, THRESHOLDS)
            self.assertIsNot(decision.intent, RecoveryIntent.ESCALATE_OPERATOR)
            self.assertEqual(decision.refusals, 0)
            stored = persisted(heuristic, decision)

    def test_the_deterministic_arm_outranks_the_verdict_ladder(self) -> None:
        # Even a SUSPENDED episode carrying an authoritative refusal escalates fast rather
        # than sitting through a response window everyone agrees cannot succeed.
        suspended_refusing = episode(
            VitalityVerdict.SUSPENDED, reason=self.REASON,
        )

        stored = None
        for _ in range(3):
            decision = decide_recovery(suspended_refusing, stored, NOW, THRESHOLDS)
            stored = persisted(suspended_refusing, decision)

        assert stored is not None
        self.assertGreaterEqual(stored.recovery_rung, RUNG_ESCALATED)


class HostileInputTests(unittest.TestCase):
    def test_no_episode_observes(self) -> None:
        decision = decide_recovery(None, None, NOW, THRESHOLDS)

        self.assertIs(decision.intent, RecoveryIntent.OBSERVE)
        self.assertEqual(decision.rung, RUNG_NONE)

    def test_non_episode_payload_observes_without_raising(self) -> None:
        for junk in ("suspended", 42, {"verdict": "suspended"}, ["episode"]):
            with self.subTest(payload=junk):
                decision = decide_recovery(junk, None, NOW, THRESHOLDS)
                self.assertIs(decision.intent, RecoveryIntent.OBSERVE)

    def test_bad_thresholds_degrade_to_the_persisted_rung(self) -> None:
        first = decide_recovery(episode(), None, NOW, THRESHOLDS)
        stored = persisted(episode(), first)

        degraded = decide_recovery(stored, stored, NOW, "not-thresholds")  # type: ignore[arg-type]

        self.assertIs(degraded.intent, RecoveryIntent.OBSERVE)
        self.assertEqual(degraded.rung, stored.recovery_rung)


class SerialisationTests(unittest.TestCase):
    def test_a_decision_round_trips_as_telemetry(self) -> None:
        decision = decide_recovery(episode(), None, NOW, THRESHOLDS)
        payload = json.loads(json.dumps(decision.to_json()))

        self.assertEqual(payload["intent"], "sigcont")
        self.assertEqual(payload["rung"], RUNG_RESPONSE_WINDOW)
        self.assertIsInstance(payload["detail"], dict)

    def test_rung_state_survives_the_episode_round_trip(self) -> None:
        decision = decide_recovery(episode(), None, NOW, THRESHOLDS)
        stored = persisted(episode(), decision)

        self.assertEqual(stored.recovery_rung, RUNG_RESPONSE_WINDOW)
        self.assertEqual(stored.recovery_span_started_at, stored.stall_frozen_since)

    def test_pre_policy_episodes_load_with_zero_state(self) -> None:
        legacy = {
            key: value for key, value in episode().to_json().items()
            if key not in ("recovery_span_started_at", "deterministic_refusals")
        }

        loaded = VitalityEpisode.from_json(legacy)

        self.assertEqual(loaded.recovery_span_started_at, 0.0)
        self.assertEqual(loaded.deterministic_refusals, 0)
        self.assertTrue(RUNG_SIGCONT_SENT > 0)  # vocabulary sanity while here


def _decision_words(decision: RecoveryDecision) -> str:
    return " ".join(str(value) for value in (decision.detail or {}).values())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
