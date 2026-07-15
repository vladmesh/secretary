from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from secretary import role_env
from secretary.dispatcher import (
    CommandHostRuntime,
    CutoverState,
    DispatcherRuntime,
    FileLegacyPauseProbe,
    HostError,
    InstanceCatalog,
    LegacyPauseSnapshot,
    PilotSelector,
    _legacy_worker_branch,
    _render_codex_command,
    _wrap_role_shell_command,
)
from secretary.dispatcher_launcher import ensure_claude_workspace_ready, ensure_claude_workspace_trusted
from secretary.dispatcher_state import attempt_request_id as _attempt_request_id
from secretary.tasks import TaskAudit, TaskReader, TaskWriter


class FakeKanboard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.columns = [
            {"id": 1, "title": "Идеи"},
            {"id": 2, "title": "Ready"},
            {"id": 3, "title": "In progress"},
            {"id": 4, "title": "Validate"},
            {"id": 5, "title": "Blocked"},
            {"id": 6, "title": "Done"},
        ]
        self.tasks = [
            {
                "id": 12,
                "reference": "secretary-510-pilot",
                "title": "Pilot",
                "description": "pilot spec",
                "column_id": 2,
                "position": 1,
                "swimlane_id": 4,
                "date_creation": 1720000000,
                "date_modification": 1720000000,
            },
            {
                "id": 13,
                "reference": "secretary-510-neighbor",
                "title": "Neighbor",
                "description": "do not claim",
                "column_id": 2,
                "position": 2,
                "swimlane_id": 4,
                "date_creation": 1720000000,
                "date_modification": 1720000000,
            },
        ]
        self.metadata = {
            12: {"project": "secretary", "task_type": "code", "slug": "pilot"},
            13: {"project": "secretary", "task_type": "code", "slug": "neighbor"},
        }
        self.comments: dict[int, list[dict]] = {12: [], 13: []}
        self.now = 1720000000

    def call(self, method: str, **params: object) -> object:
        self.calls.append((method, params))
        if method == "getProjectByName":
            return {"id": 7}
        if method == "getColumns":
            return self.columns
        if method == "getActiveSwimlanes":
            return [{"id": 4, "name": "Secretary"}]
        if method == "getAllTasks":
            return self.tasks
        if method == "getTaskByReference":
            return next((task for task in self.tasks if task["reference"] == params["reference"]), None)
        if method == "getTaskMetadata":
            return self.metadata[int(params["task_id"])]
        if method == "saveTaskMetadata":
            self.metadata[int(params["task_id"])].update(params["values"])
            return True
        if method == "moveTaskPosition":
            task = next(task for task in self.tasks if int(task["id"]) == int(params["task_id"]))
            task["column_id"] = params["column_id"]
            self.now += 1
            task["date_modification"] = self.now
            return True
        if method == "createComment":
            self.now += 1
            self.comments[int(params["task_id"])].append(
                {"date_creation": self.now, "comment": params["content"]}
            )
            return len(self.comments[int(params["task_id"])])
        if method == "getAllComments":
            return self.comments[int(params["task_id"])]
        raise AssertionError(method)


class FakeCatalog:
    def default_branch(self, project: str, override: str | None) -> str:
        return override or "main"

    def worker_head(self, task: dict) -> str:
        return "codex"

    def review_head(self, task: dict) -> str:
        return "codex-reviewer"


class FakeHost:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.prepared: list[str] = []
        self.reviews: list[str] = []
        self.stopped: list[str] = []
        self.completed: list[str] = []
        self.fail_prepare_reason = ""

    def prepare_worker(
        self,
        task: dict,
        worker_id: str,
        head: str,
        *,
        attempt_id: str = "",
    ) -> dict[str, str]:
        if self.fail_prepare_reason:
            raise HostError(self.fail_prepare_reason)
        workspace = self.root / worker_id
        workspace.mkdir(parents=True, exist_ok=True)
        self.prepared.append(task["ref"])
        return {"workspace": str(workspace), "handle": f"term:{worker_id}"}

    def start_review(self, task: dict, record) -> str:
        self.reviews.append(task["ref"])
        return f"review:{task['ref']}"

    def restore_workspace(self, task: dict, worker: str) -> str:
        return str(self.root / worker)

    def complete_green(self, task: dict, record) -> None:
        self.completed.append(task["ref"])

    def stop(self, record) -> None:
        self.stopped.append(record.worker)


