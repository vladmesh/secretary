"""What the production tick spawns and waits for is bounded by the tick itself, not by its unit.

The production dispatcher unit sets ``KillMode=process`` so the local-pty heads a tick launches
outlive it (secretary-1699). That also retires the control-group kill as the cleanup of anything
else a tick left behind. A plain ``subprocess.run`` timeout kills only the direct child, so the
two shell-running children of a tick — the host runner's ``bash -lc`` gate and adapter commands
and the head-health probe's ``sh -c`` — now run in their own process group, and a timeout kills
that whole group.
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

from secretary import _proc, head_health
from secretary.dispatch import host as dispatcher_host_module
from secretary.dispatch.host import CommandHostRuntime
from secretary.dispatch.types import HostError
from tests.fakes.dispatcher import FakeCatalog

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
