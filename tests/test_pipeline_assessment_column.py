"""The legacy pipeline surface against the Assessment column (secretary-1025).

`secretary` owns the Pipeline board schema and the role/transition model. This surface reads and
writes the same board, so two things have to hold: it must not reshape a populated board by
positional rename, and it must not answer a release decision the task protocol owns.
"""

from __future__ import annotations

import unittest
from unittest import mock

from triggered_agents.agents.pipeline import model, ops

CURRENT_COLUMNS = [
    {"id": index, "title": title, "position": index} for index, title in enumerate(model.COLUMNS, 1)
]
LEGACY_COLUMNS = [
    {"id": index, "title": title, "position": index}
    for index, title in enumerate(("Issues", "Ready", "In progress", "Validate", "Blocked", "Done"), 1)
]
KANBOARD_DEFAULTS = [
    {"id": index, "title": title, "position": index}
    for index, title in enumerate(("Backlog", "Ready", "Work in progress", "Done"), 1)
]
_SCHEMA_WRITES = ("updateColumn", "addColumn", "removeColumn")


def _board(columns, *, open_cards=(), closed_cards=()):
    """A fake Kanboard whose column list is mutated by the calls made against it."""
    columns = [dict(column) for column in columns]
    calls: list[tuple[str, dict]] = []

    def call(method, **params):
        calls.append((method, params))
        if method == "getColumns":
            return columns
        if method == "getAllTasks":
            return list(open_cards if params.get("status_id") == 1 else closed_cards)
        if method == "updateColumn":
            for column in columns:
                if column["id"] == params["column_id"]:
                    column["title"] = params["title"]
            return True
        if method == "addColumn":
            columns.append({"id": len(columns) + 1, "title": params["title"], "position": len(columns) + 1})
            return len(columns)
        if method == "removeColumn":
            columns[:] = [c for c in columns if c["id"] != params["column_id"]]
            return True
        raise AssertionError(f"unexpected call {method} {params}")

    return call, calls, columns


class EnsureStructureTests(unittest.TestCase):
    def _run(self, columns, **cards):
        call, calls, current = _board(columns, **cards)
        with (
            mock.patch.object(ops, "call", side_effect=call),
            mock.patch.object(ops, "board_id", return_value=2),
            mock.patch.dict("os.environ", {}, clear=False) as environ,
        ):
            # No admin user, so the board-membership call this would otherwise make stays out.
            environ.pop("KANBOARD_ADMIN_USER", None)
            return ops.ensure_structure(), calls, current

    def test_refuses_a_populated_legacy_board_and_names_the_migration(self):
        """The defect this guard exists for: reconciling by index would rename Blocked to
        Assessment under the cards standing in Blocked."""
        with self.assertRaises(model.GuardError) as raised:
            self._run(LEGACY_COLUMNS, open_cards=[{"id": 7, "column_id": 5}])

        message = str(raised.exception)
        self.assertIn("Blocked", message)
        self.assertIn(", ".join(model.COLUMNS), message)
        self.assertIn("secretary board migrate-assessment", message)

    def test_refuses_a_populated_board_before_any_schema_write(self):
        call, calls, columns = _board(LEGACY_COLUMNS, open_cards=[{"id": 7, "column_id": 5}])
        with (
            mock.patch.object(ops, "call", side_effect=call),
            mock.patch.object(ops, "board_id", return_value=2),
        ):
            with self.assertRaises(model.GuardError):
                ops.ensure_structure()

        self.assertEqual([c["title"] for c in columns], [c["title"] for c in LEGACY_COLUMNS])
        for method in _SCHEMA_WRITES:
            self.assertNotIn(method, [name for name, _ in calls])

    def test_a_closed_card_still_counts_as_a_populated_board(self):
        """A closed card sits in a column too, so a rename would restate what it meant."""
        with self.assertRaises(model.GuardError):
            self._run(LEGACY_COLUMNS, closed_cards=[{"id": 7, "column_id": 6}])

    def test_refuses_any_other_populated_layout(self):
        unknown = [dict(LEGACY_COLUMNS[0], title="Backlog"), *LEGACY_COLUMNS[1:]]
        with self.assertRaises(model.GuardError) as raised:
            self._run(unknown, open_cards=[{"id": 7, "column_id": 1}])
        self.assertIn("Backlog", str(raised.exception))

    def test_a_populated_board_on_the_current_layout_is_verified_not_rewritten(self):
        result, calls, columns = self._run(CURRENT_COLUMNS, open_cards=[{"id": 7, "column_id": 5}])

        self.assertEqual(result["board_id"], 2)
        self.assertEqual([c["title"] for c in columns], list(model.COLUMNS))
        for method in _SCHEMA_WRITES:
            self.assertNotIn(method, [name for name, _ in calls])

    def test_an_empty_board_is_still_reconciled(self):
        """The reconcile path is unchanged where it is safe: nothing stands in these columns."""
        result, calls, columns = self._run(KANBOARD_DEFAULTS)

        self.assertEqual([c["title"] for c in columns], list(model.COLUMNS))
        self.assertEqual(result["columns"], model.COLUMNS)
        self.assertIn("updateColumn", [name for name, _ in calls])


