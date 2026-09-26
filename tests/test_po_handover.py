"""A decision/operation card handed to the owner, the owner's answer, and the sprint reading `waiting`.

Unit-level (secretary-1761), on the fakes of cards 1 to 3. `task handover` and `task comment --role
owner` run `TaskWriter` over a mock client whose writes land on one in-memory card, with an in-memory
audit that keeps the claim rules of `SqlTaskAudit`. The dispatcher side runs the real
`advance_po_card` with a real `PoService` over its socket (`tests.test_po_cards.DispatcherFixture`).
The PostgreSQL paths (the handover and the completion each in one transaction) are covered by the
integration-board suite (`tests/test_tasks.py`, `tests/test_web_sprint_protocol.py`).
"""

from __future__ import annotations

import contextlib
import copy
import io
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest import mock

from secretary.board.owner_handover import (
    HANDED_TO_OWNER,
    MARK_KEYS,
    mark_values,
    owner_answer_event_ids,
    owner_comments_since_handover,
    render_handover_comment,
    waiting_owner,
)
from secretary.cli import main
from secretary.dispatch.po_cards import (
    PO_BLOCKED_ACTION,
    ServicePoChannel,
    advance_po_card,
    complete_command,
    handover_command,
    owner_answer_request_id,
)
from secretary.dispatch.state import attempt_request_id
from secretary.po import store as po_store
from secretary.po.queue import PoQueue
from secretary.po.sprints import SprintRecord
from secretary.tasks import TaskError, TaskReader, TaskWriter
from secretary.web import pages
from secretary.webproto import sources
from secretary.webproto.reads import _card_value
from secretary.webproto.section import Reading, SourceSet, render
from secretary.webproto.sprint_reads import (
    SECTIONS,
    SOURCE_CARDS,
    SOURCE_LIVENESS,
    SOURCE_SPRINTS,
    WAITING_BLOCKED,
    WAITING_WAITING,
    _Production,
)
from tests.po_card_fakes import DECISION_BODY, REF, SPRINT, DispatcherFixture, card
from tests.po_fake_store import FakePoStore
from tests.po_handover_fakes import (
    REASON,
    SINCE,
    HandedOverFixture,
    MemoryAudit,
    OneCardClient,
    decision_card,
)


class WriterFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.card = decision_card()
        self.client = OneCardClient(self.card, tmp)
        self.writer = TaskWriter(self.client, data_dir=tmp)  # type: ignore[arg-type]
        self.writer.audit = MemoryAudit()
        self.writer.reader = mock.Mock(show=lambda reference: copy.deepcopy(self.card))
        patcher = mock.patch("secretary.tasks._task_number", return_value=1900)
        patcher.start()
        self.addCleanup(patcher.stop)

    def hand_over(self, **fields: Any) -> dict[str, Any]:
        call = {"role": "po", "actor": "po", "reference": REF, "to": "owner", "reason": REASON,
                "request_id": "handover-1", **fields}
        return self.writer.handover(**call)