class FakeLegacyPause:
    def __init__(self) -> None:
        self.sufficient = True
        self.reason = "legacy dispatcher is freeze-paused"
        self.mode = "freeze"

    def set(self, *, sufficient: bool, reason: str, mode: str = "") -> None:
        self.sufficient = sufficient
        self.reason = reason
        self.mode = mode

    def snapshot(self) -> LegacyPauseSnapshot:
        return LegacyPauseSnapshot(
            self.sufficient,
            self.reason,
            path="/tmp/pause.json",
            mode=self.mode,
            actor="operator",
            since="2026-07-14T00:00:00+00:00",
        )


class DispatcherRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)
        self.board = FakeKanboard()
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]
        self.host = FakeHost(self.data_dir / "workspaces")
        self.legacy_pause = FakeLegacyPause()
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            TaskAudit(self.data_dir),
            CutoverState(self.data_dir),
            FakeCatalog(),  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
            legacy_pause=self.legacy_pause,  # type: ignore[arg-type]
        )
        self.selector = PilotSelector.exact("secretary-510-pilot")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def start_pilot(self) -> None:
        self.runtime.pause_old(self.selector, actor="operator", evidence="legacy hard pause")
        started = self.runtime.start_new_pilot(self.selector, actor="operator")
        self.assertEqual(started["status"], "ok")

    def commit_cutover(self) -> None:
        self.runtime.state.save({
            "version": 1,
            "phase": "cutover_committed",
            "pilot_ref": "secretary-510-pilot",
            "old_owner_paused": True,
            "records": {},
        })

    def append_committed_claim(self, attempt_id: str) -> str:
        request_id = _attempt_request_id(attempt_id, "claim", "secretary-510-pilot")
        TaskAudit(self.data_dir).append(
            request_id,
            {
                "event_id": f"evt_{attempt_id}",
                "schema_version": 1,
                "occurred_at": "2026-07-14T00:00:00Z",
                "actor": {"role": "dispatcher", "id": "secretary-pilot"},
                "kind": "claimed",
                "outcome": "success",
                "task_id": "task_kanboard_12",
                "ref": "secretary-510-pilot",
                "backend": {
                    "kind": "kanboard",
                    "task_id": 12,
                    "revision": "updated_at:2026-07-14T00:00:00Z",
                },
                "request_id": request_id,
                "payload": {
                    "worker": "secretary-510-pilot-pilot",
                    "resolved_head": "codex",
                    "resolved_review_head": "codex-reviewer",
                    "slug": "pilot",
                    "base_branch": None,
                    "cap": 3,
                },
            },
        )
        return request_id

    def audit_events(self) -> list[dict]:
        with open(TaskAudit(self.data_dir).events_path, encoding="utf-8") as events:
            return [json.loads(line) for line in events if line.strip()]

    def test_tick_fails_closed_without_cutover_guard(self) -> None:
        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.board.calls))

    def test_production_tick_fails_closed_before_committed_cutover(self) -> None:
        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "production cutover is not committed")
        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.board.calls))

    def test_production_tick_claims_first_ready_card_deterministically(self) -> None:
        self.commit_cutover()

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["actions"][0]["step"], "claim")
        self.assertEqual(result["actions"][0]["pilot_ref"], "secretary-510-pilot")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.reader.show("secretary-510-neighbor")["state"], "ready")
        self.assertEqual(self.host.prepared, ["secretary-510-pilot"])
        payload = self.runtime.production_state.load()
        self.assertEqual(payload["mode"], "production")
        self.assertEqual(list(payload["records"]), ["secretary-510-pilot"])

    def test_production_tick_repeat_does_not_launch_second_workspace(self) -> None:
        self.commit_cutover()
        self.runtime.production_tick()

        result = self.runtime.production_tick()

        self.assertEqual(result["actions"][0]["action"], "waiting-worker-report")
        self.assertEqual(self.host.prepared, ["secretary-510-pilot"])
        claim_events = [event for event in self.audit_events() if event["kind"] == "claimed"]
        self.assertEqual(len(claim_events), 1)

    def test_production_scan_skips_project_with_active_code_task(self) -> None:
        self.commit_cutover()
        self.board.tasks[0]["column_id"] = 3
        self.board.metadata[12].update({
            "claim": "secretary-510-pilot-pilot",
            "resolved_head": "codex",
            "resolved_review_head": "codex-reviewer",
        })
        self.board.tasks.append({
            "id": 14,
            "reference": "other-1",
            "title": "Other project",
            "description": "other spec",
            "column_id": 2,
            "position": 3,
            "swimlane_id": 4,
            "date_creation": 1720000000,
            "date_modification": 1720000000,
        })
        self.board.metadata[14] = {"project": "other", "task_type": "code", "slug": "other"}
        self.board.comments[14] = []

        result = self.runtime.production_tick()

        claimed = [action for action in result["actions"] if action.get("step") == "claim"][0]
        self.assertEqual(claimed["pilot_ref"], "other-1")
        self.assertEqual(self.reader.show("secretary-510-neighbor")["state"], "ready")
        self.assertEqual(self.reader.show("other-1")["state"], "in_progress")
        self.assertEqual(claimed["skipped_ready"][0]["ref"], "secretary-510-neighbor")

    def test_production_scan_skips_ready_steward_report(self) -> None:
        self.commit_cutover()
        self.board.metadata[12]["steward_report"] = "1"

        result = self.runtime.production_tick()

        claimed = [action for action in result["actions"] if action.get("step") == "claim"][0]
        self.assertEqual(claimed["pilot_ref"], "secretary-510-neighbor")
        self.assertEqual(claimed["skipped_ready"][0]["reason"], "steward report is not claimable")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "ready")

    def test_production_tick_contains_unexpected_card_exception(self) -> None:
        self.commit_cutover()
        self.board.tasks[0]["column_id"] = 3
        original_tick_task = self.runtime._tick_task

        def fail_once(task, records, payload, attempt_id):
            if task["ref"] == "secretary-510-pilot":
                raise KeyError("bad card")
            return original_tick_task(task, records, payload, attempt_id)

        self.runtime._tick_task = fail_once  # type: ignore[method-assign]

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["errors"][0]["ref"], "secretary-510-pilot")
        self.assertEqual(result["errors"][0]["code"], "unexpected_error")
        self.assertEqual(result["errors"][0]["message"], "KeyError")

    def test_production_run_backs_off_on_blocked_ticks(self) -> None:
        calls = []

        def blocked_tick():
            calls.append("tick")
            return {"status": "blocked", "step": "production-guard"}

        self.runtime.production_tick = blocked_tick  # type: ignore[method-assign]

        with mock.patch("secretary.dispatcher_production.time.sleep") as sleep:
            result = self.runtime.production_run(
                interval_seconds=1,
                max_interval_seconds=10,
                max_ticks=3,
            )

        self.assertEqual(result["ticks"], 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_production_owner_fence_loss_stops_mutations(self) -> None:
        self.commit_cutover()
        self.runtime.production_state.save({
            "version": 1,
            "mode": "production",
            "phase": "production",
            "owner": "another-dispatcher",
            "records": {},
        })

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("ownership fence", result["reason"])
        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.board.calls))

    def test_production_active_claim_divergence_does_not_claim_ready_neighbor(self) -> None:
        self.commit_cutover()
        self.board.tasks[0]["column_id"] = 3
        self.board.metadata[12].update({
            "claim": "foreign-worker",
            "resolved_head": "codex",
            "resolved_review_head": "codex-reviewer",
        })
        self.runtime.production_state.save({
            "version": 1,
            "mode": "production",
            "phase": "production",
            "owner": "secretary-pilot",
            "records": {
                "secretary-510-pilot": {
                    "attempt_id": "production-existing",
                    "claimed_at": 1720000000,
                    "comment_baseline": 0,
                    "handle": "term",
                    "head": "codex",
                    "review_baseline": 0,
                    "review_head": "codex-reviewer",
                    "state": "claimed",
                    "worker": "secretary-510-pilot-pilot",
                    "workspace": str(self.data_dir / "workspaces" / "secretary-510-pilot-pilot"),
                },
            },
        })

        result = self.runtime.production_tick()

        self.assertEqual(result["actions"][0]["status"], "blocked")
        self.assertEqual(result["actions"][0]["step"], "production-recovery")
        self.assertEqual(self.reader.show("secretary-510-neighbor")["state"], "ready")
        self.assertEqual(self.host.prepared, [])

    def test_production_singleton_lock_blocks_parallel_tick(self) -> None:
        self.commit_cutover()
        marker = self.data_dir / "lock-ready"
        lock_path = self.runtime.production_state.tick_lock
        holder = subprocess.Popen([
            sys.executable,
            "-c",
            (
                "import fcntl, pathlib, sys, time;"
                "path=pathlib.Path(sys.argv[1]); marker=pathlib.Path(sys.argv[2]);"
                "path.parent.mkdir(parents=True, exist_ok=True);"
                "handle=path.open('a+');"
                "fcntl.flock(handle.fileno(), fcntl.LOCK_EX);"
                "marker.write_text('ready', encoding='utf-8');"
                "time.sleep(5)"
            ),
            str(lock_path),
            str(marker),
        ])
        try:
            for _ in range(50):
                if marker.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            result = self.runtime.production_tick()
        finally:
            holder.terminate()
            holder.wait(timeout=5)

        self.assertEqual(result["status"], "blocked")
        self.assertIn("singleton lock", result["reason"])
        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.board.calls))

    def test_production_validate_recovery_with_review_intent_does_not_start_second_reviewer(self) -> None:
        self.commit_cutover()
        self.board.tasks[0]["column_id"] = 4
        self.board.metadata[12].update({
            "claim": "secretary-510-pilot-pilot",
            "resolved_head": "codex",
            "resolved_review_head": "codex-reviewer",
        })
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done",
            request_id="existing-report",
        )
        attempt_id = "production-adopt-secretary-510-pilot"
        self.writer.comment(
            role="dispatcher",
            actor="secretary-pilot",
            reference="secretary-510-pilot",
            body="Dispatcher review launch requested.",
            request_id=_attempt_request_id(attempt_id, "review-start-intent", "secretary-510-pilot"),
        )

        result = self.runtime.production_tick()

        self.assertEqual(result["actions"][0]["status"], "blocked")
        self.assertEqual(result["actions"][0]["reason"], "review launch outcome is unknown")
        self.assertEqual(self.host.reviews, [])

    def test_pause_old_rejects_drain_evidence_without_board_mutation(self) -> None:
        self.legacy_pause.set(
            sufficient=False,
            reason="legacy pause mode is drain, requires freeze",
            mode="drain",
        )

        result = self.runtime.pause_old(self.selector, actor="operator", evidence="legacy paused")

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "legacy pause mode is drain, requires freeze")
        self.assertNotEqual(self.runtime.state.load().get("old_owner_paused"), True)
        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.board.calls))

    def test_start_new_pilot_rechecks_legacy_freeze_before_state_change(self) -> None:
        self.runtime.pause_old(self.selector, actor="operator", evidence="legacy freeze")
        self.legacy_pause.set(
            sufficient=False,
            reason="legacy pause mode is drain, requires freeze",
            mode="drain",
        )

        result = self.runtime.start_new_pilot(self.selector, actor="operator")

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "legacy pause mode is drain, requires freeze")
        self.assertNotEqual(self.runtime.state.load().get("phase"), "new_pilot")

    def test_start_new_pilot_assigns_stable_attempt_id_and_request_ids(self) -> None:
        self.start_pilot()
        first = self.runtime.state.load()["attempt_id"]

        retried = self.runtime.start_new_pilot(self.selector, actor="operator")
        self.assertEqual(retried["attempt_id"], first)
        claimed = self.runtime.tick(self.selector)

        self.assertEqual(claimed["attempt_id"], first)
        events = self.audit_events()
        self.assertIn(
            _attempt_request_id(first, "claim", "secretary-510-pilot"),
            [event["request_id"] for event in events],
        )
        self.assertTrue(all(first in event["request_id"] for event in events))
        observed = self.runtime.observe(self.selector)
        self.assertEqual(observed["attempt_id"], first)

    def test_new_attempt_ignores_stale_committed_claim_after_ready_reset(self) -> None:
        old_request = self.append_committed_claim("attempt-old")
        self.board.tasks[0]["column_id"] = 2
        self.board.metadata[12].update({
            "claim": "",
            "resolved_head": "",
            "resolved_review_head": "",
        })
        self.start_pilot()
        new_attempt = self.runtime.state.load()["attempt_id"]
        self.board.calls.clear()

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["attempt_id"], new_attempt)
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.reader.show("secretary-510-pilot")["claim"]["worker"], "secretary-510-pilot-pilot")
        self.assertTrue(any(call[0] == "saveTaskMetadata" for call in self.board.calls))
        claim_requests = [
            event["request_id"]
            for event in self.audit_events()
            if event["kind"] == "claimed"
        ]
        self.assertIn(old_request, claim_requests)
        self.assertIn(_attempt_request_id(new_attempt, "claim", "secretary-510-pilot"), claim_requests)

    def test_claim_success_with_live_mismatch_fails_closed_before_host_launch(self) -> None:
        self.start_pilot()
        attempt_id = self.runtime.state.load()["attempt_id"]
        self.append_committed_claim(attempt_id)

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "claim live board mismatch")
        self.assertEqual(self.host.prepared, [])
        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "ready")
        self.assertIsNone(task["claim"]["worker"])
        divergences = self.runtime.state.load()["controlled_divergences"]
        self.assertEqual(divergences[-1]["attempt_id"], attempt_id)
        self.assertEqual(divergences[-1]["actual"]["state"], "ready")

    def test_claim_retry_inside_same_attempt_uses_verified_live_claim_without_backend_rewrite(self) -> None:
        self.start_pilot()
        attempt_id = self.runtime.state.load()["attempt_id"]
        self.append_committed_claim(attempt_id)
        self.board.tasks[0]["column_id"] = 3
        self.board.metadata[12].update({
            "claim": "secretary-510-pilot-pilot",
            "resolved_head": "codex",
            "resolved_review_head": "codex-reviewer",
        })
        self.board.calls.clear()

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["step"], "claim")
        self.assertEqual(self.host.prepared, ["secretary-510-pilot"])
        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.board.calls))
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.board.calls))

    def test_rollback_clears_current_records_preserves_attempt_history_and_audit(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        before = self.audit_events()
        attempt_id = self.runtime.state.load()["attempt_id"]

        self.runtime.rollback(self.selector, actor="operator", reason="pilot red")

        payload = self.runtime.state.load()
        self.assertEqual(payload["records"], {})
        self.assertEqual(payload["attempts"][-1]["attempt_id"], attempt_id)
        self.assertEqual(payload["attempts"][-1]["rolled_back_by"], "operator")
        self.assertEqual(self.audit_events(), before)

        self.runtime.pause_old(self.selector, actor="operator", evidence="legacy hard pause")
        restarted = self.runtime.start_new_pilot(self.selector, actor="operator")

        self.assertNotEqual(restarted["attempt_id"], attempt_id)
        self.assertEqual(len(self.runtime.state.load()["attempts"]), 2)

    def test_tick_refuses_before_claim_when_legacy_watchdog_is_active(self) -> None:
        self.runtime.state.save({
            "version": 1,
            "phase": "new_pilot",
            "pilot_ref": "secretary-510-pilot",
            "old_owner_paused": True,
            "records": {},
        })
        self.legacy_pause.set(
            sufficient=False,
            reason="legacy pause mode is drain, requires freeze",
            mode="drain",
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "legacy pause mode is drain, requires freeze")
        self.assertEqual(self.host.prepared, [])
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "ready")
        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.board.calls))

    def test_full_pilot_lifecycle_ignores_neighbor_ready_card(self) -> None:
        self.start_pilot()

        claimed = self.runtime.tick(self.selector)
        self.assertEqual(claimed["step"], "claim")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.reader.show("secretary-510-neighbor")["state"], "ready")

        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="PR: https://github.com/vladmesh/secretary/pull/1",
            request_id="worker-done",
        )
        advanced = self.runtime.tick(self.selector)
        self.assertEqual(advanced["to"], "validate")

        review_started = self.runtime.tick(self.selector)
        self.assertEqual(review_started["action"], "review-started")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="green",
            request_id="review-green",
        )
        done = self.runtime.tick(self.selector)

        self.assertEqual(done["to"], "done")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "done")
        neighbor = self.reader.show("secretary-510-neighbor")
        self.assertEqual(neighbor["state"], "ready")
        self.assertIsNone(neighbor["claim"]["worker"])
        self.assertEqual(self.host.completed, ["secretary-510-pilot"])

    def test_rollback_after_claim_preserves_board_state_and_claim(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)

        result = self.runtime.rollback(self.selector, actor="operator", reason="pilot red")

        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(result["phase"], "rolled_back")
        self.assertEqual(task["state"], "in_progress")
        self.assertEqual(task["claim"]["worker"], "secretary-510-pilot-pilot")
        self.assertEqual(len(task["comments"]), 1)

    def test_rollback_after_claim_is_idempotent_for_board_state(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)

        self.runtime.rollback(self.selector, actor="operator", reason="pilot red")
        result = self.runtime.rollback(self.selector, actor="operator", reason="pilot red")

        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(result["phase"], "rolled_back")
        self.assertEqual(task["state"], "in_progress")
        self.assertEqual(task["claim"]["worker"], "secretary-510-pilot-pilot")
        self.assertEqual([comment["marker"] for comment in task["comments"]], ["dispatcher"])

    def test_rollback_before_claim_preserves_ready_card(self) -> None:
        self.start_pilot()

        result = self.runtime.rollback(self.selector, actor="operator", reason="pilot red")

        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(result["phase"], "rolled_back")
        self.assertEqual(task["state"], "ready")
        self.assertIsNone(task["claim"]["worker"])
        self.assertEqual(task["comments"], [])

    def test_validate_adoption_restores_workspace_from_claim(self) -> None:
        self.start_pilot()
        self.board.tasks[0]["column_id"] = 4
        self.board.metadata[12]["claim"] = "secretary-510-pilot-pilot"

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "review-started")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

    def test_validate_adoption_processes_existing_review_verdict(self) -> None:
        self.start_pilot()
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done",
            request_id="existing-report",
        )
        self.board.tasks[0]["column_id"] = 4
        self.board.metadata[12]["claim"] = "secretary-510-pilot-pilot"
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="green",
            request_id="existing-verdict",
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["to"], "done")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "done")
        self.assertEqual(self.host.reviews, [])

    def test_host_error_comment_is_scrubbed(self) -> None:
        self.start_pilot()
        self.host.fail_prepare_reason = (
            "setup failed: API_TOKEN=secret-token "
            "raw abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN123456789"
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "blocked")
        body = task["comments"][-1]["body"]
        self.assertIn("API_TOKEN=<redacted>", body)
        self.assertNotIn("secret-token", body)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN123456789", body)

    def test_rollback_after_worker_report_preserves_validate_card_and_comments(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done body",
            request_id="worker-report",
        )
        self.runtime.tick(self.selector)

        self.runtime.rollback(self.selector, actor="operator", reason="pilot red")

        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "validate")
        self.assertEqual(task["claim"]["worker"], "secretary-510-pilot-pilot")
        self.assertEqual(
            [comment["marker"] for comment in task["comments"]],
            ["dispatcher", "report:done", "dispatcher"],
        )


