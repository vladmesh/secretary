"""Decision and operation cards: created for a sprint, executed by its PO session, completed by the PO.

Unit-level (secretary-1758). The card side runs `TaskWriter` over a mock client for every refusal, which
is decided before the board is read, and over stubbed reads for `task complete`'s replay. The dispatcher
side runs the real `claim_ready_task`/`advance_po_card` against a one-card board in memory and a real
`PoService` over its socket, with the in-memory PO store and the fake CLIs of cards 1 and 2
(`tests.po_fake_store`, `tests.po_cli_fakes`). The PostgreSQL paths of the migration and of
`task complete` are covered by the integration-board suite (`tests/test_board_store_schema.py`,
`tests/test_tasks.py`).
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import signal
import stat
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from secretary.board.completion_evidence import (
    NO_CANDIDATE_KINDS,
    has_candidate,
    missing_completion_evidence,
    po_completion_fields,
    po_completion_record,
    render_po_completion_record,
    review_required,
)
from secretary.board.models import Actor, CardState, EntityKind, Event
from secretary.board.task_routing import TaskMetadata, TaskReview, TaskType, default_review
from secretary.board.transitions import transition_for
from secretary.cli import main
from secretary.dispatch.claim import claim_ready_task
from secretary.dispatch.po_cards import (
    PO_BLOCKED_ACTION,
    PO_SUBMITTED,
    ServicePoChannel,
    _po_record,
    advance_po_card,
    complete_command,
)
from secretary.dispatch.production import ProbeAbort, _probe_runtime
from secretary.dispatch.runtime import DispatcherRuntime
from secretary.dispatch.state import DispatcherRecord, attempt_request_id, new_attempt_id
from secretary.po import store as po_store
from secretary.po.client import OutcomeUnknown
from secretary.po.runner import PoRunner
from secretary.po.service import PoService, listening
from secretary.po.sprints import SprintRecord
from secretary.tasks import TaskError, TaskWriter, is_significant_card_event, is_significant_observer_event
from tests.po_cli_fakes import FAKE_CLAUDE, eventually
from tests.po_fake_store import FakeBoard, FakePoStore, FakeSprints

REF = "secretary-1900"
SPRINT = "sprint:1"
DECISION_BODY = "## Decision\nShip the narrow cut.\n\n## How to verify\n`secretary sprint show --ref sprint:1`\n"
OPERATION_BODY = "## What was done\nRotated the key.\n\n## How to verify\n`ssh relay true` exits 0\n"


class Forbidden:
    """A collaborator a decision/operation card must never reach: no head, no workspace, no registry."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        raise AssertionError(f"a PO-executed card reached the {self._name}: {attribute}")


class OneCardBoard:
    """One card as the dispatcher reads and writes it, and the audit its writes leave behind."""

    def __init__(self, card: dict[str, Any]) -> None:
        self.card = card
        self.log: list[dict[str, Any]] = []

    # reader
    def show(self, reference: str) -> dict[str, Any]:
        assert reference == self.card["ref"], reference
        return copy.deepcopy(self.card)

    # writer
    def claim(self, *, role: str, reference: str, worker: str, request_id: str, **_: Any) -> dict[str, Any]:
        if self.committed_event(request_id) is None:
            assert self.card["state"] == "ready", "claim requires a Ready task"
            self.card["state"] = "in_progress"
            self.card["claim"] = {"worker": worker}
            self.log.append({"request_id": request_id, "ref": reference, "kind": "claim", "role": role})
        return {"action": "claimed"}

    def move(
        self, *, role: str, reference: str, target: str, reason: str, request_id: str, **fields: Any
    ) -> dict[str, Any]:
        if self.committed_event(request_id) is None:
            self.card["state"] = target
            self.log.append(
                {"request_id": request_id, "ref": reference, "kind": "move", "role": role, "to": target,
                 "reason": reason, **fields}
            )
        return {"action": "moved"}

    # audit
    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        return next((event for event in self.log if event["request_id"] == request_id), None)

    def events(self, reference: str = "", **_: Any) -> list[dict[str, Any]]:
        return [event for event in self.log if not reference or event["ref"] == reference]

    # the PO, completing the card inside its turn
    def complete_as_po(self, kind: str, body: str) -> None:
        fields, refusal = po_completion_fields(kind, body)
        assert not refusal, refusal
        self.card["comments"].append({"marker": "po", "body": "[po]\n" + render_po_completion_record(kind, fields)})
        self.card["state"] = "done"