class HandoverTests(WriterFixture):
    def test_it_marks_the_card_writes_the_comment_and_the_audit_fact_and_leaves_it_in_progress(self) -> None:
        answer = self.hand_over()

        self.assertEqual((answer["action"], answer["replayed"]), (HANDED_TO_OWNER, False))
        self.assertEqual(self.card["state"], "in_progress")
        mark = waiting_owner(self.card)
        self.assertEqual((mark["reason"], mark["by"]), (REASON, "po"))
        datetime.fromisoformat(mark["since"])
        [comment] = self.card["comments"]
        self.assertEqual(comment["body"], "[po]\n" + render_handover_comment(REASON))
        self.assertEqual(comment["body"].splitlines()[1], "[handover:owner]")
        [event] = self.writer.audit.events(REF)
        self.assertEqual((event["kind"], event["ref"], event["request_id"]), (HANDED_TO_OWNER, REF, "handover-1"))
        self.assertEqual(event["actor"], {"role": "po", "id": "po"})
        self.assertEqual(
            {key: event["payload"][key] for key in ("to", "kind", "sprint", "waiting_owner")},
            {"to": "owner", "kind": "decision", "sprint": SPRINT, "waiting_owner": mark["since"]},
        )
        # The mark is written only through the three validated fields, and never by a transition.
        [(_method, params)] = [write for write in self.client.writes if write[0] == "saveTaskMetadata"]
        self.assertEqual(set(params["values"]), set(MARK_KEYS))

    def test_a_repeat_under_the_same_id_writes_nothing_and_another_reason_is_refused(self) -> None:
        first = self.hand_over()
        writes = list(self.client.writes)

        again = self.hand_over()

        self.assertEqual((again["replayed"], again["event_id"]), (True, first["event_id"]))
        self.assertEqual(self.client.writes, writes)
        with self.assertRaises(TaskError) as raised:
            self.hand_over(reason="Something else entirely.")
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.writes, writes)

    def test_each_refusal_writes_nothing(self) -> None:
        for fields, code in (
            ({"role": "observer"}, "role_forbidden"),
            ({"role": "worker"}, "role_forbidden"),
            ({"to": "observer"}, "validation"),
            ({"reason": "   "}, "validation"),
        ):
            with self.subTest(fields=fields), self.assertRaises(TaskError) as raised:
                self.hand_over(request_id=f"refused-{code}-{len(fields)}", **fields)
            self.assertEqual(raised.exception.code, code)
        for document, code in (
            (decision_card(kind="code"), "validation"),
            (decision_card(kind="research"), "validation"),
            (decision_card(state="ready"), "transition_forbidden"),
            (decision_card(state="blocked", kind="operation"), "transition_forbidden"),
            (decision_card(state="done"), "transition_forbidden"),
        ):
            self.card.clear()
            self.card.update(document)
            with self.subTest(kind=document["type"], state=document["state"]), self.assertRaises(TaskError) as raised:
                self.hand_over(request_id=f"refused-{document['type']}-{document['state']}")
            self.assertEqual(raised.exception.code, code)
        self.assertEqual((self.client.writes, self.writer.audit.events()), ([], []))

    def test_a_card_already_handed_over_is_refused_with_its_mark(self) -> None:
        self.hand_over()
        writes = list(self.client.writes)
        with self.assertRaises(TaskError) as raised:
            self.hand_over(request_id="handover-2", reason="A second reason.")
        self.assertEqual(raised.exception.code, "already_handed_over")
        self.assertIn(REASON, raised.exception.message)
        self.assertEqual(self.client.writes, writes)
        self.assertEqual(len(self.writer.audit.events()), 1)

    def test_the_mark_is_visible_in_task_show_and_list_and_on_the_card_page(self) -> None:
        reader = TaskReader(mock.Mock())
        columns, swimlanes = {3: "In progress"}, {}
        row = {"id": 1900, "reference": REF, "title": "The decision", "column_id": 3, "is_active": 1}
        meta = {"task_type": "decision", "sprint_ref": SPRINT, **mark_values(SINCE, REASON, "po")}

        shown = reader._normalize(row, columns, swimlanes, meta, comments=[])
        plain = reader._normalize(row, columns, swimlanes, {"task_type": "decision"}, comments=[])

        self.assertEqual(shown["waiting_owner"], {"since": SINCE, "reason": REASON, "by": "po"})
        self.assertNotIn("waiting_owner", plain)
        value = _card_value(shown)
        self.assertEqual(value["waiting_owner"]["reason"], REASON)
        self.assertIsNone(_card_value(plain)["waiting_owner"])
        html = pages.task(
            {"ref": REF, "card": {"source": None, "value": value}, "project": {}, "events": {}, "agents": {}},
            runs={},
        )
        self.assertIn("waiting for the owner", html)
        self.assertIn("handed to the owner", html)
        self.assertIn("Pay the relay provider", html)

    def test_a_partial_or_malformed_mark_is_no_mark(self) -> None:
        for bag in (
            {"waiting_owner": SINCE},
            {**mark_values(SINCE, REASON, "po"), "waiting_owner_by": ""},
            mark_values("yesterday", REASON, "po"),
        ):
            with self.subTest(bag=bag):
                self.assertIsNone(waiting_owner({"extensions": {"extra": bag}}))


