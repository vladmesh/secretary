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
from secretary._fsutil import try_file_lock
from secretary.checkpoint import CheckpointPusher, CheckpointResult, CheckpointWriter
from secretary.dispatcher import (
    CommandHostRuntime,
    CutoverState,
    DispatcherError,
    DispatcherRuntime,
    FileLegacyPauseProbe,
    HostError,
    InstanceCatalog,
    LegacyPauseSnapshot,
    PilotSelector,
    _legacy_worker_branch,
    _render_codex_command,
    _wrap_role_shell_command,
    default_data_dir,
)
from secretary.dispatcher_gate import GateResult
from secretary.dispatcher_launcher import ensure_claude_workspace_ready, ensure_claude_workspace_trusted
from secretary.dispatcher_state import DispatcherRecord, attempt_request_id as _attempt_request_id
from secretary.dispatcher_types import ReviewLaunch, review_pane_label
from secretary.head_registry import canonical_heads
from secretary.dispatcher_watchdog import (
    REVIEW_VERDICT_STALL_DEFAULT,
    WORKER_REPORT_STALL_DEFAULT,
    stall_seconds,
    wait_outcome,
)
from secretary.tasks import TaskAudit, TaskReader, TaskWriter


def _clear_env(test: unittest.TestCase, *names: str) -> None:
    """Drop dispatcher env overrides for the duration of a test and restore them afterwards.
    These are documented as unit-level knobs in docs/OPERATIONS.md, so a host that exports one
    would otherwise fail tests that assert the defaults."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    test.addCleanup(patcher.stop)
    for name in names:
        os.environ.pop(name, None)


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
    def __init__(
        self,
        adapter: dict | None = None,
        *,
        default_branch: str = "",
        instance_dir: Path | None = None,
    ) -> None:
        self._adapter = adapter or {}
        self._default_branch = default_branch
        # Checkpoint freshness reads the instance repo; the default is deliberately
        # not a repo, so tests that do not care read back empty git fields.
        self.instance_dir = instance_dir or Path("/nonexistent-instance")

    def default_branch(self, project: str, override: str | None) -> str:
        # Same precedence as InstanceCatalog: card override, then the binding, then "main".
        return override or self.binding(project).get("default_branch") or "main"

    def adapter(self, project: str) -> dict:
        return self._adapter

    def worker_head(self, task: dict) -> str:
        # Routing overrides resolve ahead of the role default, as in InstanceCatalog: the resolved
        # head is written to the board at claim and re-resolved on adoption, so a fake that always
        # answers "codex" would hide an override that never propagates.
        return str(task.get("routing", {}).get("head_override") or "codex")

    def review_head(self, task: dict) -> str:
        return str(task.get("routing", {}).get("review_head_override") or "codex-reviewer")

    def binding(self, project: str) -> dict:
        binding = {"repo": f"/home/dev/{project}"}
        if self._default_branch:
            binding["default_branch"] = self._default_branch
        return binding


class FakeHost:
    def __init__(self, root: Path) -> None:
        self.root = root
        # Ordered log of every host call. The per-method lists below answer "did it happen"; this
        # answers "in what order", which some invariants depend on (complete_green must push from
        # the workspace before teardown removes it).
        self.calls: list[str] = []
        self.prepared: list[str] = []
        self.reviews: list[str] = []
        self.stopped: list[str] = []
        self.torn_down: list[str] = []
        self.completed: list[str] = []
        self.fail_prepare_reason = ""
        self.fail_result_reason = ""
        self.fail_review_error: Exception | None = None
        # Failure hooks for host calls the real runtime can fail on: a rework workspace removed
        # out of band, a merge push the remote rejects, an orca terminal inventory that errors.
        self.fail_restart_reason = ""
        self.fail_complete_reason = ""
        self.review_running_error: Exception | None = None
        # None keeps the default "a review started in this process is live"; set a bool to model a
        # reviewer terminal that died after launch, which is what recovery actually has to detect.
        self.review_running_result: bool | None = None
        # Mechanical gate results consumed FIFO; empty means the default green (ci: none / passing).
        self.gate_results: list[GateResult] = []
        self.gate_calls: list[str] = []
        self.gate_error: Exception | None = None
        # Reviewer pane bookkeeping (secretary-651): which handle each review was split off, which
        # reviewer panes were closed on their own, and the commit the checkout reports. `commit` is
        # what start_review pins; reassign it to model a checkout that moved under a green verdict.
        self.split_from: list[str] = []
        self.stopped_reviews: list[str] = []
        self.commit = "c0ffee1234567890"
        self.instance_publish_recoveries: set[tuple[str, str]] = set()

    def prepare_worker(
        self,
        task: dict,
        worker_id: str,
        head: str,
        *,
        attempt_id: str = "",
    ) -> dict[str, str]:
        self.calls.append("prepare_worker")
        if self.fail_prepare_reason:
            raise HostError(self.fail_prepare_reason)
        workspace = self.root / worker_id
        workspace.mkdir(parents=True, exist_ok=True)
        self.prepared.append(task["ref"])
        return {
            "workspace": str(workspace),
            "handle": f"term:{worker_id}",
            "base_branch": task.get("workspace", {}).get("base_branch") or "main",
        }

    def start_review(self, task: dict, record) -> ReviewLaunch:
        self.calls.append("start_review")
        if self.fail_review_error is not None:
            raise self.fail_review_error
        self.reviews.append(task["ref"])
        # Mirror the real host: the reviewer gets its own pane and the worker head is shut down,
        # pinning the commit the reviewer judges.
        self.split_from.append(record.handle)
        return ReviewLaunch(
            handle=f"review:{task['ref']}",
            leaf=f"leaf:{task['ref']}",
            commit=self.commit,
        )

    def restart_worker(self, task: dict, record) -> str:
        self.calls.append("restart_worker")
        if self.fail_restart_reason:
            raise HostError(self.fail_restart_reason)
        self.prepared.append(task["ref"])
        return f"rework:{task['ref']}"

    def review_running(self, task: dict, record) -> bool:
        self.calls.append("review_running")
        if self.review_running_error is not None:
            raise self.review_running_error
        if self.review_running_result is not None:
            return self.review_running_result
        return task["ref"] in self.reviews

    def verify_worker_result(self, task: dict, record) -> None:
        self.calls.append("verify_worker_result")
        if self.fail_result_reason:
            raise HostError(self.fail_result_reason)

    def gate_check(self, task: dict, record) -> GateResult:
        self.calls.append("gate_check")
        self.gate_calls.append(task["ref"])
        if self.gate_error is not None:
            raise self.gate_error
        if self.gate_results:
            return self.gate_results.pop(0)
        return GateResult("green", "gate green")

    def restore_workspace(self, task: dict, worker: str) -> str:
        self.calls.append("restore_workspace")
        return str(self.root / worker)

    def complete_green(self, task: dict, record) -> None:
        self.calls.append("complete_green")
        if self.fail_complete_reason:
            raise HostError(self.fail_complete_reason)
        self.completed.append(task["ref"])

    def stop(self, record) -> None:
        self.calls.append("stop")
        self.stopped.append(record.worker)

    def stop_review(self, record) -> None:
        self.calls.append("stop_review")
        if record.review_handle:
            self.stopped_reviews.append(record.review_handle)

    def head_commit(self, record) -> str:
        self.calls.append("head_commit")
        return self.commit

    def is_instance_publish_recovery(self, task: dict, record, reviewed_commit: str, current_commit: str) -> bool:
        self.calls.append("is_instance_publish_recovery")
        return (reviewed_commit, current_commit) in self.instance_publish_recoveries

    def teardown(self, record) -> None:
        self.calls.append("teardown")
        self.stop(record)
        self.torn_down.append(record.worker)


class FakeCheckpoint:
    def __init__(self, outcome: CheckpointResult | Exception) -> None:
        self.outcome = outcome
        self.calls = 0

    def write(self) -> CheckpointResult:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakePusher:
    def __init__(self, outcome: dict | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def push(self, state: dict | None = None) -> dict:
        self.calls.append(dict(state or {}))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {**(state or {}), **self.outcome}


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
    def test_default_data_dir_rejects_relative_instance_data_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            instance.write_text(
                "version: 1\n"
                "name: test\n"
                "data_dir: secretary-data\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:test/instance.git\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DispatcherError, "data_dir: value must match pattern"):
                default_data_dir(instance)

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)
        self.board = FakeKanboard()
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        # workspace is pinned off the repo checkout: these tests stand in for a worker
        # report, and the done gate would otherwise read this repo's own working tree.
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        self.host = FakeHost(self.data_dir / "workspaces")
        self.legacy_pause = FakeLegacyPause()
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            TaskAudit(self.data_dir),
            CutoverState(self.data_dir),
            FakeCatalog(instance_dir=self.data_dir),  # type: ignore[arg-type]
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

    def test_production_tick_writes_the_checkpoint_at_the_end(self) -> None:
        self.commit_cutover()
        self.runtime.checkpoint = FakeCheckpoint(
            CheckpointResult(status="committed", commit="abc123", board_cards=2)
        )

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checkpoint"]["status"], "committed")
        self.assertEqual(result["checkpoint"]["commit"], "abc123")
        payload = self.runtime.production_state.load()
        self.assertEqual(payload["checkpoint"]["commit"], "abc123")

    def test_blocked_checkpoint_does_not_fail_the_tick(self) -> None:
        self.commit_cutover()
        self.runtime.checkpoint = FakeCheckpoint(
            CheckpointResult(status="blocked", reason="secret detected in state/board/cards.ndjson")
        )

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["actions"][0]["step"], "claim")
        self.assertEqual(result["checkpoint"]["status"], "blocked")
        self.assertIn("secret detected", result["checkpoint"]["reason"])

    def test_production_tick_pushes_and_carries_the_push_state_forward(self) -> None:
        self.commit_cutover()
        pusher = FakePusher({"status": "pushed", "last_push_commit": "abc123"})
        self.runtime.checkpoint_push = pusher

        result = self.runtime.production_tick()

        self.assertEqual(result["checkpoint_push"]["status"], "pushed")
        payload = self.runtime.production_state.load()
        self.assertEqual(payload["checkpoint_push"]["last_push_commit"], "abc123")

        self.runtime.production_tick()
        # The window lives in state, so the second tick sees the first tick's result.
        self.assertEqual(pusher.calls[1]["last_push_commit"], "abc123")

    def test_failed_push_leaves_the_tick_working(self) -> None:
        self.commit_cutover()
        self.runtime.checkpoint_push = FakePusher(
            {"status": "failed", "reason": "could not resolve host github.com", "failures": 3}
        )

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["actions"][0]["step"], "claim")
        self.assertEqual(result["checkpoint_push"]["status"], "failed")
        self.assertEqual(result["checkpoint_push"]["failures"], 3)

    def test_push_crash_is_contained_and_still_closes_its_window(self) -> None:
        self.commit_cutover()
        self.runtime.checkpoint_push = FakePusher(RuntimeError("ssh agent is gone"))

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checkpoint_push"]["status"], "failed")
        self.assertIn("ssh agent is gone", result["checkpoint_push"]["reason"])
        self.assertGreater(result["checkpoint_push"]["attempted_epoch"], 0)

    def test_production_observe_reports_checkpoint_freshness(self) -> None:
        self.commit_cutover()
        self.runtime.checkpoint = FakeCheckpoint(
            CheckpointResult(status="blocked", reason="task audit has 1 unresolved pending record(s)")
        )
        self.runtime.checkpoint_push = FakePusher(
            {
                "status": "diverged",
                "reason": "remote origin/main is at deadbeef0000",
                "remote_diverged": True,
            }
        )
        self.runtime.production_tick()

        observed = self.runtime.production_observe()

        self.assertEqual(observed["checkpoint"]["push_status"], "diverged")
        self.assertTrue(observed["checkpoint"]["remote_diverged"])
        self.assertIn("unresolved pending", observed["checkpoint"]["blocked_reason"])

    def test_checkpoint_crash_is_contained_in_the_tick_result(self) -> None:
        self.commit_cutover()
        self.runtime.checkpoint = FakeCheckpoint(RuntimeError("git is gone"))

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checkpoint"]["status"], "blocked")
        self.assertIn("git is gone", result["checkpoint"]["reason"])

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

    def test_probe_reports_the_claim_the_next_tick_would_make(self) -> None:
        self.commit_cutover()

        result = self.runtime.production_probe()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["step"], "production-probe")
        self.assertEqual(result["ready"], ["secretary-510-pilot", "secretary-510-neighbor"])
        claim = [entry for entry in result["would"] if entry["operation"] == "claim"]
        self.assertEqual(claim[0]["detail"]["ref"], "secretary-510-pilot")

    def test_probe_leaves_the_board_state_and_host_untouched(self) -> None:
        self.commit_cutover()
        before = self.runtime.production_state.load()

        self.runtime.production_probe()

        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.board.calls))
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "ready")
        self.assertEqual(self.host.prepared, [])
        self.assertEqual(self.runtime.production_state.load(), before)

    def test_probe_fails_the_same_guard_the_real_tick_fails(self) -> None:
        result = self.runtime.production_probe()

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["step"], "production-probe")
        self.assertEqual(result["reason"], "production cutover is not committed")

    def test_probe_is_blocked_while_a_real_tick_holds_the_singleton_lock(self) -> None:
        self.commit_cutover()
        lock = self.runtime.production_state.tick_lock
        lock.parent.mkdir(parents=True, exist_ok=True)
        with try_file_lock(lock) as acquired:
            self.assertTrue(acquired)
            result = self.runtime.production_probe()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("singleton lock", result["reason"])

    def test_probe_surfaces_a_broken_tick_instead_of_reporting_green(self) -> None:
        self.commit_cutover()
        self.runtime.production_tick()

        def broken(task, records, payload, attempt_id):
            raise KeyError("bad card")

        self.runtime._tick_task = broken  # type: ignore[method-assign]

        result = self.runtime.production_probe()

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["errors"][0]["code"], "unexpected_error")

    def test_probe_walks_an_active_card_without_running_the_gate(self) -> None:
        self.commit_cutover()
        self.runtime.production_tick()
        self.host.prepared.clear()

        result = self.runtime.production_probe()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["active"], ["secretary-510-pilot"])
        self.assertEqual(self.host.prepared, [])

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

    def test_production_active_claim_divergence_blocks_once_and_resumes_queue(self) -> None:
        self.commit_cutover()
        self.board.tasks[0]["column_id"] = 3
        self.board.tasks[1]["column_id"] = 5
        self.board.metadata[12].update({
            "claim": "foreign-worker",
            "resolved_head": "codex",
            "resolved_review_head": "codex-reviewer",
        })
        self.board.tasks.append({
            "id": 14,
            "reference": "other-9",
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

        results = [self.runtime.production_tick() for _ in range(3)]

        self.assertEqual(results[0]["actions"][0]["status"], "blocked")
        self.assertEqual(results[0]["actions"][0]["step"], "production-recovery")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertEqual(self.reader.show("other-9")["state"], "in_progress")
        self.assertEqual(self.host.prepared, ["other-9"])
        payload = self.runtime.production_state.load()
        self.assertEqual(len(payload["controlled_divergences"]), 1)
        self.assertNotIn("secretary-510-pilot", payload["records"])

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

    def test_decommission_old_requires_active_production_owner_and_records_fence(self) -> None:
        self.commit_cutover()

        blocked = self.runtime.decommission_old(self.selector, actor="operator")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "production owner is not active")

        self.runtime.production_state.save({
            "version": 1,
            "mode": "production",
            "phase": "production",
            "owner": "secretary-production",
            "records": {},
        })
        result = self.runtime.decommission_old(self.selector, actor="operator")

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["legacy_decommissioned"])
        payload = self.runtime.state.load()
        self.assertTrue(payload["legacy_decommissioned"])
        self.assertEqual(payload["legacy_decommissioned_by"], "operator")

    def test_production_tick_accepts_recorded_legacy_decommission_without_pause_file(self) -> None:
        self.commit_cutover()
        cutover = self.runtime.state.load()
        cutover["legacy_decommissioned"] = True
        self.runtime.state.save(cutover)
        self.legacy_pause.set(
            sufficient=False,
            reason="legacy freeze pause file is missing",
        )

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["step"], "production-tick")

    def test_production_validate_recovery_with_review_intent_restarts_missing_reviewer(self) -> None:
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
        review_baseline = 1
        self.writer.comment(
            role="dispatcher",
            actor="secretary-pilot",
            reference="secretary-510-pilot",
            body="Dispatcher review launch requested.",
            request_id=_attempt_request_id("review", "start-intent", "secretary-510-pilot", str(review_baseline)),
        )

        result = self.runtime.production_tick()

        self.assertEqual(result["actions"][0]["status"], "ok")
        self.assertEqual(result["actions"][0]["action"], "review-restarted")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

    def test_production_review_starting_recovery_does_not_freeze_other_projects(self) -> None:
        self.commit_cutover()
        self.board.tasks[0]["column_id"] = 4
        self.board.metadata[12].update({
            "claim": "secretary-510-pilot-pilot",
            "resolved_head": "codex",
            "resolved_review_head": "codex-reviewer",
        })
        self.board.tasks.append({
            "id": 14,
            "reference": "other-9",
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
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done",
            request_id="hard-kill-report",
        )
        review_baseline = 1
        self.writer.comment(
            role="dispatcher",
            actor="secretary-pilot",
            reference="secretary-510-pilot",
            body="Dispatcher review launch requested.",
            request_id=_attempt_request_id("review", "start-intent", "secretary-510-pilot", str(review_baseline)),
        )

        results = [self.runtime.production_tick() for _ in range(3)]

        self.assertEqual([result["status"] for result in results], ["ok", "ok", "ok"])
        actions = [action for result in results for action in result["actions"]]
        self.assertNotIn("review launch outcome is unknown", [action.get("reason") for action in actions])
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "validate")
        self.assertEqual(self.reader.show("other-9")["state"], "in_progress")
        self.assertEqual(self.host.prepared, ["other-9"])

    def test_cutover_after_pilot_review_start_does_not_start_second_reviewer(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done",
            request_id="worker-done-before-cutover",
        )
        self.runtime.tick(self.selector)
        review_started = self.runtime.tick(self.selector)
        review_request = _attempt_request_id("review", "start-intent", "secretary-510-pilot", "2")

        self.assertEqual(review_started["action"], "review-started")
        self.assertIsNotNone(TaskAudit(self.data_dir).committed_event(review_request))
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

        self.commit_cutover()
        result = self.runtime.production_tick()

        self.assertEqual(result["actions"][0]["status"], "ok")
        self.assertEqual(result["actions"][0]["action"], "waiting-review-verdict")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

    def test_production_review_recovery_lost_state_does_not_start_second_reviewer(self) -> None:
        self.commit_cutover()
        self.runtime.production_tick()
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done",
            request_id="production-worker-done",
        )
        self.runtime.production_tick()
        review_started = self.runtime.production_tick()
        review_request = _attempt_request_id("review", "start-intent", "secretary-510-pilot", "2")

        self.assertEqual(review_started["actions"][0]["action"], "review-started")
        self.assertIsNotNone(TaskAudit(self.data_dir).committed_event(review_request))
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

        self.runtime.production_state.path.unlink()
        result = self.runtime.production_tick()

        self.assertEqual(result["actions"][0]["status"], "ok")
        self.assertEqual(result["actions"][0]["action"], "waiting-review-verdict")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

    def test_production_review_unexpected_launch_error_moves_card_blocked(self) -> None:
        self.commit_cutover()
        self.runtime.production_tick()
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done",
            request_id="production-review-error-report",
        )
        self.runtime.production_tick()
        self.host.fail_review_error = OSError(
            "review write failed: API_TOKEN=secret-token raw abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN123456789"
        )

        result = self.runtime.production_tick()

        self.assertEqual(result["actions"][0]["status"], "blocked")
        self.assertEqual(result["actions"][0]["reason"], "host review failed")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertEqual(self.host.reviews, [])
        self.assertEqual(self.runtime.production_state.load()["records"], {})
        body = self.reader.show("secretary-510-pilot")["comments"][-1]["body"]
        self.assertIn("review bring-up failed", body)
        self.assertIn("API_TOKEN=<redacted>", body)
        self.assertNotIn("secret-token", body)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN123456789", body)

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
        self.assertEqual(self.host.torn_down, self.host.stopped)
        self.assertTrue(self.host.torn_down, "worktree must be torn down on done")

    def _run_worker_to_validate(self, request_id: str = "worker-done") -> None:
        """Claim, drive the worker to report:done, and advance the card into validate."""
        self.runtime.tick(self.selector)
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done",
            request_id=request_id,
        )
        advanced = self.runtime.tick(self.selector)
        self.assertEqual(advanced["to"], "validate")

    def _rewind_wait(self, kind: str, seconds: float = 100_000.0) -> None:
        """Age the current wait so the next tick sees it past the watchdog thresholds."""
        payload = self.runtime.state.load()
        record = payload["records"]["secretary-510-pilot"]
        self.assertTrue(record[f"{kind}_waiting_since"], f"{kind} wait was never stamped")
        record[f"{kind}_waiting_since"] -= seconds
        self.runtime.state.save(payload)

    def test_silent_reviewer_is_respawned_once_then_escalated(self) -> None:
        self.start_pilot()
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")

        waiting = self.runtime.tick(self.selector)
        self.assertEqual(waiting["action"], "waiting-review-verdict")

        # The reviewer head exited without registering a verdict (secretary-637), or is up but
        # wedged. Either way nothing lands and the ceiling ends the wait.
        self._rewind_wait("review", seconds=stall_seconds("review") + 60)
        respawned = self.runtime.tick(self.selector)

        self.assertEqual(respawned["action"], "review-respawned")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot", "secretary-510-pilot"])
        card = self.reader.show("secretary-510-pilot")
        self.assertEqual(card["state"], "validate")
        # The operator must be able to tell a first stall from an already-restarted head, hours
        # before the escalation shows up.
        self.assertIn("respawned the review head", card["comments"][-1]["body"])

        self._rewind_wait("review", seconds=stall_seconds("review") + 60)
        escalated = self.runtime.tick(self.selector)

        self.assertEqual(escalated["to"], "blocked")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertEqual(len(self.host.reviews), 2, "escalation must not start a third reviewer")

    def test_live_reviewer_keeps_waiting_inside_the_stall_ceiling(self) -> None:
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.runtime.tick(self.selector)

        self._rewind_wait("review", seconds=stall_seconds("review") - 60)
        waiting = self.runtime.tick(self.selector)

        self.assertEqual(waiting["action"], "waiting-review-verdict")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

    def test_live_head_that_overwrote_its_terminal_title_is_not_treated_as_dead(self) -> None:
        """secretary-654: heads replace the launch title with their own OSC sequence, so the
        terminal-title probe reports a healthy reviewer as gone. The watchdog must never ask."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.runtime.tick(self.selector)

        self.host.review_running_result = False
        self.host.calls.clear()
        self._rewind_wait("review", seconds=stall_seconds("review") - 60)
        waiting = self.runtime.tick(self.selector)

        self.assertEqual(waiting["action"], "waiting-review-verdict")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"], "healthy reviewer was killed")
        self.assertNotIn("review_running", self.host.calls, "watchdog must not probe liveness")

    def test_dead_worker_is_respawned_once_then_escalated(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)

        waiting = self.runtime.tick(self.selector)
        self.assertEqual(waiting["action"], "waiting-worker-report")

        # The rework worker never came up / died before reporting (secretary-649).
        self._rewind_wait("worker", seconds=stall_seconds("worker") + 60)
        respawned = self.runtime.tick(self.selector)

        self.assertEqual(respawned["action"], "worker-respawned")
        self.assertIn("restart_worker", self.host.calls)
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

        self._rewind_wait("worker", seconds=stall_seconds("worker") + 60)
        escalated = self.runtime.tick(self.selector)

        self.assertEqual(escalated["to"], "blocked")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertEqual(
            self.host.calls.count("restart_worker"), 1, "escalation must not respawn again"
        )

    def _stall_worker_wait_to_blocked(self) -> dict:
        """Drive one full stall cycle: wait past the ceiling, respawn, stall again, escalate."""
        for _ in range(5):
            if self.runtime.tick(self.selector).get("action") == "waiting-worker-report":
                break
        else:
            self.fail("card never reached the worker wait")
        self._rewind_wait("worker", seconds=stall_seconds("worker") + 60)
        self.assertEqual(self.runtime.tick(self.selector)["action"], "worker-respawned")
        self._rewind_wait("worker", seconds=stall_seconds("worker") + 60)
        return self.runtime.tick(self.selector)

    def test_second_stall_cycle_escalates_again_instead_of_deduping(self) -> None:
        """secretary-654: attempt_id outlives the card and the record is dropped on escalation, so
        an escalation request-id without a per-cycle token repeats on the next stall. TaskWriter
        answers a repeated request-id with success and no mutation, which would leave the tick
        reporting "blocked" while the card sits in in_progress forever."""
        self.start_pilot()
        self.runtime.tick(self.selector)

        first = self._stall_worker_wait_to_blocked()
        self.assertEqual(first["to"], "blocked")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")

        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-pilot",
            target="in_progress",
            reason="operator retries the card",
            request_id="po-unblock",
        )
        second = self._stall_worker_wait_to_blocked()

        self.assertEqual(second["to"], "blocked")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        stalls = [
            event["request_id"]
            for event in self.audit_events()
            if event["kind"] == "moved" and "worker-wait-stall" in event["request_id"]
        ]
        self.assertEqual(len(set(stalls)), 2, f"escalations must be distinct requests: {stalls}")
        comments = self.reader.show("secretary-510-pilot")["comments"]
        respawns = [c for c in comments if "respawned the worker head" in c["body"]]
        self.assertEqual(len(respawns), 2, "each stall cycle must leave its own respawn trace")

    def _stall_worker_wait_to_respawn_failure(self) -> dict:
        """Wait past the ceiling, then fail the respawn itself (the workspace went missing)."""
        for _ in range(5):
            if self.runtime.tick(self.selector).get("action") == "waiting-worker-report":
                break
        else:
            self.fail("card never reached the worker wait")
        self._rewind_wait("worker", seconds=stall_seconds("worker") + 60)
        self.host.fail_restart_reason = "rework workspace is missing"
        try:
            return self.runtime.tick(self.selector)
        finally:
            self.host.fail_restart_reason = ""

    def test_second_respawn_failure_blocks_the_card_instead_of_deduping(self) -> None:
        """secretary-654: the respawn-failed escalation needs a per-cycle request-id for the same
        reason the stall escalation does. In production attempt_id is a constant per card, so a
        bare attempt-scoped id makes the request-id a pure function of the ref: once committed,
        every later respawn failure on that card dedups into a success with no mutation, the tick
        reports "blocked", and the card sits in in_progress being re-adopted forever."""
        self.start_pilot()
        self.runtime.tick(self.selector)

        first = self._stall_worker_wait_to_respawn_failure()
        self.assertEqual(first["status"], "blocked")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")

        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-pilot",
            target="in_progress",
            reason="operator restored the workspace",
            request_id="po-unblock",
        )
        second = self._stall_worker_wait_to_respawn_failure()

        self.assertEqual(second["status"], "blocked")
        self.assertEqual(
            self.reader.show("secretary-510-pilot")["state"],
            "blocked",
            "tick reported blocked but the card never moved",
        )
        blocks = [
            event["request_id"]
            for event in self.audit_events()
            if event["kind"] == "moved" and "worker-respawn-blocked" in event["request_id"]
        ]
        self.assertEqual(len(set(blocks)), 2, f"escalations must be distinct requests: {blocks}")

    def test_adopted_card_still_sees_a_report_the_dispatcher_never_consumed(self) -> None:
        """secretary-654: the worker posts report:done and the dispatcher loses its record before
        acting on it. Baselining adoption at len(comments) hid the report, so the card burned the
        whole worker ceiling and respawned a head to redo work already sitting on the board."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.assertEqual(self.runtime.tick(self.selector)["action"], "waiting-worker-report")
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done",
            request_id="worker-done-before-adoption",
        )
        payload = self.runtime.state.load()
        payload["records"] = {}
        self.runtime.state.save(payload)

        self.runtime.tick(self.selector)  # re-adoption re-verifies the claim
        advanced = self.runtime.tick(self.selector)

        self.assertEqual(advanced["to"], "validate")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "validate")

    def test_review_red_clears_the_review_wait_watchdog(self) -> None:
        """Each review round gets its own respawn budget. Without the reset, a round-1 stall that
        was already respawned leaves respawns=1 and a stale waiting_since, so the first waiting
        tick of round 2 escalates straight to Blocked with no respawn at all."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        self.assertEqual(self.runtime.tick(self.selector)["action"], "waiting-review-verdict")
        self._rewind_wait("review", seconds=stall_seconds("review") + 60)
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-respawned")

        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="red",
            body="findings from the respawned reviewer",
            request_id="review-red-round-1",
        )
        self.assertEqual(self.runtime.tick(self.selector)["action"], "rework-started")

        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(record["review_waiting_since"], 0.0)
        self.assertEqual(record["review_respawns"], 0)

        # And the invariant the counters exist for: round 2 still gets its one respawn.
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="reworked",
            request_id="worker-done-round-2",
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        self.assertEqual(self.runtime.tick(self.selector)["action"], "waiting-review-verdict")
        self._rewind_wait("review", seconds=stall_seconds("review") + 60)

        stalled = self.runtime.tick(self.selector)

        self.assertEqual(stalled["action"], "review-respawned")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "validate")

    def test_blocked_rework_returned_to_ready_reuses_its_workspace(self) -> None:
        self.start_pilot()
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="red",
            body="fix this",
            request_id="review-red-preserved-workspace",
        )
        original_workspace = self.runtime.state.load()["records"]["secretary-510-pilot"]["workspace"]
        original_attempt = self.runtime.state.load()["records"]["secretary-510-pilot"]["attempt_id"]
        self.host.fail_restart_reason = "terminal service unavailable"
        blocked = self.runtime.tick(self.selector)
        self.assertEqual(blocked["reason"], "rework bring-up failed")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")

        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-pilot",
            target="ready",
            reason="retry after outage",
            request_id="po-requeue-preserved-workspace",
        )
        self.host.fail_restart_reason = ""
        restarted = self.runtime.tick(self.selector)

        self.assertEqual(restarted["status"], "ok", restarted)
        self.assertEqual(restarted["workspace"], original_workspace)
        self.assertEqual(self.host.prepared, ["secretary-510-pilot", "secretary-510-pilot"])
        self.assertNotEqual(restarted["attempt_id"], original_attempt)
        self.assertNotEqual(
            _attempt_request_id(original_attempt, "worker-report-done", "secretary-510-pilot", "0"),
            _attempt_request_id(restarted["attempt_id"], "worker-report-done", "secretary-510-pilot", "0"),
        )

    def test_worker_report_clears_the_worker_wait_watchdog(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.runtime.tick(self.selector)
        self._rewind_wait("worker")
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done",
            request_id="worker-done-after-wait",
        )

        advanced = self.runtime.tick(self.selector)

        self.assertEqual(advanced["to"], "validate")
        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(record["worker_waiting_since"], 0.0)
        self.assertEqual(record["worker_respawns"], 0)

    def test_gate_green_advances_to_review(self) -> None:
        self.start_pilot()
        self.host.gate_results = [GateResult("green", "local validation passed")]
        self._run_worker_to_validate()

        gated = self.runtime.tick(self.selector)

        self.assertEqual(gated["action"], "review-started")
        self.assertEqual(self.host.gate_calls, ["secretary-510-pilot"])
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

    def test_gate_red_bounces_card_to_worker(self) -> None:
        self.start_pilot()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self._run_worker_to_validate()

        gated = self.runtime.tick(self.selector)

        self.assertEqual(gated["action"], "gate-red-rework")
        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "in_progress")
        self.assertIn("Механический гейт валидации красный", task["comments"][-1]["body"])
        self.assertEqual(self.host.reviews, [])
        # worker prepared once at claim, once on the gate-red relaunch
        self.assertEqual(self.host.prepared, ["secretary-510-pilot", "secretary-510-pilot"])

    def test_gate_red_scrubs_secrets_in_bounce_comment(self) -> None:
        self.start_pilot()
        self.host.gate_results = [
            GateResult("red", "local validation failed", "API_TOKEN=super-secret-value boom")
        ]
        self._run_worker_to_validate()

        self.runtime.tick(self.selector)

        body = self.reader.show("secretary-510-pilot")["comments"][-1]["body"]
        self.assertIn("API_TOKEN=<redacted>", body)
        self.assertNotIn("super-secret-value", body)

    def test_gate_pending_waits_without_review(self) -> None:
        self.start_pilot()
        self.host.gate_results = [GateResult("pending", "CI pending")]
        self._run_worker_to_validate()

        gated = self.runtime.tick(self.selector)

        self.assertEqual(gated["action"], "gate-pending")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "validate")
        self.assertEqual(self.host.reviews, [])

    def test_gate_pending_then_green_advances_to_review(self) -> None:
        self.start_pilot()
        self.host.gate_results = [GateResult("pending", "CI pending"), GateResult("green", "CI green")]
        self._run_worker_to_validate()

        self.runtime.tick(self.selector)
        advanced = self.runtime.tick(self.selector)

        self.assertEqual(advanced["action"], "review-started")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

    def test_gate_infra_failure_blocks_card(self) -> None:
        self.start_pilot()
        self.host.gate_error = HostError("gate workspace is missing")
        self._run_worker_to_validate()

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertEqual(self.host.reviews, [])

    def test_merge_blocked_when_gate_not_green(self) -> None:
        self.start_pilot()
        # pre-review gate green, then the merge re-check goes red (CI broke after review started).
        self.host.gate_results = [GateResult("green", "green"), GateResult("red", "CI red", "boom")]
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)  # gate green -> review started
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="green",
            request_id="review-green",
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "merge-gate-red-rework")
        self.assertEqual(self.host.completed, [], "a non-green gate must never merge")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_merge_proceeds_when_gate_green(self) -> None:
        self.start_pilot()
        self.host.gate_results = [GateResult("green", "green"), GateResult("green", "green")]
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)  # gate green -> review started
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="green",
            request_id="review-green",
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["to"], "done")
        self.assertEqual(self.host.completed, ["secretary-510-pilot"])

    def test_merge_publishes_from_the_workspace_before_tearing_it_down(self) -> None:
        """`complete_green` pushes out of the worker workspace and `teardown` removes that
        worktree. Swapping them merges nothing and only fails on a live host, so pin the order."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)  # gate green -> review started
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="green",
            request_id="review-green",
        )

        self.runtime.tick(self.selector)

        self.assertIn("complete_green", self.host.calls)
        self.assertIn("teardown", self.host.calls)
        self.assertLess(
            self.host.calls.index("complete_green"),
            self.host.calls.index("teardown"),
            "the merge must publish before the worktree is removed",
        )

    def _drive_to_green_verdict(self) -> None:
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)  # gate green -> review started
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="green",
            request_id="review-green",
        )

    def test_rejected_merge_blocks_the_card_instead_of_escaping_the_tick(self) -> None:
        """The merge push is rejected when the branch is not a fast-forward of main. That must
        park the card in Blocked: an escaping HostError would leave a green card in validate and
        every later tick would retry the same doomed merge with the worker terminals still up."""
        self.start_pilot()
        self.host.fail_complete_reason = "merge push failed: ! [rejected] non-fast-forward"
        self._drive_to_green_verdict()

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "merge failed")
        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "blocked")
        self.assertIn("non-fast-forward", task["comments"][-1]["body"])
        self.assertEqual(self.host.torn_down, [], "a failed merge must not remove the workspace")
        self.assertEqual(self.host.stopped, ["secretary-510-pilot-pilot"])

    def test_rework_bringup_failure_after_red_review_blocks_the_card(self) -> None:
        """The rework workspace can be gone by the time a red verdict lands. The card has already
        been moved to In progress at that point, so the failure has to move it on to Blocked."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.host.fail_restart_reason = "rework workspace is missing"
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="red",
            body="fix it",
            request_id="review-red",
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "rework bring-up failed")
        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "blocked")
        self.assertIn("rework workspace is missing", task["comments"][-1]["body"])
        self.assertNotIn("secretary-510-pilot", self.runtime.state.load()["records"])

    def test_rework_bringup_failure_after_red_gate_blocks_the_card(self) -> None:
        self.start_pilot()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self.host.fail_restart_reason = "rework workspace is missing"
        self._run_worker_to_validate()

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "rework bring-up failed")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")

    def test_review_recovery_restarts_a_reviewer_whose_terminal_died(self) -> None:
        """`review_running` asks the host whether the reviewer terminal is live, not whether one
        was ever launched. A dead terminal must be relaunched rather than waited on forever."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)  # gate green -> review started
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])
        record = self.runtime.state.records(self.runtime.state.load())["secretary-510-pilot"]
        self.assertEqual(record.state, "reviewing")
        record.state = "review_starting"  # a tick died between launch intent and confirmation
        payload = self.runtime.state.load()
        self.runtime.state.put_records(payload, {"secretary-510-pilot": record})
        self.runtime.state.save(payload)
        self.host.review_running_result = False

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "review-restarted")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot", "secretary-510-pilot"])

    def test_review_inventory_failure_blocks_the_card(self) -> None:
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        record = self.runtime.state.records(self.runtime.state.load())["secretary-510-pilot"]
        record.state = "review_starting"
        payload = self.runtime.state.load()
        self.runtime.state.put_records(payload, {"secretary-510-pilot": record})
        self.runtime.state.save(payload)
        self.host.review_running_error = HostError("orca terminal list failed")

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "review inventory failed")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")

    def test_routing_head_overrides_reach_the_board_and_the_host(self) -> None:
        """A card can pin its own worker/reviewer head. The resolved pair is written to the board
        at claim and re-read on adoption, so a lost override shows up as a claim divergence."""
        self.start_pilot()
        self.board.metadata[12].update({"head": "claude", "review_head": "claude-reviewer"})

        self.runtime.tick(self.selector)

        routing = self.reader.show("secretary-510-pilot")["routing"]
        self.assertEqual(routing["resolved_worker_head"], "claude")
        self.assertEqual(routing["resolved_review_head"], "claude-reviewer")
        record = self.runtime.state.records(self.runtime.state.load())["secretary-510-pilot"]
        self.assertEqual(record.head, "claude")
        self.assertEqual(record.review_head, "claude-reviewer")

    def test_reworked_card_reruns_the_gate_instead_of_coasting(self) -> None:
        """A gate-red bounce resets the pass; the next done report is fresh code and must be gated
        again. Reusing the stale green would ship exactly the regression the gate exists to stop."""
        self.start_pilot()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self._run_worker_to_validate()
        bounced = self.runtime.tick(self.selector)
        self.assertEqual(bounced["action"], "gate-red-rework")
        self.assertEqual(self.host.gate_calls, ["secretary-510-pilot"])

        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="fixed",
            request_id="worker-done-after-gate-red",
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
        advanced = self.runtime.tick(self.selector)

        self.assertEqual(advanced["action"], "review-started")
        self.assertEqual(
            self.host.gate_calls,
            ["secretary-510-pilot", "secretary-510-pilot"],
            "the gate must re-run for the reworked code state",
        )

    def test_red_review_relaunches_worker_for_rework(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="first report",
            request_id="worker-done-first",
        )
        self.runtime.tick(self.selector)
        self.runtime.tick(self.selector)
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="red",
            body="fix the hermetic test",
            request_id="review-red",
        )

        relaunched = self.runtime.tick(self.selector)

        self.assertEqual(relaunched["action"], "rework-started")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.host.prepared, ["secretary-510-pilot", "secretary-510-pilot"])
        self.assertEqual(
            self.host.stopped_reviews,
            ["review:secretary-510-pilot"],
            "a red verdict must end the reviewer's pane",
        )
        self.assertEqual(
            self.host.stopped, [], "a red verdict must not stop the whole worktree's terminals"
        )
        self.assertEqual(self.host.torn_down, [], "rework must reuse the workspace, not tear it down")

        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="rework report",
            request_id="worker-done-rework",
        )
        advanced = self.runtime.tick(self.selector)

        self.assertEqual(advanced["to"], "validate")

    def _record_json(self) -> dict:
        return self.runtime.state.load()["records"]["secretary-510-pilot"]

    def test_review_persists_the_reviewer_pane_apart_from_the_worker_handle(self) -> None:
        """secretary-651: both heads of a card live in one worktree, so one `handle` field cannot
        address them. Stopping the reviewer used to mean stopping whatever `handle` last pointed
        at, and after a launch that was the reviewer's own pane."""
        self.start_pilot()
        self._run_worker_to_validate()

        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")

        record = self._record_json()
        self.assertEqual(record["review_handle"], "review:secretary-510-pilot")
        self.assertEqual(record["review_leaf"], "leaf:secretary-510-pilot")
        self.assertEqual(record["review_commit"], self.host.commit)
        self.assertNotEqual(record["review_handle"], record["handle"])
        self.assertEqual(
            self.host.split_from,
            ["term:secretary-510-pilot-pilot"],
            "the reviewer pane must be split off the worker's own pane",
        )

    def test_interrupted_review_tick_reuses_the_existing_pane(self) -> None:
        """A tick killed between the launch and its verdict leaves the card in review_starting with
        the pane already up. Recovery must find that pane and wait, not split a second reviewer into
        the worktree next to the first."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        payload = self.runtime.state.load()
        payload["records"]["secretary-510-pilot"]["state"] = "review_starting"
        self.runtime.state.save(payload)

        recovered = self.runtime.tick(self.selector)

        self.assertEqual(recovered["action"], "waiting-review-verdict")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"], "a second reviewer was started")
        self.assertEqual(self._record_json()["state"], "reviewing")

    def test_interrupted_review_tick_restarts_a_pane_that_did_not_survive(self) -> None:
        """The mirror case: the record says review_starting but no reviewer pane is up, so the
        launch has to be redone rather than waited on forever."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        payload = self.runtime.state.load()
        payload["records"]["secretary-510-pilot"]["state"] = "review_starting"
        self.runtime.state.save(payload)
        self.host.review_running_result = False

        recovered = self.runtime.tick(self.selector)

        self.assertEqual(recovered["action"], "review-restarted")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot", "secretary-510-pilot"])

    def test_red_verdict_clears_the_reviewer_pane_from_the_record(self) -> None:
        """The workspace comes back to the worker, so a stale reviewer handle left on the record
        would make the next round's stop close a pane that is no longer the reviewer's."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="red",
            body="fix it",
            request_id="review-red-pane",
        )

        self.assertEqual(self.runtime.tick(self.selector)["action"], "rework-started")

        record = self._record_json()
        self.assertEqual(record["review_handle"], "")
        self.assertEqual(record["review_leaf"], "")
        self.assertEqual(record["review_commit"], "")
        self.assertEqual(record["handle"], "rework:secretary-510-pilot")
        self.assertEqual(self.host.torn_down, [], "the checkout must survive a red verdict")

    def test_green_verdict_for_a_moved_checkout_is_not_merged(self) -> None:
        """The reviewer judged one commit; if the checkout has moved on, that verdict says nothing
        about what would land. The card goes back to the worker instead of merging unreviewed work."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.host.commit = "0000000000000000"
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="looks good",
            request_id="review-green-drifted",
        )

        bounced = self.runtime.tick(self.selector)

        self.assertEqual(bounced["action"], "review-freeze-red-rework")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.host.completed, [], "a verdict for another code state must not merge")
        self.assertEqual(self.host.torn_down, [])
        self.assertIn("другому состоянию кода", self.reader.show("secretary-510-pilot")["comments"][-1]["body"])

    def test_green_verdict_for_a_descendant_checkout_is_not_merged_by_default(self) -> None:
        """A descendant can contain new commits after review; only the instance publish recovery
        path is allowed to finish from a moved checkout."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        reviewed = self.host.commit
        self.host.commit = "1111111111111111"
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="looks good",
            request_id="review-green-descendant",
        )

        bounced = self.runtime.tick(self.selector)

        self.assertEqual(bounced["action"], "review-freeze-red-rework")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.host.completed, [])
        self.assertIn(("is_instance_publish_recovery"), self.host.calls)
        self.assertEqual(reviewed, "c0ffee1234567890")

    def test_green_verdict_for_instance_publish_recovery_can_finish_from_published_descendant(self) -> None:
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        reviewed = self.host.commit
        self.host.commit = "2222222222222222"
        self.host.instance_publish_recoveries.add((reviewed, self.host.commit))
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="looks good",
            request_id="review-green-instance-recovery",
        )

        done = self.runtime.tick(self.selector)

        self.assertEqual(done["to"], "done")
        self.assertEqual(self.host.completed, ["secretary-510-pilot"])
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "done")

    def test_green_verdict_for_the_reviewed_checkout_merges_and_tears_down(self) -> None:
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="looks good",
            request_id="review-green-pinned",
        )

        done = self.runtime.tick(self.selector)

        self.assertEqual(done["to"], "done")
        self.assertEqual(self.host.completed, ["secretary-510-pilot"])
        self.assertEqual(self.host.torn_down, ["secretary-510-pilot-pilot"])

    def test_review_bringup_failure_blocks_without_destroying_the_workspace(self) -> None:
        """A split that fails must not park the card in `reviewing` with no reviewer behind it, and
        must leave the worker's checkout alone — it is the only copy of the work."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.host.fail_review_error = HostError("orca terminal split failed: terminal_exited")

        blocked = self.runtime.tick(self.selector)

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "host review failed")
        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "blocked")
        self.assertIn("review bring-up failed", task["comments"][-1]["body"])
        self.assertNotIn("secretary-510-pilot", self.runtime.state.load()["records"])
        self.assertEqual(self.host.torn_down, [], "a failed reviewer must not remove the checkout")

    def _reviewer_red_request_id(self) -> str:
        """The red request-id the dispatcher actually hands the reviewer, taken from the prompt
        it renders rather than recomputed here."""
        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        prompt = CommandHostRuntime(
            FakeCatalog(), self.data_dir, mode="noop"  # type: ignore[arg-type]
        )._review_prompt(
            self.reader.show("secretary-510-pilot"),
            record["attempt_id"],
            int(record["review_baseline"]),
        )
        line = next(line for line in prompt.splitlines() if "--kind red" in line)
        return line.split("--request-id ", 1)[1].split()[0]

    def test_second_red_verdict_in_one_attempt_is_registered(self) -> None:
        """secretary-654: attempt_id survives review:red -> rework -> report:done, so a round-less
        red request-id made round 2's verdict a replay of round 1. The write was deduped, the
        reviewer was told "recorded" and exited, and the card sat in validate until the watchdog
        escalated it with the findings lost."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")

        round_one = self._reviewer_red_request_id()
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="red",
            body="round 1: fix the hermetic test",
            request_id=round_one,
        )
        self.assertEqual(self.runtime.tick(self.selector)["action"], "rework-started")

        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="rework report",
            request_id="worker-done-rework",
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")

        round_two = self._reviewer_red_request_id()
        self.assertNotEqual(round_two, round_one, "round 2 must not reuse round 1's request-id")

        before = len(self.reader.show("secretary-510-pilot")["comments"])
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="red",
            body="round 2: the fix regressed the watchdog",
            request_id=round_two,
        )
        after = self.reader.show("secretary-510-pilot")["comments"]

        self.assertEqual(len(after), before + 1, "round 2 verdict was deduped away")
        self.assertIn("round 2", after[-1]["body"])

        reworked = self.runtime.tick(self.selector)
        self.assertEqual(reworked["action"], "rework-started")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_verdict_body_file_is_per_round(self) -> None:
        """Heads are told to leave the body file behind, so a shared name lets round 2 post
        round 1's body if the head reuses the file without rewriting it."""
        host = CommandHostRuntime(FakeCatalog(), self.data_dir, mode="noop")  # type: ignore[arg-type]
        task = {"ref": "secretary-510-pilot", "project": "secretary", "routing": {}}

        first = host._review_prompt(task, "attempt-1", 4)
        second = host._review_prompt(task, "attempt-1", 9)

        def body_file(doc: str) -> str:
            line = next(line for line in doc.splitlines() if "--kind red" in line)
            return line.split("--body-file ", 1)[1].split()[0]

        self.assertNotEqual(body_file(first), body_file(second))

    def test_done_report_with_uncommitted_result_blocks_before_review(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.host.fail_result_reason = "worker reported done with uncommitted changes"
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="tests pass",
            request_id="worker-done-dirty",
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "worker result is not durable")
        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "blocked")
        self.assertIn("uncommitted changes", task["comments"][-1]["body"])
        self.assertEqual(self.host.reviews, [])

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