class SprintView:
    """The dispatcher's sprint reader: the one sprint, with its comments in board order."""

    def __init__(self, comments: list[str]) -> None:
        self.comments = comments

    def show(self, reference: str, **_: Any) -> dict[str, Any]:
        assert reference == SPRINT, reference
        return {
            "ref": SPRINT,
            "status": "open",
            "comments": [
                {"created_at": f"2026-09-26T10:0{index}:00Z", "body": body}
                for index, body in enumerate(self.comments)
            ],
        }


def card(kind: str = "decision", *, state: str = "ready", description: str = "Which cut ships first?") -> dict:
    return {
        "ref": REF,
        "id": 1900,
        "title": f"The {kind} to take",
        "description": description,
        "type": kind,
        "state": state,
        "project": "secretary",
        "sprint": SPRINT,
        "review": "skipped",
        "claim": {"worker": None},
        "workspace": {"slug": None},
        "comments": [],
    }


class DispatcherFixture(unittest.TestCase):
    """A real PO service on its socket, and a dispatcher runtime around one card and one sprint.

    The service is the one `tests/test_po_service.py` drives: the fake `claude` of `tests.po_cli_fakes`
    runs every turn as a real process, and the board store is the in-memory `tests.po_fake_store`.
    """

    def setUp(self) -> None:
        # A short root: the service's Unix socket lives under it.
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="po-")))
        self.data = self.root / "data"
        (self.data / "po").mkdir(parents=True)
        claude = self.root / "bin" / "claude"
        claude.parent.mkdir()
        claude.write_text(FAKE_CLAUDE, encoding="utf-8")
        claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
        self.claude = str(claude)
        self.log = self.root / "fake.log"
        self.gate = self.log.with_name(self.log.name + ".gate")
        self.board = FakeBoard()
        self.po_sprints = FakeSprints({SPRINT: None})
        self.services: list[PoService] = []
        self.addCleanup(self.stop_everything)

    def start(self, *, listen: bool = True) -> PoService:
        runner = PoRunner(
            FakePoStore(self.board),
            self.data,
            executables={"claude": self.claude},
            env={**os.environ, "FAKE_LOG": str(self.log)},
        )
        service = PoService(runner, data_dir=self.data, sprints=self.po_sprints, models={"claude": ("opus",)})
        self.services.append(service)
        service.start()
        thread = threading.Thread(target=service.run, kwargs={"tick": 0.05, "say": lambda _line: None})
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(service.stop)
        if listen:
            self.enterContext(listening(service))
        return service

    def stop_everything(self) -> None:
        self.gate.touch()
        for service in self.services:
            service.stop()
            for live in list(service.runner._live.values()):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(live.process.pid, signal.SIGKILL)

    def session(self, service: PoService) -> str:
        return service.create_session(cli="claude", model="opus", effort="default", request_id="c-owner")[
            "session_id"
        ]

    def settled(self, session_id: str, seq: int) -> po_store.Turn:
        store = FakePoStore(self.board)
        eventually(
            lambda: len(store.turns(session_id)) >= seq and store.turns(session_id)[seq - 1].state != po_store.RUNNING,
            f"turn {seq} never settled",
        )
        return store.turns(session_id)[seq - 1]

    def reached_gate(self, session_id: str, seq: int) -> None:
        stdout = self.data / "po-runs" / session_id / f"turn-{seq:04d}.stdout"
        eventually(
            lambda: stdout.exists() and "TOOL-CALL-SECRET" in stdout.read_text(),
            f"turn {seq} never reached its gate",
        )

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def runtime(self, task: dict[str, Any], *, comments: list[str] | None = None, po: Any = None):
        self.cards = OneCardBoard(task)
        channel = ServicePoChannel(self.data, None)
        channel._store = FakePoStore(self.board)
        self.saved: list[dict[str, Any]] = []
        return SimpleNamespace(
            owner="secretary-dispatcher",
            reader=self.cards,
            writer=self.cards,
            audit=self.cards,
            sprints=SprintView(comments if comments is not None else ["Opened the sprint.", "Owner: keep it small."]),
            po=po or channel,
            host=Forbidden("host"),
            catalog=Forbidden("catalog"),
            head_health=Forbidden("head health"),
            save_records=lambda payload, records: self.saved.append(
                {ref: record.to_json() for ref, record in records.items()}
            ),
        )

    def claim(self, runtime: Any, records: dict[str, DispatcherRecord] | None = None):
        self.records = {} if records is None else records
        self.payload: dict[str, Any] = {}
        self.attempt = new_attempt_id()
        return claim_ready_task(runtime, runtime.reader.show(REF), self.records, self.payload, self.attempt)

    def tick(self, runtime: Any) -> dict[str, Any]:
        return advance_po_card(runtime, runtime.reader.show(REF), self.records, self.payload, self.attempt)

    def record(self) -> DispatcherRecord:
        return self.records[REF]

    def session_ids(self) -> list[str]:
        return list(self.board.sessions)