class OwnerCommentTests(WriterFixture):
    def test_the_owner_comments_on_any_card_as_the_owner(self) -> None:
        for document in (decision_card(), decision_card(kind="code", state="done")):
            self.card.clear()
            self.card.update(document)
            with self.subTest(kind=document["type"]):
                answer = self.writer.comment(
                    role="owner", actor="somebody", reference=REF, body="Yes, use the company card.",
                    request_id=f"owner-{document['type']}",
                )
                self.assertEqual(answer["action"], "commented")
                self.assertEqual(self.card["comments"][-1]["body"], "[owner]\nYes, use the company card.")
                self.assertEqual(self.card["comments"][-1]["marker"], "owner")
                event = self.writer.audit.committed_event(f"owner-{document['type']}")
                self.assertEqual((event["actor"], event["payload"]["marker"]), ({"role": "owner", "id": "owner"}, "owner"))

    def test_the_owner_role_is_a_comment_role_only(self) -> None:
        for call in (
            lambda: self.writer.move(role="owner", actor="owner", reference=REF, target="done", reason="x"),
            lambda: self.writer.handover(role="owner", actor="owner", reference=REF, to="owner", reason="x"),
        ):
            with self.assertRaises(TaskError) as raised:
                call()
            self.assertEqual(raised.exception.code, "role_forbidden")

    def test_the_cli_takes_owner_on_comment_only_and_passes_handover_through(self) -> None:
        writer = mock.Mock()
        writer.return_value.comment.return_value = {"action": "commented"}
        writer.return_value.handover.return_value = {"action": HANDED_TO_OWNER}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("secretary.task_commands.TaskWriter", writer),
            mock.patch("secretary.task_commands.card_client"),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            body = f"{tmp}/body.md"
            with open(body, "w", encoding="utf-8") as handle:
                handle.write(REASON)
            base = ["--instance", tmp, "--data-dir", tmp]
            codes = [
                main(["task", "comment", "--ref", REF, "--role", "owner", "--body-file", body, *base]),
                main(["task", "report", "--ref", REF, "--role", "owner", "--kind", "done", "--body-file", body, *base]),
                main(["task", "handover", "--ref", REF, "--role", "po", "--to", "owner", "--reason", REASON,
                      "--request-id", "h-1", *base]),
                main(["task", "handover", "--ref", REF, "--role", "po", "--to", "owner", "--reason-file", body, *base]),
                main(["task", "handover", "--ref", REF, "--role", "observer", "--to", "owner", "--reason", "x", *base]),
                main(["task", "handover", "--ref", REF, "--role", "po", "--to", "owner", *base]),
            ]
        self.assertEqual(codes, [0, 2, 0, 0, 2, 2])
        self.assertEqual(writer.return_value.comment.call_args.kwargs["role"], "owner")
        first, second = (call.kwargs for call in writer.return_value.handover.call_args_list)
        self.assertEqual(
            (first["reference"], first["to"], first["reason"], first["request_id"], first["role"]),
            (REF, "owner", REASON, "h-1", "po"),
        )
        self.assertEqual((second["reason"], second["request_id"]), (REASON, None))


class CompletionClearsTheMarkTests(WriterFixture):
    """`task complete` takes the mark off inside the transition's own transaction (its `finish`)."""

    def setUp(self) -> None:
        super().setUp()
        self.events: dict[str, Any] = {}
        self.writer._typed_event = lambda request_id: self.events.get(request_id)  # type: ignore[method-assign]

        def transition(**fields: Any) -> Any:
            fields["finish"](None)
            self.card["state"] = fields["target"].value
            self.events[fields["request_id"]] = SimpleNamespace(
                ref=fields["reference"], reason=fields["reason"], source_state="in_progress"
            )
            return SimpleNamespace(event=SimpleNamespace(event_id="evt-done"))

        self.writer._transition_card = transition  # type: ignore[method-assign]

    def complete(self) -> dict[str, Any]:
        return self.writer.complete(
            role="po", actor="po", reference=REF, kind="decision", body=DECISION_BODY, request_id="complete-1"
        )

    def test_completion_clears_the_mark_with_the_done(self) -> None:
        self.hand_over()
        self.assertIsNotNone(waiting_owner(self.card))

        self.complete()

        self.assertEqual(self.card["state"], "done")
        self.assertIsNone(waiting_owner(self.card))
        self.assertFalse(set(MARK_KEYS) & set(self.card["extensions"]["extra"]))

    def test_an_unmarked_completion_writes_no_mark_fields(self) -> None:
        self.complete()
        [values] = [params["values"] for method, params in self.client.writes if method == "saveTaskMetadata"]
        self.assertFalse(set(MARK_KEYS) & set(values))

    def test_any_move_out_of_in_progress_takes_the_mark_off(self) -> None:
        self.hand_over()
        writer = self.writer
        writer._reset_transition_metadata(copy.deepcopy(self.card), source="in_progress", target="blocked")
        self.assertIsNone(waiting_owner(self.card))


