"""Contract tests for the additive, backend-neutral board protocol."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from secretary.board import (
    Actor, BoardEventCanon, BoardEventPending, BoardProtocolError, Card, CardState,
    Create, EntityKind, Event, EventKind, FakeBoardHost, InvalidTransition,
    KanboardBoardHost, MutationEventTransaction, RelatedRefs, Replace, TRANSITIONS,
    TransitionRequest,
)
from secretary.board.card_transitions import CARD_TRANSITIONS, CardTransitionForbidden, card_transition


class BoardHostContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeBoardHost()
        self.actor = Actor("worker", "worker-1417", "head-run:1417")

    def test_create_read_and_transition_return_normalized_event_result(self) -> None:
        created = self.host.create(Create(
            Card("secretary-1417", "Protocol seam", CardState.READY, sprint_ref="sprint:943"),
            self.actor, "accepted into the sprint", RelatedRefs(("sprint:943",)),
        ))

        result = self.host.transition(TransitionRequest(
            EntityKind.CARD, "secretary-1417", CardState.IN_PROGRESS, self.actor,
            "worker started", RelatedRefs(("sprint:943", "head-run:1417")),
        ))

        self.assertEqual(created.event.actor.head_run_ref, "head-run:1417")
        self.assertEqual(result.entity, self.host.read(EntityKind.CARD, "secretary-1417"))
        self.assertEqual(result.entity.state, CardState.IN_PROGRESS)
        self.assertEqual(result.event.kind, EventKind.CARD_STARTED)
        self.assertEqual(result.event.related_refs.refs, ("sprint:943", "head-run:1417"))

    def test_same_state_card_transition_is_rejected_by_the_registry(self) -> None:
        self.host.create(Create(Card("secretary-1417", "Protocol seam", CardState.READY), self.actor, "create"))

        with self.assertRaises(InvalidTransition):
            self.host.transition(TransitionRequest(
                EntityKind.CARD, "secretary-1417", CardState.READY, self.actor, "no lifecycle change",
            ))

    def test_replace_cannot_bypass_the_lifecycle_registry(self) -> None:
        self.host.create(Create(Card("secretary-1417", "Protocol seam", CardState.READY), self.actor, "create"))

        with self.assertRaises(InvalidTransition):
            self.host.replace(Replace(
                Card("secretary-1417", "Protocol seam", CardState.DONE),
                self.actor, "skip review",
            ))

        self.assertEqual(self.host.read(EntityKind.CARD, "secretary-1417").state, CardState.READY)

    def test_every_declared_edge_names_a_semantic_event_kind(self) -> None:
        declared = {event.value for event in EventKind}
        for entity_kind, edges in TRANSITIONS.items():
            self.assertTrue(edges, entity_kind)
            for edge, transition in edges.items():
                self.assertEqual(edge, (transition.source, transition.target))
                self.assertIn(transition.event_kind.value, declared)

    def test_role_aware_registry_allows_a_previously_missing_steward_recovery(self) -> None:
        declaration = card_transition("steward", CardState.BLOCKED, CardState.DONE)

        self.assertEqual(declaration.source, CardState.BLOCKED)
        self.assertEqual(declaration.target, CardState.DONE)
        self.assertEqual(declaration.event_kind, EventKind.CARD_MOVED)

    def test_role_aware_registry_rejects_invalid_role_and_edge(self) -> None:
        with self.assertRaises(CardTransitionForbidden):
            card_transition("worker", CardState.READY, CardState.IN_PROGRESS)
        with self.assertRaises(CardTransitionForbidden):
            card_transition("dispatcher", CardState.READY, CardState.IN_PROGRESS)
        with self.assertRaises(CardTransitionForbidden):
            card_transition("po", CardState.READY, CardState.READY)

    def test_every_authorized_card_edge_has_a_matching_lifecycle_declaration(self) -> None:
        for role, edges in CARD_TRANSITIONS.items():
            for source, target in edges:
                declaration = card_transition(role, source, target)
                self.assertEqual(declaration, TRANSITIONS[EntityKind.CARD][(source, target)])
                self.assertEqual((declaration.source, declaration.target), (source, target))

    def test_task_writer_can_import_the_registry_without_loading_kanboard(self) -> None:
        result = subprocess.run(
            [
                sys.executable, "-P", "-c",
                "import secretary.tasks, sys; assert 'secretary.board.kanboard' not in sys.modules",
            ],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_event_is_typed_deterministic_and_deduplicates_related_refs(self) -> None:
        event = Event(
            "event-1", EventKind.CARD_STARTED, EntityKind.CARD, "secretary-1419",
            self.actor, "worker started", datetime(2026, 8, 11, 18, 0, 0, tzinfo=UTC),
            RelatedRefs(("sprint:943", "head-run:1417", "sprint:943")),
        )

        record = event.to_record("start-1")

        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(record["subject"], {"kind": "card", "ref": "secretary-1419"})
        self.assertEqual(record["related_refs"], ["sprint:943", "head-run:1417"])
        self.assertEqual(Event.from_record(record), event)
        with self.assertRaisesRegex(ValueError, "EventKind"):
            Event("event-2", "card.started", EntityKind.CARD, "secretary-1419", self.actor, "reason", event.occurred_at)

    def test_durable_fake_mutations_append_one_complete_protocol_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            host = FakeBoardHost(data_dir=tmpdir)
            result = host.create(Create(
                Card("secretary-1419", "Typed canon", CardState.READY), self.actor,
                "accepted", RelatedRefs(("sprint:943",)), "create-1",
            ))
            replacement = host.replace(Replace(
                Card("secretary-1419", "Typed canon v2", CardState.READY), self.actor,
                "correct title", request_id="replace-1",
            ))

            events = BoardEventCanon(tmpdir).events(ref="secretary-1419")
            self.assertEqual(events, (result.event, replacement.event))
            self.assertEqual(events[0].kind, EventKind.ENTITY_CREATED)
            self.assertEqual(events[1].kind, EventKind.ENTITY_UPDATED)
            self.assertTrue(all(event.reason and event.occurred_at.tzinfo for event in events))

    def test_every_registry_transition_persists_its_declared_event(self) -> None:
        # The concrete lifecycle values differ by entity type, but this test's
        # contract is intentionally registry-driven: every declared edge gets a
        # completed fake mutation and exactly its declared kind in the canon.
        from secretary.board import Issue, IssueState, Product, ProductState, Sprint, SprintState

        factories = {
            EntityKind.PRODUCT: lambda state, index: Product(f"product:{index}", "Product", state),
            EntityKind.ISSUE: lambda state, index: Issue(f"issue:{index}", "Issue", "product:1", state),
            EntityKind.SPRINT: lambda state, index: Sprint(f"sprint:{index}", "Sprint", state),
            EntityKind.CARD: lambda state, index: Card(f"card:{index}", "Card", state),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            for index, (entity_kind, edges) in enumerate(TRANSITIONS.items(), start=1):
                for edge_index, transition in enumerate(edges.values(), start=1):
                    entity = factories[entity_kind](transition.source, f"{index}-{edge_index}")
                    host = FakeBoardHost([entity], data_dir=tmpdir)
                    result = host.transition(TransitionRequest(
                        entity_kind, entity.ref, transition.target, self.actor, "registry edge",
                        request_id=f"transition-{index}-{edge_index}",
                    ))
                    self.assertEqual(result.event.kind, transition.event_kind)
            recorded = BoardEventCanon(tmpdir).events()
            self.assertEqual(len(recorded), sum(len(edges) for edges in TRANSITIONS.values()))


class BoardMutationTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.canon = BoardEventCanon(self.tmpdir.name)
        self.event = Event(
            "event-transaction", EventKind.CARD_STARTED, EntityKind.CARD, "secretary-1419",
            Actor("worker", "worker-1419"), "start work",
            datetime(2026, 8, 11, 18, 0, 0, tzinfo=UTC),
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_backend_failure_before_commit_removes_the_staged_event(self) -> None:
        transaction = MutationEventTransaction(self.canon, request_id="failure-1", event=self.event)

        with self.assertRaisesRegex(RuntimeError, "backend failed"):
            transaction.execute(lambda: (_ for _ in ()).throw(RuntimeError("backend failed")), replay=lambda: "never")

        self.assertIsNone(self.canon.event("failure-1"))
        self.assertEqual(self.canon.audit.status(), {"ok": True, "pending": 0})

    def test_event_write_failure_is_pending_and_replay_does_not_repeat_backend(self) -> None:
        transaction = MutationEventTransaction(self.canon, request_id="pending-1", event=self.event)
        with mock.patch.object(self.canon.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaises(BoardEventPending):
                transaction.execute(lambda: "backend result", replay=lambda: "replayed")
        self.assertEqual(self.canon.audit.status(), {"ok": False, "pending": 1})

        recovered = transaction.execute(
            lambda: (_ for _ in ()).throw(AssertionError("effect repeated")), replay=lambda: "confirmed",
        )
        self.assertEqual(recovered, "confirmed")
        self.assertEqual(self.canon.events(), (self.event,))

    def test_committed_request_id_replay_does_not_repeat_backend_effect(self) -> None:
        transaction = MutationEventTransaction(self.canon, request_id="replay-1", event=self.event)
        self.assertEqual(transaction.execute(lambda: "first", replay=lambda: "read"), "first")
        self.assertEqual(
            transaction.execute(
                lambda: (_ for _ in ()).throw(AssertionError("effect repeated")), replay=lambda: "replayed",
            ),
            "replayed",
        )
        self.assertEqual(self.canon.events(), (self.event,))


class KanboardBoardHostTests(unittest.TestCase):
    def test_cards_exclude_typed_rows_and_read_refuses_them(self) -> None:
        execution = {
            "ref": "secretary-1417", "title": "Protocol seam", "state": "ready",
            "record_type": "task",
        }
        issue = {
            "ref": "issue:1417", "title": "Typed issue", "state": "issues",
            "record_type": "issue",
        }
        product = {
            "ref": "product:secretary", "title": "Typed product", "state": "issues",
            "record_type": "product",
        }
        with mock.patch("secretary.board.kanboard.TaskReader") as reader_class:
            reader = reader_class.return_value
            reader.list.return_value = [execution, issue, product]
            reader.show.return_value = issue
            host = KanboardBoardHost(mock.sentinel.client)

            cards = host.list(EntityKind.CARD)
            self.assertEqual([card.ref for card in cards], ["secretary-1417"])
            with self.assertRaises(BoardProtocolError):
                host.read(EntityKind.CARD, "issue:1417")

    def test_sprint_product_ref_can_read_the_linked_product(self) -> None:
        sprint = {
            "ref": "sprint:943", "goal": "Board protocol", "status": "open",
            "product": "secretary", "issues": ["issue:1417"], "cards": [],
        }
        product = {
            "ref": "product:secretary", "title": "Secretary", "closed": False,
            "projects": ["secretary"],
        }
        with mock.patch("secretary.board.kanboard.SprintReader") as sprint_reader_class, \
             mock.patch("secretary.board.kanboard.ProductIssueStore") as store_class:
            sprint_reader_class.return_value.show.return_value = sprint
            store_class.return_value.show_product.return_value = product
            host = KanboardBoardHost(mock.sentinel.client, data_dir="/data", instance="/instance")

            normalized_sprint = host.read(EntityKind.SPRINT, "sprint:943")
            self.assertEqual(normalized_sprint.product_ref, "product:secretary")
            linked_product = host.read(EntityKind.PRODUCT, normalized_sprint.product_ref)
            self.assertEqual(linked_product.ref, normalized_sprint.product_ref)


if __name__ == "__main__":
    unittest.main()