class HeadPromptTests(unittest.TestCase):
    """The report/verdict commands handed to a head must survive the codex runtime: a concrete
    body-file path written with a normal editing tool, no inline shell assembly (secretary-637)."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # The assertions below name /tmp, and docs/OPERATIONS.md documents this override on the
        # unit, so a host that exports it would fail the suite for no reason.
        _clear_env(self, "SECRETARY_DISPATCHER_BODY_DIR")
        self.host = CommandHostRuntime(FakeCatalog(), Path(self.tmpdir.name), mode="noop")  # type: ignore[arg-type]
        self.task = {
            "ref": "secretary-510-pilot",
            "project": "secretary",
            "description": "body with `backticks` and \"quotes\"",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _command_lines(self, doc: str) -> list[str]:
        return [line for line in doc.splitlines() if "python3 -m secretary task" in line]

    def test_review_prompt_names_a_concrete_body_file(self) -> None:
        doc = self.host._review_prompt(self.task, "attempt-1", 3)
        commands = self._command_lines(doc)

        self.assertEqual(len(commands), 2, "one green and one red command")
        for command in commands:
            self.assertIn("--body-file /tmp/secretary-verdict-secretary-510-pilot-3.md", command)
            self.assertNotIn("<file>", command)

    def test_worker_prompt_names_a_concrete_body_file(self) -> None:
        doc = self.host._worker_task_doc(self.task, "main", "attempt-1")
        commands = self._command_lines(doc)

        self.assertEqual(len(commands), 1)
        self.assertIn("--body-file /tmp/secretary-report-secretary-510-pilot-0.md", commands[0])
        self.assertNotIn("<file>", commands[0])

    def test_body_file_lives_outside_the_workspace(self) -> None:
        """A body file inside the worktree would make `git status` dirty, and the done-report
        check rejects a dirty workspace."""
        for doc in (
            self.host._review_prompt(self.task, "attempt-1", 3),
            self.host._worker_task_doc(self.task, "main", "attempt-1"),
        ):
            for command in self._command_lines(doc):
                path = command.split("--body-file ", 1)[1].split()[0]
                self.assertTrue(path.startswith("/tmp/"), path)

    def _record(self, workspace: Path, review_baseline: int) -> DispatcherRecord:
        return DispatcherRecord(
            worker="secretary-510-pilot-w",
            workspace=str(workspace),
            handle="",
            head="head",
            review_head="review-head",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=review_baseline,
            state="reviewing",
            claimed_at=0.0,
        )

    def test_launching_a_head_drops_a_stale_body_file(self) -> None:
        """A respawned head inherits the ref+round path its half-dead predecessor wrote, and heads
        are told to leave the file behind. Nothing downstream catches a stale body: `_read_body`
        only rejects a missing file, and an empty one posts fine as a green verdict or a done
        report. Clearing the path first turns a skipped write into a loud failure."""
        root = Path(self.tmpdir.name)
        workspace = root / "ws"
        workspace.mkdir()
        stale = root / "secretary-verdict-secretary-510-pilot-3.md"
        stale.write_text("half-written verdict from the head that died", encoding="utf-8")

        with mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_BODY_DIR": str(root)}):
            with mock.patch.object(self.host, "_launch", return_value="term:review"):
                self.host.start_review(self.task, self._record(workspace, 3))

        self.assertFalse(stale.exists(), "respawned reviewer inherited the stale body file")

    def test_prompts_forbid_inline_shell_body_assembly(self) -> None:
        """The 637 failure mode was the body assembled inside the command. Guard the shape that
        actually prevents it: past the `secretary task` verb every argument is a plain token, so
        nothing in the body can reach the shell. The task fixture carries backticks and quotes."""
        for doc in (
            self.host._review_prompt(self.task, "attempt-1", 3),
            self.host._worker_task_doc(self.task, "main", "attempt-1"),
        ):
            for command in self._command_lines(doc):
                arguments = command.split("python3 -m secretary task", 1)[1]
                for banned in ("`", "$", "'", '"', "|", ";", "&", ">", "<", "(", ")"):
                    self.assertNotIn(banned, arguments, command)
                self.assertIn("--body-file /tmp/secretary-", command)
            self.assertIn("(no heredoc, no mktemp, no echo pipeline)", doc)


class WorkerDurabilityTests(unittest.TestCase):
    """verify_worker_result runs against a real git worktree.

    A worker cannot commit a runtime tail the secretary CLI dropped into its workspace, so an
    untracked `secretary-data/` must not read as uncommitted work (secretary-652)."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        for args in (
            ["init", "-q"],
            ["config", "user.email", "worker@example.invalid"],
            ["config", "user.name", "worker"],
        ):
            subprocess.run(["git", "-C", str(self.workspace), *args], check=True, capture_output=True)
        (self.workspace / "code.py").write_text("print(1)\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-qm", "work"], check=True, capture_output=True
        )
        self.host = CommandHostRuntime(FakeCatalog(), self.root / "data", mode="real")  # type: ignore[arg-type]
        self.record = DispatcherRecord(
            worker="secretary-652-w1",
            workspace=str(self.workspace),
            handle="",
            head="head",
            review_head="review-head",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="working",
            claimed_at=0.0,
        )

    def test_clean_commit_passes(self) -> None:
        self.assertIsNone(self.host.verify_worker_result({}, self.record))

    def test_audit_tail_from_the_task_cli_does_not_block_the_card(self) -> None:
        board = self.workspace / "secretary-data" / "board"
        board.mkdir(parents=True)
        (board / "events.ndjson").write_text("{}\n", encoding="utf-8")
        (board / ".audit.lock").write_text("", encoding="utf-8")
        self.assertIsNone(self.host.verify_worker_result({}, self.record))

    def test_real_uncommitted_work_still_blocks(self) -> None:
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")
        with self.assertRaises(HostError):
            self.host.verify_worker_result({}, self.record)

    def test_other_untracked_files_still_block(self) -> None:
        (self.workspace / "scratch.py").write_text("print(3)\n", encoding="utf-8")
        with self.assertRaises(HostError):
            self.host.verify_worker_result({}, self.record)

    def test_tracked_secretary_data_still_blocks(self) -> None:
        nested = self.workspace / "secretary-data-notes.md"
        nested.write_text("prefix collision, not the runtime tail\n", encoding="utf-8")
        with self.assertRaises(HostError):
            self.host.verify_worker_result({}, self.record)


class WaitWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        _clear_env(
            self,
            "SECRETARY_REVIEW_VERDICT_STALL_SECONDS",
            "SECRETARY_WORKER_REPORT_STALL_SECONDS",
        )

    def test_inside_the_ceiling_keeps_waiting(self) -> None:
        outcome = wait_outcome(waiting_since=0.0, now=7199.0, stall_seconds=7200, respawns=0)

        self.assertEqual(outcome, "wait")

    def test_past_the_ceiling_respawns_once_then_escalates(self) -> None:
        self.assertEqual(
            wait_outcome(waiting_since=0.0, now=7201.0, stall_seconds=7200, respawns=0),
            "respawn",
        )
        self.assertEqual(
            wait_outcome(waiting_since=0.0, now=7201.0, stall_seconds=7200, respawns=1),
            "escalate",
        )

    def test_ceiling_comes_from_the_env_at_call_time(self) -> None:
        with mock.patch.dict(
            os.environ, {"SECRETARY_REVIEW_VERDICT_STALL_SECONDS": "120"}
        ):
            self.assertEqual(stall_seconds("review"), 120)
        self.assertEqual(stall_seconds("review"), REVIEW_VERDICT_STALL_DEFAULT)

    def test_unparseable_ceiling_falls_back_to_the_default(self) -> None:
        """A typo in the unit's env must not raise out of module import and keep the dispatcher
        from starting at all."""
        for bogus in ("", "soon", "0", "-5"):
            with mock.patch.dict(
                os.environ, {"SECRETARY_WORKER_REPORT_STALL_SECONDS": bogus}
            ):
                self.assertEqual(stall_seconds("worker"), WORKER_REPORT_STALL_DEFAULT)


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
    def test_explicit_codex_terra_card_uses_terra_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            catalog = object.__new__(InstanceCatalog)
            catalog._heads = canonical_heads(Path(__file__).resolve().parents[1])  # type: ignore[attr-defined]
            task = {"routing": {"head_override": "codex-terra"}}

            head = catalog.worker_head(task)  # type: ignore[attr-defined]
            command = catalog.head_command(  # type: ignore[attr-defined]
                head,
                "TASK.md",
                workspace=str(workspace),
                role="worker",
            )

        self.assertEqual(head, "codex-terra")
        self.assertIn("-m gpt-5.6-terra", command)

    def test_unknown_explicit_head_is_rejected_before_claim(self) -> None:
        catalog = object.__new__(InstanceCatalog)
        catalog._heads = canonical_heads(Path(__file__).resolve().parents[1])  # type: ignore[attr-defined]

        with self.assertRaisesRegex(HostError, "unknown head 'codex-does-not-exist'"):
            catalog.worker_head(  # type: ignore[attr-defined]
                {"routing": {"head_override": "codex-does-not-exist"}}
            )

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

    def test_codex_exec_launch_prompt_replaces_cat_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()

            command = _render_codex_command(
                {"adapter": "codex", "model": "gpt-5.5", "codex_home": "/tmp/codex-home"},
                "TASK.md",
                workspace=str(workspace),
                launch_prompt="read TASK.md first",
            )

        self.assertIn("codex exec", command)
        self.assertNotIn('"$(cat TASK.md)"', command)
        self.assertIn("'read TASK.md first'", command)

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

    def test_binding_resolves_underscore_swimlane_to_hyphen_id(self) -> None:
        catalog = object.__new__(InstanceCatalog)
        catalog.bindings = {  # type: ignore[attr-defined]
            "codegen-orchestrator": {
                "id": "codegen-orchestrator",
                "repo": "/home/dev/projects/codegen_orchestrator",
                "enabled": True,
            }
        }

        binding = catalog.binding("codegen_orchestrator")  # type: ignore[attr-defined]
        self.assertEqual(binding["id"], "codegen-orchestrator")
        self.assertIs(binding, catalog.binding("codegen-orchestrator"))  # type: ignore[attr-defined]

        with self.assertRaises(HostError) as ctx:
            catalog.binding("missing_project")  # type: ignore[attr-defined]
        self.assertIn("not enabled", str(ctx.exception))

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

    def test_launch_prompt_is_short_pointer_and_full_spec_stays_in_task_doc(self) -> None:
        spec = "Implement the frobnicator and wire it into the widget renderer."
        with tempfile.TemporaryDirectory() as tmp:
            host = GitBranchHost(Path(tmp))
            task = {
                "ref": "secretary-510-pilot",
                "project": "secretary",
                "title": "Pilot",
                "description": spec,
                "workspace": {"base_branch": "main"},
            }

            result = host.prepare_worker(task, "secretary-510-pilot-pilot", "codex")
            task_doc = (Path(result["workspace"]) / "TASK.md").read_text(encoding="utf-8")

        delivered = host.launch_prompts[-1]
        # The head is launched with a short pointer, not the task body: the full spec is only
        # ever handed over through TASK.md, never duplicated into the delivered launch prompt.
        self.assertIsNotNone(delivered)
        self.assertIn("TASK.md", delivered)
        self.assertNotIn(spec, delivered)
        self.assertLess(len(delivered), len(task_doc))
        # TASK.md keeps the full spec and the exact per-round report command (Bug-2 fix).
        self.assertIn(spec, task_doc)
        self.assertIn("--kind done", task_doc)
        self.assertIn("--request-id ", task_doc)

    def test_report_request_id_is_distinct_per_review_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = GitBranchHost(Path(tmp))
            task = {
                "ref": "secretary-510-pilot",
                "project": "secretary",
                "description": "body",
                "workspace": {"base_branch": "main"},
            }
            first = host._worker_task_doc(task, "main", "attempt-1", 0)
            rework = host._worker_task_doc(task, "main", "attempt-1", 2)

        def rid(text: str) -> str:
            start = text.index("--request-id ") + len("--request-id ")
            return text[start:].split()[0]

        # Same attempt, different review round: the report request-id must differ, or the
        # rework done-report is idempotently deduped against the pre-review one and the
        # dispatcher waits for a report that never lands.
        self.assertNotEqual(rid(first), rid(rework))
        self.assertTrue(rid(first).endswith("-0"))
        self.assertTrue(rid(rework).endswith("-2"))

    def test_rework_task_doc_delivers_latest_review_red_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = GitBranchHost(Path(tmp))
            base_task = {
                "ref": "secretary-510-pilot",
                "project": "secretary",
                "description": "body",
                "workspace": {"base_branch": "main"},
            }
            # No review yet: no reviewer section.
            self.assertNotIn("Reviewer verdict to address", host._worker_task_doc(base_task, "main", "a", 0))
            # After a red review, the rework doc must carry the latest findings verbatim so the
            # worker does not rework blind and re-report the same commit.
            reviewed = {
                **base_task,
                "comments": [
                    {"marker": "review:red", "body": "[review:red]\nstale finding"},
                    {"marker": "report:done", "body": "[report:done]\ndone"},
                    {"marker": "review:red", "body": "[review:red]\nP1: use a time ceiling, not the terminal title"},
                ],
            }
            doc = host._worker_task_doc(reviewed, "main", "a", 2)
        self.assertIn("Reviewer verdict to address", doc)
        self.assertIn("P1: use a time ceiling, not the terminal title", doc)
        self.assertNotIn("stale finding", doc)  # only the latest red

    def test_review_verdict_request_id_is_distinct_per_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = GitBranchHost(Path(tmp))
            task = {
                "ref": "secretary-510-pilot",
                "project": "secretary",
                "description": "body",
                "workspace": {"base_branch": "main"},
            }
            first = host._review_prompt(task, "attempt-1", 3)
            later = host._review_prompt(task, "attempt-1", 7)

        def rid(text: str, kind: str) -> str:
            start = text.index(f"--kind {kind} --request-id ") + len(f"--kind {kind} --request-id ")
            return text[start:].split()[0]

        # Same attempt, different review round: the verdict request-id must differ, or a second
        # round's verdict is idempotently deduped against the first and never registers, leaving
        # the dispatcher waiting for a verdict forever.
        self.assertNotEqual(rid(first, "red"), rid(later, "red"))
        self.assertNotEqual(rid(first, "green"), rid(later, "green"))

    def test_complete_green_publishes_branch_and_fast_forwards_checkout(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            host = _RecordingMergeHost(Path(tmp))
            record = SimpleNamespace(workspace=str(Path(tmp) / "ws"))
            host.complete_green({"ref": "secretary-510-pilot", "project": "secretary"}, record)
        cmds = [" ".join(run) for run in host.runs]
        self.assertTrue(any("push origin pipeline/secretary-510-pilot:main" in c for c in cmds), cmds)
        self.assertTrue(any(c.endswith("git -C /home/dev/secretary merge --ff-only origin/main") for c in cmds), cmds)

    def test_complete_green_respects_automerge_off_kill_switch(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            host = _RecordingMergeHost(Path(tmp))
            record = SimpleNamespace(workspace=str(Path(tmp) / "ws"))
            with mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_AUTOMERGE": "off"}):
                host.complete_green({"ref": "secretary-510-pilot", "project": "secretary"}, record)
        self.assertEqual(host.runs, [])

    def test_complete_green_merges_github_project_through_pr(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            host = _RecordingMergeHost(Path(tmp), {"validation": {"ci": "github"}})
            record = SimpleNamespace(workspace=str(Path(tmp) / "ws"))
            host.complete_green({"ref": "secretary-510-pilot", "project": "codegen_orchestrator"}, record)
        cmds = [" ".join(run) for run in host.runs]
        self.assertTrue(any("gh pr merge pipeline/secretary-510-pilot --merge" in c for c in cmds), cmds)
        # never a local force-land of the branch onto main for a PR-merged project
        self.assertFalse(any("push origin pipeline/secretary-510-pilot:main" in c for c in cmds), cmds)
        self.assertTrue(any(c.endswith("merge --ff-only origin/main") for c in cmds), cmds)

    def test_instance_repo_merge_preserves_local_checkpoint_commit(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, instance, workspace = _instance_repo_fixture(root, "secretary-669")
            checkpoint = _commit_file(instance, "state/board/cards.ndjson", "checkpoint\n", "checkpoint")
            feature = _commit_file(workspace, "result.txt", "green result\n", "feature")
            host = CommandHostRuntime(
                _InstanceRepoCatalog(instance),
                root,
                mode="real",
            )

            host.complete_green(
                {"ref": "secretary-669", "project": "secretary_instance"},
                SimpleNamespace(workspace=str(workspace)),
            )

            local_head = git(instance, "rev-parse", "HEAD")
            self.assertTrue(_is_ancestor(instance, checkpoint, local_head))
            self.assertTrue(_is_ancestor(instance, feature, local_head))
            self.assertEqual(git(instance, "show", "HEAD:state/board/cards.ndjson"), "checkpoint")
            self.assertEqual(git(instance, "show", "HEAD:result.txt"), "green result")
            # The merge commit is local until the checkpoint pusher's next ff-only window.
            self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), feature)

            state = CheckpointPusher(instance).push(
                {
                    "status": "diverged",
                    "remote_diverged": True,
                    "failures": 2,
                    "attempted_epoch": time.time(),
                    "attempted_at": "2026-07-20T21:00:00Z",
                }
            )

            self.assertEqual(state["status"], "pushed")
            self.assertFalse(state["remote_diverged"])
            self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), local_head)
            self.assertEqual(git(remote, "show", "HEAD:state/board/cards.ndjson"), "checkpoint")
            self.assertEqual(git(remote, "show", "HEAD:result.txt"), "green result")

    def test_instance_repo_merge_recovers_after_remote_publish_before_local_checkout(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, instance, workspace = _instance_repo_fixture(root, "secretary-669")
            checkpoint = _commit_file(instance, "state/runs/runs.ndjson", "checkpoint\n", "checkpoint")
            feature = _commit_file(workspace, "result.txt", "green result\n", "feature")
            git(workspace, "push", "origin", "pipeline/secretary-669:main")
            host = CommandHostRuntime(
                _InstanceRepoCatalog(instance),
                root,
                mode="real",
            )

            host.complete_green(
                {"ref": "secretary-669", "project": "secretary_instance"},
                SimpleNamespace(workspace=str(workspace)),
            )

            local_head = git(instance, "rev-parse", "HEAD")
            self.assertTrue(_is_ancestor(instance, checkpoint, local_head))
            self.assertTrue(_is_ancestor(instance, feature, local_head))
            self.assertEqual(git(instance, "show", "HEAD:state/runs/runs.ndjson"), "checkpoint")
            self.assertEqual(git(instance, "show", "HEAD:result.txt"), "green result")

    def test_instance_repo_merge_preserves_checkpoint_already_pushed_to_remote(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, instance, workspace = _instance_repo_fixture(root, "secretary-669")
            checkpoint = _commit_file(instance, "state/runs/runs.ndjson", "checkpoint\n", "checkpoint")
            git(instance, "push", "--quiet", "origin", "main")
            feature = _commit_file(workspace, "result.txt", "green result\n", "feature")
            host = CommandHostRuntime(
                _InstanceRepoCatalog(instance),
                root,
                mode="real",
            )

            host.complete_green(
                {"ref": "secretary-669", "project": "secretary_instance"},
                SimpleNamespace(workspace=str(workspace)),
            )

            remote_head = git(remote, "rev-parse", "refs/heads/main")
            local_head = git(instance, "rev-parse", "HEAD")
            self.assertEqual(local_head, remote_head)
            self.assertTrue(_is_ancestor(remote, checkpoint, remote_head))
            self.assertTrue(_is_ancestor(remote, feature, remote_head))
            self.assertEqual(git(remote, "show", "HEAD:state/runs/runs.ndjson"), "checkpoint")
            self.assertEqual(git(remote, "show", "HEAD:result.txt"), "green result")

    def test_instance_publish_recovery_rejects_linear_unreviewed_descendant(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, instance, workspace = _instance_repo_fixture(root, "secretary-669")
            reviewed = _commit_file(workspace, "result.txt", "green result\n", "feature")
            current = _commit_file(workspace, "unreviewed.txt", "not reviewed\n", "unreviewed")
            git(workspace, "push", "--quiet", "origin", "pipeline/secretary-669:main")
            host = CommandHostRuntime(
                _InstanceRepoCatalog(instance),
                root,
                mode="real",
            )

            recovered = host.is_instance_publish_recovery(
                {"ref": "secretary-669", "project": "secretary_instance"},
                SimpleNamespace(workspace=str(workspace)),
                reviewed,
                current,
            )

            self.assertFalse(recovered)

    def test_instance_publish_recovery_rejects_foreign_merge_parent(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, instance, workspace = _instance_repo_fixture(root, "secretary-669")
            foreign = root / "foreign"
            git(root, "clone", "--quiet", str(remote), str(foreign))
            _configure_git_user(foreign)
            foreign_commit = _commit_file(foreign, "foreign.txt", "not reviewed\n", "foreign")
            reviewed = _commit_file(workspace, "result.txt", "green result\n", "feature")
            git(workspace, "fetch", "--quiet", str(foreign), "HEAD")
            git(workspace, "merge", "--quiet", "--no-edit", "FETCH_HEAD")
            current = git(workspace, "rev-parse", "HEAD")
            git(workspace, "push", "--quiet", "origin", "pipeline/secretary-669:main")
            host = CommandHostRuntime(
                _InstanceRepoCatalog(instance),
                root,
                mode="real",
            )

            recovered = host.is_instance_publish_recovery(
                {"ref": "secretary-669", "project": "secretary_instance"},
                SimpleNamespace(workspace=str(workspace)),
                reviewed,
                current,
            )

            self.assertFalse(recovered)
            self.assertFalse(_is_ancestor(instance, foreign_commit, git(instance, "rev-parse", "HEAD")))
            self.assertEqual(git(remote, "show", "HEAD:foreign.txt"), "not reviewed")

    def test_instance_repo_publish_rejects_foreign_remote_history(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, instance, workspace = _instance_repo_fixture(root, "secretary-669")
            foreign = root / "foreign"
            git(root, "clone", "--quiet", str(remote), str(foreign))
            _configure_git_user(foreign)
            foreign_commit = _commit_file(foreign, "foreign.txt", "not reviewed\n", "foreign")
            git(foreign, "push", "--quiet", "origin", "main")
            feature = _commit_file(workspace, "result.txt", "green result\n", "feature")
            host = CommandHostRuntime(
                _InstanceRepoCatalog(instance),
                root,
                mode="real",
            )

            with self.assertRaisesRegex(HostError, "unreviewed remote history"):
                host.complete_green(
                    {"ref": "secretary-669", "project": "secretary_instance"},
                    SimpleNamespace(workspace=str(workspace)),
                )

            self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), foreign_commit)
            self.assertFalse(_is_ancestor(remote, feature, foreign_commit))
            self.assertFalse((instance / "foreign.txt").exists())
            self.assertFalse((instance / "result.txt").exists())

    def test_finish_green_recovers_after_worker_side_checkpoint_merge_was_published(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, instance, workspace = _instance_repo_fixture(root, "secretary-510-pilot")
            checkpoint = _commit_file(instance, "state/runs/runs.ndjson", "checkpoint\n", "checkpoint")
            git(instance, "push", "--quiet", "origin", "main")
            feature = _commit_file(workspace, "result.txt", "green result\n", "feature")
            first_host = _CrashAfterMergePushHost(_InstanceRepoCatalog(instance), root, mode="real")  # type: ignore[arg-type]
            with self.assertRaisesRegex(HostError, "simulated crash after merge push"):
                first_host.complete_green(
                    {"ref": "secretary-510-pilot", "project": "secretary"},
                    SimpleNamespace(workspace=str(workspace)),
                )
            published = git(remote, "rev-parse", "refs/heads/main")
            self.assertTrue(_is_ancestor(workspace, checkpoint, published))
            self.assertTrue(_is_ancestor(workspace, feature, published))

            board = FakeKanboard()
            board.tasks[0]["column_id"] = 4
            data_dir = root / "data"
            writer = TaskWriter(board, data_dir=data_dir, workspace=data_dir)  # type: ignore[arg-type]
            runtime = DispatcherRuntime(
                TaskReader(board),  # type: ignore[arg-type]
                writer,
                TaskAudit(data_dir),
                CutoverState(data_dir),
                _InstanceRepoCatalog(instance),  # type: ignore[arg-type]
                CommandHostRuntime(_InstanceRepoCatalog(instance), root, mode="real"),  # type: ignore[arg-type]
                owner="secretary-pilot",
                legacy_pause=FakeLegacyPause(),  # type: ignore[arg-type]
            )
            record = DispatcherRecord(
                worker="secretary-510-pilot-pilot",
                workspace=str(workspace),
                handle="term:secretary-510-pilot-pilot",
                head="codex",
                review_head="codex-reviewer",
                attempt_id="attempt-1",
                comment_baseline=0,
                review_baseline=0,
                state="reviewing",
                claimed_at=time.time(),
                gate_state="green",
                review_commit=feature,
            )
            records = {"secretary-510-pilot": record}

            result = runtime._finish_green(
                TaskReader(board).show("secretary-510-pilot"),  # type: ignore[arg-type]
                record,
                records,
                {"version": 1, "phase": "cutover_committed"},
                "attempt-1",
            )

            self.assertEqual(result["to"], "done")
            self.assertEqual(TaskReader(board).show("secretary-510-pilot")["state"], "done")  # type: ignore[arg-type]
            self.assertEqual(records, {})
            local_head = git(instance, "rev-parse", "HEAD")
            self.assertEqual(local_head, published)
            self.assertTrue(_is_ancestor(instance, checkpoint, local_head))
            self.assertTrue(_is_ancestor(instance, feature, local_head))
            state = CheckpointPusher(instance, interval_seconds=0).push(
                {"status": "diverged", "remote_diverged": True}
            )
            self.assertEqual(state["status"], "unchanged")
            self.assertFalse(state["remote_diverged"])

    def test_instance_repo_merge_uses_fallback_identity_without_global_git_identity(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty_global = root / "empty-gitconfig"
            empty_global.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ):
                os.environ["GIT_CONFIG_GLOBAL"] = str(empty_global)
                os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
                for key in (
                    "EMAIL",
                    "GIT_AUTHOR_EMAIL",
                    "GIT_AUTHOR_NAME",
                    "GIT_COMMITTER_EMAIL",
                    "GIT_COMMITTER_NAME",
                ):
                    os.environ.pop(key, None)
                _, instance, workspace = _instance_repo_fixture(root, "secretary-669")
                git(instance, "config", "--unset", "user.name")
                git(instance, "config", "--unset", "user.email")

                (instance / "state" / "board" / "cards.ndjson").write_text(
                    "checkpoint\n",
                    encoding="utf-8",
                )
                checkpoint_result = CheckpointWriter(root / "data", instance)._commit_locked(
                    board_cards=1,
                    run_records=0,
                )
                self.assertEqual(checkpoint_result.status, "committed")
                checkpoint = checkpoint_result.commit

                feature = _commit_file(workspace, "result.txt", "green result\n", "feature")
                git(workspace, "push", "origin", "pipeline/secretary-669:main")
                host = CommandHostRuntime(
                    _InstanceRepoCatalog(instance),
                    root,
                    mode="real",
                )

                host.complete_green(
                    {"ref": "secretary-669", "project": "secretary_instance"},
                    SimpleNamespace(workspace=str(workspace)),
                )

                local_head = git(instance, "rev-parse", "HEAD")
                self.assertTrue(_is_ancestor(instance, checkpoint, local_head))
                self.assertTrue(_is_ancestor(instance, feature, local_head))
                self.assertEqual(git(instance, "show", "HEAD:state/board/cards.ndjson"), "checkpoint")
                self.assertEqual(git(instance, "show", "HEAD:result.txt"), "green result")

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

class _RecordingMergeHost(CommandHostRuntime):
    def __init__(self, root: Path, adapter: dict | None = None) -> None:
        super().__init__(FakeCatalog(adapter), root, mode="real")  # type: ignore[arg-type]
        self.runs: list[list[str]] = []

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        self.runs.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")


class GitBranchHost(CommandHostRuntime):
    def __init__(self, root: Path) -> None:
        super().__init__(FakeCatalog(), root, mode="real")  # type: ignore[arg-type]
        self.root = root
        self.launched: list[tuple[str, str]] = []
        self.launch_prompts: list[str | None] = []

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
        launch_prompt: str | None = None,
    ) -> str:
        self.launched.append((head, prompt_file))
        self.launch_prompts.append(launch_prompt)
        return f"test:{head}"


class WorkspaceResumeTests(unittest.TestCase):
    def test_prepare_worker_reuses_registered_branch_without_touching_commit_or_wip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            workspace_root = root / "workspaces"
            worker = "secretary-510-pilot-pilot"
            workspace = workspace_root / "secretary" / worker
            repo.mkdir()
            git(repo, "init", "--initial-branch", "main")
            _configure_git_user(repo)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "base")
            workspace.parent.mkdir(parents=True)
            git(repo, "worktree", "add", "-b", _legacy_worker_branch("secretary-510-pilot"), str(workspace))
            _configure_git_user(workspace)
            commit = _commit_file(workspace, "kept.py", "commit = True\n", "preserved commit")
            (workspace / "wip.py").write_text("uncommitted = True\n", encoding="utf-8")
            git(workspace, "add", "wip.py")
            git(workspace, "config", "core.excludesFile", str(root / "exclude"))
            (root / "exclude").write_text("TASK.md\n", encoding="utf-8")

            class Catalog(FakeCatalog):
                def binding(self, project: str) -> dict:
                    return {"repo": str(repo), "default_branch": "main"}

            host = GitBranchHost(root)
            host.catalog = Catalog()  # type: ignore[assignment]
            task = {
                "ref": "secretary-510-pilot",
                "project": "secretary",
                "description": "updated task description",
                "comments": [{"marker": "review:red", "body": "[review:red]\nlatest finding"}],
                "workspace": {"base_branch": "main"},
            }
            with mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(workspace_root)}):
                result = host.prepare_worker(task, worker, "codex", attempt_id="attempt-retry")

            self.assertEqual(result["workspace"], str(workspace))
            self.assertEqual(git(workspace, "rev-parse", "HEAD"), commit)
            self.assertEqual(git(workspace, "diff", "--cached", "--name-only"), "wip.py")
            task_doc = (workspace / "TASK.md").read_text(encoding="utf-8")
            self.assertIn("updated task description", task_doc)
            self.assertIn("latest finding", task_doc)
            self.assertIn("attempt-retry", task_doc)


