from __future__ import annotations

import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import yaml

from secretary.config import load_config, validate
from secretary.gate import _timed_out, run_gate
from secretary.onboarding import ScannerError, project_add
from secretary.provision import apply_provision_result, start_provision
from tests.test_onboarding import git, make_repo


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = make_repo(self.root)
        self.instance = self.root / "instance"
        self.instance.mkdir()
        self.assertEqual(project_add(str(self.repo), str(self.instance), dry_run=False)[0], 0)
        code, started = start_provision(str(self.instance), "sample-project")
        self.assertEqual(code, 0)
        self.task = started["task"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def binding(self) -> Path:
        return self.instance / "projects" / "sample-project.yaml"

    @property
    def adapter(self) -> Path:
        return self.instance / "adapters" / "sample-project.yaml"

    def provision(self, *, setup="true", smoke="true", validation="true", no_tests=False) -> None:
        adapter = {
            "setup": {"commands": [setup]},
            "smoke": {"command": smoke},
            "validation": {"ci": "none", "missing": ["tests"]} if no_tests else {"ci": "local", "command": validation},
            "artifact_policy": {"write_project_files": False},
        }
        result = {"version": 1, "run_id": self.task["run_id"],
                  "identity": {"id": "sample-project", "adapter": "sample-project"},
                  "input_revision": dict(self.task["input_revision"]), "status": "drafted",
                  "adapter": adapter, "project_local_adapter": {"proposed": False, "requires_opt_in": True}}
        path = self.instance / "result.yaml"
        path.write_text(yaml.safe_dump(result, sort_keys=False), encoding="utf-8")
        code, output = apply_provision_result(str(self.instance), "sample-project", str(path))
        self.assertEqual(code, 0, output)

    def test_success_enables_and_publishes_versioned_result_and_compatibility(self):
        self.provision()
        code, result = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 0, result)
        self.assertEqual(validate(result, "gate-result", "result"), [])
        self.assertTrue(load_config(self.binding)["enabled"])
        self.assertEqual(load_config(self.instance / "adapter-drafts/sample-project.yaml")["gate"]["status"], "passed")
        manifest = self.instance / "compatibility-manifests/sample-project.toml"
        self.assertTrue(manifest.exists())
        self.assertIn('project = "sample-project"', manifest.read_text())
        self.assertEqual(run_gate(str(self.instance), "sample-project"), (0, result))
        dry_code, dry_result = project_add(str(self.repo), str(self.instance), dry_run=True)
        self.assertEqual(dry_code, 0, dry_result)
        self.assertEqual(dry_result["gate"]["status"], "passed")
        self.assertTrue(load_config(self.binding)["enabled"])

    def test_compatibility_is_published_to_legacy_dispatcher_lookup(self):
        legacy = self.root / "control-panel" / "pipeline" / "manifests"
        (self.instance / "instance.yaml").write_text(yaml.safe_dump({
            "version": 1, "name": "test", "data_dir": str(self.root / "data"),
            "offsite": {"instance_remote": "git@example.invalid:test/instance.git"},
            "compatibility": {"dispatcher_manifest_dir": str(legacy)},
        }), encoding="utf-8")
        self.provision()

        code, result = run_gate(str(self.instance), "sample-project")

        self.assertEqual(code, 0, result)
        rendered = legacy / "sample-project.toml"
        self.assertEqual(tomllib.loads(rendered.read_text())["workspace"]["base_branch"], "main")
        (self.instance / "instance.yaml").write_text(yaml.safe_dump({
            "version": 1, "name": "test", "data_dir": str(self.root / "data"),
            "offsite": {"instance_remote": "git@example.invalid:test/instance.git"},
        }), encoding="utf-8")

        (self.repo / "sample.py").write_text("VALUE = 12\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "Invalidate gate")
        self.assertEqual(run_gate(str(self.instance), "sample-project")[1]["status"], "stale")
        self.assertFalse(rendered.exists())

    def test_stale_disable_tolerates_compatibility_path_added_after_enable(self):
        self.provision()
        self.assertEqual(run_gate(str(self.instance), "sample-project")[0], 0)
        legacy = self.root / "new-control-panel" / "pipeline" / "manifests"
        (self.instance / "instance.yaml").write_text(yaml.safe_dump({
            "version": 1, "name": "test", "data_dir": str(self.root / "data"),
            "offsite": {"instance_remote": "git@example.invalid:test/instance.git"},
            "compatibility": {"dispatcher_manifest_dir": str(legacy)},
        }), encoding="utf-8")
        (self.repo / "sample.py").write_text("VALUE = 13\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "Invalidate old gate")

        code, result = run_gate(str(self.instance), "sample-project")

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "stale")
        self.assertFalse(load_config(self.binding)["enabled"])

    def test_invalid_instance_config_on_enable_is_structured_conflict(self):
        self.provision()
        (self.instance / "instance.yaml").write_text("compatibility: [broken", encoding="utf-8")

        code, result = run_gate(str(self.instance), "sample-project")

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(load_config(self.binding)["enabled"])

    def test_corrupt_current_result_is_structured_conflict(self):
        self.provision()
        code, result = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 0, result)
        path = self.instance / "gate-runs" / "sample-project" / result["run_id"] / "result.json"
        path.write_text("{broken", encoding="utf-8")

        code, conflict = run_gate(str(self.instance), "sample-project")

        self.assertEqual(code, 1)
        self.assertEqual(conflict["status"], "conflict")
        self.assertTrue(load_config(self.binding)["enabled"])

    def assert_stage_failure(self, stage: str) -> None:
        command = "printf 'token=ghp_secret_value' >&2; false"
        self.provision(**{stage: command})
        code, result = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 1)
        self.assertEqual(result["checks"][stage]["status"], "failed")
        self.assertNotIn("ghp_secret_value", str(result))
        self.assertFalse(load_config(self.binding)["enabled"])
        self.assertFalse((self.instance / "compatibility-manifests/sample-project.toml").exists())

    def test_setup_failure_leaves_disabled(self):
        self.assert_stage_failure("setup")

    def test_smoke_failure_leaves_disabled(self):
        self.assert_stage_failure("smoke")

    def test_validation_failure_leaves_disabled(self):
        self.assert_stage_failure("validation")

    def test_declared_missing_tests_is_preserved(self):
        self.provision(no_tests=True)
        code, result = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["checks"]["validation"]["status"], "declared-missing")
        self.assertEqual(result["missing_coverage"], ["tests"])

    def test_adapter_mutation_before_publish_returns_stale(self):
        self.provision()
        real = Path.read_bytes
        calls = 0

        def mutate(path: Path):
            nonlocal calls
            if path == self.adapter:
                calls += 1
                if calls == 2:
                    path.write_text(path.read_text() + "\n", encoding="utf-8")
            return real(path)

        with mock.patch("pathlib.Path.read_bytes", autospec=True, side_effect=mutate):
            code, result = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "stale")
        self.assertFalse(load_config(self.binding)["enabled"])

    def test_publication_failure_rolls_back_all_enabled_state(self):
        self.provision()
        with mock.patch("secretary.gate.publish_state_atomic", side_effect=OSError(5, "injected")):
            code, result = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 1)
        self.assertEqual(result["findings"][0]["code"], "publication.failed")
        self.assertFalse(load_config(self.binding)["enabled"])
        self.assertFalse((self.instance / "compatibility-manifests/sample-project.toml").exists())

    def test_repo_revision_invalidates_enabled_result(self):
        self.provision()
        self.assertEqual(run_gate(str(self.instance), "sample-project")[0], 0)
        (self.repo / "sample.py").write_text("VALUE = 9\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "New revision")

        code, result = run_gate(str(self.instance), "sample-project")

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "stale")
        self.assertFalse(load_config(self.binding)["enabled"])
        self.assertFalse((self.instance / "compatibility-manifests/sample-project.toml").exists())

    def test_repeat_selects_current_result_after_multiple_revisions(self):
        self.provision()
        self.assertEqual(run_gate(str(self.instance), "sample-project")[0], 0)
        (self.repo / "sample.py").write_text("VALUE = 10\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "Second gate revision")
        self.assertEqual(run_gate(str(self.instance), "sample-project")[1]["status"], "stale")
        self.assertEqual(project_add(str(self.repo), str(self.instance), dry_run=False)[0], 0)
        code, started = start_provision(str(self.instance), "sample-project")
        self.assertEqual(code, 0)
        self.task = started["task"]
        self.provision()
        code, current = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 0, current)
        self.assertGreaterEqual(len(list((self.instance / "gate-runs/sample-project").glob("*/result.json"))), 2)

        repeat_code, repeated = run_gate(str(self.instance), "sample-project")

        self.assertEqual(repeat_code, 0, repeated)
        self.assertEqual(repeated["run_id"], current["run_id"])
        self.assertTrue(load_config(self.binding)["enabled"])

    def test_failure_result_publication_error_is_structured(self):
        self.provision(setup="false")
        with mock.patch("secretary.gate.publish_state_atomic", side_effect=OSError(5, "injected")):
            code, result = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 1)
        self.assertEqual(result["findings"][0]["code"], "publication.failed")

    def test_command_timeout_is_a_redacted_stage_failure(self):
        self.provision(setup="slow command")
        expired = subprocess.TimeoutExpired(
            "slow command", 300, output="AKIAABCDEFGHIJKLMNOP", stderr=""
        )
        with mock.patch("secretary.gate._command", return_value=_timed_out(expired)):
            code, result = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 1)
        self.assertEqual(result["checks"]["setup"]["status"], "failed")
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", str(result))

    def test_scan_failure_invalidates_enabled_project_without_traceback(self):
        self.provision()
        self.assertEqual(run_gate(str(self.instance), "sample-project")[0], 0)
        manifest = self.instance / "compatibility-manifests/sample-project.toml"
        with mock.patch("secretary.gate.scan_repo", side_effect=ScannerError("injected")):
            code, result = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "stale")
        self.assertFalse(load_config(self.binding)["enabled"])
        self.assertFalse(manifest.exists())

    def test_scan_failure_on_disabled_project_is_structured_stale(self):
        self.provision()
        with mock.patch("secretary.gate.scan_repo", side_effect=ScannerError("injected")):
            code, result = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "stale")
        self.assertFalse(load_config(self.binding)["enabled"])

    def test_enabled_adapter_read_failure_is_conflict_without_state_change(self):
        self.provision()
        self.assertEqual(run_gate(str(self.instance), "sample-project")[0], 0)
        real_read_bytes = Path.read_bytes

        def fail_adapter(path: Path) -> bytes:
            if path == self.adapter:
                raise OSError(5, "injected")
            return real_read_bytes(path)

        with mock.patch("pathlib.Path.read_bytes", autospec=True, side_effect=fail_adapter):
            code, result = run_gate(str(self.instance), "sample-project")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(load_config(self.binding)["enabled"])
        self.assertTrue((self.instance / "compatibility-manifests/sample-project.toml").exists())


if __name__ == "__main__":
    unittest.main()
