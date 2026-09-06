"""The product runtime: one owner of a card, an idempotent start, five outcomes, and no Orca.

Hermetic in the same strong sense the read layer's suite is: no live Orca, no network, no Kanboard
and no real agent. The board is `FakeKanboard`, the head registry is a table this file writes, and
the head backend is a fake whose whole job is to leave behind exactly the artefacts a real
supervised head leaves — a launch-identity heartbeat, a versioned journal and a result file — so
that everything the operations conclude, they conclude from the same evidence a real run produces.

`OrcaAbsenceTests` is the one that must fail rather than be believed: it is criterion 2 of
secretary-1562 as a check, and it covers both the imports and the calls.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from secretary.cli import main
from secretary.config import validate
from secretary.webproto import admission as admission_module
from secretary.webproto import lifecycle as lifecycle_module
from secretary.webproto import ops as ops_module
from secretary.webproto import run_events, run_state, workspaces
from secretary.webproto.boundary import GUARDED, IMPLEMENTATION_FAILURES, ProtocolBoundary, operations
from secretary.webproto.errors import (
    OwnerConflict,
    ReadError,
    RunNotFound,
    RuntimeUnavailable,
    ValidationRefused,
)
from secretary.webproto.ops import OperationLayer
from secretary.webproto.reads import ReadLayer
from secretary.webproto.runs import RAISED, UNRESOLVED, ProductRun, RunStore, RunStoreError
from tests.fakes.dispatcher import FakeKanboard
from triggered_agents.agents.pipeline.heads import Registry
from triggered_agents.runtime.head.identity import publish_heartbeat
from triggered_agents.runtime.head.local_pty import RUN_EXITED, RUN_STARTED
from triggered_agents.runtime.head.run import HeadRun, StopInitiator
from triggered_agents.runtime.head.runtime import (
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_GONE,
    HEAD_OK,
    DeliverReceipt,
    ObserveReceipt,
    StartReceipt,
    StopReceipt,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

WORKER_PROFILE = "codex-product-worker"
REVIEWER_PROFILE = "claude-product-reviewer"

#: A registry of exactly the two demonstration-scenario profiles, both on the supervised backend.
#: Which head a product run raises is configuration, so the tests supply it as configuration.
PROFILES = {
    WORKER_PROFILE: {
        "resource": "openai-sub",
        "adapter": "codex",
        "model": "gpt-5.6-terra",
        "effort": "default",
        "runtime": "local-pty",
        "fallback": [],
    },
    REVIEWER_PROFILE: {
        "resource": "claude-sub",
        "adapter": "claude",
        "model": "opus",
        "effort": "high",
        "runtime": "local-pty",
        "fallback": [],
    },
    "orca-held-worker": {
        "resource": "claude-sub",
        "adapter": "claude",
        "model": "opus",
        "fallback": [],
    },
}


def _registry() -> Registry:
    return Registry(
        resources={"openai-sub": {"account": "a"}, "claude-sub": {"account": "b"}},
        profiles={key: dict(value) for key, value in PROFILES.items()},
        role_defaults={},
    )


def _dead_pid() -> int:
    """A pid that names no process, so a heartbeat pointing at it classifies as dead."""
    ceiling = int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip())
    for candidate in range(ceiling - 1, 1, -1):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise unittest.SkipTest("this host has no free pid to prove a dead heartbeat with")


class FakeHeadRuntime:
    """A head backend that leaves behind exactly what a supervised head leaves behind.

    Deliberately not a mock that records calls: the operations read a heartbeat, a journal and a
    result file, so a double that only answered `ok` would leave every classification untested. This
    writes the three artefacts, and `end` is what a real head's ending does to them.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.starts: list[dict[str, Any]] = []
        self.stops: list[tuple[str, str]] = []
        self.deliveries: list[tuple[str, str, str]] = []
        self.busy_after_delivery = 0
        self.commands: list[str] = []

    def start(self, spec, workspace, task_ref, *, command, title, pointer=None, **options):
        run_id = options["run_id"]
        role = options["role"]
        self.starts.append(
            {
                "run_id": run_id,
                "role": role,
                "workspace": workspace,
                "spec": spec,
                "pointer": pointer,
            }
        )
        self.commands.append(command)
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        publish_heartbeat(
            str(run_dir / "head.pid"),
            {"run_id": run_id, "role": role, "task": task_ref.ref},
        )
        self._append(run_dir, {"kind": RUN_STARTED, "seq": 1, "run_id": run_id, "at": 1.0})
        run = HeadRun(
            run_id=run_id,
            spec=spec,
            workspace=workspace,
            task_ref=task_ref,
            role=role,
            handle=str(run_dir / "head.sock"),
            leaf=run_id,
            pid_file=str(run_dir / "head.pid"),
        ).working()
        return StartReceipt(status=HEAD_OK, run=run)

    def observe(self, run):
        """Busy for one look after a delivery, exactly as the substrate's own turn is.

        Not a constant `False`: the reason the submit key is a second delivery at all is that the
        backend refuses one made while the turn it opened over the previous payload is still
        running, so a double that was never busy would leave that wait untested.
        """
        busy = self.busy_after_delivery > 0
        self.busy_after_delivery = max(0, self.busy_after_delivery - 1)
        return ObserveReceipt(
            status=HEAD_OK, run=run, evidence={"output_bytes": 4096, "alive": True}, busy=busy
        )

    def deliver(self, run, pointer, *, subject="", **options):
        self.deliveries.append((run.run_id, pointer.text, subject))
        self.busy_after_delivery = 1
        return DeliverReceipt(status=HEAD_OK, run=run, delivery_state="complete")

    def stop(self, run, initiator, **options):
        self.stops.append((run.run_id, initiator.reason))
        self.end(run.run_id, exit_code=None, signal=15)
        return StopReceipt(status=HEAD_OK, run=run)

    # -- what a real head's ending does to the three artefacts ---------------------------------

    def end(self, run_id: str, *, exit_code: int | None, signal: int | None) -> None:
        run_dir = self.root / run_id
        record = json.loads((run_dir / "head.pid").read_text(encoding="utf-8"))
        record["pid"] = _dead_pid()
        (run_dir / "head.pid").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        self._append(
            run_dir,
            {
                "kind": RUN_EXITED,
                "seq": 2,
                "run_id": run_id,
                "at": 2.0,
                "exit_code": exit_code,
                "signal": signal,
            },
        )

    def publish_result(self, run_id: str, payload: dict[str, Any]) -> None:
        (self.root / run_id / "result.json").write_text(json.dumps(payload), encoding="utf-8")

    def _append(self, run_dir: Path, record: dict[str, Any]) -> None:
        with (run_dir / "journal.jsonl").open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(dict({"schema_version": 1}, **record), sort_keys=True) + "\n")