class OwnerAnswerDispatchTests(HandedOverFixture):
    """The dispatcher waits on a handed-over card, and hands each owner answer to the PO once."""

    def test_a_settled_turn_with_the_card_handed_over_waits_instead_of_blocking(self) -> None:
        runtime, _session = self.submitted_card()
        before = runtime.reader.show(REF)  # the tick's read, taken before the PO's handover landed
        self.hand_over()

        outcome = advance_po_card(runtime, before, self.records, self.payload, self.attempt)

        self.assertEqual((outcome["status"], outcome["action"]), ("ok", "po-card-waiting-owner"))
        self.assertIn(REASON, outcome["reason"])
        self.assertEqual(self.cards.card["state"], "in_progress")
        self.assertIn(REF, self.records)
        self.assertEqual([event["kind"] for event in self.cards.log], ["claim", HANDED_TO_OWNER])
        # And it keeps waiting, tick after tick, with nothing submitted.
        self.assertEqual(self.tick(runtime)["action"], "po-card-waiting-owner")
        self.assertEqual(self.record().po_submission.owner_request_id, "")

    def test_the_card_input_quotes_the_handover_command_with_its_derived_id(self) -> None:
        self.submitted_card()
        submission = self.record().po_submission
        self.assertEqual(submission.handover_request_id, attempt_request_id(self.attempt, "po-handover", REF))
        self.assertIn(handover_command(REF, submission.handover_request_id), submission.text)
        self.assertEqual(
            handover_command(REF, "h-1"),
            f"python3 -P -m secretary task handover --ref {REF} --role po --to owner --reason-file <file> "
            "--request-id h-1",
        )
        self.assertIn("unless you handed it to the owner in this turn", submission.text)

    def test_the_same_settled_turn_without_the_mark_blocks(self) -> None:
        runtime, session = self.submitted_card()

        outcome = self.tick(runtime)

        self.assertEqual((outcome["status"], outcome["action"]), ("blocked", PO_BLOCKED_ACTION))
        self.assertEqual(outcome["reason"], f"PO turn {session}/2 ended completed without completing the card")

    def test_each_owner_comment_becomes_one_follow_up_to_the_same_session(self) -> None:
        runtime, session = self.submitted_card()
        self.hand_over()
        self.owner_says("Use the company card, the budget allows it.", "evt-owner-1")

        submitted = self.tick(runtime)

        expected_id = owner_answer_request_id(REF, "evt-owner-1")
        self.assertEqual((submitted["action"], submitted["po_request_id"]), ("po-owner-answer-submitted", expected_id))
        self.assertEqual(expected_id, f"dispatcher-po-owner-answer-{REF}-evt-owner-1")
        self.assertEqual(self.settled(session, 3).state, po_store.COMPLETED)
        request = FakePoStore(self.board).request(expected_id)
        self.assertEqual((request.session_id, request.seq), (session, 3))
        prompt = self.calls()[-1]["prompt"]
        submission = self.record().po_submission
        self.assertEqual(prompt, submission.owner_text)
        for expected in (REF, "decision", REASON, "Use the company card, the budget allows it.",
                         complete_command(REF, "decision", submission.complete_request_id)):
            self.assertIn(expected, prompt)
        self.assertNotIn("[handover:owner]", prompt)

        # The follow-up turn settled and the card is still handed over: it waits, and nothing repeats.
        for _ in range(2):
            self.assertEqual(self.tick(runtime)["action"], "po-card-owner-answered")
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 3)

        # A second owner comment: one more follow-up, carrying both comments in order.
        self.owner_says("And cap it at the monthly plan.", "evt-owner-2")
        self.assertEqual(self.tick(runtime)["action"], "po-owner-answer-submitted")
        self.settled(session, 4)
        prompt = self.calls()[-1]["prompt"]
        first, second = (prompt.index(text) for text in ("Use the company card", "And cap it"))
        self.assertLess(first, second)
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 4)

        # The PO completes the card: the record closes.
        self.cards.complete_as_po("decision", DECISION_BODY)
        self.assertEqual(self.tick(runtime)["action"], "po-card-closed")
        self.assertNotIn(REF, self.records)

    def test_a_repeat_with_a_lost_answer_or_a_lost_record_never_duplicates_the_follow_up(self) -> None:
        runtime, session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-1")
        real_submit = runtime.po.submit
        seen: list[str] = []

        def loses_the_answer(**fields: Any) -> dict[str, Any]:
            seen.append(fields["request_id"])
            real_submit(**fields)
            if len(seen) == 1:
                from secretary.po.client import OutcomeUnknown

                raise OutcomeUnknown("no answer from the PO service: connection reset")
            return {}

        with mock.patch.object(runtime.po, "submit", side_effect=loses_the_answer):
            self.assertEqual(self.tick(runtime)["action"], "po-service-unanswered")
            self.assertEqual(self.tick(runtime)["action"], "po-owner-answer-submitted")
        self.settled(session, 3)
        self.records.clear()  # the dispatcher's state file is lost with its record

        rebuilt = [self.tick(runtime)["action"], self.tick(runtime)["action"], self.tick(runtime)["action"]]

        self.assertEqual(rebuilt, ["po-card-submitted", "po-owner-answer-submitted", "po-card-owner-answered"])
        self.assertEqual(seen, [owner_answer_request_id(REF, "evt-owner-1")] * 2)
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 3)

    def test_comments_before_the_handover_and_from_other_roles_are_not_the_owners_answer(self) -> None:
        comments = [
            {"marker": "owner", "body": "[owner]\nAn old remark."},
            {"marker": "po", "body": "[po]\n" + render_handover_comment(REASON)},
            {"marker": "observer", "body": "[observer]\nNoted."},
            {"marker": "owner", "body": "[owner]\nThe answer.", "created_at": SINCE},
        ]
        self.assertEqual(owner_comments_since_handover(comments), [{"created_at": SINCE, "body": "The answer."}])
        events = [
            {"kind": "commented", "event_id": "e1", "payload": {"marker": "owner"}},
            {"kind": HANDED_TO_OWNER, "event_id": "e2"},
            {"kind": "commented", "event_id": "e3", "payload": {"marker": "po"}},
            {"kind": "commented", "event_id": "e4", "payload": {"marker": "owner"}},
        ]
        self.assertEqual(owner_answer_event_ids(events), ["e4"])


