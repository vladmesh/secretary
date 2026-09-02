"""Hermetic contracts for the sealed offline analytics projection."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from secretary.board.analytics import AnalyticsProjectionError, project_analytics_checkpoint
from secretary.board.models import TOKEN_DIMENSIONS, Actor, EntityKind, Event, EventKind
from secretary.checkpoint import _write_analytics_manifest

CARD = "secretary-1535"
SPRINT = "sprint:1419"


def usage(*, event_id: str, attempt: int, attempt_id: str, role: str) -> Event:
    phase = "worker" if role == "worker" else "review"
    return Event(
        event_id=event_id,
        kind=EventKind.ATTEMPT_USAGE,
        entity_kind=EntityKind.CARD,
        ref=CARD,
        actor=Actor("dispatcher", "dispatcher"),
        reason="recorded provider phase usage",
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        data={
            "attempt": attempt,
            "attempt_id": attempt_id,
            "phase": phase,
            "role": role,
            "report_generation": 1,
            "head": "head",
            "adapter": "codex",
            "model": "model",
            "model_source": "profile",
            "session_id": "session",
            "session_id_reason": "",
            "launch_id": "launch",
            "outcome": "collected",
            "detail": "",
            "source_kind": "test",
            "records": 1,
            "skipped_records": 0,
            "tokens": dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 10, "output": 3},
            "session_totals": dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 10, "output": 3},
            "phase_baseline": dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 0, "output": 0},
        },
    )


def effect(*, event_id: str) -> Event:
    return Event(
        event_id=event_id,
        kind=EventKind.CARD_MOVED,
        entity_kind=EntityKind.CARD,
        ref=CARD,
        actor=Actor("dispatcher", "dispatcher"),
        reason="terminal lifecycle effect",
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        source_state="validate",
        target_state="done",
    )


def outcome(
    *,
    event_id: str,
    attempt: int,
    attempt_id: str,
    worker_usage: str | None,
    review_usage: str | None,
    worker_completeness: str = "collected",
    review_completeness: str = "collected",
    sprint_ref: str | None = SPRINT,
    verdict: str = "green",
) -> Event:
    return Event(
        event_id=event_id,
        kind=EventKind.ATTEMPT_OUTCOME,
        entity_kind=EntityKind.CARD,
        ref=CARD,
        actor=Actor("dispatcher", "dispatcher"),
        reason="confirmed terminal lifecycle effect",
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        data={
            "version": 1,
            "attempt_id": attempt_id,
            "attempt": attempt,
            "report_generation": 1,
            "sprint_ref": sprint_ref,
            "specification_revision": None,
            "terminal_state": "done",
            "verdict": verdict,
            "disposition": "release",
            "blocked_reason": None,
            "source_event_ids": {
                "report": None,
                "verdict": None,
                "decision": None,
                "effect": "effect",
                "worker_usage": worker_usage,
                "review_usage": review_usage,
            },
            "usage_completeness": {
                "worker": worker_completeness,
                "review": review_completeness,
            },
        },
    )


class AnalyticsProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.board = Path(self.tmpdir.name) / "copied-board"
        self.board.mkdir()

    def seal(
        self, events: list[dict], *, cards: list[dict] | None = None, sprints: list[dict] | None = None
    ) -> None:
        cards = cards if cards is not None else [{"reference": CARD, "comments": ["ignored"]}]
        sprints = sprints if sprints is not None else [{"reference": SPRINT}]
        for name, values in (("cards.ndjson", cards), ("sprints.ndjson", sprints), ("events.ndjson", events)):
            (self.board / name).write_text(
                "".join(json.dumps(value) + "\n" for value in values), encoding="utf-8"
            )
        (self.board / "export.json").write_text(
            json.dumps({"version": 1, "card_count": len(cards), "sprint_count": len(sprints)}) + "\n",
            encoding="utf-8",
        )
        _write_analytics_manifest(self.board)

    @staticmethod
    def record(event: Event, request: str) -> dict:
        return event.to_record(request)

    def valid_records(self) -> list[dict]:
        events: list[Event] = [effect(event_id="effect")]
        for attempt, attempt_id in ((1, "round-1"), (2, "round-2")):
            worker = usage(
                event_id=f"worker-{attempt}", attempt=attempt, attempt_id=attempt_id, role="worker"
            )
            reviewer = usage(
                event_id=f"review-{attempt}", attempt=attempt, attempt_id=attempt_id, role="reviewer"
            )
            events.extend(
                [
                    worker,
                    reviewer,
                    outcome(
                        event_id=f"outcome-{attempt}",
                        attempt=attempt,
                        attempt_id=attempt_id,
                        worker_usage=worker.event_id,
                        review_usage=reviewer.event_id,
                    ),
                ]
            )
        return [self.record(event, f"request-{number}") for number, event in enumerate(events, start=1)]

    def test_projects_a_typed_multi_round_cut_through_explicit_usage_ids(self) -> None:
        self.seal(self.valid_records())

        projection = project_analytics_checkpoint(self.board)

        self.assertFalse(projection.incomplete)
        self.assertEqual(len(projection.rows), 2)
        self.assertEqual(projection.rows[1]["attempt_id"], "round-2")
        self.assertEqual(projection.rows[1]["worker_usage"]["event_id"], "worker-2")
        self.assertEqual(projection.rows[1]["review_usage"]["phase"], "review")
        self.assertEqual(len(projection.ndjson().splitlines()), 2)

    def test_row_order_and_usage_joins_do_not_depend_on_append_order(self) -> None:
        records = self.valid_records()
        self.seal(list(reversed(records)))

        projection = project_analytics_checkpoint(self.board)

        self.assertEqual([row["attempt_id"] for row in projection.rows], ["round-1", "round-2"])
        self.assertEqual(projection.rows[0]["worker_usage"]["event_id"], "worker-1")

    def test_exact_duplicate_replay_is_one_row(self) -> None:
        records = self.valid_records()
        self.seal(records + [dict(records[-1])])

        projection = project_analytics_checkpoint(self.board)

        self.assertEqual(len(projection.rows), 2)

    def test_conflicting_natural_key_event_identity_and_request_owner_fail_closed(self) -> None:
        cases: dict[str, list[dict]] = {}
        natural = self.valid_records()
        conflict = outcome(
            event_id="outcome-conflict",
            attempt=1,
            attempt_id="round-1",
            worker_usage="worker-1",
            review_usage="review-1",
        )
        cases["analytics_conflicting_outcome_natural_key"] = natural + [
            self.record(conflict, "other-request")
        ]

        identity = self.valid_records()
        changed = dict(identity[0]) | {"request_id": "another-request"}
        cases["analytics_conflicting_event_identity"] = identity + [changed]

        request = self.valid_records()
        other = effect(event_id="another-effect")
        cases["analytics_conflicting_request_ownership"] = request + [self.record(other, "request-1")]

        for code, records in cases.items():
            with self.subTest(code=code):
                self.seal(records)
                with self.assertRaisesRegex(AnalyticsProjectionError, code):
                    project_analytics_checkpoint(self.board)

    def test_dangling_and_incompatible_refs_fail_closed(self) -> None:
        records = self.valid_records()
        records[-1] = dict(records[-1])
        records[-1]["data"] = dict(records[-1]["data"])
        records[-1]["data"]["source_event_ids"] = dict(records[-1]["data"]["source_event_ids"])
        records[-1]["data"]["source_event_ids"]["worker_usage"] = "missing-usage"
        self.seal(records)
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_dangling_source_event_ref"):
            project_analytics_checkpoint(self.board)

        records = self.valid_records()
        records[-1] = dict(records[-1])
        records[-1]["data"] = dict(records[-1]["data"]) | {"sprint_ref": "sprint:missing"}
        self.seal(records)
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_dangling_sprint_ref"):
            project_analytics_checkpoint(self.board)

        records = self.valid_records()
        records[-1] = dict(records[-1])
        records[-1]["data"] = dict(records[-1]["data"])
        records[-1]["data"]["source_event_ids"] = dict(records[-1]["data"]["source_event_ids"])
        records[-1]["data"]["source_event_ids"]["review_usage"] = "worker-2"
        self.seal(records)
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_incompatible_usage_join"):
            project_analytics_checkpoint(self.board)

    def test_malformed_typed_schema_and_unknown_enum_fail_closed(self) -> None:
        records = self.valid_records()
        records[0] = dict(records[0]) | {"schema_version": 999}
        self.seal(records)
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_invalid_typed_event"):
            project_analytics_checkpoint(self.board)

        self.seal(self.valid_records() + [["not an NDJSON object"]])
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_malformed_row"):
            project_analytics_checkpoint(self.board)

        records = self.valid_records()
        records[0] = dict(records[0]) | {"kind": "card.unknown"}
        self.seal(records)
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_invalid_typed_event"):
            project_analytics_checkpoint(self.board)

    def test_legacy_and_no_outcome_evidence_remains_explicitly_incomplete(self) -> None:
        legacy = outcome(
            event_id="legacy-outcome",
            attempt=1,
            attempt_id="legacy-round",
            worker_usage=None,
            review_usage=None,
            worker_completeness="legacy",
            review_completeness="legacy",
            verdict="legacy",
        )
        self.seal(
            [self.record(effect(event_id="effect"), "effect-request"), self.record(legacy, "legacy-request")]
        )

        projection = project_analytics_checkpoint(self.board)

        self.assertTrue(projection.incomplete)
        self.assertIn("legacy_outcome", projection.incomplete_reasons)
        self.assertIsNone(projection.rows[0]["worker_usage"])
        self.assertEqual(projection.rows[0]["usage_completeness"]["worker"], "legacy")

        records = self.valid_records()
        records[1] = dict(records[1])
        records[1]["data"] = dict(records[1]["data"]) | {
            "outcome": "source_unreadable",
            "tokens": dict.fromkeys(TOKEN_DIMENSIONS),
            "session_totals": dict.fromkeys(TOKEN_DIMENSIONS),
            "phase_baseline": dict.fromkeys(TOKEN_DIMENSIONS),
        }
        records[3] = dict(records[3])
        records[3]["data"] = dict(records[3]["data"])
        records[3]["data"]["usage_completeness"] = {
            "worker": "degraded",
            "review": "collected",
        }
        self.seal(records)
        projection = project_analytics_checkpoint(self.board)
        self.assertEqual(projection.rows[0]["usage_completeness"]["worker"], "degraded")
        self.assertIsNone(projection.rows[0]["worker_usage"]["tokens"]["input"])

        self.seal([])
        projection = project_analytics_checkpoint(self.board)
        self.assertTrue(projection.incomplete)
        self.assertEqual(projection.incomplete_reasons, ("no_attempt_outcome_v1",))

    def test_projection_has_no_live_board_provider_or_comment_dependency(self) -> None:
        self.seal(self.valid_records())
        with (
            mock.patch("secretary.tasks.TaskReader", side_effect=AssertionError("live board read")),
            mock.patch(
                "secretary.dispatch.attempt_usage.collect_usage", side_effect=AssertionError("provider read")
            ),
        ):
            projection = project_analytics_checkpoint(self.board)

        self.assertEqual(len(projection.rows), 2)
