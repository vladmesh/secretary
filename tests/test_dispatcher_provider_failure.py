"""secretary-1799: a head whose first turn ended on a provider error falls back, it does not stall.

These drive one card through the real dispatcher tick with the fake host. The provider error each
test feeds in is parsed by the real reader (`runtime.provider_errors`) from the record shapes the
providers write; which run it belongs to is the fake host's only scripted fact.
"""

from __future__ import annotations

import json
import time
import unittest
from typing import Any
from unittest import mock

from secretary.dispatch.production import _budget_event_type
from secretary.dispatch.provider_failure import PROVIDER_UNAVAILABLE_READY_ACTION
from secretary.dispatch.state import DispatcherRecord, attempt_request_id
from secretary.dispatch.types import STOPPED_BY_PROVIDER_FAILURE
from secretary.head_health import resource_health_path
from secretary.runtime.provider_errors import (
    ProviderError,
    claude_first_turn_failure,
    codex_first_turn_failure,
)
from tests.dispatcher_fixtures import DispatcherRuntimeFixture

REF = "secretary-510"

# The 2026-09-25 reviewer's rollout tail, as Codex wrote it (codegen-orchestrator-1373): the turn
# opened at 22:58:11 and closed at 22:58:41 on the backend's 401. The key Codex echoed back is
# masked the way Codex itself masks it.
CODEX_401_ERROR = (
    "unexpected status 401 Unauthorized: Incorrect API key provided: sk-svcac"
    + "*" * 40
    + "fvMA. You can find your API key at https://platform.openai.com/account/api-keys., "
    "url: https://chatgpt.com/backend-api/codex/responses, cf-ray: a40da28f7896ec3e-VNO, "
    "request id: 831b9421-ddf3-4abf-bf14-f013e47cd11e"
)


def codex_401_turn(started_at: float) -> list[dict[str, Any]]:
    return [
        {"type": "session_meta", "payload": {"session_id": "s-1", "cwd": "/w"}},
        {
            "timestamp": _iso(started_at),
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "t-1"},
        },
        {"type": "response_item", "payload": {"type": "message", "role": "user"}},
        {
            "timestamp": _iso(started_at + 35),
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "t-1",
                "last_agent_message": None,
                "error": {"message": CODEX_401_ERROR, "codex_error_info": "other"},
            },
        },
    ]


def claude_auth_turn(started_at: float) -> list[dict[str, Any]]:
    return [
        {
            "type": "user",
            "timestamp": _iso(started_at),
            "message": {"role": "user", "content": "read TASK.md"},
        },
        {
            "type": "assistant",
            "timestamp": _iso(started_at + 4),
            "isApiErrorMessage": True,
            "apiErrorStatus": 401,
            "error": "authentication_failed",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": "API Error: 401 invalid token · Please run /login"}],
            },
        },
    ]


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + f".{int(epoch % 1 * 1000):03d}Z"