class ClaimAndSubmitTests(DispatcherFixture):
    def test_the_claim_launches_no_head_and_cuts_no_workspace(self) -> None:
        self.start()
        runtime = self.runtime(card())

        outcome = self.claim(runtime)

        self.assertEqual((outcome["status"], outcome["action"]), ("ok", "po-card-submitted"))
        self.assertEqual(self.cards.card["state"], "in_progress")
        record = self.record()
        self.assertEqual((record.workspace, record.handle, record.head, record.review_head), ("", "", "", ""))
        self.assertFalse(record.needs_settling())
        self.assertEqual(record.state, PO_SUBMITTED)
        self.assertEqual([event["kind"] for event in self.cards.log], ["claim"])

    def test_resolve_and_submit_carry_ids_derived_from_the_card_and_the_claim_attempt(self) -> None:
        self.start()
        runtime = self.runtime(card())

        self.claim(runtime)

        submission = self.record().po_submission
        self.assertEqual(
            (submission.session_request_id, submission.submit_request_id, submission.complete_request_id),
            tuple(attempt_request_id(self.attempt, action, REF) for action in ("po-session", "po-submit", "po-complete")),
        )
        [session] = self.session_ids()
        self.assertEqual((submission.session_id, submission.session_outcome), (session, "created"))
        self.assertEqual(self.po_sprints.records[SPRINT].po_session, session)
        # The resolver's seed is the session's first input; the card is the next one, from the dispatcher.
        self.settled(session, 2)
        request = FakePoStore(self.board).request(submission.submit_request_id)
        self.assertEqual((request.operation, request.session_id, request.seq), (po_store.SEND, session, 2))

    def test_an_open_recorded_session_is_used_and_nothing_else_is_opened(self) -> None:
        service = self.start()
        session = self.session(service)
        self.po_sprints.records[SPRINT] = SprintRecord(SPRINT, "open", session)
        runtime = self.runtime(card("operation"))

        self.claim(runtime)

        submission = self.record().po_submission
        self.assertEqual((submission.session_id, submission.session_outcome), (session, "recorded"))
        self.assertEqual(self.session_ids(), [session])
        self.assertEqual(self.settled(session, 1).state, po_store.COMPLETED)

    def test_the_input_carries_the_card_the_sprint_comments_in_order_and_the_command(self) -> None:
        self.start()
        runtime = self.runtime(
            card(description="Pick between the two cuts in issue:abc."),
            comments=["First: the sprint opened.", "Second: the owner wants it small.", "Third: noted."],
        )

        self.claim(runtime)

        submission = self.record().po_submission
        [session] = self.session_ids()
        self.settled(session, 2)
        prompt = [call["prompt"] for call in self.calls()][-1]
        self.assertEqual(prompt, submission.text)
        for expected in (
            REF,
            "decision",
            "The decision to take",
            "Pick between the two cuts in issue:abc.",
            "## Decision",
            "## How to verify",
            complete_command(REF, "decision", submission.complete_request_id),
            "Keep the turn short; anything long-running becomes a card.",
        ):
            self.assertIn(expected, prompt)
        positions = [prompt.index(comment) for comment in ("First:", "Second:", "Third:")]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(
            complete_command(REF, "decision", submission.complete_request_id),
            f"python3 -P -m secretary task complete --ref {REF} --role po --kind decision --body-file <file> "
            f"--request-id {submission.complete_request_id}",
        )

    def test_the_dispatcher_is_the_source_of_its_input(self) -> None:
        service = self.start(listen=False)
        runtime = self.runtime(card())
        with mock.patch.object(service.queue, "put", wraps=service.queue.put) as put, listening(service):
            self.claim(runtime)
        self.assertEqual([call.kwargs["source"] for call in put.call_args_list], ["po-service", "dispatcher"])