class ProductRuntimeFixture(unittest.TestCase):
    """One instance, one fake board with a backlog card, one registry and one fake backend."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data_dir = self.tmp / "data"
        (self.data_dir / "dispatcher").mkdir(parents=True)
        (self.data_dir / "board").mkdir(parents=True)
        (self.data_dir / "sprints").mkdir(parents=True)
        self.repo = self._repo()
        self.instance = self._instance()
        self.board = FakeKanboard()
        self._backlog_card()
        self.runtime = FakeHeadRuntime(self.data_dir / "webproto" / "heads")
        (self.data_dir / "dispatcher" / "production-state.json").write_text(
            json.dumps({"phase": "production", "records": {}}), encoding="utf-8"
        )
        self.clock = 1788652800.0

    # -- fixture pieces ------------------------------------------------------------------------

    def _instance(self) -> Path:
        instance_dir = self.tmp / "instance"
        (instance_dir / "projects").mkdir(parents=True)
        (instance_dir / "instance.yaml").write_text(
            "version: 1\nname: test\n"
            f"data_dir: {self.data_dir}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        (instance_dir / "projects" / "secretary.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "secretary",
                    "repo": str(self.repo),
                    "enabled": True,
                    "adapter": "secretary",
                    "default_branch": "main",
                }
            ),
            encoding="utf-8",
        )
        return instance_dir

    def _repo(self) -> Path:
        """A real, tiny git repository: the workspace this product cuts is a real worktree."""
        repo = self.tmp / "project"
        repo.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        for argv in (
            ["git", "init", "--initial-branch=main", "-q"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "seed"],
        ):
            subprocess.run(argv, cwd=repo, env=env, check=True, capture_output=True)
        return repo

    def _backlog_card(self, reference: str = "secretary-run-1", column: int = 1) -> None:
        task_id = 40 + len(self.board.tasks)
        self.board.tasks.append(
            {
                "id": task_id,
                "reference": reference,
                "title": "A small task",
                "description": "Add a line to README.md.",
                "column_id": column,
                "position": 1,
                "swimlane_id": 4,
                "date_creation": 1720000000,
                "date_modification": 1720000000,
            }
        )
        self.board.metadata[task_id] = {"project": "secretary", "task_type": "code", "slug": "run"}
        self.board.comments[task_id] = []

    def layer(self, **kwargs) -> OperationLayer:
        options = {
            "data_dir": self.data_dir,
            "board_client": self.board,
            "registry": _registry(),
            "runtime_factory": lambda _root: self.runtime,
            "clock": lambda: self.clock,
            # A fake head is ready the moment it exists; the real wait is exercised against a real
            # TUI, not against a double that could only ever confirm the number it was given.
            "settle_seconds": 0.0,
        }
        options.update(kwargs)
        return OperationLayer(self.instance, **options)

    def reads(self) -> ReadLayer:
        return ReadLayer(
            self.instance,
            data_dir=self.data_dir,
            board_client=self.board,
            status_reader=lambda: {"schema_version": 1},
            clock=lambda: self.clock,
        )

    def start(self, request_id: str = "req-1", ref: str = "secretary-run-1") -> dict[str, Any]:
        return self.layer().run_start(ref, request_id=request_id, profile=WORKER_PROFILE)

    def reserve_sprint(self, project: str, sprint: str) -> None:
        (self.data_dir / "sprints" / "active-repositories.json").write_text(
            json.dumps({"version": 2, "projects": {project: [sprint]}}), encoding="utf-8"
        )


class StartTests(ProductRuntimeFixture):
    def test_a_start_owns_its_workspace_process_pid_and_logs(self) -> None:
        document = self.start()
        run = document["run"]
        self.assertEqual(document["kind"], "product_run")
        self.assertEqual(run["role"], "worker")
        self.assertEqual(run["runtime"], "local-pty")
        self.assertEqual(run["profile"], WORKER_PROFILE)
        # Every path a run leaves behind is under this product's own data plane.
        for key in ("workspace", "run_dir", "pid_file", "journal_path", "log_path", "result_path"):
            self.assertTrue(run[key].startswith(str(self.data_dir)), f"{key}: {run[key]}")
        self.assertTrue(Path(run["workspace"], ".git").exists())
        self.assertTrue(Path(run["run_dir"], "TASK.md").exists())
        self.assertGreater(run["head_pid"], 0)
        self.assertEqual(document["state"]["value"], "running")

    def test_the_task_document_is_written_outside_the_workspace(self) -> None:
        run = self.start()["run"]
        self.assertFalse(Path(run["workspace"], "TASK.md").exists())
        body = Path(run["run_dir"], "TASK.md").read_text(encoding="utf-8")
        self.assertIn("Add a line to README.md.", body)
        self.assertIn("SECRETARY_RUN_RESULT", body)

    def test_an_interactive_head_is_pointed_at_its_task_after_it_comes_up(self) -> None:
        """A TUI that is still drawing its banner takes a line into an unsent composer."""
        document = self.start(request_id="req-point")
        run = document["run"]
        self.assertIsNone(self.runtime.starts[0]["pointer"], "the head is raised bare")
        # Two deliveries, in the shape a keyboard has: the line, and then Enter as its own payload.
        # One payload carrying both is read as a paste and sends nothing, so the head would sit
        # idle with its prompt unsent and the run would read as started while it is not.
        self.assertEqual(len(self.runtime.deliveries), 2)
        (run_id, text, subject), (again, submit, submit_subject) = self.runtime.deliveries
        self.assertEqual(run_id, run["run_id"])
        self.assertIn(str(Path(run["run_dir"], "TASK.md")), text)
        self.assertEqual(subject, f"product-run:{run['run_id']}")
        self.assertEqual(again, run["run_id"])
        self.assertEqual(submit, ops_module.SUBMIT_KEY)
        self.assertEqual(submit_subject, f"product-run:{run['run_id']}:submit")

    def test_a_refused_submit_fails_the_run_rather_than_reading_as_sent(self) -> None:
        """A `HEAD_BUSY` accepted as success is a prompt nobody sent and a run nobody started."""
        real = self.runtime.deliver

        def deliver(run, pointer, *, subject="", **options):
            if pointer.text == ops_module.SUBMIT_KEY:
                return DeliverReceipt(status=HEAD_BUSY, reason="a turn is already running")
            return real(run, pointer, subject=subject, **options)

        self.runtime.deliver = deliver
        with self.assertRaises(RuntimeUnavailable) as refused:
            self.start(request_id="req-busy")
        self.assertIn("could not be told to send it", str(refused.exception))

    def test_a_task_that_cannot_be_put_in_front_of_a_head_fails_the_run(self) -> None:
        self.runtime.deliver = lambda run, pointer, **options: DeliverReceipt(
            status=HEAD_GONE, reason="the head is gone"
        )
        with self.assertRaises(RuntimeUnavailable) as refused:
            self.start(request_id="req-lost")
        self.assertIn("could not be put in front of it", str(refused.exception))

    def test_a_head_that_carries_its_prompt_on_its_command_line_gets_no_delivery(self) -> None:
        worker = self.start(request_id="req-worker")
        self.runtime.publish_result(worker["run"]["run_id"], {"status": "done"})
        self.layer().run_state(worker["run"]["run_id"])
        self.runtime.deliveries.clear()
        review = self.layer().run_review(
            request_id="rev-1", profile=REVIEWER_PROFILE, worker_run_id=worker["run"]["run_id"]
        )
        self.assertEqual(self.runtime.deliveries, [])
        self.assertIn(str(Path(review["review"]["run"]["run_dir"], "REVIEW.md")), self.runtime.commands[-1])

    def test_a_profile_on_the_other_backend_is_refused_rather_than_run_here(self) -> None:
        with self.assertRaises(ValidationRefused) as refused:
            self.layer().run_start("secretary-run-1", request_id="req-x", profile="orca-held-worker")
        self.assertIn("local-pty", str(refused.exception))
        self.assertEqual(self.runtime.starts, [])

    def test_an_unknown_profile_is_a_typed_refusal(self) -> None:
        with self.assertRaises(ValidationRefused):
            self.layer().run_start("secretary-run-1", request_id="req-y", profile="no-such-head")

    def test_a_bring_up_that_fails_closes_the_run_it_opened(self) -> None:
        """A card is never left owned by a run whose head was never raised."""

        def refuse(*_args, **_kwargs):
            raise OSError("the supervisor would not come up")

        self.runtime.start = refuse
        # `RuntimeUnavailable`, not the backend's own `OSError`: since
        # :mod:`secretary.webproto.boundary` the layer's contract is that a caller sees a protocol
        # code, and this is the case that class was written for -- "the product runtime could not
        # raise, reach or record a head". What this test is about is unchanged and asserted below:
        # the run the failed bring-up opened is closed, and the card is admitted again.
        with self.assertRaises(RuntimeUnavailable) as refused:
            self.start(request_id="req-broken")
        self.assertEqual(refused.exception.code, "backend_unavailable")
        self.assertIn("would not come up", refused.exception.message)
        store = RunStore(self.data_dir)
        broken = store.by_request("req-broken")
        self.assertIsNotNone(broken)
        self.assertEqual(broken.settled_state, "process_failed")
        self.assertIn("could not be raised", broken.settled_reason)
        # And the next run of the card is admitted rather than fenced out by the failed one.
        self.runtime = FakeHeadRuntime(self.data_dir / "webproto" / "heads")
        self.assertEqual(self.start(request_id="req-after")["state"]["value"], "running")


class IdempotencyTests(ProductRuntimeFixture):
    def test_the_same_request_id_twice_is_one_run_one_process_and_one_workspace(self) -> None:
        first = self.start(request_id="req-same")
        second = self.start(request_id="req-same")
        self.assertEqual(first["run"]["run_id"], second["run"]["run_id"])
        self.assertEqual(len(self.runtime.starts), 1)
        self.assertEqual(
            len(list((self.data_dir / "webproto" / "workspaces").iterdir())),
            1,
        )

    def test_a_reconnecting_client_reads_the_same_run_rather_than_starting_one(self) -> None:
        first = self.start(request_id="req-reconnect")
        # A whole new layer object: nothing of the first call is remembered in this process.
        again = self.layer().run_start("secretary-run-1", request_id="req-reconnect", profile=WORKER_PROFILE)
        self.assertEqual(again["run"]["run_id"], first["run"]["run_id"])
        self.assertEqual(len(self.runtime.starts), 1)

    def test_a_second_request_id_over_a_live_run_is_refused_as_a_second_owner(self) -> None:
        self.start(request_id="req-first")
        with self.assertRaises(OwnerConflict) as refused:
            self.start(request_id="req-second")
        self.assertIn("unsettled product run", str(refused.exception))
        self.assertEqual(len(self.runtime.starts), 1)

    def test_a_review_is_idempotent_on_its_own_request_id(self) -> None:
        worker = self._settled_worker()
        first = self.layer().run_review(
            request_id="rev-1", profile=REVIEWER_PROFILE, worker_run_id=worker["run"]["run_id"]
        )
        again = self.layer().run_review(
            request_id="rev-1", profile=REVIEWER_PROFILE, worker_run_id=worker["run"]["run_id"]
        )
        self.assertEqual(first["review"]["run"]["run_id"], again["review"]["run"]["run_id"])
        self.assertEqual(len(self.runtime.starts), 2)

    def test_a_request_id_belongs_to_the_operation_it_was_made_under(self) -> None:
        """A worker's request id repeated on `review` is refused, not aliased to the worker run.

        The failure this pins down returned a `product_review` document whose run was the worker's
        own -- `role == "worker"`, no parent run -- while no reviewer process was ever raised: a
        caller was told a review had happened when none had.
        """
        worker = self._settled_worker()
        with self.assertRaises(ValidationRefused) as refused:
            self.layer().run_review(
                request_id="req-worker",
                profile=REVIEWER_PROFILE,
                worker_run_id=worker["run"]["run_id"],
            )
        self.assertIn("run_start", str(refused.exception))
        self.assertEqual(len(self.runtime.starts), 1)

    def test_a_repeat_with_different_inputs_is_refused_rather_than_answered(self) -> None:
        self._backlog_card("secretary-run-2")
        self.start(request_id="req-same")
        with self.assertRaises(ValidationRefused) as refused:
            self.start(request_id="req-same", ref="secretary-run-2")
        self.assertIn("different inputs", str(refused.exception))
        self.assertEqual(len(self.runtime.starts), 1)

    def test_a_review_repeated_with_a_different_worker_is_refused(self) -> None:
        worker = self._settled_worker()
        self.layer().run_review(
            request_id="rev-1", profile=REVIEWER_PROFILE, worker_run_id=worker["run"]["run_id"]
        )
        with self.assertRaises(ValidationRefused):
            self.layer().run_review(
                request_id="rev-1", profile=REVIEWER_PROFILE, worker_run_id="pr-someone-else"
            )
        self.assertEqual(len(self.runtime.starts), 2)

    def _settled_worker(self) -> dict[str, Any]:
        document = self.start(request_id="req-worker")
        run_id = document["run"]["run_id"]
        self.runtime.publish_result(run_id, {"status": "done", "summary": "added a line"})
        return self.layer().run_state(run_id)


class ReviewTests(ProductRuntimeFixture):
    def test_a_review_waits_for_the_worker_run_to_end(self) -> None:
        worker = self.start(request_id="req-worker")
        with self.assertRaises(OwnerConflict) as refused:
            self.layer().run_review(
                request_id="rev-1", profile=REVIEWER_PROFILE, worker_run_id=worker["run"]["run_id"]
            )
        self.assertIn("raised by a worker's result", str(refused.exception))
        self.assertEqual(len(self.runtime.starts), 1)

    def test_a_review_runs_in_the_worker_workspace_and_is_handed_its_result(self) -> None:
        worker = self.start(request_id="req-worker")
        worker_id = worker["run"]["run_id"]
        self.runtime.publish_result(worker_id, {"status": "done", "summary": "added a line"})
        self.layer().run_state(worker_id)
        document = self.layer().run_review(
            request_id="rev-1", profile=REVIEWER_PROFILE, worker_run_id=worker_id
        )
        review = document["review"]["run"]
        self.assertEqual(document["kind"], "product_review")
        self.assertEqual(review["role"], "reviewer")
        self.assertEqual(review["parent_run_id"], worker_id)
        self.assertEqual(review["workspace"], worker["run"]["workspace"])
        prompt = Path(review["run_dir"], "REVIEW.md").read_text(encoding="utf-8")
        self.assertIn(worker_id, prompt)
        self.assertIn("added a line", document["worker"]["state"]["result"]["value"]["summary"])

    def test_a_verdict_is_read_off_the_reviewer_own_result(self) -> None:
        worker = self.start(request_id="req-worker")
        worker_id = worker["run"]["run_id"]
        self.runtime.publish_result(worker_id, {"status": "done"})
        self.layer().run_state(worker_id)
        review_id = self.layer().run_review(
            request_id="rev-1", profile=REVIEWER_PROFILE, worker_run_id=worker_id
        )["review"]["run"]["run_id"]
        self.runtime.publish_result(review_id, {"verdict": "green", "summary": "it stands"})
        state = self.layer().run_state(review_id)["state"]
        self.assertEqual(state["value"], "finished")
        self.assertEqual(state["result"]["verdict"], "green")

    def test_a_card_with_no_worker_run_has_nothing_to_review(self) -> None:
        with self.assertRaises(RunNotFound):
            self.layer().run_review(request_id="rev-1", profile=REVIEWER_PROFILE, ref="secretary-run-1")


class AdmissionTests(ProductRuntimeFixture):
    """Criterion 4: one gate, one order, and every start path through it."""

    def test_an_open_sprint_reservation_refuses_the_run(self) -> None:
        self.reserve_sprint("secretary", "sprint:1427")
        with self.assertRaises(OwnerConflict) as refused:
            self.start()
        self.assertIn("sprint:1427", str(refused.exception))
        self.assertEqual(self.runtime.starts, [])

    def test_a_card_in_the_dispatcher_lane_is_refused_by_its_state(self) -> None:
        # `secretary-510-pilot` is the fixture board's Ready card: the dispatcher's own lane.
        with self.assertRaises(OwnerConflict) as refused:
            self.layer().run_start("secretary-510-pilot", request_id="req-lane", profile=WORKER_PROFILE)
        self.assertIn("production dispatcher's lane", str(refused.exception))

    def test_a_durable_dispatcher_record_refuses_the_run(self) -> None:
        (self.data_dir / "dispatcher" / "production-state.json").write_text(
            json.dumps({"records": {"secretary-run-1": {"attempt_id": "a", "state": "validate"}}}),
            encoding="utf-8",
        )
        with self.assertRaises(OwnerConflict) as refused:
            self.start()
        self.assertIn("durable record", str(refused.exception))

    def test_an_unreadable_dispatcher_state_refuses_rather_than_assumes(self) -> None:
        (self.data_dir / "dispatcher" / "production-state.json").write_text("{oops", encoding="utf-8")
        with self.assertRaises(OwnerConflict) as refused:
            self.start()
        self.assertIn("could not be read", str(refused.exception))

    def test_an_unregistered_project_is_refused(self) -> None:
        self._backlog_card("other-run-1")
        task_id = self.board.tasks[-1]["id"]
        self.board.metadata[task_id]["project"] = "not-registered"
        with self.assertRaises(ValidationRefused):
            self.layer().run_start("other-run-1", request_id="req-p", profile=WORKER_PROFILE)

    def test_every_start_path_goes_through_the_one_gate(self) -> None:
        """Both operations that raise a head call `admit`, and neither has a branch around it."""
        worker = self.start(request_id="req-worker")
        self.runtime.publish_result(worker["run"]["run_id"], {"status": "done"})
        self.layer().run_state(worker["run"]["run_id"])
        seen: list[str] = []
        real = admission_module.admit

        def watched(ref, **kwargs):
            seen.append(ref)
            return real(ref, **kwargs)

        with mock.patch.object(ops_module, "admit", watched):
            self.layer().run_review(
                request_id="rev-1",
                profile=REVIEWER_PROFILE,
                worker_run_id=worker["run"]["run_id"],
            )
            self._backlog_card("secretary-run-2")
            with contextlib.suppress(OwnerConflict):
                self.layer().run_start("secretary-run-2", request_id="req-2", profile=WORKER_PROFILE)
        self.assertEqual(seen, ["secretary-run-1", "secretary-run-2"])

    def test_the_gate_decides_in_the_order_it_documents(self) -> None:
        """Six refusals at once, and the first of the documented six is the one that answers.

        The order is part of the contract and not an implementation detail: an unregistered project
        must not be reported as a sprint reservation, and a card the dispatcher already holds must
        not be reported as one this layer holds. Peeling them off one at a time is what proves the
        order rather than the set.
        """
        self._backlog_card("secretary-run-3")
        task_id = self.board.tasks[-1]["id"]
        self.board.metadata[task_id]["project"] = "not-registered"
        self.reserve_sprint("secretary", "sprint:1427")
        (self.data_dir / "dispatcher" / "production-state.json").write_text(
            json.dumps({"records": {"secretary-run-3": {"state": "validate"}}}), encoding="utf-8"
        )

        def refusal(ref: str) -> str:
            with self.assertRaises(Exception) as raised:
                self.layer().run_start(ref, request_id=f"req-{ref}", profile=WORKER_PROFILE)
            return str(raised.exception)

        # 1. the card exists, before anything else is asked.
        self.assertIn("holds no card", refusal("secretary-does-not-exist"))
        # 2. its project is registered, before the reservation index is consulted.
        self.assertIn("not registered", refusal("secretary-run-3"))
        self.board.metadata[task_id]["project"] = "secretary"
        # 3. the reservation, before the card's own state.
        self.assertIn("sprint:1427", refusal("secretary-run-3"))
        self.reserve_sprint("other", "sprint:1427")
        # 4. the card's state, before the dispatcher's durable record.
        self.board.tasks[-1]["column_id"] = 2
        self.assertIn("dispatcher's lane", refusal("secretary-run-3"))
        self.board.tasks[-1]["column_id"] = 1
        # 5. the dispatcher's record, before this layer's own runs.
        self.assertIn("durable record", refusal("secretary-run-3"))


    # -- the fence reads one fact ---------------------------------------------------------------

    def _record(self, run_id: str, **overrides: Any) -> ProductRun:
        """One run of the fixture's card, written into the store as it stands."""
        payload = {
            "run_id": run_id,
            "request_id": f"req-{run_id}",
            "ref": "secretary-run-1",
            "project": "secretary",
            "role": "worker",
            "profile": WORKER_PROFILE,
            "started_at": self.clock,
        }
        payload.update(overrides)
        run = ProductRun.from_json(payload)
        RunStore(self.data_dir).save(run)
        return run

    def _admit(self) -> Any:
        return admission_module.admit(
            "secretary-run-1",
            report=self.layer().report(),
            data_dir=self.data_dir,
            board=self.board,
            store=RunStore(self.data_dir),
            production_state=self.data_dir / "dispatcher" / "production-state.json",
        )

    def test_the_fence_is_lifted_by_the_run_being_over_and_never_by_what_it_ended_as(self) -> None:
        """Criterion 3 of secretary-1563, in both directions and over all five values.

        The gate's sixth condition asks one question -- is the run that already sits on this card
        over -- and a run that is over frees the card whatever it ended as. `source_unavailable`
        and `unknown` are in the list deliberately: under the old contract only `finished` and
        `process_failed` freed a card, which is precisely why a run confirmed over with unreadable
        evidence had to be given a value it had not earned.
        """
        for value in ("finished", "process_failed", "source_unavailable", "unknown", "running"):
            with self.subTest(value=value):
                run = self._record(
                    f"pr-over-{value}",
                    phase="settled",
                    ended=True,
                    settled_state=value,
                    settled_reason=f"this run ended as {value}",
                    settled_at=self.clock,
                )
                self.assertEqual(self._admit().runs[-1].run_id, run.run_id)
                (self.data_dir / "webproto" / "runs" / f"{run.run_id}.json").unlink()

        # And a run that is not over fences the card, whatever it may say about a process: an
        # unresolved run carries no ending at all, and a head may still be alive under it.
        self._record("pr-in-doubt", phase="unresolved", unresolved_reason="the stop was refused")
        with self.assertRaises(OwnerConflict) as refused:
            self._admit()
        self.assertIn("pr-in-doubt", str(refused.exception))

    def test_no_branch_of_the_gate_reads_what_a_run_ended_as(self) -> None:
        """The static half: the outcome vocabulary is not reachable from this module's code.

        A behavioural check can only sample the values; this says the question is not asked at all.
        Docstrings are excluded on purpose -- the module explains the distinction at length, and
        naming a value while explaining why it is not consulted is not consulting it.
        """
        import ast

        tree = ast.parse(
            (REPO_ROOT / "src" / "secretary" / "webproto" / "admission.py").read_text(encoding="utf-8")
        )
        forbidden = {"running", "finished", "process_failed", "source_unavailable", "unknown"}
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
                body = node.body[1:] if ast.get_docstring(node) else node.body
                for statement in body:
                    for inner in ast.walk(statement):
                        if isinstance(inner, ast.Constant) and inner.value in forbidden:
                            offenders.append(f"line {inner.lineno}: {inner.value!r}")
                        if isinstance(inner, ast.Attribute) and inner.attr in {
                            "settled_state",
                            "settled_reason",
                        }:
                            offenders.append(f"line {inner.lineno}: .{inner.attr}")
        self.assertEqual(sorted(set(offenders)), [])


