from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import bootstrap as bootstrap_module
from secretary.bootstrap import (
    BOOTSTRAP_STAMP,
    BootstrapError,
    _host_supported,
    _install_platform,
    bootstrap,
)
from secretary.runtime_env import read_runtime_env


class BootstrapTests(unittest.TestCase):
    # secretary-756: the four scenarios formerly here (idempotent ownership, refusing an
    # unowned matching unit, starting a foreign/legacy-CLI Orca ahead of ownership removal,
    # and a missing-executable error preceding any unit write) all called `_start_orca_service`,
    # which bootstrap no longer defines. Orca is host-owned and external (secretary-739/755):
    # bootstrap never installs, starts, or owns a `secretary-orca.service` unit, so none of
    # these scenarios has a current-contract equivalent. Deleted rather than rewritten.

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
                "apt-get",
                "install",
                "--yes",
                "curl",
                "fuse",
                "libnss3",
                "libgtk-3-0t64",
                "libgbm1",
                "libasound2t64",
                "xvfb",
                "docker.io",
                "docker-compose-v2",
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

    def _clone(self, _remote: str, directory: Path, **_kwargs: object) -> str:
        directory.mkdir()
        subprocess.run(["git", "init", "--quiet", str(directory)], check=True)
        subprocess.run(["git", "-C", str(directory), "config", "user.name", "Test"], check=True)
        subprocess.run(
            ["git", "-C", str(directory), "config", "user.email", "test@example.invalid"], check=True
        )
        (directory / "instance.yaml").write_text(
            "version: 1\nname: bootstrap\ndata_dir: "
            + str(directory.parent / "data")
            + "\noffsite:\n  instance_remote: git@example.invalid:bootstrap/instance\n"
            + "host:\n  unit_prefix: secretary-\n",
            encoding="utf-8",
        )
        return "cloned private instance remote"

    def _bootstrap(self, target: Path, *, dry_run: bool = False) -> tuple[int, mock.Mock]:
        """Run bootstrap with the host edges stubbed, recording the board-store steps in order."""
        args = SimpleNamespace(
            instance_dir=str(target),
            instance_remote="remote",
            installation_user="dev",
            dry_run=dry_run,
        )
        steps = mock.Mock()
        clone = mock.Mock(
            side_effect=lambda remote, directory, **kwargs: (
                "reused checkpoint checkout" if directory.exists() else self._clone(remote, directory)
            )
        )
        refuse_kanboard = AssertionError("bootstrap reached Kanboard")
        with (
            mock.patch("secretary.bootstrap.os.geteuid", return_value=0),
            mock.patch("secretary.bootstrap._host_supported"),
            mock.patch("secretary.bootstrap._ensure_installation_user"),
            mock.patch("secretary.bootstrap._clone_or_reuse", clone),
            mock.patch("secretary.bootstrap._install_platform", steps.install_platform),
            mock.patch("secretary.bootstrap._set_installation_owner"),
            mock.patch("secretary.bootstrap.provision_board_store", steps.provision),
            mock.patch("secretary.bootstrap.migrate_instance", steps.migrate),
            mock.patch("secretary.bootstrap.verify_board_store_roles", steps.verify),
            mock.patch("secretary.bootstrap._run", side_effect=refuse_kanboard),
            mock.patch("secretary.tasks.KanboardClient.for_instance", side_effect=refuse_kanboard),
            mock.patch("secretary.tasks.KanboardClient.call", side_effect=refuse_kanboard),
            mock.patch("builtins.print"),
        ):
            code = bootstrap(args)
        return code, steps

    def test_bootstrap_provisions_migrates_and_verifies_the_store_with_no_kanboard_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"

            code, steps = self._bootstrap(target)

            self.assertEqual(code, 0)
            # The whole board-side sequence, in order: nothing starts, waits for or shapes Kanboard.
            self.assertEqual(
                steps.mock_calls,
                [
                    mock.call.install_platform(dry_run=False, runtime_user="dev"),
                    mock.call.provision(target, allow_create=True),
                    mock.call.migrate(target),
                    mock.call.verify(target),
                ],
            )
            self.assertFalse((target / "board-transport.env").exists())
            gitignore = target / ".gitignore"
            self.assertNotIn(
                "board-transport.env", gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
            )
            self.assertTrue((target / BOOTSTRAP_STAMP).is_file())
            exclude = (target / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn(f"/{BOOTSTRAP_STAMP}", exclude)
            self.assertIn("/runtime.env", exclude)
            for removed in (
                "ensure_pipeline_board",
                "migrate_assessment_column",
                "_wait_for_kanboard",
                "_compose_file",
                "KANBOARD_IMAGE",
            ):
                self.assertFalse(hasattr(bootstrap_module, removed), removed)

    def test_a_fresh_bootstrap_leaves_the_installation_selecting_postgres(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"

            code, _steps = self._bootstrap(target)

            self.assertEqual(code, 0)
            runtime = target / "runtime.env"
            self.assertEqual(runtime.read_text(encoding="utf-8"), "SECRETARY_CARD_BACKEND=postgres\n")
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o600)
            # The same validated reader the instance-bound CLI and the units' environment go through.
            self.assertEqual(read_runtime_env(target)["SECRETARY_CARD_BACKEND"], "postgres")

    def test_a_rerun_keeps_runtime_lines_and_names_the_backend_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            self.assertEqual(self._bootstrap(target)[0], 0)
            runtime = target / "runtime.env"
            runtime.write_text(
                "# operator note\nEXAMPLE_TOKEN=kept\nSECRETARY_CARD_BACKEND=kanboard\n", encoding="utf-8"
            )

            self.assertEqual(self._bootstrap(target)[0], 0)

            self.assertEqual(
                runtime.read_text(encoding="utf-8"),
                "# operator note\nEXAMPLE_TOKEN=kept\nSECRETARY_CARD_BACKEND=postgres\n",
            )

    def test_a_preview_writes_no_runtime_file_and_touches_no_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"

            code, steps = self._bootstrap(target, dry_run=True)

            self.assertEqual(code, 0)
            self.assertEqual(steps.mock_calls, [])
            self.assertFalse((target / "runtime.env").exists())

    def test_rejects_unsupported_host_before_creating_user_or_checkout(self) -> None:
        args = SimpleNamespace(
            instance_dir="/tmp/instance",
            instance_remote="remote",
            installation_user="dev",
            dry_run=False,
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