class RepeatTests(DispatcherFixture):
    def test_an_unanswered_resolve_is_repeated_with_the_same_id_and_opens_one_session(self) -> None:
        self.start()
        self.po_sprints.fail["record_po_session"] = 1
        runtime = self.runtime(card())

        first = self.claim(runtime)

        self.assertEqual((first["status"], first["action"]), ("degraded", "po-service-unanswered"))
        self.assertEqual(self.cards.card["state"], "in_progress")
        submission = self.record().po_submission
        self.assertEqual((submission.session_id, submission.submitted, submission.unanswered), ("", False, 1))
        self.assertIn("OutcomeUnknown", submission.last_error)
        session_request = submission.session_request_id

        second = self.tick(runtime)

        self.assertEqual(second["action"], "po-card-submitted")
        self.assertEqual(self.record().po_submission.session_request_id, session_request)
        [session] = self.session_ids()
        self.assertEqual(self.record().po_submission.session_id, session)
        self.assertEqual(self.po_sprints.records[SPRINT].po_session, session)
        self.assertEqual(len(self.po_sprints.comments), 1)

    def test_an_unanswered_submit_is_repeated_with_the_same_id_and_makes_one_input(self) -> None:
        self.start()

        class LosesTheFirstAnswer:
            def __init__(self, inner: Any) -> None:
                self.inner, self.lost, self.ids = inner, False, []

            def submit(self, **fields: Any) -> dict[str, Any]:
                self.ids.append(fields["request_id"])
                answer = self.inner.submit(**fields)
                if not self.lost:
                    self.lost = True
                    raise OutcomeUnknown("no answer from the PO service: connection reset")
                return answer

            def __getattr__(self, name: str) -> Any:
                return getattr(self.inner, name)

        runtime = self.runtime(card())
        runtime.po = channel = LosesTheFirstAnswer(runtime.po)

        first = self.claim(runtime)
        second = self.tick(runtime)

        self.assertEqual((first["action"], second["action"]), ("po-service-unanswered", "po-card-submitted"))
        self.assertEqual(len(set(channel.ids)), 1)
        self.assertEqual(len(channel.ids), 2)
        [session] = self.session_ids()
        self.settled(session, 2)
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 2)

    def test_a_service_that_is_down_leaves_the_card_in_progress_and_the_next_tick_submits(self) -> None:
        service = self.start(listen=False)
        runtime = self.runtime(card())

        down = [self.claim(runtime), self.tick(runtime)]

        self.assertEqual([outcome["action"] for outcome in down], ["po-service-unanswered"] * 2)
        self.assertEqual(self.cards.card["state"], "in_progress")
        self.assertEqual(self.record().po_submission.unanswered, 2)
        self.assertIn("ServiceUnavailable", self.record().po_submission.last_error)
        self.assertEqual(self.session_ids(), [])
        with listening(service):
            self.assertEqual(self.tick(runtime)["action"], "po-card-submitted")
        self.assertEqual(len(self.session_ids()), 1)

    def test_a_lost_record_is_rebuilt_from_the_claim_and_repeats_the_same_requests(self) -> None:
        self.start()
        runtime = self.runtime(card())
        self.claim(runtime)
        before = self.record().po_submission

        self.records.clear()
        again = self.tick(runtime)

        rebuilt = self.record().po_submission
        self.assertEqual(again["action"], "po-card-submitted")
        self.assertEqual(
            (rebuilt.session_request_id, rebuilt.submit_request_id, rebuilt.session_id),
            (before.session_request_id, before.submit_request_id, before.session_id),
        )
        [session] = self.session_ids()
        self.settled(session, 2)
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 2)

    def test_a_refused_resolve_blocks_the_card(self) -> None:
        self.po_sprints = FakeSprints({SPRINT: None}, status={SPRINT: "closed"})
        self.start()
        runtime = self.runtime(card())

        outcome = self.claim(runtime)

        self.assertEqual((outcome["status"], outcome["action"]), ("blocked", PO_BLOCKED_ACTION))
        self.assertEqual(self.cards.card["state"], "blocked")
        self.assertIn("the PO service refused the resolve", self.cards.log[-1]["reason"])
        self.assertNotIn(REF, self.records)


