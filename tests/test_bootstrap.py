from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.bootstrap import (
    BOOTSTRAP_STAMP,
    LEGACY_PIPELINE_COLUMNS,
    PIPELINE_COLUMNS,
    BootstrapError,
    _host_supported,
    _install_platform,
    bootstrap,
    ensure_pipeline_board,
    migrate_assessment_column,
)
from secretary.tasks import TaskError


class Board:
    def __init__(self) -> None:
        self.project: dict[str, object] | None = None
        self.columns: list[dict[str, object]] = []
        self.lanes: list[dict[str, object]] = []
        self.tasks: list[dict[str, object]] = []
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
            status = params.get("status_id")
            if status not in {0, 1}:
                return []
            return [
                task for task in self.tasks
                if (int(task.get("is_active", 1) or 0) != 0) == (status == 1)
            ]
        if method == "updateColumn":
            for column in self.columns:
                if column["id"] == params["column_id"]:
                    column["title"] = params["title"]
            return True
        if method == "addColumn":
            self.columns.append({"id": len(self.columns) + 1, "title": params["title"]})
            return len(self.columns)
        if method == "changeColumnPosition":
            column = next(
                (item for item in self.columns if item["id"] == params["column_id"]), None
            )
            if column is None:
                return False
            self.columns.remove(column)
            self.columns.insert(int(params["position"]) - 1, column)  # type: ignore[arg-type]
            return True
        if method == "removeColumn":
            self.columns = [column for column in self.columns if column["id"] != params["column_id"]]
            return True
        if method == "getActiveSwimlanes":
            return self.lanes
        if method == "addSwimlane":
            self.lanes.append({"id": len(self.lanes) + 1, "name": params["name"]})
            return len(self.lanes)
        raise AssertionError(method)


def _legacy_board() -> Board:
    """A live board on the pre-Assessment layout, with cards spread over its columns."""
    board = Board()
    board.project = {"id": 7, "name": "Pipeline"}
    board.columns = [
        {"id": index, "title": title}
        for index, title in enumerate(LEGACY_PIPELINE_COLUMNS, 1)
    ]
    board.tasks = [
        {"id": 11, "column_id": 2, "position": 1, "is_active": 1},
        {"id": 12, "column_id": 4, "position": 1, "is_active": 1},
        {"id": 13, "column_id": 5, "position": 2, "is_active": 1},
        {"id": 14, "column_id": 6, "position": 1, "is_active": 0},
    ]
    return board