class RunStateTests(ProductRuntimeFixture):
    """Criterion 5: five outcomes, told apart, out of the read layer's own five values."""

    def _run(self, **overrides) -> ProductRun:
        run_dir = self.tmp / "rundir"
        run_dir.mkdir(exist_ok=True)
        payload = {
            "run_id": "pr-test",
            "request_id": "req",
            "ref": "secretary-run-1",
            "project": "secretary",
            "role": "worker",
            "profile": WORKER_PROFILE,
            "run_dir": str(run_dir),
            "pid_file": str(run_dir / "head.pid"),
            "journal_path": str(run_dir / "journal.jsonl"),
            "result_path": str(run_dir / "result.json"),
            "head_run": {"run_id": "pr-test"},
        }
        payload.update(overrides)
        return ProductRun.from_json(payload)

    def _exited(self, run: ProductRun, *, exit_code: int | None, signal: int | None) -> None:
        Path(run.journal_path).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "seq": 2,
                    "kind": RUN_EXITED,
                    "run_id": run.run_id,
                    "at": 2.0,
                    "exit_code": exit_code,
                    "signal": signal,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _dead_heartbeat(self, run: ProductRun) -> None:
        Path(run.pid_file).write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": _dead_pid(),
                    "boot_id": "boot",
                    "proc_starttime_ticks": "42",
                    "run_id": run.run_id,
                    "role": run.role,
                    "task": run.ref,
                }
            ),
            encoding="utf-8",
        )

    def test_a_live_matching_process_is_running(self) -> None:
        run = self._run()
        publish_heartbeat(run.pid_file, run_state.expected_identity(run))
        state = run_state.observe(run, now=self.clock)
        self.assertEqual(state["value"], "running")
        self.assertFalse(state["ended"])

    def test_a_published_result_and_an_ended_process_is_a_normal_completion(self) -> None:
        run = self._run()
        self._dead_heartbeat(run)
        self._exited(run, exit_code=None, signal=15)
        Path(run.result_path).write_text(json.dumps({"status": "done"}), encoding="utf-8")
        state = run_state.observe(run, now=self.clock)
        self.assertEqual(state["value"], "finished")
        self.assertEqual(state["result"]["value"], {"status": "done"})
        self.assertEqual(state["exit"]["signal"], 15)

    def test_a_zero_exit_without_a_result_is_still_a_normal_ending(self) -> None:
        run = self._run()
        self._dead_heartbeat(run)
        self._exited(run, exit_code=0, signal=None)
        state = run_state.observe(run, now=self.clock)
        self.assertEqual(state["value"], "finished")
        self.assertEqual(state["exit"]["code"], 0)

    def test_a_non_zero_exit_is_a_failure_that_carries_its_status(self) -> None:
        run = self._run()
        self._dead_heartbeat(run)
        self._exited(run, exit_code=17, signal=None)
        state = run_state.observe(run, now=self.clock)
        self.assertEqual(state["value"], "process_failed")
        self.assertEqual(state["exit"], {"code": 17, "signal": None, "at": 2.0})
        self.assertIn("exited with status 17", state["reason"])

    def test_a_process_that_died_is_told_apart_from_one_that_exited(self) -> None:
        run = self._run()
        self._dead_heartbeat(run)
        self._exited(run, exit_code=None, signal=9)
        state = run_state.observe(run, now=self.clock)
        self.assertEqual(state["value"], "process_failed")
        self.assertEqual(state["exit"]["code"], None)
        self.assertEqual(state["exit"]["signal"], 9)
        self.assertIn("signal 9", state["reason"])

    def test_a_process_that_is_simply_gone_says_so(self) -> None:
        run = self._run()
        self._dead_heartbeat(run)
        state = run_state.observe(run, now=self.clock)
        self.assertEqual(state["value"], "process_failed")
        self.assertIn("nothing recorded how it ended", state["reason"])

    def test_an_unreadable_launch_identity_is_a_source_failure_not_a_dead_run(self) -> None:
        run = self._run()
        Path(run.pid_file).write_text("{not json", encoding="utf-8")
        state = run_state.observe(run, now=self.clock)
        self.assertEqual(state["value"], "source_unavailable")
        self.assertEqual(state["source"]["state"], "unavailable")
        # The same value a confirmed ending with an unreadable journal settles as, and the fact
        # beside it is what tells the two apart: nothing here says this run's process is gone.
        self.assertFalse(state["ended"])

    def test_one_value_answers_two_questions_and_they_are_read_separately(self) -> None:
        """`source_unavailable` says nothing about whether a run is over, so `ended` says it.

        The same value over two situations, and the fact beside it is what tells them apart: a head
        that is confirmed gone while its journal cannot be read has *ended* and something must
        record that; a run whose launch identity itself cannot be read has not been shown to end
        anything. Deriving "is it over" from the value would have to answer both the same way.
        """
        gone = self._run()
        self._dead_heartbeat(gone)
        Path(gone.journal_path).mkdir()
        ended = run_state.observe(gone, now=self.clock)
        self.assertEqual(ended["value"], "source_unavailable")
        self.assertTrue(ended["ended"])
        self.assertIn("could not be read", ended["reason"])

        unreadable = self._run()
        Path(unreadable.pid_file).write_text("{not json", encoding="utf-8")
        still_open = run_state.observe(unreadable, now=self.clock)
        self.assertEqual(still_open["value"], "source_unavailable")
        self.assertFalse(still_open["ended"])

    def test_no_evidence_yet_is_unknown_rather_than_absent(self) -> None:
        state = run_state.observe(self._run(), now=self.clock)
        self.assertEqual(state["value"], "unknown")
        self.assertIn("has not published a launch heartbeat", state["reason"])
        never_raised = run_state.observe(self._run(head_run={}), now=self.clock)
        self.assertEqual(never_raised["value"], "unknown")
        self.assertIn("no head has been raised", never_raised["reason"])

    def test_a_window_is_never_consulted(self) -> None:
        """Criterion 5's last sentence: a pane or panel is not evidence of a live run."""
        source = (REPO_ROOT / "src" / "secretary" / "webproto" / "run_state.py").read_text(encoding="utf-8")
        for word in ("pane", "terminal list", "session"):
            self.assertNotIn(f"{word}(", source)


