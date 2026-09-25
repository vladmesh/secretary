"""secretary-1703: the head view against a real supervisor, while its head runs and after it ends.

The hermetic half is `test_web_head_view`. This half starts a real supervisor on a real pty
under the card's own run id and reads its journal through the web app while running and after
exit. Every process started here is taken back.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import unittest
from pathlib import Path

from secretary.runtime.head.local_pty.client import HeadHandle, spawn_head
from secretary.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from secretary.webproto import head_view as head_reads
from tests.web_head_view_fixtures import REF, SECRET, TOKEN, WORKER, HeadViewFixture, _run

REPO = Path(__file__).resolve().parents[1]
CHILD_COMMAND = f"{sys.executable} -u {REPO / 'tests' / 'fixtures' / 'local_pty_child.py'}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat[stat.rfind(")") + 2 :].split()[0] != "Z"


class ARunningHeadAndThenAFinishedOneTests(HeadViewFixture):
    def setUp(self) -> None:
        super().setUp()
        self.records[REF]["worker_head_run"] = _run(REF, WORKER, role="worker", runtime=LOCAL_PTY_RUNTIME)
        self.write_production()

    def _start(self) -> HeadHandle:
        handle = spawn_head(
            root=self.root,
            run_id=WORKER,
            role="worker",
            task=f"card:{REF}",
            command=CHILD_COMMAND,
            quiet_seconds=0.4,
        )

        def reap() -> None:
            for pid in (handle.head_pid, handle.supervisor_pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

        self.addCleanup(reap)
        return handle

    def _await(self, predicate, *, timeout: float = 10.0, message: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(message)

    def _document(self) -> dict:
        response = self.app().handle("GET", f"/api/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(response.status, 200)
        return json.loads(response.body)

    def test_the_view_shows_the_journal_while_running_and_after_exit(self) -> None:
        handle = self._start()
        client = handle.connect()
        self.addCleanup(client.close)
        self.assertTrue(client.send_input(f"<script>x</script>\ntoken {TOKEN}\n")["ok"])
        self._await(
            lambda: b"ECHO token" in client.read_output()["bytes_data"],
            message="the head never echoed its input",
        )
        live = self._document()
        self.assertEqual(live["head"]["state"], head_reads.RUNNING)
        self.assertNotIn("transcript", live)
        self.assertIn("input.accepted", [record.get("kind") for record in live["journal"]["tail"]])
        status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(status, 200)
        self.assertNotIn("Terminal output", page)
        for body in (page, json.dumps(live)):
            self.assertNotIn(TOKEN, body)
            self.assertNotIn(SECRET, body)
            self.assertNotIn("ECHO <script>", body)
        self.assertTrue(client.status()["alive"])

        self.assertTrue(client.send_input("quit\n")["ok"])
        self._await(lambda: not _alive(handle.supervisor_pid), message="the supervisor never let go")
        finished = self._document()
        self.assertEqual(finished["head"]["state"], head_reads.FINISHED)
        self.assertNotIn("transcript", finished)
        self.assertIn("run.exited", [record.get("kind") for record in finished["journal"]["tail"]])
        self.assertNotIn(TOKEN, json.dumps(finished))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
