from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
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
    LaunchedHead,
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
from secretary.dispatcher_helpers import RED_REVIEW_CEILING, red_review_count
from secretary.dispatcher_observer import (
    OBSERVER_HEAD_FALLBACK,
    ObserverRecord,
)
from secretary.dispatcher_launcher import (
    claude_launch_model,
    ensure_claude_workspace_ready,
    ensure_claude_workspace_trusted,
    ensure_codex_workspace_trusted,
    role_launch_env,
    with_pid_heartbeat,
)
from secretary.dispatcher_review import start_review as start_reviewer
from secretary.dispatcher_state import DispatcherRecord, attempt_request_id as _attempt_request_id
from secretary.dispatcher_types import HeadLaunchAborted, ReviewLaunch, review_pane_label
from secretary.head_registry import canonical_heads
from secretary.routing_journal import (
    HeadRun,
    attempts as routing_attempts,
    head_run_from_profile,
)
from secretary.head_health import HeadReadiness
from secretary.sprints import SPRINT_BOARD_NAME
from secretary.dispatcher_watchdog import (
    INITIAL_OUTPUT_STALL_DEFAULT,
    REVIEW_VERDICT_STALL_DEFAULT,
    WORKER_REPORT_STALL_DEFAULT,
    head_process_status,
    pid_file_path,
    stall_seconds,
    wait_outcome,
)
from secretary.dispatcher_worker_lifecycle import WorkerContinuation, WorkerContinuationStage
from secretary.tasks import TaskAudit, TaskError, TaskReader, TaskWriter


class WorkerContinuationStateTests(unittest.TestCase):
    def test_flat_retained_worker_state_is_migrated(self) -> None:
        record = DispatcherRecord.from_json({
            "state": "worker_resuming",
            "worker_retained_at": 10.0,
            "worker_resume_phase": "merge-gate",
            "worker_resume_delivery": "pending",
            "worker_resume_sent_at": 12.0,
        })

        self.assertEqual(
            record.worker_continuation.stage,
            WorkerContinuationStage.DELIVERY_PENDING,
        )
        self.assertEqual(record.worker_continuation.phase, "merge-gate")
        self.assertEqual(record.worker_continuation.retained_at, 10.0)
        self.assertEqual(record.worker_continuation.sent_at, 12.0)

    def test_flat_pre_validate_checkpoint_is_migrated(self) -> None:
        record = DispatcherRecord.from_json({
            "state": "worker_retained",
            "worker_retained_at": 10.0,
        })

        self.assertEqual(
            record.worker_continuation.stage,
            WorkerContinuationStage.VALIDATION_MOVE_PENDING,
        )

    def test_a_park_outlives_the_session_it_was_opened_over(self) -> None:
        """A dropped session ends a plain retention. It does not end a park: the card is still
        waiting for a decision, and a rework decision on it is owed a replacement worker."""
        continuation = WorkerContinuation()
        continuation.begin_retention(10.0)
        continuation.confirm_validation_move()
        continuation.begin_park("review", 4, "parked", "red")
        continuation.confirm_park()

        continuation.drop_session()

        self.assertEqual(continuation.stage, WorkerContinuationStage.ASSESSMENT_PARKED)
        self.assertFalse(continuation.session_held)
        self.assertFalse(continuation.retained)
        self.assertTrue(continuation.parked)

    def test_a_park_is_confirmed_only_from_its_own_pending_stage(self) -> None:
        continuation = WorkerContinuation()

        with self.assertRaises(ValueError):
            continuation.confirm_park()

        continuation.begin_park("review", 2, "parked", "green")
        continuation.confirm_park()
        continuation.confirm_park()  # idempotent: the recovery of a lost checkpoint re-enters it

        self.assertEqual(continuation.stage, WorkerContinuationStage.ASSESSMENT_PARKED)

        continuation.begin_red_transition("review", 2, "rework", "red", "rework")

        self.assertEqual(continuation.stage, WorkerContinuationStage.RED_TRANSITION_PENDING)
        self.assertEqual(continuation.decision, "rework")
        with self.assertRaises(ValueError):
            continuation.begin_park("review", 2, "parked again", "red")

    def test_unknown_nested_stage_is_not_silently_discarded(self) -> None:
        with self.assertRaises(ValueError):
            DispatcherRecord.from_json({
                "worker_continuation": {"stage": "future-stage"},
            })


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
            {"id": 1, "title": "Issues"},
            {"id": 2, "title": "Ready"},
            {"id": 3, "title": "In progress"},
            {"id": 4, "title": "Validate"},
            {"id": 7, "title": "Assessment"},
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
        # The sprint entities live on their own Kanboard board (`Secretary sprints`, project id 8),
        # so a card is never readable as a sprint and an empty sprint board is the default.
        self.sprints: list[dict] = []
        self.now = 1720000000

    def add_sprint(self, reference: str, *, status: str = "open", **metadata: object) -> dict:
        task_id = 100 + len(self.sprints)
        sprint = {
            "id": task_id,
            "reference": reference,
            "title": metadata.get("sprint_goal", "sprint"),
            "description": "",
            "column_id": 1,
            "position": len(self.sprints) + 1,
            "date_creation": 1720000000,
            "date_modification": 1720000000,
        }
        self.sprints.append(sprint)
        self.metadata[task_id] = {
            "sprint_goal": "ship the thing",
            "sprint_definition_of_done": "the thing ships",
            "sprint_repositories": '["secretary"]',
            "sprint_status": status,
            "sprint_current_task": "",
            **{key: str(value) for key, value in metadata.items()},
        }
        self.comments.setdefault(task_id, [])
        return sprint

    def call(self, method: str, **params: object) -> object:
        self.calls.append((method, params))
        if method == "getProjectByName":
            return {"id": 8} if params.get("name") == SPRINT_BOARD_NAME else {"id": 7}
        if method == "getColumns":
            return self.columns
        if method == "getActiveSwimlanes":
            return [{"id": 4, "name": "Secretary"}]
        if method == "getAllTasks":
            status = params.get("status_id")
            if status not in {0, 1}:
                return []
            pool = self.sprints if int(params.get("project_id") or 0) == 8 else self.tasks
            return [
                task for task in pool
                if (int(task.get("is_active", task.get("status", 1)) or 0) != 0) == (status == 1)
            ]
        if method == "getTaskByReference":
            pool = self.sprints if int(params.get("project_id") or 0) == 8 else self.tasks
            return next((task for task in pool if task["reference"] == params["reference"]), None)
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
        # A trimmed stand-in for heads.yaml: enough profiles to tell two families apart in the
        # routing journal, including one that pins no model at all.
        self.profiles = {
            "codex": {"adapter": "codex", "model": "gpt-5.6-terra", "effort": "default", "resource": "openai-sub"},
            "codex-reviewer": {
                "adapter": "codex", "model": "gpt-5.6-terra", "effort": "extra", "resource": "openai-sub",
            },
            "claude-opus": {"adapter": "claude", "model": "opus", "resource": "claude-sub"},
            "claude-default": {"adapter": "claude", "resource": "claude-sub"},
        }
        self.resources = {
            "openai-sub": {"account": "openai-subscription"},
            "claude-sub": {"account": "claude-subscription"},
        }
        self.profiles["codex-observer"] = {
            "adapter": "codex", "model": "gpt-5.6-terra", "effort": "extra",
            "resource": "openai-sub", "codex_mode": "tui",
        }
        # Mutable, like the role_defaults block of heads.yaml: an operator can re-point a role
        # while cards are in flight.
        self.role_defaults = {
            "new_card": "codex", "reviewer": "codex-reviewer", "observer": "codex-observer",
        }

    def default_branch(self, project: str, override: str | None) -> str:
        # Same precedence as InstanceCatalog: card override, then the binding, then "main".
        return override or self.binding(project).get("default_branch") or "main"

    def adapter(self, project: str) -> dict:
        return self._adapter

    def worker_head(self, task: dict) -> str:
        # Routing overrides resolve ahead of the role default, as in InstanceCatalog: the resolved
        # head is written to the board at claim and re-resolved on adoption, so a fake that always
        # answers "codex" would hide an override that never propagates.
        return str(task.get("routing", {}).get("head_override") or self.role_defaults["new_card"])

    def review_head(self, task: dict) -> str:
        return str(
            task.get("routing", {}).get("review_head_override") or self.role_defaults["reviewer"]
        )

    def claimed_worker_head(self, task: dict) -> str:
        # Same rule as InstanceCatalog: the head the claim wrote onto the card wins over whatever
        # the override and the role default say now, and a claimed head that has left the registry
        # stops the bring-up instead of falling back to the current default.
        return self._claimed_head(task, "resolved_worker_head", self.worker_head)

    def claimed_review_head(self, task: dict) -> str:
        return self._claimed_head(task, "resolved_review_head", self.review_head)

    def _claimed_head(self, task: dict, key: str, current) -> str:
        claimed = task.get("routing", {}).get(key)
        if not claimed:
            return current(task)
        head = str(claimed)
        if head not in self.profiles:
            raise HostError(f"head {head!r} recorded at claim is unavailable")
        return head

    def head_run(self, task: dict, *, role: str, head: str = "", workspace: str = "") -> HeadRun:
        """Mirror InstanceCatalog.head_run over a four-profile registry: `codex` for the worker,
        `codex-reviewer` for the reviewer, `claude-opus` as the other family and `claude-default` as
        the profile that pins no model. Same rule as the real catalog: the head comes from the
        bring-up, its configuration from the registry as it reads right now."""
        routing = task.get("routing") or {}
        if role == "worker":
            override = routing.get("head_override")
            asked = str(override or self.role_defaults["new_card"])
            codex_mode = str(routing.get("codex_launch_mode") or "")
        else:
            override = routing.get("review_head_override")
            asked = str(override or self.role_defaults["reviewer"])
            codex_mode = ""
        launched = str(head) if head else asked
        profile = self.profiles.get(launched, {"adapter": "codex", "resource": "openai-sub"})
        model: str | None = None
        model_source = ""
        if str(profile.get("adapter") or "") == "claude":
            # Same as InstanceCatalog: a claude profile that pins no model leaves the choice to the
            # CLI, and the snapshot names the model that CLI resolves at this bring-up.
            model, model_source = claude_launch_model(
                profile, workspace=workspace, env=role_launch_env(role)
            )
        return head_run_from_profile(
            role=role,
            head=launched,
            head_source=(
                "record" if launched != asked else ("card" if override else "role_default")
            ),
            profile=profile,
            resources=self.resources,
            codex_mode=codex_mode,
            model=model,
            model_source=model_source,
        )

    def observer_head(self) -> str:
        # Same rule as InstanceCatalog: the observer's own role_defaults key, with a named fallback
        # profile rather than the worker's default.
        head = str(self.role_defaults.get("observer") or OBSERVER_HEAD_FALLBACK)
        if head not in self.profiles:
            raise HostError(f"unknown head {head!r}")
        return head

    def observer_profile(self, head: str) -> dict:
        # Same rule as InstanceCatalog: one lookup for a head a sprint declared, no fallback. A
        # profile that has left the registry makes the sprint unrunnable, and the fence says so.
        if head not in self.profiles:
            raise HostError(f"unknown head {head!r}")
        return self.profiles[head]

    def observer_run(self, head: str, *, workspace: str = "") -> HeadRun:
        profile = self.profiles.get(head, {"adapter": "codex", "resource": "openai-sub"})
        return head_run_from_profile(
            role="observer",
            head=head,
            head_source="role_default",
            profile=profile,
            resources=self.resources,
        )

    def binding(self, project: str) -> dict:
        binding = {"repo": f"/home/dev/{project}"}
        if self._default_branch:
            binding["default_branch"] = self._default_branch
        return binding


class FakeHost:
    def __init__(self, root: Path, catalog: "FakeCatalog | None" = None) -> None:
        self.root = root
        # The real host snapshots the head at bring-up and hands the record back; the fake goes
        # through the same catalog so the routing journal sees real configurations here too.
        self.catalog = catalog or FakeCatalog()
        # Ordered log of every host call. The per-method lists below answer "did it happen"; this
        # answers "in what order", which some invariants depend on (complete_green must push from
        # the workspace before teardown removes it).
        self.calls: list[str] = []
        self.prepared: list[str] = []
        self.prepare_requires_existing: list[bool] = []
        self.reviews: list[str] = []
        self.stopped: list[str] = []
        self.torn_down: list[str] = []
        self.completed: list[str] = []
        self.fail_prepare_reason = ""
        # A bring-up failure the caller has to read for more than its message, the worker twin of
        # `fail_observer_error`: a HeadLaunchAborted carrying the pane that stayed up.
        self.fail_prepare_error: Exception | None = None
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
        self.worker_status_result: dict | None = None
        self.review_status_result: dict | None = None
        self.worker_status_error: Exception | None = None
        self.review_status_error: Exception | None = None
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
        # Observer heads (secretary-793): which sprints got one, which handles were stopped, and
        # the pid the fake heartbeat writes. os.getpid() is a live process, so the default launch
        # reads as alive; point it at a free pid to model a head that died.
        self.observers: list[str] = []
        self.observer_nudges: list[str] = []
        # The delivery criterion each wake was handed, so a test can prove the lifecycle passes one.
        self.observer_wake_confirms: list = []
        self.stopped_observers: list[str] = []
        # workspace -> live terminal handle, the inventory Orca answers `terminal list` from.
        self.observer_terminals: dict[str, str] = {}
        self.observer_pid = os.getpid()
        # Work liveness is separate from the pid.  Tests can make a live TUI report a completed,
        # stale queue without pretending the process has died.
        self.observer_status_result: dict | None = None
        self.fail_observer_reason = ""
        # A bring-up failure the caller has to read for more than its message, e.g. an
        # ObserverLaunchAborted that carries the handle of a terminal that stayed up.
        self.fail_observer_error: Exception | None = None
        # Orca refusing to close an observer pane: the head must be assumed alive afterwards.
        self.fail_stop_observer_reason = ""
        # The pid a worker/reviewer bring-up writes to its heartbeat file, the way the real
        # launcher's `with_pid_heartbeat` wrapper does. Launch-intent recovery reads it, so a fake
        # that never wrote one would make every intent look like a head that never came up. None
        # models a runtime that writes no heartbeat at all.
        self.head_pid: int | None = os.getpid()
        # Stop refusals (secretary-820). A stop the host will not confirm must never be followed by
        # a replacement head, and these are how a test makes one refuse.
        self.fail_stop_workspace_reason = ""
        self.fail_stop_head_reason = ""
        self.fail_stop_review_reason = ""
        self.fail_freeze_worker_reason = ""
        self.fail_retain_worker_reason = ""
        # Most fixture cards use the ordinary exec profile, which has no conversation to resume.
        # Tests that model a retained Codex TUI clear this explicitly.
        self.fail_resume_worker_reason = "retained worker session cannot accept a continuation"
        self.retained_workers: list[str] = []
        self.resumed_workers: list[str] = []
        # A retained session the heartbeat can no longer confirm as suspended: set False to model
        # the head dying while the reviewer judged its checkout.
        self.retained_worker_alive = True

    def _write_head_pid(self, kind: str, reference: str) -> None:
        path = Path(pid_file_path(kind, reference))
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.head_pid is None:
            path.unlink(missing_ok=True)
            return
        path.write_text(str(self.head_pid), encoding="utf-8")

    def prepare_worker(
        self,
        task: dict,
        worker_id: str,
        head: str,
        *,
        attempt_id: str = "",
        require_existing_workspace: bool = False,
    ) -> dict[str, str]:
        self.calls.append("prepare_worker")
        self.prepare_requires_existing.append(require_existing_workspace)
        if self.fail_prepare_error is not None:
            if isinstance(self.fail_prepare_error, HeadLaunchAborted):
                # A bring-up that failed with its terminal already open: the head is running, so
                # its heartbeat is there for recovery to find, exactly as after a real launch.
                self._write_head_pid("worker", task["ref"])
            raise self.fail_prepare_error
        if self.fail_prepare_reason:
            raise HostError(self.fail_prepare_reason)
        workspace = self.root / worker_id
        workspace.mkdir(parents=True, exist_ok=True)
        self.prepared.append(task["ref"])
        self._write_head_pid("worker", task["ref"])
        launched = self._launched(f"term:{worker_id}", head, task, "worker")
        return {
            "workspace": str(workspace),
            "handle": launched.handle,
            "base_branch": task.get("workspace", {}).get("base_branch") or "main",
            "run": launched.run,
        }

    def observer_workspace(self, reference: str) -> str:
        return str(self.root / "observers" / reference.replace(":", "-"))

    def observer_pid_file(self, reference: str) -> str:
        return str(self.root / "observers" / f"{reference.replace(':', '-')}.pid")

    def prepare_observer(self, sprint: dict, head: str, *, prompt: str) -> dict:
        self.calls.append("prepare_observer")
        if self.fail_observer_error is not None:
            raise self.fail_observer_error
        if self.fail_observer_reason:
            raise HostError(self.fail_observer_reason)
        reference = str(sprint["ref"])
        workspace = Path(self.observer_workspace(reference))
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "SPRINT.md").write_text(prompt, encoding="utf-8")
        self.observers.append(reference)
        pid_file = Path(self.observer_pid_file(reference))
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(self.observer_pid), encoding="utf-8")
        handle = f"observer:{reference}"
        # Like Orca: the terminal is findable by its workspace, which is how a head whose handle
        # was lost with its tick still gets stopped.
        self.observer_terminals[str(workspace)] = handle
        return {
            "workspace": str(workspace),
            "handle": handle,
            "pid_file": str(pid_file),
            "run": self.catalog.observer_run(head, workspace=str(workspace)).to_json(),
        }

    def observer_status(self, _record) -> dict:
        if self.observer_status_result is not None:
            return dict(self.observer_status_result)
        return {"last_activity": time.time(), "idle": False}

    def nudge_observer(self, record, *, confirm=None) -> str:
        self.calls.append("nudge_observer")
        if self.fail_observer_reason:
            raise HostError(self.fail_observer_reason)
        self.observer_nudges.append(str(record.sprint))
        # Like the real host: the pane took the prompt, and the delivery criterion the lifecycle
        # passed in is what decides whether the batch is closed.
        self.observer_wake_confirms.append(confirm)
        return "confirmed" if confirm is not None and confirm(time.time()) else "accepted"

    def stop_observer(self, record) -> None:
        self.calls.append("stop_observer")
        if self.fail_stop_observer_reason:
            raise HostError(self.fail_stop_observer_reason)
        handle = record.handle or self.observer_terminals.get(str(record.workspace) or "", "")
        self.observer_terminals.pop(str(record.workspace) or "", None)
        if handle:
            self.stopped_observers.append(handle)

    def pane_leaf(self, workspace: str, handle: str) -> str:
        return f"leaf:{handle}"

    def start_review(self, task: dict, record) -> ReviewLaunch:
        self.calls.append("start_review")
        if self.fail_review_error is not None:
            raise self.fail_review_error
        self.reviews.append(task["ref"])
        self._write_head_pid("review", task["ref"])
        # Mirror the real host: the reviewer gets its own pane and the worker head is shut down,
        # pinning the commit the reviewer judges.
        self.split_from.append(record.handle)
        launched = self._launched(
            f"review:{task['ref']}", record.review_head, task, "reviewer", record.workspace
        )
        try:
            if record.worker_continuation.retained:
                # Mirror the real host: a retained worker is already suspended, so the reviewer
                # judges a checkout nothing is editing without ending that conversation.
                self.confirm_worker_retained(record)
            else:
                self.freeze_worker(record)
        except HostError as exc:
            # The reviewer pane is up and the worker would not go: the real host hands the pane
            # back with the failure rather than reporting a bring-up that left nothing running.
            raise HeadLaunchAborted(
                f"worker freeze failed: {exc}",
                handle=launched.handle,
                workspace=record.workspace,
                pid_file=pid_file_path("review", task["ref"]),
            ) from None
        return ReviewLaunch(
            handle=launched.handle,
            leaf=f"leaf:{task['ref']}",
            commit=self.commit,
            run=launched.run,
        )

    def restart_worker(self, task: dict, record) -> LaunchedHead:
        self.calls.append("restart_worker")
        if self.fail_restart_reason:
            raise HostError(self.fail_restart_reason)
        self.prepared.append(task["ref"])
        self._write_head_pid("worker", task["ref"])
        return self._launched(f"rework:{task['ref']}", record.head, task, "worker")

    def _launched(
        self, handle: str, head: str, task: dict, role: str, workspace: str = ""
    ) -> LaunchedHead:
        return LaunchedHead(
            handle=handle,
            head=head,
            run=self.catalog.head_run(task, role=role, head=head, workspace=workspace).to_json(),
        )

    def review_running(self, task: dict, record) -> bool:
        self.calls.append("review_running")
        if self.review_running_error is not None:
            raise self.review_running_error
        if self.review_running_result is not None:
            return self.review_running_result
        return task["ref"] in self.reviews

    def worker_status(self, task: dict, record) -> dict:
        self.calls.append("worker_status")
        if self.worker_status_error is not None:
            raise self.worker_status_error
        return self.worker_status_result or {"known": True, "live": True, "reason": "live"}

    def review_status(self, task: dict, record) -> dict:
        self.calls.append("review_status")
        if self.review_status_error is not None:
            raise self.review_status_error
        running = self.review_running(task, record)
        return self.review_status_result or {"known": True, "live": running, "reason": "live" if running else "missing-terminal"}

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
        self._kill_head("worker", record)
        self._kill_head("review", record)

    def stop_workspace(self, record) -> None:
        """The confirmed twin of `stop`: a refusal reaches the caller (secretary-820)."""
        self.calls.append("stop_workspace")
        if self.fail_stop_workspace_reason:
            raise HostError(self.fail_stop_workspace_reason)
        self.stop(record)

    def stop_head(self, record, kind: str) -> None:
        self.calls.append(f"stop_head:{kind}")
        if self.fail_stop_head_reason:
            raise HostError(self.fail_stop_head_reason)
        handle = record.review_handle if kind == "review" else record.handle
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        if not handle and not pid_file:
            raise HostError(f"{kind} head has neither a pane handle nor a pid heartbeat")
        self._kill_head(kind, record)

    def freeze_worker(self, record) -> None:
        self.calls.append("freeze_worker")
        if self.fail_freeze_worker_reason:
            raise HostError(self.fail_freeze_worker_reason)
        if record.handle or record.worker_pid_file:
            self.stop_head(record, "worker")

    def retain_worker(self, record) -> None:
        self.calls.append("retain_worker")
        if self.fail_retain_worker_reason:
            raise HostError(self.fail_retain_worker_reason)
        if not record.handle and not record.worker_pid_file:
            raise HostError("worker session is unavailable for retention")
        if not record.handle:
            # Like the real host: a head with no pane is unaddressable, so there is nothing to
            # retain and the caller stops it instead.
            raise HostError("worker session has no addressable pane to retain")
        self.retained_workers.append(record.handle)

    def worker_retained_alive(self, record) -> bool:
        if not record.worker_continuation.retained:
            return False
        return bool(self.retained_worker_alive and (record.handle or record.worker_pid_file))

    def confirm_worker_retained(self, record) -> None:
        self.calls.append("confirm_worker_retained")
        # `fail_freeze_worker_reason` is the knob for "the host cannot vouch that this worker is
        # not writing". Suspending it for the reviewer instead of stopping it does not change what
        # a reviewer launch needs to hear before it takes the checkout.
        if self.fail_freeze_worker_reason:
            raise HostError(self.fail_freeze_worker_reason)
        if not self.worker_retained_alive(record):
            raise HostError("retained worker session is no longer confirmably suspended")

    def resume_worker(self, task: dict, record) -> None:
        self.calls.append("resume_worker")
        if self.fail_resume_worker_reason:
            raise HostError(self.fail_resume_worker_reason)
        if not record.handle and not record.worker_pid_file:
            raise HostError("retained worker session exited")
        self.resumed_workers.append(record.handle)

    def _kill_head(self, kind: str, record) -> None:
        """Drop the heartbeat of a stopped head, the way a closed pty tree does.

        Without this a stop would leave a pid file that still names this live test process, and
        every later liveness read would answer that the head the test just stopped is running.
        """
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        if pid_file:
            Path(pid_file).unlink(missing_ok=True)

    def stop_review(self, record) -> None:
        self.calls.append("stop_review")
        if not record.review_handle and not record.review_pid_file:
            return
        if self.fail_stop_review_reason:
            raise HostError(self.fail_stop_review_reason)
        if record.review_handle:
            self.stopped_reviews.append(record.review_handle)
        self._kill_head("review", record)

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


