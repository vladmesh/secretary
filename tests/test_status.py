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
from secretary.host import CollectResult, HostInventory, build_doctor_expectations
from secretary.host_apply import resolve_packaged
from secretary.config import validate_instance


class StatusCliTests(unittest.TestCase):
    # status and doctor resolve the packaged Orca runtime. tests/__init__.py
    # routes that discovery to the repo fixture for every test by default, so
    # this class no longer needs its own patch (secretary-705, secretary-738,
    # secretary-748).

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
                json.dumps({"mode": "freeze", "actor": "secretary", "since": "2999-01-01T00:00:00Z"}), encoding="utf-8"
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
                "heads:\n  - role: worker\n    model: codex\n"
                "host:\n  projects_root: /projects\n  unit_prefix: secretary-\n"
                "  orca_repos:\n    - demo\n",
                encoding="utf-8",
            )
            projects = root / "projects"
            projects.mkdir()
            (projects / "demo.yaml").write_text(
                "id: demo\nrepo: /projects/demo\nenabled: true\nadapter: demo\ndefault_branch: main\n",
                encoding="utf-8",
            )
            fixture = root / "host"
            fixture.mkdir()
            report = validate_instance(instance)
            expected = build_doctor_expectations(report.instance, report.bindings, packaged=resolve_packaged(report.instance, instance_path=root))
            (fixture / "units.txt").write_text("\n".join(sorted(expected.units)), encoding="utf-8")
            (fixture / "projects.txt").write_text("/projects/demo\n", encoding="utf-8")
            (fixture / "orca-repos.txt").write_text("demo\n", encoding="utf-8")
            (fixture / "unit-states.txt").write_text(
                "\n".join(f"{name} enabled active" for name in sorted(expected.units)), encoding="utf-8"
            )
            output = io.StringIO()
            checkpoint = {
                "last_commit": "abc123", "last_commit_at": "2026-07-25T12:00:00Z",
                "last_push_at": "2026-07-25T11:50:00Z", "last_push_commit": "old123",
                "lag_commits": 9, "lag_minutes": 7, "push_status": "pending",
                "push_reason": "", "push_failures": 0, "remote_diverged": False,
                "blocked_reason": "",
            }
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
        self.assertEqual(payload["installation"]["projects"], 1)
        self.assertEqual(payload["installation"]["heads"][0]["role"], "worker")
        self.assertEqual(payload["installation"]["cards"]["total"], 1)
        self.assertIsNotNone(payload["host"]["resources"]["disk_free_bytes"])
        self.assertEqual(len(payload["host"]["resources"]["load_average"]), 3)
        self.assertEqual(payload["checkpoint"]["last_commit"], "abc123")
        self.assertEqual(payload["checkpoint"]["lag_commits"], 9)
        self.assertTrue(payload["dispatcher"]["pause"]["paused"])
        self.assertEqual(payload["dispatcher"]["pause"]["mode"], "freeze")
        self.assertEqual(payload["dispatcher"]["pause"]["actor"], "secretary")
        self.assertEqual(payload["dispatcher"]["pause"]["auto_resume"]["reason"], "fresh")
        self.assertEqual(payload["memory"]["fact_count"], 2)
        self.assertIsNotNone(payload["memory"]["last_reindex_at"])

    def test_status_human_output_and_live_watchdog_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            (data_dir / "dispatcher").mkdir(parents=True)
            (data_dir / "dispatcher" / "production-state.json").write_text(json.dumps({"records": {
                "secretary-727": {"head": "codex", "worker_progress_at": 1, "worker_respawns": 2}
            }}), encoding="utf-8")
            instance = root / "instance.yaml"
            instance.write_text(
                "version: 1\nname: test\ndata_dir: " + str(data_dir) + "\n"
                "offsite:\n  instance_remote: git@example.invalid:x/y.git\n"
                "host:\n  unit_prefix: secretary-\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            inventory = HostInventory(units=set(), unit_states={})
            panel = {"known": True, "live": True, "reason": "pane-active"}
            with contextlib.redirect_stdout(output), mock.patch(
                "secretary.status.LiveHostSource.collect", return_value=CollectResult(inventory)
            ), mock.patch("secretary.status.command_terminal_status", return_value=panel), mock.patch(
                "secretary.status.checkpoint_snapshot", return_value={"lag_minutes": 4}
            ):
                code = main(["status", "--instance", str(instance)])
            report = validate_instance(instance)
            with mock.patch("secretary.status.LiveHostSource.collect", return_value=CollectResult(inventory)), mock.patch(
                "secretary.status.command_terminal_status", return_value=panel
            ), mock.patch("secretary.status.checkpoint_snapshot", return_value={"lag_minutes": 4}):
                from secretary.status import collect_status
                snapshot = collect_status(report)

        self.assertEqual(code, 0)
        self.assertIn("Secretary status:", output.getvalue())
        self.assertTrue(snapshot["dispatcher"]["active_attempts"][0]["watchdogs"]["worker"]["panel"]["live"])

    def test_invalid_status_json_uses_the_documented_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance.yaml"
            instance.write_text("not: an-instance\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["status", "--json", "--instance", str(instance)])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(validate(payload, "status", "status.json"), [])

    def test_doctor_json_has_structured_findings(self):
        root = Path(__file__).resolve().parents[1]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["doctor", "--offline", "--json", "--instance", str(root / "examples" / "instance")])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0, payload)
        self.assertIn("findings", payload)
        self.assertIn("status", payload)

    def test_doctor_json_reports_the_same_missing_host_resource_as_doctor(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            text_output = io.StringIO()
            json_output = io.StringIO()
            arguments = ["doctor", "--host-fixture", str(fixture), "--instance", str(root / "examples" / "instance")]
            with contextlib.redirect_stdout(text_output):
                text_code = main(arguments)
            with contextlib.redirect_stdout(json_output):
                code = main(["doctor", "--json", *arguments[1:]])
        payload = json.loads(json_output.getvalue())
        self.assertEqual(text_code, 1, text_output.getvalue())
        self.assertIn("missing-on-host:", text_output.getvalue())
        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["ok"])
        self.assertTrue(any(finding["code"] == "missing_on_host" for finding in payload["findings"]))

    def test_doctor_json_reports_unit_runtime_findings(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            report = validate_instance(root / "examples" / "instance")
            expected = build_doctor_expectations(
                report.instance, report.bindings, packaged=resolve_packaged(report.instance, instance_path=root / "examples" / "instance")
            )
            (fixture / "units.txt").write_text("\n".join(sorted(expected.units)), encoding="utf-8")
            (fixture / "unit-states.txt").write_text(
                "\n".join(f"{name} disabled inactive" for name in sorted(expected.units)), encoding="utf-8"
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["doctor", "--json", "--host-fixture", str(fixture), "--instance", str(root / "examples" / "instance")])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1, payload)
        self.assertTrue(any(finding["code"] == "unit_runtime" for finding in payload["findings"]))
