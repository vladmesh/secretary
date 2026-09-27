"""The e2e stage: an adapter-declared workflow run on a code card's candidate, waited for by a wait card.

secretary-1795. The whole dispatcher runs here — `DispatcherRuntime._tick_task` over a real card store
(the disposable PostgreSQL board), with the fake host of the other dispatcher suites — and GitHub is
a fake behind the one host call every gate question goes through (`run_capture`): the repository name,
the `workflow_dispatch` POST, the workflow's run listing, the run the wait card reads, its jobs and its
`--log-failed`. Each test drives the code card and its wait card tick by tick, the way the production
tick interleaves them.

The declaration's own reading and the adapter schema are unit tests in `tests/test_e2e_declaration.py`.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from secretary._fsutil import file_lock
from secretary.board.e2e_record import e2e_state
from secretary.board.wait_card import wait_spec, wait_state
from secretary.dispatch import e2e_stage
from secretary.dispatch.gate import GateResult
from secretary.dispatch.gate_receipt import mint_gate_receipt
from secretary.dispatch.runtime import DispatcherRuntime
from secretary.dispatch.state import new_attempt_id, now_rfc3339
from secretary.tasks import TaskWriter
from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from tests.fakes.dispatcher import FakeHost
from tests.integration_setup import require_disposable_board_fixture
from tests.sql_backend_fixtures import PostgresBoard

REPO = "vladmesh/secretary"
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


def setUpModule() -> None:
    require_disposable_board_fixture(PostgresBoard.shared)


class SimulatedCrash(Exception):
    """The dispatcher process died here."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class E2eGitHubHost(FakeHost):
    """The fake host with a GitHub behind `run_capture`, and a github gate that mints real receipts.

    `dispatch_answer`: `ok`; `crash` (GitHub took it, then the dispatcher died before reading the
    answer); `hidden` (GitHub took it, but the run's title never carries the dispatch id); or
    `("http", <gh stderr>)`. `run_answer`: a run's status and conclusion, or `("http", <gh stderr>)`.
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

    # the github gate, green with an exact-SHA receipt unless a test scripts it
    def gate_check(self, task: dict, record) -> GateResult:
        if self.gate_results or self.gate_error is not None:
            return super().gate_check(task, record)
        self.calls.append("gate_check")
        self.gate_calls.append(task["ref"])
        receipt = mint_gate_receipt(
            validated_sha=self.commit,
            base_sha="b" * 40,
            gate_mode="github",
            required_checks=[{"name": "test", "conclusion": "SUCCESS", "url": ""}],
            check_set_identity='{"required":["test"]}',
        )
        return GateResult("green", f"CI green @ {self.commit[:12]}", attestation=receipt)

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
        inputs = {
            key[len("inputs[") : -1]: value for key, value in fields.items() if key.startswith("inputs[")
        }
        self.dispatches.append({"path": args[4], "ref": fields.get("ref"), "inputs": inputs})
        if isinstance(self.dispatch_answer, tuple):
            return subprocess.CompletedProcess(args, 1, "", self.dispatch_answer[1])
        run_id = FIRST_RUN + len(self.runs)
        title = "e2e" if self.dispatch_answer == "hidden" else f"e2e {inputs['secretary_dispatch_id']}"
        self.runs[run_id] = {
            "id": run_id,
            "display_title": title,
            "name": "e2e",
            "head_sha": self.run_head_sha or self.commit,
            "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
            "status": "queued",
            "created_at": _now(),
        }
        if self.dispatch_answer == "crash":
            raise SimulatedCrash("the dispatcher died after GitHub took the dispatch")
        return self._ok(args, "")

    def _run(self, args: list[str], run_id: int) -> subprocess.CompletedProcess:
        if isinstance(self.run_answer, tuple) and self.run_answer[0] == "http":
            return subprocess.CompletedProcess(args, 1, "", self.run_answer[1])
        status, conclusion = self.run_answer
        run = {
            "status": status,
            "conclusion": conclusion,
            "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
            "created_at": _now(),
            "run_started_at": _now(),
            "updated_at": _now(),
        }
        return self._ok(args, json.dumps(run))


class E2eStageTests(DispatcherRuntimeFixture, unittest.TestCase):
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
            owner="secretary-pilot",
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

    # --- placement ---------------------------------------------------------------------------------

    def test_green_review_dispatches_once_waits_on_a_wait_card_and_parks_with_the_result(self) -> None:
        waiting = self.to_waiting()

        [dispatch] = self.host.dispatches
        self.assertEqual(dispatch["path"], f"repos/{REPO}/actions/workflows/e2e.yml/dispatches")
        self.assertEqual(dispatch["ref"], BRANCH)
        [run] = e2e_state(self.card()).runs
        self.assertEqual(
            dispatch["inputs"], {"suite": "mega", "sha": SHA, "secretary_dispatch_id": run.dispatch_id}
        )
        self.assertEqual((run.sha, run.run_id, run.run_url), (SHA, FIRST_RUN, RUN_URL))
        self.assertEqual((waiting["run"], waiting["wait_card"]), (RUN_URL, run.wait_ref))
        # The wait card is on the board, in the card's sprint, waiting for that run and returning here.
        wait = self.reader.show(run.wait_ref)
        self.assertEqual((wait["type"], wait["state"], wait["sprint"]), ("wait", "ready", "sprint:1031"))
        spec = wait_spec(wait)
        assert spec is not None
        self.assertEqual((spec.target.repo, spec.target.run_id), (REPO, FIRST_RUN))
        self.assertEqual(spec.returns, (f"card:{CARD_REF}",))
        # `task show` of the code card names the run, the wait card and the count.
        view = self.card()["e2e"]
        self.assertEqual((view["runs_dispatched"], view["run_cap"]), (1, 3))
        self.assertEqual(view["runs"][0]["wait_card"], run.wait_ref)
        self.assertEqual(view["runs"][0]["state"], "waiting")
        self.assertEqual(view["runs"][0]["run"], RUN_URL)

        # While the run is in flight nothing but the wait card is asked: no gate read, no dispatch.
        gate_reads = len(self.host.gate_calls)
        self.assertEqual(self.tick()["action"], "e2e-waiting")
        self.assertEqual(self.tick_wait()["action"], "wait-waiting")
        self.assertEqual(self.tick()["action"], "e2e-waiting")
        self.assertEqual(len(self.host.gate_calls), gate_reads)

        self.conclude("success")
        self.assertEqual(self.reader.show(run.wait_ref)["state"], "done")
        self.assertTrue(self.comments(f"[wait:target_reached] {run.wait_ref}"))
        parked = self.tick()

        self.assertEqual(parked["to"], "assessment", parked)
        self.assertEqual(self.card()["state"], "assessment")
        self.assertEqual(len(self.host.dispatches), 1)
        self.assertEqual(self.card()["e2e"]["runs"][0]["state"], "success")
        [green] = self.comments("## E2E — green")
        self.assertIn(RUN_URL, green)
        self.assertIn(SHA, green)

        # The release audit on the same SHA: no second run.
        self._decide("release")
        released = self.tick()
        self.assertEqual(released["to"], "done", released)
        self.assertEqual(len(self.host.dispatches), 1)
        self.assertEqual(len(self.wait_cards()), 1)

    def test_review_skipped_runs_the_e2e_after_green_ci_and_then_releases(self) -> None:
        self.arrange(review="skipped", observed=False)
        self._run_worker_to_validate()

        self.assertEqual(self.tick()["action"], "e2e-waiting")
        self.assertEqual(self.host.reviews, [])
        self.assertEqual(len(self.host.dispatches), 1)
        self.conclude("success")
        released = self.tick()

        self.assertEqual(released["to"], "done", released)
        self.assertEqual(self.host.completed, [CARD_REF])
        self.assertEqual(len(self.host.dispatches), 1)

    def test_a_red_review_dispatches_nothing(self) -> None:
        self.arrange()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self._review_red()

        parked = self.tick()

        self.assertEqual(parked["to"], "assessment")
        self.assertEqual(self.host.dispatches, [])
        self.assertNotIn("e2e", self.card())

    def test_a_release_decided_without_a_green_run_waits_for_one_before_the_merge(self) -> None:
        """A card parked by a red review never ran the stage; a release decision runs it, once."""
        self.arrange()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self._review_red()
        self.assertEqual(self.tick()["to"], "assessment")
        self._decide("release")

        waiting = self.tick()
        self.assertEqual((waiting["action"], waiting["step"]), ("e2e-waiting", "assessment"), waiting)
        self.assertEqual(self.tick()["action"], "e2e-waiting")
        self.assertEqual(self.card()["state"], "assessment")
        self.assertEqual(self.host.completed, [])
        self.conclude("success")
        released = self.tick()

        self.assertEqual(released["to"], "done", released)
        self.assertEqual(self.host.completed, [CARD_REF])
        self.assertEqual(len(self.host.dispatches), 1)

    def test_a_project_without_e2e_behaves_as_before(self) -> None:
        self.arrange()
        self.catalog._adapter = {"validation": {"ci": "github", "required_checks": ["test"]}}
        self.to_green_review()

        self.assertEqual(self.tick()["to"], "assessment")
        self.assertEqual(self.host.dispatches, [])
        self.assertEqual(self.wait_cards(), [])

    # --- crash and restart ---------------------------------------------------------------------------

    def test_a_crash_after_the_dispatch_finds_the_run_by_its_dispatch_id(self) -> None:
        self.arrange()
        self.to_green_review()
        self.host.dispatch_answer = "crash"
        with self.assertRaises(SimulatedCrash):
            self.tick()
        [intent] = e2e_state(self.card()).runs
        self.assertEqual((intent.sha, intent.run_id, intent.dispatch), (SHA, 0, "intent"))

        self.host.dispatch_answer = "ok"
        recovered = self._runtime()
        waiting = self.tick(recovered)

        self.assertEqual(waiting["action"], "e2e-waiting", waiting)
        self.assertEqual(len(self.host.dispatches), 1, "recovery never dispatches again")
        [run] = e2e_state(self.card()).runs
        self.assertEqual((run.dispatch_id, run.run_id), (intent.dispatch_id, FIRST_RUN))
        self.assertTrue(run.wait_ref)

    def test_a_crash_after_the_wait_card_was_created_creates_no_second_one(self) -> None:
        self.arrange()
        self.to_green_review()
        record = TaskWriter.record_e2e_state
        crashed: list[str] = []

        def crash_on_the_wait_ref(writer: TaskWriter, **fields: Any) -> None:
            if not crashed and '"wait_ref":"secretary-' in fields["state"]:
                crashed.append(fields["state"])
                raise SimulatedCrash("the dispatcher died before recording the wait card")
            record(writer, **fields)

        with (
            mock.patch.object(TaskWriter, "record_e2e_state", crash_on_the_wait_ref),
            self.assertRaises(SimulatedCrash),
        ):
            self.tick()
        self.assertEqual(len(self.wait_cards()), 1)
        self.assertEqual(e2e_state(self.card()).runs[0].wait_ref, "")

        waiting = self.tick(self._runtime())

        self.assertEqual(waiting["action"], "e2e-waiting", waiting)
        [wait] = self.wait_cards()
        self.assertEqual(e2e_state(self.card()).runs[0].wait_ref, wait["ref"])
        self.assertEqual(len(self.host.dispatches), 1)

    def test_a_restart_while_the_run_is_pending_continues_the_card(self) -> None:
        self.to_waiting()
        self.tick_wait()

        restarted = self._runtime()
        self.assertEqual(self.tick(restarted)["action"], "e2e-waiting")
        self.host.run_answer = ("completed", "success")
        self.assertEqual(self.tick_wait(restarted)["action"], "wait-target-reached")
        parked = self.tick(restarted)

        self.assertEqual(parked["to"], "assessment", parked)
        self.assertEqual(len(self.host.dispatches), 1)
        self.assertEqual(len(self.wait_cards()), 1)

    # --- outcomes ------------------------------------------------------------------------------------

    def test_a_failed_run_returns_the_card_to_rework_with_the_evidence_in_task_md(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.to_waiting()
        self.host.jobs = [
            {"name": "setup", "conclusion": "success", "html_url": RUN_URL, "steps": []},
            {"name": "mega", "conclusion": "failure", "html_url": RUN_URL, "steps": ["Run e2e suite"]},
        ]
        self.conclude("failure")

        reworked = self.tick()

        self.assertEqual(self.card()["state"], "in_progress", reworked)
        document = self._task_document()
        self.assertIn("## Mechanical gate failure to address", document)
        self.assertIn(RUN_URL, document)
        self.assertIn("concluded failure", document)
        self.assertIn("job «mega»", document)
        self.assertIn('step "Run e2e suite"', document)
        self.assertIn("stand 2 never answered its health check", document)
        self.assertEqual(self._pilot_record()["rejected_sha"], SHA)
        self.assertEqual(self.card()["e2e"]["runs"][0]["state"], "failure")
        self.assertEqual(len(self.host.dispatches), 1)

    def test_a_cancelled_run_blocks_the_card_as_infrastructure(self) -> None:
        self.to_waiting()
        wait = self.wait_ref()
        self.conclude("cancelled")

        blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, "concluded cancelled", RUN_URL, wait)
        self.assertEqual(self.card()["e2e"]["runs"][0]["state"], "cancelled")

    def test_a_passed_deadline_blocks_the_card(self) -> None:
        self.to_waiting()
        wait = self.wait_ref()
        later = datetime.now(UTC) + timedelta(hours=3)
        with mock.patch("secretary.dispatch.wait_cards.utcnow", return_value=later):
            self.assertEqual(self.tick_wait()["action"], "wait-ended")
        self.assertEqual(wait_state(self.reader.show(wait)).result["outcome"], "deadline_passed")

        blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, "deadline_passed", wait)
        self.assertEqual(self.card()["e2e"]["runs"][0]["state"], "deadline_passed")

    def test_an_unreachable_run_blocks_the_card(self) -> None:
        self.to_waiting()
        wait = self.wait_ref()
        self.host.run_answer = ("http", "gh: Not Found (HTTP 404)")
        self.assertEqual(self.tick_wait()["action"], "wait-ended")

        blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, "source_unreachable", wait)

    def test_a_refused_dispatch_blocks_the_card_with_the_reason(self) -> None:
        self.arrange()
        self.to_green_review()
        self.host.dispatch_answer = (
            "http",
            "gh: Workflow does not have 'workflow_dispatch' trigger (HTTP 422)",
        )

        blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, "does not have 'workflow_dispatch' trigger", "e2e.yml")
        view = self.card()["e2e"]
        self.assertEqual(view["runs_dispatched"], 0)
        self.assertEqual(view["runs"][0]["state"], "dispatch_refused")
        self.assertEqual(self.wait_cards(), [])

    def test_a_run_never_identified_blocks_the_card_and_is_not_dispatched_again(self) -> None:
        self.arrange()
        self.to_green_review()
        self.host.dispatch_answer = "hidden"
        self.assertEqual(self.tick()["action"], "e2e-identifying")
        self.assertEqual(self.tick()["action"], "e2e-identifying")
        later = datetime.now(UTC) + timedelta(hours=1)
        with mock.patch.object(e2e_stage, "utcnow", return_value=later):
            blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, "secretary_dispatch_id", "run-name")
        self.assertEqual(len(self.host.dispatches), 1)
        self.assertEqual(self.wait_cards(), [])

    # --- the interim cap ------------------------------------------------------------------------------

    def test_the_fourth_run_is_not_dispatched_and_the_card_is_blocked(self) -> None:
        self.arrange()
        previous = {
            "runs": [
                {
                    "dispatch_id": f"{CARD_REF}-e2e-{n}-0000000{n}",
                    "sha": sha,
                    "repo": REPO,
                    "branch": BRANCH,
                    "workflow": "e2e.yml",
                    "intent_at": "2026-09-27T10:00:00Z",
                    "dispatch": "sent",
                    "run_id": 8000 + n,
                    "run_url": f"https://github.com/{REPO}/actions/runs/{8000 + n}",
                    "result": {"outcome": "target_reached", "conclusion": "failure", "summary": "red"},
                }
                for n, sha in ((1, "1" * 40), (2, "2" * 40), (3, SECOND_SHA))
            ]
        }
        self.writer.record_e2e_state(
            role="dispatcher", actor="secretary-pilot", reference=CARD_REF, state=json.dumps(previous)
        )
        self.to_green_review()

        blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, "e2e run cap reached (3)", taxonomy="other")
        self.assertEqual(blocked["reason"], "e2e run cap reached (3)")
        self.assertEqual(self.host.dispatches, [])
        self.assertEqual(self.card()["e2e"]["runs_dispatched"], 3)


if __name__ == "__main__":
    unittest.main()