class _InstanceRepoCatalog:
    def __init__(self, instance_dir: Path) -> None:
        self.instance_dir = instance_dir

    def binding(self, project: str) -> dict:
        return {"repo": str(self.instance_dir), "default_branch": "main"}

    def default_branch(self, project: str, override: str | None) -> str:
        return override or "main"

    def adapter(self, project: str) -> dict:
        return {}


class _CrashAfterMergePushHost(CommandHostRuntime):
    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        completed = super()._run(args, label, cwd=cwd)
        if label == "merge push":
            raise HostError("simulated crash after merge push")
        return completed


def _instance_repo_fixture(root: Path, ref: str) -> tuple[Path, Path, Path]:
    remote = root / "remote.git"
    instance = root / "secretary-instance"
    workspace = root / "workspace"
    git(root, "init", "--quiet", "--bare", "--initial-branch", "main", str(remote))
    git(root, "clone", "--quiet", str(remote), str(instance))
    _configure_git_user(instance)
    _commit_file(instance, "README.md", "seed\n", "seed")
    _commit_file(instance, "state/board/cards.ndjson", "", "seed board checkpoint")
    _commit_file(instance, "state/runs/runs.ndjson", "", "seed runs checkpoint")
    git(instance, "push", "--quiet", "origin", "main")
    git(root, "clone", "--quiet", str(remote), str(workspace))
    _configure_git_user(workspace)
    git(workspace, "checkout", "--quiet", "-b", _legacy_worker_branch(ref))
    return remote, instance, workspace