class SettleTests(DispatcherFixture):
    def test_a_card_waits_while_its_turn_runs_and_closes_once_the_po_completed_it(self) -> None:
        self.start()
        runtime = self.runtime(card(description="GATE: hold the turn until the test opens it."))
        self.claim(runtime)
        [session] = self.session_ids()
        eventually(lambda: len(FakePoStore(self.board).turns(session)) == 2, "the card never became a turn")

        running = self.tick(runtime)
        self.assertEqual(running["action"], "po-card-turn-running")
        self.assertEqual(self.record().po_submission.seq, 2)

        self.cards.complete_as_po("decision", DECISION_BODY)
        self.gate.touch()
        self.settled(session, 2)
        closed = self.tick(runtime)

        self.assertEqual((closed["action"], closed["state"], closed["completion"]), ("po-card-closed", "done", "recorded"))
        self.assertNotIn(REF, self.records)
        self.assertEqual([event["kind"] for event in self.cards.log], ["claim"])

    def assert_blocked_after(self, description: str, state: str) -> None:
        self.start()
        runtime = self.runtime(card(description=description))
        self.claim(runtime)
        session = self.record().po_submission.session_id
        self.assertEqual(self.settled(session, 2).state, state)

        blocked = self.tick(runtime)

        reason = f"PO turn {session}/2 ended {state} without completing the card"
        self.assertEqual((blocked["status"], blocked["reason"]), ("blocked", reason))
        self.assertEqual(self.cards.card["state"], "blocked")
        move = self.cards.log[-1]
        self.assertEqual(
            (move["kind"], move["role"], move["to"], move["reason"], move["request_id"]),
            ("move", "dispatcher", "blocked", reason, attempt_request_id(self.attempt, PO_BLOCKED_ACTION, REF)),
        )
        self.assertEqual(move["terminal_taxonomy"]["disposition"], "blocked")
        self.assertNotIn(REF, self.records)

    def test_a_completed_turn_that_left_the_card_in_progress_blocks_it(self) -> None:
        self.assert_blocked_after("An ordinary question.", po_store.COMPLETED)

    def test_a_failed_turn_blocks_the_card(self) -> None:
        self.assert_blocked_after("FAIL this turn.", po_store.FAILED)

    def test_an_input_queued_behind_another_turn_waits(self) -> None:
        service = self.start()
        session = self.session(service)
        self.po_sprints.records[SPRINT] = SprintRecord(SPRINT, "open", session)
        service.submit(session_id=session, text="GATE the owner's long question", request_id="owner-1")
        self.reached_gate(session, 1)
        runtime = self.runtime(card())

        self.claim(runtime)
        waiting = self.tick(runtime)

        self.assertEqual(waiting["action"], "po-card-queued")
        self.assertIsNone(self.record().po_submission.seq)
        self.gate.touch()
        self.settled(session, 2)
        self.assertEqual(self.tick(runtime)["status"], "blocked")


class DispatcherEntryTests(unittest.TestCase):
    def test_the_tick_hands_a_po_card_to_its_own_lane_before_any_head_path(self) -> None:
        runtime = SimpleNamespace()
        with mock.patch("secretary.dispatch.runtime._advance_po_card", return_value={"action": "po"}) as lane:
            for kind in ("decision", "operation"):
                outcome = DispatcherRuntime._tick_task(runtime, card(kind, state="in_progress"), {}, {}, "a-1")
                self.assertEqual(outcome, {"action": "po"})
        self.assertEqual(lane.call_count, 2)

    def test_the_probe_aborts_before_a_resolve_or_a_submit(self) -> None:
        runtime = SimpleNamespace(writer=object(), host=object(), production_state=object(), po=mock.Mock())
        probe = _probe_runtime(runtime)
        for call in (
            lambda: probe.po.sprint_session(sprint_ref=SPRINT, request_id="r"),
            lambda: probe.po.submit(session_id="s", text="t", request_id="r", source="dispatcher"),
        ):
            with self.assertRaises(ProbeAbort):
                call()
        probe.po.request("r")
        runtime.po.request.assert_called_once_with("r")
        runtime.po.submit.assert_not_called()

    def test_the_record_round_trips_and_a_headed_record_keeps_its_released_shape(self) -> None:
        headed = DispatcherRecord(
            worker="w", workspace="/w", handle="", head="codex", review_head="", attempt_id="a",
            comment_baseline=0, review_baseline=0, state="claimed", claimed_at=1.0,
        )
        self.assertNotIn("po_submission", headed.to_json())
        self.assertFalse(DispatcherRecord.from_json(headed.to_json()).po_submission)

        record = _po_record(card(state="in_progress"), "attempt-1")
        record.po_submission.session_id, record.po_submission.seq = "s-1", 3
        loaded = DispatcherRecord.from_json(json.loads(json.dumps(record.to_json())))
        self.assertEqual(loaded.po_submission, record.po_submission)
        self.assertEqual(loaded.po_submission.kind, "decision")


