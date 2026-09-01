"""Sealed, observational attempt outcome occurrence contracts."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from unittest import mock

from secretary.board.events import AnalyticsOutcomeConflict, BoardEventCanon
from secretary.board.models import Actor, EntityKind, Event, EventKind
from secretary.dispatcher_gate import GateResult
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

    def _start_worker_round(self):
        self.start_dispatcher()
        self.tick()
        payload = self.runtime.production_state.load()
        return payload, self.runtime.production_state.records(payload)[CARD_REF]

    def test_worker_report_blocked_seals_a_worker_only_key(self) -> None:
        self._start_worker_round()
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="blocked",
            classification="external_fact",
            body="worker cannot proceed",
            request_id=self._worker_report_request_id("blocked", "external_fact"),
        )

        blocked = self.tick()

        self.assertEqual(blocked["to"], "blocked")
        occurrence = self._outcomes()[0].event.data
        self.assertEqual((occurrence["verdict"], occurrence["disposition"]), ("blocked", "blocked"))
        self.assertEqual(occurrence["blocked_reason"], "other")
        self.assertEqual(occurrence["usage_completeness"]["review"], "missing")

    def test_worker_wrong_task_definition_maps_forward_to_task_contract(self) -> None:
        self._start_worker_round()
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="blocked",
            classification="wrong_task_definition",
            body="the contract contradicts itself",
            request_id=self._worker_report_request_id("blocked", "wrong_task_definition"),
        )

        self.tick()

        occurrence = self._outcomes()[0].event.data
        self.assertEqual(occurrence["blocked_reason"], "task_contract")
        effect = next(
            event
            for event in self.writer.board_host.canon.events(ref=CARD_REF)
            if event.kind.value == "card.blocked"
        )
        self.assertEqual(
            effect.data["terminal_taxonomy"],
            {
                "version": 2,
                "disposition": "blocked",
                "blocked_reason": "task_contract",
                "source_evidence": "wrong_task_definition",
                "budget_class": "blocked",
                "provenance": "forward",
            },
        )

    def test_malformed_taxonomy_does_not_gate_the_lifecycle_effect(self) -> None:
        _payload, record = self._start_worker_round()

        self.runtime.terminal_effect(
            self.reader.show(CARD_REF),
            record,
            target="blocked",
            reason="a lifecycle effect with malformed analytics input",
            request_id="malformed-taxonomy-effect",
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason="not-a-taxonomy-reason",
        )

        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.assertEqual(self._outcomes(), ())

    def test_done_then_red_gate_seals_rework_before_opening_the_next_round(self) -> None:
        self.start_dispatcher()
        self.host.gate_results = [GateResult("red", "test gate rejected the candidate")]
        self._run_worker_to_validate()

        reworked = self.tick()

        self.assertEqual(reworked["action"], "gate-red-rework")
        occurrence = self._outcomes()[0].event.data
        self.assertEqual(
            (occurrence["terminal_state"], occurrence["verdict"], occurrence["disposition"]),
            ("in_progress", "red", "rework"),
        )

    def test_no_observer_green_release_seals_the_reviewed_round(self) -> None:
        self.start_dispatcher()
        self.board.metadata[12].pop("sprint_ref", None)
        self.sprints.rows.clear()
        self.board.sprints.clear()
        self._run_worker_to_validate()
        self.tick()  # gate green -> review started
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="green",
            body="green",
            request_id="outcome-no-observer-green",
        )

        released = self.tick()

        self.assertEqual(released["to"], "done")
        occurrence = self._outcomes()[0].event.data
        self.assertEqual((occurrence["verdict"], occurrence["disposition"]), ("green", "release"))

    def test_infrastructure_bringup_block_seals_the_claimed_round(self) -> None:
        self.start_dispatcher()
        self.host.fail_prepare_reason = "workspace is unavailable"

        blocked = self.tick()

        self.assertEqual(blocked["status"], "blocked")
        occurrence = self._outcomes()[0].event.data
        self.assertEqual(
            (occurrence["disposition"], occurrence["blocked_reason"]), ("blocked", "infrastructure")
        )

    def test_fanout_refusal_commits_its_lifecycle_effect_and_outcome(self) -> None:
        self.start_dispatcher()
        with mock.patch(
            "secretary.dispatcher._write_launch_intent",
            return_value="codex-fanout-policy: prohibited source",
        ):
            blocked = self.tick()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        occurrence = self._outcomes()[0].event.data
        self.assertEqual(occurrence["blocked_reason"], "provider")

    def test_committed_obligations_retire_before_the_next_tick(self) -> None:
        _payload, record = self._start_worker_round()
        effect = self.runtime.terminal_effect(
            self.reader.show(CARD_REF),
            record,
            target="blocked",
            reason="terminal for recovery retirement",
            request_id="outcome-retirement-effect",
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason="infrastructure",
        )
        canon = self.writer.board_host.canon
        self.assertEqual(canon.attempt_outcome_effects(), ())

        with mock.patch.object(self.runtime, "_finish_attempt_outcome") as finish:
            self.assertEqual(self.runtime.publish_pending_attempt_outcomes(), [])
            self.assertEqual(self.runtime.publish_pending_attempt_outcomes(), [])
        finish.assert_not_called()
        self.assertEqual(self._outcomes()[0].event.data["source_event_ids"]["effect"], effect["event_id"])

    def test_crash_after_stage_reuses_the_pending_occurrence(self) -> None:
        _payload, record = self._start_worker_round()
        with mock.patch.object(
            self.writer,
            "_commit_attempt_outcome",
            side_effect=TaskError("audit_pending", "append interrupted", 4),
        ):
            effect = self.runtime.terminal_effect(
                self.reader.show(CARD_REF),
                record,
                target="blocked",
                reason="terminal before append",
                request_id="outcome-stage-crash",
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="infrastructure",
            )

        self.assertTrue(self._outcomes()[0].pending)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.runtime.publish_pending_attempt_outcomes()
        occurrence = self._outcomes()[0]
        self.assertFalse(occurrence.pending)
        self.assertEqual(occurrence.event.data["source_event_ids"]["effect"], effect["event_id"])

    def test_operator_move_without_a_round_identity_creates_no_outcome(self) -> None:
        self.writer.move(
            role="po",
            actor="operator",
            reference=CARD_REF,
            target="blocked",
            reason="operator stop before claim",
            request_id="operator-stop-no-round",
        )

        self.assertEqual(self._outcomes(), ())

    def test_unresumable_lost_claim_commits_blocked_without_inventing_a_key(self) -> None:
        payload, record = self._start_worker_round()

        blocked = self.runtime._block_unresumable(
            self.reader.show(CARD_REF),
            {},
            payload,
            record.attempt_id,
            "advance",
            RuntimeError("head vanished"),
        )

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.assertEqual(self._outcomes(), ())

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