class SetAsideInputTests(DispatcherFixture):
    """5a: an input the PO service set aside in `po-queue/refused/` Blocks the card, with its reason."""

    def test_a_set_aside_input_blocks_the_card_with_the_services_reason(self) -> None:
        self.start()
        runtime = self.runtime(card())
        queue = PoQueue(self.data)

        class SetsItAside(ServicePoChannel):
            def submit(self, **fields: Any) -> dict[str, Any]:
                item = queue.put(source="dispatcher", **{k: fields[k] for k in ("session_id", "text", "request_id")})
                queue.refuse(item, "PO session s-1 is closed; open a new session to continue")
                return {"session_id": fields["session_id"], "queued": True, "seq": None}

        channel = SetsItAside(self.data, None)
        channel._store = FakePoStore(self.board)
        channel._client = runtime.po._service()
        runtime.po = channel

        submitted = self.claim(runtime)
        blocked = self.tick(runtime)

        self.assertEqual(submitted["action"], "po-card-submitted")
        self.assertEqual((blocked["status"], blocked["action"]), ("blocked", PO_BLOCKED_ACTION))
        self.assertEqual(
            blocked["reason"],
            "the PO service set the card's input aside and will not run it: "
            "PO session s-1 is closed; open a new session to continue",
        )
        self.assertEqual(self.cards.card["state"], "blocked")


class SetAsideOwnerAnswerTests(HandedOverFixture):
    """The same for the follow-up: an owner answer the service set aside Blocks the card, with its reason."""

    def test_a_set_aside_follow_up_blocks_the_card(self) -> None:
        runtime, session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-1")
        self.assertEqual(self.tick(runtime)["action"], "po-owner-answer-submitted")
        self.settled(session, 3)
        request_id = owner_answer_request_id(REF, "evt-owner-1")
        with (
            mock.patch.object(runtime.po, "request", return_value=None),
            mock.patch.object(runtime.po, "refused", return_value={"reason": "PO session closed"}) as refused,
        ):
            blocked = self.tick(runtime)
        refused.assert_called_once_with(request_id)
        self.assertEqual((blocked["status"], blocked["action"]), ("blocked", PO_BLOCKED_ACTION))
        self.assertEqual(
            blocked["reason"], "the PO service set the owner's answer aside and will not run it: PO session closed"
        )


