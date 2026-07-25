from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import main
from secretary.config import validate
from secretary.host import build_doctor_expectations
from secretary.host_apply import resolve_packaged
from secretary.config import validate_instance


class StatusCliTests(unittest.TestCase):
    def test_status_json_has_the_documented_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            (data_dir / "dispatcher").mkdir(parents=True)
            (data_dir / "memory").mkdir()
            (data_dir / "memory" / "manifest.json").write_text(
                json.dumps({"journal": {"fact_count": 2}}), encoding="utf-8"
            )
            (data_dir / "memory" / "index.sqlite").write_text("index", encoding="utf-8")
            (data_dir / "board").mkdir()
            (data_dir / "board" / "cards.ndjson").write_text('{"ref":"secretary-727"}\n', encoding="utf-8")
            (data_dir / "dispatcher" / "pause.json").write_text(
                json.dumps({"mode": "freeze", "actor": "secretary", "since": "2026-07-25T12:00:00Z"}), encoding="utf-8"
            )
            (data_dir / "dispatcher" / "production-state.json").write_text(
                json.dumps({"phase": "production", "records": {
                    "secretary-727": {"attempt_id": "a1", "head": "codex", "workspace": "/work", "worker_progress_at": 1, "worker_respawns": 1, "paused_worker_at": 1}
                }}), encoding="utf-8"
            )
            instance = root / "instance.yaml"
            instance.write_text(
                "version: 1\nname: test\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n  instance_remote: git@example.invalid:x/y.git\n"
                "host:\n  unit_prefix: secretary-\n",
                encoding="utf-8",
            )
            fixture = root / "host"
            fixture.mkdir()
            report = validate_instance(instance)
            expected = build_doctor_expectations(report.instance, report.bindings, packaged=resolve_packaged(report.instance, instance_path=root))
            (fixture / "units.txt").write_text("\n".join(sorted(expected.units)), encoding="utf-8")
            (fixture / "unit-states.txt").write_text(
                "\n".join(f"{name} enabled active" for name in sorted(expected.units)), encoding="utf-8"
            )
            output = io.StringIO()
            checkpoint = {"last_commit": "abc123", "lag_minutes": 7, "remote_diverged": False, "blocked_reason": None}
            with contextlib.redirect_stdout(output), mock.patch("secretary.status.checkpoint_snapshot", return_value=checkpoint):
                code = main(["status", "--json", "--host-fixture", str(fixture), "--instance", str(instance)])

        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(validate(payload, "status", "status.json"), [])
        self.assertEqual(payload["dispatcher"]["active_attempts"][0]["head"], "codex")
        self.assertEqual(payload["dispatcher"]["active_attempts"][0]["watchdogs"]["worker"]["respawns"], 1)
        self.assertTrue(payload["dispatcher"]["active_attempts"][0]["paused"]["worker"])
        self.assertTrue(payload["host"]["units"])
        self.assertTrue(payload["host"]["schedules"])
        self.assertEqual(payload["host"]["units"][0]["enabled"], "enabled")
        self.assertEqual(payload["checkpoint"]["last_commit"], "abc123")
        self.assertTrue(payload["dispatcher"]["pause"]["paused"])
        self.assertEqual(payload["dispatcher"]["pause"]["mode"], "freeze")

    def test_doctor_json_has_structured_findings(self):
        root = Path(__file__).resolve().parents[1]
        output = io.StringIO()
        # The doctor path resolves the packaged Orca runtime, which a CI runner does not have:
        # without this the test is green on a host with Orca installed and red everywhere else
        # (secretary-705, commit c1edf14 did the same for tests/test_cli.py).
        legacy_orca = root / "tests" / "fixtures" / "legacy-orca"
        with contextlib.redirect_stdout(output), mock.patch(
            "secretary.host_apply.find_orca_executable", return_value=legacy_orca
        ):
            code = main(["doctor", "--offline", "--json", "--instance", str(root / "examples" / "instance")])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0, payload)
        self.assertIn("findings", payload)
        self.assertIn("status", payload)

    def test_doctor_json_reports_the_same_missing_host_resource_as_doctor(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), mock.patch("secretary.host_apply.find_orca_executable", return_value=root / "tests" / "fixtures" / "legacy-orca"):
                code = main(["doctor", "--json", "--host-fixture", str(fixture), "--instance", str(root / "examples" / "instance")])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["ok"])
        self.assertTrue(any(finding["code"] == "missing_on_host" for finding in payload["findings"]))
