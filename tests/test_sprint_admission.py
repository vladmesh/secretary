from __future__ import annotations

import unittest

from secretary.board.models import SprintState
from secretary.board.sprint_admission import SprintAdmission, SprintReservationIndex


class SprintAdmissionModelTests(unittest.TestCase):
    def test_document_boundary_types_the_closed_admission_shape(self) -> None:
        admission = SprintAdmission.from_document(
            {
                "ref": "sprint:42",
                "product": " product:a ",
                "reservations": [" beta ", "alpha", "alpha", ""],
                "repositories": ["/repo/a", "/repo/b"],
                "status": "open",
                "ignored": {"legacy": True},
            }
        )

        self.assertEqual(admission.ref, "sprint:42")
        self.assertEqual(admission.product, "product:a")
        self.assertEqual(admission.reservations, (" beta ", "alpha", "alpha", ""))
        self.assertEqual(admission.repositories, ("/repo/a", "/repo/b"))
        self.assertIs(admission.state, SprintState.OPEN)
        self.assertTrue(admission.is_open)
        self.assertEqual(admission.guard_projects, ("alpha", "beta"))

    def test_unknown_state_does_not_become_an_open_guard_reservation(self) -> None:
        admission = SprintAdmission.from_document(
            {"ref": "sprint:42", "status": "future", "reservations": ["secretary"]}
        )

        self.assertIsNone(admission.state)
        self.assertFalse(admission.is_open)
        self.assertEqual(SprintReservationIndex.from_sprints([admission]).entries, ())

    def test_guard_index_round_trip_preserves_released_versioned_shape(self) -> None:
        raw = {
            "version": 2,
            "projects": {
                "secretary": ["sprint:2", "sprint:1", "sprint:1", ""],
                "other": [],
                "ignored": "not-a-list",
            },
        }

        index = SprintReservationIndex.from_document(raw, version=2)

        self.assertIsNotNone(index)
        assert index is not None
        self.assertEqual(
            index.to_document(version=2),
            {
                "version": 2,
                "projects": {"other": [], "secretary": ["sprint:1", "sprint:2"]},
            },
        )
        self.assertIsNone(SprintReservationIndex.from_document(raw, version=3))
        self.assertIsNone(SprintReservationIndex.from_document({"version": 2}, version=2))

    def test_index_updates_are_immutable_and_keep_open_sprints_only(self) -> None:
        first = SprintAdmission.from_document(
            {"ref": "sprint:1", "status": "open", "reservations": ["secretary"]}
        )
        second = SprintAdmission.from_document(
            {"ref": "sprint:2", "status": "open", "reservations": ["secretary", "other"]}
        )
        closed = SprintAdmission.from_document(
            {"ref": "sprint:3", "status": "closed", "reservations": ["secretary"]}
        )

        index = SprintReservationIndex.from_sprints([first, second, closed])
        removed = index.without_sprint("sprint:1")

        self.assertEqual(
            index.to_projects_document(),
            {"other": ["sprint:2"], "secretary": ["sprint:1", "sprint:2"]},
        )
        self.assertEqual(
            removed.to_projects_document(),
            {"other": ["sprint:2"], "secretary": ["sprint:2"]},
        )
        self.assertEqual(index.to_projects_document()["secretary"], ["sprint:1", "sprint:2"])


if __name__ == "__main__":
    unittest.main()