def _configure_git_user(repo: Path) -> None:
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")


def _commit_file(repo: Path, relative: str, text: str, message: str) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", relative)
    git(repo, "commit", "--quiet", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return result.returncode == 0


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


class GateCatalog:
    def __init__(self, adapter: dict) -> None:
        self._adapter = adapter

    def adapter(self, project: str) -> dict:
        return self._adapter

    def default_branch(self, project: str, override: str | None) -> str:
        return override or "main"

    def binding(self, project: str) -> dict:
        return {"repo": f"/home/dev/{project}"}


class GateHost(CommandHostRuntime):
    def __init__(self, root: Path, adapter: dict) -> None:
        super().__init__(GateCatalog(adapter), root, mode="real")  # type: ignore[arg-type]


class GithubGateHost(CommandHostRuntime):
    """Runs the real gate over a real git workspace but fakes every `gh` shell-out: repo view,
    the PR list/create idempotency probes, and the check-runs/status CI poll."""

    def __init__(self, root: Path, adapter: dict, *, pr_open: bool, check_runs: list, statuses: list | None = None) -> None:
        super().__init__(GateCatalog(adapter), root, mode="real")  # type: ignore[arg-type]
        self._pr_open = pr_open
        self._check_runs = check_runs
        self._statuses = statuses or []
        self.gh: list[list[str]] = []

    def _fake_gh(self, args):
        self.gh.append(list(args))

        def done(out=""):
            return subprocess.CompletedProcess(args, 0, out, "")

        if args[1:3] == ["repo", "view"]:
            return done("vladmesh/sample\n")
        if args[1:3] == ["pr", "list"]:
            return done("42\n" if self._pr_open else "\n")
        if args[1:3] == ["pr", "create"]:
            self._pr_open = True
            return done("https://github.com/vladmesh/sample/pull/42\n")
        if args[1] == "api":
            path = args[2]
            if path.endswith("/check-runs"):
                return done(json.dumps(self._check_runs))
            if path.endswith("/status"):
                return done(json.dumps(self._statuses))
        return done("[]")

    def run_capture(self, args, label, *, cwd=None):  # type: ignore[override]
        if args[:1] == ["gh"]:
            return self._fake_gh(args)
        return super().run_capture(args, label, cwd=cwd)

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        if args[:1] == ["gh"]:
            return self._fake_gh(args)
        return super()._run(args, label, cwd=cwd)


def _build_gated_workspace(root: Path, base: str, branch: str) -> Path:
    """A worker workspace on `branch` with an `origin` remote carrying `base`, one work commit
    ahead — the shape gate_check's base-freshness recovery and local gate run against."""
    bare = root / "origin.git"
    bare.mkdir()
    git(bare, "init", "--bare", "--initial-branch", base)
    ws = root / "ws"
    ws.mkdir()
    git(ws, "init", "--initial-branch", base)
    git(ws, "config", "user.name", "Test User")
    git(ws, "config", "user.email", "test@example.invalid")
    (ws / "README.md").write_text("seed\n", encoding="utf-8")
    git(ws, "add", "README.md")
    git(ws, "commit", "-m", "seed")
    git(ws, "remote", "add", "origin", str(bare))
    git(ws, "push", "origin", base)
    git(ws, "checkout", "-b", branch)
    (ws / "work.txt").write_text("work\n", encoding="utf-8")
    git(ws, "add", "work.txt")
    git(ws, "commit", "-m", "work")
    return ws


class DispatcherGateTests(unittest.TestCase):
    def _record(self, workspace: Path):
        from types import SimpleNamespace

        return SimpleNamespace(workspace=str(workspace))

    def _task(self) -> dict:
        return {"ref": "secretary-633", "project": "secretary", "workspace": {"base_branch": "main"}}

    def test_ci_none_skips_gate_without_touching_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = GateHost(Path(tmp), {"validation": {"ci": "none", "missing": ["tests"]}})
            record = self._record(Path(tmp) / "absent")
            result = host.gate_check(self._task(), record)
        self.assertEqual(result.status, "green")

    def test_local_gate_green_on_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GateHost(Path(tmp), {"validation": {"ci": "local", "command": "test -f work.txt"}})
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "green")

    def test_local_gate_red_on_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GateHost(Path(tmp), {"validation": {"ci": "local", "command": "echo boom >&2; exit 1"}})
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "red")
        self.assertIn("boom", result.log)

    def test_base_freshness_recovers_behind_branch_before_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            # advance origin/main ahead of the branch on a different file (no conflict)
            git(ws, "checkout", "main")
            (ws / "base.txt").write_text("base\n", encoding="utf-8")
            git(ws, "add", "base.txt")
            git(ws, "commit", "-m", "base moves")
            git(ws, "push", "origin", "main")
            git(ws, "checkout", "pipeline/secretary-633")
            host = GateHost(Path(tmp), {"validation": {"ci": "local", "command": "test -f base.txt"}})
            result = host.gate_check(self._task(), self._record(ws))
            # recovery merged origin/main in, so the base file is present and the tree is a FF of main
            self.assertEqual(result.status, "green")
            self.assertEqual(git(ws, "rev-list", "--count", "HEAD..origin/main"), "0")

    def test_base_freshness_conflict_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            git(ws, "checkout", "pipeline/secretary-633")
            (ws / "README.md").write_text("branch edit\n", encoding="utf-8")
            git(ws, "add", "README.md")
            git(ws, "commit", "-m", "branch edits readme")
            git(ws, "checkout", "main")
            (ws / "README.md").write_text("base edit\n", encoding="utf-8")
            git(ws, "add", "README.md")
            git(ws, "commit", "-m", "base edits readme")
            git(ws, "push", "origin", "main")
            git(ws, "checkout", "pipeline/secretary-633")
            host = GateHost(Path(tmp), {"validation": {"ci": "local", "command": "true"}})
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "red")
        self.assertIn("fell behind base", result.summary)

    def _github_adapter(self) -> dict:
        return {"validation": {"ci": "github"}}

    def _pr_calls(self, host: "GithubGateHost", verb: str) -> list:
        return [c for c in host.gh if c[1:3] == ["pr", verb]]

    def test_github_gate_opens_pr_when_absent_then_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._github_adapter(),
                pr_open=False, check_runs=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "green")
        self.assertEqual(len(self._pr_calls(host, "create")), 1)
        create = self._pr_calls(host, "create")[0]
        self.assertIn("--base", create)
        self.assertIn("main", create)
        self.assertIn("pipeline/secretary-633", create)

    def test_github_gate_reuses_existing_open_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._github_adapter(),
                pr_open=True, check_runs=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "green")
        self.assertEqual(self._pr_calls(host, "create"), [], "an open PR must not be duplicated")

    def test_github_gate_red_on_failed_pr_ci(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._github_adapter(),
                pr_open=True, check_runs=[{"status": "COMPLETED", "conclusion": "FAILURE", "name": "tests"}],
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "red")
        self.assertIn("tests", result.summary)

    def test_github_gate_pending_while_pr_ci_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._github_adapter(),
                pr_open=True, check_runs=[{"status": "IN_PROGRESS"}],
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "pending")

    def test_github_rollup_classification(self) -> None:
        from secretary.dispatcher_gate import _rollup

        self.assertEqual(_rollup([])[0], "NONE")
        self.assertEqual(_rollup([{"status": "COMPLETED", "conclusion": "SUCCESS"}])[0], "SUCCESS")
        self.assertEqual(
            _rollup([
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "IN_PROGRESS"},
            ])[0],
            "PENDING",
        )
        rollup, failed = _rollup([
            {"status": "COMPLETED", "conclusion": "FAILURE", "name": "tests"},
        ])
        self.assertEqual(rollup, "FAILURE")
        self.assertEqual(failed["name"], "tests")
        # a legacy commit status still counts
        self.assertEqual(_rollup([{"state": "success"}])[0], "SUCCESS")
        self.assertEqual(_rollup([{"state": "failure"}])[0], "FAILURE")


