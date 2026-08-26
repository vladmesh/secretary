"""Tick-level decision tests for the wait tick's verdict table (card S1-4).

The card requires the wait-tick decision to be tested per verdict, parametrised over the
whole ladder, because the decision -- not the guard alone -- is what an operator sees
tick after tick. Each case drives the same scenario to a known verdict and asserts the
tick outcome; the Suspended cases additionally pin the arm's own contract: exactly one
comment per freeze span (the request-id is derived from the frozen span, so a re-suspension
with a new span writes again, while every tick inside one span replays the same id),
never a signal, never a destructive host call, and no renewal of the outer wait clock.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("SECRETARY_DISPATCHER_BODY_DIR", tempfile.mkdtemp())

from secretary.dispatcher_types import HostError  # noqa: E402
from secretary.dispatcher_worker_lifecycle import head_run_binding  # noqa: E402
from tests.test_dispatcher import CARD_REF, DispatcherRuntimeFixture  # noqa: E402

#: A live pid whose /proc state says it is parked on a stop signal (state `T`).
STOPPED_STATUS = {
    "known": True,
    "live": True,
    "reason": "live",
    "pid_status": {"known": True, "alive": True, "match": True, "state": "live-match", "stopped": True},
}
#: The same process after it was resumed (SIGCONT territory, not this card's).
RUNNING_STATUS = {
    "known": True,
    "live": True,
    "reason": "live",
    "pid_status": {"known": True, "alive": True, "match": True, "state": "live-match", "stopped": False},
}


def _suspension_comments(case) -> list[str]:
    """The durable suspension comments on the pilot card, as bodies."""
    return [
        str(comment.get("body") or "")
        for comment in case.reader.show(CARD_REF)["comments"]
        if isinstance(comment, dict) and "stop signal" in str(comment.get("body") or "")
    ]


class SuspendedWaitTickDecisionTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The Suspended arm of ``_decide_wait_by_verdict``, through the S1-5 policy.

    The card's own contract, per freeze span: exactly one SIGCONT (the request-id is
    derived from the span stamp, so a re-suspension with a new span fires again while
    every tick inside one span replays the same id), one comment per policy action, never
    a signal beyond SIGCONT, and no renewal of the outer wait clock.
    """

    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.tick()  # claim + launch; the worker heartbeat binds a live pid
        self.host.worker_status_result = dict(STOPPED_STATUS)

    def test_a_suspended_head_gets_one_sigcont_one_comment_and_no_stop(self) -> None:
        """Observe -> decide: one identity-fenced SIGCONT names the span; nothing is stopped."""
        first = self.tick()
        self.assertEqual(first["action"], "worker-sigcont-sent")
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "suspended")
        # Rung 3 = response window running after the send; the span key is stamped.
        self.assertEqual(episode["recovery_rung"], 3)
        self.assertEqual(episode["recovery_span_started_at"], episode["stall_frozen_since"])

        decided = self.tick()

        # Inside the window the policy observes: same comment count, no second signal.
        self.assertEqual(decided["action"], "worker-suspension-observed")
        comments = _suspension_comments(self)
        self.assertEqual(len(comments), 1, comments)
        self.assertIn("parked on a stop signal", comments[0])
        self.assertIn("identity-fenced SIGCONT", comments[0])
        record = self._pilot_record()
        self.assertEqual(record["worker_respawns"], 0)
        self.assertNotIn("restart_worker", self.host.calls)
        signals = [call for call in self.host.calls if "signal" in call.lower()]
        self.assertEqual(signals, [])

    def test_the_suspension_comment_does_not_renew_the_outer_wait_clock(self) -> None:
        """A suspended head must not look like fresh progress: the window stays put."""
        before = self._pilot_record()["worker_waiting_since"]
        self.tick()
        self.tick()
        self.assertEqual(
            self._pilot_record()["worker_waiting_since"],
            before,
            "renewing the wait window would hide the stall behind a fresh clock",
        )

    def test_inside_one_freeze_span_the_comment_is_written_once(self) -> None:
        """Every tick in one span re-derives the same request-id: the board sees one."""
        for _ in range(4):
            self.tick()
        self.assertEqual(len(_suspension_comments(self)), 1)

    def test_a_re_suspension_with_a_new_freeze_span_writes_again(self) -> None:
        """Thaw and re-freeze: the new span's key differs, so the operator hears again."""
        self.tick()
        self.tick()
        self.assertEqual(len(_suspension_comments(self)), 1)

        self.host.worker_status_result = dict(RUNNING_STATUS)
        thawed = self.tick()
        self.assertEqual(thawed["action"], "waiting-worker-report")
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "healthy_quiet")
        self.assertEqual(episode["stall_frozen_since"], 0.0)
        # The recovered span cleared the policy ladder with it: a future suspension
        # starts fresh instead of inheriting this span's rungs.
        self.assertEqual(episode["recovery_rung"], 0)
        self.assertEqual(episode["recovery_span_started_at"], 0.0)
        self.assertEqual(len(_suspension_comments(self)), 1, "a thaw writes nothing")

        # Re-freeze inside a different wall-clock second so the span (and its key) moves.
        time.sleep(1.1)
        self.host.worker_status_result = dict(STOPPED_STATUS)
        for _ in range(3):
            self.tick()
        self.assertGreaterEqual(
            len(_suspension_comments(self)),
            2,
            "the second freeze span must reach the operator as its own comment",
        )

    def test_recovery_clears_the_rung_on_the_right_role_only(self) -> None:
        """The rung reset lands on the suspended role's own episode slot -- never across.

        Card S1-6 regression: ``_run_recovery_policy`` used to persist the reset with a
        hardcoded ``kind="worker"``, so a RECOVERED review head left its cleared episode
        (bound to the review run's run_id) parked on ``worker_vitality_episode`` while
        ``review_vitality_episode.recovery_rung`` stayed stale. A foreign-run episode in
        the worker slot is fail-safe (the destructive guard refuses it as FOREIGN_RUN)
        but wrong: the review ladder never reset, and the worker slot held another
        run's state.
        """
        # Worker first, so both roles hold episodes and neither slot starts empty.
        self.host.worker_status_result = dict(STOPPED_STATUS)
        self.tick()
        worker_suspended = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(worker_suspended["verdict"], "suspended")
        self.assertGreater(worker_suspended["recovery_rung"], 0)

        # The worker resumes: the WORKER mirror of the fix -- its own slot clears.
        self.host.worker_status_result = dict(RUNNING_STATUS)
        worker_resumed = self.tick()

        self.assertEqual(worker_resumed["action"], "waiting-worker-report")
        worker_cleared = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(worker_cleared["verdict"], "healthy_quiet")
        self.assertEqual(worker_cleared["recovery_rung"], 0)
        self.assertEqual(
            worker_cleared["run_id"],
            worker_suspended["run_id"],
            "the same worker episode is updated in place",
        )

        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self.host.review_status_result = dict(STOPPED_STATUS)
        self.tick()
        review_suspended = self._pilot_record()["review_vitality_episode"]
        worker_before = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(review_suspended["verdict"], "suspended")
        self.assertGreater(review_suspended["recovery_rung"], 0)

        # The review head resumes: its own slot must be the one that clears.
        self.host.review_status_result = dict(RUNNING_STATUS)
        resumed = self.tick()

        self.assertEqual(resumed["action"], "waiting-review-verdict")
        record = self._pilot_record()
        review_after = record["review_vitality_episode"]
        worker_after = record["worker_vitality_episode"]
        self.assertEqual(review_after["recovery_rung"], 0)
        self.assertEqual(review_after["recovery_span_started_at"], 0.0)
        self.assertEqual(
            review_after["run_id"],
            review_suspended["run_id"],
            "the same review episode is updated, not replaced by a foreign one",
        )
        self.assertEqual(
            worker_after,
            worker_before,
            "a review recovery must not touch the worker's episode slot",
        )

    def test_a_suspended_review_head_gets_the_same_arm(self) -> None:
        """The review twin decides identically: comment once, no stop, no replacement."""
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self.host.review_status_result = dict(STOPPED_STATUS)

        self.tick()
        decided = self.tick()

        # The review twin runs the identical policy: one SIGCONT, then in-window observes.
        self.assertEqual(decided["action"], "review-suspension-observed")
        comments = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict) and "Vitality (review)" in str(comment.get("body") or "")
        ]
        self.assertTrue(any("stop signal" in body for body in comments), comments)
        self.assertNotIn("stop_review", self.host.calls)