class FakeSprints:
    """The sprint facts the card cycle asks about, and nothing else.

    `show` answers what a card's sprint declares, which is what decides whether a verdict parks.
    `list` stays empty on purpose: the observer *head* lifecycle is reconciled from it, and these
    tests are about the cards, not about the head that watches them.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def list(self, *args, **kwargs) -> list[dict]:
        return []

    def show(self, reference: str, **kwargs) -> dict:
        if reference not in self.rows:
            raise TaskError("not_found", f"no sprint {reference}", 3)
        return self.rows[reference]


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
        # Head heartbeats are keyed on the card reference alone, so without this every test in the
        # process would read and overwrite the same /tmp pid files.
        env = mock.patch.dict(
            os.environ, {"SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies")}
        )
        env.start()
        self.addCleanup(env.stop)
        self.board = FakeKanboard()
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        # workspace is pinned off the repo checkout: these tests stand in for a worker
        # report, and the done gate would otherwise read this repo's own working tree.
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.legacy_pause = FakeLegacyPause()
        self.sprints = FakeSprints()
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            TaskAudit(self.data_dir),
            CutoverState(self.data_dir),
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
            legacy_pause=self.legacy_pause,  # type: ignore[arg-type]
            sprints=self.sprints,
        )
        self.selector = PilotSelector.exact("secretary-510-pilot")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def observed_sprint(self, *, profile: str = "claude-observer", status: str = "open") -> None:
        """Put the pilot card in a sprint that declares a concrete observer head.

        That declaration is what makes a substantive verdict park for a decision: a card with
        nobody to release it keeps the immediate behaviour, which `unobserved_card` restores.

        The sprint goes onto the sprint board as well, reserving the pilot's project, because the
        observer's decision is guarded by that reservation: an observer decides only about a card
        whose project its own open sprint holds.
        """
        self.board.metadata[12]["sprint_ref"] = "sprint:1031"
        self.sprints.rows["sprint:1031"] = {
            "ref": "sprint:1031", "status": status,
            "observer": {"kind": "head", "profile": profile},
        }
        row = next((row for row in self.board.sprints if row["reference"] == "sprint:1031"), None)
        if row is None:
            self.board.add_sprint(
                "sprint:1031", status=status, sprint_reservations='["secretary"]',
            )
        else:
            self.board.metadata[int(row["id"])]["sprint_status"] = status

    def unobserved_card(self) -> None:
        """Take the observer away again: the card parks nowhere and its verdicts act at once."""
        self.board.metadata[12].pop("sprint_ref", None)
        self.sprints.rows.clear()
        self.board.sprints.clear()

    def start_pilot(self) -> None:
        self.observed_sprint()
        self.runtime.pause_old(self.selector, actor="operator", evidence="legacy hard pause")
        started = self.runtime.start_new_pilot(self.selector, actor="operator")
        self.assertEqual(started["status"], "ok")

    def test_unauthenticated_worker_resource_is_not_claimed(self) -> None:
        self.start_pilot()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unauthenticated", "resource authentication failed", 1.0
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "resource-not-ready")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "ready")
        self.assertNotIn("prepare_worker", self.host.calls)

    def test_unavailable_worker_resource_is_not_claimed(self) -> None:
        self.start_pilot()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unavailable", "resource provider is unavailable", 1.0
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "resource-not-ready")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "ready")
        self.assertNotIn("prepare_worker", self.host.calls)

    def test_unready_retry_does_not_create_an_attempt(self) -> None:
        self.start_pilot()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unavailable", "resource provider is unavailable", 1.0
        )
        payload = {"resume_workspaces": {"secretary-510-pilot": {}}}

        result = self.runtime._claim(
            self.reader.show("secretary-510-pilot"), {}, payload, "old-attempt", resume_workspace=True
        )

        self.assertEqual(result["action"], "resource-not-ready")
        self.assertNotIn("attempt_id", payload)
        self.assertNotIn("attempts", payload)

    def test_unknown_probe_does_not_block_worker_launch(self) -> None:
        self.start_pilot()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unknown", "probe timed out", 1.0
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["step"], "claim")
        self.assertIn("prepare_worker", self.host.calls)

    def test_unavailable_reviewer_resource_does_not_launch_reviewer(self) -> None:
        record = DispatcherRecord(
            worker="secretary-510-pilot-pilot",
            workspace=str(self.data_dir / "workspaces" / "pilot"),
            handle="term:pilot",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt",
            comment_baseline=0,
            review_baseline=0,
            state="review_starting",
            claimed_at=1.0,
        )
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unavailable", "resource provider is unavailable", 1.0
        )

        result = start_reviewer(
            self.runtime,
            self.reader.show("secretary-510-pilot"),
            {},
            record,
            "attempt",
            action="review-started",
            payload={},
        )

        self.assertEqual(result["action"], "review-resource-not-ready")
        self.assertFalse(self.host.reviews)

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

    def test_production_tick_reconciles_a_record_left_behind_by_a_move_to_issues(self) -> None:
        self.commit_cutover()
        self.runtime.production_tick()
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-pilot",
            target="issues",
            reason="PO pulled it back out of the cycle",
            request_id="move-to-issues",
        )
        self.host.calls.clear()
        self.host.prepared.clear()

        result = self.runtime.production_tick()

        reconcile_actions = [a for a in result["actions"] if a["step"] == "production-reconcile"]
        self.assertEqual(len(reconcile_actions), 1)
        action = reconcile_actions[0]
        self.assertEqual(action["ref"], "secretary-510-pilot")
        self.assertEqual(action["action"], "record-removed")
        self.assertEqual(action["card_state"], "issues")
        payload = self.runtime.production_state.load()
        self.assertNotIn("secretary-510-pilot", payload["records"])
        # The record owns the live head. It must be stopped before the record can disappear, or a
        # later requeue will open another writer in the same workspace.
        self.assertEqual(self.host.stopped, ["secretary-510-pilot-pilot"])
        self.assertEqual(self.host.torn_down, [])
        self.assertNotIn("secretary-510-pilot", self.host.prepared)

    def test_production_reconcile_keeps_record_when_head_stop_is_unconfirmed(self) -> None:
        self.commit_cutover()
        self.runtime.production_tick()
        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-pilot",
            target="blocked",
            reason="park it",
            request_id="move-to-blocked-stop-refused",
        )
        self.host.fail_stop_workspace_reason = "orca terminal stop failed"

        result = self.runtime.production_tick()

        actions = [a for a in result["actions"] if a["step"] == "production-reconcile"]
        self.assertEqual([a["action"] for a in actions], ["head-stop-unconfirmed"])
        self.assertEqual(result["status"], "degraded")
        self.assertIn("secretary-510-pilot", self.runtime.production_state.load()["records"])

    def test_production_requeue_stops_previous_head_before_claiming_again(self) -> None:
        self.commit_cutover()
        self.runtime.production_tick()
        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-pilot",
            target="ready",
            reason="replace the active attempt",
            request_id="production-fast-requeue",
        )
        self.host.calls.clear()

        result = self.runtime.production_tick()

        claim = [a for a in result["actions"] if a.get("step") == "claim"]
        self.assertEqual(len(claim), 1)
        self.assertIn("stop_workspace", self.host.calls, result)
        self.assertEqual(self.host.prepared.count("secretary-510-pilot"), 2)

    def test_fresh_claim_stops_an_unowned_live_worker_before_launch(self) -> None:
        self.commit_cutover()
        pid_file = Path(pid_file_path("worker", "secretary-510-pilot"))
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        pid_file.write_text(str(process.pid), encoding="utf-8")

        result = self.runtime.production_tick()

        claim = [a for a in result["actions"] if a.get("step") == "claim"]
        self.assertEqual([a.get("status") for a in claim], ["ok"])
        self.assertIn("stop_workspace", self.host.calls)
        self.assertEqual(self.host.prepared, ["secretary-510-pilot"])

    def test_fresh_claim_blocks_when_an_unowned_worker_will_not_stop(self) -> None:
        self.commit_cutover()
        pid_file = Path(pid_file_path("worker", "secretary-510-pilot"))
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.host.fail_stop_workspace_reason = "orca terminal stop failed"

        result = self.runtime.production_tick()

        claim = [a for a in result["actions"] if a.get("step") == "claim"]
        self.assertEqual([a["action"] for a in claim], ["orphan-worker-stop-unconfirmed"])
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertEqual(self.host.prepared, [])
        self.assertIn("secretary-510-pilot", self.runtime.production_state.load()["records"])

    def test_production_tick_stops_respawn_started_after_po_parked_the_card(self) -> None:
        self.commit_cutover()
        self.runtime.production_tick()
        payload = self.runtime.production_state.load()
        record = payload["records"]["secretary-510-pilot"]
        stale = time.time() - stall_seconds("worker") - 60
        record["worker_started_at"] = stale
        record["worker_progress_at"] = stale
        self.runtime.production_state.save(payload)
        self.host.worker_status_result = {
            "known": True,
            "live": False,
            "reason": "missing-terminal",
        }
        real_show = self.reader.show
        raced = {"done": False}

        def show_then_park(reference: str):
            task = real_show(reference)
            if reference == "secretary-510-pilot" and not raced["done"]:
                raced["done"] = True
                self.writer.move(
                    role="po",
                    actor="operator",
                    reference=reference,
                    target="blocked",
                    reason="park after the active reread",
                    request_id="park-during-active-tick",
                )
            return task

        with mock.patch.object(self.reader, "show", side_effect=show_then_park):
            result = self.runtime.production_tick()

        actions = [a for a in result["actions"] if a.get("ref") == "secretary-510-pilot"]
        self.assertEqual(actions, [])
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")

        result = self.runtime.production_tick()

        actions = [a for a in result["actions"] if a.get("ref") == "secretary-510-pilot"]
        self.assertEqual([a["action"] for a in actions], ["record-removed"])
        self.assertIn("stop_workspace", self.host.calls)
        self.assertNotIn("secretary-510-pilot", self.runtime.production_state.load()["records"])

    def test_production_tick_does_not_reconcile_a_card_that_races_back_to_in_progress(self) -> None:
        # secretary-755 reviewer finding: `active_refs` is a snapshot taken at the top of the
        # tick, before reconciliation runs. If a PO moves the card out and back between that
        # snapshot and the moment reconciliation asks the board directly, the ref is missing from
        # the snapshot even though the card is live again. Only the state fetched immediately
        # before removal may decide the record is orphaned.
        self.commit_cutover()
        self.runtime.production_tick()
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        # This race concerns an execution card already in the dispatcher cycle. It is not an
        # card the PO moved back to the Issues backlog.
        self.board.metadata[12]["record_type"] = "task"

        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-pilot",
            target="issues",
            reason="PO pulled it back out of the cycle",
            request_id="move-to-issues-race",
        )
        self.host.calls.clear()
        self.host.prepared.clear()

        real_show = self.reader.show
        raced = {"done": False}

        def racing_show(reference: str):
            if reference == "secretary-510-pilot" and not raced["done"]:
                raced["done"] = True
                self.writer.move(
                    role="po",
                    actor="operator",
                    reference="secretary-510-pilot",
                    target="in_progress",
                    reason="PO put it right back before the tick finished looking",
                    request_id="move-back-to-in-progress-race",
                )
            return real_show(reference)

        with mock.patch.object(self.reader, "show", side_effect=racing_show):
            result = self.runtime.production_tick()

        reconcile_actions = [a for a in result["actions"] if a["step"] == "production-reconcile"]
        self.assertEqual(reconcile_actions, [])
        payload = self.runtime.production_state.load()
        self.assertIn("secretary-510-pilot", payload["records"])
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_production_tick_stamps_last_reconciled_at_distinctly_from_last_tick_finished_at(self) -> None:
        # secretary-755 reviewer finding: `last_tick_finished_at` predates reconciliation and is
        # stamped by every tick regardless of dispatcher version, so it cannot serve as evidence
        # that this tick's code actually ran the new reconciliation pass.
        self.commit_cutover()
        self.assertNotIn("last_reconciled_at", self.runtime.production_state.load())

        self.runtime.production_tick()

        payload = self.runtime.production_state.load()
        self.assertIsInstance(payload.get("last_reconciled_at"), str)
        self.assertTrue(payload["last_reconciled_at"])

    def test_production_tick_leaves_in_progress_and_validate_records_intact(self) -> None:
        self.commit_cutover()
        self.runtime.production_tick()
        self.assertEqual(list(self.runtime.production_state.load()["records"]), ["secretary-510-pilot"])

        result = self.runtime.production_tick()

        reconcile_actions = [a for a in result["actions"] if a["step"] == "production-reconcile"]
        self.assertEqual(reconcile_actions, [])
        self.assertIn("secretary-510-pilot", self.runtime.production_state.load()["records"])

    def test_production_tick_closes_a_divergence_once_its_card_leaves_the_active_cycle(self) -> None:
        self.commit_cutover()
        self.runtime.production_state.save({
            "version": 1,
            "mode": "production",
            "phase": "production",
            "owner": "secretary-pilot",
            "records": {},
            "controlled_divergences": [
                {
                    "id": "div_stale0000000000",
                    "at": "2026-07-01T00:00:00Z",
                    "attempt_id": "attempt-old",
                    "pilot_ref": "secretary-510-pilot",
                    "step": "production-recovery",
                    "reason": "active_claim_mismatch",
                    "expected": {},
                    "actual": {},
                    "details": ["worker"],
                    # No "status" field: a pre-existing divergence from before this field existed.
                },
            ],
        })
        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-pilot",
            target="issues",
            reason="PO pulled it back out of the cycle",
            request_id="move-to-issues-2",
        )

        result = self.runtime.production_tick()

        reconcile_actions = [a for a in result["actions"] if a["step"] == "production-reconcile"]
        closed = [a for a in reconcile_actions if a["action"] == "divergences-closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["divergence_ids"], ["div_stale0000000000"])
        payload = self.runtime.production_state.load()
        divergence = payload["controlled_divergences"][0]
        self.assertEqual(divergence["status"], "closed")
        self.assertIn("closed_at", divergence)
        self.assertIn("closed_reason", divergence)

    def test_production_tick_does_not_close_a_divergence_while_its_card_is_still_active(self) -> None:
        self.commit_cutover()
        self.runtime.production_state.save({
            "version": 1,
            "mode": "production",
            "phase": "production",
            "owner": "secretary-pilot",
            "records": {},
            "controlled_divergences": [
                {
                    "id": "div_live00000000000",
                    "at": "2026-07-01T00:00:00Z",
                    "attempt_id": "attempt-old",
                    "pilot_ref": "secretary-510-pilot",
                    "step": "production-recovery",
                    "reason": "active_claim_mismatch",
                    "expected": {},
                    "actual": {},
                    "details": ["worker"],
                    "status": "open",
                },
            ],
        })
        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-pilot",
            target="in_progress",
            reason="claimed elsewhere",
            request_id="move-to-in-progress",
        )

        self.runtime.production_tick()

        payload = self.runtime.production_state.load()
        divergence = payload["controlled_divergences"][0]
        self.assertEqual(divergence["status"], "open")

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

    def test_production_requeue_after_failed_rework_requires_the_preserved_workspace(self) -> None:
        """A fresh production attempt must retain the failed rework's resume provenance."""
        self.observed_sprint()
        self.commit_cutover()
        self.runtime.production_tick()
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="ready for review",
            request_id="production-worker-done",
        )
        self.assertEqual(self.runtime.production_tick()["actions"][0]["to"], "validate")
        self.assertEqual(self.runtime.production_tick()["actions"][0]["action"], "review-started")
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="red",
            body="fix the outage regression",
            request_id="production-review-red",
        )
        self.assertEqual(self.runtime.production_tick()["actions"][0]["to"], "assessment")
        self._decide("rework", request_id="production-decision-rework")
        self.host.fail_restart_reason = "terminal service unavailable"
        blocked = self.runtime.production_tick()
        self.assertEqual(blocked["actions"][0]["reason"], "rework bring-up failed")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertIn("secretary-510-pilot", self.runtime.production_state.load()["resume_workspaces"])

        self.writer.move(
            role="po",
            actor="operator",
            sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
            reference="secretary-510-pilot",
            target="ready",
            reason="retry after infrastructure outage",
            request_id="production-requeue-missing-workspace",
        )
        self.host.fail_restart_reason = ""
        self.host.fail_prepare_reason = "resume workspace is missing"
        retry = self.runtime.production_tick()

        self.assertEqual(retry["actions"][0]["status"], "blocked")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertEqual(self.host.prepare_requires_existing, [False, True])
        self.assertIn("secretary-510-pilot", self.runtime.production_state.load()["resume_workspaces"])

    def test_production_requeue_after_failed_gate_rework_preserves_workspace_provenance(self) -> None:
        """A failed gate rework resumes the same committed and dirty worker checkout."""
        self.commit_cutover()
        self.runtime.production_tick()
        workspace = self.data_dir / "workspaces" / "secretary-510-pilot-pilot"
        git(workspace, "init", "-q")
        _configure_git_user(workspace)
        (workspace / "kept.py").write_text("committed = True\n", encoding="utf-8")
        git(workspace, "add", "kept.py")
        git(workspace, "commit", "-qm", "preserved worker commit")
        commit = git(workspace, "rev-parse", "HEAD")
        (workspace / "wip.py").write_text("uncommitted = True\n", encoding="utf-8")
        git(workspace, "add", "wip.py")

        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="ready for validation",
            request_id="production-worker-done-before-gate-red",
        )
        self.assertEqual(self.runtime.production_tick()["actions"][0]["to"], "validate")
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self.host.fail_restart_reason = "terminal service unavailable"
        blocked = self.runtime.production_tick()

        self.assertEqual(blocked["actions"][0]["reason"], "rework bring-up failed")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertIn("secretary-510-pilot", self.runtime.production_state.load()["resume_workspaces"])

        self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-510-pilot",
            target="ready",
            reason="retry preserved gate workspace after infrastructure outage",
            request_id="production-requeue-gate-workspace",
        )
        self.host.fail_restart_reason = ""
        self.host.fail_prepare_reason = "resume workspace is missing"
        retry = self.runtime.production_tick()

        self.assertEqual(retry["actions"][0]["status"], "blocked")
        self.assertEqual(self.host.prepare_requires_existing, [False, True])
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), commit)
        self.assertEqual(git(workspace, "diff", "--cached", "--name-only"), "wip.py")
        self.assertIn("secretary-510-pilot", self.runtime.production_state.load()["resume_workspaces"])

    def test_pilot_requeues_after_failed_merge_gate_rework_in_preserved_workspace(self) -> None:
        """A pilot retry gives a failed merge-gate rework a new claim and its old checkout."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        first_attempt = self.runtime.state.load()["attempt_id"]
        workspace = self.data_dir / "workspaces" / "secretary-510-pilot-pilot"
        git(workspace, "init", "-q")
        _configure_git_user(workspace)
        (workspace / "kept.py").write_text("committed = True\n", encoding="utf-8")
        git(workspace, "add", "kept.py")
        git(workspace, "commit", "-qm", "preserved worker commit")
        commit = git(workspace, "rev-parse", "HEAD")
        (workspace / "wip.py").write_text("uncommitted = True\n", encoding="utf-8")
        git(workspace, "add", "wip.py")

        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="ready for validation",
            request_id="pilot-worker-done-before-merge-gate-red",
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
        self.host.gate_results = [GateResult("green", "pre-review green"), GateResult("red", "merge red")]
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="green",
            body="green",
            request_id="pilot-review-green-before-merge-gate-red",
        )
        self.host.fail_restart_reason = "terminal service unavailable"
        blocked = self.runtime.tick(self.selector)

        self.assertEqual(blocked["reason"], "rework bring-up failed")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertIn("secretary-510-pilot", self.runtime.state.load()["resume_workspaces"])

        self.writer.move(
            role="po",
            actor="operator",
            sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
            reference="secretary-510-pilot",
            target="ready",
            reason="retry preserved merge-gate workspace after infrastructure outage",
            request_id="pilot-requeue-merge-gate-workspace",
        )
        self.host.fail_restart_reason = ""
        retried = self.runtime.tick(self.selector)

        self.assertEqual(retried["status"], "ok")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.host.prepare_requires_existing, [False, True])
        self.assertNotEqual(self.runtime.state.load()["attempt_id"], first_attempt)
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), commit)
        self.assertEqual(git(workspace, "diff", "--cached", "--name-only"), "wip.py")

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

    def test_production_scan_continues_after_unready_resource(self) -> None:
        self.commit_cutover()
        self.runtime.catalog.worker_head = (  # type: ignore[method-assign]
            lambda task: "claude-opus" if task["ref"] == "secretary-510-pilot" else "codex"
        )

        def readiness(head: str) -> HeadReadiness:
            if head == "claude-opus":
                return HeadReadiness("claude-sub", "unauthenticated", "claude login expired", 1.0)
            return HeadReadiness("openai-sub", "ready", "resource is ready", 1.0)

        self.runtime.head_readiness = readiness
        result = self.runtime.production_tick()

        claimed = [action for action in result["actions"] if action.get("step") == "claim"][0]
        self.assertEqual(claimed["pilot_ref"], "secretary-510-neighbor")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "ready")
        self.assertEqual(self.reader.show("secretary-510-neighbor")["state"], "in_progress")
        self.assertEqual(
            claimed["skipped_ready"][0],
            {"ref": "secretary-510-pilot", "reason": "claude login expired"},
        )

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
            body="PR: https://github.com/example-org/secretary/pull/1",
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
        # The green verdict parks the card; the merge happens on the observer's release.
        done = self._park_and_decide("release")

        self.assertEqual(done["to"], "done")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "done")
        neighbor = self.reader.show("secretary-510-neighbor")
        self.assertEqual(neighbor["state"], "ready")
        self.assertIsNone(neighbor["claim"]["worker"])
        self.assertEqual(self.host.completed, ["secretary-510-pilot"])
        self.assertEqual(self.host.torn_down, self.host.stopped)
        # A green round never stops the worker head on its own: it stays suspended from its done
        # report until the merge tears the whole worktree down.
        self.assertNotIn("stop_head:worker", self.host.calls)
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

    def _decide(self, kind: str, reason: str = "the observer looked and decided", *, request_id: str = "") -> None:
        """The observer's decision on a parked card, the only thing that releases it."""
        self.writer.decide(
            role="observer", actor="observer", reference="secretary-510-pilot",
            kind=kind, body=reason, request_id=request_id or f"decision-{kind}",
        )

    def _park_and_decide(self, kind: str, *, request_id: str = "") -> dict:
        """Tick the parked verdict through the seam and hand back the tick that acted on it."""
        parked = self.runtime.tick(self.selector)
        self.assertEqual(parked["to"], "assessment")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "assessment")
        self._decide(kind, request_id=request_id)
        return self.runtime.tick(self.selector)

    def _drop_records_and_restart_attempt(self) -> None:
        """A dispatcher that came back without its records: the card is mid-flight on the board and
        the next tick has to adopt it under a fresh attempt id."""
        payload = self.runtime.state.load()
        self.runtime.state.put_records(payload, {})
        payload["attempt_id"] = "attempt-after-restart"
        self.runtime.state.save(payload)

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

    def test_fresh_output_keeps_a_live_worker_past_the_old_total_wait_ceiling(self) -> None:
        """A progress signal renews the silence window instead of respawning real work."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.runtime.tick(self.selector)
        self._rewind_wait("worker", seconds=stall_seconds("worker") + 60)
        self.host.worker_status_result = {
            "known": True, "live": True, "reason": "live", "last_activity": time.time() - 1,
        }

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "waiting-worker-report")
        self.assertNotIn("restart_worker", self.host.calls)

    def test_fresh_output_keeps_a_live_reviewer_past_the_old_total_wait_ceiling(self) -> None:
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.runtime.tick(self.selector)
        self._rewind_wait("review", seconds=stall_seconds("review") + 60)
        self.host.review_status_result = {
            "known": True, "live": True, "reason": "live", "last_activity": time.time() - 1,
        }

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "waiting-review-verdict")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"])

    def test_live_reviewer_is_checked_by_its_saved_handle(self) -> None:
        """The wait path probes every tick, but does not use the mutable terminal title."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.runtime.tick(self.selector)

        self.host.calls.clear()
        self._rewind_wait("review", seconds=stall_seconds("review") - 60)
        waiting = self.runtime.tick(self.selector)

        self.assertEqual(waiting["action"], "waiting-review-verdict")
        self.assertEqual(self.host.reviews, ["secretary-510-pilot"], "healthy reviewer was killed")
        self.assertIn("review_status", self.host.calls)

    def test_missing_worker_terminal_respawns_without_waiting_for_ceiling(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "worker-respawned")
        self.assertIn("terminal missing-terminal", self.reader.show("secretary-510-pilot")["comments"][-1]["body"])

    def test_worker_process_exited_with_shell_left_behind_respawns_without_waiting_for_ceiling(self) -> None:
        """secretary-751 (the secretary-736/secretary-731 incident): the head crashed, Orca kept
        the workspace's own shell alive in the pane, and only the pid heartbeat says the head
        itself is gone. First observation respawns in the same workspace; the next escalates to
        Blocked. Committed and uncommitted worker work survive both."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        workspace = self.data_dir / "workspaces" / "secretary-510-pilot-pilot"
        git(workspace, "init", "-q")
        _configure_git_user(workspace)
        (workspace / "kept.py").write_text("committed = True\n", encoding="utf-8")
        git(workspace, "add", "kept.py")
        git(workspace, "commit", "-qm", "preserved worker commit")
        commit = git(workspace, "rev-parse", "HEAD")
        (workspace / "wip.py").write_text("uncommitted = True\n", encoding="utf-8")
        git(workspace, "add", "wip.py")
        self.host.worker_status_result = {"known": True, "live": False, "reason": "process-exited"}

        respawned = self.runtime.tick(self.selector)

        self.assertEqual(respawned["action"], "worker-respawned")
        self.assertIn(
            "terminal process-exited",
            self.reader.show("secretary-510-pilot")["comments"][-1]["body"],
        )
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), commit)
        self.assertEqual(git(workspace, "diff", "--cached", "--name-only"), "wip.py")

        self.host.worker_status_result = {"known": True, "live": False, "reason": "process-exited"}
        escalated = self.runtime.tick(self.selector)

        self.assertEqual(escalated["to"], "blocked")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertEqual(
            self.host.calls.count("restart_worker"), 1, "escalation must not respawn again"
        )
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), commit)
        self.assertEqual(git(workspace, "diff", "--cached", "--name-only"), "wip.py")

    def test_missing_reviewer_terminal_respawns_without_waiting_for_ceiling(self) -> None:
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.host.review_status_result = {"known": True, "live": False, "reason": "missing-terminal"}

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "review-respawned")

    def test_live_worker_without_new_output_is_respawned(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        payload = self.runtime.state.load()
        payload["records"]["secretary-510-pilot"]["worker_progress_at"] = time.time() - stall_seconds("worker") - 1
        self.runtime.state.save(payload)
        self.host.worker_status_result = {"known": True, "live": True, "reason": "live", "last_activity": time.time() - stall_seconds("worker") - 1}

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "worker-respawned")

    def test_worker_without_first_output_is_respawned_within_the_short_window(self) -> None:
        """A live login prompt has activity at launch, but never progresses past it."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        payload = self.runtime.state.load()
        record = payload["records"]["secretary-510-pilot"]
        started = time.time() - INITIAL_OUTPUT_STALL_DEFAULT - 1
        record["worker_started_at"] = started
        record["worker_progress_at"] = started
        self.runtime.state.save(payload)
        self.host.worker_status_result = {
            "known": True, "live": True, "reason": "live", "last_activity": started,
        }

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "worker-respawned")

    def test_reviewer_without_first_output_is_respawned_within_the_short_window(self) -> None:
        self.start_pilot()
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        payload = self.runtime.state.load()
        record = payload["records"]["secretary-510-pilot"]
        started = time.time() - INITIAL_OUTPUT_STALL_DEFAULT - 1
        record["review_started_at"] = started
        record["review_progress_at"] = started
        self.runtime.state.save(payload)
        self.host.review_status_result = {
            "known": True, "live": True, "reason": "live", "last_activity": started,
        }

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "review-respawned")

    def test_pid_confirmed_worker_silent_past_first_output_window_is_not_respawned(self) -> None:
        """secretary-751: a runtime that can prove liveness via pid must not be respawned or
        blocked for silence alone. Only an actual exit (already covered by the process-exited
        path) may end this wait for it."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        payload = self.runtime.state.load()
        record = payload["records"]["secretary-510-pilot"]
        started = time.time() - INITIAL_OUTPUT_STALL_DEFAULT - 1
        record["worker_started_at"] = started
        record["worker_progress_at"] = started
        self.runtime.state.save(payload)
        self.host.worker_status_result = {
            "known": True, "live": True, "reason": "live", "last_activity": started,
            "pid_confirmed": True,
        }

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "waiting-worker-report")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_pid_confirmed_reviewer_silent_past_the_long_ceiling_is_not_blocked(self) -> None:
        """A pid-confirmed live reviewer must survive even the long inactivity ceiling: the pid
        heartbeat is the stronger signal, so the timing fallback never applies to it."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        self.assertEqual(self.runtime.tick(self.selector)["action"], "waiting-review-verdict")
        self._rewind_wait("review", seconds=stall_seconds("review") + 60)
        self.host.review_status_result = {
            "known": True, "live": True, "reason": "live", "last_activity": None,
            "pid_confirmed": True,
        }

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "waiting-review-verdict")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "validate")

    def test_runtime_inventory_failure_is_degraded_not_a_head_death(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.host.worker_status_error = HostError("orca terminal list failed")

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "worker-runtime-unavailable")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_runtime_inventory_failure_still_uses_the_wait_ceiling(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.runtime.tick(self.selector)
        self._rewind_wait("worker", seconds=stall_seconds("worker") + 60)
        self.host.worker_status_error = HostError("orca terminal list failed")

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "worker-respawned")
        self.assertIn("restart_worker", self.host.calls)

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
            sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
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
            sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
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
        self.assertEqual(self._park_and_decide("rework")["action"], "rework-started")

        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(record["review_waiting_since"], 0.0)
        self.assertEqual(record["review_respawns"], 0)

        # And the invariant the counters exist for: round 2 still gets its one respawn.
        self.host.commit = "review-rework-c0ffee"
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
        """A failed rework launch must not turn a preserved checkout into a fresh branch.

        This models the outage path: the reviewer and worker workspace remain, the rework launch
        fails, then an operator moves the card back from Blocked to Ready for a new attempt.
        """
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
        self.assertEqual(self.runtime.tick(self.selector)["to"], "assessment")
        self._decide("rework")
        self.host.fail_restart_reason = "terminal service unavailable"
        blocked = self.runtime.tick(self.selector)
        self.assertEqual(blocked["reason"], "rework bring-up failed")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")

        self.writer.move(
            role="po",
            actor="operator",
            sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
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
        self.assertEqual(self.host.prepare_requires_existing, [False, True])
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
        # The worker is suspended, not stopped: the reviewer is the only head acting on the
        # checkout, and the round keeps a conversation for a red verdict to continue.
        self.assertNotIn("stop_head:worker", self.host.calls)
        self.assertIn("confirm_worker_retained", self.host.calls)

    def test_gate_red_reuses_the_retained_worker_conversation(self) -> None:
        """A live TUI session keeps both its terminal identity and its provider conversation."""
        self.host.fail_resume_worker_reason = ""
        self.start_pilot()
        self.runtime.tick(self.selector)
        initial = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="done", request_id="worker-done-reused-session",
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
        retained = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(
            retained["worker_continuation"]["stage"],
            WorkerContinuationStage.RETAINED.value,
        )

        gated = self.runtime.tick(self.selector)

        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(gated["action"], "gate-red-reused-worker")
        self.assertEqual(record["handle"], initial["handle"])
        self.assertEqual(record["worker_pid_file"], initial["worker_pid_file"])
        self.assertEqual(record["worker_run"], initial["worker_run"])
        self.assertEqual(self.host.prepared, ["secretary-510-pilot"])
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertEqual(self.host.resumed_workers, [initial["handle"]])
        continuation = self.reader.show("secretary-510-pilot")["comments"][-1]["body"]
        self.assertIn("continuation: reused", continuation)
        self.assertIn("worker profile codex", continuation)

    def test_gate_red_replaces_a_session_that_cannot_continue(self) -> None:
        """A failed continuation is stopped before exactly one durable replacement is launched."""
        self.start_pilot()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self._run_worker_to_validate()

        gated = self.runtime.tick(self.selector)

        self.assertEqual(gated["action"], "gate-red-rework")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertLess(self.host.calls.index("stop_head:worker"), self.host.calls.index("restart_worker"))
        self.assertIn("continuation: replacement", self.reader.show("secretary-510-pilot")["comments"][-1]["body"])

    def _review_red(self, request_id: str = "review-red") -> None:
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot", kind="red",
            body="fix the hermetic test", request_id=request_id,
        )

    def test_red_review_reuses_the_retained_worker_conversation(self) -> None:
        """The round that wrote the code gets its verdict: same session, same round of work."""
        self.host.fail_resume_worker_reason = ""
        self.start_pilot()
        self._run_worker_to_validate()
        initial = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        self._review_red()

        # The verdict parks first; the rework is the observer's decision, not the verdict's.
        reworked = self._park_and_decide("rework")

        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(reworked["action"], "review-red-reused-worker")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertEqual(self.host.resumed_workers, [initial["handle"]])
        self.assertEqual(record["handle"], initial["handle"])
        self.assertEqual(record["worker_run"], initial["worker_run"])
        self.assertEqual(record["attempt_round"], initial["attempt_round"] + 1)
        self.assertEqual(
            self.host.stopped_reviews,
            ["review:secretary-510-pilot"],
            "the reviewer's stop is confirmed before its findings are delivered",
        )
        self.assertLess(
            self.host.calls.index("stop_review"), self.host.calls.index("resume_worker")
        )
        continuation = self.reader.show("secretary-510-pilot")["comments"][-1]["body"]
        self.assertIn("review red continuation: reused", continuation)
        self.assertIn("worker profile codex", continuation)

    def test_a_red_review_keeps_the_worker_when_the_rework_report_arrives(self) -> None:
        """The reused session reports into the next round instead of being waited on forever."""
        self.host.fail_resume_worker_reason = ""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self._review_red()
        self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")
        self.host.commit = "review-rework-accepted-c0ffee"
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="rework report", request_id="worker-done-after-red-review",
        )

        advanced = self.runtime.tick(self.selector)

        self.assertEqual(advanced["to"], "validate")
        self.assertEqual(self.host.prepared, ["secretary-510-pilot"])

    def test_a_red_review_will_not_deliver_while_the_reviewer_refuses_to_stop(self) -> None:
        """An unconfirmed reviewer stop is not a checkout the worker may be woken into."""
        self.host.fail_resume_worker_reason = ""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self._review_red()
        self.host.fail_stop_review_reason = "Orca cannot confirm terminal stop"

        refused = self.runtime.tick(self.selector)

        self.assertEqual(refused["action"], "review-stop-unconfirmed")
        # The refusal now lands one step earlier, at the park: a card is never parked with a
        # reviewer that may still be alive in its checkout, so it does not leave Validate either.
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "validate")
        self.assertEqual(self.host.resumed_workers, [])
        self.assertNotIn("restart_worker", self.host.calls)

        self.host.fail_stop_review_reason = ""
        retried = self._park_and_decide("rework")

        self.assertEqual(retried["action"], "review-red-reused-worker")
        self.assertEqual(self.host.calls.count("resume_worker"), 1)

    def test_a_red_review_replaces_a_session_that_refuses_the_continuation(self) -> None:
        """A refused delivery is stopped — confirmed — before one replacement is launched."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self._review_red()

        reworked = self._park_and_decide("rework")

        self.assertEqual(reworked["action"], "rework-started")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertLess(
            self.host.calls.index("stop_head:worker"), self.host.calls.index("restart_worker")
        )
        self.assertIn(
            "review red continuation: replacement",
            self.reader.show("secretary-510-pilot")["comments"][-1]["body"],
        )

    def test_a_red_review_retries_an_unconfirmed_stop_before_a_replacement(self) -> None:
        """A stop the host will not confirm never earns a replacement, only the next tick."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self._review_red()
        parked = self.runtime.tick(self.selector)
        self.assertEqual(parked["to"], "assessment")
        self._decide("rework")
        self.host.fail_stop_head_reason = "Orca cannot confirm terminal stop"

        stopped = self.runtime.tick(self.selector)

        self.assertEqual(stopped["action"], "worker-stop-unconfirmed")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        # The card is already back with the worker: the delivery boundary on the record is what
        # the next tick picks the round up from, and it stays on the red-review branch.
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

        self.host.fail_stop_head_reason = ""
        retried = self.runtime.tick(self.selector)

        self.assertEqual(retried["action"], "rework-started")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertIn(
            "review red continuation: replacement",
            self.reader.show("secretary-510-pilot")["comments"][-1]["body"],
        )

    # the verdict seam (secretary-1031) ---------------------------------------

    def _parked_record(self) -> dict:
        return self.runtime.state.load()["records"]["secretary-510-pilot"]

    def test_a_green_verdict_parks_before_it_merges(self) -> None:
        """The ordering proof, in two halves.

        The release effect is broken before the verdict is even given. The verdict's own tick
        parks the card and stops there, so the failure never happens: nothing merged, nothing
        torn down. Then the decision is recorded and the broken effect does run, and the card is
        still parked in Assessment rather than merged, blocked or sent back for rework.
        """
        self.start_pilot()
        self.host.fail_complete_reason = "merge push failed: ! [rejected] non-fast-forward"
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)  # gate green -> review started
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot",
            kind="green", body="looks good", request_id="review-green-parks",
        )

        parked = self.runtime.tick(self.selector)

        self.assertEqual(parked["to"], "assessment")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "assessment")
        self.assertEqual(self.host.completed, [], "nothing was merged")
        self.assertEqual(self.host.torn_down, [], "the checkout is kept for the decision")
        self.assertEqual(
            self._parked_record()["worker_continuation"]["stage"],
            WorkerContinuationStage.ASSESSMENT_PARKED.value,
        )
        # The reviewer is stopped cleanly and the worker of the round is still owned.
        self.assertEqual(self.host.stopped_reviews, ["review:secretary-510-pilot"])
        self.assertEqual(self.host.stopped, [])
        self.assertTrue(self._parked_record()["worker_continuation"]["session_held"])
        # The reviewed commit outlives the reviewer's pane: the release may land that and nothing
        # else, however long the decision takes.
        self.assertEqual(self._parked_record()["review_commit"], self.host.commit)

        # And it stays parked: an undecided card is not something a later tick acts on.
        waiting = self.runtime.tick(self.selector)

        self.assertEqual(waiting["action"], "waiting-observer-decision")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "assessment")
        self.assertEqual(self.host.completed, [])

        # Now record the decision, so the broken effect is actually reached.
        self._decide("release")

        failed = self.runtime.tick(self.selector)

        self.assertEqual(failed["status"], "blocked")
        self.assertIn("complete_green", self.host.calls, "the release effect was exercised")
        self.assertEqual(self.host.completed, [], "nothing was merged")
        self.assertEqual(self.host.torn_down, [])
        self.assertEqual(self.host.resumed_workers, [], "and it was not reworked either")
        # A release the dispatcher cannot carry out goes to Blocked with the reason on it, which
        # is where a merge that cannot land has always ended up. Keeping the card parked and
        # taking the decision back down is the deferred recovery card, not this one.
        card = self.reader.show("secretary-510-pilot")
        self.assertEqual(card["state"], "blocked")
        self.assertIn("non-fast-forward", card["comments"][-1]["body"])
        self.assertEqual(self.host.calls.count("complete_green"), 1)

    def test_a_red_verdict_parks_before_the_worker_continues(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self._review_red()

        parked = self.runtime.tick(self.selector)

        self.assertEqual(parked["to"], "assessment")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "assessment")
        self.assertEqual(self.host.resumed_workers, [], "no rework round was opened")
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertEqual(self._parked_record()["attempt_round"], 1)

        waiting = self.runtime.tick(self.selector)

        self.assertEqual(waiting["action"], "waiting-observer-decision")
        self.assertEqual(self.host.resumed_workers, [])

    def test_a_mechanical_gate_verdict_never_passes_through_assessment(self) -> None:
        """CI and the local gate resolve in Validate, before the observer is ever involved."""
        self.start_pilot()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self._run_worker_to_validate()

        gated = self.runtime.tick(self.selector)

        self.assertEqual(gated["action"], "gate-red-rework")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_a_gate_that_turns_red_under_a_green_verdict_bounces_from_validate(self) -> None:
        """The pre-merge re-check stays on the Validate side: a card only parks once the
        mechanical state is green, so a red gate is never a question for the observer."""
        self.start_pilot()
        self.host.gate_results = [GateResult("green", "green"), GateResult("red", "CI went red")]
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot",
            kind="green", body="looks good", request_id="review-green-late-red-gate",
        )

        bounced = self.runtime.tick(self.selector)

        self.assertEqual(bounced["action"], "merge-gate-red-rework")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_a_pending_gate_under_a_green_verdict_waits_in_validate(self) -> None:
        self.start_pilot()
        self.host.gate_results = [GateResult("green", "green"), GateResult("pending", "CI running")]
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot",
            kind="green", body="looks good", request_id="review-green-pending-gate",
        )

        waiting = self.runtime.tick(self.selector)

        self.assertEqual(waiting["action"], "merge-gate-pending")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "validate")

    def test_a_reslice_decision_stops_the_heads_and_keeps_the_workspace(self) -> None:
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self._review_red()

        resliced = self._park_and_decide("reslice")

        self.assertEqual((resliced["to"], resliced["decision"]), ("blocked", "reslice"))
        card = self.reader.show("secretary-510-pilot")
        self.assertEqual(card["state"], "blocked")
        self.assertIn("Observer decision: reslice", card["comments"][-1]["body"])
        self.assertIn("stop_head:worker", self.host.calls)
        self.assertEqual(self.host.torn_down, [], "the recut starts from the work that is there")
        self.assertIn(
            "secretary-510-pilot", self.runtime.state.load()["resume_workspaces"]
        )

    def test_a_parked_card_survives_a_dispatcher_restart_with_its_worker(self) -> None:
        """Criterion 3: the park is on disk, so a dispatcher that comes back finds the card still
        waiting, the workspace still owned and the round's own conversation still resumable."""
        self.host.fail_resume_worker_reason = ""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        before = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self._review_red()
        self.assertEqual(self.runtime.tick(self.selector)["to"], "assessment")

        restarted = DispatcherRuntime(
            self.reader,
            self.writer,
            TaskAudit(self.data_dir),
            CutoverState(self.data_dir),
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
            legacy_pause=self.legacy_pause,  # type: ignore[arg-type]
        )

        self.assertEqual(restarted.tick(self.selector)["action"], "waiting-observer-decision")
        parked = restarted.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(parked["workspace"], before["workspace"])
        self.assertEqual(parked["handle"], before["handle"])
        self.assertTrue(parked["worker_continuation"]["session_held"])

        self._decide("rework")
        reworked = restarted.tick(self.selector)

        self.assertEqual(reworked["action"], "review-red-reused-worker")
        self.assertEqual(self.host.resumed_workers, [before["handle"]])

    def test_a_release_move_carries_its_decision_into_the_audit(self) -> None:
        """Criterion 4: the transition out of Assessment names the decision it performed, so the
        seam is checkable from the audit without reading a comment."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot",
            kind="green", body="looks good", request_id="review-green-audited",
        )

        self.assertEqual(self._park_and_decide("release")["to"], "done")

        audit = TaskAudit(self.data_dir)
        decided = audit.events("secretary-510-pilot", kind="decided")[-1]
        moved = [
            event for event in audit.events("secretary-510-pilot", kind="moved")
            if event["payload"]["from"] == "assessment"
        ]
        self.assertEqual(decided["payload"]["decision"], "release")
        self.assertEqual(len(moved), 1)
        self.assertEqual((moved[0]["payload"]["to"], moved[0]["payload"]["decision"]), ("done", "release"))

    def test_a_checkout_that_moved_while_parked_blocks_the_release(self) -> None:
        """The reviewed commit is the only thing a release may land, and a park can last a while.
        A card whose checkout moved under it is a release that cannot be carried out, so it goes
        to Blocked naming the commit the decision was made about."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        reviewed = self.host.commit
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot",
            kind="green", body="looks good", request_id="review-green-drift-while-parked",
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "assessment")
        self.host.commit = "moved-under-the-park-c0ffee"
        self._decide("release")

        blocked = self.runtime.tick(self.selector)

        self.assertEqual(blocked["status"], "blocked")
        card = self.reader.show("secretary-510-pilot")
        self.assertEqual(card["state"], "blocked")
        self.assertIn(reviewed[:12], card["comments"][-1]["body"])
        self.assertEqual(self.host.completed, [])
        self.assertEqual(self.host.torn_down, [])

    def test_a_crash_inside_the_release_resumes_the_parked_card(self) -> None:
        """A tick that dies inside the merge itself. There is no half-release state to recover:
        the card resumes parked with the decision still standing, and the next tick runs the
        release from the top. Telling a publish that landed from one that did not, so the retry
        can be skipped, is the deferred recovery card."""
        self.start_pilot()
        self._drive_to_green_verdict()
        self.assertEqual(self.runtime.tick(self.selector)["to"], "assessment")
        self._decide("release")

        def die_before_publishing(task: dict, record) -> None:
            raise OSError("the dispatcher died on its way into the merge")

        with mock.patch.object(self.host, "complete_green", die_before_publishing):
            with self.assertRaises(OSError):
                self.runtime.tick(self.selector)

        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "assessment")
        self.assertEqual(
            self._parked_record()["worker_continuation"]["stage"],
            WorkerContinuationStage.ASSESSMENT_PARKED.value,
            "the card resumes parked, with the decision still the only thing standing",
        )
        self.assertEqual(self.host.completed, [])

        recovered = self.runtime.tick(self.selector)

        self.assertEqual(recovered["to"], "done")
        self.assertEqual(self.host.completed, ["secretary-510-pilot"])
        self.assertEqual(
            self.host.calls.count("complete_green"), 1,
            "the crashed attempt never reached the host's own merge, and recovery ran it once",
        )

    def test_a_card_with_no_observer_merges_on_the_verdict_tick(self) -> None:
        """Criterion: a card nobody watches must not be parked, because nothing would release it.
        Its green verdict merges and its red verdict reworks, exactly as before the seam."""
        self.start_pilot()
        self.unobserved_card()
        self._drive_to_green_verdict()

        merged = self.runtime.tick(self.selector)

        self.assertEqual(merged["to"], "done")
        self.assertEqual(self.host.completed, ["secretary-510-pilot"])

    def test_a_card_whose_sprint_declares_no_observer_reworks_on_the_verdict_tick(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.start_pilot()
        self.sprints.rows["sprint:1031"]["observer"] = {"kind": "none"}
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self._review_red()

        reworked = self.runtime.tick(self.selector)

        self.assertEqual(reworked["action"], "review-red-reused-worker")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_a_closed_sprint_does_not_park_the_cards_it_left_behind(self) -> None:
        self.start_pilot()
        self.sprints.rows["sprint:1031"]["status"] = "closed"
        self._drive_to_green_verdict()

        merged = self.runtime.tick(self.selector)

        self.assertEqual(merged["to"], "done")

    def test_an_unreadable_sprint_board_does_not_park(self) -> None:
        """An answer that cannot be read is not a reason to put a card in a wait nobody can end."""
        self.start_pilot()
        self.sprints.rows.clear()
        self._drive_to_green_verdict()

        self.assertEqual(self.runtime.tick(self.selector)["to"], "done")

    def test_a_crash_between_the_verdict_and_the_park_resumes_parked(self) -> None:
        """Boundary one. The verdict is durable before the board moves, so the recovery of a
        tick that died in between is the park itself, never a re-decision of the verdict."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self._review_red()
        real_move = self.writer.move

        def fail_the_park(**kwargs):
            if kwargs.get("target") == "assessment":
                raise OSError("dispatcher died before the park's board move")
            return real_move(**kwargs)

        with mock.patch.object(self.writer, "move", fail_the_park):
            with self.assertRaises(OSError):
                self.runtime.tick(self.selector)

        stranded = self._parked_record()
        self.assertEqual(
            stranded["worker_continuation"]["stage"],
            WorkerContinuationStage.ASSESSMENT_PENDING.value,
        )
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "validate")
        self.assertEqual(self.host.resumed_workers, [])

        recovered = self.runtime.tick(self.selector)

        self.assertEqual(recovered["to"], "assessment")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "assessment")
        self.assertEqual(
            self._parked_record()["worker_continuation"]["stage"],
            WorkerContinuationStage.ASSESSMENT_PARKED.value,
        )

    def test_a_crash_between_the_park_and_the_release_resumes_parked(self) -> None:
        """Boundary two. The card is on the board in Assessment and the record died before its
        checkpoint: the single well-defined state is parked, with the decision re-read from the
        card rather than replayed from anything the dead tick believed."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot",
            kind="green", body="looks good", request_id="review-green-crash",
        )
        real_save = self.runtime.state.save

        def die_after_the_park(payload: dict) -> None:
            record = payload.get("records", {}).get("secretary-510-pilot", {})
            if record.get("state") == "assessment":
                raise OSError("dispatcher died after the park's board move")
            real_save(payload)

        with mock.patch.object(self.runtime.state, "save", die_after_the_park):
            with self.assertRaises(OSError):
                self.runtime.tick(self.selector)

        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "assessment")
        self.assertEqual(
            self._parked_record()["worker_continuation"]["stage"],
            WorkerContinuationStage.ASSESSMENT_PENDING.value,
        )

        recovered = self.runtime.tick(self.selector)

        self.assertEqual(recovered["to"], "assessment")
        self.assertEqual(self.host.completed, [], "recovery merges nothing on its own")
        self._decide("release")

        released = self.runtime.tick(self.selector)

        self.assertEqual(released["to"], "done")
        self.assertEqual(self.host.completed, ["secretary-510-pilot"])

    def test_a_parked_card_whose_record_was_lost_is_adopted_as_parked(self) -> None:
        """A dispatcher restart over a parked card: the board is the fact, and the decision is
        still the only thing that moves it."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.runtime.tick(self.selector)
        self._review_red()
        self.assertEqual(self.runtime.tick(self.selector)["to"], "assessment")
        self._drop_records_and_restart_attempt()

        adopted = self.runtime.tick(self.selector)

        self.assertEqual(adopted["action"], "waiting-observer-decision")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "assessment")
        record = self._parked_record()
        self.assertEqual(record["state"], "assessment")
        self.assertEqual(
            record["worker_continuation"]["stage"],
            WorkerContinuationStage.ASSESSMENT_PARKED.value,
        )

        self._decide("rework")
        reworked = self.runtime.tick(self.selector)

        # Nothing proves the old session is still suspended, so the rework opens a replacement
        # behind a confirmed stop of the checkout rather than resuming a conversation on trust.
        self.assertEqual(reworked["action"], "rework-started")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.host.resumed_workers, [])
        self.assertLess(
            self.host.calls.index("stop_workspace"), self.host.calls.index("restart_worker")
        )

    def test_a_retained_worker_of_unclear_liveness_is_stopped_before_the_reviewer(self) -> None:
        """Retention is a record; the heartbeat decides. An unclear answer costs the session."""
        self.host.fail_resume_worker_reason = ""
        self.start_pilot()
        self._run_worker_to_validate()
        self.host.retained_worker_alive = False

        started = self.runtime.tick(self.selector)

        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(started["action"], "review-started")
        self.assertIn("stop_head:worker", self.host.calls)
        self.assertLess(
            self.host.calls.index("stop_head:worker"), self.host.calls.index("start_review")
        )
        self.assertEqual(record["worker_continuation"], {})
        self._review_red()

        reworked = self._park_and_decide("rework")

        self.assertEqual(reworked["action"], "rework-started")
        self.assertEqual(self.host.resumed_workers, [])
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_gate_red_with_an_old_record_stops_before_replacement(self) -> None:
        """A pre-retention record cannot turn unknown worker liveness into a second writer."""
        self.start_pilot()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self._run_worker_to_validate()
        # The head is up and suspended; only the record forgot about it, the way one written by a
        # dispatcher that predates retention would have.
        payload = self.runtime.state.load()
        payload["records"]["secretary-510-pilot"]["worker_continuation"] = {}
        self.runtime.state.save(payload)
        self.host.calls.clear()

        gated = self.runtime.tick(self.selector)

        self.assertEqual(gated["action"], "gate-red-rework")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertLess(self.host.calls.index("stop_head:worker"), self.host.calls.index("restart_worker"))

    def test_gate_red_retries_an_unconfirmed_stop_before_a_replacement(self) -> None:
        """A non-retained worker cannot strand the card after a red gate stop refusal.

        The red transition is already durable and the card is already back with the worker: what
        the refusal costs is the replacement, and the next tick picks the transition up from the
        record rather than re-running the gate.
        """
        self.start_pilot()
        self.host.gate_results = [
            GateResult("red", "local validation failed", "assert False"),
            GateResult("red", "local validation failed", "assert False"),
        ]
        self._run_worker_to_validate()
        payload = self.runtime.state.load()
        payload["records"]["secretary-510-pilot"]["worker_continuation"] = {}
        self.runtime.state.save(payload)
        self.host.fail_stop_head_reason = "Orca cannot confirm terminal stop"

        stopped = self.runtime.tick(self.selector)

        self.assertEqual(stopped["action"], "worker-stop-unconfirmed")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(
            record["worker_continuation"]["stage"],
            WorkerContinuationStage.RED_TRANSITION_PENDING.value,
        )

        self.host.fail_stop_head_reason = ""
        retried = self.runtime.tick(self.selector)

        self.assertEqual(retried["action"], "gate-red-rework")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_failed_retention_with_an_unconfirmed_stop_never_enters_validate(self) -> None:
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.host.fail_retain_worker_reason = "head is gone"
        self.host.fail_stop_head_reason = "Orca cannot confirm terminal stop"
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="done", request_id="worker-done-unconfirmed-retention",
        )

        outcome = self.runtime.tick(self.selector)

        self.assertEqual(outcome["action"], "worker-stop-unconfirmed")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_gate_red_bounces_card_to_worker(self) -> None:
        self.start_pilot()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self._run_worker_to_validate()

        gated = self.runtime.tick(self.selector)

        self.assertEqual(gated["action"], "gate-red-rework")
        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "in_progress")
        self.assertIn("The mechanical validation gate is red", task["comments"][-2]["body"])
        self.assertIn("continuation: replacement", task["comments"][-1]["body"])
        self.assertEqual(self.host.reviews, [])
        # worker prepared once at claim, once on the gate-red relaunch
        self.assertEqual(self.host.prepared, ["secretary-510-pilot", "secretary-510-pilot"])

    def test_repeated_gate_red_for_the_same_reason_is_marked_as_a_second_pass(self) -> None:
        """secretary-766: a second bounce for the identical failure must say so, or it reads to
        the worker (and the PO) as if `restart_worker` silently did nothing the first time."""
        self.start_pilot()
        self.host.gate_results = [
            GateResult("red", "local validation failed", "assert False"),
            GateResult("red", "local validation failed", "assert False"),
        ]
        self._run_worker_to_validate()
        first = self.runtime.tick(self.selector)
        self.assertEqual(first["action"], "gate-red-rework")
        self.assertNotIn("Repeat return", self.reader.show("secretary-510-pilot")["comments"][-1]["body"])

        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.host.commit = "newc0ffee1234567"
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="fixed",
            request_id=_attempt_request_id(
                record["attempt_id"], "worker-report-done", "secretary-510-pilot", str(record["review_baseline"]),
            ),
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")

        second = self.runtime.tick(self.selector)

        self.assertEqual(second["action"], "gate-red-rework")
        self.assertIn("Repeat return", self.reader.show("secretary-510-pilot")["comments"][-2]["body"])

    def test_repeated_github_gate_red_for_the_same_reason_survives_a_new_sha(self) -> None:
        """secretary-766 review: a GitHub gate's rendered detail always carries the head SHA,
        which changes on every rework commit, so repeat detection keyed on that text never fires
        twice. It must key on the fingerprint (job/step/error text) instead."""
        self.start_pilot()
        self.host.gate_results = [
            GateResult(
                "red", 'CI red: job "tests" failed on `pipeline/x` @ `aaa111`', "AssertionError: boom",
                fingerprint="ci-boom",
            ),
            GateResult(
                "red", 'CI red: job "tests" failed on `pipeline/x` @ `bbb222`', "AssertionError: boom",
                fingerprint="ci-boom",
            ),
        ]
        self._run_worker_to_validate()
        first = self.runtime.tick(self.selector)
        self.assertEqual(first["action"], "gate-red-rework")
        self.assertNotIn("Repeat return", self.reader.show("secretary-510-pilot")["comments"][-1]["body"])

        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.host.commit = "newc0ffee1234567"
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="fixed",
            request_id=_attempt_request_id(
                record["attempt_id"], "worker-report-done", "secretary-510-pilot", str(record["review_baseline"]),
            ),
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")

        second = self.runtime.tick(self.selector)

        self.assertEqual(second["action"], "gate-red-rework")
        self.assertIn("Repeat return", self.reader.show("secretary-510-pilot")["comments"][-2]["body"])

    def test_gate_red_with_a_different_local_error_is_not_marked_as_a_repeat(self) -> None:
        """secretary-766 review: two distinct local-gate failures must not be conflated into a
        'same reason' repeat just because both summaries read 'local validation failed'."""
        self.start_pilot()
        self.host.gate_results = [
            GateResult("red", "local validation failed", "assert False"),
            GateResult("red", "local validation failed", "TypeError: boom"),
        ]
        self._run_worker_to_validate()
        first = self.runtime.tick(self.selector)
        self.assertEqual(first["action"], "gate-red-rework")

        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.host.commit = "newc0ffee1234567"
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="fixed",
            request_id=_attempt_request_id(
                record["attempt_id"], "worker-report-done", "secretary-510-pilot", str(record["review_baseline"]),
            ),
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")

        second = self.runtime.tick(self.selector)

        self.assertEqual(second["action"], "gate-red-rework")
        self.assertNotIn("Repeat return", self.reader.show("secretary-510-pilot")["comments"][-1]["body"])

    def test_done_at_a_gate_rejected_sha_is_returned_for_rework(self) -> None:
        self.start_pilot()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "gate-red-rework")
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="nothing changed", request_id="worker-done-stale",
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["action"], "stale-done-rework")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertIn("was already rejected", self.reader.show("secretary-510-pilot")["comments"][-1]["body"])
        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(record["rejected_sha"], self.host.commit)
        self.assertEqual(record["rejected_done_reports"], 1)

    def test_done_after_a_new_commit_is_accepted_after_stale_done_rework(self) -> None:
        self.start_pilot()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "gate-red-rework")
        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="nothing changed",
            request_id=_attempt_request_id(
                record["attempt_id"],
                "worker-report-done",
                "secretary-510-pilot",
                str(record["review_baseline"]),
            ),
        )
        self.assertEqual(self.runtime.tick(self.selector)["action"], "stale-done-rework")

        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.host.commit = "newc0ffee1234567"
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="fixed",
            request_id=_attempt_request_id(
                record["attempt_id"],
                "worker-report-done",
                "secretary-510-pilot",
                str(record["review_baseline"]),
            ),
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["to"], "validate")
        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(record["rejected_done_reports"], 0)

    def test_second_done_at_a_rejected_sha_blocks_the_card(self) -> None:
        self.start_pilot()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "gate-red-rework")
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="nothing changed", request_id="worker-done-stale-one",
        )
        self.assertEqual(self.runtime.tick(self.selector)["action"], "stale-done-rework")
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="still nothing changed", request_id="worker-done-stale-two",
        )

        result = self.runtime.tick(self.selector)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "blocked")
        self.assertIn("reported done twice", self.reader.show("secretary-510-pilot")["comments"][-1]["body"])

    def test_gate_red_scrubs_secrets_in_bounce_comment(self) -> None:
        self.start_pilot()
        self.host.gate_results = [
            GateResult("red", "local validation failed", "API_TOKEN=super-secret-value boom")
        ]
        self._run_worker_to_validate()

        self.runtime.tick(self.selector)

        body = self.reader.show("secretary-510-pilot")["comments"][-2]["body"]
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

        result = self._park_and_decide("release")

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

        self._park_and_decide("release")

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
        """The merge push is rejected when the branch is not a fast-forward of main.

        On a card that merges on its own tick there is no parked state to hold it in, so the
        card lands in Blocked: an escaping HostError would leave a green verdict in Validate and
        every later tick would retry the same doomed merge with the worker terminals still up.
        A card with an observer holds the decision instead, which is the test below.
        """
        self.start_pilot()
        self.unobserved_card()
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

        result = self._park_and_decide("rework")

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

    def routing_history(self) -> list:
        return routing_attempts(
            TaskAudit(self.data_dir).events("secretary-510-pilot", kind="routing")
        )

    def test_both_attempts_keep_their_head_pair_in_the_journal(self) -> None:
        """secretary-716: a finished card must still say who worked and who reviewed each attempt.

        The board cannot answer this: `resolved_review_head` is cleared on the way out of Validate
        and the whole routing block is reset on the way back to Ready. So the append-only journal is
        the record, and a second round adds an attempt instead of overwriting the first.
        """
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot",
            kind="done", body="first", request_id="worker-done-attempt-1",
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot",
            kind="red", body="fix it", request_id="review-red-attempt-1",
        )
        # The registry is re-pinned between the two rounds. Attempt 1 keeps the model it actually
        # ran on; only attempt 2 sees the new pin.
        self.catalog.profiles["codex"] = dict(self.catalog.profiles["codex"], model="gpt-6-terra")
        self.assertEqual(self._park_and_decide("rework")["action"], "rework-started")
        # The rework produced new work; a done report on the rejected SHA would bounce instead.
        self.host.commit = "attempt-two-c0ffee"
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot",
            kind="done", body="reworked", request_id="worker-done-attempt-2",
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot",
            kind="green", body="ok", request_id="review-green-attempt-2",
        )
        self.assertEqual(
            self._park_and_decide("release", request_id="decision-release-attempt-2")["to"], "done"
        )

        card = self.reader.show("secretary-510-pilot")
        self.assertEqual(card["state"], "done")
        self.assertIsNone(
            card["routing"]["resolved_review_head"],
            "the board is expected to have dropped the reviewer head; the journal is the record",
        )
        history = self.routing_history()
        self.assertEqual([attempt.attempt for attempt in history], [1, 2])
        self.assertEqual([attempt.outcome for attempt in history], ["red", "green"])
        first, second = history
        self.assertEqual((first.worker.head, first.reviewer.head), ("codex", "codex-reviewer"))
        self.assertEqual((second.worker.head, second.reviewer.head), ("codex", "codex-reviewer"))
        self.assertEqual(first.worker.model, "gpt-5.6-terra")
        self.assertEqual(second.worker.model, "gpt-6-terra", "the round must keep its own snapshot")
        self.assertEqual(first.reviewer.effort, "extra")
        self.assertEqual(first.reviewer.account, "openai-subscription")
        self.assertEqual(first.worker.codex_mode, "exec")

    def test_adoption_keeps_the_heads_the_card_was_claimed_with(self) -> None:
        """The head is decided once, at claim. A dispatcher that lost its record and picks the card
        back up must resume the attempt's own pair: re-reading the role default here would move the
        reviewer of a running attempt to whatever the registry says now, and the journal would
        faithfully record a head that was never the one this attempt was claimed with."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot",
            kind="done", body="done", request_id="worker-done-adopted",
        )
        self._drop_records_and_restart_attempt()
        self.catalog.role_defaults = {"new_card": "claude-opus", "reviewer": "claude-opus"}

        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")

        record = self.runtime.state.records(self.runtime.state.load())["secretary-510-pilot"]
        self.assertEqual((record.head, record.review_head), ("codex", "codex-reviewer"))
        attempt = self.routing_history()[-1]
        self.assertEqual(attempt.worker.head, "codex")
        self.assertEqual(attempt.reviewer.head, "codex-reviewer")
        self.assertEqual(attempt.reviewer.model, "gpt-5.6-terra")

    def test_adopted_worker_relaunch_keeps_the_head_the_card_was_claimed_with(self) -> None:
        """Same loss inside the attempt that claimed the card: the dispatcher re-verifies the claim
        and brings the worker back up. That bring-up belongs to the running attempt, so it uses the
        claimed head rather than the role default as it reads now."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        payload = self.runtime.state.load()
        self.runtime.state.put_records(payload, {})
        self.runtime.state.save(payload)
        self.catalog.role_defaults = {"new_card": "claude-opus", "reviewer": "claude-opus"}

        relaunched = self.runtime.tick(self.selector)

        self.assertEqual(relaunched["status"], "ok", relaunched)
        record = self.runtime.state.records(self.runtime.state.load())["secretary-510-pilot"]
        self.assertEqual(record.head, "codex")
        self.assertEqual(self.routing_history()[-1].worker.head, "codex")

    def test_adoption_of_a_card_claimed_before_heads_were_recorded_uses_the_current_default(
        self,
    ) -> None:
        """A card claimed by an older dispatcher carries no resolved pair. There is nothing to
        resume, so adoption falls back to the current decision rather than refusing to pick it up."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot",
            kind="done", body="done", request_id="worker-done-legacy",
        )
        self._drop_records_and_restart_attempt()
        self.board.metadata[12].update({"resolved_head": "", "resolved_review_head": ""})
        self.catalog.role_defaults = {"new_card": "claude-opus", "reviewer": "claude-opus"}

        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")

        record = self.runtime.state.records(self.runtime.state.load())["secretary-510-pilot"]
        self.assertEqual((record.head, record.review_head), ("claude-opus", "claude-opus"))

    def test_adoption_of_a_card_whose_claimed_head_left_the_registry_blocks(self) -> None:
        """The claimed head is gone from `heads.yaml` and the dispatcher lost its record. There is
        no substitution at bring-up in this installation, so the attempt stops: launching today's
        role default would put a head the claim never picked into the running attempt. The card goes
        to Blocked for a human, and the journal keeps the attempt as the last real bring-up left
        it."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot",
            kind="done", body="done", request_id="worker-done-lost-profile",
        )
        self._drop_records_and_restart_attempt()
        before = self.routing_history()
        self.catalog.profiles.pop("codex")
        self.catalog.role_defaults = {"new_card": "claude-opus", "reviewer": "claude-opus"}

        blocked = self.runtime.tick(self.selector)

        self.assertEqual(blocked["status"], "blocked", blocked)
        self.assertEqual(blocked["reason"], "claimed head is unavailable")
        card = self.reader.show("secretary-510-pilot")
        self.assertEqual(card["state"], "blocked")
        self.assertIn("claimed head is unavailable", card["comments"][-1]["body"])
        self.assertIn("codex", card["comments"][-1]["body"])
        self.assertNotIn("secretary-510-pilot", self.runtime.state.load()["records"])
        self.assertEqual(self.host.prepared, ["secretary-510-pilot"], "nothing new may be launched")
        self.assertEqual(self.host.reviews, [])
        history = self.routing_history()
        self.assertEqual(
            [[run.head for run in attempt.worker_runs] for attempt in history],
            [[run.head for run in attempt.worker_runs] for attempt in before],
            "a head that never launched must not be appended to the attempt",
        )
        self.assertIsNone(history[-1].reviewer)

    def test_reviewer_without_a_pinned_model_records_the_model_the_cli_resolves(self) -> None:
        """`claude-default` pins no model: the launcher renders `claude` with no `--model` and the
        CLI resolves one at startup. The journal has to name that model, or the profile id becomes
        the only historical key, which is exactly what this telemetry exists to avoid."""
        self.start_pilot()
        self.board.metadata[12]["review_head"] = "claude-default"
        with tempfile.TemporaryDirectory() as config:
            (Path(config) / "settings.json").write_text(
                json.dumps({"model": "opus"}), encoding="utf-8"
            )
            env = {
                "CLAUDE_CONFIG_DIR": config,
                "CLAUDE_MANAGED_SETTINGS": str(Path(config) / "absent.json"),
            }
            with mock.patch.dict(os.environ, env):
                os.environ.pop("ANTHROPIC_MODEL", None)
                self._run_worker_to_validate()
                self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")

        reviewer = self.routing_history()[-1].reviewer
        self.assertEqual((reviewer.head, reviewer.adapter), ("claude-default", "claude"))
        self.assertEqual((reviewer.model, reviewer.model_source), ("opus", "user_settings"))
        self.assertEqual(reviewer.account, "claude-subscription")

    def test_reviewer_model_the_cli_picks_itself_is_marked_not_left_blank(self) -> None:
        """Nothing pins a model anywhere: the CLI falls back to its own built-in default, which the
        dispatcher cannot read. The record says the model was resolved at runtime instead of
        carrying a silent empty string."""
        self.start_pilot()
        self.board.metadata[12]["review_head"] = "claude-default"
        with tempfile.TemporaryDirectory() as empty:
            env = {
                "CLAUDE_CONFIG_DIR": str(Path(empty) / "none"),
                "CLAUDE_MANAGED_SETTINGS": str(Path(empty) / "absent.json"),
            }
            with mock.patch.dict(os.environ, env):
                os.environ.pop("ANTHROPIC_MODEL", None)
                self._run_worker_to_validate()
                self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")

        reviewer = self.routing_history()[-1].reviewer
        self.assertEqual((reviewer.model, reviewer.model_source), ("", "cli_default"))

    def test_card_requeued_to_ready_starts_a_new_attempt(self) -> None:
        """An operator-approved retry is a second attempt, not a rewrite of the first: the Ready
        reset wipes the card's routing metadata, so only the journal can tell the two apart."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot",
            kind="blocked", body="stuck", request_id="worker-blocked-attempt-1",
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "blocked")
        self.writer.move(
            role="po", actor="operator", reference="secretary-510-pilot", sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
            target="ready", reason="retry", request_id="po-requeue-attempt-2",
        )
        self.board.metadata[12]["head"] = "claude-opus"

        self.assertEqual(self.runtime.tick(self.selector)["step"], "claim")

        history = self.routing_history()
        self.assertEqual([attempt.attempt for attempt in history], [1, 2])
        self.assertEqual(history[0].worker.head, "codex")
        self.assertEqual(history[1].worker.head, "claude-opus")
        self.assertEqual(history[1].worker.head_source, "card")

    def test_active_card_preempted_back_to_ready_starts_a_new_attempt(self) -> None:
        """A preempt out of in_progress is part of the documented workflow and nothing about it is
        blocked, so the retry-after-block path never sees it. The card still has to be claimed
        again, and the second bring-up has to reach the journal as its own attempt."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        first_attempt = self.runtime.state.load()["attempt_id"]
        payload = self.runtime.state.load()
        record = payload["records"]["secretary-510-pilot"]
        record["handle"] = ""
        record["worker_leaf"] = ""
        record["worker_pid_file"] = ""
        record["review_handle"] = ""
        record["review_leaf"] = ""
        record["review_pid_file"] = ""
        self.runtime.state.save(payload)
        Path(pid_file_path("worker", "secretary-510-pilot")).unlink(missing_ok=True)
        self.writer.move(
            role="po", actor="operator", reference="secretary-510-pilot", sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
            target="ready", reason="preempted", request_id="po-preempt-attempt-2",
        )
        self.board.metadata[12]["head"] = "claude-opus"

        claimed = self.runtime.tick(self.selector)

        self.assertEqual(claimed["step"], "claim")
        self.assertEqual(claimed["status"], "ok")
        self.assertNotEqual(claimed["attempt_id"], first_attempt)
        card = self.reader.show("secretary-510-pilot")
        self.assertEqual(card["state"], "in_progress")
        self.assertEqual(card["routing"]["resolved_worker_head"], "claude-opus")
        # The preempted head is not left running in the workspace the new round claims.
        self.assertEqual(self.host.stopped, ["secretary-510-pilot-pilot"])
        history = self.routing_history()
        self.assertEqual([attempt.attempt for attempt in history], [1, 2])
        self.assertEqual([attempt.worker.head for attempt in history], ["codex", "claude-opus"])

    def test_card_preempted_out_of_validate_starts_a_new_attempt(self) -> None:
        """Same for a card pulled back from validate: the first attempt keeps its reviewer, and the
        second is a new pair rather than an overwrite of the first."""
        self.start_pilot()
        self._run_worker_to_validate()
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        first_attempt = self.runtime.state.load()["attempt_id"]
        self.writer.move(
            role="po", actor="operator", reference="secretary-510-pilot", sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
            target="ready", reason="preempted in review", request_id="po-preempt-validate",
        )

        claimed = self.runtime.tick(self.selector)

        self.assertEqual(claimed["step"], "claim")
        self.assertNotEqual(claimed["attempt_id"], first_attempt)
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        # start_review already closed the worker pane, so only the reviewer of the preempted
        # attempt is still up. It has to go before a new worker takes over the same checkout.
        self.assertEqual(
            self.host.stopped_reviews,
            ["review:secretary-510-pilot"],
            "the preempted attempt's reviewer must not outlive the claim of the next one",
        )
        history = self.routing_history()
        self.assertEqual([attempt.attempt for attempt in history], [1, 2])
        self.assertEqual(history[0].reviewer.head, "codex-reviewer")
        self.assertIsNone(history[1].reviewer)

    def test_a_preempt_out_of_validate_drops_the_retained_session(self) -> None:
        """Retention follows the attempt, not the workspace. A preempt back to Ready ends the
        attempt, so the suspended worker is stopped and the next round gets a fresh head rather
        than the conversation that was frozen for the gate."""
        self.host.fail_resume_worker_reason = ""
        self.start_pilot()
        self._run_worker_to_validate()
        retained = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(
            retained["worker_continuation"]["stage"], WorkerContinuationStage.RETAINED.value
        )
        self.writer.move(
            role="po", actor="operator", reference="secretary-510-pilot", sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
            target="ready", reason="preempted while validating", request_id="po-preempt-retained",
        )

        claimed = self.runtime.tick(self.selector)

        self.assertEqual(claimed["step"], "claim")
        self.assertEqual(self.host.resumed_workers, [])
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        record = self.runtime.state.load()["records"]["secretary-510-pilot"]
        self.assertEqual(record["worker_continuation"], {})

    def test_worker_respawn_on_an_unchanged_head_stays_one_record(self) -> None:
        """A respawn inside a round is the same head coming back, not a second worker: the round
        keeps one launch record, and the journal does not read as two heads on one attempt."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.assertEqual(self.runtime.tick(self.selector)["action"], "waiting-worker-report")
        self._rewind_wait("worker", seconds=stall_seconds("worker") + 60)

        self.assertEqual(self.runtime.tick(self.selector)["action"], "worker-respawned")

        attempt = self.routing_history()[-1]
        self.assertEqual([run.head for run in attempt.worker_runs], ["codex"])

    def test_worker_respawned_onto_a_repinned_profile_is_a_second_record(self) -> None:
        """A respawn after a registry repin runs a different configuration, and the round's verdict
        belongs to that one. Both bring-ups stay in the journal."""
        self.start_pilot()
        self.runtime.tick(self.selector)
        self.assertEqual(self.runtime.tick(self.selector)["action"], "waiting-worker-report")
        self.catalog.profiles["codex"] = dict(self.catalog.profiles["codex"], effort="high")
        self._rewind_wait("worker", seconds=stall_seconds("worker") + 60)

        self.assertEqual(self.runtime.tick(self.selector)["action"], "worker-respawned")

        attempt = self.routing_history()[-1]
        self.assertEqual([run.effort for run in attempt.worker_runs], ["default", "high"])
        self.assertEqual(attempt.worker.effort, "high", "the round follows the head that is up")

    def test_reworked_card_reruns_the_gate_instead_of_coasting(self) -> None:
        """A gate-red bounce resets the pass; the next done report is fresh code and must be gated
        again. Reusing the stale green would ship exactly the regression the gate exists to stop."""
        self.start_pilot()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self._run_worker_to_validate()
        bounced = self.runtime.tick(self.selector)
        self.assertEqual(bounced["action"], "gate-red-rework")
        self.assertEqual(self.host.gate_calls, ["secretary-510-pilot"])

        self.host.commit = "gate-rework-c0ffee"
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

        relaunched = self._park_and_decide("rework")

        self.assertEqual(relaunched["action"], "rework-started")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self.host.prepared, ["secretary-510-pilot", "secretary-510-pilot"])
        self.assertEqual(
            self.host.stopped_reviews,
            ["review:secretary-510-pilot"],
            "a red verdict must end the reviewer's pane",
        )
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        self.assertEqual(
            self.host.stopped,
            [],
            "the green gate stops the retained worker before review; a red verdict stops only the reviewer",
        )
        self.assertEqual(self.host.torn_down, [], "rework must reuse the workspace, not tear it down")

        self.host.commit = "review-rework-accepted-c0ffee"
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
        self.assertNotIn(
            "stop_workspace",
            self.host.calls,
            "green handoff must leave the worktree's other panes alone",
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

        self.assertEqual(self._park_and_decide("rework")["action"], "rework-started")

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
        comments = self.reader.show("secretary-510-pilot")["comments"]
        self.assertTrue(any("a different state of the code" in comment["body"] for comment in comments))
        self.assertIn("continuation: replacement", comments[-1]["body"])

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

        done = self._park_and_decide("release")

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

        done = self._park_and_decide("release")

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
        self.assertEqual(
            self._park_and_decide("rework", request_id="decision-rework-round-1")["action"],
            "rework-started",
        )

        self.host.commit = "round-two-c0ffee"
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

        reworked = self._park_and_decide("rework", request_id="decision-rework-round-2")
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

        result = self._park_and_decide("release")

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

    # the no-observer ceiling (secretary-1033) --------------------------------

    def _unobserved_card_in_progress(self) -> None:
        """A claimed card whose sprint has nobody to decide for it, worker up and running."""
        self.host.fail_resume_worker_reason = ""
        self.start_pilot()
        self.unobserved_card()
        self.runtime.tick(self.selector)

    def _red_round(self, index: int) -> dict:
        """Drive the running worker round to a red review and hand back the tick that acts on it.

        Each round reports a fresh commit: a done report at the SHA the previous round's verdict
        rejected is bounced by the stale-done check and never reaches a reviewer.
        """
        self.host.commit = f"round{index}-c0ffee1234"
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="done", request_id=f"worker-done-{index}",
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
        self.assertEqual(self.runtime.tick(self.selector)["action"], "review-started")
        self._review_red(f"review-red-{index}")
        return self.runtime.tick(self.selector)

    def test_third_red_review_blocks_a_card_with_no_observer(self) -> None:
        """Criterion: a card nobody decides for consumes a bounded number of rounds and stops.

        The first two reds open another round exactly as before. The third does not: it names the
        ceiling, leaves the checkout and the branch where the round left them, and asks a person.
        """
        self.assertEqual(RED_REVIEW_CEILING, 3, "this test drives the ceiling by hand")
        self._unobserved_card_in_progress()
        workspace = self.data_dir / "workspaces" / "secretary-510-pilot-pilot"

        self.assertEqual(self._red_round(1)["action"], "review-red-reused-worker")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        self.assertEqual(self._red_round(2)["action"], "review-red-reused-worker")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

        third = self._red_round(3)

        self.assertEqual(third["status"], "blocked")
        self.assertEqual(third["reason"], "red review ceiling reached")
        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "blocked")
        blocked_reason = task["comments"][-1]["body"]
        self.assertIn("3 substantive red reviews", blocked_reason)
        self.assertIn("no-observer ceiling", blocked_reason)
        # The ceiling stops the card; it does not throw the round's work away.
        self.assertTrue(workspace.is_dir(), "the workspace survives the ceiling")
        self.assertEqual(self.host.torn_down, [], "the checkout is kept for whoever unblocks it")
        self.assertEqual(
            self.host.calls.count("resume_worker"), 2, "the third red opened no further round"
        )
        # The verdict still happened: it is recorded against the heads that earned it.
        verdicts = [
            event for event in self.audit_events()
            if event["kind"] == "routing" and event["payload"]["phase"] == "verdict"
        ]
        self.assertEqual([event["payload"]["outcome"] for event in verdicts], ["red", "red", "red"])

    def test_a_replayed_red_verdict_does_not_advance_the_counter_twice(self) -> None:
        """Idempotence: the same verdict is one red however many times it is written or read.

        A verdict retried under its own request id creates no second comment, and a tick that
        re-reads the board counts the same comments, so the second round still opens.
        """
        self._unobserved_card_in_progress()
        self._red_round(1)
        self._red_round(2)
        # The reviewer's client retried the same verdict, and the dispatcher tick ran again.
        self._review_red("review-red-2")
        self._review_red("review-red-2")
        task = self.reader.show("secretary-510-pilot")

        self.assertEqual(red_review_count(task), 2)

        third = self._red_round(3)

        self.assertEqual(third["status"], "blocked")
        self.assertEqual(third["reason"], "red review ceiling reached")

    def test_the_red_counter_survives_a_dispatcher_restart(self) -> None:
        """The count is on the card, so a dispatcher that lost every record still finds it.

        Two reds, then the records are dropped and the attempt id is fresh: the tick that adopts
        the card re-reads its comments, counts the third red as the third, and blocks. Nothing
        restart-local is consulted.
        """
        self._unobserved_card_in_progress()
        self._red_round(1)
        self._red_round(2)

        self._drop_records_and_restart_attempt()

        third = self._red_round(3)

        self.assertEqual(third["status"], "blocked")
        self.assertEqual(third["reason"], "red review ceiling reached")
        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(task["state"], "blocked")
        self.assertIn("3 substantive red reviews", task["comments"][-1]["body"])

    def test_a_mechanical_gate_red_does_not_count_toward_the_ceiling(self) -> None:
        """The separation the sprint budget already makes: a red gate is not a red review.

        Two red reviews with a red gate bounce between them leaves the counter at two, so the
        card gets its third worker round instead of being blocked by CI's opinion.
        """
        self._unobserved_card_in_progress()
        self._red_round(1)
        self.host.commit = "gate-red-c0ffee1234"
        self.host.gate_results = [GateResult("red", "pytest failed", "E   assert False")]
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
            body="done", request_id="worker-done-gate-red",
        )
        self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
        gated = self.runtime.tick(self.selector)
        self.assertIn("gate-red", gated["action"])

        self.assertEqual(red_review_count(self.reader.show("secretary-510-pilot")), 1)

        self.assertEqual(self._red_round(2)["action"], "review-red-reused-worker")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_an_observed_card_is_never_blocked_by_the_red_review_counter(self) -> None:
        """Criterion: with an observer the ceiling is the observer's judgement.

        Three reds with a rework decision on each: the card parks every time and never blocks,
        because a counter that fired here would be deciding a card the observer is still holding.
        """
        self.host.fail_resume_worker_reason = ""
        self.start_pilot()
        self.runtime.tick(self.selector)
        for index in (1, 2, 3):
            self.host.commit = f"round{index}-c0ffee1234"
            self.writer.report(
                role="worker", actor="worker", reference="secretary-510-pilot", kind="done",
                body="done", request_id=f"worker-done-{index}",
            )
            self.assertEqual(self.runtime.tick(self.selector)["to"], "validate")
            self.runtime.tick(self.selector)
            self._review_red(f"review-red-{index}")
            reworked = self._park_and_decide("rework", request_id=f"decision-rework-{index}")
            self.assertEqual(reworked["action"], "review-red-reused-worker")

        task = self.reader.show("secretary-510-pilot")
        self.assertEqual(red_review_count(task), 3)
        self.assertEqual(task["state"], "in_progress", "the observer decides, not the counter")


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

        self.assertEqual(len(commands), 2, "one done and one blocked command")
        for command in commands:
            self.assertIn("--body-file /tmp/secretary-report-secretary-510-pilot-0.md", command)
            self.assertNotIn("<file>", command)

    def test_worker_prompt_limits_blocked_reports_to_an_obvious_wrong_cut(self) -> None:
        doc = self.host._worker_task_doc(self.task, "main", "attempt-1")

        self.assertIn("only for an obvious wrong cut", doc)
        self.assertIn("requires a new durable protocol, product contract, or trust", doc)
        self.assertIn("Difficulty or size alone is not a reason to stop", doc)
        self.assertIn("conflict and the observer decision needed", doc)

    def test_worker_prompt_forbids_writing_to_the_live_installation(self) -> None:
        doc = self.host._worker_task_doc(self.task, "main", "attempt-1")

        self.assertIn("read the live installation; they never write to it", doc)
        self.assertIn("deploys, syncs, provisions or reconciles live state", doc)
        self.assertIn("--product-root .", doc)
        self.assertIn("what you could not verify", doc)

    def test_worker_prompt_makes_an_existing_test_change_reportable(self) -> None:
        doc = self.host._worker_task_doc(self.task, "main", "attempt-1")

        self.assertIn("Do not change or weaken an existing test", doc)
        self.assertIn("name the test, what it", doc)
        self.assertIn("silently rewritten assertion", doc)

    def test_review_prompt_refuses_a_fixture_as_backend_evidence(self) -> None:
        doc = self.host._review_prompt(self.task, "attempt-1", 3)

        self.assertIn("a passing fixture is not", doc)
        self.assertIn("same wrong assumption as the code", doc)
        self.assertIn("no end-to-end check against the real backend", doc)
        self.assertIn("which assumption stays unverified", doc)

    def test_review_prompt_requires_evidence_for_every_red_blocker(self) -> None:
        doc = self.host._review_prompt(self.task, "attempt-1", 3)

        self.assertIn("concrete reachable scenario", doc)
        self.assertIn("violated acceptance", doc)
        self.assertIn("whether this branch introduced", doc)
        self.assertIn("compatibility promise", doc)
        self.assertIn("do not silently widen the supported boundary or decide sprint scope", doc)

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
            with mock.patch.object(
                self.host, "_launch", return_value=LaunchedHead("term:review", "codex-reviewer")
            ):
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
    # Which model a codex head runs on is installation configuration, not something the shipped
    # registry decides, so the model-pinning cases here run against a fixture registry of their own.
    PINNED_REGISTRY = {
        "resources": {"openai-sub": {"account": "openai-subscription"}},
        "profiles": {
            "pinned-terra": {"resource": "openai-sub", "adapter": "codex",
                             "model": "gpt-5.6-terra", "effort": "extra"},
        },
        "role_defaults": {"new_card": "pinned-terra"},
    }

    def test_a_card_head_override_launches_that_profiles_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            catalog = object.__new__(InstanceCatalog)
            catalog._heads = self.PINNED_REGISTRY  # type: ignore[attr-defined]
            task = {"routing": {"head_override": "pinned-terra"}}

            head = catalog.worker_head(task)  # type: ignore[attr-defined]
            command = catalog.head_command(  # type: ignore[attr-defined]
                head,
                "TASK.md",
                workspace=str(workspace),
                role="worker",
            )

        self.assertEqual(head, "pinned-terra")
        self.assertIn("-m gpt-5.6-terra", command)

    def test_head_run_snapshots_the_launched_profiles_configuration(self) -> None:
        """The launch record must carry the configuration, not just the profile id: two profiles
        can be one model at different effort, so the id alone cannot answer which head ran."""
        catalog = object.__new__(InstanceCatalog)
        catalog._heads = self.PINNED_REGISTRY  # type: ignore[attr-defined]

        worker = catalog.head_run(  # type: ignore[attr-defined]
            {"routing": {"head_override": "pinned-terra", "codex_launch_mode": "tui"}}, role="worker"
        )

        self.assertEqual(worker.to_json(), {
            "role": "worker", "head": "pinned-terra", "head_source": "card",
            "adapter": "codex", "model": "gpt-5.6-terra", "model_source": "profile",
            "effort": "extra",
            # The card pinned the launch mode, so the record shows the mode the head really ran in.
            "codex_mode": "tui", "resource": "openai-sub", "account": "openai-subscription",
        })

    def test_head_run_reads_the_reviewer_role_default_from_the_registry(self) -> None:
        """Which profile reviews is configuration and moves with the quota that is up; what this
        asserts is that the record carries that profile's real configuration rather than its id."""
        catalog = object.__new__(InstanceCatalog)
        catalog._heads = canonical_heads(Path(__file__).resolve().parents[1])  # type: ignore[attr-defined]

        reviewer = catalog.head_run({"routing": {}}, role="reviewer")  # type: ignore[attr-defined]

        expected = catalog._heads["role_defaults"]["reviewer"]  # type: ignore[attr-defined]
        profile = catalog._heads["profiles"][expected]  # type: ignore[attr-defined]
        self.assertEqual(reviewer.head_source, "role_default")
        self.assertEqual(reviewer.head, expected)
        self.assertEqual(reviewer.effort, profile.get("effort", ""))
        self.assertEqual(reviewer.model, profile.get("model", ""))

    def test_head_run_snapshots_the_cli_model_for_a_profile_that_pins_none(self) -> None:
        """`claude-default` pins no model, so the CLI picks one from its settings at startup. The
        record has to name that model: an empty field would make the profile id the only historical
        key, which is exactly what this telemetry exists to avoid."""
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "claude-config"
            config.mkdir()
            (config / "settings.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
            workspace = Path(tmp) / "workspace"
            (workspace / ".claude").mkdir(parents=True)
            catalog = object.__new__(InstanceCatalog)
            catalog._heads = canonical_heads(Path(__file__).resolve().parents[1])  # type: ignore[attr-defined]
            card = {"routing": {"review_head_override": "claude-default"}}
            env = {"CLAUDE_CONFIG_DIR": str(config), "CLAUDE_MANAGED_SETTINGS": str(config / "none.json")}
            with mock.patch.dict(os.environ, env):
                os.environ.pop("ANTHROPIC_MODEL", None)
                user = catalog.head_run(card, role="reviewer", workspace=str(workspace))  # type: ignore[attr-defined]
                # The workspace's own settings win over the user's, as they do for the CLI.
                (workspace / ".claude" / "settings.json").write_text(
                    json.dumps({"model": "sonnet"}), encoding="utf-8"
                )
                project = catalog.head_run(card, role="reviewer", workspace=str(workspace))  # type: ignore[attr-defined]

        self.assertEqual((user.head, user.adapter), ("claude-default", "claude"))
        self.assertEqual((user.model, user.model_source), ("opus", "user_settings"))
        self.assertEqual((project.model, project.model_source), ("sonnet", "project_settings"))

    def test_claude_snapshot_reads_the_env_the_role_wrapper_delivers(self) -> None:
        """The head does not run in the dispatcher's environment. `wrap_role_shell_command` hands
        it to `secretary.role_env exec`, which drops every `runtime.env` variable that is not
        role-allowlisted, and `ANTHROPIC_MODEL` is not. A snapshot read from `os.environ` would
        journal a model the launched CLI never receives, so the record is taken from the env the
        wrapper delivers and checked here against what the wrapped process actually gets."""
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / ".claude").mkdir(parents=True)
            (home / ".claude" / "settings.json").write_text(
                json.dumps({"model": "sonnet"}), encoding="utf-8"
            )
            workspace = root / "workspace"
            workspace.mkdir()
            runtime = root / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://board.invalid\n"
                "KANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=board-token\n"
                "ANTHROPIC_MODEL=opus\n",
                encoding="utf-8",
            )
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(home),
                "SECRETARY_RUNTIME_ENV_FILE": str(runtime),
                "TA_SECRETARY_REPO": str(repo),
                "ANTHROPIC_MODEL": "opus",
                "CLAUDE_MANAGED_SETTINGS": str(root / "no-managed.json"),
                "KANBOARD_URL": "http://board.invalid",
                "KANBOARD_API_USER": "jsonrpc",
                "KANBOARD_API_TOKEN": "board-token",
            }
            catalog = object.__new__(InstanceCatalog)
            catalog._heads = canonical_heads(repo)  # type: ignore[attr-defined]
            card = {"routing": {"review_head_override": "claude-default"}}
            probe = (
                "python3 -c 'import json,sys;"
                "from secretary.dispatcher_launcher import claude_launch_model;"
                'print(json.dumps(claude_launch_model({"adapter": "claude"}, workspace=sys.argv[1])))\' '
                + shlex.quote(str(workspace))
            )
            with mock.patch.dict(os.environ, env, clear=True):
                run = catalog.head_run(  # type: ignore[attr-defined]
                    card, role="reviewer", workspace=str(workspace)
                )
                # What the dispatcher's own environment says, which is the value the wrapper drops.
                naive = claude_launch_model({"adapter": "claude"}, workspace=str(workspace))
                # The wrapper binds names out of the launcher's own environment, so it has to be
                # rendered inside it: rendered outside, a live host's SECRETARY_RUNTIME_ENV_FILE
                # would reach the launched process and the fixture's runtime.env never would.
                wrapped = _wrap_role_shell_command("reviewer", probe)

            delivered = subprocess.run(
                ["/bin/sh", "-c", wrapped],
                capture_output=True,
                text=True,
                env=env,
                cwd=tmp,
            )

        self.assertEqual(delivered.returncode, 0, delivered.stderr)
        self.assertEqual(naive, ("opus", "env:ANTHROPIC_MODEL"))
        self.assertEqual(json.loads(delivered.stdout.strip().splitlines()[-1]), ["sonnet", "user_settings"])
        self.assertEqual((run.model, run.model_source), ("sonnet", "user_settings"))

    def test_claude_launch_model_reports_the_cli_default_it_cannot_name(self) -> None:
        """Nothing pinned anywhere: the CLI falls back to its own built-in default, which the
        dispatcher has no way to read. The record says so instead of inventing a model id."""
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CLAUDE_CONFIG_DIR": str(Path(tmp) / "empty"),
                "CLAUDE_MANAGED_SETTINGS": str(Path(tmp) / "no-managed.json"),
            }
            with mock.patch.dict(os.environ, env):
                os.environ.pop("ANTHROPIC_MODEL", None)
                unpinned = claude_launch_model({"adapter": "claude"}, workspace=tmp)
                os.environ["ANTHROPIC_MODEL"] = "opus"
                from_env = claude_launch_model({"adapter": "claude"}, workspace=tmp)
                pinned = claude_launch_model({"adapter": "claude", "model": "fable"}, workspace=tmp)

        self.assertEqual(unpinned, ("", "cli_default"))
        self.assertEqual(from_env, ("opus", "env:ANTHROPIC_MODEL"))
        # A profile that pins a model renders `--model`, which outranks the environment.
        self.assertEqual(pinned, ("fable", "profile"))

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

    def _codex_worktree(self, tmp: Path) -> tuple[Path, Path]:
        """A worktree of a repo that sits somewhere else, the shape an observer workspace has."""
        repo = tmp / "root"
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", "-b", "obs", str(repo)], check=True)
        subprocess.run(
            [
                "git", "-C", str(repo),
                "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "--quiet", "--allow-empty", "-m", "root",
            ],
            check=True,
        )
        workspace = tmp / "workspaces" / "sprint-1"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--quiet", "-b", "w", str(workspace)],
            check=True,
        )
        return repo, workspace

    def test_codex_trust_records_the_repository_root_the_dialog_asks_about(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            repo, workspace = self._codex_worktree(tmp)
            home = tmp / "codex-home"
            home.mkdir()
            config = home / "config.toml"
            config.write_text(
                '# keep me\nmodel_reasoning_summary = "auto"\n\n'
                '[projects."/already/trusted"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )

            ensure_codex_workspace_trusted({"adapter": "codex", "codex_home": str(home)}, str(workspace))
            after_first = config.read_text(encoding="utf-8")
            with mock.patch("secretary.dispatcher_launcher.os.replace") as replace:
                ensure_codex_workspace_trusted(
                    {"adapter": "codex", "codex_home": str(home)}, str(workspace)
                )

        data = tomllib.loads(after_first)
        # codex asks about the repository root of the directory it starts in, so that is the entry
        # that has to be there; the workspace itself covers a workspace that is no repo at all.
        self.assertEqual(data["projects"][str(repo.resolve())]["trust_level"], "trusted")
        self.assertEqual(data["projects"][str(workspace.resolve())]["trust_level"], "trusted")
        self.assertEqual(data["projects"]["/already/trusted"]["trust_level"], "trusted")
        self.assertEqual(data["model_reasoning_summary"], "auto")
        self.assertIn("# keep me", after_first)
        replace.assert_not_called()

    def test_codex_trust_writes_the_workspace_alone_outside_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            workspace = tmp / "plain"
            workspace.mkdir()
            config = tmp / "config.toml"

            ensure_codex_workspace_trusted({"adapter": "codex"}, str(workspace), config)

            data = tomllib.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(list(data["projects"]), [str(workspace.resolve())])

    def test_codex_trust_leaves_a_path_somebody_kept_untrusted(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            workspace = tmp / "plain"
            workspace.mkdir()
            config = tmp / "config.toml"
            original = f'[projects.{json.dumps(str(workspace.resolve()))}]\ntrust_level = "untrusted"\n'
            config.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "trust_level"):
                ensure_codex_workspace_trusted({"adapter": "codex"}, str(workspace), config)

            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_codex_trust_rejects_corrupt_or_symlinked_config(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            workspace = tmp / "plain"
            workspace.mkdir()
            corrupt = tmp / "corrupt.toml"
            corrupt.write_text("[projects\n", encoding="utf-8")
            target = tmp / "target.toml"
            target.write_text("", encoding="utf-8")
            symlink = tmp / "link.toml"
            symlink.symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "cannot read codex config"):
                ensure_codex_workspace_trusted({"adapter": "codex"}, str(workspace), corrupt)
            with self.assertRaisesRegex(RuntimeError, "refusing symlinked codex config"):
                ensure_codex_workspace_trusted({"adapter": "codex"}, str(workspace), symlink)

    def test_codex_trust_fails_closed_when_atomic_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            workspace = tmp / "plain"
            workspace.mkdir()
            config = tmp / "config.toml"
            original = 'model_reasoning_summary = "auto"\n'
            config.write_text(original, encoding="utf-8")

            with mock.patch("secretary.dispatcher_launcher.os.replace", side_effect=OSError("boom")):
                with self.assertRaisesRegex(RuntimeError, "cannot update codex config"):
                    ensure_codex_workspace_trusted({"adapter": "codex"}, str(workspace), config)

            self.assertEqual(config.read_text(encoding="utf-8"), original)

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

    def test_rework_task_doc_delivers_latest_gate_red_cause(self) -> None:
        """secretary-766: the worker must see why the mechanical gate bounced the card — the
        failing job/step and an error-focused log fragment — not just the reviewer findings."""
        with tempfile.TemporaryDirectory() as tmp:
            host = GitBranchHost(Path(tmp))
            base_task = {
                "ref": "secretary-510-pilot",
                "project": "secretary",
                "description": "body",
                "workspace": {"base_branch": "main"},
            }
            self.assertNotIn("Mechanical gate failure", host._worker_task_doc(base_task, "main", "a", 0))
            gated = {
                **base_task,
                "comments": [
                    {
                        "marker": "dispatcher",
                        "body": (
                            '[dispatcher]\nThe mechanical validation gate is red: CI red: job "tests", '
                            'step "pytest" failed on `pipeline/secretary-510-pilot` @ `abc123`. The card '
                            'is back in In progress for rework.\nTail:\n```\nAssertionError: boom\n```'
                        ),
                    },
                ],
            }
            doc = host._worker_task_doc(gated, "main", "a", 1)
        self.assertIn("Mechanical gate failure", doc)
        self.assertIn('job "tests", step "pytest"', doc)
        self.assertIn("AssertionError: boom", doc)

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

    def test_complete_green_refreshes_checkout_from_default_branch_for_stacked_base(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            host = _RecordingMergeHost(Path(tmp), {"validation": {"ci": "github"}})
            record = SimpleNamespace(workspace=str(Path(tmp) / "ws"))
            host.complete_green(
                {
                    "ref": "secretary-510-pilot",
                    "project": "codegen_orchestrator",
                    "workspace": {"base_branch": "pipeline/secretary-890"},
                },
                record,
            )
        cmds = [" ".join(run) for run in host.runs]
        self.assertTrue(any("gh pr merge pipeline/secretary-510-pilot --merge" in c for c in cmds), cmds)
        # The checkout tracks main, so the stacked base is never what it is fast-forwarded to.
        self.assertFalse(any("origin/pipeline/secretary-890" in c for c in cmds), cmds)
        self.assertTrue(any(c.endswith("merge --ff-only origin/main") for c in cmds), cmds)

    def test_complete_green_survives_stacked_base_diverged_from_default_branch(self) -> None:
        """secretary-899: a stacked card's PR merges on GitHub, and an unrelated card has landed on
        the default branch since the base branch was cut. The post-merge refresh of the project
        checkout must not report that merge as failed, and must leave the checkout on the default
        branch at the remote tip."""
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            repo = root / "checkout"
            workspace = root / "workspace"
            git(root, "init", "--quiet", "--bare", "--initial-branch", "main", str(remote))
            git(root, "clone", "--quiet", str(remote), str(repo))
            _configure_git_user(repo)
            _commit_file(repo, "README.md", "seed\n", "seed")
            git(repo, "push", "--quiet", "origin", "main")
            # The stacked base, cut from the seed and already carrying the parent card.
            git(repo, "checkout", "--quiet", "-b", "pipeline/secretary-890")
            _commit_file(repo, "parent.txt", "parent card\n", "parent card")
            git(repo, "push", "--quiet", "origin", "pipeline/secretary-890")
            # An unrelated card lands on the default branch while the stack is in flight, so
            # origin/main and origin/pipeline/secretary-890 diverge.
            git(repo, "checkout", "--quiet", "main")
            unrelated = _commit_file(repo, "other.txt", "unrelated card\n", "unrelated card")
            git(repo, "push", "--quiet", "origin", "main")
            git(root, "clone", "--quiet", str(remote), str(workspace))
            _configure_git_user(workspace)
            git(workspace, "checkout", "--quiet", "-b", _legacy_worker_branch("secretary-899"), "origin/pipeline/secretary-890")
            _commit_file(workspace, "child.txt", "child card\n", "child card")

            host = _FakeGhMergeHost(_StackedBaseCatalog(repo), root, mode="real")
            host.complete_green(
                {
                    "ref": "secretary-899",
                    "project": "secretary",
                    "workspace": {"base_branch": "pipeline/secretary-890"},
                },
                SimpleNamespace(workspace=str(workspace)),
            )

            self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
            self.assertEqual(git(repo, "rev-parse", "HEAD"), unrelated)

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

            # The card carries no sprint, so the green verdict merges on its own tick: the
            # entry point moved with the seam, what it does on this path did not.
            result = runtime._park_green_verdict(
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
        self.assertNotIn("TA_CODEX_MODE", env)
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


class _StackedBaseCatalog:
    def __init__(self, repo: Path) -> None:
        self.instance_dir = Path("/nonexistent-instance")
        self._repo = repo

    def binding(self, project: str) -> dict:
        return {"repo": str(self._repo), "default_branch": "main"}

    def default_branch(self, project: str, override: str | None) -> str:
        return override or "main"

    def adapter(self, project: str) -> dict:
        return {"validation": {"ci": "github"}}


class _FakeGhMergeHost(CommandHostRuntime):
    """Real git over real repos, with `gh pr merge` stubbed: the PR merge is GitHub's side."""

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        if args and args[0] == "gh":
            return subprocess.CompletedProcess(list(args), 0, "", "")
        return super()._run(args, label, cwd=cwd)


class GitBranchHost(CommandHostRuntime):
    def __init__(self, root: Path) -> None:
        super().__init__(FakeCatalog(), root, mode="real")  # type: ignore[arg-type]
        self.root = root
        self.launched: list[tuple[str, str]] = []
        self.launch_prompts: list[str | None] = []

    def _create_workspace(
        self, project: str, worker_id: str, base: str, *, expected: str = ""
    ) -> str:
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
        split_from: str = "",
        task: dict | None = None,
    ) -> LaunchedHead:
        self.launched.append((head, prompt_file))
        self.launch_prompts.append(launch_prompt)
        return LaunchedHead(f"test:{head}", head)


class WorkspaceResumeTests(unittest.TestCase):
    def test_fresh_workspace_branch_rename_is_not_forced(self) -> None:
        host = GitBranchHost(Path("/tmp"))
        with mock.patch.object(host, "_run") as run:
            host._set_worker_branch("/workspace", "pipeline/secretary-510-pilot")

        run.assert_called_once_with(
            ["git", "-C", "/workspace", "branch", "-m", "pipeline/secretary-510-pilot"],
            "git branch",
        )

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

    def test_prepare_worker_refuses_missing_workspace_when_resuming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspaces"
            host = GitBranchHost(root)
            task = {
                "ref": "secretary-510-pilot",
                "project": "secretary",
                "description": "updated task description",
                "workspace": {"base_branch": "main"},
            }

            with mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(workspace_root)}):
                with self.assertRaisesRegex(HostError, "resume workspace is missing"):
                    host.prepare_worker(
                        task,
                        "secretary-510-pilot-pilot",
                        "codex",
                        attempt_id="attempt-retry",
                        require_existing_workspace=True,
                    )

    def test_prepare_worker_refuses_resumed_workspace_on_a_different_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            workspace_root = root / "workspaces"
            workspace = workspace_root / "secretary" / "secretary-510-pilot-pilot"
            repo.mkdir()
            git(repo, "init", "--initial-branch", "main")
            _configure_git_user(repo)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "base")
            workspace.parent.mkdir(parents=True)
            git(repo, "worktree", "add", "-b", "foreign-branch", str(workspace))

            class Catalog(FakeCatalog):
                def binding(self, project: str) -> dict:
                    return {"repo": str(repo), "default_branch": "main"}

            host = GitBranchHost(root)
            host.catalog = Catalog()  # type: ignore[assignment]
            task = {
                "ref": "secretary-510-pilot",
                "project": "secretary",
                "workspace": {"base_branch": "main"},
            }
            with mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(workspace_root)}):
                with self.assertRaisesRegex(HostError, "resume workspace is on branch foreign-branch"):
                    host.prepare_worker(
                        task,
                        "secretary-510-pilot-pilot",
                        "codex",
                        attempt_id="attempt-retry",
                        require_existing_workspace=True,
                    )


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

    def __init__(
        self,
        root: Path,
        adapter: dict,
        *,
        pr_open: bool,
        check_runs: list,
        statuses: list | None = None,
        run_log: str = "",
        run_log_error: bool = False,
    ) -> None:
        super().__init__(GateCatalog(adapter), root, mode="real")  # type: ignore[arg-type]
        self._pr_open = pr_open
        self._check_runs = check_runs
        self._statuses = statuses or []
        self._run_log = run_log
        self._run_log_error = run_log_error
        self.gh: list[list[str]] = []

    def _fake_gh(self, args):
        self.gh.append(list(args))

        def done(out="", code=0):
            return subprocess.CompletedProcess(args, code, out, "")

        if args[1:3] == ["repo", "view"]:
            return done("example-org/sample\n")
        if args[1:3] == ["pr", "list"]:
            return done("42\n" if self._pr_open else "\n")
        if args[1:3] == ["pr", "create"]:
            self._pr_open = True
            return done("https://github.com/example-org/sample/pull/42\n")
        if args[1:3] == ["run", "view"]:
            if self._run_log_error:
                return done("", code=1)
            return done(self._run_log)
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

    def test_github_gate_red_fragment_skips_aggregate_job_echo(self) -> None:
        """secretary-766: `--log-failed` dumps every failed job, including one that only
        aggregates the others (`needs: [...]`) and echoes a generic summary after the real
        error. The fragment must come from the actually-failed job's own `##[error]` line,
        not a blind tail that lands on the aggregator's echo."""
        run_log = "\n".join([
            "tests\tRun pytest\tcollecting tests",
            "tests\tRun pytest\t##[error]AssertionError: expected 2, got 3",
            "tests\tRun pytest\t##[error]Process completed with exit code 1.",
            "gate\tSummarize\tone or more jobs failed",
            "gate\tSummarize\t##[error]Process completed with exit code 1.",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._github_adapter(),
                pr_open=True,
                check_runs=[{
                    "status": "COMPLETED", "conclusion": "FAILURE", "name": "tests",
                    "details_url": "https://github.com/example-org/sample/actions/runs/999",
                }],
                run_log=run_log,
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "red")
        self.assertIn('step "Run pytest"', result.summary)
        self.assertIn("AssertionError: expected 2, got 3", result.log)
        self.assertNotIn("one or more jobs failed", result.log)

    def test_github_gate_red_fragment_keeps_unmarked_error_ahead_of_the_runner_echo(self) -> None:
        """secretary-766 review: gh only tags its own generic completion line with `##[error]`;
        the actual Python exception above it usually carries no marker at all. Filtering the
        fragment down to `##[error]`-only lines then keeps just the completion echo and drops
        the real cause — reproduced here with the exact two-line log from the review."""
        run_log = "\n".join([
            "tests\tRun script\tFileNotFoundError: absent",
            "tests\tRun script\t##[error]Process completed with exit code 1.",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._github_adapter(),
                pr_open=True,
                check_runs=[{
                    "status": "COMPLETED", "conclusion": "FAILURE", "name": "tests",
                    "details_url": "https://github.com/example-org/sample/actions/runs/999",
                }],
                run_log=run_log,
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "red")
        self.assertIn("FileNotFoundError: absent", result.log)

    def test_github_gate_red_flags_an_infra_failure(self) -> None:
        run_log = "\n".join([
            "tests\tPull image\t##[error]docker: pull access denied for registry.internal/app",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._github_adapter(),
                pr_open=True,
                check_runs=[{
                    "status": "COMPLETED", "conclusion": "FAILURE", "name": "tests",
                    "details_url": "https://github.com/example-org/sample/actions/runs/999",
                }],
                run_log=run_log,
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "red")
        self.assertIn("infrastructure setup failure", result.summary)

    def test_github_gate_red_reports_unavailable_log_when_not_an_actions_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._github_adapter(),
                pr_open=True,
                # A legacy commit status has no Actions run URL at all.
                check_runs=[{"status": "COMPLETED", "conclusion": "FAILURE", "context": "external-ci"}],
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "red")
        self.assertIn("log unavailable", result.log)

    def test_github_gate_red_reports_unavailable_log_when_gh_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._github_adapter(),
                pr_open=True,
                check_runs=[{
                    "status": "COMPLETED", "conclusion": "FAILURE", "name": "tests",
                    "details_url": "https://github.com/example-org/sample/actions/runs/999",
                }],
                run_log_error=True,
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "red")
        self.assertIn("log unavailable", result.log)

    def test_github_gate_pending_while_pr_ci_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._github_adapter(),
                pr_open=True, check_runs=[{"status": "IN_PROGRESS"}],
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "pending")

    def _required_adapter(self, *names: str) -> dict:
        return {"validation": {"ci": "github", "required_checks": list(names)}}

    def test_github_gate_green_when_required_check_passes_next_to_a_failed_optional(self) -> None:
        """The declared set is the whole truth. An `optional-suite` failing on the
        same sha is not the project's gate and must not bounce the card."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._required_adapter("test"),
                pr_open=True,
                check_runs=[
                    {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "test"},
                    {"status": "COMPLETED", "conclusion": "FAILURE", "name": "optional-suite"},
                ],
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "green")

    def test_github_gate_red_names_the_failed_required_check(self) -> None:
        run_log = "\n".join([
            "test\tRun unittest\t##[error]AssertionError: expected 2, got 3",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._required_adapter("test"),
                pr_open=True,
                check_runs=[
                    {
                        "status": "COMPLETED", "conclusion": "FAILURE", "name": "test",
                        "details_url": "https://github.com/example-org/sample/actions/runs/999",
                    },
                    {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "optional-suite"},
                ],
                run_log=run_log,
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "red")
        self.assertIn("test", result.summary)
        self.assertIn("AssertionError: expected 2, got 3", result.log)

    def test_github_gate_pending_while_a_required_check_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._required_adapter("test"),
                pr_open=True,
                check_runs=[
                    {"status": "IN_PROGRESS", "name": "test"},
                    {"status": "COMPLETED", "conclusion": "FAILURE", "name": "optional-suite"},
                ],
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "pending")

    def test_github_gate_pending_while_a_required_check_is_missing(self) -> None:
        """A required name nothing posted for this sha is "CI did not start", not green: the
        pending watchdog escalates it if it never arrives."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._required_adapter("test", "lint"),
                pr_open=True,
                check_runs=[{"status": "COMPLETED", "conclusion": "SUCCESS", "name": "test"}],
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "pending")

    def test_github_gate_matches_a_required_legacy_status_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._required_adapter("external-ci"),
                pr_open=True,
                check_runs=[{"status": "COMPLETED", "conclusion": "FAILURE", "name": "optional-suite"}],
                statuses=[{"state": "success", "context": "external-ci"}],
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "green")

    def test_github_gate_without_a_required_list_still_judges_every_check(self) -> None:
        """Migration safety: an adapter that has not declared `required_checks` keeps the pre-841
        behaviour, where any failing check on the sha is red."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _build_gated_workspace(Path(tmp), "main", "pipeline/secretary-633")
            host = GithubGateHost(
                Path(tmp), self._github_adapter(),
                pr_open=True,
                check_runs=[
                    {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "test"},
                    {"status": "COMPLETED", "conclusion": "FAILURE", "name": "optional-suite"},
                ],
            )
            result = host.gate_check(self._task(), self._record(ws))
        self.assertEqual(result.status, "red")
        self.assertIn("optional-suite", result.summary)

    def test_github_rollup_honours_the_required_set(self) -> None:
        from secretary.dispatcher_gate import _rollup

        items = [
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "test"},
            {"status": "COMPLETED", "conclusion": "FAILURE", "name": "optional-suite"},
            {"status": "IN_PROGRESS", "name": "slow-optional"},
        ]
        self.assertEqual(_rollup(items, ["test"])[0], "SUCCESS")
        self.assertEqual(_rollup(items, ["test", "optional-suite"])[0], "FAILURE")
        self.assertEqual(_rollup(items, ["absent"])[0], "PENDING")
        self.assertEqual(_rollup([], ["test"])[0], "PENDING")
        # legacy status entries match on `context`
        self.assertEqual(_rollup([{"state": "failure", "context": "external-ci"}], ["external-ci"])[0], "FAILURE")

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

    def prepare_head_workspace(self, head: str, workspace: str, *, role: str = "") -> None:
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


class PidHeartbeatTests(unittest.TestCase):
    """secretary-751: the pid a head writes for itself before it execs, and how the watchdog
    reads it back. This is the signal that distinguishes a live silent head from a shell left
    behind after the head exits, without reading terminal text, title, or a generic running flag.
    """

    def test_heartbeat_writes_the_shells_own_pid_then_execs_the_head(self) -> None:
        wrapped = with_pid_heartbeat("codex exec --dangerously-bypass-approvals-and-sandbox", "/tmp/x.pid")

        self.assertEqual(
            wrapped,
            'echo "$$" > /tmp/x.pid; exec env codex exec --dangerously-bypass-approvals-and-sandbox',
        )

    def test_heartbeat_survives_a_leading_environment_assignment(self) -> None:
        """secretary-751 review: catalog commands from `head_launch` start with `NAME=value`, which
        bare `exec` cannot run directly. Executed through a real `/bin/sh` (not just string
        comparison), the wrapped command must still exec successfully and the pid file must end up
        holding the pid of the process that was actually running when it exited."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "x.pid")
            wrapped = with_pid_heartbeat(
                'FOO=bar python3 -c "import os; print(os.getpid())"', pid_file
            )

            result = subprocess.run(
                ["/bin/sh", "-lc", wrapped],
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            reported_pid = result.stdout.strip()
            heartbeat_pid = Path(pid_file).read_text(encoding="utf-8").strip()
            self.assertEqual(reported_pid, heartbeat_pid)

    def test_heartbeat_quotes_a_pid_file_path_with_spaces(self) -> None:
        wrapped = with_pid_heartbeat("codex exec", "/tmp/weird dir/x.pid")

        self.assertIn(shlex.quote("/tmp/weird dir/x.pid"), wrapped)

    def test_pid_file_path_is_keyed_on_kind_and_reference_only(self) -> None:
        """A respawn in the same workspace must land on the same path as the launch before it, so
        clearing the file before a fresh launch actually removes the predecessor's pid."""
        self.assertEqual(
            pid_file_path("worker", "secretary-751"),
            pid_file_path("worker", "secretary-751"),
        )
        self.assertNotEqual(
            pid_file_path("worker", "secretary-751"),
            pid_file_path("review", "secretary-751"),
        )

    def test_pid_file_path_honours_the_body_dir_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_BODY_DIR": tmp}):
                self.assertTrue(pid_file_path("worker", "secretary-751").startswith(tmp))

    def test_a_process_that_has_exited_is_not_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["true"])
            proc.wait()
            pid_file.write_text(str(proc.pid), encoding="utf-8")

            status = head_process_status(str(pid_file))

            self.assertEqual(status, {"known": True, "alive": False})

    def test_a_running_process_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["sleep", "5"])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.terminate)
            pid_file.write_text(str(proc.pid), encoding="utf-8")

            status = head_process_status(str(pid_file))

            self.assertEqual(status, {"known": True, "alive": True, "stopped": False})

    def test_a_pid_file_that_has_not_been_written_yet_is_not_known(self) -> None:
        """A fresh launch has not run its `echo $$` yet, and a raw
        `SECRETARY_DISPATCHER_*_COMMAND` override never will. Neither is evidence of death."""
        with tempfile.TemporaryDirectory() as tmp:
            status = head_process_status(str(Path(tmp) / "never-written.pid"))

        self.assertEqual(status, {"known": False})

    def test_garbage_pid_file_contents_are_not_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            pid_file.write_text("not-a-pid\n", encoding="utf-8")

            status = head_process_status(str(pid_file))

        self.assertEqual(status, {"known": False})


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
        _clear_env(self, "SECRETARY_DISPATCHER_BODY_DIR")
        os.environ["SECRETARY_DISPATCHER_BODY_DIR"] = str(self.root)

    def _dead_pid(self) -> int:
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def _live_pid(self) -> int:
        proc = subprocess.Popen(["sleep", "5"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        return proc.pid

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

    def test_worker_leaf_identifies_the_pane_when_the_handle_alias_changed(self) -> None:
        host = self._host([
            {"handle": "term-alias", "leafId": "leaf-worker", "connected": True},
        ])

        record = self._record(worker_leaf="leaf-worker")

        self.assertTrue(host.worker_status(self.task, record)["live"])

    def test_last_output_at_is_converted_from_milliseconds_to_epoch_seconds(self) -> None:
        host = self._host([
            {"handle": "term-worker", "leafId": "leaf-worker", "connected": True, "lastOutputAt": 1_753_456_789_123},
        ])

        status = host.worker_status(self.task, self._record())

        self.assertEqual(status["last_activity"], 1_753_456_789.123)

    def test_invalid_or_missing_last_output_at_has_no_activity(self) -> None:
        for terminal in (
            {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            {"handle": "term-worker", "leafId": "leaf-worker", "connected": True, "lastOutputAt": "not-a-time"},
        ):
            with self.subTest(terminal=terminal):
                status = self._host([terminal]).worker_status(self.task, self._record())
                self.assertIsNone(status["last_activity"])

    def test_tui_supplement_newer_than_last_output_at_wins(self) -> None:
        host = self._host([
            {"handle": "term-worker", "leafId": "leaf-worker", "connected": True, "lastOutputAt": 1_753_456_789_123},
        ])
        host.codex_tui_activity = lambda _task, _record, _kind: 1_753_456_800.0  # type: ignore[method-assign]

        status = host.worker_status(self.task, self._record())

        self.assertEqual(status["last_activity"], 1_753_456_800.0)

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

    def test_connected_worker_pane_with_an_exited_head_process_is_not_live(self) -> None:
        """secretary-751: Codex crashed and Orca kept the pane's own workspace shell alive. The
        pane answers connected and even keeps producing output (the shell's own prompt), so only
        the pid heartbeat tells the watchdog the head itself is gone."""
        Path(pid_file_path("worker", self.task["ref"])).write_text(
            str(self._dead_pid()), encoding="utf-8"
        )
        host = self._host([
            {"handle": "term-worker", "leafId": "leaf-worker", "connected": True, "lastOutputAt": 1_753_456_789_123},
        ])

        status = host.worker_status(self.task, self._record())

        self.assertFalse(status["live"])
        self.assertEqual(status["reason"], "process-exited")

    def test_connected_reviewer_pane_with_an_exited_head_process_is_not_live(self) -> None:
        Path(pid_file_path("review", self.task["ref"])).write_text(
            str(self._dead_pid()), encoding="utf-8"
        )
        host = self._host([
            {"handle": "term-review", "leafId": "leaf-review", "connected": True},
        ])

        status = host.review_status(self.task, self._record(review_handle="term-review"))

        self.assertFalse(status["live"])
        self.assertEqual(status["reason"], "process-exited")

    def test_connected_pane_with_a_live_head_process_stays_live(self) -> None:
        Path(pid_file_path("worker", self.task["ref"])).write_text(
            str(self._live_pid()), encoding="utf-8"
        )
        host = self._host([
            {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
        ])

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["live"])

    def test_an_adopted_head_with_no_pane_identity_is_live_on_its_heartbeat(self) -> None:
        """secretary-820: a head adopted from a launch intent never had its handle persisted, so
        the inventory cannot name its pane. Its heartbeat can, and reading it as a missing terminal
        would respawn a working head: the second launch the intent exists to prevent."""
        Path(pid_file_path("worker", self.task["ref"])).write_text(
            str(self._live_pid()), encoding="utf-8"
        )
        host = self._host([{"handle": "term-other", "leafId": "leaf-other", "connected": True}])

        status = host.worker_status(self.task, self._record(handle="", worker_leaf=""))

        self.assertTrue(status["live"])
        self.assertEqual(status["reason"], "pid")
        self.assertTrue(status["pid_confirmed"])

    def test_a_record_with_no_pane_identity_and_a_dead_head_is_still_missing(self) -> None:
        Path(pid_file_path("worker", self.task["ref"])).write_text(
            str(self._dead_pid()), encoding="utf-8"
        )
        host = self._host([{"handle": "term-other", "leafId": "leaf-other", "connected": True}])

        status = host.worker_status(self.task, self._record(handle="", worker_leaf=""))

        self.assertFalse(status["live"])
        self.assertEqual(status["reason"], "missing-terminal")

    def test_a_head_silent_since_launch_is_still_live_while_its_process_runs(self) -> None:
        """The pid signal must not read silence as death: a head that has said nothing since it
        started is a separate, pre-existing case (secretary-726's short initial-output window),
        not this one."""
        Path(pid_file_path("worker", self.task["ref"])).write_text(
            str(self._live_pid()), encoding="utf-8"
        )
        host = self._host([
            {"handle": "term-worker", "leafId": "leaf-worker", "connected": True, "lastOutputAt": 1_000_000},
        ])

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["live"])

    def test_pid_file_not_written_yet_falls_back_to_ordinary_liveness(self) -> None:
        """Nothing has written the heartbeat file yet (a launch mid-flight, or a raw
        SECRETARY_DISPATCHER_*_COMMAND override that never will). That is not evidence of death."""
        host = self._host([
            {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
        ])

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["live"])


class ProductionPauseTests(unittest.TestCase):
    """The pause the operator actually presses (secretary-731).

    The bug this covers: `pause` wrote a flag the production dispatcher never read, so the operator
    watched the pipeline claim new cards straight through a successful pause.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        # The legacy mirror is written next to the live pipeline worktree by default; keep every
        # test's copy inside its own tmpdir.
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.legacy_mirror = self.data_dir / "legacy-pause.json"
        self.board = FakeKanboard()
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            TaskAudit(self.data_dir),
            CutoverState(self.data_dir),
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
            legacy_pause=FakeLegacyPause(),  # type: ignore[arg-type]
        )
        self.ref = "secretary-510-pilot"
        self.runtime.state.save({
            "version": 1,
            "phase": "cutover_committed",
            "pilot_ref": self.ref,
            "old_owner_paused": True,
            "records": {},
        })

    def pause(self, mode: str, **kwargs) -> dict:
        return self.runtime.pause_pipeline(
            mode=mode, actor="operator", reason="host maintenance", **kwargs
        )

    def report_done(self, request_id: str = "worker-done") -> None:
        self.writer.report(
            role="worker",
            actor="worker",
            reference=self.ref,
            kind="done",
            body="ready for review",
            request_id=request_id,
        )

    def drive_into_review(self) -> None:
        self.runtime.production_tick()
        self.report_done()
        self.runtime.production_tick()
        self.assertEqual(self.runtime.production_tick()["actions"][0]["action"], "review-started")

    def record(self) -> DispatcherRecord:
        payload = self.runtime.production_state.load()
        return self.runtime.production_state.records(payload)[self.ref]

    def test_drain_stops_new_claims(self) -> None:
        self.pause("drain")

        result = self.runtime.production_tick()

        self.assertEqual(result["pause"]["mode"], "drain")
        self.assertEqual(self.reader.show(self.ref)["state"], "ready")
        self.assertEqual(self.host.prepared, [])
        self.assertEqual(self.runtime.production_state.load().get("records") or {}, {})

    def test_paused_tick_does_not_claim_a_new_card(self) -> None:
        """Regression for the reported bug: pause, tick, and the card must still be Ready."""
        for mode in ("drain", "freeze"):
            with self.subTest(mode=mode):
                self.pause(mode)
                self.runtime.production_tick()
                self.assertEqual(self.reader.show(self.ref)["state"], "ready")
                self.assertEqual(self.reader.show("secretary-510-neighbor")["state"], "ready")
                self.runtime.resume_pipeline(actor="operator")

    def test_drain_keeps_driving_the_card_already_in_flight(self) -> None:
        self.runtime.production_tick()
        self.report_done()
        self.pause("drain")

        result = self.runtime.production_tick()

        self.assertEqual(result["actions"][0]["to"], "validate")
        self.assertEqual(self.reader.show(self.ref)["state"], "validate")
        # ...and the Ready neighbour is still not claimed while the drain holds.
        self.assertEqual(self.reader.show("secretary-510-neighbor")["state"], "ready")

    def test_freeze_stops_the_worker_head_without_touching_the_workspace(self) -> None:
        self.runtime.production_tick()
        workspace = self.record().workspace

        status = self.pause("freeze")

        self.assertEqual(status["stopped_worker"], [self.ref])
        self.assertEqual(self.host.stopped, [f"{self.ref}-pilot"])
        self.assertEqual(self.host.torn_down, [])
        self.assertTrue(Path(workspace).is_dir())
        record = self.record()
        self.assertEqual(record.handle, "")
        self.assertEqual(record.workspace, workspace)
        self.assertGreater(record.paused_worker_at, 0)

    def test_freeze_stops_the_reviewer_head(self) -> None:
        self.drive_into_review()

        status = self.pause("freeze")

        self.assertEqual(status["stopped_reviewer"], [self.ref])
        self.assertEqual(self.host.stopped_reviews, [f"review:{self.ref}"])
        self.assertEqual(self.host.torn_down, [])
        record = self.record()
        self.assertEqual(record.review_handle, "")
        self.assertGreater(record.paused_reviewer_at, 0)

    def test_freeze_advances_nothing(self) -> None:
        self.runtime.production_tick()
        self.report_done()
        self.pause("freeze")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "pipeline is frozen by pause")
        self.assertEqual(self.reader.show(self.ref)["state"], "in_progress")

    def test_resume_relaunches_the_worker_in_the_same_workspace(self) -> None:
        self.runtime.production_tick()
        workspace = self.record().workspace
        self.pause("freeze")

        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["relaunched"], [f"{self.ref}:worker"])
        self.assertIn("restart_worker", self.host.calls)
        record = self.record()
        self.assertEqual(record.handle, f"rework:{self.ref}")
        self.assertEqual(record.workspace, workspace)
        self.assertEqual(record.paused_worker_at, 0.0)
        self.assertFalse(self.runtime.pause.path.exists())

    def test_resume_relaunches_the_reviewer(self) -> None:
        self.drive_into_review()
        self.pause("freeze")

        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["relaunched"], [f"{self.ref}:reviewer"])
        record = self.record()
        self.assertEqual(record.review_handle, f"review:{self.ref}")
        self.assertEqual(record.state, "reviewing")
        self.assertEqual(record.paused_reviewer_at, 0.0)

    def test_a_resume_relaunch_that_dies_mid_flight_is_adopted_by_the_next_tick(self) -> None:
        """secretary-820: the resume is a bring-up like any other, so it writes its intent first.

        A resume killed between the head coming up and the state write that records it would
        otherwise leave a live worker with no handle in the record, and the next tick's watchdog
        would respawn a head that is working.
        """
        self.runtime.production_tick()
        self.pause("freeze")
        real_save = self.runtime.production_state.save
        real_restart = self.host.restart_worker
        launched = {"yet": False}

        def save(payload: dict) -> None:
            if launched["yet"]:
                raise OSError("production state is not writable")
            real_save(payload)

        def restart(task: dict, record):
            result = real_restart(task, record)
            launched["yet"] = True
            return result

        with mock.patch.object(self.runtime.production_state, "save", save):
            with mock.patch.object(self.host, "restart_worker", restart):
                with self.assertRaises(OSError):
                    self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.record().launch_intent["action"], "worker-resume")

        # The operator retries the resume. It parks the card rather than guessing at the head the
        # dead run may have left: the tick's recovery is the one place that decides.
        retried = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(retried["parked"], [f"{self.ref}:worker"])
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

        adopted = self.runtime.production_tick()["actions"][0]

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.record().state, "claimed")

    def test_resume_leaves_a_card_that_reported_during_the_freeze_to_the_tick(self) -> None:
        """A relaunched head would start a fresh turn on work that is already finished."""
        self.runtime.production_tick()
        self.pause("freeze")
        self.report_done()

        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["parked"], [f"{self.ref}:worker"])
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertEqual(self.runtime.production_tick()["actions"][0]["to"], "validate")

    def test_resume_hands_the_wait_watchdog_a_fresh_window(self) -> None:
        """A freeze advances nothing, so its whole length would otherwise count as head silence."""
        self.runtime.production_tick()
        self.runtime.production_tick()
        payload = self.runtime.production_state.load()
        records = self.runtime.production_state.records(payload)
        stale = time.time() - (WORKER_REPORT_STALL_DEFAULT * 2)
        records[self.ref].worker_waiting_since = stale
        records[self.ref].worker_progress_at = stale
        self.runtime.production_state.put_records(payload, records)
        self.runtime.production_state.save(payload)
        self.pause("freeze")

        for _ in range(3):
            self.assertEqual(self.runtime.production_tick()["status"], "skipped")
        paused = self.record()
        self.assertEqual(paused.worker_respawns, 0)
        self.assertEqual(self.reader.show(self.ref)["state"], "in_progress")

        self.runtime.resume_pipeline(actor="operator")

        self.assertGreater(self.record().worker_waiting_since, stale)
        # The watchdog did not read the paused head as a stall: no respawn, no Blocked.
        self.assertEqual(self.runtime.production_tick()["actions"][0]["action"], "waiting-worker-report")
        self.assertEqual(self.reader.show(self.ref)["state"], "in_progress")

    def test_pause_status_reports_the_live_data_plane_and_stopped_heads(self) -> None:
        self.runtime.production_tick()
        self.pause("freeze")

        status = self.runtime.pause_status()

        self.assertTrue(status["paused"])
        self.assertEqual(status["mode"], "freeze")
        self.assertEqual(status["pause_file"], str(self.data_dir / "dispatcher" / "pause.json"))
        self.assertEqual(
            status["dispatcher"]["state_file"], str(self.data_dir / "dispatcher" / "production-state.json")
        )
        self.assertEqual(status["dispatcher"]["owner"], "secretary-pilot")
        head = next(entry for entry in status["heads"] if entry["ref"] == self.ref)
        self.assertEqual(head["worker"], "stopped-by-pause")

    def test_a_head_that_was_never_up_is_not_reported_as_pause_stopped(self) -> None:
        self.runtime.production_tick()
        self.pause("freeze")

        head = next(entry for entry in self.runtime.pause_status()["heads"] if entry["ref"] == self.ref)

        self.assertEqual(head["worker"], "stopped-by-pause")
        self.assertEqual(head["reviewer"], "not-running")

    def test_repeated_pause_in_the_same_mode_is_a_noop(self) -> None:
        self.pause("drain")

        again = self.pause("drain")

        self.assertEqual(again["action"], "noop")
        self.assertEqual(again["mode"], "drain")

    def test_switching_mode_while_paused_is_refused(self) -> None:
        self.pause("drain")

        with self.assertRaisesRegex(DispatcherError, "already paused"):
            self.pause("freeze")

    def test_legacy_aliases_still_parse(self) -> None:
        self.assertEqual(self.pause("soft")["mode"], "drain")
        self.runtime.resume_pipeline(actor="operator")
        self.assertEqual(self.pause("hard")["mode"], "freeze")

    def test_unknown_mode_is_refused(self) -> None:
        with self.assertRaisesRegex(DispatcherError, "unknown pause mode"):
            self.pause("halt")

    def test_resume_without_a_pause_is_a_noop(self) -> None:
        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["action"], "noop")
        self.assertFalse(result["paused"])

    def test_pause_mirrors_the_flag_the_background_roles_read(self) -> None:
        """steward/curator/retro still read the legacy flag, so a pause has to reach it too."""
        self.pause("freeze")

        mirrored = json.loads(self.legacy_mirror.read_text(encoding="utf-8"))
        self.assertEqual(mirrored["mode"], "hard")
        self.assertEqual(mirrored["actor"], "operator")

        self.runtime.resume_pipeline(actor="operator")

        self.assertFalse(self.legacy_mirror.exists())

    def test_pause_never_takes_over_a_legacy_flag_it_did_not_write(self) -> None:
        self.legacy_mirror.write_text(json.dumps({"mode": "hard", "actor": "someone-else"}), encoding="utf-8")

        status = self.pause("freeze")

        self.assertFalse(status["legacy_mirror"]["written"])
        self.runtime.resume_pipeline(actor="operator")
        self.assertEqual(
            json.loads(self.legacy_mirror.read_text(encoding="utf-8"))["actor"], "someone-else"
        )

    def test_freeze_leaves_an_excluded_workspace_running(self) -> None:
        """The backup worker freezes the pipeline from inside its own workspace."""
        self.runtime.production_tick()
        workspace = self.record().workspace

        status = self.pause("freeze", exclude_workspaces=[workspace])

        self.assertEqual(status["excluded_worker"], [self.ref])
        self.assertEqual(status["stopped_worker"], [])
        self.assertEqual(self.host.stopped, [])
        self.assertEqual(self.record().handle, f"term:{self.ref}-pilot")

    def test_probe_reports_a_freeze_instead_of_a_stuck_dispatcher(self) -> None:
        self.pause("freeze")

        result = self.runtime.production_probe()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pause"]["mode"], "freeze")
        self.assertEqual(result["would"], [])

    def test_freeze_over_unreadable_state_sets_the_flag_without_touching_it(self) -> None:
        """An unreadable state file must not be replaced by an empty one on the way to a freeze."""
        self.runtime.production_state.path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime.production_state.path.write_text("{ not json", encoding="utf-8")

        status = self.pause("freeze")

        self.assertTrue(status["paused"])
        self.assertIn("production state is unreadable", " ".join(status["warnings"]))
        self.assertEqual(
            self.runtime.production_state.path.read_text(encoding="utf-8"), "{ not json"
        )
        self.assertEqual(self.runtime.production_tick()["status"], "skipped")

    def age_the_pause(self, seconds: int) -> None:
        state = self.runtime.pause.load()
        state["since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))
        self.runtime.pause.save(state)

    def test_an_expired_automation_freeze_is_resumed_by_the_next_tick(self) -> None:
        """A backup killed before its `finally` must not freeze the dispatcher forever."""
        self.runtime.production_tick()
        workspace = self.record().workspace
        self.runtime.pause_pipeline(
            mode="freeze", actor="secretary-backup", reason="backup snapshot"
        )
        self.age_the_pause(3600)

        result = self.runtime.production_tick()

        self.assertEqual(result["auto_resume"]["reason"], "stale-automation-freeze")
        self.assertEqual(result["auto_resume"]["relaunched"], [f"{self.ref}:worker"])
        self.assertFalse(self.runtime.pause.path.exists())
        self.assertNotEqual(result["status"], "skipped")
        record = self.record()
        self.assertEqual(record.handle, f"rework:{self.ref}")
        self.assertEqual(record.workspace, workspace)
        self.assertEqual(record.paused_worker_at, 0.0)

    def test_a_fresh_automation_freeze_is_left_alone(self) -> None:
        self.runtime.production_tick()
        self.runtime.pause_pipeline(
            mode="freeze", actor="secretary-backup", reason="backup snapshot"
        )

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertIsNone(result.get("auto_resume"))
        self.assertEqual(result["pause"]["auto_resume"]["reason"], "fresh")
        self.assertTrue(self.runtime.pause.path.exists())

    def test_an_operator_freeze_never_expires(self) -> None:
        """A person holding a maintenance window decides when it ends, however long it runs."""
        self.runtime.production_tick()
        self.pause("freeze")
        self.age_the_pause(3600 * 12)

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["pause"]["auto_resume"]["reason"], "manual-or-unknown-actor")
        self.assertTrue(self.runtime.pause.path.exists())

    def test_auto_resume_honours_the_ttl_override(self) -> None:
        self.runtime.pause_pipeline(mode="freeze", actor="secretary-backup", reason="backup")
        self.age_the_pause(3600)

        with mock.patch.dict(os.environ, {"TA_HARD_PAUSE_AUTO_RESUME_TTL_S": "0"}):
            self.assertEqual(self.runtime.production_tick()["status"], "skipped")

        self.assertEqual(self.runtime.production_tick()["auto_resume"]["resumed"], True)

    def test_a_failed_auto_resume_holds_the_freeze_and_says_why(self) -> None:
        self.runtime.production_tick()
        self.runtime.pause_pipeline(mode="freeze", actor="secretary-backup", reason="backup")
        self.age_the_pause(3600)

        with mock.patch.object(self.runtime.pause, "clear", side_effect=OSError("read-only fs")):
            result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["auto_resume"]["resumed"])
        self.assertIn("read-only fs", result["auto_resume"]["error"])
        self.assertTrue(self.runtime.pause.path.exists())
        # The retry on the next tick does not launch a second head on top of the one it just put back.
        retry = self.runtime.production_tick()
        self.assertEqual(retry["auto_resume"]["parked"], [f"{self.ref}:worker"])

    def test_a_frozen_tick_still_writes_and_pushes_the_checkpoint(self) -> None:
        """Freeze stops cards moving, not durability: a long freeze must not be a snapshot hole."""
        self.runtime.checkpoint = FakeCheckpoint(
            CheckpointResult(status="committed", commit="abc123", board_cards=2)
        )
        self.runtime.checkpoint_push = FakePusher({"status": "pushed", "last_push_commit": "abc123"})
        self.pause("freeze")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["checkpoint"]["commit"], "abc123")
        self.assertEqual(result["checkpoint_push"]["status"], "pushed")
        payload = self.runtime.production_state.load()
        self.assertEqual(payload["checkpoint"]["commit"], "abc123")
        self.assertEqual(payload["checkpoint_push"]["last_push_commit"], "abc123")
        # ...and the frozen tick still moved nothing.
        self.assertEqual(self.reader.show(self.ref)["state"], "ready")

    def test_a_failing_push_on_a_frozen_tick_is_reported_not_raised(self) -> None:
        self.runtime.checkpoint_push = FakePusher(RuntimeError("ssh agent is gone"))
        self.pause("freeze")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["checkpoint_push"]["status"], "failed")
        self.assertIn("ssh agent is gone", result["checkpoint_push"]["reason"])

    def test_the_mirror_lands_where_the_background_roles_look(self) -> None:
        """resolve_pipeline_state_dir's own order: a mirror written elsewhere sheds nothing."""
        state_dir = self.data_dir / "ta-state"
        with mock.patch.dict(os.environ, {"TA_PIPELINE_STATE_DIR": str(state_dir)}):
            os.environ.pop("SECRETARY_LEGACY_PAUSE_FILE", None)
            self.pause("drain")

        self.assertTrue((state_dir / "pause.json").is_file())


class _SelectorNotFoundHost(CommandHostRuntime):
    """Stubs the orca CLI to answer `selector_not_found` for a terminal stop, as it does for a
    workspace already removed out from under the dispatcher."""

    def __init__(self, root: Path, *, reply: str = "selector_not_found") -> None:
        super().__init__(FakeCatalog(), root, mode="real")  # type: ignore[arg-type]
        self.calls: list[list[str]] = []
        self._reply = reply

    def _run_json(self, args: list[str]) -> dict:
        self.calls.append(args)
        if args[:3] == ["orca", "terminal", "stop"]:
            raise HostError(self._reply)
        return {}


class CommandHostStopWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.record = DispatcherRecord(
            worker="secretary-997-w1",
            workspace=str(self.root / "workspaces" / "secretary-997"),
            handle="",
            head="head",
            review_head="review-head",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="working",
            claimed_at=0.0,
        )

    def test_a_worktree_orca_no_longer_knows_reads_as_already_stopped(self) -> None:
        host = _SelectorNotFoundHost(self.root)

        host.stop_workspace(self.record)  # must not raise

        self.assertTrue(any(call[:3] == ["orca", "terminal", "stop"] for call in host.calls))

    def test_any_other_stop_refusal_still_raises(self) -> None:
        host = _SelectorNotFoundHost(self.root, reply="orca terminal stop failed")

        with self.assertRaises(HostError):
            host.stop_workspace(self.record)