class RebuiltRecordTests(DispatcherFixture):
    """5b: a rebuilt record whose input is still queued for the same session counts it as submitted."""

    def test_a_rebuilt_record_with_its_input_still_queued_is_submitted_not_blocked(self) -> None:
        service = self.start()
        session = self.session(service)
        self.po_sprints.records[SPRINT] = SprintRecord(SPRINT, "open", session)
        service.submit(session_id=session, text="GATE the owner's long question", request_id="owner-1")
        self.reached_gate(session, 1)
        runtime = self.runtime(card(), comments=["The sprint opened."])
        self.claim(runtime)
        submit_id = self.record().po_submission.submit_request_id
        self.assertIsNotNone(PoQueue(self.data).find(submit_id))

        self.records.clear()
        runtime.sprints.comments.append("A comment that landed after the submit.")
        rebuilt = self.tick(runtime)

        self.assertEqual((rebuilt["status"], rebuilt["action"]), ("ok", "po-card-submitted"))
        self.assertEqual(self.cards.card["state"], "in_progress")
        self.assertEqual([item.request_id for item in PoQueue(self.data).pending(session)], [submit_id])
        self.assertEqual(self.tick(runtime)["action"], "po-card-queued")
        self.gate.touch()
        self.settled(session, 2)


class SprintWaitingTests(unittest.TestCase):
    """4: a sprint whose current card is an open decision/operation card reads `waiting`, with a pointer."""

    def waiting(self, current: dict[str, Any] | None, records: dict[str, Any] | None = None) -> dict[str, Any]:
        now = 1_000_000.0
        row = {"ref": SPRINT, "status": "open", "current_task": REF if current is not None else None}
        read = SourceSet(
            [
                Reading(SOURCE_SPRINTS, sources.available(now), (row, row)),
                Reading(SOURCE_CARDS, sources.available(now), {SPRINT: [current] if current else []}),
                Reading(SOURCE_LIVENESS, sources.available(now), _Production({"records": records or {}}, {})),
            ]
        )
        return render(SECTIONS.waiting(read))

    def test_with_the_po_and_handed_to_the_owner(self) -> None:
        # The dispatcher holds its record for the card: a head-less card is still not `working`.
        records = {REF: {"state": "po_submitted"}}
        for kind in ("decision", "operation"):
            with self.subTest(kind=kind):
                with_po = self.waiting(card(kind, state="in_progress"), records)
                self.assertEqual(
                    (with_po["state"], with_po["reason"], with_po["card"]),
                    (WAITING_WAITING, f"{REF} ({kind}) is with the PO", REF),
                )
                handed = card(kind, state="in_progress")
                handed["extensions"] = {"extra": mark_values(SINCE, REASON, "po")}
                with_owner = self.waiting(handed, records)
                self.assertEqual(
                    (with_owner["state"], with_owner["reason"], with_owner["card"]),
                    (WAITING_WAITING, f"{REF} ({kind}) is handed to the owner: {REASON}", REF),
                )
                self.assertEqual(with_owner["source"]["name"], SOURCE_CARDS)

    def test_other_cards_and_columns_keep_their_reading(self) -> None:
        blocked = self.waiting(card(state="blocked"), {})
        self.assertEqual((blocked["state"], blocked["card"]), (WAITING_BLOCKED, REF))
        code = self.waiting({**card(state="in_progress"), "type": "code"}, {REF: {"state": "claimed"}})
        self.assertEqual((code["state"], code["card"]), ("working", REF))
        nothing = self.waiting(None)
        self.assertEqual((nothing["state"], nothing["card"]), (WAITING_WAITING, None))

    def test_the_dashboard_row_says_it_with_the_card(self) -> None:
        item = {
            "ref": SPRINT,
            "goal": "g",
            "waiting": {"state": "waiting", "reason": f"{REF} (decision) is handed to the owner: {REASON}", "card": REF},
        }
        html = pages._compact_sprint_card(item)
        self.assertIn("is handed to the owner: Pay the relay provider", html)
        self.assertIn(f'href="/tasks/{REF}"', html)


if __name__ == "__main__":
    unittest.main()
