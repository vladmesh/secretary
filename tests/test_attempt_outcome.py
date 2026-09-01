"""Sealed, observational attempt outcome occurrence contracts."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from unittest import mock

from secretary.board.events import AnalyticsOutcomeConflict, BoardEventCanon
from secretary.board.models import Actor, EntityKind, Event, EventKind
from secretary.tasks import TaskError
from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture


def outcome(*, disposition: str = "rework", effect: str = "evt-effect") -> Event:
    return Event(
        event_id="evt-outcome",
        kind=EventKind.ATTEMPT_OUTCOME,
        entity_kind=EntityKind.CARD,
        ref="secretary-1532",
        actor=Actor("dispatcher", "dispatcher"),
        reason="confirmed terminal lifecycle effect",
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        data={
            "version": 1,
            "attempt_id": "attempt-1",
            "attempt": 1,
            "report_generation": 1,
            "sprint_ref": "sprint:1419",
            "specification_revision": None,
            "terminal_state": "in_progress" if disposition == "rework" else "blocked",
            "verdict": "red",
            "disposition": disposition,
            "blocked_reason": "infrastructure" if disposition == "blocked" else None,
            "source_event_ids": {
                "report": None,
                "verdict": None,
                "decision": None,
                "effect": effect,
                "worker_usage": None,
                "review_usage": None,
            },
            "usage_completeness": {"worker": "missing", "review": "missing"},
        },
    )


class AttemptOutcomeTests(unittest.TestCase):
    def test_stage_recovery_and_exact_replay_have_one_natural_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            canon = BoardEventCanon(directory)
            event = outcome()
            canon.stage("outcome-1", event)
            staged = canon.attempt_outcome_occurrences()
            self.assertEqual(len(staged), 1)
            self.assertTrue(staged[0].pending)
            canon.commit("outcome-1", event)
            canon.commit("outcome-1", event)
            committed = canon.attempt_outcome_occurrences()
            self.assertEqual(len(committed), 1)
            self.assertFalse(committed[0].pending)

    def test_conflicting_natural_key_is_a_named_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            canon = BoardEventCanon(directory)
            first = outcome()
            canon.commit("outcome-1", first)
            second = outcome(effect="evt-other")
            second = Event(
                event_id="evt-outcome-other",
                kind=second.kind,
                entity_kind=second.entity_kind,
                ref=second.ref,
                actor=second.actor,
                reason=second.reason,
                occurred_at=second.occurred_at,
                data=second.data,
            )
            canon.commit("outcome-2", second)
            with self.assertRaises(AnalyticsOutcomeConflict):
                canon.attempt_outcome_occurrences()

    def test_unknown_version_and_missingness_are_rejected_by_event_reader(self) -> None:
        event = outcome()
        record = event.to_record("outcome-1")
        record["data"] = dict(record["data"], version=2)
        with self.assertRaisesRegex(ValueError, "unsupported attempt outcome version"):
            Event.from_record(record)
        record = event.to_record("outcome-1")
        record["data"] = dict(record["data"])
        record["data"]["usage_completeness"] = {"worker": "zero", "review": "missing"}
        with self.assertRaisesRegex(ValueError, "completeness"):
            Event.from_record(record)


class AttemptOutcomeLifecycleTests(DispatcherRuntimeFixture, unittest.TestCase):
    def _outcomes(self):
        return self.writer.board_host.canon.attempt_outcome_occurrences(ref=CARD_REF)

    def test_reviewed_red_reslice_preserves_the_verdict_and_disposition(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.tick()
        self._review_red()

        self._park_and_decide("reslice")

        outcome = self._outcomes()[0].event.data
        self.assertEqual((outcome["verdict"], outcome["disposition"]), ("red", "reslice"))
        self.assertEqual(outcome["terminal_state"], "blocked")

    def test_reviewed_green_release_has_one_sealed_outcome(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.tick()
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="green",
            body="looks good",
            request_id="outcome-review-green",
        )

        self._park_and_decide("release")

        outcomes = self._outcomes()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(
            (outcomes[0].event.data["verdict"], outcomes[0].event.data["disposition"]),
            ("green", "release"),
        )
        self.assertFalse(outcomes[0].pending)

    def test_append_failure_after_a_terminal_effect_is_recovered_without_lifecycle_work(self) -> None:
        """The outcome journal is weaker than the transition it observes."""
        self.start_dispatcher()
        self.tick()  # Claim and start the first durable worker round.
        payload = self.runtime.production_state.load()
        records = self.runtime.production_state.records(payload)
        record = records[CARD_REF]
        task = self.reader.show(CARD_REF)

        with mock.patch.object(
            self.writer,
            "attempt_outcome",
            side_effect=TaskError("audit_pending", "append refused", 4),
        ):
            effect = self.runtime.terminal_effect(
                task,
                record,
                target="blocked",
                reason="test terminal effect",
                request_id="attempt-outcome-terminal-effect",
                terminal_state="blocked",
                disposition="blocked",
                verdict="blocked",
                blocked_reason="infrastructure",
            )

        self.assertTrue(effect["event_id"])
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        # This is what the normal terminal handler does after the confirmed
        # effect. Its success is independent of analytics publication.
        records.pop(CARD_REF)
        self.runtime.production_state.put_records(payload, records)
        self.runtime.production_state.save(payload)

        recovered = self.runtime.publish_pending_attempt_outcomes()

        self.assertEqual(recovered, [])
        occurrences = self.writer.board_host.canon.attempt_outcome_occurrences(ref=CARD_REF)
        self.assertEqual(len(occurrences), 1)
        self.assertFalse(occurrences[0].pending)
        self.assertEqual(occurrences[0].event.data["source_event_ids"]["effect"], effect["event_id"])
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.assertEqual(self.runtime.production_state.records(payload), {})
