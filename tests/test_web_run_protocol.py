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
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from secretary.cli import main
from secretary.config import validate
from secretary.webproto import admission as admission_module
from secretary.webproto import ops as ops_module
from secretary.webproto import run_events, run_state, workspaces
from secretary.webproto.errors import (
    OwnerConflict,
    RunNotFound,
    RuntimeUnavailable,
    ValidationRefused,
)
from secretary.webproto.ops import OperationLayer
from secretary.webproto.reads import ReadLayer
from secretary.webproto.runs import ProductRun, RunStore
from tests.fakes.dispatcher import FakeKanboard
from triggered_agents.agents.pipeline.heads import Registry
from triggered_agents.runtime.head.identity import publish_heartbeat
from triggered_agents.runtime.head.local_pty import RUN_EXITED, RUN_STARTED
from triggered_agents.runtime.head.run import HeadRun
from triggered_agents.runtime.head.runtime import (
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
        with self.assertRaises(OSError):
            self.start(request_id="req-broken")
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
        self.assertFalse(state["terminal"])

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
        self.assertFalse(state["terminal"])

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

    def test_no_second_history_is_written_anywhere(self) -> None:
        run_id = self.start()["run"]["run_id"]
        self.runtime.publish_result(run_id, {"status": "done"})
        self.layer().run_state(run_id)
        journals = sorted(
            path.relative_to(self.data_dir).as_posix() for path in self.data_dir.rglob("*.ndjson")
        )
        self.assertEqual(journals, ["board/events.ndjson"])


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