class SettlingTests(ProductRuntimeFixture):
    def test_a_published_result_ends_the_head_this_product_owns(self) -> None:
        document = self.start()
        run_id = document["run"]["run_id"]
        self.runtime.publish_result(run_id, {"status": "done", "summary": "ok"})
        settled = self.layer().run_state(run_id)
        self.assertEqual(settled["state"]["value"], "finished")
        self.assertEqual(self.runtime.stops, [(run_id, ops_module.STOP_RESULT_IN)])

    def test_a_run_past_its_deadline_is_ended_without_a_result(self) -> None:
        document = self.layer(deadline_seconds=10.0).run_start(
            "secretary-run-1", request_id="req-deadline", profile=WORKER_PROFILE
        )
        run_id = document["run"]["run_id"]
        self.clock += 11.0
        settled = self.layer(deadline_seconds=10.0).run_state(run_id)
        self.assertEqual(self.runtime.stops, [(run_id, ops_module.STOP_DEADLINE)])
        self.assertEqual(settled["state"]["value"], "process_failed")

    def test_a_settled_run_says_the_same_thing_forever(self) -> None:
        document = self.start()
        run_id = document["run"]["run_id"]
        self.runtime.publish_result(run_id, {"status": "done"})
        first = self.layer().run_state(run_id)
        # The whole run directory is swept afterwards; the recorded ending is unchanged.
        for path in (self.data_dir / "webproto" / "heads" / run_id).iterdir():
            path.unlink()
        again = self.layer().run_state(run_id)
        self.assertEqual(again["state"]["value"], first["state"]["value"])
        self.assertEqual(again["state"]["reason"], first["state"]["reason"])

    def test_an_unknown_run_is_a_typed_not_found(self) -> None:
        with self.assertRaises(RunNotFound):
            self.layer().run_state("pr-nothing")


class EventVisibilityTests(ProductRuntimeFixture):
    """Criterion 6: the existing `task_events` and `task_snapshot`, and no second history."""

    def test_the_run_is_read_back_through_the_read_layer_that_already_exists(self) -> None:
        document = self.start()
        run_id = document["run"]["run_id"]
        page = self.reads().task_events("secretary-run-1", None, limit=10)
        started = [item for item in page["items"] if item["kind"] == run_events.STARTED]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["data"]["run_id"], run_id)
        self.assertEqual(started[0]["data"]["pid_file"], document["run"]["pid_file"])

        self.runtime.publish_result(run_id, {"status": "done"})
        self.layer().run_state(run_id)
        snapshot = self.reads().task_snapshot("secretary-run-1")
        kinds = [item["kind"] for item in snapshot["events"]["items"]]
        self.assertEqual(kinds, [run_events.STARTED, run_events.FINISHED])
        finished = snapshot["events"]["items"][-1]
        self.assertEqual(finished["data"]["state"], "finished")
        self.assertEqual(finished["data"]["result"], {"status": "done"})

    def test_the_cursor_still_continues_across_the_run_events(self) -> None:
        document = self.start()
        run_id = document["run"]["run_id"]
        first = self.reads().task_events("secretary-run-1", None, limit=10)
        self.assertEqual([item["kind"] for item in first["items"]], [run_events.STARTED])
        self.runtime.publish_result(run_id, {"status": "done"})
        self.layer().run_state(run_id)
        after = self.reads().task_events("secretary-run-1", first["next_cursor"], limit=10)
        self.assertEqual([item["kind"] for item in after["items"]], [run_events.FINISHED])

    def test_the_ending_is_published_once_however_often_it_is_observed(self) -> None:
        run_id = self.start()["run"]["run_id"]
        self.runtime.publish_result(run_id, {"status": "done"})
        for _ in range(3):
            self.layer().run_state(run_id)
        page = self.reads().task_events("secretary-run-1", None, limit=50)
        finished = [item for item in page["items"] if item["kind"] == run_events.FINISHED]
        self.assertEqual(len(finished), 1)

    def test_a_start_event_lost_to_one_journal_failure_is_recovered_by_the_retry(self) -> None:
        """Criterion 6 has to survive a journal that is briefly unavailable.

        The head is up and its run record is durable; publishing its `product_run.started` fails
        once. The retry the idempotency contract invites finds the record -- and must publish the
        event it owes rather than only hand the document back, or the launch is invisible forever.
        """
        with mock.patch.object(
            ops_module.run_events, "publish_started", side_effect=RuntimeUnavailable("journal down")
        ):
            with self.assertRaises(RuntimeUnavailable):
                self.start(request_id="lost-start-event")
        self.assertEqual(
            self.reads().task_events("secretary-run-1", None, limit=10)["items"], []
        )

        again = self.start(request_id="lost-start-event")
        self.assertEqual(len(self.runtime.starts), 1)
        page = self.reads().task_events("secretary-run-1", None, limit=10)
        started = [item for item in page["items"] if item["kind"] == run_events.STARTED]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["data"]["run_id"], again["run"]["run_id"])

    def test_an_ending_lost_to_one_journal_failure_is_recovered_by_the_next_read(self) -> None:
        """The same hole under `run_state`: the settle is durable before its publication is."""
        run_id = self.start()["run"]["run_id"]
        self.runtime.publish_result(run_id, {"status": "done"})
        with mock.patch.object(
            ops_module.run_events, "publish_finished", side_effect=RuntimeUnavailable("journal down")
        ):
            with self.assertRaises(RuntimeUnavailable):
                self.layer().run_state(run_id)
        kinds = [item["kind"] for item in self.reads().task_events("secretary-run-1", None, limit=10)["items"]]
        self.assertEqual(kinds, [run_events.STARTED])

        state = self.layer().run_state(run_id)
        self.assertEqual(state["state"]["value"], "finished")
        page = self.reads().task_events("secretary-run-1", None, limit=10)
        finished = [item for item in page["items"] if item["kind"] == run_events.FINISHED]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["data"]["result"], {"status": "done"})

    def test_a_recovered_ending_republishes_the_same_record_after_a_sweep(self) -> None:
        """The republished event is the record the journal already holds, byte for byte.

        A terminal event whose payload were re-derived from the run directory would differ once
        that directory had been swept, and `TaskAudit` would refuse it as a different payload under
        a taken request id -- so the recovery above would fail exactly when it is needed.
        """
        run_id = self.start()["run"]["run_id"]
        self.runtime.publish_result(run_id, {"status": "done"})
        self.layer().run_state(run_id)
        for path in (self.data_dir / "webproto" / "heads" / run_id).iterdir():
            path.unlink()
        for _ in range(2):
            self.layer().run_state(run_id)
        page = self.reads().task_events("secretary-run-1", None, limit=50)
        finished = [item for item in page["items"] if item["kind"] == run_events.FINISHED]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["data"]["result"], {"status": "done"})

    def test_no_second_history_is_written_anywhere(self) -> None:
        run_id = self.start()["run"]["run_id"]
        self.runtime.publish_result(run_id, {"status": "done"})
        self.layer().run_state(run_id)
        journals = sorted(
            path.relative_to(self.data_dir).as_posix() for path in self.data_dir.rglob("*.ndjson")
        )
        self.assertEqual(journals, ["board/events.ndjson"])


