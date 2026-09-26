"""Production rights: an operation card names its production, and the PO service enforces the sprint's rule.

Unit-level (secretary-1764), on the fakes of cards 1 to 4. The card side runs `TaskWriter.create` over a
mock client, every refusal decided before the board is read. The rule runs in a real `PoService` over its
socket, reached by the real `advance_po_card`, with the in-memory PO store, the fake CLI, the fake sprint
port and the one-card board of `tests.po_card_fakes` (its `handover` is what `task handover` leaves). The
PostgreSQL path of create and the stored field is covered by the integration-board suite
(`tests/test_tasks.py`).
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from secretary.board.owner_handover import HANDED_TO_OWNER, HANDOVER_MARKER, waiting_owner
from secretary.board.production_rights import (
    CARD_INPUT,
    OWNER_ANSWER_INPUT,
    card_facts,
    facts_problem,
    refusal_reason,
    touches_production,
)
from secretary.cli import main
from secretary.dispatch.po_cards import ServicePoChannel, complete_command, owner_answer_request_id
from secretary.dispatch.state import DispatcherRecord
from secretary.po import store as po_store
from secretary.po.client import OutcomeUnknown
from secretary.po.queue import PoQueue
from secretary.po.sprints import BoardSprintSessions, HandoverRefused, SprintRecord
from secretary.tasks import TaskError, TaskReader, TaskWriter
from secretary.web import pages
from secretary.webproto.reads import _card_value
from tests.po_card_fakes import OPERATION_BODY, REF, SPRINT, DispatcherFixture, card
from tests.po_fake_store import FakePoStore

NOT_ALLOWED = f"operation touches production relay; sprint {SPRINT} allows []"


class CreateValidationTests(unittest.TestCase):
    """`--touches-production`: required on an operation, refused elsewhere, an unknown project refused."""

    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        registry = Path(self.tmp) / "projects"
        registry.mkdir()
        (registry / "secretary.yaml").write_text("id: secretary\n", encoding="utf-8")
        self.client = mock.Mock(instance_dir=self.tmp)
        self.writer = TaskWriter(self.client, data_dir=self.tmp)

    def create(self, kind: str, **fields: Any) -> dict:
        return self.writer.create(
            role="observer", actor="observer", project="secretary", task_type=kind, title="T", sprint=SPRINT,
            **fields,
        )

    def test_each_refusal_is_decided_before_the_board_is_read(self) -> None:
        for kind, fields, reason in (
            ("operation", {}, "an operation card needs --touches-production <project>|none"),
            ("operation", {"touches_production": "relay"}, "unknown registered project: relay"),
            ("operation", {"touches_production": "../relay"}, "unknown registered project: ../relay"),
            ("decision", {"touches_production": "none"}, "a decision card takes none"),
            ("code", {"touches_production": "secretary"}, "a code card takes none"),
            ("research", {"touches_production": "none"}, "a research card takes none"),
        ):
            with self.subTest(kind=kind, fields=fields), self.assertRaisesRegex(TaskError, reason) as raised:
                self.create(kind, **fields)
            self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.mock_calls, [])

    def test_without_a_project_registry_a_named_production_is_refused(self) -> None:
        (Path(self.tmp) / "projects" / "secretary.yaml").unlink()
        (Path(self.tmp) / "projects").rmdir()
        with self.assertRaisesRegex(TaskError, "project registry is unavailable"):
            self.create("operation", touches_production="secretary")
        self.assertEqual(self.client.mock_calls, [])

    def test_a_registered_project_and_none_pass_to_the_board(self) -> None:
        for value in ("secretary", "none"):
            # Past the production check the create reads the sprint: the refusal is not ours.
            with (
                self.subTest(value=value),
                mock.patch("secretary.sprints.SprintReader.show", side_effect=RuntimeError("the board")),
                self.assertRaisesRegex(RuntimeError, "the board"),
            ):
                self.create("operation", touches_production=value)

    def test_the_cli_passes_the_flag_through(self) -> None:
        writer = mock.Mock()
        writer.return_value.create.return_value = {"action": "created"}
        with (
            mock.patch("secretary.task_commands.TaskWriter", writer),
            mock.patch("secretary.task_commands.card_client"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = main(
                ["task", "create", "--role", "observer", "--instance", self.tmp, "--data-dir", self.tmp,
                 "--project", "secretary", "--type", "operation", "--title", "T", "--sprint", SPRINT,
                 "--touches-production", "secretary"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(writer.return_value.create.call_args.kwargs["touches_production"], "secretary")

    def test_the_value_is_read_from_the_bag_and_shown_by_task_show_and_the_card_page(self) -> None:
        reader = TaskReader(mock.Mock())
        row = {"id": 1900, "reference": REF, "title": "Rotate the key", "column_id": 3, "is_active": 1}
        meta = {"task_type": "operation", "sprint_ref": SPRINT, "touches_production": "secretary"}

        shown = reader._normalize(row, {3: "Ready"}, {}, meta, comments=[])
        plain = reader._normalize(row, {3: "Ready"}, {}, {"task_type": "decision"}, comments=[])

        self.assertEqual(shown["touches_production"], "secretary")
        self.assertEqual(shown["extensions"]["extra"]["touches_production"], "secretary")
        self.assertNotIn("touches_production", plain)
        value = _card_value(shown)
        self.assertEqual(value["touches_production"], "secretary")
        self.assertIsNone(_card_value(plain)["touches_production"])
        html = pages.task(
            {"ref": REF, "card": {"source": None, "value": value}, "project": {}, "events": {}, "agents": {}},
            runs={},
        )
        self.assertIn("touches production", html)

    def test_a_malformed_value_is_no_value(self) -> None:
        for value in ("", "  ", "../etc", 7, None):
            with self.subTest(value=value):
                self.assertIsNone(touches_production({"extensions": {"extra": {"touches_production": value}}}))
        self.assertIsNone(touches_production({}))


class CardFactsTests(unittest.TestCase):
    def test_what_is_missing_or_malformed_is_named(self) -> None:
        good = card_facts(card_ref=REF, kind="operation", touches_production="relay", sprint_ref=SPRINT)
        self.assertEqual(facts_problem(good), "")
        self.assertEqual(
            card_facts(card_ref=REF, kind="decision", touches_production="relay", sprint_ref=SPRINT)[
                "touches_production"
            ],
            None,
        )
        for card_value, problem in (
            (None, "carries no card facts"),
            ({**good, "card_ref": ""}, "name no card_ref"),
            ({**good, "sprint_ref": None}, "name no sprint_ref"),
            ({**good, "kind": "code"}, "kind 'code'"),
            ({**good, "input": "other"}, "input 'other'"),
            ({**good, "touches_production": None}, f"operation card {REF} names no production"),
            ({**good, "kind": "decision"}, "a decision card names no production"),
        ):
            with self.subTest(card=card_value):
                self.assertIn(problem, facts_problem(card_value))
        # The owner's answer is not checked again, so it needs no production.
        self.assertEqual(facts_problem({**good, "touches_production": None, "input": OWNER_ANSWER_INPUT}), "")

    def test_the_refusal_reason(self) -> None:
        self.assertEqual(refusal_reason("relay", SPRINT, ()), NOT_ALLOWED)
        self.assertEqual(
            refusal_reason("relay", SPRINT, ("secretary", "site")),
            f"operation touches production relay; sprint {SPRINT} allows [secretary, site]",
        )


class RuleFixture(DispatcherFixture):
    def allow(self, *productions: str, session: str | None = None) -> None:
        self.po_sprints.records[SPRINT] = SprintRecord(SPRINT, "open", session, productions)

    def operation(self, production: str | None, **fields: Any) -> dict[str, Any]:
        return card("operation", production=production, description="Rotate the relay key.", **fields)

    def owner_says(self, text: str, event_id: str) -> None:
        self.cards.card["comments"].append({"created_at": "2026-09-26T16:00:00Z", "marker": "owner", "body": f"[owner]\n{text}"})
        self.cards.log.append(
            {"request_id": f"req-{event_id}", "ref": REF, "kind": "commented", "event_id": event_id,
             "payload": {"marker": "owner"}}
        )

    def handovers(self) -> list[dict[str, Any]]:
        return [event for event in self.cards.log if event["kind"] == HANDED_TO_OWNER]

    def handover_comments(self) -> list[str]:
        return [c["body"] for c in self.cards.card["comments"] if f"[{HANDOVER_MARKER}]" in c["body"]]


class AllowedTests(RuleFixture):
    def test_an_allowed_production_is_queued_and_runs_and_the_facts_bind_the_id(self) -> None:
        self.start()
        self.allow("relay")
        runtime = self.runtime(self.operation("relay"))

        submitted = self.claim(runtime)

        self.assertEqual(submitted["action"], "po-card-submitted")
        submission = self.record().po_submission
        facts = {"card_ref": REF, "kind": "operation", "touches_production": "relay", "sprint_ref": SPRINT,
                 "input": CARD_INPUT}
        self.assertEqual(submission.card, facts)
        self.assertFalse(submission.handed_over)
        session = submission.session_id
        self.assertEqual(self.settled(session, 2).state, po_store.COMPLETED)
        request = FakePoStore(self.board).request(submission.submit_request_id)
        self.assertEqual(request.fingerprint, po_store.send_fingerprint(session, submission.text, facts))
        self.assertNotEqual(request.fingerprint, po_store.send_fingerprint(session, submission.text))
        self.assertIn("Touches production: relay.", self.calls()[-1]["prompt"])
        self.assertEqual(self.handovers(), [])
        self.cards.complete_as_po("operation", OPERATION_BODY)
        self.assertEqual(self.tick(runtime)["action"], "po-card-closed")

    def test_none_is_queued_whatever_the_sprint_allows(self) -> None:
        self.start()
        runtime = self.runtime(self.operation("none"))

        self.assertEqual(self.claim(runtime)["action"], "po-card-submitted")

        submission = self.record().po_submission
        self.assertEqual(submission.card["touches_production"], "none")
        self.settled(submission.session_id, 2)
        self.assertIn("Touches production: none. Touch no production", self.calls()[-1]["prompt"])
        self.assertEqual(self.handovers(), [])

    def test_a_decision_carries_the_same_facts_with_no_production(self) -> None:
        self.start()
        runtime = self.runtime(card())

        self.assertEqual(self.claim(runtime)["action"], "po-card-submitted")

        self.assertEqual(
            self.record().po_submission.card,
            {"card_ref": REF, "kind": "decision", "touches_production": None, "sprint_ref": SPRINT,
             "input": CARD_INPUT},
        )
        self.assertEqual(self.handovers(), [])

    def test_the_facts_survive_the_dispatcher_record(self) -> None:
        self.start()
        runtime = self.runtime(self.operation("none"))
        self.claim(runtime)
        record = self.record()
        loaded = DispatcherRecord.from_json(record.to_json())
        self.assertEqual(loaded.po_submission, record.po_submission)


class NotAllowedTests(RuleFixture):
    def test_a_production_the_sprint_does_not_allow_is_handed_to_the_owner_and_not_queued(self) -> None:
        self.start()
        runtime = self.runtime(self.operation("relay"))

        outcome = self.claim(runtime)

        self.assertEqual((outcome["status"], outcome["action"]), ("ok", "po-card-handed-over"))
        self.assertEqual(outcome["reason"], NOT_ALLOWED)
        submission = self.record().po_submission
        self.assertTrue(submission.submitted and submission.handed_over)
        self.assertEqual(self.cards.card["state"], "in_progress")
        mark = waiting_owner(self.cards.card)
        self.assertEqual((mark["reason"], mark["by"]), (NOT_ALLOWED, "po-service"))
        self.assertEqual(self.handover_comments(), [f"[po]\n[{HANDOVER_MARKER}]\n\n{NOT_ALLOWED}\n"])
        [handover] = self.handovers()
        self.assertEqual(
            (handover["request_id"], handover["role"], handover["actor"]),
            (f"{submission.submit_request_id}:handover", "po", "po-service"),
        )
        # Nothing was queued and no turn exists for the card: the session holds its seed only.
        self.assertIsNone(PoQueue(self.data).find(submission.submit_request_id))
        self.assertIsNone(FakePoStore(self.board).request(submission.submit_request_id))
        self.settled(submission.session_id, 1)
        self.assertEqual(len(FakePoStore(self.board).turns(submission.session_id)), 1)
        # And the dispatcher waits for the owner, tick after tick.
        for _ in range(2):
            self.assertEqual(self.tick(runtime)["action"], "po-card-waiting-owner")
        self.assertEqual(len(self.handovers()), 1)

    def test_the_owners_answer_is_queued_without_a_recheck_and_the_po_completes(self) -> None:
        self.start()
        runtime = self.runtime(self.operation("relay"))
        self.claim(runtime)
        session = self.record().po_submission.session_id
        self.settled(session, 1)
        self.owner_says("Go ahead on relay, once.", "evt-owner-1")

        answered = self.tick(runtime)

        # The sprint still allows nothing: the owner decided, so the follow-up is not checked again.
        self.assertEqual(self.po_sprints.records[SPRINT].allowed_productions, ())
        request_id = owner_answer_request_id(REF, "evt-owner-1")
        self.assertEqual((answered["action"], answered["po_request_id"]), ("po-owner-answer-submitted", request_id))
        self.assertEqual(self.settled(session, 2).state, po_store.COMPLETED)
        submission = self.record().po_submission
        request = FakePoStore(self.board).request(request_id)
        facts = {**submission.card, "input": OWNER_ANSWER_INPUT}
        self.assertEqual(request.fingerprint, po_store.send_fingerprint(session, submission.owner_text, facts))
        prompt = self.calls()[-1]["prompt"]
        self.assertEqual(prompt, submission.owner_text)
        for expected in ("The PO service handed it to the owner", "Rotate the relay key.", NOT_ALLOWED,
                         "Go ahead on relay, once.", "Touches production: relay.",
                         complete_command(REF, "operation", submission.complete_request_id)):
            self.assertIn(expected, prompt)
        self.assertEqual(len(self.handovers()), 1)

        self.cards.complete_as_po("operation", OPERATION_BODY)
        self.assertIsNone(waiting_owner(self.cards.card))
        self.assertEqual(self.tick(runtime)["action"], "po-card-closed")
        self.assertNotIn(REF, self.records)

    def test_a_repeat_of_the_same_submit_hands_over_once(self) -> None:
        self.start()
        runtime = self.runtime(self.operation("relay"))
        real = runtime.po
        answers: list[dict[str, Any]] = []

        class LosesTheFirstAnswer:
            def submit(self, **fields: Any) -> dict[str, Any]:
                answers.append(real.submit(**fields))
                if len(answers) == 1:
                    raise OutcomeUnknown("no answer from the PO service: connection reset")
                return answers[-1]

            def __getattr__(self, name: str) -> Any:
                return getattr(real, name)

        runtime.po = LosesTheFirstAnswer()

        first = self.claim(runtime)
        second = self.tick(runtime)
        self.records.clear()  # and the dispatcher's record is lost: rebuilt from the claim, same ids
        third = self.tick(runtime)

        self.assertEqual(
            [first["action"], second["action"], third["action"]],
            ["po-service-unanswered", "po-card-handed-over", "po-card-handed-over"],
        )
        self.assertEqual([answer["repeated"] for answer in answers], [False, True, True])
        self.assertEqual(len(self.handovers()), 1)
        self.assertEqual(len(self.handover_comments()), 1)
        self.assertEqual(self.tick(runtime)["action"], "po-card-waiting-owner")


class NeverExecutedTests(RuleFixture):
    def test_a_sprint_that_cannot_be_read_is_never_executed(self) -> None:
        self.start()
        self.allow("relay")
        runtime = self.runtime(self.operation("relay"))
        sprints = self.po_sprints

        class FailsTheSprintReadAtSubmit(ServicePoChannel):
            def submit(self, **fields: Any) -> dict[str, Any]:
                sprints.fail["sprint"] = 1
                return super().submit(**fields)

        channel = FailsTheSprintReadAtSubmit(self.data, None)
        channel._store = FakePoStore(self.board)
        runtime.po = channel

        outcomes = [self.claim(runtime), self.tick(runtime)]

        for outcome in outcomes:
            self.assertEqual((outcome["status"], outcome["action"]), ("degraded", "po-service-unanswered"))
        self.assertIn(f"sprint {SPRINT} cannot be read for its allowed productions", outcomes[-1]["reason"])
        submission = self.record().po_submission
        self.assertFalse(submission.submitted)
        self.assertEqual(self.cards.card["state"], "in_progress")
        self.assertIsNone(PoQueue(self.data).find(submission.submit_request_id))
        self.assertIsNone(FakePoStore(self.board).request(submission.submit_request_id))
        self.assertEqual(self.handovers(), [])
        # Once the sprint reads again, the same request id is checked and runs.
        runtime.po = ServicePoChannel(self.data, None)
        runtime.po._store = FakePoStore(self.board)
        self.assertEqual(self.tick(runtime)["action"], "po-card-submitted")
        self.settled(submission.session_id, 2)

    def test_an_operation_that_names_no_production_is_never_executed(self) -> None:
        self.start()
        self.allow("relay")
        runtime = self.runtime(self.operation(None))

        outcomes = [self.claim(runtime), self.tick(runtime)]

        for outcome in outcomes:
            self.assertEqual(outcome["action"], "po-service-unanswered")
        self.assertIn(f"operation card {REF} names no production it touches", outcomes[-1]["reason"])
        submission = self.record().po_submission
        self.assertIsNone(PoQueue(self.data).find(submission.submit_request_id))
        self.assertIsNone(FakePoStore(self.board).request(submission.submit_request_id))
        self.assertEqual(self.handovers(), [])

    def test_card_facts_come_from_the_dispatcher_only_and_it_always_sends_them(self) -> None:
        service = self.start(listen=False)
        session = self.session(service)
        text = "Rotate the relay key."
        facts = card_facts(card_ref=REF, kind="operation", touches_production="none", sprint_ref=SPRINT)
        web = service.handle(
            {"op": "submit", "session_id": session, "text": text, "request_id": "w-1", "source": "web", "card": facts}
        )
        bare = service.handle(
            {"op": "submit", "session_id": session, "text": text, "request_id": "d-1", "source": "dispatcher"}
        )
        self.assertEqual((web["error"]["code"], web["error"].get("nothing_written")), ("validation", True))
        self.assertEqual(bare["error"]["code"], "unavailable")
        self.assertIn("carries no card facts", bare["error"]["message"])
        self.assertEqual(PoQueue(self.data).pending(), [])

    def test_a_handover_the_board_refuses_refuses_the_submit(self) -> None:
        service = self.start(listen=False)
        session = self.session(service)
        self.po_sprints.cards = mock.Mock(
            handover=mock.Mock(side_effect=HandoverRefused("transition_forbidden", f"{REF} is blocked"))
        )
        facts = card_facts(card_ref=REF, kind="operation", touches_production="relay", sprint_ref=SPRINT)
        answer = service.handle(
            {"op": "submit", "session_id": session, "text": "x", "request_id": "d-2", "source": "dispatcher",
             "card": facts}
        )
        self.assertEqual((answer["error"]["code"], answer["error"].get("nothing_written")), ("validation", True))
        self.assertIn(f"{REF} could not be handed to the owner", answer["error"]["message"])
        # A handover that may have committed is a repeat, never a refusal.
        self.po_sprints.cards = mock.Mock(handover=mock.Mock(side_effect=RuntimeError("connection reset")))
        answer = service.handle(
            {"op": "submit", "session_id": session, "text": "x", "request_id": "d-3", "source": "dispatcher",
             "card": facts}
        )
        self.assertEqual(answer["error"]["code"], "outcome_unknown")
        self.assertEqual(PoQueue(self.data).pending(), [])


class BoardHandoverTests(unittest.TestCase):
    """`BoardSprintSessions.hand_over` is `task handover` as role po, actor po-service, under the given id."""

    def test_it_runs_the_task_handover_and_maps_a_definite_refusal(self) -> None:
        writer = mock.Mock()
        writer.return_value.handover.return_value = {"action": HANDED_TO_OWNER, "replayed": True}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("secretary.tasks.TaskWriter", writer),
            mock.patch("secretary.board.backend.card_client"),
        ):
            port = BoardSprintSessions(tmp, tmp)
            self.assertTrue(port.hand_over(REF, NOT_ALLOWED, request_id="s-1:handover"))
            writer.return_value.handover.assert_called_once_with(
                role="po", actor="po-service", reference=REF, to="owner", reason=NOT_ALLOWED,
                request_id="s-1:handover",
            )
            for code, raised in (("already_handed_over", HandoverRefused), ("backend_unavailable", TaskError)):
                writer.return_value.handover.side_effect = TaskError(code, "no", 3)
                with self.subTest(code=code), self.assertRaises(raised):
                    port.hand_over(REF, NOT_ALLOWED, request_id="s-1:handover")

    def test_the_sprint_record_carries_its_allowed_productions(self) -> None:
        document = {"ref": SPRINT, "status": "open", "po_session": None, "allowed_productions": ["relay"]}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("secretary.sprints.SprintReader.show", return_value=document),
            mock.patch("secretary.board.backend.board_client"),
        ):
            record = BoardSprintSessions(tmp, tmp).sprint(SPRINT)
        self.assertEqual(record, SprintRecord(SPRINT, "open", None, ("relay",)))


if __name__ == "__main__":
    unittest.main()
