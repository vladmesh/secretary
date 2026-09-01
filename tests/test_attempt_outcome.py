"""Sealed, observational attempt outcome occurrence contracts."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime

from secretary.board.events import AnalyticsOutcomeConflict, BoardEventCanon
from secretary.board.models import Actor, EntityKind, Event, EventKind


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
