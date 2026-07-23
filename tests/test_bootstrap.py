from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.bootstrap import BOOTSTRAP_STAMP, PIPELINE_COLUMNS, bootstrap, ensure_pipeline_board


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
        if method == "getVersion":
            return "1.2.46"
        if method == "createProject":
            self.project = {"id": 7, "name": params["name"]}
            # Kanboard 1.2.46 creates these four columns for a new project.
            self.columns = [{"id": n, "title": title} for n, title in enumerate(("Backlog", "Ready", "Work in progress", "Done"), 1)]
            return 7
        if method == "getColumns":
            return self.columns
        if method == "getAllTasks":
            return []
        if method == "updateColumn":
            for column in self.columns:
                if column["id"] == params["column_id"]:
                    column["title"] = params["title"]
            return True
        if method == "addColumn":
            self.columns.append({"id": len(self.columns) + 1, "title": params["title"]})
            return len(self.columns)
        if method == "removeColumn":
            self.columns = [column for column in self.columns if column["id"] != params["column_id"]]
            return True
        if method == "getActiveSwimlanes":
            return self.lanes
        if method == "addSwimlane":
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

    def test_removes_surplus_columns_with_supported_method(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instance = Path(temporary)
            board = Board()
            board.project = {"id": 7, "name": "Pipeline"}
            board.columns = [
                {"id": index, "title": f"old-{index}"} for index in range(1, 9)
            ]

            ensure_pipeline_board(instance, client=board)

            self.assertEqual([column["title"] for column in board.columns], list(PIPELINE_COLUMNS))
            self.assertIn("removeColumn", board.calls)

    def test_bootstrap_generates_usable_runtime_and_ignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            board = Board()

            def clone(_remote: str, directory: Path, **_kwargs: object) -> str:
                directory.mkdir()
                (directory / ".git" / "info").mkdir(parents=True)
                return "cloned private instance remote"

            args = SimpleNamespace(
                instance_dir=str(target), instance_remote="remote", installation_user="dev", dry_run=False,
            )
            with (
                mock.patch("secretary.bootstrap.os.geteuid", return_value=0),
                mock.patch("secretary.bootstrap._ensure_installation_user"),
                mock.patch("secretary.bootstrap._clone_or_reuse", side_effect=clone),
                mock.patch("secretary.bootstrap._install_platform"),
                mock.patch("secretary.bootstrap._start_orca_service"),
                mock.patch("secretary.bootstrap._set_installation_owner"),
                mock.patch("secretary.bootstrap._compose_file"),
                mock.patch("secretary.bootstrap._run"),
                mock.patch("secretary.bootstrap.KanboardClient", return_value=board),
            ):
                self.assertEqual(bootstrap(args), 0)

            runtime = (target / "runtime.env").read_text(encoding="utf-8")
            self.assertIn("KANBOARD_API_USER=jsonrpc\n", runtime)
            self.assertIn("KANBOARD_API_TOKEN=", runtime)
            self.assertNotIn("KANBOARD_BOOTSTRAP_TOKEN", runtime)
            self.assertNotIn("KANBOARD_IMAGE", runtime)
            self.assertNotIn("KANBOARD_ADMIN_PASSWORD", runtime)
            self.assertEqual((target / "runtime.env").stat().st_mode & 0o777, 0o600)
            self.assertTrue((target / BOOTSTRAP_STAMP).is_file())
            exclude = (target / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn(f"/{BOOTSTRAP_STAMP}", exclude)
            self.assertIn("/runtime.env", exclude)
            compose = (target.parent / "compose.yml")
            from secretary.bootstrap import _compose_file
            _compose_file(compose)
            contents = compose.read_text(encoding="utf-8")
            self.assertIn("API_AUTHENTICATION_TOKEN: ${KANBOARD_API_TOKEN}", contents)
            self.assertIn("image: kanboard/kanboard:v1.2.46", contents)


if __name__ == "__main__":
    unittest.main()