class LifecycleTests(ProductRuntimeFixture):
    """The structural half of round 3: one place owns every spawn and every close.

    Three defects of this card had one shape -- "a process may now exist" and "the durable record
    says so" were ordered differently in three places. These are the checks that there is now one
    place, that it writes ahead of the spawn, that ownership is recoverable from the record alone,
    and that a cleanup it could not confirm is never written as a terminal outcome.
    """

    # -- the one place -------------------------------------------------------------------------

    @contextlib.contextmanager
    def _only_inside_advance(self):
        """Detonate if a spawn or a close is reached with no `RunLifecycle.advance` on the stack.

        The same shape as `test_every_start_path_goes_through_the_one_gate`, over the other
        invariant: `runtime.start`, `runtime.stop` and `RunStore.settle` are the three verbs that
        can put a process into the world or end a run, and none of them may be reached around the
        function that owns the order between them.
        """
        depth = [0]
        seen: list[tuple[str, str]] = []
        real_advance = lifecycle_module.RunLifecycle.advance

        def advance(layer, run, to, **kwargs):
            depth[0] += 1
            seen.append((run.run_id, to))
            try:
                return real_advance(layer, run, to, **kwargs)
            finally:
                depth[0] -= 1

        def guarded(name, real):
            def call(*args, **kwargs):
                if depth[0] == 0:
                    raise AssertionError(f"{name} was reached outside RunLifecycle.advance")
                return real(*args, **kwargs)

            return call

        real_start, real_stop = self.runtime.start, self.runtime.stop
        self.runtime.start = guarded("the backend's start", real_start)
        self.runtime.stop = guarded("the backend's stop", real_stop)
        try:
            with (
                mock.patch.object(lifecycle_module.RunLifecycle, "advance", advance),
                mock.patch.object(RunStore, "settle", guarded("RunStore.settle", RunStore.settle)),
            ):
                yield seen
        finally:
            self.runtime.start, self.runtime.stop = real_start, real_stop

    def test_every_spawning_or_closing_path_goes_through_the_one_place(self) -> None:
        self._backlog_card("secretary-run-2")
        with self._only_inside_advance() as seen:
            # 1. a start that succeeds
            worker = self.start(request_id="req-one")
            worker_id = worker["run"]["run_id"]
            # 2. a read that closes the run on its published result
            self.runtime.publish_result(worker_id, {"status": "done"})
            self.layer().run_state(worker_id)
            # 3. a review, which raises a second head
            review = self.layer().run_review(
                request_id="rev-one", profile=REVIEWER_PROFILE, worker_run_id=worker_id
            )
            review_id = review["review"]["run"]["run_id"]
            # 4. a read that closes a run on its deadline
            self.clock += 3601.0
            self.layer().run_state(review_id)
            # 5. a bring-up that fails after the backend has spawned
            self.runtime.deliver = lambda run, pointer, **options: DeliverReceipt(
                status=HEAD_GONE, reason="the head is gone"
            )
            with self.assertRaises(RuntimeUnavailable):
                self.start(request_id="req-two", ref="secretary-run-2")
        phases = {run_id: [to for owner, to in seen if owner == run_id] for run_id, _ in seen}
        self.assertEqual(phases[worker_id][:2], ["raising", "raised"])
        self.assertIn("settled", phases[worker_id])
        self.assertEqual(phases[review_id][:2], ["raising", "raised"])
        self.assertIn("settled", phases[review_id])
        broken = RunStore(self.data_dir).by_request("req-two")
        self.assertEqual(phases[broken.run_id], ["raising", "raised", "settled"])

    def test_the_three_verbs_are_named_in_one_module_of_the_layer(self) -> None:
        """The static half: no other module of the layer can even reach a spawn or a settle."""
        import ast

        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "src" / "secretary" / "webproto").glob("*.py")):
            if path.name == "lifecycle.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"start", "stop", "settle"}:
                        offenders.append(f"{path.name}:{node.lineno} .{node.func.attr}()")
        self.assertEqual(offenders, [])

    # -- write-ahead, and ownership recovered from disk -----------------------------------------

    def test_the_record_can_address_the_head_before_the_spawn_returns(self) -> None:
        """Criterion of round 3, point 1: the write-ahead exists before a process can.

        Captured at the moment the backend's `start` is entered -- which is the first instant a
        process can exist -- and read back out of the store rather than out of this process.
        """
        captured: list[ProductRun] = []
        real_start = self.runtime.start

        def start(*args, **kwargs):
            captured.append(RunStore(self.data_dir).get(kwargs["run_id"]))
            return real_start(*args, **kwargs)

        self.runtime.start = start
        document = self.start(request_id="req-ahead")
        ahead = captured[0]
        self.assertIsNotNone(ahead, "the run record is durable before the spawn")
        self.assertEqual(ahead.phase, "raising")
        self.assertTrue(Path(ahead.run_dir).is_dir(), "the run directory exists before the spawn")
        self.assertEqual(ahead.pid_file, document["run"]["pid_file"])
        head = HeadRun.from_json(ahead.head_run)
        self.assertEqual(head.run_id, ahead.run_id)
        self.assertEqual(head.pid_file, ahead.pid_file)
        self.assertEqual(head.role, "worker")

    def test_a_head_is_addressable_from_the_write_ahead_record_alone(self) -> None:
        """The addressing half of point 2, and only that half.

        `LocalPtyHeadRuntime.stop` reaches a head through `_address`, which derives the run
        directory from `root/run_id`, the socket and journal from that directory, and the pid file
        from the run's own `pid_file`; nothing it consults is remembered by the process that
        spawned. This asserts that derivation lands on this product's own paths, with a `HeadRun`
        rebuilt out of the store and nothing else -- and it asserts nothing about a process,
        because it starts and stops none. The lifecycle claim itself, a real head ended by a
        recovered record and confirmed gone, is executed in `RealHeadOwnershipTests`.
        """
        from triggered_agents.runtime.local_pty_head import LocalPtyHeadRuntime

        run_id = self.start(request_id="req-address")["run"]["run_id"]
        stored = RunStore(self.data_dir).get(run_id)
        backend = LocalPtyHeadRuntime(
            self.data_dir / "webproto" / "heads",
            head_process_status=lambda *args, **kwargs: {"state": "dead"},
        )
        address = backend._address(HeadRun.from_json(stored.head_run))
        self.assertIsNotNone(address)
        self.assertEqual(address.run_dir, Path(stored.run_dir))
        self.assertEqual(address.pid_file, Path(stored.pid_file))
        self.assertEqual(address.journal_path, Path(stored.journal_path))

    # -- a cleanup that could not be confirmed --------------------------------------------------

    def _failing_save(self, phase: str = RAISED):
        """Fail exactly the durable write that binds a raised head, as the reviewer's repro does."""
        real = RunStore.save

        def save(store, run):
            if run.phase == phase:
                raise RunStoreError("disk write failed")
            return real(store, run)

        return mock.patch.object(RunStore, "save", save)

    def test_a_save_failure_after_a_successful_start_leaves_no_live_head(self) -> None:
        """The reviewer's scenario, with the cleanup confirmed: no orphan, and the card is free."""
        with self._failing_save():
            with self.assertRaises(RuntimeUnavailable) as refused:
                self.start(request_id="req-lost-save")
        self.assertIn("disk write failed", str(refused.exception))
        self.assertEqual(len(self.runtime.starts), 1)
        run = RunStore(self.data_dir).by_request("req-lost-save")
        self.assertEqual(self.runtime.stops, [(run.run_id, lifecycle_module.STOP_BRING_UP_FAILED)])
        self.assertEqual(run.settled_state, "process_failed")
        # The head was ended and confirmed gone, so the card is genuinely free for the next run.
        self.assertEqual(self.start(request_id="req-after")["state"]["value"], "running")

    def test_an_unconfirmed_cleanup_is_never_written_as_a_terminal_outcome(self) -> None:
        """The same scenario with the stop refused: unresolved, `unknown`, and the card is fenced.

        This is the invariant the three point repairs kept missing: a record that settles over a
        process nobody ended frees the card for a second owner beside a live head.
        """
        self.runtime.stop = lambda run, initiator, **options: StopReceipt(
            status=HEAD_ALIVE, run=run, reason="the head's process outlived the stop it was sent"
        )
        with self._failing_save():
            with self.assertRaises(RuntimeUnavailable):
                self.start(request_id="req-in-doubt")
        run = RunStore(self.data_dir).by_request("req-in-doubt")
        self.assertEqual(run.phase, UNRESOLVED)
        self.assertFalse(run.ended, "an unconfirmed cleanup is not an ending")
        self.assertIn("may still be running", run.unresolved_reason)

        # It reads through the layer as `unknown`, never as a terminal outcome.
        state = self.layer().run_state(run.run_id)["state"]
        self.assertEqual(state["value"], "unknown")
        self.assertFalse(state["ended"])

        # And no second run of the card is admitted while ownership is in doubt.
        with self.assertRaises(OwnerConflict) as refused:
            self.start(request_id="req-second")
        self.assertIn("unsettled product run", str(refused.exception))

    def test_a_recovered_run_that_published_a_result_settles_as_finished(self) -> None:
        """The recovery path must tell a normal ending apart from a failure. AC 5, at its edge.

        The sequence is the one the `unresolved` phase exists for: a save fails after a successful
        start, one stop cannot be confirmed, and the head then goes on to do its work, publish its
        result and end. The read that finally confirms the stop is holding positive evidence of a
        run that *finished* -- and must classify from that evidence rather than from the record's
        own `unresolved` shortcut, which would settle a published success as `process_failed`
        permanently, and only ever here.
        """
        self.runtime.stop = lambda run, initiator, **options: StopReceipt(
            status=HEAD_ALIVE, run=run, reason="the head's process outlived the stop it was sent"
        )
        with self._failing_save():
            with self.assertRaises(RuntimeUnavailable):
                self.start(request_id="req-late-result")
        run_id = RunStore(self.data_dir).by_request("req-late-result").run_id

        # The head survived that stop, did its work and published its result.
        self.runtime.publish_result(run_id, {"status": "done", "summary": "it finished after all"})
        self.runtime.stop = FakeHeadRuntime.stop.__get__(self.runtime, FakeHeadRuntime)

        settled = self.layer().run_state(run_id)
        self.assertEqual(settled["state"]["value"], "finished")
        self.assertTrue(settled["state"]["ended"])
        self.assertEqual(
            settled["state"]["result"]["value"], {"status": "done", "summary": "it finished after all"}
        )
        # And the run's one terminal event says so too, on the card's own history.
        page = self.reads().task_events("secretary-run-1", None, limit=50)
        finished = [item for item in page["items"] if item["kind"] == run_events.FINISHED]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["data"]["state"], "finished")
        self.assertEqual(
            finished[0]["data"]["result"], {"status": "done", "summary": "it finished after all"}
        )

    def test_a_confirmed_ending_nothing_can_classify_settles_without_inventing_a_failure(self) -> None:
        """The reviewer's scenario of secretary-1562, and the reason the two facts are two.

        A save fails after a successful start, one stop cannot be confirmed, and the run is fenced
        as `unresolved`. Later the head is confirmed gone -- but its journal cannot be read and it
        published no result, so *how* it ended is not establishable by anything. The run is over
        and must settle: leaving it open would fence the card forever. What it settles as is
        `source_unavailable` with the reason, because that is what the evidence supports.

        `process_failed` is the answer the old contract was forced into, and it appears nowhere
        here -- not in the returned document, not in the record, and not in the event on the card's
        history, which is the copy no later read could correct.
        """
        self.runtime.stop = lambda run, initiator, **options: StopReceipt(
            status=HEAD_ALIVE, run=run, reason="the head's process outlived the stop it was sent"
        )
        with self._failing_save():
            with self.assertRaises(RuntimeUnavailable):
                self.start(request_id="req-dark")
        run = RunStore(self.data_dir).by_request("req-dark")
        self.assertEqual(run.phase, UNRESOLVED)

        # The journal becomes unreadable -- a directory in its place is an `OSError` on the read,
        # which is what "the source could not say" is, as opposed to a swept run directory saying
        # nothing. The head publishes no result, and the next stop is confirmed.
        journal = Path(run.journal_path)
        journal.unlink()
        journal.mkdir()
        self.assertFalse(Path(run.result_path).exists())

        def confirmed_stop(head, initiator, **options):
            self.runtime.stops.append((head.run_id, initiator.reason))
            record = json.loads(Path(run.pid_file).read_text(encoding="utf-8"))
            record["pid"] = _dead_pid()
            Path(run.pid_file).write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            return StopReceipt(status=HEAD_OK, run=head)

        self.runtime.stop = confirmed_stop
        document = self.layer().run_state(run.run_id)
        state = document["state"]

        self.assertTrue(state["ended"], "a head confirmed gone is a run that is over")
        self.assertEqual(state["value"], "source_unavailable")
        self.assertIn("could not be read", state["reason"])
        self.assertIn("how it ended could not be established", state["reason"])

        settled = RunStore(self.data_dir).get(run.run_id)
        self.assertTrue(settled.ended)
        self.assertEqual(settled.settled_state, "source_unavailable")
        self.assertEqual(settled.settled_reason, state["reason"])

        # The one terminal event says the same thing the document does, and the card's history
        # never carries the accusation.
        page = self.reads().task_events("secretary-run-1", None, limit=50)
        finished = [item for item in page["items"] if item["kind"] == run_events.FINISHED]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["data"]["state"], "source_unavailable")
        self.assertEqual(finished[0]["data"]["reason"], state["reason"])
        snapshot = self.reads().task_snapshot("secretary-run-1")
        published = [item for item in snapshot["events"]["items"] if item["kind"] == run_events.FINISHED]
        self.assertEqual(published[0]["data"]["state"], "source_unavailable")
        self.assertEqual(published[0]["data"]["reason"], state["reason"])
        for where, payload in (
            ("the state document", document),
            ("the run record", settled.to_json()),
            ("the card's history", page["items"]),
        ):
            self.assertNotIn("process_failed", json.dumps(payload), f"{where} invents a failure")

        # And the card is free, because the fence is lifted by the run being over and by nothing
        # about what it ended as.
        self.assertEqual(self.start(request_id="req-after")["state"]["value"], "running")

    def test_an_unresolved_run_settles_as_soon_as_the_ending_is_confirmed(self) -> None:
        """An unresolved run is a fence, not a dead end: the next read retries the same stop."""
        refusing = lambda run, initiator, **options: StopReceipt(  # noqa: E731
            status=HEAD_ALIVE, run=run, reason="the head's process outlived the stop it was sent"
        )
        self.runtime.stop = refusing
        with self._failing_save():
            with self.assertRaises(RuntimeUnavailable):
                self.start(request_id="req-recover")
        run_id = RunStore(self.data_dir).by_request("req-recover").run_id

        self.runtime.stop = FakeHeadRuntime.stop.__get__(self.runtime, FakeHeadRuntime)
        settled = self.layer().run_state(run_id)
        self.assertEqual(settled["state"]["value"], "process_failed")
        self.assertTrue(settled["state"]["ended"])
        self.assertEqual(self.start(request_id="req-after")["state"]["value"], "running")