class AssessmentMigrationTests(unittest.TestCase):
    """secretary-1025: adding the Assessment column to a board that already holds cards."""

    def test_adds_the_column_at_index_five_without_touching_any_card(self) -> None:
        board = _legacy_board()
        before = [dict(task) for task in board.tasks]

        result = migrate_assessment_column(client=board)

        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["cards"], 4)
        self.assertEqual([column["title"] for column in board.columns], list(PIPELINE_COLUMNS))
        self.assertEqual(board.columns[4]["title"], "Assessment")
        self.assertEqual(board.tasks, before)
        self.assertNotIn("updateColumn", board.calls)
        self.assertNotIn("removeColumn", board.calls)
        self.assertNotIn("moveTaskPosition", board.calls)

    def test_running_it_twice_is_a_no_op_with_a_success_result(self) -> None:
        board = _legacy_board()
        migrate_assessment_column(client=board)
        columns = [dict(column) for column in board.columns]
        board.calls.clear()

        result = migrate_assessment_column(client=board)

        self.assertEqual(result["status"], "unchanged")
        self.assertIs(result["ok"], True)
        self.assertEqual([dict(column) for column in board.columns], columns)
        self.assertNotIn("addColumn", board.calls)
        self.assertNotIn("changeColumnPosition", board.calls)

    def test_ensure_pipeline_board_accepts_the_migrated_board_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            board = _legacy_board()
            migrate_assessment_column(client=board)
            columns = [dict(column) for column in board.columns]
            board.calls.clear()

            self.assertEqual(ensure_pipeline_board(Path(temporary), client=board), 7)

            self.assertEqual([dict(column) for column in board.columns], columns)
            self.assertNotIn("addColumn", board.calls)
            self.assertNotIn("updateColumn", board.calls)
            self.assertNotIn("removeColumn", board.calls)

    def test_refuses_a_board_whose_layout_is_neither_known_one(self) -> None:
        board = _legacy_board()
        board.columns[0]["title"] = "Backlog"

        with self.assertRaises(BootstrapError) as raised:
            migrate_assessment_column(client=board)

        message = str(raised.exception)
        self.assertIn("Backlog", message)
        self.assertIn(", ".join(LEGACY_PIPELINE_COLUMNS), message)
        self.assertIn(", ".join(PIPELINE_COLUMNS), message)
        self.assertNotIn("addColumn", board.calls)

    def test_refuses_a_declined_reposition_instead_of_reporting_success(self) -> None:
        board = _legacy_board()

        def declined(method: str, **params: object) -> object:
            if method == "changeColumnPosition":
                board.calls.append(method)
                return False
            return Board.call(board, method, **params)

        board.call = declined  # type: ignore[method-assign]
        with self.assertRaisesRegex(BootstrapError, "did not move Assessment"):
            migrate_assessment_column(client=board)

    def test_a_retry_finishes_a_column_whose_add_response_was_lost(self) -> None:
        """The ambiguous write: Kanboard committed addColumn and the answer never came back."""
        board = _legacy_board()
        before = [dict(task) for task in board.tasks]

        def lost_reply(method: str, **params: object) -> object:
            result = Board.call(board, method, **params)
            if method == "addColumn":
                raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1)
            return result

        board.call = lost_reply  # type: ignore[method-assign]
        with self.assertRaisesRegex(BootstrapError, "unavailable"):
            migrate_assessment_column(client=board)
        self.assertEqual(
            [column["title"] for column in board.columns],
            [*LEGACY_PIPELINE_COLUMNS, "Assessment"],
        )

        board.call = lambda method, **params: Board.call(board, method, **params)  # type: ignore[method-assign,assignment]
        board.calls.clear()
        result = migrate_assessment_column(client=board)

        self.assertEqual(result["status"], "resumed")
        self.assertEqual([column["title"] for column in board.columns], list(PIPELINE_COLUMNS))
        self.assertEqual(board.tasks, before)
        # The existing column is finished, never a second one.
        self.assertNotIn("addColumn", board.calls)
        self.assertEqual([column["title"] for column in board.columns].count("Assessment"), 1)

    def test_a_retry_finishes_a_reposition_that_failed(self) -> None:
        board = _legacy_board()
        before = [dict(task) for task in board.tasks]

        def declined(method: str, **params: object) -> object:
            if method == "changeColumnPosition":
                board.calls.append(method)
                return False
            return Board.call(board, method, **params)

        board.call = declined  # type: ignore[method-assign]
        with self.assertRaises(BootstrapError):
            migrate_assessment_column(client=board)

        board.call = lambda method, **params: Board.call(board, method, **params)  # type: ignore[method-assign,assignment]
        result = migrate_assessment_column(client=board)

        self.assertEqual(result["status"], "resumed")
        self.assertEqual([column["title"] for column in board.columns], list(PIPELINE_COLUMNS))
        self.assertEqual(board.tasks, before)

    def test_a_reposition_whose_response_was_lost_retries_as_unchanged(self) -> None:
        """The board is already correct; the second run must recognise that, not repair it."""
        board = _legacy_board()

        def lost_reply(method: str, **params: object) -> object:
            result = Board.call(board, method, **params)
            if method == "changeColumnPosition":
                raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1)
            return result

        board.call = lost_reply  # type: ignore[method-assign]
        with self.assertRaisesRegex(BootstrapError, "unavailable"):
            migrate_assessment_column(client=board)

        board.call = lambda method, **params: Board.call(board, method, **params)  # type: ignore[method-assign,assignment]
        board.calls.clear()
        result = migrate_assessment_column(client=board)

        self.assertEqual(result["status"], "unchanged")
        self.assertEqual([column["title"] for column in board.columns], list(PIPELINE_COLUMNS))
        self.assertNotIn("addColumn", board.calls)
        self.assertNotIn("changeColumnPosition", board.calls)

    def test_a_resume_still_refuses_when_a_card_moved_underneath_it(self) -> None:
        board = _legacy_board()
        board.columns.append({"id": 7, "title": "Assessment"})

        def moves_a_card(method: str, **params: object) -> object:
            result = Board.call(board, method, **params)
            if method == "changeColumnPosition":
                board.tasks[0]["column_id"] = 3
            return result

        board.call = moves_a_card  # type: ignore[method-assign]
        with self.assertRaisesRegex(BootstrapError, "moved or lost"):
            migrate_assessment_column(client=board)

    def test_refuses_a_missing_board(self) -> None:
        board = Board()
        with self.assertRaisesRegex(BootstrapError, "does not exist"):
            migrate_assessment_column(client=board)