class ReviewCatalog(FakeCatalog):
    """FakeCatalog plus the head-launch surface the real bring-up path calls into."""

    def prepare_head_workspace(self, head: str, workspace: str) -> None:
        return None

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        codex_mode: str | None = None,
        launch_prompt: str | None = None,
    ):
        from secretary.dispatcher_launcher import HeadLaunch

        return HeadLaunch(f"run-{role}", prompt_after_start=False)


class RecordingReviewHost(CommandHostRuntime):
    """CommandHostRuntime with the orca CLI and git stubbed, so the reviewer bring-up runs for
    real: anchor pick, split, label, worker freeze, pinned commit."""

    def __init__(
        self,
        root: Path,
        *,
        terminals: list[dict] | None = None,
        fail_ops: set[str] | None = None,
    ) -> None:
        super().__init__(ReviewCatalog(), root, mode="real")  # type: ignore[arg-type]
        self.calls: list[list[str]] = []
        self.fail_ops = fail_ops or set()
        self.terminals = [
            {"handle": "term-worker", "leafId": "leaf-worker", "title": "codex", "connected": True}
        ] if terminals is None else terminals

    def _run_json(self, args: list[str]) -> dict:
        self.calls.append(args)
        op = args[2] if args[:2] == ["orca", "terminal"] else ""
        if op in self.fail_ops:
            raise HostError(f"orca terminal {op} failed")
        if op == "list":
            return {"terminals": self.terminals}
        if op == "split":
            # The new pane joins the worktree's inventory, which is how the caller resolves its
            # leafId afterwards.
            self.terminals.append(
                {"handle": "term-review", "leafId": "leaf-review", "title": None, "connected": True}
            )
            return {"split": {"handle": "term-review", "tabId": "tab-1", "paneRuntimeId": -1}}
        if op == "create":
            return {"terminal": {"handle": "term-created"}}
        return {}

    def _run(self, args: list[str], label: str, *, cwd: Path | None = None):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="deadbeefcafe0000\n", stderr="")

    def ops(self) -> list[str]:
        return [call[2] for call in self.calls if call[:2] == ["orca", "terminal"]]

    def call_for(self, op: str) -> list[str]:
        return next(call for call in self.calls if call[:3] == ["orca", "terminal", op])