class LegacyClaimTests(unittest.TestCase):
    """This surface still exposes `pipeline --role dispatcher claim`, so its own guards have to
    know that a parked card holds a worker and a project checkout for as long as the decision
    takes. Counting only In progress and Validate would let a second writer into the checkout a
    card in Assessment still owns."""

    def _claim(self, parked_meta, cap=3):
        cards = {
            7: {"id": 7, "reference": "secretary-1", "column_id": 2, "swimlane_id": 1},
            8: {"id": 8, "reference": "secretary-2", "column_id": 5, "swimlane_id": 1},
        }
        meta = {
            7: {model.META_TASK_TYPE: "code", model.META_PROJECT: "secretary"},
            8: dict(parked_meta),
        }

        def call(method, **params):
            if method == "getColumns":
                return CURRENT_COLUMNS
            if method == "getTaskByReference":
                return cards[7]
            if method == "getAllTasks":
                return list(cards.values())
            if method == "getTaskMetadata":
                return meta[int(params["task_id"])]
            if method in ("saveTaskMetadata", "setTaskTags", "moveTaskPosition"):
                return True
            if method == "getTaskTags":
                return {}
            raise AssertionError(f"unexpected call {method} {params}")

        with (
            mock.patch.object(ops, "call", side_effect=call),
            mock.patch.object(ops, "board_id", return_value=2),
            mock.patch.object(ops, "_check_head", return_value=None),
            mock.patch.object(ops.heads, "default_head", return_value="sol"),
        ):
            return ops.claim_card("secretary-1", "worker-a", cap=cap)

    def test_a_parked_card_still_holds_its_project_against_a_legacy_claim(self):
        with self.assertRaises(model.GuardError) as raised:
            self._claim({model.META_TASK_TYPE: "code", model.META_PROJECT: "secretary"})

        message = str(raised.exception)
        self.assertIn("one code task per project", message)
        self.assertIn("Assessment", message)

    def test_a_parked_card_counts_toward_the_legacy_cap(self):
        """Another project, so the per-project guard says nothing: the parked card still holds a
        worker session, which is what the cap counts."""
        with self.assertRaises(model.GuardError) as raised:
            self._claim({model.META_TASK_TYPE: "code", model.META_PROJECT: "other"}, cap=1)

        self.assertIn("cap reached", str(raised.exception))


class AssessmentGuardTests(unittest.TestCase):
    def test_the_steward_escalation_is_the_one_edge_this_surface_owns(self):
        model.check_move("steward", "Assessment", "Blocked")
        self.assertIn(("Assessment", "Blocked"), model.STEWARD_ESCALATIONS)

    def test_a_release_decision_is_refused_and_points_at_the_task_protocol(self):
        for role, source, target in (
            ("po", "Assessment", "Done"),
            ("steward", "Assessment", "Ready"),
            ("dispatcher", "Validate", "Assessment"),
            ("dispatcher", "Assessment", "Done"),
        ):
            with self.subTest(role=role, source=source, target=target):
                with self.assertRaises(model.GuardError) as raised:
                    model.check_move(role, source, target)
                self.assertIn("secretary task move", str(raised.exception))

    def test_this_surface_still_knows_only_its_own_roles(self):
        """`observer` is not added here: the task protocol carries the observer's authority."""
        self.assertNotIn("observer", model.ROLES)
        with self.assertRaisesRegex(model.GuardError, "unknown role"):
            model.check_move("observer", "Assessment", "Done")

    def test_the_column_is_known_so_a_card_there_can_still_be_read(self):
        self.assertEqual(model.COLUMNS.index("Assessment"), 4)


if __name__ == "__main__":
    unittest.main()
