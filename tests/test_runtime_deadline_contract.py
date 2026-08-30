"""The small runtime-component proof that production deadline defaults remain wired.

Expiry and lifecycle regressions elsewhere in the runtime-component suite inject short bounds.
This is intentionally the only named proof that starts the production local-PTY runtime without
overrides and reads back the deadline its supervisor actually admitted.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from secretary.dispatcher_watchdog import head_process_status
from triggered_agents.runtime.head import HeadSpec, TaskRef
from triggered_agents.runtime.head.local_pty import protocol
from triggered_agents.runtime.head.local_pty.client import SupervisorClient, spawn_head
from triggered_agents.runtime.local_pty_head import (
    DELIVERY_GRACE_SECONDS,
    STOP_CONFIRM_SECONDS,
    LocalPtyHeadRuntime,
)

REPO = Path(__file__).resolve().parents[1]
CHILD = REPO / "tests" / "fixtures" / "local_pty_child.py"
CHILD_COMMAND = f"{sys.executable} -u {CHILD}"
SPEC = HeadSpec(profile_id="codex-worker", adapter="codex", effort="high", codex_mode="tui")


def _kill(pid: int, *, group: bool = False) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(pid, signal.SIGKILL) if group else os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat[stat.rfind(")") + 2 :].split()[0] != "Z"


class ShippedRuntimeDeadlineContractTests(unittest.TestCase):
    """Production defaults are exercised here, not in the short-bound expiry regressions."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="lp-default-contract-"))
        self.workspace = Path(tempfile.mkdtemp(prefix="lp-default-workspace-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.addCleanup(self._reap)
        self._pids: list[tuple[int, int]] = []

    def _reap(self) -> None:
        for head, supervisor in self._pids:
            _kill(head, group=True)
            _kill(head)
            _kill(supervisor)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and any(_alive(head) for head, _supervisor in self._pids):
            time.sleep(0.02)
        self.assertFalse(
            any(_alive(head) for head, _supervisor in self._pids),
            "the default-deadline contract left a head process behind",
        )

    def _remember(self, run_dir: Path) -> None:
        try:
            head = int(json.loads((run_dir / protocol.PID_FILE_NAME).read_text(encoding="utf-8"))["pid"])
            supervisor = int((run_dir / protocol.SUPERVISOR_PID_NAME).read_text(encoding="utf-8"))
        except (KeyError, OSError, TypeError, ValueError):
            self.fail("the production runtime did not leave its process identities")
        self._pids.append((head, supervisor))

    def test_spawn_head_uses_the_shipped_delivery_deadline_when_no_override_is_supplied(self) -> None:
        handle = spawn_head(
            root=self.root,
            run_id="substrate-default",
            role="worker",
            task="secretary-1511",
            command=CHILD_COMMAND,
            quiet_seconds=0.1,
        )
        self._pids.append((handle.head_pid, handle.supervisor_pid))

        with handle.connect() as client:
            admitted = client.send_input("default\n")
            self.assertTrue(admitted["ok"], admitted)
            self.assertEqual(admitted["delivery"]["timeout_seconds"], protocol.INPUT_DELIVERY_SECONDS)
            delivered = client.wait_for_delivery(admitted["delivery"]["id"], timeout=2.0)

        self.assertEqual(delivered["state"], protocol.DELIVERY_COMPLETE)

    def test_local_pty_runtime_forwards_the_shipped_deadlines_to_its_production_substrate(self) -> None:
        forwarded: list[float | None] = []

        def production_spawn(**kwargs):
            forwarded.append(kwargs["delivery_seconds"])
            return spawn_head(**kwargs)

        runtime = LocalPtyHeadRuntime(
            self.root,
            head_process_status=head_process_status,
            spawn=production_spawn,
        )
        receipt = runtime.start(
            SPEC,
            str(self.workspace),
            TaskRef.card("secretary-1511", document=str(self.workspace / "TASK.md")),
            command=CHILD_COMMAND,
            title="deadline default contract",
            role="worker",
            quiet_seconds=0.1,
        )
        self.assertTrue(receipt.ok, receipt.reason)
        self.assertEqual(forwarded, [None], "the runtime overrode the substrate's shipped deadline")
        self._remember(self.root / receipt.run.run_id)

        with SupervisorClient.connect(self.root / receipt.run.run_id / protocol.SOCKET_NAME) as client:
            admitted = client.send_input("runtime default\n")
            self.assertTrue(admitted["ok"], admitted)
            self.assertEqual(admitted["delivery"]["timeout_seconds"], protocol.INPUT_DELIVERY_SECONDS)

        self.assertEqual(
            runtime.delivery_wait_for(protocol.INPUT_DELIVERY_SECONDS),
            protocol.INPUT_DELIVERY_SECONDS + DELIVERY_GRACE_SECONDS,
        )
        self.assertEqual(runtime._stop_timeout, STOP_CONFIRM_SECONDS)


if __name__ == "__main__":
    unittest.main()
