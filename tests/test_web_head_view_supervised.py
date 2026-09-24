"""secretary-1703: the head view against a real supervisor, while its head runs and after it ends.

The hermetic half is `test_web_head_view`. This half starts a real supervisor on a real pty -- the
trivial child `test_local_pty_supervisor` uses -- under the card's own run id, and reads it through
the web app exactly as an operator would: the live tail while the head runs, the kept tail once it
has ended, and the journal both times. Every process started here is taken back.
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

    def test_the_view_follows_a_head_from_its_live_tail_to_the_tail_it_kept(self) -> None:
        handle = self._start()
        client = handle.connect()
        self.addCleanup(client.close)
        self.assertTrue(client.send_input("<script>x</script>\n")["ok"])
        self.assertTrue(client.send_input(f"token {TOKEN}\n")["ok"])
        self._await(
            lambda: b"ECHO token" in client.read_output()["bytes_data"],
            message="the head never echoed its input",
        )

        # Running: the supervisor answers, and its answer is the freshest tail there is.
        live = self._document()
        self.assertEqual(live["head"]["state"], head_reads.RUNNING)
        self.assertEqual(live["transcript"]["source"], "supervisor")
        self.assertIn("ECHO <script>x</script>", live["transcript"]["text"])
        self.assertIn("input.accepted", [record.get("kind") for record in live["journal"]["tail"]])
        status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(status, 200)
        self.assertIn("its supervisor, live", page)
        self.assertIn("ECHO &lt;script&gt;x&lt;/script&gt;", page)
        for body in (page, json.dumps(live)):
            self.assertNotIn(TOKEN, body)
            self.assertNotIn(SECRET, body)
        rows = {row["run_id"]: row for row in self.layer().task_snapshot(REF)["heads"]["items"]}
        self.assertEqual(rows[WORKER]["state"], head_reads.RUNNING)
        # Asking was all it did: the head is still up and nothing was typed into it.
        self.assertTrue(client.status()["alive"])

        # Finished: the socket is gone, and the tail the supervisor kept is what is shown.
        self.assertTrue(client.send_input("quit\n")["ok"])
        self._await(lambda: not _alive(handle.supervisor_pid), message="the supervisor never let go")
        kept = self._document()
        self.assertEqual(kept["head"]["state"], head_reads.FINISHED)
        self.assertEqual(kept["transcript"]["source"], "output.tail")
        self.assertIn("ECHO <script>x</script>", kept["transcript"]["text"])
        self.assertIn("BYE", kept["transcript"]["text"])
        self.assertIn("run.exited", [record.get("kind") for record in kept["journal"]["tail"]])
        self.assertNotIn(TOKEN, json.dumps(kept))
        self.assertNotIn(SECRET, json.dumps(kept))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
