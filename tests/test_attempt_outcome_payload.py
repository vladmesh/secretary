"""Typed payload coverage for the closed ``attempt.outcome`` board-event schema."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from secretary.board.attempt_outcome import (
    AttemptOutcomeCompleteness,
    AttemptOutcomeLineageRequired,
    AttemptOutcomePayload,
    AttemptOutcomeSourceEventIds,
    AttemptOutcomeTerminalState,
    AttemptOutcomeUsageCompleteness,
    AttemptOutcomeVerdict,
)
from secretary.board.events import AttemptOutcomeOccurrence
from secretary.board.models import Actor, EntityKind, Event, EventKind
from secretary.board.roles import Role


class AttemptOutcomePayloadTests(unittest.TestCase):
    def _v1_payload(self) -> AttemptOutcomePayload:
        return AttemptOutcomePayload(
            version=1,
            attempt_id="attempt-2",
            attempt=2,
            report_generation=3,
            sprint_ref="sprint:1",
            specification_revision=None,
            terminal_state=AttemptOutcomeTerminalState.IN_PROGRESS,
            verdict=AttemptOutcomeVerdict.RED,
            disposition="rework",
            blocked_reason=None,
            source_event_ids=AttemptOutcomeSourceEventIds(
                report=None,
                verdict=None,
                decision=None,
                effect="effect-1",
                worker_usage=None,
                review_usage=None,
            ),
            usage_completeness=AttemptOutcomeUsageCompleteness(
                worker=AttemptOutcomeCompleteness.MISSING,
                review=AttemptOutcomeCompleteness.MISSING,
            ),
        )

    def _v2_payload(self) -> AttemptOutcomePayload:
        return AttemptOutcomePayload(
            version=2,
            attempt_id="attempt-3",
            attempt=3,
            report_generation=4,
            sprint_ref="sprint:2",
            specification_revision="spec-1",
            terminal_state=AttemptOutcomeTerminalState.DONE,
            verdict=AttemptOutcomeVerdict.GREEN,
            disposition="release",
            blocked_reason=None,
            source_event_ids=AttemptOutcomeSourceEventIds(
                report="report-1",
                verdict="verdict-1",
                decision=None,
                effect="effect-2",
                worker_usage="usage-worker",
                review_usage="usage-review",
            ),
            usage_completeness=AttemptOutcomeUsageCompleteness(
                worker=AttemptOutcomeCompleteness.COLLECTED,
                review=AttemptOutcomeCompleteness.DEGRADED,
            ),
            lineage_required=AttemptOutcomeLineageRequired(
                specification_revision=True,
                report=True,
                verdict=True,
                decision=True,
                effect=True,
                worker_usage=True,
                review_usage=True,
            ),
        )

    def test_v1_round_trip_preserves_the_released_dictionary_exactly(self) -> None:
        payload = self._v1_payload()
        data = payload.to_data()

        self.assertEqual(AttemptOutcomePayload.from_data(data), payload)
        self.assertEqual(AttemptOutcomePayload.from_data(data).to_data(), data)
        self.assertNotIn("lineage_required", data)
        self.assertEqual(payload.natural_key("card-1"), ("card-1", "attempt-2", 3))

    def test_v2_round_trip_keeps_typed_lineage_and_reports_missing_required_evidence(self) -> None:
        payload = self._v2_payload()
        data = payload.to_data()

        self.assertEqual(AttemptOutcomePayload.from_data(data), payload)
        self.assertEqual(data["lineage_required"]["decision"], True)
        self.assertEqual(payload.lineage_missing(), ("decision",))

    def test_usage_completeness_requires_its_usage_event_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, "worker completeness requires its usage event ref"):
            AttemptOutcomePayload(
                version=1,
                attempt_id="attempt-1",
                attempt=1,
                report_generation=1,
                sprint_ref=None,
                specification_revision=None,
                terminal_state=AttemptOutcomeTerminalState.DONE,
                verdict=AttemptOutcomeVerdict.GREEN,
                disposition="release",
                blocked_reason=None,
                source_event_ids=AttemptOutcomeSourceEventIds(
                    report=None,
                    verdict=None,
                    decision=None,
                    effect="effect-1",
                    worker_usage=None,
                    review_usage=None,
                ),
                usage_completeness=AttemptOutcomeUsageCompleteness(
                    worker=AttemptOutcomeCompleteness.COLLECTED,
                    review=AttemptOutcomeCompleteness.MISSING,
                ),
            )

    def test_occurrence_exposes_the_same_typed_payload_from_legacy_event_data(self) -> None:
        payload = self._v1_payload()
        event = Event(
            event_id="event-outcome",
            kind=EventKind.ATTEMPT_OUTCOME,
            entity_kind=EntityKind.CARD,
            ref="card-1",
            actor=Actor(Role.DISPATCHER, "dispatcher"),
            reason="confirmed terminal lifecycle effect",
            occurred_at=datetime(2026, 9, 17, tzinfo=UTC),
            data=payload.to_data(),
        )
        occurrence = AttemptOutcomeOccurrence("request-1", event, False)

        self.assertEqual(occurrence.payload, payload)

    def test_unknown_fields_fail_closed(self) -> None:
        data = self._v2_payload().to_data()
        data["future"] = "value"

        with self.assertRaisesRegex(ValueError, "unsupported field set"):
            AttemptOutcomePayload.from_data(data)


if __name__ == "__main__":
    unittest.main()