class LegacyPauseProbeTests(unittest.TestCase):
    def test_file_probe_requires_freeze_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pause = Path(tmp) / "pause.json"
            pause.write_text(json.dumps({"mode": "soft", "actor": "operator"}), encoding="utf-8")

            result = FileLegacyPauseProbe(pause).snapshot()

        self.assertFalse(result.sufficient)
        self.assertEqual(result.reason, "legacy pause mode is drain, requires freeze")

    def test_file_probe_rejects_fresh_automation_owned_auto_resume_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pause = Path(tmp) / "pause.json"
            pause.write_text(
                json.dumps({
                    "mode": "hard",
                    "actor": "secretary",
                    "since": "2026-07-14T00:00:00+00:00",
                }),
                encoding="utf-8",
            )

            result = FileLegacyPauseProbe(pause).snapshot()

        self.assertFalse(result.sufficient)
        self.assertEqual(result.reason, "legacy freeze is automation-owned and auto-resume eligible")

    def test_file_probe_rejects_automation_owned_freeze_without_since(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pause = Path(tmp) / "pause.json"
            pause.write_text(json.dumps({"mode": "hard", "actor": "secretary"}), encoding="utf-8")

            result = FileLegacyPauseProbe(pause).snapshot()

        self.assertFalse(result.sufficient)
        self.assertEqual(result.reason, "legacy freeze is automation-owned and auto-resume eligible")

    def test_file_probe_accepts_automation_owned_freeze_when_auto_resume_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"TA_HARD_PAUSE_AUTO_RESUME_TTL_S": "0"}):
            pause = Path(tmp) / "pause.json"
            pause.write_text(json.dumps({"mode": "hard", "actor": "secretary"}), encoding="utf-8")

            result = FileLegacyPauseProbe(pause).snapshot()

        self.assertTrue(result.sufficient)