class CreateValidationTests(unittest.TestCase):
    """Every refusal of a decision/operation create is decided before the board is read."""

    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.client = mock.Mock(instance_dir=tmp)
        self.writer = TaskWriter(self.client, data_dir=tmp)

    def create(self, kind: str, role: str = "observer", **fields: Any) -> dict:
        return self.writer.create(
            role=role, actor=role, project="secretary", task_type=kind, title="T", **fields
        )

    def test_every_refused_flag_and_the_missing_sprint_is_refused_with_a_reason(self) -> None:
        for kind in ("decision", "operation"):
            for fields, reason in (
                ({}, f"a {kind} card needs --sprint"),
                ({"sprint": SPRINT, "head": "codex"}, "takes no --head"),
                ({"sprint": SPRINT, "review_head": "claude-opus"}, "takes no --review-head"),
                ({"sprint": SPRINT, "review": "required"}, "takes no --review required"),
                ({"sprint": SPRINT, "live_impact": True}, "takes no --live-impact"),
                ({"sprint": SPRINT, "seed_ref": "abc123", "supersedes": "secretary-1"}, "takes no --seed-ref"),
                ({"sprint": SPRINT, "base_branch": "main"}, "takes no --base-branch"),
            ):
                for role in ("observer", "po"):
                    with self.subTest(kind=kind, fields=fields, role=role):
                        with self.assertRaisesRegex(TaskError, reason) as raised:
                            self.create(kind, role, **fields)
                        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.mock_calls, [])

    def test_only_the_observer_and_the_po_cut_one(self) -> None:
        for role in ("worker", "reviewer", "retro", "steward"):
            with self.subTest(role=role), self.assertRaisesRegex(TaskError, "cut by the observer or the PO"):
                self.create("decision", role, sprint=SPRINT, target="issues")
        self.assertEqual(self.client.mock_calls, [])

    def test_no_edit_puts_a_head_on_one(self) -> None:
        self.writer.reader = mock.Mock(show=lambda reference: card("operation"))
        for fields in ({"head": "codex"}, {"review_head": "claude-opus"}):
            with self.subTest(fields=fields), self.assertRaisesRegex(TaskError, "takes no head or reviewer"):
                self.writer.edit(role="po", actor="po", reference=REF, **fields)
        self.assertEqual(self.client.mock_calls, [])

    def test_the_kinds_review_default_is_skipped(self) -> None:
        for kind in (TaskType.DECISION, TaskType.OPERATION):
            self.assertIs(default_review(kind), TaskReview.SKIPPED)

    def test_the_cli_passes_both_kinds_through_and_offers_complete_to_the_po_only(self) -> None:
        for kind in ("decision", "operation"):
            writer = mock.Mock()
            writer.return_value.create.return_value = {"action": "created"}
            with (
                tempfile.TemporaryDirectory() as tmp,
                mock.patch("secretary.task_commands.TaskWriter", writer),
                mock.patch("secretary.task_commands.card_client"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = main(
                    ["task", "create", "--role", "observer", "--instance", tmp, "--data-dir", tmp,
                     "--project", "secretary", "--type", kind, "--title", "T", "--sprint", SPRINT]
                )
            self.assertEqual(code, 0)
            kwargs = writer.return_value.create.call_args.kwargs
            self.assertEqual((kwargs["task_type"], kwargs["sprint"], kwargs["review"]), (kind, SPRINT, ""))

        writer = mock.Mock()
        writer.return_value.complete.return_value = {"action": "completed"}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("secretary.task_commands.TaskWriter", writer),
            mock.patch("secretary.task_commands.card_client"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            body = f"{tmp}/body.md"
            with open(body, "w", encoding="utf-8") as handle:
                handle.write(OPERATION_BODY)
            code = main(
                ["task", "complete", "--ref", REF, "--role", "po", "--kind", "operation", "--instance", tmp,
                 "--data-dir", tmp, "--body-file", body, "--request-id", "done-1"]
            )
            with contextlib.redirect_stderr(io.StringIO()):
                refused = main(
                    ["task", "complete", "--ref", REF, "--role", "observer", "--kind", "operation",
                     "--body-file", body]
                )
        self.assertEqual((code, refused), (0, 2))
        kwargs = writer.return_value.complete.call_args.kwargs
        self.assertEqual(
            (kwargs["reference"], kwargs["kind"], kwargs["body"], kwargs["request_id"]),
            (REF, "operation", OPERATION_BODY, "done-1"),
        )


class TaskCompleteTests(unittest.TestCase):
    """`TaskWriter.complete` over stubbed reads: its refusals write nothing, a repeat writes nothing new."""

    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.client = mock.Mock(instance_dir=tmp)
        self.writer = TaskWriter(self.client, data_dir=tmp)
        self.card = card(state="in_progress")
        self.writer.reader = mock.Mock(show=lambda reference: copy.deepcopy(self.card))
        self.events: dict[str, Any] = {}
        self.writer._typed_event = lambda request_id: self.events.get(request_id)  # type: ignore[method-assign]
        self.transitions: list[dict[str, Any]] = []

        def transition(**fields: Any) -> Any:
            self.transitions.append(fields)
            fields["finish"](None)
            self.events[fields["request_id"]] = SimpleNamespace(
                ref=fields["reference"], reason=fields["reason"], source_state="in_progress"
            )
            self.card["state"] = fields["target"].value
            return SimpleNamespace(event=SimpleNamespace(event_id=f"evt-{len(self.transitions)}"))

        self.writer._transition_card = transition  # type: ignore[method-assign]

    def complete(self, body: str = DECISION_BODY, kind: str = "decision", **fields: Any) -> dict:
        return self.writer.complete(
            role=fields.pop("role", "po"), actor="po", reference=REF, kind=kind, body=body,
            request_id=fields.pop("request_id", "complete-1"),
        )

    def comments_written(self) -> list[str]:
        return [
            call.kwargs["content"] for call in self.client.call.call_args_list if call.args[0] == "createComment"
        ]

    def test_it_writes_the_record_as_the_po_and_moves_the_card_to_done_once(self) -> None:
        first = self.complete()
        again = self.complete()

        self.assertEqual((first["action"], first["replayed"], again["replayed"]), ("completed", False, True))
        self.assertEqual([t["target"] for t in self.transitions], [CardState.DONE, CardState.DONE])
        self.assertEqual({t["role"] for t in self.transitions}, {"po"})
        [comment] = self.comments_written()
        self.assertTrue(comment.startswith("[po]\n[completion:decision]\n"))
        stored = {**self.card, "comments": [{"marker": "po", "body": comment}]}
        self.assertEqual(
            po_completion_record(stored),
            {"Decision": "Ship the narrow cut.", "How to verify": "`secretary sprint show --ref sprint:1`"},
        )

    def test_the_same_id_with_another_record_is_a_conflict(self) -> None:
        self.complete()
        with self.assertRaises(TaskError) as raised:
            self.complete(DECISION_BODY.replace("narrow", "wide"))
        self.assertEqual(raised.exception.code, "request_conflict")
        self.assertEqual(len(self.transitions), 1)

    def test_each_refusal_writes_nothing(self) -> None:
        cases = [
            ({"body": "## Decision\nYes.\n"}, "validation", "`## How to verify`"),
            ({"body": "## Decision\n\n## How to verify\nlook\n"}, "validation", "`## Decision`"),
            ({"kind": "operation", "body": DECISION_BODY}, "validation", "`## What was done`"),
            ({"kind": "infra"}, "validation", "--kind decision, operation"),
            ({"role": "observer"}, "role_forbidden", ""),
        ]
        for fields, code, fragment in cases:
            with self.subTest(fields=fields), self.assertRaises(TaskError) as raised:
                self.complete(**fields)
            self.assertEqual(raised.exception.code, code)
            self.assertIn(fragment, raised.exception.message)
        # The kind the card has, and the column it stands in.
        with self.assertRaisesRegex(TaskError, "completes only a operation card"):
            self.complete(OPERATION_BODY, "operation")
        for state in ("ready", "blocked", "done"):
            self.card["state"] = state
            with self.subTest(state=state), self.assertRaises(TaskError) as raised:
                self.complete(request_id=f"complete-{state}")
            self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertEqual((self.transitions, self.comments_written()), ([], []))


class CompletionRecordTests(unittest.TestCase):
    def test_both_kinds_are_no_candidate_kinds_and_skip_review(self) -> None:
        for kind in ("decision", "operation"):
            self.assertIn(kind, NO_CANDIDATE_KINDS)
            self.assertFalse(has_candidate({"type": kind}))
            self.assertFalse(review_required({"type": kind, "review": "skipped"}))

    def test_the_record_is_read_back_only_from_a_po_comment(self) -> None:
        for kind, body in (("decision", DECISION_BODY), ("operation", OPERATION_BODY)):
            fields, refusal = po_completion_fields(kind, body)
            self.assertEqual(refusal, "")
            rendered = render_po_completion_record(kind, fields)
            self.assertTrue(rendered.startswith(f"[completion:{kind}]\n"))
            for marker, expected in (("po", fields), ("dispatcher", None), ("observer", None)):
                task = {"type": kind, "comments": [{"marker": marker, "body": f"[{marker}]\n{rendered}"}]}
                with self.subTest(kind=kind, marker=marker):
                    self.assertEqual(po_completion_record(task), expected)
                    self.assertEqual(missing_completion_evidence(task), "" if expected else f"completion:{kind}")
        # A decision record on an operation card is not its record.
        decision = render_po_completion_record("decision", po_completion_fields("decision", DECISION_BODY)[0])
        self.assertIsNone(
            po_completion_record({"type": "operation", "comments": [{"marker": "po", "body": f"[po]\n{decision}"}]})
        )


class ObserverWakeTests(unittest.TestCase):
    LINKED = frozenset({REF})

    def done(self, role: str, source: CardState = CardState.IN_PROGRESS, target: CardState = CardState.DONE):
        declaration = transition_for(EntityKind.CARD, source, target)
        return Event(
            f"evt-{role}-{target.value}",
            declaration.event_kind,
            EntityKind.CARD,
            REF,
            Actor(role, role),
            "[completion:decision]\n\n## Decision\n\nShip it.\n\n## How to verify\n\nlook",
            datetime(2026, 9, 26, tzinfo=UTC),
            source_state=source.value,
            target_state=target.value,
        ).to_record(f"request-{role}-{target.value}")

    def test_the_po_completion_and_the_blocked_turn_wake_the_observer(self) -> None:
        completion = self.done("po")
        blocked = self.done("dispatcher", target=CardState.BLOCKED)
        for event in (completion, blocked):
            with self.subTest(target=event["transition"]["target"]):
                self.assertTrue(is_significant_card_event(event, linked_refs=set(self.LINKED)))
                self.assertTrue(is_significant_observer_event(event, linked_refs=set(self.LINKED), sprint_ref=SPRINT))
        # Only for a card of that sprint.
        self.assertFalse(is_significant_observer_event(completion, linked_refs=set(), sprint_ref=SPRINT))

    def test_the_claim_and_the_submit_do_not(self) -> None:
        claim = self.done("dispatcher", source=CardState.READY, target=CardState.IN_PROGRESS)
        self.assertFalse(is_significant_observer_event(claim, linked_refs=set(self.LINKED), sprint_ref=SPRINT))


class PreexistingKindsTests(unittest.TestCase):
    def test_cards_of_the_three_old_kinds_and_the_typeless_card_load_unchanged(self) -> None:
        for raw, kind, review in (
            ({"task_type": "code"}, TaskType.CODE, "required"),
            ({"task_type": "research", "review": "skipped"}, TaskType.RESEARCH, "skipped"),
            ({"task_type": "infra", "review": "skipped"}, TaskType.INFRA, "skipped"),
            ({}, None, "required"),
        ):
            with self.subTest(raw=raw):
                metadata = TaskMetadata.from_legacy(raw, codex_modes={"tui"})
                self.assertIs(metadata.task_type, kind)
                self.assertEqual(metadata.review.value, review)
                self.assertEqual(has_candidate({"type": metadata.task_type_text}), kind is TaskType.CODE or kind is None)
        for kind in ("decision", "operation"):
            self.assertIs(TaskMetadata.from_legacy({"task_type": kind}, codex_modes={"tui"}).task_type, TaskType(kind))


if __name__ == "__main__":
    unittest.main()
