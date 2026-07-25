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
            (data_dir / "dispatcher" / "production-state.json").write_text(
                json.dumps({"phase": "production", "records": {
                    "secretary-727": {"attempt_id": "a1", "head": "codex", "workspace": "/work", "worker_progress_at": 1, "worker_respawns": 1, "paused_worker_at": 1}
                }}), encoding="utf-8"
            )
            instance = root / "instance.yaml"
            instance.write_text(
                "version: 1\nname: test\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["status", "--offline", "--json", "--instance", str(instance)])

        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(validate(payload, "status", "status.json"), [])
        self.assertEqual(payload["dispatcher"]["active_attempts"][0]["head"], "codex")
        self.assertEqual(payload["dispatcher"]["active_attempts"][0]["watchdogs"]["worker"]["respawns"], 1)
        self.assertTrue(payload["dispatcher"]["active_attempts"][0]["paused"]["worker"])

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
