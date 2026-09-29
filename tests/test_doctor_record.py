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

    def baseline(self, *, code=1, findings=None, started=1_800_000_000 - 10, mode="live"):
        if findings is None:
            findings = [{"code": "recovery_bypass", "message": "ambient credential configuration",
                         "capability": "checkpoint-git-authentication"}] if code else []
        return {"schema_version": 1, "installation": records.identity(self.instance, self.data),
                "run_at": records.utc(started), "completed_at": records.utc(started + 2),
                "mode": mode, "outcome": "unavailable" if code == 2 else "result",
                "exit_code": code, "reason": None,
                "result": {"schema_version": 1, "ok": not findings, "findings": findings}}

    def envelope(self, completed, collecting):
        return {"schema_version": 2, "installation": records.identity(self.instance, self.data),
                "completed": {key: value for key, value in completed.items()
                              if key not in ("schema_version", "installation")} if completed else None,
                "collecting": collecting}

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
        envelope = self.document()
        document = envelope["completed"]
        self.assertEqual(envelope["schema_version"], 2)
        self.assertIsNone(envelope["collecting"])
        self.assertEqual((document["outcome"], document["exit_code"]), ("result", 1))
        bypass = [finding for finding in document["result"]["findings"] if finding["code"] == "recovery_bypass"]
        self.assertTrue(bypass, document)
        self.assertEqual(bypass[0]["capability"], "checkpoint-git-authentication")
        self.assertNotIn("fixture-helper", json.dumps(document))
        direct = subprocess.run([sys.executable, "-P", "-m", "secretary", "doctor", "--instance", str(self.instance),
                                 "--offline", "--json"], env=environment, capture_output=True, text=True, timeout=50, check=False)
        self.assertEqual(direct.returncode, 1)
        self.assertEqual(document["result"]["findings"], json.loads(direct.stdout)["findings"])
        self.assertEqual(envelope["installation"], records.identity(self.instance, self.data))
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
        records.publish(self.path, self.baseline(started=time.time() - 10))
        for failure in (TimeoutError("sensitive-output-placeholder"), ValueError("sensitive-output-placeholder"), OSError("sensitive-output-placeholder")):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(records, "collect", side_effect=failure):
                self.assertEqual(records.record(self.instance, offline=True), 2)
                document = self.document()["completed"]
                self.assertEqual(document["outcome"], "failed")
                self.assertIsNone(document["result"])
                self.assertNotIn("sensitive-output-placeholder", json.dumps(document))
                self.assertIsNotNone(document["completed_at"])
        unavailable = {"schema_version": 1, "ok": False, "findings": [{"code": "host_inventory_unavailable", "message": "bus unavailable"}]}
        with mock.patch.object(records, "collect", return_value=(2, unavailable)):
            self.assertEqual(records.record(self.instance), 2)
        self.assertEqual(self.document()["completed"]["result"], unavailable)
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

    def test_atomic_replace_cannot_publish_partial_json_and_interruption_preserves_completion(self):
        records.publish(self.path, {"prior": True})
        with mock.patch.object(records.os, "replace", side_effect=OSError("interrupted")), self.assertRaises(OSError):
            records.publish(self.path, {"next": True})
        self.assertEqual(self.document(), {"prior": True})
        self.assertEqual(list(self.path.parent.glob(".latest-*")), [])
        prior = self.baseline(started=time.time() - 10)
        records.publish(self.path, prior)
        with mock.patch.object(records, "collect", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            records.record(self.instance)
        reading = records.read_latest(self.instance, self.data, now=time.time())
        self.assertEqual(reading["state"], "available")
        self.assertEqual(reading["findings"], prior["result"]["findings"])
        self.assertEqual(reading["run_at"], prior["run_at"])
        self.assertIsNotNone(reading["collecting"])
        with mock.patch.object(records, "publish", side_effect=OSError("cannot write")), mock.patch.object(records, "collect") as collect:
            self.assertEqual(records.record(self.instance), 2)
        collect.assert_not_called()

    def test_start_and_completion_retain_then_replace_the_completed_attempt(self):
        now = 1_800_000_000
        prior = self.baseline()
        records.publish(self.path, prior)
        before = records.read_latest(self.instance, self.data, now=now)

        def collect(command, **kwargs):
            during = records.read_latest(self.instance, self.data, now=now)
            self.assertEqual({k: v for k, v in during.items() if k != "collecting"},
                             {k: v for k, v in before.items() if k != "collecting"})
            self.assertEqual(during["collecting"]["run_at"], records.utc(now))
            return 0, {"schema_version": 1, "ok": True, "findings": []}

        with mock.patch.object(records.time, "time", return_value=now), mock.patch.object(records, "collect", collect):
            self.assertEqual(records.record(self.instance), 0)
        after = records.read_latest(self.instance, self.data, now=now + 1)
        self.assertEqual(after["state"], "available")
        self.assertEqual(after["run_at"], records.utc(now))
        self.assertEqual(after["findings"], [])
        self.assertIsNone(after["collecting"])

    def test_failed_atomic_start_or_completion_never_redates_the_completed_result(self):
        now = 1_800_000_000
        prior = self.baseline()
        for failure_at in (1, 2):
            with self.subTest(failure_at=failure_at):
                records.publish(self.path, prior)
                replace = records.os.replace
                calls = 0

                def fail_once(*args):
                    nonlocal calls
                    calls += 1
                    if calls == failure_at:
                        raise OSError("interrupted")
                    return replace(*args)

                with mock.patch.object(records.time, "time", return_value=now), \
                     mock.patch.object(records.os, "replace", fail_once), \
                     mock.patch.object(records, "collect", return_value=(0, {"schema_version": 1, "ok": True, "findings": []})) as collect:
                    self.assertEqual(records.record(self.instance), 2)
                self.assertEqual(collect.call_count, failure_at - 1)
                reading = records.read_latest(self.instance, self.data, now=now)
                self.assertEqual(reading["state"], "available")
                self.assertEqual(reading["run_at"], prior["run_at"])
                self.assertEqual(reading["findings"], prior["result"]["findings"])
                self.assertEqual(list(self.path.parent.glob(".latest-*")), [])

    def test_initial_interruption_and_atomic_failure_remain_explicit_unknown(self):
        now = 1_800_000_000
        with mock.patch.object(records.time, "time", return_value=now), \
             mock.patch.object(records, "collect", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            records.record(self.instance)
        reading = records.read_latest(self.instance, self.data, now=now)
        self.assertEqual((reading["state"], reading["reason"]), ("unknown", "not yet collected"))
        self.assertEqual(reading["collecting"]["elapsed_seconds"], 0)
        self.path.unlink()
        with mock.patch.object(records.os, "replace", side_effect=OSError("interrupted")):
            self.assertEqual(records.record(self.instance), 2)
        self.assertEqual(records.read_latest(self.instance, self.data, now=now)["state"], "unknown")

    def test_freshness_and_stuck_boundaries_are_independent_of_current_collection(self):
        now = 1_800_000_000
        records.publish(self.path, self.envelope(self.baseline(started=now - records.FRESH_SECONDS),
                                               {"run_at": records.utc(now), "mode": "live"}))
        self.assertEqual(records.read_latest(self.instance, self.data, now=now)["state"], "available")
        self.assertEqual(records.read_latest(self.instance, self.data, now=now + .01)["state"], "stale")
        for completed in (None, self.baseline()):
            records.publish(self.path, self.envelope(completed, {"run_at": records.utc(now), "mode": "live"}))
            for elapsed in (records.STUCK_SECONDS - .01, records.STUCK_SECONDS, records.STUCK_SECONDS + .01):
                reading = records.read_latest(self.instance, self.data, now=now + elapsed)
                self.assertEqual(reading["collecting"]["stuck"], elapsed > records.STUCK_SECONDS)
                self.assertEqual(reading["state"], "available" if completed else "unknown")

    def test_old_progress_cannot_override_a_newer_completion_or_its_mode(self):
        now = 1_800_000_000
        completed = self.baseline()
        for start in (now - 120, now - 10, now - 8):
            records.publish(self.path, self.envelope(completed, {"run_at": records.utc(start), "mode": "offline"}))
            reading = records.read_latest(self.instance, self.data, now=now)
            self.assertEqual(reading["state"], "available")
            self.assertIsNone(reading["collecting"])

    def test_released_collecting_record_is_unknown_and_still_has_a_stuck_threshold(self):
        now = 1_800_000_000
        document = self.baseline(started=now)
        document.update(outcome="collecting", completed_at=None, exit_code=None, result=None)
        records.publish(self.path, document)
        for elapsed in (0, 60, 61, 181):
            reading = records.read_latest(self.instance, self.data, now=now + elapsed)
            self.assertEqual((reading["state"], reading["reason"]), ("unknown", "not yet collected"))
            self.assertEqual(reading["findings"], [])
            self.assertEqual(reading["collecting"]["stuck"], elapsed > records.STUCK_SECONDS)

    def test_restart_retains_version_two_completed_failures_and_staleness(self):
        now = 1_800_000_000
        for state in ("available", "unavailable", "failed", "stale"):
            with self.subTest(state=state):
                completed = self.baseline(code=2 if state == "unavailable" else 1,
                                          started=now - 181 if state == "stale" else now - 10)
                if state == "failed":
                    completed.update(outcome="failed", result=None, exit_code=None, reason="collection failed")
                records.publish(self.path, self.envelope(completed, {"run_at": records.utc(now - 5), "mode": "live"}))
                before = records.read_latest(self.instance, self.data, now=now)
                with mock.patch.object(records.time, "time", return_value=now), \
                     mock.patch.object(records, "collect", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                    records.record(self.instance)
                after = records.read_latest(self.instance, self.data, now=now)
                self.assertEqual(after["state"], state)
                self.assertEqual({k: v for k, v in before.items() if k != "collecting"},
                                 {k: v for k, v in after.items() if k != "collecting"})
                self.assertEqual(after["collecting"]["run_at"], records.utc(now))

    def test_both_record_parts_are_validated_and_wrong_installation_is_not_retained(self):
        now = 1_800_000_000
        for completed_mode, collecting_mode in (("offline", "live"), ("live", "host_fixture")):
            document = self.envelope(self.baseline(mode=completed_mode), {"run_at": records.utc(now), "mode": collecting_mode})
            records.publish(self.path, document)
            self.assertEqual(records.read_latest(self.instance, self.data, now=now)["state"], "wrong_mode")
        for progress in ({"run_at": "broken", "mode": "live"}, {"run_at": records.utc(now + 6), "mode": "live"},
                         {"run_at": records.utc(now), "mode": "invalid"}, "broken"):
            records.publish(self.path, self.envelope(self.baseline(), progress))
            self.assertEqual(records.read_latest(self.instance, self.data, now=now)["state"], "malformed")
        document["installation"] = {"instance": "another", "data_dir": "another"}
        records.publish(self.path, document)
        self.assertEqual(records.read_latest(self.instance, self.data, now=now)["state"], "wrong_installation")
        with mock.patch.object(records.time, "time", return_value=now), \
             mock.patch.object(records, "collect", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            records.record(self.instance)
        self.assertIsNone(self.document()["completed"])

    def web_fixture(self):
        from secretary.web.app import WebApp
        from secretary.web.commands import health_layers
        from tests.web_fakes import Recording, system_snapshot

        self.web_clock = 1_800_000_000
        self.web_status = {}
        self.web_count = 0
        reads, doctor = health_layers(str(self.instance), data_dir=str(self.data), now=lambda: self.web_clock)

        def status():
            self.web_count += 1
            return self.web_status

        reads._status_reader = status

        class DashboardReads(Recording):
            def system_snapshot(inner):
                snapshot = system_snapshot()
                snapshot["installation"]["health"] = reads._system_health(reads.report(), self.data, now=self.web_clock)
                return snapshot

        app = WebApp(DashboardReads(), Recording(), Recording(sprint_list={"sprints": {"items": []}}),
                     Recording(), Recording(pause_state={}), Recording(), Recording(), Recording(), doctor=doctor)
        return app, doctor

    def assert_web_state(self, app, doctor, colour, *, codes):
        from secretary.config import validate
        from tests.web_fakes import system_snapshot

        document = doctor.doctor_snapshot()
        self.assertEqual(document["colour"], colour)
        self.assertEqual([item["code"] for item in document["problems"]], codes)
        self.assertEqual(doctor.health_snapshot()["health"]["combined"]["findings"], document["problems"])
        snapshot = {**system_snapshot(), "schema_version": 1, "kind": "system"}
        snapshot["installation"]["health"] = doctor.health_snapshot()["health"]
        self.assertEqual(validate(snapshot, "web-read", "system"), [])
        for route in ("/", "/doctor"):
            page = app.handle("GET", route).body.decode()
            self.assertIn(f"lamp lamp-{colour}", page)
            for code in codes:
                self.assertIn(code, page)
            if "health.unreadable" not in codes:
                self.assertNotIn("health.unreadable", page)
        return document

    def test_actual_producer_transitions_share_cached_dashboard_lamp_and_doctor(self):
        from secretary.web.doctor import CACHE_SECONDS
        app, doctor = self.web_fixture()
        prior = self.baseline()
        records.publish(self.path, prior)
        before = self.assert_web_state(app, doctor, "yellow", codes=["recovery_bypass"])
        self.web_clock += CACHE_SECONDS + 1

        def collect(command, **kwargs):
            during = self.assert_web_state(app, doctor, "yellow", codes=["recovery_bypass"])
            self.assertEqual(during["problems"], before["problems"])
            self.assertEqual(during["doctor_run_at"], before["doctor_run_at"])
            self.assertEqual(self.web_count, 2)
            for route in ("/", "/doctor"):
                page = app.handle("GET", route).body.decode()
                self.assertIn("checkpoint-git-authentication", page)
                self.assertIn(f"run in progress since {records.utc(self.web_clock)}", page)
                self.assertIn(prior["run_at"], page)
            self.web_clock += 2
            return 1, {"schema_version": 1, "ok": False, "findings": [{"code": "unit.failed", "message": "a.service is failed"}]}

        with mock.patch.object(records.time, "time", side_effect=lambda: self.web_clock), mock.patch.object(records, "collect", collect):
            self.assertEqual(records.record(self.instance), 0)
        self.assert_web_state(app, doctor, "yellow", codes=["recovery_bypass"])
        self.assertEqual(self.web_count, 2, "completion is visible after the shared cache expires")
        self.web_clock += CACHE_SECONDS + 1
        after = self.assert_web_state(app, doctor, "red", codes=["unit.failed"])
        self.assertIsNone(after["doctor"]["collecting"])
        self.assertEqual(self.web_count, 3)

    def test_stuck_collection_is_a_separate_finding_at_the_injected_clock_boundary(self):
        app, doctor = self.web_fixture()
        start = self.web_clock
        records.publish(self.path, self.envelope(self.baseline(), {"run_at": records.utc(start), "mode": "live"}))
        for elapsed in (0, records.STUCK_SECONDS, records.STUCK_SECONDS + .01):
            self.web_clock = start + elapsed
            doctor._cached = None
            stuck = elapsed > records.STUCK_SECONDS
            codes = ["recovery_bypass"] + (["doctor.collection_stuck"] if stuck else [])
            document = self.assert_web_state(app, doctor, "red" if stuck else "yellow", codes=codes)
            if stuck:
                problem = document["problems"][-1]
                self.assertAlmostEqual(problem["elapsed_seconds"], elapsed)
                self.assertEqual(problem["threshold_seconds"], records.STUCK_SECONDS)
                self.assertIn("secretary-doctor.service", problem["message"])

    def test_missing_and_actual_first_collection_are_unknown_without_hiding_status_problems(self):
        app, doctor = self.web_fixture()

        def unknown():
            self.assert_web_state(app, doctor, "unknown", codes=[])
            for route in ("/", "/doctor"):
                page = app.handle("GET", route).body.decode()
                self.assertIn("unknown / not yet collected", page)
                self.assertNotIn("nothing needs attention", page)
                self.assertNotIn("every check this installation records answered", page)

        unknown()

        def collect(command, **kwargs):
            doctor._cached = None
            unknown()
            self.assertIn("run in progress since", app.handle("GET", "/doctor").body.decode())
            return 0, {"schema_version": 1, "ok": True, "findings": []}

        with mock.patch.object(records.time, "time", side_effect=lambda: self.web_clock), mock.patch.object(records, "collect", collect):
            self.assertEqual(records.record(self.instance), 0)
        doctor._cached = None
        self.assert_web_state(app, doctor, "green", codes=[])
        self.path.unlink()
        for colour, status, code in (
            ("red", {"host": {"units": [{"name": "a.service", "kind": "service", "present": True, "active": "failed"}]}}, "unit.failed"),
            ("yellow", {"dispatcher": {"pause": {"paused": True, "mode": "drain"}}}, "pipeline.paused"),
        ):
            self.web_status = status
            doctor._cached = None
            self.assert_web_state(app, doctor, colour, codes=[code])

    def test_initial_unknown_does_not_hide_malformed_unreadable_or_completed_failure(self):
        app, doctor = self.web_fixture()
        self.path.write_text("{broken")
        self.assert_web_state(app, doctor, "red", codes=["health.unreadable"])
        self.path.unlink()
        doctor._cached = None
        with mock.patch.object(Path, "open", side_effect=PermissionError("cannot read")):
            reading = records.read_latest(self.instance, self.data, now=self.web_clock)
        self.assertEqual(reading["state"], "unavailable")
        for state, change in (
            ("failed", {"outcome": "failed", "exit_code": None, "result": None}),
            ("unavailable", {"outcome": "unavailable", "exit_code": 2}),
            ("wrong_mode", {"mode": "offline"}),
            ("stale", {"run_at": records.utc(self.web_clock - 181), "completed_at": records.utc(self.web_clock - 180)}),
        ):
            document = self.baseline()
            document.update(change)
            records.publish(self.path, document)
            doctor._cached = None
            result = doctor.doctor_snapshot()
            self.assertEqual(result["doctor"]["state"], state)
            self.assertEqual(result["colour"], "red")
            self.assertIn("health.unreadable", [problem["code"] for problem in result["problems"]])

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