class ReviewPaneTests(unittest.TestCase):
    """secretary-651: the reviewer runs in a visible split pane of the worker's own worktree."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        _clear_env(self, "SECRETARY_DISPATCHER_REVIEW_COMMAND")
        os.environ["SECRETARY_DISPATCHER_BODY_DIR"] = str(self.root)
        self.task = {
            "ref": "secretary-651",
            "project": "secretary",
            "description": "spec",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self, handle: str = "term-worker") -> DispatcherRecord:
        return DispatcherRecord(
            worker="secretary-651-w",
            workspace=str(self.workspace),
            handle=handle,
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="review_starting",
            claimed_at=0.0,
        )

    def test_reviewer_is_split_off_the_worker_pane_and_labelled(self) -> None:
        host = RecordingReviewHost(self.root)

        launch = host.start_review(self.task, self._record())

        split = host.call_for("split")
        self.assertEqual(split[split.index("--terminal") + 1], "term-worker")
        self.assertIn("--command", split)
        rename = host.call_for("rename")
        self.assertEqual(rename[rename.index("--terminal") + 1], "term-review")
        self.assertEqual(rename[rename.index("--title") + 1], review_pane_label("secretary-651"))
        self.assertEqual(launch.handle, "term-review")
        self.assertEqual(launch.leaf, "leaf-review")
        self.assertEqual(launch.commit, "deadbeefcafe0000")
        self.assertNotIn("create", host.ops(), "the reviewer must not open its own terminal tab")
        self.assertFalse(
            [call for call in host.calls if "worktree" in call and "create" in call],
            "the reviewer must reuse the worker's worktree, never make its own",
        )

    def test_reviewer_pane_carries_the_reference_and_the_role(self) -> None:
        host = RecordingReviewHost(self.root)

        host.start_review(self.task, self._record())

        label = host.call_for("rename")[host.call_for("rename").index("--title") + 1]
        self.assertIn("secretary-651", label)
        self.assertIn("reviewer", label)

    def test_worker_pane_is_shut_down_once_the_reviewer_is_up(self) -> None:
        """Nothing else stops the worker head from editing the checkout mid-review."""
        host = RecordingReviewHost(self.root)

        host.start_review(self.task, self._record())

        closed = host.call_for("close")
        self.assertEqual(closed[closed.index("--terminal") + 1], "term-worker")
        self.assertLess(host.ops().index("split"), host.ops().index("close"), "split needs a live pane")

    def test_reviewer_falls_back_to_its_own_terminal_without_a_live_pane(self) -> None:
        """A worktree whose panes all died still has to get its card reviewed; a background
        terminal is less visible than a split but better than a card parked forever."""
        host = RecordingReviewHost(self.root, terminals=[])

        launch = host.start_review(self.task, self._record(handle=""))

        self.assertEqual(launch.handle, "term-created")
        self.assertNotIn("split", host.ops())

    def test_dead_worker_pane_is_not_used_as_the_split_anchor(self) -> None:
        host = RecordingReviewHost(
            self.root,
            terminals=[
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": False},
                {"handle": "term-other", "leafId": "leaf-other", "connected": True},
            ],
        )

        host.start_review(self.task, self._record())

        split = host.call_for("split")
        self.assertEqual(split[split.index("--terminal") + 1], "term-other")

    def test_split_failure_raises_and_leaves_the_worker_pane_alone(self) -> None:
        host = RecordingReviewHost(self.root, fail_ops={"split"})

        with self.assertRaises(HostError):
            host.start_review(self.task, self._record())

        self.assertNotIn("close", host.ops(), "a failed reviewer must not kill the worker head")

    def test_label_failure_closes_the_new_pane(self) -> None:
        """Half a bring-up is worse than none: an unlabelled pane is indistinguishable from the
        worker's, and the card would go to Blocked with a live reviewer still running in it."""
        host = RecordingReviewHost(self.root, fail_ops={"rename"})

        with self.assertRaises(HostError):
            host.start_review(self.task, self._record())

        closed = host.call_for("close")
        self.assertEqual(closed[closed.index("--terminal") + 1], "term-review")

    def test_stop_review_closes_only_the_reviewer_pane(self) -> None:
        host = RecordingReviewHost(self.root)
        record = self._record()
        record.review_handle = "term-review"

        host.stop_review(record)

        self.assertEqual(host.ops(), ["close"])
        self.assertEqual(host.call_for("close")[host.call_for("close").index("--terminal") + 1], "term-review")


