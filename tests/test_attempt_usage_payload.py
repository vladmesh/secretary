"""Typed payload coverage for the closed ``attempt.usage`` board-event schema."""

from __future__ import annotations

import unittest

from secretary.board.attempt_usage import AttemptUsagePayload, AttemptUsagePhase, TokenAccount
from secretary.board.events import AttemptUsageOccurrence
from secretary.board.models import Actor, AttemptUsageOutcome, EntityKind, Event, EventKind
from secretary.board.roles import Role
from datetime import UTC, datetime


class AttemptUsagePayloadTests(unittest.TestCase):
    def _payload(self) -> AttemptUsagePayload:
        return AttemptUsagePayload(
            attempt=2,
            attempt_id="attempt-2",
            phase=AttemptUsagePhase.REVIEW,
            role=Role.REVIEWER,
            report_generation=3,
            head="codex-reviewer",
            adapter="codex",
            model="gpt-5.6",
            model_source="profile",
            session_id="session-1",
            session_id_reason="",
            launch_id="launch-1",
            outcome=AttemptUsageOutcome.COLLECTED,
            detail="",
            source_kind="codex_session_event_jsonl",
            records=4,
            skipped_records=1,
            tokens=TokenAccount(input=30, output=7, reasoning=2),
            session_totals=TokenAccount(input=130, output=27, reasoning=8),
            phase_baseline=TokenAccount(input=100, output=20, reasoning=6),
        )

    def test_round_trip_preserves_the_released_dictionary_exactly(self) -> None:
        payload = self._payload()
        data = payload.to_data()

        self.assertEqual(AttemptUsagePayload.from_data(data), payload)
        self.assertEqual(AttemptUsagePayload.from_data(data).to_data(), data)
        self.assertEqual(data["phase"], "review")
        self.assertEqual(data["role"], "reviewer")
        self.assertEqual(data["tokens"]["input"], 30)
        self.assertIsNone(data["tokens"]["cache_input"])

    def test_degraded_payload_keeps_all_three_accounts_empty(self) -> None:
        empty = TokenAccount()
        payload = AttemptUsagePayload(
            attempt=1,
            attempt_id="attempt-1",
            phase=AttemptUsagePhase.WORKER,
            role=Role.WORKER,
            report_generation=1,
            head="",
            adapter="claude",
            model="",
            model_source="cli_default",
            session_id=None,
            session_id_reason="provider did not bind a session",
            launch_id="",
            outcome=AttemptUsageOutcome.SESSION_UNAVAILABLE,
            detail="no session",
            source_kind="",
            records=0,
            skipped_records=0,
            tokens=empty,
            session_totals=empty,
            phase_baseline=empty,
        )

        self.assertEqual(AttemptUsagePayload.from_data(payload.to_data()), payload)

    def test_collected_interval_must_match_total_minus_baseline(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match its session-total interval"):
            AttemptUsagePayload(
                attempt=1,
                attempt_id="attempt-1",
                phase=AttemptUsagePhase.WORKER,
                role=Role.WORKER,
                report_generation=1,
                head="codex",
                adapter="codex",
                model="gpt-5.6",
                model_source="profile",
                session_id="session-1",
                session_id_reason="",
                launch_id="launch-1",
                outcome=AttemptUsageOutcome.COLLECTED,
                detail="",
                source_kind="codex_session_event_jsonl",
                records=1,
                skipped_records=0,
                tokens=TokenAccount(input=11),
                session_totals=TokenAccount(input=20),
                phase_baseline=TokenAccount(input=10),
            )

    def test_occurrence_exposes_the_same_typed_payload_from_legacy_event_data(self) -> None:
        payload = self._payload()
        event = Event(
            event_id="event-usage",
            kind=EventKind.ATTEMPT_USAGE,
            entity_kind=EntityKind.CARD,
            ref="card-1",
            actor=Actor(Role.REVIEWER, "reviewer-1"),
            reason=payload.reason(),
            occurred_at=datetime(2026, 9, 17, tzinfo=UTC),
            data=payload.to_data(),
        )
        occurrence = AttemptUsageOccurrence("request-1", event, False)

        self.assertEqual(occurrence.payload, payload)

    def test_unknown_fields_fail_closed(self) -> None:
        data = self._payload().to_data()
        data["future"] = "value"

        with self.assertRaisesRegex(ValueError, "unsupported field set"):
            AttemptUsagePayload.from_data(data)


if __name__ == "__main__":
    unittest.main()
