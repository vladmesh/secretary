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
        self.assertEqual(command[4], "git")
        self.assertNotIn("safe.directory", command)

    def test_non_root_calls_git_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch("secretary.state_repo.subprocess.run", return_value=result) as run:
                state_repo.git(Path(tmp), ["status", "--porcelain"], label="test")
        self.assertEqual(run.call_args.args[0][0], "git")


if __name__ == "__main__":
    unittest.main()
