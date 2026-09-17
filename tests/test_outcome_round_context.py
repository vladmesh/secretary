from __future__ import annotations

import unittest

from secretary.board.outcome_round_context import OutcomeRoundContext, OutcomeRoundPhase


class OutcomeRoundContextTests(unittest.TestCase):
    def test_v1_decision_round_trips_exact_wire_shape(self) -> None:
        data = {
            "version": 1,
            "phase": "decision",
            "attempt_id": "attempt-1",
            "attempt": 2,
            "report_generation": 3,
            "request_ids": ["decision-request"],
            "assessment_visit": "visit-1",
            "source_event_id": "evt-decision",
        }

        context = OutcomeRoundContext.from_data(data)

        self.assertIs(context.phase, OutcomeRoundPhase.DECISION)
        self.assertEqual(context.request_ids, ("decision-request",))
        self.assertEqual(context.to_data(), data)

    def test_v2_worker_round_trips_exact_wire_shape(self) -> None:
        data = {
            "version": 2,
            "phase": "worker",
            "round_id": "round-1",
            "attempt_id": "attempt-1",
            "attempt": 1,
            "report_generation": 1,
            "request_ids": ["report-red", "report-done"],
            "assessment_visit": "",
            "source_event_id": "",
            "specification_revision": "evt-spec",
            "marker": "",
        }

        context = OutcomeRoundContext.from_data(data)

        self.assertIs(context.phase, OutcomeRoundPhase.WORKER)
        self.assertEqual(context.round_id, "round-1")
        self.assertEqual(context.specification_revision, "evt-spec")
        self.assertEqual(context.to_data(), data)

    def test_v2_source_round_requires_marker_and_source_event(self) -> None:
        data = {
            "version": 2,
            "phase": "report",
            "round_id": "round-1",
            "attempt_id": "attempt-1",
            "attempt": 1,
            "report_generation": 1,
            "request_ids": ["evt-report"],
            "assessment_visit": "",
            "source_event_id": "evt-report",
            "specification_revision": "evt-spec",
            "marker": "report:done",
        }

        context = OutcomeRoundContext.from_data(data)
        self.assertEqual(context.marker, "report:done")
        self.assertEqual(context.source_event_id, "evt-report")

        missing_marker = dict(data, marker="")
        with self.assertRaisesRegex(ValueError, "source outcome round context needs its marker"):
            OutcomeRoundContext.from_data(missing_marker)

        missing_source = dict(data, source_event_id="")
        with self.assertRaisesRegex(ValueError, "source outcome round context needs its source event id"):
            OutcomeRoundContext.from_data(missing_source)

    def test_v2_verdict_source_round_trips_exact_wire_shape(self) -> None:
        data = {
            "version": 2,
            "phase": "verdict",
            "round_id": "round-2",
            "attempt_id": "attempt-2",
            "attempt": 2,
            "report_generation": 4,
            "request_ids": ["review-green"],
            "assessment_visit": "",
            "source_event_id": "evt-verdict",
            "specification_revision": "evt-spec-2",
            "marker": "review:green",
        }

        context = OutcomeRoundContext.from_data(data)

        self.assertIs(context.phase, OutcomeRoundPhase.VERDICT)
        self.assertEqual(context.source_event_id, "evt-verdict")
        self.assertEqual(context.to_data(), data)

    def test_closed_schema_rejects_extra_fields(self) -> None:
        data = {
            "version": 1,
            "phase": "worker",
            "attempt_id": "attempt-1",
            "attempt": 1,
            "report_generation": 1,
            "request_ids": ["report-done"],
            "assessment_visit": "",
            "source_event_id": "",
            "extra": True,
        }

        with self.assertRaisesRegex(ValueError, "unsupported field set"):
            OutcomeRoundContext.from_data(data)

    def test_request_ids_are_nonempty_unique_strings(self) -> None:
        base = {
            "version": 1,
            "phase": "worker",
            "attempt_id": "attempt-1",
            "attempt": 1,
            "report_generation": 1,
            "request_ids": ["same", "same"],
            "assessment_visit": "",
            "source_event_id": "",
        }

        with self.assertRaisesRegex(ValueError, "needs unique request ids"):
            OutcomeRoundContext.from_data(base)

    def test_decision_visit_is_only_valid_for_decision(self) -> None:
        worker = OutcomeRoundContext(
            version=2,
            phase=OutcomeRoundPhase.WORKER,
            round_id="round-1",
            attempt_id="attempt-1",
            attempt=1,
            report_generation=1,
            request_ids=("report-done",),
            assessment_visit="",
            source_event_id="",
        )
        self.assertEqual(worker.assessment_visit, "")

        with self.assertRaisesRegex(ValueError, "only decision outcome round context"):
            OutcomeRoundContext(
                version=2,
                phase=OutcomeRoundPhase.WORKER,
                round_id="round-1",
                attempt_id="attempt-1",
                attempt=1,
                report_generation=1,
                request_ids=("report-done",),
                assessment_visit="visit-1",
                source_event_id="",
            )


if __name__ == "__main__":
    unittest.main()