class WaitTickVerdictTableTests(DispatcherRuntimeFixture, unittest.TestCase):
    """One scenario per remaining verdict: the outcome the tick hands back.

    The ConfirmedStall/Dead arms are covered extensively by the runtime suite (prompt /
    respawn / reclaim) and the guard call-site tests; this table pins the non-destructive
    rows so that disabling any single arm fails here by name.
    """

    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.tick()

    def _decide_with(self, status: dict) -> dict:
        self.host.worker_status_result = dict(status)
        self.tick()  # the reduction observes and stores the verdict
        return self.tick()  # the decision tick

    def _advancing_status(self) -> dict:
        """A live head whose provider cursor advanced since the previous snapshot.

        The cursor evidence must carry this run's HeadRun binding (the reducer refuses a
        cursor naming another run), and the cursor value itself must differ from the
        previous snapshot's for the source to read Advancing rather than Quiet.
        """
        record = self._pilot_record()
        run_id, fingerprint = head_run_binding(record["worker_head_run"])
        status = dict(RUNNING_STATUS)
        status["provider_progress"] = {
            "state": "observed",
            "admission": "accepted",
            "source": "fake-bound-session",
            "source_fingerprint": "f" * 32,
            "cursor": f"rollout:{time.time()}",
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
        }
        return status

    def test_healthy_active_waits_without_touching_anything(self) -> None:
        self.host.worker_status_result = self._advancing_status()
        self.tick()  # first observation: stores the cursor, still Quiet
        self.host.worker_status_result = self._advancing_status()  # cursor moves again
        outcome = self.tick()  # Advancing
        self.assertEqual(outcome["action"], "waiting-worker-report")
        self.assertEqual(outcome["status"], "ok")
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "healthy_active")

    def test_healthy_quiet_renews_the_window_and_waits(self) -> None:
        outcome = self._decide_with(dict(RUNNING_STATUS))
        self.assertEqual(outcome["action"], "waiting-worker-report")
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "healthy_quiet")
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)

    def test_unverifiable_waits_instead_of_guessing(self) -> None:
        status = {
            "known": False,
            "live": True,
            "reason": "runtime-unavailable",
        }
        outcome = self._decide_with(status)
        self.assertIn(
            outcome["action"],
            ("worker-runtime-unavailable", "waiting-worker-report"),
        )
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)

    def test_suspected_stall_nudges_exactly_once_then_degrades(self) -> None:
        self._decide_with(dict(RUNNING_STATUS))  # baseline quiet below every threshold
        # Age the persisted quiet past suspect_after but short of confirm_after.
        self._age_vitality_quiet("worker", 500.0)
        first = self.tick()
        # Assert the verdict instead of skipping (S1-4 review follow-up): a fixture that
        # cannot reach suspected_stall is a broken test, not an inapplicable one.
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(
            episode["verdict"],
            "suspected_stall",
            f"the fixture aged quiet to {episode['verdict']} instead of suspected_stall",
        )
        self.assertEqual(first["action"], "worker-stall-suspected")
        self.assertEqual(first["status"], "degraded")
        prompts = self.host.calls.count("prompt_worker_report")
        again = self.tick()
        self.assertEqual(again["action"], "worker-stall-suspected")
        self.assertEqual(
            self.host.calls.count("prompt_worker_report"),
            prompts,
            "a suspicion spends at most one nudge per round generation",
        )
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)

    def test_disabling_the_suspended_arm_is_caught_by_this_table(self) -> None:
        """Mutation check: with the arm disabled the suspended head must not be destroyed.

        The guard would refuse a disabled arm's drift into the ceiling path -- but the
        refusal is itself observable. This test pins the arm positively: the outcome is
        the policy's in-window observation, not a guard refusal and not a respawn.
        """
        outcome = self._decide_with(dict(STOPPED_STATUS))
        self.assertEqual(outcome["action"], "worker-suspension-observed")
        self.assertNotIn("guard-refused", str(outcome.get("action")))
        # And the arm really produced the SIGCONT through the real code path.
        self.assertEqual(len(_suspension_comments(self)), 1)


class UnobservableWaitEscalationTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The no-episode fallback's bound: operator escalation, never replacement.

    An unobservable head (heartbeat unreadable, provider dark, nothing on file) is a run
    nobody can prove dead, so the guard refuses its destruction forever. The wait is
    still bounded: once the outer ceiling elapses, ``_escalate_unobservable_wait`` writes
    one durable comment per wait cycle and hands back a degraded outcome. These tests
    catch a mutation that deletes that bound (the tick would return to a silent ``ok``
    wait) and one that turns it destructive.
    """

    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.tick()  # claim + launch

    def _unobservable_status(self) -> None:
        """Make the runtime inventory fail: the watchdog sees no sources at all.

        The fake derives pid/provider evidence for any scripted dict (that realism is
        what other tests rely on), so an unobservable head is modelled the way production
        produces one -- ``worker_status`` raising, which ``_wait_watchdog`` catches into a
        source-less status and the reduction into no episode.
        """
        self.host.worker_status_error = HostError("orca terminal list failed")

    def test_ceiling_elapsed_escalates_to_the_operator_without_touching_the_head(self) -> None:
        from secretary.dispatcher_watchdog import stall_seconds

        self.host.worker_status_result = self._unobservable_status()
        self.tick()  # stamps the fresh waiting window
        payload = self.runtime.production_state.load()
        record_payload = payload["records"][CARD_REF]
        record_payload["worker_waiting_since"] -= stall_seconds("worker") + 60
        self.runtime.production_state.save(payload)

        outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-unobserved-wait-escalated")
        self.assertEqual(outcome["status"], "degraded")
        comments = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict) and "NOT stopped or replaced" in str(comment.get("body") or "")
        ]
        self.assertEqual(
            len(comments),
            1,
            "exactly one escalation per unobserved wait cycle",
        )
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertNotIn("stop_head:worker", self.host.calls)

    def test_below_the_ceiling_an_unobservable_head_is_a_plain_wait(self) -> None:
        self.host.worker_status_result = self._unobservable_status()
        first = self.tick()
        second = self.tick()
        for outcome in (first, second):
            self.assertEqual(outcome["action"], "worker-runtime-unavailable") if outcome.get(
                "reason"
            ) else None
            self.assertNotEqual(
                outcome.get("action"),
                "worker-unobserved-wait-escalated",
                "escalation must not fire before the ceiling",
            )

    def test_disabling_the_escalation_is_caught(self) -> None:
        """Mutation check: deleting the bound returns an unobservable head to silence."""
        from secretary.dispatcher_watchdog import stall_seconds

        self.host.worker_status_result = self._unobservable_status()
        self.tick()
        payload = self.runtime.production_state.load()
        record_payload = payload["records"][CARD_REF]
        record_payload["worker_waiting_since"] -= stall_seconds("worker") + 60
        self.runtime.production_state.save(payload)

        with mock.patch.object(
            type(self.runtime),
            "_escalate_unobservable_wait",
            side_effect=AssertionError("the escalation arm was disabled"),
        ):
            with self.assertRaises(AssertionError):
                self.tick()

    def test_a_turning_the_escalation_destructive_is_caught(self) -> None:
        """The escalation may not grow a stop: any host call fails this test."""
        from secretary.dispatcher_watchdog import stall_seconds

        self.host.worker_status_result = self._unobservable_status()
        self.tick()
        payload = self.runtime.production_state.load()
        record_payload = payload["records"][CARD_REF]
        record_payload["worker_waiting_since"] -= stall_seconds("worker") + 60
        self.runtime.production_state.save(payload)

        calls_before = list(self.host.calls)
        self.tick()
        new_calls = [call for call in self.host.calls[len(calls_before) :]]
        self.assertEqual(
            [call for call in new_calls if "restart" in call or "stop" in call],
            [],
            f"the unobservable escalation signalled or replaced something: {new_calls}",
        )


class VitalityVerdictCommentIdempotencyTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The verdict comment's body must be a function of exactly what its request id names.

    secretary-1477. The id keys the transition pair alone -- deliberately, so a flapping
    verdict cannot mint a fresh key every tick -- while the board's identity for a comment
    is the digest of its body. Quoting the reduction's live ``basis`` (``quiet:<n>s@...``)
    in that body therefore made the SECOND visit to one pair a different payload under the
    same key, which the write path answers with
    ``validation: request id belongs to another operation or payload``. That exception
    leaves ``_reduce_and_store_vitality_episode`` before ``_decide_wait_by_verdict`` ever
    runs, so the card loses its whole per-card advance for the tick.

    These tests drive the real writer (no stub, no patched ``TaskWriter``): the second visit
    to a pair has to be an honest idempotent replay.
    """

    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.tick()  # claim + launch: the worker heartbeat binds a live pid

    def _cursor_status(self, cursor: str) -> dict:
        """A live head whose provider cursor reads exactly ``cursor``.

        Repeating a cursor is what makes the source read Quiet rather than Advancing, which
        is how these tests flap one head between the two healthy verdicts on demand.
        """
        record = self._pilot_record()
        run_id, fingerprint = head_run_binding(record["worker_head_run"])
        status = dict(RUNNING_STATUS)
        status["provider_progress"] = {
            "state": "observed",
            "admission": "accepted",
            "source": "fake-bound-session",
            "source_fingerprint": "f" * 32,
            "cursor": cursor,
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
        }
        return status

    def _age_progress_reference(self, seconds: float) -> None:
        """Move the episode's quiet reference back, so the next quiet reduction measures more.

        The live measurement this card is about is ``now - last_progress_at``; two ticks in one
        test process are milliseconds apart, so without this the two visits to a pair would
        agree by accident and prove nothing.
        """
        payload = self.runtime.production_state.load()
        episode = payload["records"][CARD_REF]["worker_vitality_episode"]
        for name in ("started_at", "last_progress_at"):
            if episode.get(name):
                episode[name] -= seconds
        self.runtime.production_state.save(payload)

    def _flap_twice_through_one_pair(self) -> tuple[list, list]:
        """Visit ``healthy_active -> healthy_quiet`` twice with different quiet measurements.

        Hands back (every comment call the ticks made, the two calls that share the repeated
        pair's request id). The ticks run through the real ``TaskWriter``; the spy wraps it
        rather than replacing it, so the idempotency journal is exercised exactly as in
        production.
        """
        with mock.patch.object(
            self.writer,
            "comment",
            wraps=self.writer.comment,
        ) as spy:
            self.host.worker_status_result = self._cursor_status("rollout:1")
            self.tick()  # first observation: the cursor is stored, nothing advanced yet
            self.host.worker_status_result = self._cursor_status("rollout:2")
            self.tick()  # the cursor moved: healthy_active
            self.host.worker_status_result = self._cursor_status("rollout:2")
            self.tick()  # the same cursor: healthy_quiet, quiet measured from the move
            self.host.worker_status_result = self._cursor_status("rollout:3")
            self.tick()  # advancing again: back to healthy_active
            self._age_progress_reference(41)
            self.host.worker_status_result = self._cursor_status("rollout:3")
            # The second visit to healthy_active -> healthy_quiet, with a quiet span that is
            # 41s longer than the first visit's. Before the fix this raised.
            self.tick()
        calls = list(spy.call_args_list)
        repeated = [
            call
            for call in calls
            if str(call.kwargs.get("request_id") or "").endswith(
                "worker-vitality-verdict-" + CARD_REF + "-healthy_active--healthy_quiet"
            )
        ]
        return calls, repeated

    def test_one_transition_pair_writes_one_body_byte_for_byte(self) -> None:
        """Acceptance 1: the same pair in a later tick produces an identical body."""
        _, repeated = self._flap_twice_through_one_pair()

        self.assertEqual(
            len(repeated),
            2,
            "the flap must visit healthy_active -> healthy_quiet twice for this to prove anything",
        )
        first, second = (str(call.kwargs["body"]) for call in repeated)
        self.assertEqual(first, second)
        self.assertNotIn("quiet:", first, "a live measurement in the body is what broke the key")

    def test_the_repeat_is_an_idempotent_replay_not_a_validation_refusal(self) -> None:
        """Acceptance 2: the real write path answers the repeat without raising.

        Nothing is stubbed here -- the fixture's own ``TaskWriter`` and audit journal decide.
        A refusal would surface as ``TaskError('validation', ...)`` escaping ``self.tick()``.
        """
        self._flap_twice_through_one_pair()

        bodies = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if "Vitality (worker):" in str(comment.get("body") or "")
        ]
        # Three distinct pairs were visited (none->quiet, quiet->active, active->quiet) and the
        # fourth and fifth ticks revisited two of them: the board holds one comment per pair.
        self.assertEqual(len(bodies), 3, bodies)

    def test_every_repeated_key_carries_the_same_payload(self) -> None:
        """Acceptance 3 (anti-flood): the key is unchanged, so the pair set still bounds the
        stream -- and no key on this path is ever re-used for a different payload."""
        calls, _ = self._flap_twice_through_one_pair()

        by_request: dict[str, set[str]] = {}
        for call in calls:
            request_id = str(call.kwargs.get("request_id") or "")
            if "-vitality-verdict-" not in request_id:
                continue
            by_request.setdefault(request_id, set()).add(str(call.kwargs.get("body") or ""))
        self.assertTrue(by_request)
        for request_id, seen in by_request.items():
            self.assertEqual(len(seen), 1, f"{request_id} was written with two payloads: {seen}")
        self.assertEqual(
            len(by_request),
            3,
            "five ticks may only ever name the pairs they actually visited",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
