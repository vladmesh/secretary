from __future__ import annotations

import unittest

from secretary.board import Role as ExportedRole
from secretary.board.card_transitions import (
    CARD_TRANSITIONS,
    CardTransitionForbidden,
    card_transition,
)
from secretary.board.models import CardState
from secretary.board.roles import (
    BOARD_ROLES,
    CREATE_ROLES,
    EDIT_ROLES,
    PROPOSAL_CREATE_ROLES,
    Role,
)
from secretary.tasks import (
    _CREATE_ROLES,
    _EDIT_ROLES,
    _PROPOSAL_CREATE_ROLES,
    _ROLES,
)


class RoleVocabularyTests(unittest.TestCase):
    def test_role_is_public_string_compatible_closed_vocabulary(self) -> None:
        self.assertIs(ExportedRole, Role)
        self.assertIs(Role("worker"), Role.WORKER)
        self.assertEqual(str(Role.WORKER), "worker")
        self.assertEqual(
            {role.value for role in BOARD_ROLES},
            {"po", "dispatcher", "worker", "reviewer", "observer", "steward", "retro"},
        )

    def test_legacy_task_role_sets_cannot_drift_from_canonical_product_sets(self) -> None:
        self.assertEqual(_ROLES, {role.value for role in BOARD_ROLES})
        self.assertEqual(_CREATE_ROLES, {role.value for role in CREATE_ROLES})
        self.assertEqual(_PROPOSAL_CREATE_ROLES, {role.value for role in PROPOSAL_CREATE_ROLES})
        self.assertEqual(_EDIT_ROLES, {role.value for role in EDIT_ROLES})

    def test_card_transition_registry_uses_the_canonical_role_type(self) -> None:
        self.assertEqual(set(CARD_TRANSITIONS), BOARD_ROLES)
        by_enum = card_transition(Role.DISPATCHER, CardState.READY, CardState.IN_PROGRESS)
        by_string = card_transition("dispatcher", "ready", "in_progress")
        self.assertEqual(by_enum, by_string)

    def test_unknown_role_is_refused_at_the_transition_boundary(self) -> None:
        with self.assertRaises(CardTransitionForbidden):
            card_transition("dispatchre", CardState.READY, CardState.IN_PROGRESS)


if __name__ == "__main__":
    unittest.main()
