"""How the pipeline route numbers a new card's reference (issue:f32aa04c).

Reproduced live on 2026-08-18: `create` named the card after the Kanboard row id it had just been
given, the ids had grown into a range of references handed out under an older numbering, and the
new card's reference resolved to an archived card belonging to someone else. Every later `show`,
`update`, `move` and `blocked_by` on that reference addressed the old row without saying so.
"""
from __future__ import annotations

import contextlib
import unittest
from unittest import mock

from triggered_agents.agents.pipeline import ops
from triggered_agents.runtime.kanboard import KanboardError

BOARD_COLUMNS = [
    {"id": 1, "title": "Issues", "position": 1}, {"id": 2, "title": "Ready", "position": 2},
    {"id": 3, "title": "In progress", "position": 3}, {"id": 4, "title": "Validate", "position": 4},
    {"id": 5, "title": "Blocked", "position": 5}, {"id": 6, "title": "Done", "position": 6},
]
# The row a fresh createTask is given, far below the references the board has handed out.
NEW_ROW_ID = 41


def _board(rows, calls):
    def call(method, **params):
        calls.append((method, params))
        if method == "getAllProjects":
            return [{"id": 2, "name": ops.model.BOARD_NAME}]
        if method == "getColumns":
            return BOARD_COLUMNS
        if method == "getActiveSwimlanes":
            return [{"id": 1, "name": "secretary"}]
        if method == "getAllTasks":
            return [dict(row) for row in rows if row["is_active"] == (params["status_id"] == 1)]
        if method == "getTaskByReference":
            return next(
                (dict(row) for row in rows if row["reference"] == params["reference"]), None,
            )
        if method == "createTask":
            rows.append({
                "id": NEW_ROW_ID, "reference": params.get("reference", ""), "is_active": True,
            })
            return NEW_ROW_ID
        if method in ("updateTask", "saveTaskMetadata"):
            return True
        if method == "getTaskTags":
            return {}
        raise AssertionError(f"unexpected call {method} {params}")

    return call


def _rows():
    return [
        {"id": 5, "reference": "secretary-902", "is_active": True},
        # The half the row id forgets: an archived card keeps its reference for good.
        {"id": 6, "reference": "secretary-1404", "is_active": False},
        {"id": 7, "reference": "other-2000", "is_active": True},
    ]


class CardReferenceTests(unittest.TestCase):
    def test_a_new_card_is_numbered_above_every_reference_of_its_project(self) -> None:
        calls: list[tuple[str, dict]] = []
        with mock.patch.object(ops, "call", side_effect=_board(_rows(), calls)), \
             mock.patch.object(ops, "_sync_head_tags"):
            created = ops.create_card(
                project="secretary", task_type="code", title="numbered", role="po",
            )

        self.assertEqual(created["reference"], "secretary-1405")
        # Written by the create itself: a row is never published under a reference it has to be
        # patched into afterwards.
        written = next(params for method, params in calls if method == "createTask")
        self.assertEqual(written["reference"], "secretary-1405")
        self.assertFalse(any(method == "updateTask" for method, _params in calls))

    def test_a_reference_the_board_already_holds_is_refused_on_both_paths(self) -> None:
        for reference, description in (
            ("secretary-1404", "the caller's own reference, archived"),
            (None, "an allocated reference the enumeration did not see"),
        ):
            with self.subTest(reference=description):
                calls: list[tuple[str, dict]] = []
                board = _board(_rows(), calls)
                # With no reference of its own the create allocates one; the patch stands in for
                # an enumeration that did not see the row holding it.
                allocated = (
                    mock.patch.object(ops, "next_reference", return_value="secretary-1404")
                    if reference is None else contextlib.nullcontext()
                )
                with mock.patch.object(ops, "call", side_effect=board), allocated:
                    with self.assertRaisesRegex(KanboardError, "secretary-1404 is already claimed"):
                        ops.create_card(
                            project="secretary", task_type="code", title="collides",
                            ref=reference, role="po",
                        )

                self.assertFalse(any(method == "createTask" for method, _params in calls))


if __name__ == "__main__":
    unittest.main()
