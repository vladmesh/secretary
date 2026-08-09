"""Privilege-boundary regressions for instance repository lifecycle writes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import state_repo


class StateRepoPrivilegeTests(unittest.TestCase):
    def test_root_delegates_git_to_the_instance_owner_before_git_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
            account = SimpleNamespace(pw_name="runtime")
            with (
                mock.patch("secretary.state_repo.os.getuid", return_value=0),
                mock.patch("secretary.state_repo.pwd.getpwuid", return_value=account),
                mock.patch("secretary.state_repo.subprocess.run", return_value=result) as run,
            ):
                state_repo.git(instance, ["status", "--porcelain"], label="test")

        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["runuser", "--user", "runtime", "--"])
        self.assertEqual(command[4], "env")
        self.assertIn("GIT_TERMINAL_PROMPT=0", command)
        self.assertIn("GIT_SSH_COMMAND=ssh -o BatchMode=yes", command)
        self.assertIn("git", command)
        self.assertIn(f"safe.directory={instance.resolve()}", command)
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_root_boundary_never_runs_runtime_hooks_or_config_as_root(self) -> None:
        """The pusher shares this boundary before any Git subcommand starts."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
            account = SimpleNamespace(pw_name="runtime")
            with (
                mock.patch("secretary.state_repo.os.getuid", return_value=0),
                mock.patch("secretary.state_repo.pwd.getpwuid", return_value=account),
                mock.patch("secretary.state_repo.subprocess.run", return_value=result) as run,
            ):
                state_repo.run_git(instance, ["push", "origin", "HEAD:main"], label="test")

        command = run.call_args.args[0]
        git = command.index("git")
        self.assertEqual(command[:4], ["runuser", "--user", "runtime", "--"])
        self.assertEqual(command[git + 1:git + 5], [
            "-c", f"safe.directory={instance.resolve()}", "-c", "core.hooksPath=/dev/null",
        ])

    def test_non_root_calls_git_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch("secretary.state_repo.subprocess.run", return_value=result) as run:
                state_repo.git(Path(tmp), ["status", "--porcelain"], label="test")
        self.assertEqual(run.call_args.args[0][0], "git")


if __name__ == "__main__":
    unittest.main()
