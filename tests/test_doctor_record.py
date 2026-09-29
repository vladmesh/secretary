"""Native doctor subprocess, atomic publication and bounded failure of the timer producer."""

from __future__ import annotations

import fcntl
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import main
from secretary.infra import doctor_record as records


class DoctorRecordTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.instance = self.root / "instance"
        self.instance.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        (self.instance / "instance.yaml").write_text(
            f"version: 1\nname: recorded\ndata_dir: {self.data}\noffsite:\n  instance_remote: https://github.com/example/fixture.git\n"
        )
        self.path = self.data / records.RESULT_PATH
        self.path.parent.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"),
        }))

    def document(self):
        return json.loads(self.path.read_text())

    def test_native_scheduled_command_records_the_real_recovery_predicate_even_at_rc_one(self):
        subprocess.run(["git", "init", "-q", str(self.instance)], check=True)
        subprocess.run(["git", "-C", str(self.instance), "config", "remote.origin.url",
                        "https://github.com/example/fixture.git"], check=True)
        # No secret: the real ambient-helper predicate sees a declared helper name.
        (self.home / ".gitconfig").write_text("[credential]\n\thelper = fixture-helper\n")
        command = [sys.executable, "-P", "-m", "secretary", "doctor-record", "--instance", str(self.instance),
                   "--data-dir", str(self.data), "--offline"]
        environment = {**os.environ, "PYTHONPATH": str(Path(records.__file__).resolve().parents[2])}
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=50, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = self.document()
        self.assertEqual((document["outcome"], document["exit_code"]), ("result", 1))
        bypass = [finding for finding in document["result"]["findings"] if finding["code"] == "recovery_bypass"]
        self.assertTrue(bypass, document)
        self.assertEqual(bypass[0]["capability"], "checkpoint-git-authentication")
        self.assertNotIn("fixture-helper", json.dumps(document))
        direct = subprocess.run([sys.executable, "-P", "-m", "secretary", "doctor", "--instance", str(self.instance),
                                 "--offline", "--json"], env=environment, capture_output=True, text=True, timeout=50, check=False)
        self.assertEqual(direct.returncode, 1)
        self.assertEqual(document["result"]["findings"], json.loads(direct.stdout)["findings"])
        self.assertEqual(document["installation"], records.identity(self.instance, self.data))
        self.assertTrue(document["run_at"].endswith("Z"))
        self.assertIsNotNone(document["completed_at"])
        from secretary.web.app import WebApp
        from secretary.web.commands import health_layers
        from tests.web_fakes import Recording

        reads, doctor = health_layers(str(self.instance), data_dir=str(self.data), offline=True)
        reads._status_reader = lambda: {}
        app = WebApp(reads, Recording(), Recording(sprint_list={"sprints": {"items": []}}),
                     Recording(), Recording(pause_state={}), Recording(), Recording(), Recording(), doctor=doctor)
        for route in ("/", "/doctor"):
            page = app.handle("GET", route).body.decode()
            self.assertNotIn("lamp lamp-green", page)
            self.assertIn("checkpoint-git-authentication", page)
            self.assertIn(document["run_at"], page)
        self.assertIn("recovery_bypass", app.handle("GET", "/doctor").body.decode())

    def test_timeout_parse_process_and_unavailable_outcomes_replace_the_previous_success(self):
        for failure in (TimeoutError("sensitive-output-placeholder"), ValueError("sensitive-output-placeholder"), OSError("sensitive-output-placeholder")):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(records, "collect", side_effect=failure):
                self.assertEqual(records.record(self.instance, offline=True), 2)
                document = self.document()
                self.assertEqual(document["outcome"], "failed")
                self.assertIsNone(document["result"])
                self.assertNotIn("sensitive-output-placeholder", json.dumps(document))
                self.assertIsNotNone(document["completed_at"])
        unavailable = {"schema_version": 1, "ok": False, "findings": [{"code": "host_inventory_unavailable", "message": "bus unavailable"}]}
        with mock.patch.object(records, "collect", return_value=(2, unavailable)):
            self.assertEqual(records.record(self.instance), 2)
        self.assertEqual(self.document()["result"], unavailable)
        self.assertEqual(records.read_latest(self.instance, self.data, now=time.time())["state"], "unavailable")

    def test_native_timeout_kills_collector_and_its_children_and_parse_output_is_bounded(self):
        pidfile = self.root / "child-pid"
        code = f"import subprocess,time,pathlib; p=subprocess.Popen(['sleep','60']); pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)"
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            records.collect([sys.executable, "-c", code], timeout=0.5)
        self.assertLess(time.monotonic() - started, 3)
        child = int(pidfile.read_text())
        proc = Path(f"/proc/{child}/stat")
        try:
            descriptor = os.pidfd_open(child)
        except ProcessLookupError:
            descriptor = None
        if descriptor is not None:
            try:
                poll = select.poll()
                poll.register(descriptor, select.POLLIN)
                self.assertTrue(poll.poll(1000), "grandchild did not stop")
            finally:
                os.close(descriptor)
        if proc.exists():
            self.assertEqual(proc.read_text().split()[2], "Z", "grandchild is stopped, pending init reap")
        for code in ("print('broken')", "raise SystemExit(7)", f"import os; os.write(1,b'x'*{records.MAX_BYTES + 1})"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                records.collect([sys.executable, "-c", code], timeout=2)

    def test_atomic_replace_cannot_publish_partial_json_and_interruption_invalidates_success(self):
        records.publish(self.path, {"prior": True})
        with mock.patch.object(records.os, "replace", side_effect=OSError("interrupted")), self.assertRaises(OSError):
            records.publish(self.path, {"next": True})
        self.assertEqual(self.document(), {"prior": True})
        self.assertEqual(list(self.path.parent.glob(".latest-*")), [])
        with mock.patch.object(records, "collect", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            records.record(self.instance)
        self.assertEqual(self.document()["outcome"], "collecting")
        self.assertEqual(records.read_latest(self.instance, self.data, now=time.time())["state"], "collecting")
        with mock.patch.object(records, "publish", side_effect=OSError("cannot write")), mock.patch.object(records, "collect") as collect:
            self.assertEqual(records.record(self.instance), 2)
        collect.assert_not_called()

    def test_overlapping_command_is_refused_without_collecting_or_changing_the_result(self):
        records.publish(self.path, {"prior": True})
        with (self.path.parent / "record.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(records, "collect") as collect:
                self.assertEqual(main(["doctor-record", "--instance", str(self.instance)]), 2)
            collect.assert_not_called()
        self.assertEqual(self.document(), {"prior": True})


if __name__ == "__main__":
    unittest.main()
