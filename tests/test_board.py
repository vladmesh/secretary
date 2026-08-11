"""Contract tests for the additive, backend-neutral board protocol."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from secretary.board import (
    Actor, BoardProtocolError, Card, CardState, Create, EntityKind, EventKind,
    FakeBoardHost, InvalidTransition, KanboardBoardHost, RelatedRefs, Replace,
    TRANSITIONS, TransitionRequest,
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