class RealHeadFixture(ProductRuntimeFixture):
    """The pieces the two real-head suites below share: a real backend, and nothing left running.

    Everywhere else in this file the backend is a double, deliberately: a unit test must not raise
    real agents. These two suites are the exceptions, and they are exceptions for two different
    claims -- that a record alone can end a real process, and that a real head's result and exit
    status reach the product's own documents -- so the fixture is here and the claims are apart.
    """

    #: A real process on a real terminal, and deliberately not an agent. The same child the
    #: substrate's own suite proves process ownership with.
    CHILD_COMMAND = f"{sys.executable} -u {REPO_ROOT / 'tests' / 'fixtures' / 'local_pty_child.py'}"

    #: The child that publishes a result and the one that refuses: see its own docstring.
    PRODUCT_CHILD = f"{sys.executable} -u {REPO_ROOT / 'tests' / 'fixtures' / 'product_run_child.py'}"

    def real_backend(self):
        """The real supervised backend, over this fixture's own data directory, reaped afterwards."""
        from secretary.dispatcher_watchdog import head_process_status
        from triggered_agents.runtime.local_pty_head import LocalPtyHeadRuntime

        root = self.data_dir / "webproto" / "heads"
        self.addCleanup(self._reap, root)
        return root, LocalPtyHeadRuntime(root, head_process_status=head_process_status)

    @contextlib.contextmanager
    def commands(self, *rendered: str):
        """Run the block with each bring-up given the next of these commands, in order.

        Which binary a head is, is configuration -- this card's own words -- so it is the one thing
        substituted here. Everything the claims are about (the record, the ordering, the backend,
        the result file, the exit status, the stop and its confirmation) is real.
        """
        from triggered_agents.runtime.head.command import HeadCommand

        pending = list(rendered)

        def render(profile, **kwargs):
            return HeadCommand(command=pending.pop(0), adapter="claude")

        with (
            mock.patch.object(lifecycle_module, "CLAUDE_JSON", self.tmp / "claude.json"),
            mock.patch.object(lifecycle_module, "render_head_command", render),
        ):
            yield
        self.assertEqual(pending, [], "every rendered command was used")

    # -- keeping a real process out of the rest of the suite ------------------------------------

    def _await(self, predicate, message: str, *, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.assertTrue(predicate(), message)

    def _reap(self, root: Path) -> None:
        """Leave no process behind, whatever the test did or failed to do."""
        for run_dir in sorted(root.glob("*")) if root.is_dir() else []:
            for name, group in (("head.pid", True), ("supervisor.pid", False)):
                try:
                    raw = (run_dir / name).read_text(encoding="utf-8")
                    pid = int(json.loads(raw)["pid"] if name.endswith(".pid") and raw.strip().startswith("{") else raw)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                for number in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(pid, number) if group else os.kill(pid, number)
                    except OSError:
                        break


class RealHeadOwnershipTests(RealHeadFixture):
    """The outermost claim of this card, executed rather than derived.

    Everywhere else the backend is a double, deliberately: a unit test must not raise real agents.
    But "Secretary owns the process" has one edge that a double cannot stand in for, because the
    thing being claimed is that the *durable record alone* is enough to end a real process. So this
    one test raises a real head under the real `LocalPtyHeadRuntime`, through the product's own
    start path, then throws the handle away -- a brand new runtime object with no memory of that
    head, and a `HeadRun` rebuilt out of the **write-ahead** record, which has no socket on it --
    stops it by that, and confirms it is actually gone from the launch identity, the supervisor's
    journal and the process table.

    The one substitution is which binary the head is, which this card says is configuration:
    `render_head_command` is replaced with a child process the test can watch. Everything the claim
    is about -- the record, the write-ahead ordering, the backend, the stop and its confirmation --
    is real.
    """

    def test_a_real_head_is_stopped_by_a_record_recovered_from_the_store(self) -> None:
        from secretary.dispatcher_watchdog import (
            HEARTBEAT_DEAD,
            HEARTBEAT_LIVE_MATCH,
            head_process_status,
        )
        from triggered_agents.runtime.head.command import HeadCommand
        from triggered_agents.runtime.local_pty_head import LocalPtyHeadRuntime, head_run_journal

        root = self.data_dir / "webproto" / "heads"
        backend = LocalPtyHeadRuntime(root, head_process_status=head_process_status)
        self.addCleanup(self._reap, root)

        write_ahead: list[ProductRun] = []
        real_start = backend.start

        def start(*args, **kwargs):
            # The record exactly as it lies on disk at the first instant a process can exist.
            write_ahead.append(RunStore(self.data_dir).get(kwargs["run_id"]))
            return real_start(*args, **kwargs)

        backend.start = start
        with (
            mock.patch.object(lifecycle_module, "CLAUDE_JSON", self.tmp / "claude.json"),
            mock.patch.object(
                lifecycle_module,
                "render_head_command",
                lambda profile, **kwargs: HeadCommand(command=self.CHILD_COMMAND, adapter="claude"),
            ),
        ):
            document = self.layer(runtime_factory=lambda _root: backend).run_start(
                "secretary-run-1", request_id="req-real-head", profile=REVIEWER_PROFILE
            )

        run_id = document["run"]["run_id"]
        run_dir = root / run_id
        pid_file = run_dir / "head.pid"
        stored = RunStore(self.data_dir).get(run_id)
        self.assertEqual(str(pid_file), stored.pid_file)

        # A real process, on a real terminal, under a supervisor this product owns.
        self._await(pid_file.exists, "the head never published a launch identity")
        head_pid = int(json.loads(pid_file.read_text(encoding="utf-8"))["pid"])
        expected = run_state.expected_identity(stored)
        self._await(
            lambda: head_process_status(str(pid_file), expected=expected)["state"]
            == HEARTBEAT_LIVE_MATCH,
            "the head's launch identity never became a live match",
        )
        self.assertTrue(_alive(head_pid), "the head's process is running")

        # Now throw the handle away. The record used below is the write-ahead one -- written before
        # the spawn, carrying no socket -- and the runtime is a new object that never started
        # anything, which is what a later dispatcher process has.
        ahead = write_ahead[0]
        self.assertEqual(ahead.phase, "raising")
        recovered_head = HeadRun.from_json(ahead.head_run)
        self.assertFalse(recovered_head.handle, "the write-ahead record carries no socket handle")
        del backend
        recovered_runtime = LocalPtyHeadRuntime(root, head_process_status=head_process_status)

        receipt = recovered_runtime.stop(
            recovered_head,
            StopInitiator("secretary.webproto", "stopped by a record recovered from the store"),
        )

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(head_process_status(str(pid_file))["state"], HEARTBEAT_DEAD)
        self.assertIn(RUN_EXITED, [record.get("kind") for record in head_run_journal(run_dir)])
        self.assertFalse(_alive(head_pid), "the head's process is gone")



#: What the real children below publish: a worker's report and a reviewer's verdict, in the shape
#: the product tells a head to write.
REAL_RESULT = {"status": "done", "summary": "a line was added to README.md", "changed": ["README.md"]}
REAL_VERDICT = {"verdict": "green", "summary": "the work stands", "findings": []}


class RealBackendContractTests(RealHeadFixture):
    """`run_start` and `run_review` over the **real** head backend, not over a double.

    secretary-1564 left this open and said so: every failing-backend case in this file was
    hermetic, the Kanboard and the head runtime under those cases were fakes, and the premise that
    the real backend honours this layer's contract was therefore assumed rather than checked. The
    three tests below are that premise, executed. Each of them raises a real process on a real
    terminal under `LocalPtyHeadRuntime`, through this product's own operations, and reads the
    answer out of the documents those operations publish:

    * a head that **publishes a result** and is then ended by the product that owns it is
      `finished`, and the result it wrote is in the document;
    * a head that **exits non-zero on its own** is `process_failed` carrying that exit status, and
      is told apart from both a success and a run nothing is known about -- the failure case the
      published web has to draw as a refusal;
    * a **review is raised by a real worker's result**, in the worker's own workspace, and its
      verdict is read off the reviewer's own result file.

    Only the binary each head is gets substituted, which this card calls configuration. The record,
    the ordering, the backend, the workspace, the result file, the exit status, the stop and its
    confirmation are all real.
    """

    def test_a_real_head_that_published_a_result_finishes_and_is_ended_by_its_owner(self) -> None:
        root, backend = self.real_backend()
        layer = self.layer(runtime_factory=lambda _root: backend)
        with self.commands(self._publishing(REAL_RESULT)):
            started = layer.run_start(
                "secretary-run-1", request_id="req-real-result", profile=REVIEWER_PROFILE
            )
        run_id = started["run"]["run_id"]
        self.assertEqual(started["state"]["value"], "running")

        stored = RunStore(self.data_dir).get(run_id)
        self.assertEqual(Path(stored.result_path).parent, root / run_id)
        self._await(
            lambda: Path(stored.result_path).exists(), "the head never published its result file"
        )

        # The product owns the process, so the read that sees a published result ends the head.
        document = layer.run_state(run_id)
        self.assertEqual(document["state"]["value"], "finished")
        self.assertTrue(document["state"]["ended"])
        self.assertEqual(document["state"]["result"]["value"], REAL_RESULT)
        ended = RunStore(self.data_dir).get(run_id)
        self.assertFalse(_alive(ended.head_pid), "the head this product owned is gone")
        self.assertIn(RUN_EXITED, [record.get("kind") for record in _journal_of(root / run_id)])

    def test_a_real_head_that_exits_non_zero_is_a_failure_and_not_a_success(self) -> None:
        _root, backend = self.real_backend()
        layer = self.layer(runtime_factory=lambda _root: backend)
        with self.commands(f"{self.PRODUCT_CHILD} exit 7"):
            started = layer.run_start(
                "secretary-run-1", request_id="req-real-failure", profile=REVIEWER_PROFILE
            )
        run_id = started["run"]["run_id"]
        self._await(
            lambda: layer.run_state(run_id)["state"]["ended"], "the head never ended", timeout=30.0
        )

        document = layer.run_state(run_id)
        state = document["state"]
        # The three things a refusal has to be told apart from, and it is told apart from each.
        self.assertEqual(state["value"], "process_failed")
        self.assertTrue(state["ended"])
        self.assertEqual(state["exit"]["code"], 7)
        self.assertIn("7", state["reason"])
        self.assertFalse(state["result"]["present"])
        self.assertIsNone(state["result"]["verdict"])
        # And it is on the card's own history as an ending, once.
        finished = [
            event
            for event in self.reads().task_events("secretary-run-1", None)["items"]
            if event["kind"] == run_events.FINISHED
        ]
        self.assertEqual(len(finished), 1)
        # The journal's own two-valued field says the work did not get done; the state beside it
        # says what the process did. Neither stands in for the other.
        self.assertEqual(finished[0]["outcome"], "failure")
        self.assertEqual(finished[0]["data"]["state"], "process_failed")
        self.assertEqual(finished[0]["data"]["exit"]["code"], 7)

    def test_a_real_review_is_raised_over_a_real_worker_run_in_its_workspace(self) -> None:
        root, backend = self.real_backend()
        layer = self.layer(runtime_factory=lambda _root: backend)
        with self.commands(self._publishing(REAL_RESULT), self._publishing(REAL_VERDICT)):
            worker = layer.run_start(
                "secretary-run-1", request_id="req-real-worker", profile=REVIEWER_PROFILE
            )["run"]["run_id"]
            self._await(
                lambda: layer.run_state(worker)["state"]["ended"],
                "the worker never ended",
                timeout=30.0,
            )
            review = layer.run_review(
                request_id="req-real-review", profile=REVIEWER_PROFILE, worker_run_id=worker
            )

        self.assertEqual(review["kind"], "product_review")
        review_id = review["review"]["run"]["run_id"]
        self.assertEqual(review["review"]["run"]["parent_run_id"], worker)
        # The review reads the work, so it runs where the work is.
        self.assertEqual(
            review["review"]["run"]["workspace"], review["worker"]["run"]["workspace"]
        )
        prompt = (root / review_id / "REVIEW.md").read_text(encoding="utf-8")
        self.assertIn(worker, prompt)
        self.assertIn(review["worker"]["run"]["result_path"], prompt)

        self._await(
            lambda: Path(review["review"]["run"]["result_path"]).exists(),
            "the reviewer never published its verdict",
            timeout=30.0,
        )
        settled = layer.run_state(review_id)
        self.assertEqual(settled["state"]["value"], "finished")
        self.assertEqual(settled["state"]["result"]["verdict"], "green")

    def _publishing(self, document: dict[str, Any]) -> str:
        return f"{self.PRODUCT_CHILD} result {shlex.quote(json.dumps(document))}"


def _journal_of(run_dir: Path) -> tuple[dict[str, Any], ...]:
    from triggered_agents.runtime.local_pty_head import head_run_journal

    return head_run_journal(run_dir)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True



class ErrorContractTests(ProductRuntimeFixture):
    """The layer's failure vocabulary, kept in one place and checked for every operation.

    `secretary.webproto` promises that what leaves an operation is a typed protocol code, because
    that promise is what lets each transport hold one code table and one containment branch. Before
    :mod:`secretary.webproto.boundary` the promise was kept a call site at a time, and `run_list`
    was the site that forgot: an unreadable run record escaped as `RunStoreError`, past a transport
    that catches only `ReadError`, and took a whole card page down.

    So this is written the way `OrcaAbsenceTests` is written -- as something a new violation breaks
    by itself. The roster below is checked against `boundary.operations()`, so an operation added to
    either layer fails this suite until it is listed and shown to hold the contract; and the
    contract is then exercised by breaking the durable sources under *every* operation at once,
    rather than by naming the ones somebody remembered.
    """

    REF = "secretary-run-1"

    READ_CALLS = {
        "report": lambda layer: layer.report(),
        "data_dir": lambda layer: layer.data_dir(),
        "system_snapshot": lambda layer: layer.system_snapshot(),
        "task_snapshot": lambda layer: layer.task_snapshot("secretary-run-1"),
        "task_events": lambda layer: layer.task_events("secretary-run-1", None, limit=10),
    }

    OPERATION_CALLS = {
        "report": lambda layer: layer.report(),
        "data_dir": lambda layer: layer.data_dir(),
        "store": lambda layer: layer.store(),
        "run_start": lambda layer: layer.run_start(
            "secretary-run-1", request_id="contract-start", profile=WORKER_PROFILE
        ),
        "run_review": lambda layer: layer.run_review(
            request_id="contract-review", profile=REVIEWER_PROFILE, ref="secretary-run-1"
        ),
        "run_state": lambda layer: layer.run_state("pr-contract"),
        "run_list": lambda layer: layer.run_list("secretary-run-1"),
    }

    # -- what the roster is worth -------------------------------------------------------------

    def test_the_roster_names_every_public_operation_of_both_layers(self) -> None:
        """Adding an operation without covering it here is itself the failure."""
        self.assertEqual(set(operations(ReadLayer)), set(self.READ_CALLS))
        self.assertEqual(set(operations(OperationLayer)), set(self.OPERATION_CALLS))

    def test_every_public_operation_leaves_through_the_boundary(self) -> None:
        for layer in (ReadLayer, OperationLayer):
            for name in operations(layer):
                with self.subTest(layer=layer.__name__, operation=name):
                    self.assertTrue(getattr(getattr(layer, name), GUARDED, False))

    # -- the contract itself ------------------------------------------------------------------

    def _each_operation(self) -> list[tuple[str, Any]]:
        reads, ops = self.reads(), self.layer()
        return [(f"reads.{name}", lambda call=call: call(reads)) for name, call in self.READ_CALLS.items()] + [
            (f"ops.{name}", lambda call=call: call(ops)) for name, call in self.OPERATION_CALLS.items()
        ]

    def _assert_typed(self, label: str) -> None:
        for name, call in self._each_operation():
            with self.subTest(source=label, operation=name):
                try:
                    call()
                except ReadError:
                    pass  # the contract: a protocol code, whatever it is
                except Exception as exc:  # noqa: BLE001 -- naming the violation is the point
                    self.fail(
                        f"{name} let {type(exc).__module__}.{type(exc).__name__} out of the layer "
                        f"with {label} broken: {exc}"
                    )

    def test_an_unreadable_run_record_is_a_protocol_code_from_every_operation(self) -> None:
        runs = self.data_dir / "webproto" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "pr-broken.json").write_text("{not json", encoding="utf-8")
        self._assert_typed("an unreadable run record")

    def test_a_run_store_that_is_not_a_directory_is_a_protocol_code_from_every_operation(self) -> None:
        """The store's own `OSError`, which is a different vocabulary from a parse failure."""
        webproto = self.data_dir / "webproto"
        webproto.mkdir(parents=True, exist_ok=True)
        (webproto / "runs").write_text("not a directory", encoding="utf-8")
        self._assert_typed("a run store that is a file")

    def test_an_unreadable_event_journal_is_a_protocol_code_from_every_operation(self) -> None:
        (self.data_dir / "board" / "events.ndjson").mkdir(parents=True, exist_ok=True)
        self._assert_typed("an event journal that is a directory")

    def test_a_broken_run_store_is_backend_unavailable_and_not_some_other_code(self) -> None:
        runs = self.data_dir / "webproto" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "pr-broken.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeUnavailable) as refused:
            self.layer().run_list(self.REF)
        self.assertEqual(refused.exception.code, "backend_unavailable")
        self.assertIn("pr-broken.json", refused.exception.message)

    # -- and what the boundary deliberately does not do ----------------------------------------

    def test_a_defect_of_the_layer_is_not_dressed_as_an_unavailable_backend(self) -> None:
        """Only the durable sources' vocabularies are translated; a bug travels as a bug."""

        class Faulty(ProtocolBoundary):
            def operation(self):
                raise TypeError("this is a defect of the layer, not an unreadable file")

        with self.assertRaises(TypeError):
            Faulty().operation()
        self.assertNotIn(TypeError, IMPLEMENTATION_FAILURES)

    def test_the_run_store_error_is_never_the_kind_of_thing_a_caller_sees(self) -> None:
        self.assertFalse(issubclass(RunStoreError, ReadError))
        self.assertIn(RunStoreError, IMPLEMENTATION_FAILURES)