class ProviderFailureFallbackTests(DispatcherRuntimeFixture, unittest.TestCase):
    def _record(self) -> DispatcherRecord:
        return self.runtime.production_state.records(self.runtime.production_state.load())[REF]

    def _fail(self, kind: str, error: ProviderError) -> str:
        """Mark the role's current run as the one whose first turn ended on `error`."""
        record = self._record()
        run_id = str((record.review_head_run if kind == "review" else record.worker_head_run)["run_id"])
        self.host.__dict__.setdefault("failed_runs", {})[run_id] = error
        return run_id

    def _resource_health(self) -> dict[str, Any]:
        path = resource_health_path(self.data_dir)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _comments(self) -> list[str]:
        return [comment["body"] for comment in self.reader.show(REF).get("comments") or []]

    def _budget_types(self) -> list[str]:
        events = self.writer.audit.events(REF)
        return [kind for kind in (_budget_event_type(event) for event in events) if kind]

    def _assert_no_stall_path(self, outcomes: list[dict[str, Any]]) -> None:
        actions = [str(outcome.get("action") or "") for outcome in outcomes]
        for forbidden in ("stall-suspected", "respawned", "escalate", "guard-refused"):
            self.assertFalse([a for a in actions if forbidden in a], (forbidden, actions))
        for body in self._comments():
            self.assertNotIn("respawned the", body)
            self.assertNotIn("Another stall escalates", body)

    # ---- AC1: the 2026-09-25 reviewer -------------------------------------------------------

    def test_codex_reviewer_401_falls_back_to_the_chain_head_on_the_next_tick(self) -> None:
        self.catalog.profiles["codex-reviewer"]["fallback"] = ["claude-opus"]
        self.start_dispatcher()
        self._run_worker_to_validate()
        started = self.tick()
        self.assertEqual(started["action"], "review-started")
        launched_at = self._record().review_started_at
        error = codex_first_turn_failure(codex_401_turn(launched_at))
        assert error is not None
        failed_run = self._fail("review", error)

        # The fake clock: the tick that observes the turn's end runs a minute after the error,
        # long before any stall threshold, and the relaunch happens on it.
        tick_at = error.at + 60
        with mock.patch("time.time", return_value=tick_at):
            fell_back = self.tick()

        self.assertEqual(fell_back["action"], "review-provider-fallback")
        self.assertEqual(fell_back["status"], "ok")
        self.assertEqual(fell_back["head"], "codex-reviewer")
        self.assertEqual(fell_back["resource"], "openai-sub")
        self.assertEqual(fell_back["switched_to"], "claude-opus")
        self.assertIn("401 Unauthorized: Incorrect API key provided", fell_back["error"])
        self.assertNotIn("sk-svcac", json.dumps(fell_back))
        self.assertNotIn("cf-ray", json.dumps(fell_back))
        record = self._record()
        self.assertEqual(record.review_head, "claude-opus")
        self.assertEqual(record.preferred_review_head, "codex-reviewer")
        self.assertNotEqual(record.review_head_run["run_id"], failed_run)
        self.assertLessEqual(record.review_started_at - error.at, 300)
        self.assertEqual(self.host.reviews, [REF, REF])
        self.assertEqual(self.host.review_stop_initiators, [STOPPED_BY_PROVIDER_FAILURE])
        # Nothing was charged: no respawn, no round, no budget.
        self.assertEqual(record.review_respawns, 0)
        self.assertEqual(record.report_generation, 1)
        self.assertEqual(self._budget_types(), [])
        health = self._resource_health()["openai-sub"]
        self.assertEqual(health["status"], "unavailable")
        self.assertIn("codex-reviewer", health["reason"])
        self.assertNotIn("sk-svcac", health["reason"])
        card = self.reader.show(REF)
        self.assertEqual(card["state"], "validate")
        comment = self._comments()[-1]
        for part in ("codex-reviewer", "openai-sub", "401 Unauthorized", "claude-opus"):
            self.assertIn(part, comment)
        self.assertNotIn("sk-svcac", comment)

        # The replacement is left to work: the next ticks wait on its verdict.
        waited = self.tick()
        self.assertEqual(waited["action"], "waiting-review-verdict")
        self._assert_no_stall_path([started, fell_back, waited])

    # ---- AC2: a worker's first turn ---------------------------------------------------------

    def test_codex_worker_401_is_relaunched_on_its_chain_head(self) -> None:
        self.catalog.profiles["codex"]["fallback"] = ["claude-opus"]
        self.start_dispatcher()
        claimed = self.tick()
        self.assertEqual(claimed["step"], "claim")
        record = self._record()
        self.assertEqual(record.head, "codex")
        error = codex_first_turn_failure(codex_401_turn(record.worker_started_at))
        assert error is not None
        self._fail("worker", error)

        fell_back = self.tick()

        self.assertEqual(fell_back["action"], "worker-provider-fallback")
        self.assertEqual(fell_back["switched_to"], "claude-opus")
        self.assertEqual(fell_back["resource"], "openai-sub")
        record = self._record()
        self.assertEqual(record.head, "claude-opus")
        self.assertEqual(record.preferred_head, "codex")
        self.assertEqual(record.worker_respawns, 0)
        self.assertEqual(record.report_generation, 1)
        self.assertIn("restart_worker", self.host.calls)
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self._resource_health()["openai-sub"]["status"], "unavailable")
        self.assertEqual(self._budget_types(), [])
        self.assertIn("relaunched on claude-opus", self._comments()[-1])
        self.assertEqual(self.tick()["action"], "waiting-worker-report")

    def test_codex_worker_401_with_an_empty_chain_goes_to_ready_and_is_claimed_again(self) -> None:
        self.start_dispatcher()
        self.tick()
        record = self._record()
        error = codex_first_turn_failure(codex_401_turn(record.worker_started_at))
        assert error is not None
        failed_run = self._fail("worker", error)

        returned = self.tick()

        self.assertEqual(returned["action"], "worker-provider-unavailable")
        self.assertEqual(returned["to"], "ready")
        self.assertEqual(returned["reason"], "provider unavailable: openai-sub")
        card = self.reader.show(REF)
        self.assertEqual(card["state"], "ready")
        self.assertNotEqual(card["state"], "blocked")
        # The move is visible with its reason, and the sprint budget does not charge it.
        moves = [
            event
            for event in self.writer.audit.events(REF)
            if PROVIDER_UNAVAILABLE_READY_ACTION in str(event.get("request_id") or "")
        ]
        self.assertEqual(len(moves), 1)
        self.assertIn("provider unavailable: openai-sub", json.dumps(moves[0]))
        self.assertEqual(self._budget_types(), [])

        # While the chain has nothing launchable the card waits in Ready ...
        skipped = self.tick()
        self.assertEqual(skipped["action"], "resource-not-ready")
        self.assertEqual(self.reader.show(REF)["state"], "ready")

        # ... and once the provider recovers it is claimed again, onto a fresh head.
        resource_health_path(self.data_dir).unlink()
        reclaimed = self.tick()
        self.assertEqual(reclaimed["step"], "claim")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertNotEqual(self._record().worker_head_run["run_id"], failed_run)

    def test_the_ready_return_is_no_budget_event_and_a_plain_preempt_still_is(self) -> None:
        def moved(request_id: str) -> dict[str, Any]:
            return {
                "kind": "moved",
                "request_id": request_id,
                "payload": {"from": "in_progress", "to": "ready"},
            }

        ours = attempt_request_id("attempt-1", PROVIDER_UNAVAILABLE_READY_ACTION, REF, "run-1")
        self.assertIsNone(_budget_event_type(moved(ours)))
        self.assertEqual(_budget_event_type(moved("po-preempt-1")), "preempt")

    # ---- AC3: a Claude head ----------------------------------------------------------------

    def test_claude_worker_auth_error_is_handled_the_same_way(self) -> None:
        self.catalog.profiles["claude-opus"]["fallback"] = ["codex"]
        self.start_dispatcher()
        self.board.save_metadata(12, head="claude-opus")
        self.tick()
        record = self._record()
        self.assertEqual(record.head, "claude-opus")
        error = claude_first_turn_failure(claude_auth_turn(record.worker_started_at))
        assert error is not None
        self.assertEqual((error.kind, error.status), ("auth", 401))
        self._fail("worker", error)

        fell_back = self.tick()

        self.assertEqual(fell_back["action"], "worker-provider-fallback")
        self.assertEqual(fell_back["resource"], "claude-sub")
        self.assertEqual(fell_back["switched_to"], "codex")
        self.assertEqual(self._record().head, "codex")
        self.assertEqual(self._resource_health()["claude-sub"]["status"], "unavailable")
        self.assertNotEqual(self.reader.show(REF)["state"], "blocked")

    def test_claude_reviewer_auth_error_falls_back_to_the_codex_chain_head(self) -> None:
        self.catalog.role_defaults["reviewer"] = "claude-default"
        self.catalog.profiles["claude-default"]["fallback"] = ["codex-reviewer"]
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        error = claude_first_turn_failure(claude_auth_turn(self._record().review_started_at))
        assert error is not None
        self._fail("review", error)

        fell_back = self.tick()

        self.assertEqual(fell_back["action"], "review-provider-fallback")
        self.assertEqual(fell_back["switched_to"], "codex-reviewer")
        self.assertEqual(self._record().review_head, "codex-reviewer")
        self.assertEqual(self._resource_health()["claude-sub"]["status"], "unavailable")

    # ---- AC5: the reviewer's empty chain ------------------------------------------------------

    def test_reviewer_with_an_empty_chain_waits_in_validate_and_launches_on_recovery(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        record = self._record()
        candidate = record.gate_attestation.to_json()
        error = codex_first_turn_failure(codex_401_turn(record.review_started_at))
        assert error is not None
        self._fail("review", error)

        held = self.tick()

        self.assertEqual(held["action"], "review-provider-unavailable")
        self.assertEqual(held["reason"], "provider unavailable: openai-sub")
        self.assertEqual(self.host.reviews, [REF])
        self.assertEqual(self.reader.show(REF)["state"], "validate")
        self.assertIn("stays in Validate with no reviewer", self._comments()[-1])
        record = self._record()
        self.assertTrue(record.review_provider_hold.startswith("provider unavailable: openai-sub"))
        self.assertEqual(record.gate_attestation.to_json(), candidate)

        # Held, not retried into Blocked: tick after tick nothing is launched and nothing counted.
        for _ in range(4):
            waiting = self.tick()
            self.assertEqual(waiting["action"], "review-provider-unavailable")
            self.assertEqual(waiting["status"], "degraded")
        record = self._record()
        self.assertEqual(record.review_infra_failures, 0)
        self.assertEqual(record.review_respawns, 0)
        self.assertEqual(self.host.reviews, [REF])
        self.assertEqual(self.reader.show(REF)["state"], "validate")
        self.assertEqual(self._budget_types(), [])

        # The resource recovers: the next tick launches the reviewer on its chain.
        self.runtime.head_health.record("openai-sub", "ready", "probe succeeded")
        launched = self.tick()

        self.assertEqual(launched["action"], "review-restarted")
        self.assertEqual(launched["status"], "ok")
        self.assertEqual(self.host.reviews, [REF, REF])
        record = self._record()
        self.assertEqual(record.review_provider_hold, "")
        self.assertEqual(record.review_head, "codex-reviewer")
        self.assertEqual(record.gate_attestation.to_json(), candidate)
        self.assertIn("Provider recovered (reviewer)", self._comments()[-1])
        self.assertEqual(self.tick()["action"], "waiting-review-verdict")

    def test_the_hold_survives_a_record_round_trip(self) -> None:
        record = DispatcherRecord(
            worker="w",
            workspace="/w",
            handle="",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="a",
            comment_baseline=0,
            review_baseline=0,
            state="review_starting",
            claimed_at=1.0,
            review_provider_hold="provider unavailable: openai-sub",
        )
        self.assertEqual(
            DispatcherRecord.from_json(record.to_json()).review_provider_hold, record.review_provider_hold
        )
        record.review_provider_hold = ""
        self.assertNotIn("review_provider_hold", record.to_json())

    # ---- B.4: only the first turn ----------------------------------------------------------

    def test_a_head_with_no_provider_failure_keeps_the_ordinary_wait(self) -> None:
        self.catalog.profiles["codex-reviewer"]["fallback"] = ["claude-opus"]
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        # A failure of some other run is not this head's.
        self.host.__dict__.setdefault("failed_runs", {})["another-run"] = ProviderError("auth", 401, "401")

        waited = self.tick()

        self.assertEqual(waited["action"], "waiting-review-verdict")
        self.assertEqual(self._record().review_head, "codex-reviewer")
        self.assertEqual(self._resource_health(), {})


if __name__ == "__main__":
    unittest.main()
