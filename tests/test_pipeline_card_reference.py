"""How the pipeline route numbers a new card's reference (issue:f32aa04c).

Reproduced live on 2026-08-18: `create` named the card after the Kanboard row id it had just been
given, the ids had grown into a range of references handed out under an older numbering, and the
new card's reference resolved to an archived card belonging to someone else. Every later `show`,
`update`, `move` and `blocked_by` on that reference addressed the old row without saying so.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import unittest
from unittest import mock

from triggered_agents.agents.pipeline import ops
from triggered_agents.runtime.kanboard import KanboardError

BOARD_COLUMNS = [
    {"id": 1, "title": "Issues", "position": 1},
    {"id": 2, "title": "Ready", "position": 2},
    {"id": 3, "title": "In progress", "position": 3},
    {"id": 4, "title": "Validate", "position": 4},
    {"id": 5, "title": "Blocked", "position": 5},
    {"id": 6, "title": "Done", "position": 6},
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
                (dict(row) for row in rows if row["reference"] == params["reference"]),
                None,
            )
        if method == "createTask":
            rows.append(
                {
                    "id": NEW_ROW_ID,
                    "reference": params.get("reference", ""),
                    "is_active": True,
                }
            )
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
    def setUp(self) -> None:
        # A create takes the board's allocation lock, which lives in the installation's data plane.
        # The suite is hermetic, so it points at a temporary one rather than the live host's.
        data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(data_dir.cleanup)
        patched = mock.patch.dict(os.environ, {"SECRETARY_DATA_DIR": data_dir.name})
        patched.start()
        self.addCleanup(patched.stop)

    def test_a_new_card_is_numbered_above_every_reference_of_its_project(self) -> None:
        calls: list[tuple[str, dict]] = []
        with (
            mock.patch.object(ops, "call", side_effect=_board(_rows(), calls)),
            mock.patch.object(ops, "_sync_head_tags"),
        ):
            created = ops.create_card(
                project="secretary",
                task_type="code",
                title="numbered",
                role="po",
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
                    if reference is None
                    else contextlib.nullcontext()
                )
                with mock.patch.object(ops, "call", side_effect=board), allocated:
                    with self.assertRaisesRegex(KanboardError, "secretary-1404 is already claimed"):
                        ops.create_card(
                            project="secretary",
                            task_type="code",
                            title="collides",
                            ref=reference,
                            role="po",
                        )

                self.assertFalse(any(method == "createTask" for method, _params in calls))


class ConcurrentCreateTests(unittest.TestCase):
    """Two local creators cannot hand out one reference (issue:f32aa04c).

    The claim check only rules out a collision that already existed when it ran. What stops two
    creators from both allocating the same free number is that the whole allocate-check-write runs
    inside the board's one allocation lock.
    """

    def setUp(self) -> None:
        data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(data_dir.cleanup)
        patched = mock.patch.dict(os.environ, {"SECRETARY_DATA_DIR": data_dir.name})
        patched.start()
        self.addCleanup(patched.stop)

    def test_two_concurrent_creates_take_different_references(self) -> None:
        rows = [{"id": 5, "reference": "secretary-902", "is_active": True}]
        guard = threading.Lock()
        # Both creators try to meet here while enumerating. Serialized, one of them cannot arrive
        # until the other has written its card, so the barrier breaks on its timeout instead of
        # letting the two share a high-water mark - which is the collision this asserts against.
        rendezvous = threading.Barrier(2, timeout=0.5)

        def call(method, **params):
            if method == "getAllProjects":
                return [{"id": 2, "name": ops.model.BOARD_NAME}]
            if method == "getColumns":
                return BOARD_COLUMNS
            if method == "getActiveSwimlanes":
                return [{"id": 1, "name": "secretary"}]
            if method == "getAllTasks":
                with contextlib.suppress(threading.BrokenBarrierError):
                    rendezvous.wait()
                with guard:
                    return [dict(row) for row in rows if row["is_active"] == (params["status_id"] == 1)]
            if method == "getTaskByReference":
                with guard:
                    return next(
                        (dict(row) for row in rows if row["reference"] == params["reference"]),
                        None,
                    )
            if method == "createTask":
                with guard:
                    row = {"id": 40 + len(rows), "reference": params["reference"], "is_active": True}
                    rows.append(row)
                    return row["id"]
            if method in ("updateTask", "saveTaskMetadata"):
                return True
            if method == "getTaskTags":
                return {}
            raise AssertionError(f"unexpected call {method} {params}")

        created: list[str] = []

        def create(title: str) -> None:
            card = ops.create_card(project="secretary", task_type="code", title=title, role="po")
            with guard:
                created.append(card["reference"])

        with mock.patch.object(ops, "call", side_effect=call), mock.patch.object(ops, "_sync_head_tags"):
            threads = [threading.Thread(target=create, args=(f"card {index}",)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(sorted(created), ["secretary-903", "secretary-904"])
        self.assertEqual(
            sorted(row["reference"] for row in rows),
            ["secretary-902", "secretary-903", "secretary-904"],
        )


if __name__ == "__main__":
    unittest.main()
