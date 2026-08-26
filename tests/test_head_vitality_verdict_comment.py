"""The vitality verdict comment's idempotency key, and what a refused write may mean.

Card secretary-1471. The verdict comment is deduped on the transition pair, but its body
carries the live ``basis`` measurement, and the board's identity for a comment is that
body's digest. A pair walked twice therefore reused a stable key over a moving payload and
the writer refused it -- as a plain ``TaskError("validation")`` out of the middle of
``_reduce_and_store_vitality_episode``, which aborted the whole per-card advance for that
tick (production, secretary-1468 on 2026-08-26: three ticks lost, no action entry at all).

The route taken here is (a)+(c) of the card: the round joins the key the way
``_review_prompt`` already puts it in the verdict ids, so the second round records its own
walk of the ladder instead of losing it, and the repeat that remains -- one pair walked
twice inside one round -- is recognised before the write, from the dispatcher's own durable
record of the transitions it has commented on. No write failure is swallowed: a request id
claimed by anything else still raises out of the tick.

That record is kept in two steps, because the claim and the comment are two durable
operations and a tick can die between them. The intent -- the id and the exact body claimed
for it -- is written first; the transition counts as recorded only once the write has
answered; and a later tick replays the stored body under the stored id, which the writer
answers as the same operation whether the first attempt reached the board or never started.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("SECRETARY_DISPATCHER_BODY_DIR", tempfile.mkdtemp())

from secretary.dispatcher_state import attempt_request_id, request_token  # noqa: E402
from secretary.tasks import TaskError  # noqa: E402
from tests.test_dispatcher import CARD_REF, DispatcherRuntimeFixture  # noqa: E402


def vitality_comments(case, kind: str = "worker") -> list[str]:
    """The durable vitality-verdict comments for one role, as bodies."""
    return [
        str(comment.get("body") or "")
        for comment in case.reader.show(CARD_REF)["comments"]
        if f"Vitality ({kind}):" in str(comment.get("body") or "")
    ]


class VitalityVerdictCommentTests(DispatcherRuntimeFixture, unittest.TestCase):
    """One card, one attempt, a flapping verdict -- through the real tick."""

    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.host.fail_resume_worker_reason = ""
        self.tick()  # claim + launch; the worker heartbeat binds a live pid
        self._head_at_its_prompt()

    def _advance(self, kind: str = "worker") -> dict:
        """One tick in which the provider cursor moved: an Advancing reading."""
        self.host.provider_cursor = f"cursor:{time.time()!r}"
        return self.tick()

    def _quiet(self, kind: str = "worker", *, aged: float = 0.0) -> dict:
        """One tick in which the provider cursor was re-read unchanged: a Quiet reading.

        ``aged`` moves the episode's quiet reference back before the reduction runs, which
        is the only thing that makes one Quiet reading's ``basis`` differ from another's:
        the token is ``quiet:<seconds>s@provider_cursor``.
        """
        if aged:
            self._age_quiet_reference(kind, aged)
        return self.tick()

    def _age_quiet_reference(self, kind: str, seconds: float) -> None:
        """Move the episode's quiet reference back, so the next Quiet reading measures more.

        The ladder measures quiet from ``last_progress_at`` (falling back to ``started_at``),
        so that is what an aged head's clock has to move -- and moving it is exactly what
        makes the second walk of one transition pair carry a ``basis`` the first one did not.
        """
        payload = self.runtime.production_state.load()
        record = payload["records"][CARD_REF]
        episode = record.get(f"{kind}_vitality_episode")
        if episode is None:
            return
        for name in ("started_at", "updated_at", "last_progress_at"):
            if episode.get(name):
                episode[name] -= seconds
        record[f"{kind}_vitality_episode"] = episode
        self.runtime.production_state.save(payload)

    def _flap_to_a_repeated_transition(self, kind: str = "worker") -> dict:
        """Walk `healthy_active -> healthy_quiet` twice under one attempt and one round, and
        hand back the tick where that pair repeats with a different measured quiet."""
        self._advance(kind)  # the first reading has no earlier cursor to compare against
        self._quiet(kind)
        self._advance(kind)  # healthy_quiet -> healthy_active
        self._quiet(kind)  # healthy_active -> healthy_quiet, measured at quiet:0s
        self._advance(kind)
        return self._quiet(kind, aged=120.0)  # the same pair again, at quiet:120s

    def test_a_repeated_transition_does_not_abort_the_hosting_tick(self) -> None:
        """AC1+AC2: the second walk of one pair is a no-op, not a lost per-card advance."""
        repeated = self._flap_to_a_repeated_transition()

        self.assertEqual(repeated["action"], "waiting-worker-report")
        self.assertNotEqual(repeated.get("status"), "degraded")
        self.assertEqual(repeated["pilot_ref"], CARD_REF)
        self.assertEqual(
            self._pilot_record()["worker_vitality_episode"]["verdict"], "healthy_quiet",
            "the episode itself is stored either way; only the comment was refused",
        )

    def test_a_flapping_verdict_does_not_mint_one_comment_per_tick(self) -> None:
        """AC3: the bound is one comment per attempt, round, role and transition pair."""
        self._flap_to_a_repeated_transition()
        before = vitality_comments(self)
        # Keep flapping across the same two pairs: every one of them is already recorded.
        for _ in range(3):
            self._advance()
            self._quiet(aged=120.0)

        self.assertEqual(vitality_comments(self), before, "a flap wrote a comment per tick")
        pairs = {
            (body.split("(was ", 1)[1].split(")", 1)[0], body.split(": ", 1)[1].split(" ", 1)[0])
            for body in before if "(was " in body
        }
        self.assertEqual(
            len(before), len(pairs) + 1,
            "one comment per distinct pair, plus the first observation",
        )

    def test_a_new_round_records_its_own_walk_of_the_ladder(self) -> None:
        """AC3's other half: the round is in the key, so a reworked card is not silenced.

        This is the production symptom: ``attempt_id`` does not move when a card goes back
        to rework, so round 2's step into a stall used to be swallowed by round 1's record.
        """
        self._flap_to_a_repeated_transition()
        first_round = vitality_comments(self)
        self.assertEqual(self._pilot_record()["report_generation"], 1)

        self._report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.tick()
        self._review_red()
        self._park_and_decide("rework")
        self.assertEqual(self._pilot_record()["report_generation"], 2)
        self._head_at_its_prompt()
        self._advance()
        self._quiet(aged=200.0)

        second_round = vitality_comments(self)
        self.assertGreater(
            len(second_round), len(first_round),
            "round 2 walked the same pairs again and recorded nothing",
        )

    def test_the_review_head_goes_through_the_same_path(self) -> None:
        """AC5: both kinds share the key and the no-op; neither aborts its tick."""
        self._report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")
        self._head_at_its_prompt("review")

        repeated = self._flap_to_a_repeated_transition("review")

        self.assertEqual(repeated["action"], "waiting-review-verdict")
        self.assertNotEqual(repeated.get("status"), "degraded")
        review_comments = vitality_comments(self, "review")
        self.assertTrue(review_comments, "the review head recorded no verdict at all")
        self.assertEqual(
            len(review_comments), len(set(review_comments)),
            "a review flap wrote the same body twice",
        )

    def test_a_validation_refusal_from_the_writer_still_raises(self) -> None:
        """AC4: an unforeseen refusal is not a no-op; only a known repeat is skipped."""
        refusal = TaskError("validation", "request id belongs to another operation or payload", 2)
        with mock.patch.object(self.writer, "comment", side_effect=refusal):
            with self.assertRaises(TaskError) as raised:
                self._advance()
        self.assertEqual(raised.exception.code, "validation")

    def test_a_non_validation_write_failure_still_raises(self) -> None:
        """AC4: nothing here catches a write error at all."""
        broken = TaskError("audit_pending", "backend write committed; audit repair is required", 4)
        with mock.patch.object(self.writer, "comment", side_effect=broken):
            with self.assertRaises(TaskError) as raised:
                self._advance()
        self.assertEqual(raised.exception.code, "audit_pending")

    def test_a_foreign_claim_on_the_derived_request_id_still_raises(self) -> None:
        """AC4: another operation holding this id is a genuine violation, not a repeat.

        ``secretary task comment`` takes both the role and the request id from its caller, so a
        dispatcher-marked comment can be put on the card under the id this transition derives.
        The vitality write then collides with a body that is not its own; reading the refusal
        back out of the comment audit could not tell the two apart, because the audit records a
        role marker and a body digest and neither names the operation.
        """
        record = self._pilot_record()
        request_id = attempt_request_id(
            str(record["attempt_id"]), "worker-vitality-verdict", CARD_REF,
            suffix=request_token(f"{record['report_generation']}:worker:none->healthy_quiet"),
        )
        self.writer.comment(
            role="dispatcher", actor=self.runtime.owner, reference=CARD_REF,
            body="a foreign dispatcher comment holding that id", request_id=request_id,
        )

        with self.assertRaises(TaskError) as raised:
            self._advance()

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(vitality_comments(self), [], "the vitality comment was never written")
        self.assertNotIn(
            f"{record['report_generation']}:worker:none->healthy_quiet",
            self._pilot_record()["vitality_verdicts_written"],
            "a failed write must not leave the transition claimed",
        )

    def _die_on_the_next_vitality_write(self, kind: str = "worker") -> tuple:
        """A writer whose first vitality comment for `kind` dies the way a killed tick does.

        ``SystemExit`` is the point: a process that is being torn down does not run the
        caller's ``except Exception``, so this is the window between the two durable
        operations rather than a mocked backend refusal.
        """
        real = self.writer.comment
        seen: dict[str, str] = {}

        def comment(*, role: str, actor: str, reference: str, body: str, request_id: str | None = None):
            if "body" not in seen and body.startswith(f"Vitality ({kind}):"):
                seen["body"] = body
                seen["request_id"] = str(request_id)
                raise SystemExit("the tick died between the claim and the write")
            return real(role=role, actor=actor, reference=reference, body=body, request_id=request_id)

        return comment, seen

    def test_a_tick_killed_before_the_write_still_records_the_transition(self) -> None:
        """The interrupted write is finished later, with the body it was claimed for.

        A claim taken before the comment used to end the story: the record said the
        transition was written, no comment existed, and no later tick would try again.
        """
        killer, seen = self._die_on_the_next_vitality_write()
        with mock.patch.object(self.writer, "comment", killer):
            with self.assertRaises(SystemExit):
                self._advance()

        record = self._pilot_record()
        self.assertEqual(vitality_comments(self), [], "the write never reached the board")
        self.assertEqual(
            record["vitality_verdicts_written"], [],
            "a write nobody confirmed must not read as a recorded transition",
        )
        self.assertEqual(record["vitality_verdict_pending"]["marker"], "1:worker:none->healthy_quiet")

        self._quiet(aged=120.0)  # an unchanged verdict: it never reaches the comment path itself

        recorded = vitality_comments(self)
        self.assertEqual(len(recorded), 1, "the interrupted transition was lost, or recovered twice")
        self.assertTrue(
            recorded[0].endswith(seen["body"]), recorded[0],
        )
        recovered = self._pilot_record()
        self.assertEqual(recovered["vitality_verdicts_written"], ["1:worker:none->healthy_quiet"])
        self.assertEqual(recovered["vitality_verdict_pending"], {})

    def test_a_killed_review_write_is_recovered_the_same_way(self) -> None:
        """AC5: the recovery is on the shared path, so the review head gets it too."""
        self._report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")
        self._head_at_its_prompt("review")

        killer, seen = self._die_on_the_next_vitality_write("review")
        with mock.patch.object(self.writer, "comment", killer):
            with self.assertRaises(SystemExit):
                self._advance("review")
        self.assertEqual(vitality_comments(self, "review"), [])

        self._quiet("review", aged=120.0)

        recovered = vitality_comments(self, "review")
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0].endswith(seen["body"]), recovered[0])
        self.assertEqual(self._pilot_record()["vitality_verdict_pending"], {})

    def test_a_recovered_write_replays_the_body_it_claimed_the_id_for(self) -> None:
        """The replay is the same operation, not a fresh one over a moved measurement.

        The board's identity for a comment is its body digest, so recovering the write with
        this tick's ``basis`` would be a stable id over a changed payload -- the refusal the
        whole record exists to prevent, arriving now out of the recovery itself.
        """
        killer, seen = self._die_on_the_next_vitality_write()
        with mock.patch.object(self.writer, "comment", killer):
            with self.assertRaises(SystemExit):
                self._advance()

        written: list[tuple[str, str]] = []
        real = self.writer.comment

        def recording(*, role: str, actor: str, reference: str, body: str, request_id: str | None = None):
            written.append((body, str(request_id)))
            return real(role=role, actor=actor, reference=reference, body=body, request_id=request_id)

        with mock.patch.object(self.writer, "comment", recording):
            self._quiet(aged=300.0)

        self.assertIn((seen["body"], seen["request_id"]), written)

    def test_a_recovery_that_the_writer_refuses_still_raises(self) -> None:
        """AC4 across the window: a refused replay is an incident, and is not left to loop."""
        killer, _ = self._die_on_the_next_vitality_write()
        with mock.patch.object(self.writer, "comment", killer):
            with self.assertRaises(SystemExit):
                self._advance()

        refusal = TaskError("validation", "request id belongs to another operation or payload", 2)
        with mock.patch.object(self.writer, "comment", side_effect=refusal):
            with self.assertRaises(TaskError) as raised:
                self._quiet()
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(
            self._pilot_record()["vitality_verdict_pending"], {},
            "a reported failure spends no id, so it must not leave the intent open forever",
        )

    def test_the_written_record_is_bounded_by_the_round(self) -> None:
        """AC3, at the record: the durable set covers one round, not the card's whole life."""
        self._flap_to_a_repeated_transition()
        first_round = list(self._pilot_record()["vitality_verdicts_written"])
        self.assertTrue(all(item.startswith("1:") for item in first_round), first_round)
        self.assertEqual(len(first_round), len(set(first_round)))

        self._report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.tick()
        self._review_red()
        self._park_and_decide("rework")
        self._head_at_its_prompt()
        self._advance()
        self._quiet(aged=200.0)

        second_round = list(self._pilot_record()["vitality_verdicts_written"])
        self.assertTrue(second_round, "round 2 recorded no transition of its own")
        self.assertTrue(all(item.startswith("2:") for item in second_round), second_round)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
