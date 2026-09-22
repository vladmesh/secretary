from __future__ import annotations

from unittest import TestCase

from secretary.board.models import SprintState
from secretary.board.roles import Role
from secretary.board.sprint_write import (
    SprintCreateIntent,
    SprintMutationReceipt,
    SprintReopenIntent,
    SprintWriteSnapshot,
)
from tests.retired_board import RETIRED_STORE


class SprintWriteValueTests(TestCase):
    def test_snapshot_parses_writer_fields_and_admission(self) -> None:
        snapshot = SprintWriteSnapshot.from_document(
            {
                "id": "sprint_postgres_42",
                "ref": "sprint:42",
                "status": "stopped",
                "repositories": ["/srv/a", "/srv/b"],
                "product": "product:7",
                "issues": ["issue:8"],
                "reservations": ["secretary"],
                "budget": {"by_type": {"blocked": 2}},
                "current_task": "secretary-9",
                "observer": {"kind": "none"},
                "audit": {"updated_at": "2026-09-16T12:00:00Z"},
            }
        )
        self.assertEqual(snapshot.state, SprintState.STOPPED)
        self.assertEqual(snapshot.repositories, ("/srv/a", "/srv/b"))
        self.assertEqual(snapshot.issues, ("issue:8",))
        self.assertEqual(snapshot.budget.by_type["blocked"], 2)
        self.assertEqual(snapshot.current_task, "secretary-9")
        self.assertEqual(snapshot.updated_at, "2026-09-16T12:00:00Z")
        admission = snapshot.admission()
        self.assertEqual(admission.ref, "sprint:42")
        self.assertEqual(admission.product, "product:7")
        self.assertEqual(admission.reservations, ("secretary",))
        self.assertTrue(admission.is_open is False)

    def test_snapshot_keeps_reader_legacy_unknown_state_fallback(self) -> None:
        snapshot = SprintWriteSnapshot.from_document({"id": f"sprint_{RETIRED_STORE}_1", "status": "mystery"})
        self.assertEqual(snapshot.state, SprintState.OPEN)

    def test_create_intent_round_trips_released_document(self) -> None:
        document = {
            "role": "po",
            "actor": "owner",
            "goal": "Ship it",
            "definition_of_done": "green",
            "repositories": ["/srv/a"],
            "product": "product:1",
            "issues": ["issue:2"],
            "reservations": ["secretary"],
            "reference": "sprint:3",
            "status": "open",
            "observer": {"kind": "none"},
            "worker": None,
            "reviewer": "review-head",
        }
        intent = SprintCreateIntent.from_document(document)
        self.assertEqual(intent.role, Role.PO)
        self.assertEqual(intent.state, SprintState.OPEN)
        self.assertEqual(intent.to_document(), document)
        self.assertEqual(intent.admission().repositories, ("/srv/a",))

    def test_reopen_intent_round_trips_released_document(self) -> None:
        document = {
            "role": "po",
            "actor": "owner",
            "reference": "sprint:3",
            "observer": {"kind": "head", "profile": "observer"},
        }
        intent = SprintReopenIntent.from_document(document)
        self.assertEqual(intent.role, Role.PO)
        self.assertEqual(intent.to_document(), document)

    def test_receipt_projects_the_existing_public_document(self) -> None:
        sprint = {"ref": "sprint:3", "status": "open", "nested": {"kept": True}}
        result = SprintMutationReceipt("commented", "evt_1").to_document(sprint)
        self.assertEqual(
            result,
            {"action": "commented", "sprint": sprint, "event_id": "evt_1"},
        )
        self.assertIsNot(result["sprint"], sprint)