class ReviewLivenessTests(unittest.TestCase):
    """Which pane counts as "the reviewer" for lifecycle checks."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.task = {"ref": "secretary-651", "project": "secretary", "routing": {}}

    def _host(self, terminals: list[dict]) -> RecordingReviewHost:
        return RecordingReviewHost(self.root, terminals=terminals)

    def _record(self, **fields) -> DispatcherRecord:
        record = DispatcherRecord(
            worker="secretary-651-w",
            workspace=str(self.workspace),
            handle="term-worker",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="reviewing",
            claimed_at=0.0,
        )
        for name, value in fields.items():
            setattr(record, name, value)
        return record

    def test_persisted_handle_survives_the_heads_own_title_rewrite(self) -> None:
        """A codex head overwrites the terminal title with its own OSC sequence seconds after
        launch. A title-only check then reads the live reviewer as gone and splits a second one."""
        host = self._host([
            {"handle": "term-review", "leafId": "leaf-review", "title": "codex", "connected": True},
        ])

        self.assertTrue(host.review_running(self.task, self._record(review_handle="term-review")))

    def test_leaf_identifies_the_pane_when_the_handle_alias_changed(self) -> None:
        """`terminal list` can answer with a different handle alias for the same pty, so the leaf
        is the token that survives it."""
        host = self._host([
            {"handle": "term-alias", "leafId": "leaf-review", "title": "codex", "connected": True},
        ])

        record = self._record(review_handle="term-review", review_leaf="leaf-review")
        self.assertTrue(host.review_running(self.task, record))

    def test_disconnected_reviewer_pane_is_not_running(self) -> None:
        host = self._host([
            {"handle": "term-review", "leafId": "leaf-review", "connected": False},
        ])

        self.assertFalse(host.review_running(self.task, self._record(review_handle="term-review")))

    def test_worker_pane_is_never_mistaken_for_the_reviewer(self) -> None:
        host = self._host([
            {"handle": "term-worker", "leafId": "leaf-worker", "title": "codex", "connected": True},
        ])

        self.assertFalse(host.review_running(self.task, self._record(review_handle="term-review")))

    def test_label_finds_an_orphan_pane_when_no_handle_was_persisted(self) -> None:
        """The tick that split the pane died before writing the handle to state, so the label is
        all that is left to recognise it by — and a duplicate reviewer is the cost of missing it."""
        host = self._host([
            {"handle": "term-review", "leafId": "leaf-review", "title": "secretary-651 reviewer", "connected": True},
        ])

        self.assertTrue(host.review_running(self.task, self._record()))

    def test_label_fallback_still_matches_a_pre_651_reviewer(self) -> None:
        """A card already in review when the dispatcher upgraded must not get a second reviewer."""
        host = self._host([
            {"handle": "term-review", "leafId": "leaf-review", "title": "secretary-651 review", "connected": True},
        ])

        self.assertTrue(host.review_running(self.task, self._record()))