class DispatcherLauncherTests(unittest.TestCase):
    def test_codex_command_uses_unattended_profile_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()

            command = _render_codex_command(
                {"adapter": "codex", "model": "gpt-5.5", "effort": "extra", "codex_home": "/tmp/codex-home"},
                "TASK.md",
                workspace=str(workspace),
            )

        self.assertIn("CODEX_HOME=/tmp/codex-home codex exec", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("-m gpt-5.5", command)
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertIn("trust_level=\"trusted\"", command)
        self.assertIn('"$(cat TASK.md)"', command)
        self.assertNotIn('codex "$(cat TASK.md)"', command)

    def test_codex_tui_command_omits_exec_and_prompt_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()

            command = _render_codex_command(
                {"adapter": "codex", "model": "gpt-5.5", "effort": "extra", "codex_home": "/tmp/codex-home"},
                "TASK.md",
                workspace=str(workspace),
                mode="tui",
            )

        self.assertIn("CODEX_HOME=/tmp/codex-home codex --dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("codex exec", command)
        self.assertNotIn("--skip-git-repo-check", command)
        self.assertNotIn('"$(cat TASK.md)"', command)
        self.assertIn("trust_level=\"trusted\"", command)

    def test_card_launch_mode_overrides_codex_profile_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            catalog = object.__new__(InstanceCatalog)
            catalog._heads = {  # type: ignore[attr-defined]
                "profiles": {
                    "codex-tui": {
                        "adapter": "codex",
                        "model": "gpt-5.5",
                        "codex_mode": "tui",
                        "codex_home": "/tmp/codex-home",
                    }
                }
            }

            launch = catalog.head_launch(  # type: ignore[attr-defined]
                "codex-tui",
                "TASK.md",
                workspace=str(workspace),
                role="worker",
                codex_mode="exec",
            )

        self.assertFalse(launch.prompt_after_start)
        self.assertIn("codex exec", launch.command)
        self.assertIn('"$(cat TASK.md)"', launch.command)

    def test_claude_command_prepares_workspace_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".claude.json"
            workspace = str(Path(tmp) / "workspace")
            catalog = object.__new__(InstanceCatalog)
            catalog._heads = {  # type: ignore[attr-defined]
                "profiles": {"claude-opus": {"adapter": "claude", "model": "opus"}}
            }
            with mock.patch.dict(os.environ, {"TA_CLAUDE_JSON": str(config)}):
                command = catalog.head_command(  # type: ignore[attr-defined]
                    "claude-opus",
                    "TASK.md",
                    workspace=workspace,
                    role="worker",
                )
            data = json.loads(config.read_text(encoding="utf-8"))

        self.assertTrue(data["projects"][workspace]["hasTrustDialogAccepted"])
        self.assertEqual(data["theme"], "dark")
        self.assertIn("claude --dangerously-skip-permissions --model opus", command)
        self.assertIn("python3 -m secretary.role_env exec --role worker", command)

    def test_claude_ready_preserves_existing_theme_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".claude.json"
            config.write_text(
                json.dumps({
                    "theme": "light",
                    "projects": {"/ws/x": {"hasTrustDialogAccepted": True}},
                }),
                encoding="utf-8",
            )

            ensure_claude_workspace_ready("/ws/x", config)
            after_first = json.loads(config.read_text(encoding="utf-8"))
            with mock.patch("secretary.dispatcher_launcher.os.replace") as replace:
                ensure_claude_workspace_ready("/ws/x", config)

        self.assertEqual(after_first["theme"], "light")
        self.assertTrue(after_first["projects"]["/ws/x"]["hasTrustDialogAccepted"])
        replace.assert_not_called()

    def test_claude_trust_preserves_other_config_entries_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".claude.json"
            config.write_text(
                json.dumps({
                    "theme": "dark",
                    "projects": {
                        "/old": {"hasTrustDialogAccepted": False, "note": "keep"},
                        "/ws/x": {"hasTrustDialogAccepted": True, "other": 1},
                    },
                    "other": {"keep": True},
                }),
                encoding="utf-8",
            )

            ensure_claude_workspace_trusted("/ws/new", config)
            after_first = json.loads(config.read_text(encoding="utf-8"))
            with mock.patch("secretary.dispatcher_launcher.os.replace") as replace:
                ensure_claude_workspace_trusted("/ws/new", config)

        self.assertEqual(after_first["theme"], "dark")
        self.assertEqual(after_first["other"], {"keep": True})
        self.assertEqual(after_first["projects"]["/old"], {"hasTrustDialogAccepted": False, "note": "keep"})
        self.assertEqual(after_first["projects"]["/ws/x"], {"hasTrustDialogAccepted": True, "other": 1})
        self.assertTrue(after_first["projects"]["/ws/new"]["hasTrustDialogAccepted"])
        replace.assert_not_called()

    def test_claude_trust_rejects_corrupt_or_symlinked_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corrupt = Path(tmp) / "corrupt.json"
            corrupt.write_text("{not-json", encoding="utf-8")
            target = Path(tmp) / "target.json"
            target.write_text("{}", encoding="utf-8")
            symlink = Path(tmp) / "link.json"
            symlink.symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "cannot read Claude config"):
                ensure_claude_workspace_trusted("/ws/x", corrupt)
            with self.assertRaisesRegex(RuntimeError, "refusing symlinked Claude config"):
                ensure_claude_workspace_trusted("/ws/x", symlink)

    def test_claude_trust_fails_closed_when_atomic_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".claude.json"
            config.write_text(json.dumps({"projects": {"/old": {"keep": True}}}), encoding="utf-8")

            with mock.patch("secretary.dispatcher_launcher.os.replace", side_effect=OSError("boom")):
                with self.assertRaisesRegex(RuntimeError, "cannot update Claude config"):
                    ensure_claude_workspace_trusted("/ws/x", config)

            data = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(data, {"projects": {"/old": {"keep": True}}})

    def test_claude_ready_fails_closed_when_atomic_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".claude.json"
            original = {"hasCompletedOnboarding": True, "projects": {"/old": {"keep": True}}}
            config.write_text(json.dumps(original), encoding="utf-8")

            with mock.patch("secretary.dispatcher_launcher.os.replace", side_effect=OSError("boom")):
                with self.assertRaisesRegex(RuntimeError, "cannot update Claude config"):
                    ensure_claude_workspace_ready("/ws/x", config)

            data = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(data, original)

    def test_prepare_worker_lands_on_legacy_pipeline_branch_for_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = GitBranchHost(Path(tmp))
            task = {
                "ref": "secretary-510-pilot",
                "project": "secretary",
                "title": "Pilot",
                "description": "body",
                "workspace": {"base_branch": "main"},
            }

            result = host.prepare_worker(task, "secretary-510-pilot-pilot", "codex")
            branch = git(Path(result["workspace"]), "branch", "--show-current")

        self.assertEqual(branch, _legacy_worker_branch("secretary-510-pilot"))
        self.assertEqual(host.launched, [("codex", "TASK.md")])

    def test_worker_command_is_wrapped_in_role_env(self) -> None:
        wrapped = _wrap_role_shell_command("worker", "CODEX_HOME=/tmp/codex-home codex exec --dangerously-bypass-approvals-and-sandbox")

        self.assertIn("python3 -m secretary.role_env exec --role worker", wrapped)
        self.assertIn("/bin/sh -lc", wrapped)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", wrapped)

    def test_role_env_loads_board_env_and_strips_unallowed_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join([
                    "KANBOARD_URL=https://kanboard.example",
                    "KANBOARD_API_USER=bot",
                    "KANBOARD_API_TOKEN=board-token",
                    "PANELMEM_KB_PAT=memory-token",
                    "TA_CODEX_MODE=exec",
                ]),
                encoding="utf-8",
            )

            env = role_env.runtime_env(
                "worker",
                base_env={"GITHUB_TOKEN": "github-token", "PATH": "/usr/bin"},
                env_file=env_file,
                require=True,
            )

        self.assertEqual(env["BOARD_ROLE"], "worker")
        self.assertEqual(env["KANBOARD_API_TOKEN"], "board-token")
        self.assertEqual(env["TA_CODEX_MODE"], "exec")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("PANELMEM_KB_PAT", env)
        self.assertNotIn("GITHUB_TOKEN", env)

class GitBranchHost(CommandHostRuntime):
    def __init__(self, root: Path) -> None:
        super().__init__(FakeCatalog(), root, mode="real")  # type: ignore[arg-type]
        self.root = root
        self.launched: list[tuple[str, str]] = []

    def _create_workspace(self, project: str, worker_id: str, base: str) -> str:
        workspace = self.root / worker_id
        workspace.mkdir(parents=True)
        git(workspace, "init", "--initial-branch", base)
        git(workspace, "config", "user.name", "Test User")
        git(workspace, "config", "user.email", "test@example.invalid")
        (workspace / "README.md").write_text("seed\n", encoding="utf-8")
        git(workspace, "add", "README.md")
        git(workspace, "commit", "-m", "seed")
        return str(workspace)

    def _run_setup(self, project: str, workspace: str) -> None:
        return None

    def _launch(
        self,
        workspace: str,
        title: str,
        head: str,
        prompt_file: str,
        *,
        role: str,
        env_name: str,
        codex_mode: str | None = None,
    ) -> str:
        self.launched.append((head, prompt_file))
        return f"test:{head}"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()
