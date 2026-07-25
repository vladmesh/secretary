from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.bootstrap import (
    BOOTSTRAP_STAMP,
    PIPELINE_COLUMNS,
    BootstrapError,
    _host_supported,
    _install_platform,
    bootstrap,
    ensure_pipeline_board,
)


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
            # Kanboard's default is open cards; status_id=0 selects closed.
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
    def legacy_orca_bootstrap_uses_catalogue_and_records_idempotent_ownership(self) -> None:
        class Installer:
            def __init__(self) -> None:
                self.files: dict[str, bytes] = {}
                self.calls: list[tuple[str, str]] = []

            def installed(self, name: str) -> bytes | None:
                return self.files.get(name)

            def install(self, unit) -> None:
                self.files[unit.name] = unit.content
                self.calls.append(("install", unit.name))

            def daemon_reload(self) -> None:
                self.calls.append(("reload", ""))

            def enable(self, name: str) -> None:
                self.calls.append(("enable", name))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            data = root / "data"
            (instance / "instance.yaml").write_text(
                "version: 1\nname: bootstrap\ndata_dir: " + str(data)
                + "\noffsite:\n  instance_remote: git@example.invalid:bootstrap/instance\n"
                + "host:\n  unit_prefix: secretary-\n",
                encoding="utf-8",
            )
            installer = Installer()
            account = SimpleNamespace(pw_dir="/home/operator")
            with (
                mock.patch("secretary.bootstrap.os.geteuid", return_value=0),
                mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
                mock.patch("secretary.host_apply.find_orca_executable", return_value=Path("/usr/local/bin/orca")),
                mock.patch("secretary.host_apply._is_executable", return_value=True),
                mock.patch("secretary.bootstrap.SystemdUnitInstaller", return_value=installer),
                mock.patch("secretary.bootstrap._wait_for_orca"),
            ):
                _start_orca_service("operator", instance)
                _start_orca_service("operator", instance)

            self.assertEqual(installer.calls.count(("install", "secretary-orca.service")), 1)
            self.assertEqual(installer.calls.count(("enable", "secretary-orca.service")), 2)
            self.assertIn(b"User=operator", installer.files["secretary-orca.service"])
            self.assertIn(b"ExecStart=/usr/local/bin/orca", installer.files["secretary-orca.service"])
            managed = (data / "host-managed.json").read_text(encoding="utf-8")
            self.assertIn('"systemd:unit:secretary-orca.service"', managed)

    def legacy_orca_bootstrap_refuses_matching_unowned_unit(self) -> None:
        class Installer:
            def __init__(self) -> None:
                self.files: dict[str, bytes] = {}
                self.calls: list[tuple[str, str]] = []

            def installed(self, name: str) -> bytes | None:
                return self.files.get(name)

            def install(self, unit) -> None:
                self.files[unit.name] = unit.content
                self.calls.append(("install", unit.name))

            def daemon_reload(self) -> None:
                self.calls.append(("reload", ""))

            def enable(self, name: str) -> None:
                self.calls.append(("enable", name))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            data = root / "data"
            (instance / "instance.yaml").write_text(
                "version: 1\nname: bootstrap\ndata_dir: " + str(data)
                + "\noffsite:\n  instance_remote: git@example.invalid:bootstrap/instance\n"
                + "host:\n  unit_prefix: secretary-\n",
                encoding="utf-8",
            )
            installer = Installer()
            account = SimpleNamespace(pw_dir="/home/operator")
            with (
                mock.patch("secretary.bootstrap.os.geteuid", return_value=0),
                mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
                mock.patch("secretary.host_apply.find_orca_executable", return_value=Path("/home/operator/.local/bin/orca")),
                mock.patch("secretary.host_apply._is_executable", return_value=True),
                mock.patch("secretary.bootstrap.SystemdUnitInstaller", return_value=installer),
                mock.patch("secretary.bootstrap._wait_for_orca"),
            ):
                _start_orca_service("operator", instance)
                (data / "host-managed.json").unlink()
                with self.assertRaisesRegex(BootstrapError, "not owned by this instance"):
                    _start_orca_service("operator", instance)

            self.assertEqual(installer.calls.count(("install", "secretary-orca.service")), 1)
            self.assertFalse((data / "host-managed.json").exists())

    def legacy_user_orca_starts_before_foreign_ownership_is_removed(self) -> None:
        class Installer:
            def __init__(self) -> None:
                self.files: dict[str, bytes] = {}
                self.calls: list[tuple[str, str]] = []

            def installed(self, name: str) -> bytes | None:
                return self.files.get(name)

            def install(self, unit) -> None:
                self.files[unit.name] = unit.content
                self.calls.append(("install", unit.name))

            def daemon_reload(self) -> None:
                self.calls.append(("reload", ""))

            def enable(self, name: str) -> None:
                self.calls.append(("enable", name))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            data = root / "data"
            legacy = root / "home" / ".local" / "bin" / "orca"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("#!/bin/sh\n", encoding="utf-8")
            legacy.chmod(0o755)
            (instance / "instance.yaml").write_text(
                "version: 1\nname: bootstrap\ndata_dir: " + str(data)
                + "\noffsite:\n  instance_remote: git@example.invalid:bootstrap/instance\n"
                + "host:\n  unit_prefix: secretary-\n  foreign_units:\n    - secretary-orca.service\n",
                encoding="utf-8",
            )
            installer = Installer()
            account = SimpleNamespace(pw_dir=str(root / "home"))
            with (
                mock.patch("secretary.bootstrap.os.geteuid", return_value=0),
                mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
                mock.patch("secretary.bootstrap.SystemdUnitInstaller", return_value=installer),
                mock.patch("secretary.bootstrap._wait_for_orca"),
            ):
                _start_orca_service("operator", instance)

            self.assertIn(("install", "secretary-orca.service"), installer.calls)
            self.assertIn(("enable", "secretary-orca.service"), installer.calls)
            self.assertIn(f"ExecStart={legacy}".encode(), installer.files["secretary-orca.service"])
            self.assertFalse((data / "host-managed.json").exists())

    def legacy_orca_runtime_error_precedes_unit_write_and_ownership(self) -> None:
        class Installer:
            def install(self, unit) -> None:
                raise AssertionError(f"unexpected install of {unit.name}")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            data = root / "data"
            (instance / "instance.yaml").write_text(
                "version: 1\nname: bootstrap\ndata_dir: " + str(data)
                + "\noffsite:\n  instance_remote: git@example.invalid:bootstrap/instance\n"
                + "host:\n  unit_prefix: secretary-\n",
                encoding="utf-8",
            )
            account = SimpleNamespace(pw_dir=str(root / "home"))
            with (
                mock.patch("secretary.bootstrap.os.geteuid", return_value=0),
                mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
                mock.patch("secretary.bootstrap.SystemdUnitInstaller", return_value=Installer()),
                mock.patch("secretary.bootstrap._wait_for_orca"),
            ):
                with self.assertRaisesRegex(BootstrapError, "Orca executable for operator is unavailable"):
                    _start_orca_service("operator", instance)
            self.assertFalse((data / "host-managed.json").exists())

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
                    self.assertEqual(params.get("status_id"), 0)
                    return [{"id": 3, "is_active": 0}]
                return Board.call(board, method, **params)

            board.call = closed_cards  # type: ignore[method-assign]
            with self.assertRaisesRegex(BootstrapError, "cards but an incompatible"):
                ensure_pipeline_board(instance, client=board)

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