class BootstrapBoardTests(unittest.TestCase):
    # secretary-756: the four scenarios formerly here (idempotent ownership, refusing an
    # unowned matching unit, starting a foreign/legacy-CLI Orca ahead of ownership removal,
    # and a missing-executable error preceding any unit write) all called `_start_orca_service`,
    # which bootstrap no longer defines. Orca is host-owned and external (secretary-739/755):
    # bootstrap never installs, starts, or owns a `secretary-orca.service` unit, so none of
    # these scenarios has a current-contract equivalent. Deleted rather than rewritten.

    def test_creates_pipeline_schema_and_registry_lanes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instance = Path(temporary)
            projects = instance / "projects"
            projects.mkdir()
            (projects / "api.yaml").write_text("id: api\n", encoding="utf-8")
            (projects / "web.yaml").write_text(
                "id: web\norca_binding: web_runtime\n", encoding="utf-8"
            )
            board_state = instance / "state" / "board"
            board_state.mkdir(parents=True)
            (board_state / "cards.ndjson").write_text(
                '{"reference":"retired-1","swimlane":"retired_project"}\n',
                encoding="utf-8",
            )
            board = Board()

            self.assertEqual(ensure_pipeline_board(instance, client=board), 7)
            self.assertEqual([column["title"] for column in board.columns], list(PIPELINE_COLUMNS))
            self.assertEqual(
                [lane["name"] for lane in board.lanes],
                ["api", "retired_project", "web_runtime"],
            )
            calls = len(board.calls)

            self.assertEqual(ensure_pipeline_board(instance, client=board), 7)
            self.assertEqual(len(board.calls), calls + 3)
            self.assertEqual(
                [lane["name"] for lane in board.lanes],
                ["api", "retired_project", "web_runtime"],
            )

    def test_refuses_a_populated_board_with_another_layout_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instance = Path(temporary)
            board = Board()
            board.project = {"id": 7, "name": "Pipeline"}
            board.columns = [
                {"id": index, "title": title}
                for index, title in enumerate(("Backlog", *PIPELINE_COLUMNS[1:]), 1)
            ]

            def populated(method: str, **params: object) -> object:
                if method == "getAllTasks":
                    board.calls.append(method)
                    return [{"id": 3, "column_id": 1}]
                return Board.call(board, method, **params)

            board.call = populated  # type: ignore[method-assign]
            with self.assertRaises(BootstrapError) as raised:
                ensure_pipeline_board(instance, client=board)

            message = str(raised.exception)
            self.assertIn("Backlog", message)
            self.assertIn(PIPELINE_COLUMNS[0], message)
            self.assertEqual(board.columns[0]["title"], "Backlog")
            self.assertNotIn("updateColumn", board.calls)
            self.assertNotIn("removeColumn", board.calls)
            self.assertNotIn("addColumn", board.calls)

    def test_refuses_a_declined_column_rename_on_an_empty_board(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instance = Path(temporary)
            board = Board()
            board.project = {"id": 7, "name": "Pipeline"}
            board.columns = [
                {"id": index, "title": f"old-{index}"}
                for index in range(1, len(PIPELINE_COLUMNS) + 1)
            ]

            def declined(method: str, **params: object) -> object:
                if method == "updateColumn":
                    board.calls.append(method)
                    return False
                return Board.call(board, method, **params)

            board.call = declined  # type: ignore[method-assign]
            with self.assertRaisesRegex(BootstrapError, "did not rename"):
                ensure_pipeline_board(instance, client=board)

            self.assertEqual(board.columns[0]["title"], "old-1")
            self.assertNotIn("addSwimlane", board.calls)

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

    def test_refuses_to_remove_surplus_columns_when_only_closed_cards_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instance = Path(temporary)
            board = Board()
            board.project = {"id": 7, "name": "Pipeline"}
            board.columns = [{"id": index, "title": f"old-{index}"} for index in range(1, 9)]

            def closed_cards(method: str, **params: object) -> object:
                if method == "getAllTasks":
                    self.assertIn(params.get("status_id"), {0, 1})
                    return [{"id": 3, "is_active": 0}] if params.get("status_id") == 0 else []
                return Board.call(board, method, **params)

            board.call = closed_cards  # type: ignore[method-assign]
            with self.assertRaisesRegex(BootstrapError, "cards but an incompatible"):
                ensure_pipeline_board(instance, client=board)

    def test_refuses_a_populated_legacy_board_and_names_the_migration(self) -> None:
        """The pre-Assessment layout is refused like any other, but with a way out."""
        with tempfile.TemporaryDirectory() as temporary:
            instance = Path(temporary)
            board = _legacy_board()

            with self.assertRaises(BootstrapError) as raised:
                ensure_pipeline_board(instance, client=board)

            message = str(raised.exception)
            self.assertIn(", ".join(LEGACY_PIPELINE_COLUMNS), message)
            self.assertIn(", ".join(PIPELINE_COLUMNS), message)
            self.assertIn("board migrate-assessment", message)
            self.assertNotIn("addColumn", board.calls)

    def test_platform_uses_distribution_compose_and_ubuntu_fuse_packages(self) -> None:
        with (
            mock.patch("secretary.bootstrap.os.geteuid", return_value=0),
            mock.patch("secretary.bootstrap.shutil.which", side_effect=lambda name: None),
            mock.patch("secretary.bootstrap._docker_compose_available", return_value=False),
            mock.patch("secretary.bootstrap._compose_package", return_value="docker-compose-v2"),
            mock.patch("secretary.bootstrap._ensure_docker_ready"),
            mock.patch("secretary.bootstrap._install_orca") as install_orca,
            mock.patch("secretary.bootstrap._run") as run,
            mock.patch("secretary.bootstrap.write_text_atomic"),
            mock.patch("secretary.bootstrap.Path.mkdir"),
            mock.patch("secretary.bootstrap.Path.chmod"),
        ):
            _install_platform(dry_run=False)

        self.assertIn(
            [
                "apt-get", "install", "--yes", "curl", "fuse", "libnss3", "libgtk-3-0t64",
                "libgbm1", "libasound2t64", "xvfb", "docker.io", "docker-compose-v2",
            ],
            [call.args[0] for call in run.call_args_list],
        )
        install_orca.assert_called_once_with()

    def test_clean_bootstrap_installs_pinned_runtime_despite_legacy_user_cli(self) -> None:
        with (
            mock.patch("secretary.bootstrap.os.geteuid", return_value=0),
            mock.patch("secretary.bootstrap.shutil.which", return_value="/usr/bin/docker"),
            mock.patch("secretary.bootstrap._docker_compose_available", return_value=True),
            mock.patch("secretary.bootstrap.pinned_orca_executable", return_value=None),
            mock.patch("secretary.bootstrap._ensure_docker_ready"),
            mock.patch("secretary.bootstrap._install_orca") as install_orca,
            mock.patch("secretary.bootstrap._run"),
        ):
            _install_platform(dry_run=False, runtime_user="existing-dedicated-user")

        install_orca.assert_called_once_with()

    def test_host_contract_accepts_only_ubuntu_2404(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "os-release"
            release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n', encoding="utf-8")
            _host_supported(release)
            release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
            with self.assertRaisesRegex(BootstrapError, "Ubuntu 24.04 only"):
                _host_supported(release)
            release.write_text('ID=debian\nVERSION_ID="12"\n', encoding="utf-8")
            with self.assertRaisesRegex(BootstrapError, "Ubuntu 24.04 only"):
                _host_supported(release)

    def test_bootstrap_generates_usable_runtime_and_ignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            board = Board()

            def clone(_remote: str, directory: Path, **_kwargs: object) -> str:
                directory.mkdir()
                (directory / ".git" / "info").mkdir(parents=True)
                (directory / "instance.yaml").write_text(
                    "version: 1\nname: bootstrap\ndata_dir: " + str(directory.parent / "data")
                    + "\noffsite:\n  instance_remote: git@example.invalid:bootstrap/instance\n"
                    + "host:\n  unit_prefix: secretary-\n",
                    encoding="utf-8",
                )
                return "cloned private instance remote"

            args = SimpleNamespace(
                instance_dir=str(target), instance_remote="remote", installation_user="dev", dry_run=False,
            )
            with (
                mock.patch("secretary.bootstrap.os.geteuid", return_value=0),
                mock.patch("secretary.bootstrap._ensure_installation_user"),
                mock.patch("secretary.bootstrap._clone_or_reuse", side_effect=clone),
                mock.patch("secretary.bootstrap._install_platform"),
                mock.patch("secretary.bootstrap._set_installation_owner"),
                mock.patch("secretary.bootstrap._compose_file"),
                mock.patch("secretary.bootstrap._run"),
                mock.patch("secretary.bootstrap.KanboardClient", return_value=board),
            ):
                self.assertEqual(bootstrap(args), 0)

            runtime = (target / "runtime.env").read_text(encoding="utf-8")
            self.assertNotIn("KANBOARD_API_USER=", runtime)
            self.assertNotIn("KANBOARD_API_TOKEN=", runtime)
            self.assertNotIn("KANBOARD_BOOTSTRAP_TOKEN", runtime)
            self.assertNotIn("KANBOARD_IMAGE", runtime)
            self.assertNotIn("KANBOARD_ADMIN_PASSWORD", runtime)
            self.assertEqual((target / "runtime.env").stat().st_mode & 0o777, 0o600)
            self.assertTrue((target / BOOTSTRAP_STAMP).is_file())
            exclude = (target / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn(f"/{BOOTSTRAP_STAMP}", exclude)
            self.assertIn("/runtime.env", exclude)
            self.assertIn("/board-transport.env", exclude)
            transport = (target / "board-transport.env").read_text(encoding="utf-8")
            self.assertIn("KANBOARD_API_USER=jsonrpc\n", transport)
            compose = (target.parent / "compose.yml")
            from secretary.bootstrap import _compose_file
            _compose_file(compose)
            contents = compose.read_text(encoding="utf-8")
            self.assertIn("API_AUTHENTICATION_TOKEN: ${KANBOARD_API_TOKEN}", contents)
            self.assertIn("image: kanboard/kanboard:v1.2.46", contents)

    def test_rejects_unsupported_host_before_creating_user_or_checkout(self) -> None:
        args = SimpleNamespace(
            instance_dir="/tmp/instance", instance_remote="remote", installation_user="dev", dry_run=False,
        )
        with (
            mock.patch("secretary.bootstrap.os.geteuid", return_value=0),
            mock.patch("secretary.bootstrap._host_supported", side_effect=BootstrapError("unsupported")),
            mock.patch("secretary.bootstrap._ensure_installation_user") as ensure_user,
            mock.patch("secretary.bootstrap._clone_or_reuse") as clone,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(bootstrap(args), 1)
        ensure_user.assert_not_called()
        clone.assert_not_called()


if __name__ == "__main__":
    unittest.main()
