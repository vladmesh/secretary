"""SQL dispatcher provider-block contracts; run only in the integration shard."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from ummanu.dispatch.launch import WORKER_ROLE, REVIEW_ROLE
from ummanu.dispatch.state import DispatcherRecord
from ummanu.runtime.head import TaskRef
from tests.dispatcher_fixtures import DispatcherRuntimeFixture, CARD_REF
from tests.fanout_fixtures import accepted_transport_run

class ProviderBlockThroughDispatcherRuntimeTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The owner's ``block`` callback, driven through the real runtime wiring, blocks the card."""

    def _bound_block(self, role: str) -> tuple[DispatcherRecord, object]:
        self.start_dispatcher()
        self.tick()
        payload = self.runtime.production_state.load()
        records = self.runtime.production_state.records(payload)
        record = records[CARD_REF]
        run = accepted_transport_run(
            "codex-contract",
            role=role,
            workspace=str(self.data_dir / "workspace"),
            task_ref=TaskRef.card(CARD_REF),
            pid_file=str(self.data_dir / f"{role}.pid"),
            run_id=f"run-{role}",
        )
        run = run.with_fanout_policy(
            {**run.fanout_policy, "provider_source": {"version": 1, "kind": "codex_session_event_jsonl"}}
        )
        if role == WORKER_ROLE:
            record.worker_head_run = run.to_json()
        else:
            record.review_head_run = run.to_json()
        installed: dict[str, object] = {}

        def configure(run, *, persist, stop, block) -> None:
            installed[run.run_id] = block

        with mock.patch.object(self.host, "configure_codex_provider_ingress", side_effect=configure):
            self.runtime.bind_codex_provider_ingress(record, records, payload, role=role, reference=CARD_REF)
        return record, installed[f"run-{role}"]

    def _assert_blocked_once(self, record: DispatcherRecord, block, role: str) -> None:
        request_id = (
            f"dispatcher-{record.attempt_id}-codex-provider-event-blocked-{CARD_REF}-{role}-run-{role}"
        )

        evidence = {"state": "prohibited", "reason": "child agent spawn observed"}
        block(dict(evidence))
        block(dict(evidence))

        card = self.reader.show(CARD_REF)
        self.assertEqual(card["state"], "blocked")
        moves = [
            event
            for event in self.writer.audit.events(reference=CARD_REF)
            if event.get("request_id") == request_id
        ]
        self.assertEqual(len(moves), 1, "a replayed block commits once under its stable request id")
        self.assertIn(
            "Codex provider fan-out policy blocked this head: prohibited; child agent spawn observed",
            json.dumps(moves[0]),
        )
        occurrences = self.writer.board_host.canon.attempt_outcome_occurrences(ref=CARD_REF)
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].event.data["disposition"], "blocked")
        self.assertEqual(occurrences[0].event.data["blocked_reason"], "provider")

    def test_worker_provider_block_blocks_the_card_under_a_stable_request_id(self) -> None:
        record, block = self._bound_block(WORKER_ROLE)
        self._assert_blocked_once(record, block, "worker")

    def test_review_provider_block_blocks_the_card_under_a_stable_request_id(self) -> None:
        record, block = self._bound_block(REVIEW_ROLE)
        self._assert_blocked_once(record, block, "review")
