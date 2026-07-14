from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from secretary.config import load_config, validate
from secretary.gate import run_gate
from secretary.onboarding import project_add
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


if __name__ == "__main__":
    unittest.main()
