from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from secretary.bootstrap import PIPELINE_COLUMNS, ensure_pipeline_board


class Board:
    def __init__(self) -> None:
        self.project: dict[str, object] | None = None
        self.columns: list[dict[str, object]] = []
        self.lanes: list[dict[str, object]] = []
        self.calls: list[str] = []

    def call(self, method: str, **params: object) -> object:
        self.calls.append(method)
        if method == "getProjectByName":
            return self.project
        if method == "createProject":
            self.project = {"id": 7, "name": params["name"]}
            # Kanboard creates three default columns for a new project.
            self.columns = [{"id": n, "title": title} for n, title in enumerate(("Backlog", "Ready", "Done"), 1)]
            return 7
        if method == "getColumns":
            return self.columns
        if method == "getAllTasks":
            return []
        if method == "updateColumn":
            for column in self.columns:
                if column["id"] == params["id"]:
                    column["title"] = params["title"]
            return True
        if method == "createColumn":
            self.columns.append({"id": len(self.columns) + 1, "title": params["title"]})
            return len(self.columns)
        if method == "getActiveSwimlanes":
            return self.lanes
        if method == "createSwimlane":
            self.lanes.append({"id": len(self.lanes) + 1, "name": params["name"]})
            return len(self.lanes)
        raise AssertionError(method)


class BootstrapBoardTests(unittest.TestCase):
    def test_creates_pipeline_schema_and_registry_lanes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instance = Path(temporary)
            projects = instance / "projects"
            projects.mkdir()
            (projects / "api.yaml").write_text("id: api\n", encoding="utf-8")
            (projects / "web.yaml").write_text("id: web\n", encoding="utf-8")
            board = Board()

            self.assertEqual(ensure_pipeline_board(instance, client=board), 7)
            self.assertEqual([column["title"] for column in board.columns], list(PIPELINE_COLUMNS))
            self.assertEqual([lane["name"] for lane in board.lanes], ["api", "web"])
            calls = len(board.calls)

            self.assertEqual(ensure_pipeline_board(instance, client=board), 7)
            self.assertEqual(len(board.calls), calls + 3)
            self.assertEqual([lane["name"] for lane in board.lanes], ["api", "web"])


if __name__ == "__main__":
    unittest.main()
