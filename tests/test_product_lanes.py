"""The supported repair of where Product and Issue rows sit on the board.

The placement rule is `product_swimlane_id`; these tests are about the rows that predate it, or
that a restore brought back into the lane an old checkpoint recorded.  Nothing here touches a live
board: every case is a fixture whose lanes, columns and positions behave like Kanboard's.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import main
from secretary.product_issues import ProductIssueStore
from secretary.tasks import TaskError
from tests.test_product_issues import ProductBoard


class LaneBoard(ProductBoard):
    """A Pipeline board whose typed rows sit where an older writer put them.

    Lane 1 is the board's `Default swimlane`, lane 4 is `secretary`, lane 5 is `butler`.  There is
    no `codegen` lane at all, which is the live shape: the command has to create it.
    """

    fail_move_after: int | None = None

    def __init__(self) -> None:
        super().__init__()
        self.swimlanes = [
            {"id": 1, "name": "Default swimlane", "position": 1},
            {"id": 4, "name": "secretary", "position": 2},
            {"id": 5, "name": "butler", "position": 3},
        ]
        self.moves = 0
        # The two inherited legacy rows are ordinary execution cards here: they share the board,
        # and a lane move must place a record among them without disturbing them.
        for task in self.tasks:
            task["column_id"] = int(task.get("column_id") or 0)
            task["swimlane_id"] = int(task.get("swimlane_id") or 0)
            task["position"] = int(task["position"]) if str(task.get("position") or "").isdigit() else 1
        self.record(100, "product:secretary", {"record_type": "product", "product_id": "secretary",
                                               "product_projects": '["secretary"]'},
                    swimlane_id=1, position=1)
        self.record(110, "issue:s1", self._issue("secretary"), swimlane_id=1, position=2,
                    comments=("[issue:priority]\nbecause", ))
        self.record(111, "issue:s2", self._issue("secretary"), swimlane_id=1, position=3)
        self.record(112, "issue:c1", self._issue("codegen"), swimlane_id=1, position=4)
        self.record(115, "issue:done", self._issue("codegen"), swimlane_id=1, position=5, closed=True)
        self.record(116, "issue:ghost", self._issue("nowhere"), swimlane_id=1, position=6)
        self.record(117, "issue:blank", self._issue(""), swimlane_id=1, position=7)
        self.record(101, "product:codegen", {"record_type": "product", "product_id": "codegen",
                                             "product_projects": '["codegen"]'},
                    swimlane_id=4, position=1)
        self.record(113, "issue:c2", self._issue("codegen"), swimlane_id=4, position=2)
        self.record(102, "product:butler", {"record_type": "product", "product_id": "butler",
                                            "product_projects": '["butler"]'},
                    swimlane_id=5, position=1)
        self.record(114, "issue:b1", self._issue("butler"), swimlane_id=5, position=2)

    @staticmethod
    def _issue(product: str) -> dict[str, str]:
        return {
            "record_type": "issue", "issue_product": product, "issue_kind": "feature",
            "issue_priority": "P2",
        }

    def record(
        self, task_id: int, reference: str, metadata: dict[str, str], *, column_id: int = 1,
        swimlane_id: int = 1, position: int = 1, closed: bool = False, comments: tuple = (),
    ) -> None:
        self.tasks.append({
            "id": task_id, "reference": reference, "title": reference.upper(),
            "description": f"body of {reference}", "column_id": column_id, "position": position,
            "swimlane_id": swimlane_id, "is_active": 0 if closed else 1,
            "date_creation": "1720000000", "date_modification": "1720000000",
        })
        self.metadata[task_id] = dict(metadata)
        self.comments[task_id] = [
            {"date_creation": "1720000020", "comment": text} for text in comments
        ]

    def call(self, method: str, **params: object) -> object:
        if method == "moveTaskPosition":
            if self.fail_move_after is not None and self.moves >= self.fail_move_after:
                self.calls.append((method, params))
                raise TaskError("backend_error", "Kanboard backend is unavailable", 1)
            self.moves += 1
        return super().call(method, **params)


class ProductLaneReconcileTests(unittest.TestCase):
    MOVING = ["product:secretary", "issue:s1", "issue:s2", "issue:c1", "product:codegen", "issue:c2"]

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "projects").mkdir()
        (self.root / "projects" / "secretary.yaml").write_text("id: secretary\n", encoding="utf-8")
        self.client = LaneBoard()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _store(self, client=None) -> ProductIssueStore:
        return ProductIssueStore(
            client or self.client, data_dir=self.root / "data", instance=self.root,
        )

    def _row(self, reference: str) -> dict:
        return next(task for task in self.client.tasks if task.get("reference") == reference)

    def _lane_of(self, reference: str) -> str:
        identifier = int(self._row(reference)["swimlane_id"])
        return next(
            (str(lane["name"]) for lane in self.client.swimlanes if int(lane["id"]) == identifier), "",
        )

    def _lane_order(self, name: str) -> list[str]:
        identifier = next(int(lane["id"]) for lane in self.client.swimlanes if lane["name"] == name)
        rows = [
            task for task in self.client.tasks
            if int(task.get("swimlane_id") or 0) == identifier and int(task.get("column_id") or 0) == 1
        ]
        return [str(row["reference"]) for row in sorted(rows, key=lambda row: int(row["position"]))]

    def _snapshot(self) -> dict:
        return {
            str(task["reference"]): {
                "title": task["title"], "description": task.get("description", ""),
                "column_id": task["column_id"], "is_active": task.get("is_active", 1),
                "metadata": dict(self.client.metadata[int(task["id"])]),
                "comments": list(self.client.comments[int(task["id"])]),
            }
            for task in self.client.tasks if str(task.get("reference") or "")
        }

    def _written(self, since: int = 0) -> list[str]:
        writes = {"moveTaskPosition", "addSwimlane", "createTask", "updateTask", "saveTaskMetadata",
                  "createComment", "closeTask", "removeTask"}
        return [method for method, _ in self.client.calls[since:] if method in writes]

    def test_the_plan_writes_nothing_and_names_every_move(self) -> None:
        before = json.dumps(self.client.tasks, sort_keys=True)

        result = self._store().reconcile_lanes()

        self.assertEqual(result["mode"], "plan")
        self.assertEqual(result["moved"], 0)
        self.assertEqual([move["ref"] for move in result["moves"]], self.MOVING)
        self.assertEqual(self._written(), [])
        self.assertEqual(json.dumps(self.client.tasks, sort_keys=True), before)
        first = result["moves"][0]
        self.assertEqual(first["from"], {"swimlane_id": 1, "swimlane": "Default swimlane"})
        self.assertEqual(first["to"], {"swimlane": "secretary", "swimlane_id": 4})
        codegen = next(move for move in result["moves"] if move["ref"] == "issue:c1")
        # The `codegen` lane does not exist yet, so the plan says so instead of inventing an id.
        self.assertEqual(codegen["to"], {"swimlane": "codegen", "swimlane_id": None})
        self.assertEqual(result["lanes_to_create"], ["codegen"])
        self.assertEqual(result["summary"], [
            {"product": "butler", "lane": "butler", "move": 0, "in_place": 2, "closed": 0},
            {"product": "codegen", "lane": "codegen", "move": 3, "in_place": 0, "closed": 1},
            {"product": "secretary", "lane": "secretary", "move": 3, "in_place": 0, "closed": 0},
        ])
        self.assertEqual(result["totals"], {
            "records": 11, "move": 6, "in_place": 2, "closed": 1, "unresolved": 2,
        })

    def test_a_record_without_a_registered_product_is_listed_and_left_alone(self) -> None:
        result = self._store().reconcile_lanes(apply=True)

        self.assertEqual(result["unresolved"], [
            {"ref": "issue:ghost", "record_type": "issue", "product": "nowhere",
             "reason": "product is not a registered Product"},
            {"ref": "issue:blank", "record_type": "issue", "product": "",
             "reason": "product is not stated on the record"},
        ])
        for reference in ("issue:ghost", "issue:blank"):
            self.assertEqual(self._lane_of(reference), "Default swimlane")
        self.assertFalse(any(lane["name"] in {"nowhere", ""} for lane in self.client.swimlanes))

    def test_a_closed_record_is_counted_and_not_moved(self) -> None:
        result = self._store().reconcile_lanes(apply=True)

        self.assertEqual(result["totals"]["closed"], 1)
        self.assertEqual(self._lane_of("issue:done"), "Default swimlane")
        self.assertNotIn("issue:done", [move["ref"] for move in result["moves"]])

    def test_apply_moves_the_lane_and_nothing_else(self) -> None:
        before = self._snapshot()

        result = self._store().reconcile_lanes(apply=True)

        self.assertEqual(result["mode"], "apply")
        self.assertEqual(result["moved"], 6)
        # One lane created, `codegen`, and one move per row: nothing else is written at all.
        written = self._written()
        self.assertEqual(written.count("addSwimlane"), 1)
        self.assertEqual(written.count("moveTaskPosition"), 6)
        self.assertEqual(len(written), 7)
        for reference in self.MOVING:
            product = reference.removeprefix("product:")
            if reference.startswith("issue:"):
                product = self.client.metadata[int(self._row(reference)["id"])]["issue_product"]
            self.assertEqual(self._lane_of(reference), product)
        self.assertEqual(self._snapshot(), before)
        moved = next(move for move in result["moves"] if move["ref"] == "issue:c1")
        self.assertEqual(moved["to"]["swimlane"], "codegen")
        self.assertIsInstance(moved["to"]["swimlane_id"], int)

    def test_the_records_that_travel_together_keep_their_order(self) -> None:
        self._store().reconcile_lanes(apply=True)

        self.assertEqual(self._lane_order("secretary"), ["product:secretary", "issue:s1", "issue:s2"])
        self.assertEqual(self._lane_order("codegen"), ["issue:c1", "product:codegen", "issue:c2"])
        # The lane that was already right is untouched, in the order it already had.
        self.assertEqual(self._lane_order("butler"), ["product:butler", "issue:b1"])

    def test_a_second_run_on_a_reconciled_board_writes_nothing(self) -> None:
        self._store().reconcile_lanes(apply=True)
        mark = len(self.client.calls)

        again = self._store().reconcile_lanes(apply=True)

        self.assertEqual(again["moves"], [])
        self.assertEqual(again["moved"], 0)
        self.assertEqual(again["totals"]["move"], 0)
        self.assertEqual(again["lanes_to_create"], [])
        self.assertEqual(self._written(mark), [])

    def test_an_interrupted_run_is_finished_by_the_next_one(self) -> None:
        self.client.fail_move_after = 2
        with self.assertRaises(TaskError):
            self._store().reconcile_lanes(apply=True)
        self.assertEqual(self.client.moves, 2)
        self.client.fail_move_after = None

        result = self._store().reconcile_lanes(apply=True)

        # Four moves are left, and the two rows already carried over are not carried again.
        self.assertEqual(result["moved"], 4)
        self.assertEqual(self.client.moves, 6)
        self.assertEqual(self._lane_order("secretary"), ["product:secretary", "issue:s1", "issue:s2"])
        self.assertEqual(self._lane_order("codegen"), ["issue:c1", "product:codegen", "issue:c2"])
        self.assertEqual(self._store().reconcile_lanes()["moves"], [])

    def test_the_command_plans_by_default_and_writes_only_with_apply(self) -> None:
        with mock.patch(
            "secretary.product_issue_commands.KanboardClient.for_instance", return_value=self.client
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main([
                    "product", "reconcile-lanes", "--instance", str(self.root),
                    "--data-dir", str(self.root / "data"),
                ])
            self.assertEqual(code, 0)
            planned = json.loads(output.getvalue())
            self.assertEqual(planned["mode"], "plan")
            self.assertEqual(planned["moved"], 0)
            self.assertEqual(self._written(), [])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main([
                    "product", "reconcile-lanes", "--instance", str(self.root),
                    "--data-dir", str(self.root / "data"), "--apply",
                ])
            self.assertEqual(code, 0)
            applied = json.loads(output.getvalue())
            self.assertEqual(applied["mode"], "apply")
            self.assertEqual(applied["moved"], len(planned["moves"]))
            self.assertEqual(self._lane_of("issue:c1"), "codegen")


if __name__ == "__main__":
    unittest.main()
