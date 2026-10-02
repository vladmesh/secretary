"""The e2e stage's test fixture: the dispatcher over a real board, with GitHub faked (secretary-1795).

`E2eGitHubHost` is the fake host with a GitHub behind `run_capture`, and `E2eStageFixture` drives a code
card and its wait card tick by tick. Shared by `tests/test_e2e_stage.py` and `tests/test_e2e_budget.py`.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from tests.fakes.dispatcher import FakeHost
from ummanu._fsutil import file_lock
from ummanu.board.e2e_record import e2e_state
from ummanu.dispatch import e2e_stage
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.gate_receipt import mint_gate_receipt
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.state import new_attempt_id, now_rfc3339

REPO = "vladmesh/ummanu"
SHA = ("c0ffee12" * 5)[:40]
SECOND_SHA = ("feedbeef" * 5)[:40]
FIRST_RUN = 9001
BRANCH = f"pipeline/{CARD_REF}"
RUN_URL = f"https://github.com/{REPO}/actions/runs/{FIRST_RUN}"
E2E = {"workflow": "e2e.yml", "inputs": {"suite": "mega"}, "candidate_input": "sha", "deadline": "2h"}
FAILED_LOG = (
    "mega\tSet up job\tRunner ready\n"
    "mega\tRun e2e suite\tstarting stands\n"
    "mega\tRun e2e suite\t##[error]stand 2 never answered its health check\n"
)


class SimulatedCrash(Exception):
    """The dispatcher process died here."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class E2eGitHubHost(FakeHost):
    """The fake host with a GitHub behind `run_capture`, and a github gate that mints real receipts.

    `dispatch_answer`: `ok` (the run's details, as `return_run_details` answers); `bare` (accepted with
    no details, as the API answered before it named runs); `crash` (GitHub took it, then the dispatcher
    died before reading the answer); `hidden` (accepted with no details, and the run never shows up);
    or `("http", <gh stderr>)`. `run_answer`: a run's status and conclusion, or `("http", <gh stderr>)`.
    `run_listings` counts the reads of the workflow's run list, the recovery lookup.
    """

    def __init__(self, root: Path, catalog: Any) -> None:
        super().__init__(root, catalog)
        self.commit = SHA
        self.dispatches: list[dict[str, Any]] = []
        self.runs: dict[int, dict[str, Any]] = {}
        self.dispatch_answer: Any = "ok"
        self.run_answer: Any = ("in_progress", None)
        self.run_head_sha = ""
        self.jobs: list[dict[str, Any]] = []
        self.failed_log = FAILED_LOG
        self.gh_calls: list[list[str]] = []
        self.run_listings = 0
        # The title a dispatched run gets; None: `e2e <the dispatch id input>`.
        self.run_title: str | None = None
        # The HEAD each coming gate read moves the checkout to first, as `_recover_base` does when it
        # merges a newer base; and the SHA pairs `reconcile_reviewed_base_move` accepts as base-only.
        self.move_on_gate: list[str] = []
        self.base_only: set[tuple[str, str]] = set()

    # the github gate, green with an exact-SHA receipt unless a test scripts it
    def gate_check(self, task: dict, record) -> GateResult:
        if self.gate_results or self.gate_error is not None:
            return super().gate_check(task, record)
        self.calls.append("gate_check")
        self.gate_calls.append(task["ref"])
        if self.move_on_gate:
            self.commit = self.move_on_gate.pop(0)
        receipt = mint_gate_receipt(
            validated_sha=self.commit,
            base_sha="b" * 40,
            gate_mode="github",
            required_checks=[{"name": "test", "conclusion": "SUCCESS", "url": ""}],
            check_set_identity='{"required":["test"]}',
        )
        return GateResult("green", f"CI green @ {self.commit[:12]}", attestation=receipt)

    def reconcile_reviewed_base_move(
        self, task: dict, record, reviewed_commit: str, current_commit: str
    ) -> dict[str, str | int] | None:
        """The real rule's answer, scripted: only a declared pair is a base-only move."""
        self.calls.append("reconcile_reviewed_base_move")
        if (reviewed_commit, current_commit) not in self.base_only:
            return None
        return {
            "reviewed_sha": reviewed_commit,
            "head_sha": current_commit,
            "base_sha": "b" * 40,
            "reviewed_paths": 2,
        }

    # GitHub
    def run_capture(self, args: list[str], label: str, *, cwd: Any = None) -> subprocess.CompletedProcess:
        args = list(args)
        self.gh_calls.append(args)
        if args[:3] == ["gh", "repo", "view"]:
            return self._ok(args, REPO)
        if args[:4] == ["gh", "api", "--method", "POST"]:
            return self._dispatch(args)
        if args[:2] == ["gh", "api"]:
            path = args[2]
            if re.fullmatch(rf"repos/{REPO}/actions/workflows/e2e\.yml/runs\?.*", path):
                assert "event=workflow_dispatch" in path and f"branch={BRANCH}" in path, path
                assert f"head_sha={self.commit}" in path, path
                self.run_listings += 1
                return self._ok(args, json.dumps(list(self.runs.values())))
            if match := re.fullmatch(rf"repos/{REPO}/actions/runs/(\d+)", path):
                return self._run(args, int(match.group(1)))
            if re.fullmatch(rf"repos/{REPO}/actions/runs/\d+/jobs\?per_page=100", path):
                return self._ok(args, json.dumps(self.jobs))
        if args[:3] == ["gh", "run", "view"] and "--log-failed" in args:
            return self._ok(args, self.failed_log)
        raise AssertionError(f"unexpected GitHub call {args}")

    @staticmethod
    def _ok(args: list[str], stdout: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def _dispatch(self, args: list[str]) -> subprocess.CompletedProcess:
        fields = dict(args[index + 1].split("=", 1) for index, arg in enumerate(args) if arg == "-f")
        typed = dict(args[index + 1].split("=", 1) for index, arg in enumerate(args) if arg == "-F")
        inputs = {
            key[len("inputs[") : -1]: value for key, value in fields.items() if key.startswith("inputs[")
        }
        self.dispatches.append({"path": args[4], "ref": fields.get("ref"), "inputs": inputs, "typed": typed})
        if isinstance(self.dispatch_answer, tuple):
            return subprocess.CompletedProcess(args, 1, "", self.dispatch_answer[1])
        if self.dispatch_answer == "hidden":
            return self._ok(args, "")
        run_id = self.add_run(
            self.run_title if self.run_title is not None else f"e2e {inputs.get('sid', '')}".strip()
        )
        if self.dispatch_answer == "crash":
            raise SimulatedCrash("the dispatcher died after GitHub took the dispatch")
        if self.dispatch_answer == "bare":
            return self._ok(args, "")
        url = f"https://github.com/{REPO}/actions/runs/{run_id}"
        details = {
            "workflow_run_id": run_id,
            "run_url": f"https://api.github.com/{url[19:]}",
            "html_url": url,
        }
        return self._ok(args, json.dumps(details))

    def add_run(self, title: str = "e2e") -> int:
        """A `workflow_dispatch` run of the workflow on the card's branch, created now."""
        run_id = FIRST_RUN + len(self.runs)
        self.runs[run_id] = {
            "id": run_id,
            "event": "workflow_dispatch",
            "head_branch": BRANCH,
            "display_title": title,
            "name": "e2e",
            "head_sha": self.run_head_sha or self.commit,
            "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
            "status": "queued",
            "created_at": _now(),
        }
        return run_id

    def _run(self, args: list[str], run_id: int) -> subprocess.CompletedProcess:
        if isinstance(self.run_answer, tuple) and self.run_answer[0] == "http":
            return subprocess.CompletedProcess(args, 1, "", self.run_answer[1])
        status, conclusion = self.run_answer
        run = {
            "head_sha": self.runs.get(run_id, {}).get("head_sha", self.commit),
            "status": status,
            "conclusion": conclusion,
            "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
            "created_at": _now(),
            "run_started_at": _now(),
            "updated_at": _now(),
        }
        return self._ok(args, json.dumps(run))


class E2eStageFixture(DispatcherRuntimeFixture):
    """The dispatcher over a real board with GitHub faked, and the steps that drive the e2e stage.

    A plain mixin, so `tests/test_e2e_budget.py` drives the same stage without re-running these tests.
    """

    def setUp(self) -> None:
        super().setUp()
        self.host = E2eGitHubHost(self.data_dir / "workspaces", self.catalog)
        self.host.audit = self.writer.audit
        self.runtime = self._runtime()

    def _runtime(self) -> DispatcherRuntime:
        """A dispatcher process: a new one shares nothing with the last but the board and the state."""
        return DispatcherRuntime(
            self.reader,
            self.writer,
            self.writer.audit,
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="ummanu-pilot",
            sprints=self.sprints,
        )

    # --- arrangement ------------------------------------------------------------------------------

    def arrange(self, *, review: str = "required", observed: bool = True, e2e: dict | None = None) -> None:
        self.catalog._adapter = {
            "validation": {"ci": "github", "required_checks": ["test"], "e2e": dict(e2e or E2E)}
        }
        self.board.save_metadata(12, task_type="code")
        self.board.save_metadata(12, review=review)
        self.start_dispatcher()
        if not observed:
            self.board.save_metadata(12, {"sprint_ref": ""})
            self.sprints.rows.clear()
            self.board.clear_sprints()

    def to_green_review(self) -> None:
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="green",
            body="green",
            request_id="pilot-review-green",
        )

    def to_waiting(self, **arrangement: Any) -> dict[str, Any]:
        """A green review, then the stage's first tick: the dispatch, the run and its wait card."""
        self.arrange(**arrangement)
        self.to_green_review()
        waiting = self.tick()
        self.assertEqual(waiting["action"], "e2e-waiting", waiting)
        return waiting

    def card(self) -> dict[str, Any]:
        return self.reader.show(CARD_REF)

    def wait_ref(self) -> str:
        [run] = e2e_state(self.card()).runs
        return run.wait_ref

    def wait_cards(self) -> list[dict[str, Any]]:
        return [card for card in self.reader.list() if card.get("type") == "wait"]

    def tick_wait(self, runtime: DispatcherRuntime | None = None) -> dict[str, Any]:
        """One tick of the wait card, as the production tick advances it beside the code card."""
        runtime = runtime or self.runtime
        with file_lock(runtime.production_state.tick_lock):
            payload = runtime.production_state.load()
            records = runtime.production_state.records(payload)
            outcome = runtime._tick_task(
                self.reader.show(self.wait_ref()), records, payload, new_attempt_id()
            )
            runtime.production_state.put_records(payload, records)
            payload["last_tick_at"] = now_rfc3339()
            runtime.production_state.save(payload)
        return outcome

    def conclude(self, conclusion: str) -> dict[str, Any]:
        """The run concludes; the wait card sees it, freezes it and delivers it to the code card."""
        self.host.run_answer = ("completed", conclusion)
        ended = self.tick_wait()
        self.assertIn(ended["action"], {"wait-target-reached", "wait-ended"}, ended)
        return ended

    @staticmethod
    def settled(minutes: float = 6) -> Any:
        """The stage's clock past the recovery settle time (margin 2 min + settle 3 min) of a fresh intent."""
        return mock.patch.object(
            e2e_stage, "utcnow", return_value=datetime.now(UTC) + timedelta(minutes=minutes)
        )

    def comments(self, needle: str) -> list[str]:
        return [comment["body"] for comment in self.card()["comments"] if needle in comment["body"]]

    def blocked_transition(self) -> dict[str, Any]:
        """The committed move of the code card into Blocked, as its audit record holds it."""
        for event in reversed(self.writer.audit.events(CARD_REF)):
            transition = event.get("transition") if isinstance(event.get("transition"), dict) else {}
            if transition.get("target") == "blocked":
                return event
        self.fail("the card has no committed move into Blocked")

    def assertBlockedAsInfrastructure(
        self, blocked: dict[str, Any], *needles: str, taxonomy: str = "infrastructure"
    ) -> None:
        self.assertEqual(blocked["status"], "blocked", blocked)
        card = self.card()
        self.assertEqual(card["state"], "blocked")
        event = self.blocked_transition()
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        self.assertEqual(data["terminal_taxonomy"]["blocked_reason"], taxonomy)
        self.assertNotIn("gate-red", str(event.get("request_id")), "no worker round is charged")
        reason = str(event.get("reason") or "") or "\n".join(c["body"] for c in card["comments"][-1:])
        for needle in needles:
            self.assertIn(needle, reason)
