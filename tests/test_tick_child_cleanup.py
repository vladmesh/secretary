"""What the production tick spawns and waits for is bounded by the tick itself, not by its unit.

The production dispatcher unit sets ``KillMode=process`` so the local-pty heads a tick launches
outlive it (secretary-1699). That also retires the control-group kill as the cleanup of anything
else a tick left behind. A plain ``subprocess.run`` timeout kills only the direct child, so the
children a tick waits on with a timeout — the host runner's ``bash -lc`` gate and adapter
commands, the head-health probe's ``sh -c`` and instance/project Git with its remote helper — run
through ``_proc.run_isolated`` in their own process group, and a timeout kills that whole group.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from secretary import _proc, head_health, state_repo
from secretary.dispatch import host as dispatcher_host_module
from secretary.dispatch.host import CommandHostRuntime
from secretary.dispatch.types import HostError
from tests.fakes.dispatcher import FakeCatalog

# A remote helper Git forks into its own process group for `hang::` URLs: it records its pid and
# hangs, as a `git-remote-https` stuck in a transport operation does.
HANGING_REMOTE_HELPER = "#!/bin/sh\necho $$ > {pid_file}\nexec sleep 60\n"

# A shell that leaves a long-lived descendant behind, records its pid, and then hangs on it.
HANGING_SHELL = "sleep 60 & echo $! > {pid_file}; wait"


def _gone(pid: int) -> bool:
    for _ in range(200):
        if not Path(f"/proc/{pid}").exists():
            return True
        time.sleep(0.01)
    return False


class TickChildCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pid_file = Path(self.tmp.name) / "descendant.pid"

    def descendant(self) -> int:
        pid = int(self.pid_file.read_text(encoding="utf-8"))
        self.addCleanup(_kill_quietly, pid)
        return pid

    def test_a_timed_out_host_shell_takes_its_descendants_with_it(self):
        """The local gate's `bash -lc` and adapter setup: the test processes under the shell die."""
        host = CommandHostRuntime(FakeCatalog(), Path(self.tmp.name), mode="real")
        command = HANGING_SHELL.format(pid_file=self.pid_file)
        with (
            mock.patch.object(dispatcher_host_module, "HOST_COMMAND_TIMEOUT_SECONDS", 0.5),
            self.assertRaises(HostError) as caught,
        ):
            host.run_capture(["bash", "-lc", command], "local gate", cwd=Path(self.tmp.name))
        self.assertIn("timed out", str(caught.exception))
        self.assertTrue(_gone(self.descendant()), "the gate shell's descendant outlived its timeout")

    def test_a_timed_out_head_health_probe_takes_the_provider_cli_with_it(self):
        """The probe is `sh -c <registry probe>`; the provider CLI under it must not survive."""
        probe = HANGING_SHELL.format(pid_file=self.pid_file)
        with mock.patch.object(head_health, "PROBE_TIMEOUT_SECONDS", 0.5):
            readiness = head_health.run_probe("openai-sub", probe, time.time())
        self.assertEqual(readiness.status, "unknown")
        self.assertTrue(_gone(self.descendant()), "the probe's descendant outlived its timeout")

    def test_a_timed_out_git_takes_its_remote_helper_with_it(self):
        """`run_git` (managed project fetch/push, checkpoint push): Git's remote helper must die too."""
        helper_dir = Path(self.tmp.name) / "bin"
        helper_dir.mkdir()
        helper = helper_dir / "git-remote-hang"
        helper.write_text(HANGING_REMOTE_HELPER.format(pid_file=self.pid_file), encoding="utf-8")
        helper.chmod(0o755)
        path = f"{helper_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        with (
            mock.patch.dict(os.environ, {"PATH": path}),
            self.assertRaises(state_repo.StateRepoError) as caught,
        ):
            state_repo.run_git(Path(self.tmp.name), ["ls-remote", "hang::remote"], label="fetch", timeout=1)
        self.assertIn("fetch failed", str(caught.exception))
        self.assertIn("timed out", str(caught.exception))
        self.assertTrue(_gone(self.descendant()), "Git's remote helper outlived the Git timeout")

    def test_a_crossing_whose_group_cannot_be_signalled_still_returns_a_bounded_timeout(self):
        """`runuser` as the group leader, and `killpg` refused (EPERM): no raise, no unbounded reap.

        A root tick crosses to the runtime user through `runuser`, which keeps its child in the
        group, and root may signal every member. EPERM is what a caller gets when it may signal no
        member at all; the timeout must still come back as the same bounded Git error.
        """
        runuser_dir = Path(self.tmp.name) / "bin"
        runuser_dir.mkdir()
        runuser = runuser_dir / "runuser"
        # Stands in for `runuser --user X -- env ... git ...`: drop everything up to `--`, exec the rest.
        crossed = Path(self.tmp.name) / "crossed"
        runuser.write_text(
            f'#!/bin/sh\ntouch {crossed}\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n',
            encoding="utf-8",
        )
        runuser.chmod(0o755)
        path = f"{runuser_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        hanging_git = ["sh", "-c", HANGING_SHELL.format(pid_file=self.pid_file)]
        started = time.monotonic()
        with (
            mock.patch.dict(os.environ, {"PATH": path}),
            mock.patch("secretary.state_repo.os.getuid", return_value=0),
            mock.patch("secretary.state_repo.pwd.getpwuid", return_value=mock.Mock(pw_name="runtime")),
            mock.patch("secretary.state_repo.git_command", return_value=hanging_git),
            mock.patch.object(_proc.os, "killpg", side_effect=PermissionError(1, "Operation not permitted")),
            mock.patch.object(_proc, "_REAP_GRACE_SECONDS", 0.5),
            self.assertRaises(state_repo.StateRepoError) as caught,
        ):
            state_repo.run_git(
                Path(self.tmp.name),
                ["fetch"],
                label="fetch",
                timeout=0.5,
                child=state_repo.GitChildIdentity(12345, 12345, "runtime"),
            )
        self.assertIn("timed out", str(caught.exception))
        self.assertTrue(crossed.exists(), "the Git child did not cross through runuser")
        # The member this caller could not signal is left running, never waited on.
        self.descendant()
        self.assertLess(time.monotonic() - started, 10)

    def test_a_descendant_that_left_the_group_cannot_hold_the_reap_open(self):
        """Only a `setsid` escapee survives the group kill; it may not turn the timeout unbounded."""
        script = f"setsid sleep 60 & echo $! > {self.pid_file}; wait"
        started = time.monotonic()
        with (
            mock.patch.object(_proc, "_REAP_GRACE_SECONDS", 0.5),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            _proc.run_isolated(["bash", "-c", script], timeout=0.5)
        self.descendant()
        self.assertLess(time.monotonic() - started, 10)


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


if __name__ == "__main__":
    unittest.main()