class OrcaAbsenceTests(ProductRuntimeFixture):
    """Criterion 2, as a check rather than a promise.

    What this test asserts, in the words of the card:

    * **no Orca import** on any module of the layer -- neither the CLI client (`orca_rpc`), nor the
      pane host, nor the legacy backend, nor the control plane's Orca-facing terminal readers;
    * **no Orca call** on the start path or the result-reading path: with `orca_rpc.call`, every
      verb of `pane_host`'s session host, and `secretary.dispatch.head_status` replaced by
      detonators, `run_start`, `run_state` and `run_review` all complete;
    * **no `orca` process**: every child process the two paths spawn is captured, and none of them
      is an `orca` executable;
    * **no session manager**: the backend is built by name through the product's one
      name-to-backend mapping, with a session factory that raises if anything ever calls it;
    * **the supervised backend and no other**: the name handed to that mapping is `local-pty`.
    """

    FORBIDDEN_IMPORTS = frozenset(
        """
        triggered_agents.runtime.orca_rpc triggered_agents.runtime.pane_host
        triggered_agents.runtime.orca_legacy_head triggered_agents.runtime.tui_delivery
        secretary.dispatch.head_status secretary.dispatch.host secretary.dispatcher_review
        orca
        """.split()
    )

    def _imports(self) -> list[tuple[str, str]]:
        import ast

        found: list[tuple[str, str]] = []
        for path in sorted((REPO_ROOT / "src" / "secretary" / "webproto").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.extend((path.name, alias.name) for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.append((path.name, node.module))
        return found

    def test_the_layer_imports_no_orca(self) -> None:
        offenders = [
            f"{module}: {imported}"
            for module, imported in self._imports()
            if imported in self.FORBIDDEN_IMPORTS or imported.split(".")[0] == "orca"
        ]
        self.assertEqual(offenders, [])

    def test_the_start_and_result_paths_call_no_orca(self) -> None:
        from triggered_agents.runtime import orca_rpc, pane_host

        def detonate(*_args, **_kwargs):
            raise AssertionError("the product runtime reached Orca")

        spawned: list[list[str]] = []
        real_run = subprocess.run

        def watched_run(argv, *args, **kwargs):
            spawned.append(list(argv))
            return real_run(argv, *args, **kwargs)

        with (
            mock.patch.object(orca_rpc, "call", detonate),
            mock.patch.object(pane_host, "session_host", detonate),
            mock.patch.object(pane_host.SessionHost, "open_pane", detonate, create=True),
            mock.patch.object(pane_host.SessionHost, "list_panes", detonate, create=True),
            mock.patch("subprocess.run", watched_run),
            mock.patch.dict("sys.modules", {"secretary.dispatch.head_status": None}),
        ):
            document = self.start(request_id="req-orca")
            run_id = document["run"]["run_id"]
            self.runtime.publish_result(run_id, {"status": "done"})
            self.layer().run_state(run_id)
            self.layer().run_review(request_id="rev-orca", profile=REVIEWER_PROFILE, worker_run_id=run_id)
        self.assertTrue(spawned, "the start path provisions a workspace with git")
        for argv in spawned:
            self.assertNotIn("orca", Path(argv[0]).name)

    def test_the_backend_is_named_and_holds_no_session_manager(self) -> None:
        seen: list[dict[str, Any]] = []

        def watched(name, *, session, local_pty_root, head_process_status):
            seen.append(
                {"name": name, "session": session, "root": local_pty_root(), "identity": head_process_status}
            )
            return self.runtime

        with mock.patch.object(ops_module, "build_head_runtime", watched):
            # No `runtime_factory`: this is the backend the product chooses for itself, and the
            # arguments it chooses it with. Only the backend object is a double, so that a unit
            # test never raises a real head.
            layer = self.layer(runtime_factory=None)
            layer.run_start("secretary-run-1", request_id="req-named", profile=WORKER_PROFILE)
        self.assertTrue(seen, "the start path builds a backend by name")
        for call in seen:
            self.assertEqual(call["name"], "local-pty")
            self.assertEqual(call["root"], self.data_dir / "webproto" / "heads")
            # The session manager it is handed is one that refuses to exist.
            with self.assertRaises(RuntimeUnavailable):
                call["session"]()
            # And liveness is the product's one launch-identity reader, not a scheme of its own.
            from secretary.dispatcher_watchdog import head_process_status

            self.assertIs(call["identity"], head_process_status)


class WorkspaceOwnershipTests(ProductRuntimeFixture):
    def test_a_workspace_is_a_detached_worktree_this_product_cuts_and_takes_back(self) -> None:
        target = self.data_dir / "webproto" / "workspaces" / "pr-manual"
        head = workspaces.provision(self.repo, target, base="main")
        self.assertTrue((target / "README.md").exists())
        self.assertEqual(len(head), 40)
        branch = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=target,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(branch.returncode, 0, "a product workspace creates no branch")
        self.assertTrue(workspaces.release(self.repo, target))
        self.assertFalse(target.exists())

    def test_provisioning_the_same_workspace_twice_reuses_it(self) -> None:
        target = self.data_dir / "webproto" / "workspaces" / "pr-manual"
        first = workspaces.provision(self.repo, target, base="main")
        again = workspaces.provision(self.repo, target, base="main")
        self.assertEqual(first, again)


class SchemaTests(ProductRuntimeFixture):
    def test_every_document_validates_against_the_published_schema(self) -> None:
        document = self.start()
        run_id = document["run"]["run_id"]
        self.runtime.publish_result(run_id, {"status": "done"})
        settled = self.layer().run_state(run_id)
        review = self.layer().run_review(request_id="rev-1", profile=REVIEWER_PROFILE, worker_run_id=run_id)
        for candidate in (document, settled, review):
            with self.subTest(kind=candidate["kind"]):
                self.assertEqual(validate(candidate, "web-run", candidate["kind"]), [])
                json.dumps(candidate)


class WebRunCommandTests(ProductRuntimeFixture):
    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([*argv, "--instance", str(self.instance)])
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def _patched(self):
        layer = self.layer()
        with mock.patch(
            "secretary.webproto.commands.OperationLayer",
            lambda instance, **kwargs: layer,
        ):
            yield

    def test_the_group_starts_reads_and_lists_a_run(self) -> None:
        with self._patched():
            code, out, _ = self._run(
                "web-run",
                "start",
                "--ref",
                "secretary-run-1",
                "--request-id",
                "cli-1",
                "--profile",
                WORKER_PROFILE,
                "--json",
            )
            self.assertEqual(code, 0)
            run_id = json.loads(out)["run"]["run_id"]

            code, out, _ = self._run("web-run", "state", "--run-id", run_id)
            self.assertEqual(code, 0)
            self.assertIn("state: running", out)
            self.assertIn("workspace:", out)

            code, out, _ = self._run("web-run", "list", "--ref", "secretary-run-1")
            self.assertEqual(code, 0)
            self.assertIn(run_id, out)
            # The listing carries each run's state, not just its record: an open run that reads
            # `unknown` must not print the same line as one that is running.
            self.assertIn("running (open)", out)

    def test_an_owner_conflict_exits_on_its_own_status(self) -> None:
        self.reserve_sprint("secretary", "sprint:1427")
        with self._patched():
            code, _, err = self._run(
                "web-run",
                "start",
                "--ref",
                "secretary-run-1",
                "--request-id",
                "cli-2",
                "--profile",
                WORKER_PROFILE,
                "--json",
            )
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(err)["error"]["code"], "owner_conflict")

    def test_an_unknown_run_exits_on_not_found(self) -> None:
        with self._patched():
            code, _, err = self._run("web-run", "state", "--run-id", "pr-nothing", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"]["code"], "not_found")


class StoreTests(ProductRuntimeFixture):
    def test_a_request_id_never_becomes_a_path(self) -> None:
        store = RunStore(self.data_dir)
        store.claim(
            "../../escape",
            lambda run_id: ProductRun(
                run_id=run_id,
                request_id="../../escape",
                ref="r",
                project="p",
                role="worker",
                profile=WORKER_PROFILE,
            ),
        )
        names = [path.name for path in (self.data_dir / "webproto" / "runs" / "requests").iterdir()]
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^[0-9a-f]{64}\.json$")

    def test_an_ending_is_recorded_once(self) -> None:
        store = RunStore(self.data_dir)
        run, _ = store.claim(
            "req",
            lambda run_id: ProductRun(
                run_id=run_id,
                request_id="req",
                ref="r",
                project="p",
                role="worker",
                profile=WORKER_PROFILE,
            ),
        )
        first, owed = store.settle(run.run_id, "finished", "done", now=1.0)
        second, owed_again = store.settle(run.run_id, "process_failed", "no", now=2.0)
        self.assertTrue(owed)
        self.assertFalse(owed_again)
        self.assertEqual(second.settled_state, first.settled_state)


if __name__ == "__main__":
    unittest.main()
