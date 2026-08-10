from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import yaml

from secretary._fsutil import publish_pair_atomic
from secretary.config import load_config, validate
from secretary.cli import main
from secretary.onboarding import project_add
from secretary.provision import apply_provision_result, start_provision
from tests.test_onboarding import git, make_repo


class ProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = make_repo(self.root)
        self.instance = self.root / "instance"
        self.instance.mkdir()
        code, _ = project_add(str(self.repo), str(self.instance), dry_run=False)
        self.assertEqual(code, 0)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def draft_path(self) -> Path:
        return self.instance / "adapter-drafts" / "sample-project.yaml"

    @property
    def binding_path(self) -> Path:
        return self.instance / "projects" / "sample-project.yaml"

    @property
    def adapter_path(self) -> Path:
        return self.instance / "adapters" / "sample-project.yaml"

    def start(self) -> dict:
        code, result = start_provision(str(self.instance), "sample-project")
        self.assertEqual(code, 0, result)
        self.assertEqual(validate(result["task"], "provision-task", "task"), [])
        return result

    def write_result(self, result: dict) -> Path:
        run_id = result["run_id"]
        path = self.instance / "provision-runs" / "sample-project" / run_id / "result.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(result, sort_keys=False), encoding="utf-8")
        return path

    def drafted_result(self, task: dict) -> dict:
        return {
            "version": 1,
            "run_id": task["run_id"],
            "identity": {
                "id": task["identity"]["id"],
                "adapter": task["identity"]["adapter"],
            },
            "input_revision": dict(task["input_revision"]),
            "status": "drafted",
            "adapter": {
                "setup": {"commands": ["python3 -m pip install -e ."]},
                "smoke": {"command": "python3 -m unittest discover -s tests"},
                "validation": {"ci": "github"},
                "artifact_policy": {"write_project_files": False},
            },
            "project_local_adapter": {
                "proposed": False,
                "requires_opt_in": True,
            },
        }

    def test_start_requires_existing_valid_fresh_draft(self):
        self.draft_path.unlink()
        code, result = start_provision(str(self.instance), "sample-project")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "draft_missing")

        code, _ = project_add(str(self.repo), str(self.instance), dry_run=False)
        self.assertEqual(code, 0)
        valid_draft = self.draft_path.read_bytes()
        self.draft_path.write_text("identity: [broken\n", encoding="utf-8")
        code, result = start_provision(str(self.instance), "sample-project")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "draft_invalid")

        self.draft_path.write_bytes(valid_draft)
        (self.repo / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "Change")
        code, result = start_provision(str(self.instance), "sample-project")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "stale_input")
        self.assertNotEqual(result["expected_scanner_head"], result["actual_scanner_head"])
        self.assertFalse(self.adapter_path.exists())

    def test_task_document_contains_scanner_facts_and_operator_constraints_only(self):
        binding = load_config(self.binding_path)
        binding["plane"] = "project"
        binding["policy"] = {"code_concurrency": 2}
        self.binding_path.write_text(yaml.safe_dump(binding), encoding="utf-8")

        result = self.start()
        task = result["task"]

        self.assertEqual(task["constraints"]["adapter_storage"], "external")
        self.assertTrue(task["constraints"]["project_local_requires_opt_in"])
        self.assertTrue(task["constraints"]["project_local_allowed"])
        self.assertEqual(task["constraints"]["plane"], "project")
        self.assertEqual(task["constraints"]["policy"], {"code_concurrency": 2})
        self.assertIn("ci_files", task["scanner"]["facts"])
        self.assertNotIn("plane", task["identity"])
        self.assertNotIn("policy", task["identity"])

    def test_apply_publishes_valid_adapter_and_keeps_binding_disabled(self):
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))

        code, result = apply_provision_result(str(self.instance), "sample-project", str(result_path))

        self.assertEqual(code, 0, result)
        self.assertTrue(self.adapter_path.exists())
        adapter = load_config(self.adapter_path)
        draft = load_config(self.draft_path)
        binding = load_config(self.binding_path)
        self.assertEqual(validate(adapter, "adapter", self.adapter_path.name), [])
        self.assertEqual(validate(draft, "onboarding-contract", self.draft_path.name), [])
        self.assertEqual(draft["provision"]["status"], "drafted")
        self.assertFalse(draft["provision"]["binding"]["enabled"])
        self.assertFalse(binding["enabled"])

    def test_apply_is_idempotent_for_same_result_and_revision(self):
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))

        first = apply_provision_result(str(self.instance), "sample-project", str(result_path))
        adapter_bytes = self.adapter_path.read_bytes()
        draft_bytes = self.draft_path.read_bytes()
        second = apply_provision_result(str(self.instance), "sample-project", str(result_path))

        self.assertEqual(first, second)
        self.assertEqual(adapter_bytes, self.adapter_path.read_bytes())
        self.assertEqual(draft_bytes, self.draft_path.read_bytes())

    def test_project_add_resets_provision_when_scanner_head_changes(self):
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))
        code, result = apply_provision_result(str(self.instance), "sample-project", str(result_path))
        self.assertEqual(code, 0, result)
        (self.repo / "sample.py").write_text("VALUE = 4\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "Change after draft")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0, artifact)
        self.assertEqual(artifact["provision"]["status"], "pending")
        self.assertEqual(artifact["gate"]["status"], "pending")
        self.assertEqual(artifact["provision"]["adapter"]["status"], "unresolved")
        self.assertEqual(
            artifact["ownership"]["adapter"]["storage"],
            "secretary-instance/adapter-drafts/<project>.yaml",
        )
        stored = load_config(self.draft_path)
        self.assertEqual(stored["provision"]["status"], "pending")
        self.assertFalse(self.adapter_path.exists())

    def test_re_onboard_takes_down_an_enabled_binding_with_a_drafted_provision(self):
        """An enabled binding whose draft carries a drafted provision is still taken down: the
        adapter that was executing under the enable cannot ride into the new draft unverified."""
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))
        code, result = apply_provision_result(str(self.instance), "sample-project", str(result_path))
        self.assertEqual(code, 0, result)
        binding = load_config(self.binding_path)
        binding["enabled"] = True
        self.binding_path.write_text(yaml.safe_dump(binding), encoding="utf-8")

        code, artifact = project_add(
            str(self.repo), str(self.instance), dry_run=False, re_onboard=True
        )

        self.assertEqual(code, 0, artifact)
        self.assertFalse(load_config(self.binding_path)["enabled"])
        self.assertFalse(self.adapter_path.exists())
        self.assertEqual(artifact["provision"]["status"], "pending")
        self.assertEqual(artifact["provision"]["adapter"]["status"], "unresolved")
        self.assertEqual(artifact["gate"]["status"], "pending")
        self.assertEqual(load_config(self.draft_path), artifact)

    def test_re_onboard_voids_the_previous_run_evidence_on_an_unchanged_head(self):
        """Taking an enabled binding down starts a new onboarding cycle. Run ids are derived from
        the cycle too, so the previous result cannot republish the adapter the takedown deleted and
        the previous gate result cannot supersede the new one."""
        task = self.start()["task"]
        old_result = self.write_result(self.drafted_result(task))
        code, result = apply_provision_result(str(self.instance), "sample-project", str(old_result))
        self.assertEqual(code, 0, result)
        binding = load_config(self.binding_path)
        binding["enabled"] = True
        self.binding_path.write_text(yaml.safe_dump(binding), encoding="utf-8")
        code, _ = project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)
        self.assertEqual(code, 0)

        fresh = self.start()

        self.assertNotEqual(fresh["task"]["run_id"], task["run_id"])
        self.assertTrue(old_result.exists())
        code, refused = apply_provision_result(str(self.instance), "sample-project", None)
        self.assertEqual(code, 1, refused)
        self.assertFalse(self.adapter_path.exists())

    def test_re_onboard_keeps_a_drafted_provision_on_a_disabled_binding(self):
        """Once the binding is disabled the flag is inert, so re-running it after
        provision-apply does not throw away the run the operator just published."""
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))
        code, result = apply_provision_result(str(self.instance), "sample-project", str(result_path))
        self.assertEqual(code, 0, result)
        draft_bytes = self.draft_path.read_bytes()
        adapter_bytes = self.adapter_path.read_bytes()

        code, artifact = project_add(
            str(self.repo), str(self.instance), dry_run=False, re_onboard=True
        )

        self.assertEqual(code, 0, artifact)
        self.assertEqual(artifact["provision"]["status"], "drafted")
        self.assertEqual(draft_bytes, self.draft_path.read_bytes())
        self.assertEqual(adapter_bytes, self.adapter_path.read_bytes())

    def test_re_onboard_rolls_back_when_the_adapter_delete_fails(self):
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))
        code, result = apply_provision_result(str(self.instance), "sample-project", str(result_path))
        self.assertEqual(code, 0, result)
        binding = load_config(self.binding_path)
        binding["enabled"] = True
        self.binding_path.write_text(yaml.safe_dump(binding), encoding="utf-8")
        before = self.binding_path.read_bytes(), self.draft_path.read_bytes()

        with mock.patch("pathlib.Path.unlink", side_effect=PermissionError("injected")):
            code, artifact = project_add(
                str(self.repo), str(self.instance), dry_run=False, re_onboard=True
            )

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertTrue(self.adapter_path.exists())
        self.assertTrue(load_config(self.binding_path)["enabled"])
        self.assertEqual(before, (self.binding_path.read_bytes(), self.draft_path.read_bytes()))

    def test_project_add_rolls_back_when_stale_adapter_delete_fails(self):
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))
        code, result = apply_provision_result(str(self.instance), "sample-project", str(result_path))
        self.assertEqual(code, 0, result)
        old_draft = self.draft_path.read_bytes()
        (self.repo / "sample.py").write_text("VALUE = 6\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "Change before failed cleanup")
        with mock.patch("pathlib.Path.unlink", side_effect=PermissionError("injected")):
            code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(old_draft, self.draft_path.read_bytes())
        self.assertTrue(self.adapter_path.exists())

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)
        self.assertEqual(code, 0, artifact)
        self.assertEqual(load_config(self.draft_path)["provision"]["status"], "pending")
        self.assertFalse(self.adapter_path.exists())

    def test_project_add_cleans_existing_pending_draft_stale_adapter(self):
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))
        code, result = apply_provision_result(str(self.instance), "sample-project", str(result_path))
        self.assertEqual(code, 0, result)
        draft = load_config(self.draft_path)
        draft["provision"] = {
            "owner": "provision-agent",
            "status": "pending",
            "binding": {"enabled": False},
            "adapter": {
                "status": "unresolved",
                "required_decisions": [
                    "setup.commands",
                    "smoke.command",
                    "validation.ci",
                    "artifact_policy.write_project_files",
                ],
            },
            "findings": [],
        }
        draft["ownership"]["adapter"]["storage"] = "secretary-instance/adapter-drafts/<project>.yaml"
        self.draft_path.write_text(yaml.safe_dump(draft, sort_keys=False), encoding="utf-8")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0, artifact)
        self.assertEqual(load_config(self.draft_path)["provision"]["status"], "pending")
        self.assertFalse(self.adapter_path.exists())

    def test_project_add_waits_for_apply_and_resets_stale_adapter(self):
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))
        workers: list[threading.Thread] = []

        def publish_then_race(first, first_text, second, second_text):
            (self.repo / "sample.py").write_text("VALUE = 5\n", encoding="utf-8")
            git(self.repo, "add", "sample.py")
            git(self.repo, "commit", "-m", "Concurrent scanner update")
            worker = threading.Thread(
                target=lambda: project_add(str(self.repo), str(self.instance), dry_run=False)
            )
            workers.append(worker)
            worker.start()
            publish_pair_atomic(first, first_text, second, second_text)

        with mock.patch("secretary.provision.publish_pair_atomic", side_effect=publish_then_race):
            code, result = apply_provision_result(str(self.instance), "sample-project", str(result_path))

        self.assertEqual(code, 0, result)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        stored = load_config(self.draft_path)
        self.assertEqual(stored["provision"]["status"], "pending")
        self.assertEqual(stored["scanner"]["repo"]["head"], git(self.repo, "rev-parse", "HEAD"))
        self.assertFalse(self.adapter_path.exists())

    def test_malformed_foreign_and_stale_results_do_not_publish(self):
        task = self.start()["task"]
        broken = self.instance / "broken.yaml"
        broken.write_text("not: [valid\n", encoding="utf-8")
        code, result = apply_provision_result(str(self.instance), "sample-project", str(broken))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "result_invalid")

        foreign = self.drafted_result(task)
        foreign["identity"]["id"] = "other"
        code, result = apply_provision_result(str(self.instance), "sample-project", str(self.write_result(foreign)))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "result_foreign")

        stale = self.drafted_result(task)
        stale["input_revision"]["scanner_head"] = "different"
        code, result = apply_provision_result(str(self.instance), "sample-project", str(self.write_result(stale)))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "stale_input")
        self.assertFalse(self.adapter_path.exists())

    def test_apply_rejects_result_when_repo_head_changed_after_start(self):
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))
        (self.repo / "sample.py").write_text("VALUE = 3\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "Change after provision start")

        code, result = apply_provision_result(str(self.instance), "sample-project", str(result_path))

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "stale_input")
        self.assertEqual(result["expected_scanner_head"], task["input_revision"]["scanner_head"])
        self.assertNotEqual(result["expected_scanner_head"], result["actual_scanner_head"])
        self.assertFalse(self.adapter_path.exists())

    def test_undeclared_ci_invalid_adapter_and_project_local_write_are_rejected(self):
        task = self.start()["task"]
        missing_ci = self.drafted_result(task)
        missing_ci["adapter"]["validation"] = {}
        code, result = apply_provision_result(str(self.instance), "sample-project", str(self.write_result(missing_ci)))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "ci_undeclared")

        invalid = self.drafted_result(task)
        invalid["adapter"]["setup"]["commands"] = []
        code, result = apply_provision_result(str(self.instance), "sample-project", str(self.write_result(invalid)))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "adapter_invalid")

        local_write = self.drafted_result(task)
        local_write["adapter"]["artifact_policy"]["write_project_files"] = True
        code, result = apply_provision_result(str(self.instance), "sample-project", str(self.write_result(local_write)))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "result_invalid")
        self.assertFalse(self.adapter_path.exists())

        proposal = self.drafted_result(task)
        proposal["project_local_adapter"] = {
            "proposed": True,
            "path": ".secretary/adapter.yaml",
            "requires_opt_in": True,
        }
        code, result = apply_provision_result(str(self.instance), "sample-project", str(self.write_result(proposal)))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "result_invalid")

    def test_environment_escalation_records_failure_and_same_run_resumes(self):
        task = self.start()["task"]
        env = {
            "version": 1,
            "run_id": task["run_id"],
            "identity": {"id": "sample-project", "adapter": "sample-project"},
            "input_revision": dict(task["input_revision"]),
            "status": "environment_failed",
            "environment": {
                "run_id": task["run_id"],
                "status": "failed",
                "code": "dependency-missing",
                "retry": "same-run",
            },
        }
        code, result = apply_provision_result(str(self.instance), "sample-project", str(self.write_result(env)))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "environment_failed")
        self.assertEqual(result["environment"]["summary"], "required dependency is missing")
        self.assertFalse(self.adapter_path.exists())
        draft = load_config(self.draft_path)
        self.assertEqual(draft["provision"]["status"], "failed")
        self.assertEqual(
            draft["provision"]["findings"][0]["message"],
            "required dependency is missing",
        )
        self.assertFalse(load_config(self.binding_path)["enabled"])

        restarted = self.start()
        self.assertEqual(restarted["task"]["run_id"], task["run_id"])
        ok = self.drafted_result(task)
        code, result = apply_provision_result(str(self.instance), "sample-project", str(self.write_result(ok)))
        self.assertEqual(code, 0, result)
        self.assertTrue(self.adapter_path.exists())

    def test_environment_failure_publication_error_is_structured(self):
        task = self.start()["task"]
        env = {
            "version": 1,
            "run_id": task["run_id"],
            "identity": {"id": "sample-project", "adapter": "sample-project"},
            "input_revision": dict(task["input_revision"]),
            "status": "environment_failed",
            "environment": {
                "run_id": task["run_id"],
                "status": "failed",
                "code": "dependency-missing",
                "retry": "same-run",
            },
        }

        with mock.patch("secretary._fsutil.os.replace", side_effect=OSError(5, "injected")):
            code, result = apply_provision_result(str(self.instance), "sample-project", str(self.write_result(env)))

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "publication_failed")
        self.assertFalse(self.adapter_path.exists())

    def test_environment_failure_after_success_is_transition_conflict(self):
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))
        code, result = apply_provision_result(
            str(self.instance), "sample-project", str(result_path)
        )
        self.assertEqual(code, 0, result)
        draft_before = self.draft_path.read_bytes()
        adapter_before = self.adapter_path.read_bytes()

        environment_failed = {
            "version": 1,
            "run_id": task["run_id"],
            "identity": {"id": "sample-project", "adapter": "sample-project"},
            "input_revision": dict(task["input_revision"]),
            "status": "environment_failed",
            "environment": {
                "run_id": task["run_id"],
                "status": "failed",
                "code": "runtime-error",
                "retry": "same-run",
            },
        }
        result_path = self.write_result(environment_failed)
        code, result = apply_provision_result(
            str(self.instance), "sample-project", str(result_path)
        )

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "transition_conflict")
        self.assertEqual(result["current_status"], "drafted")
        self.assertEqual(self.draft_path.read_bytes(), draft_before)
        self.assertEqual(self.adapter_path.read_bytes(), adapter_before)

    def test_environment_failure_rejects_raw_secret_message(self):
        task = self.start()["task"]
        secret = "ghp_secret_token_value"
        env = {
            "version": 1,
            "run_id": task["run_id"],
            "identity": {"id": "sample-project", "adapter": "sample-project"},
            "input_revision": dict(task["input_revision"]),
            "status": "environment_failed",
            "environment": {
                "run_id": task["run_id"],
                "status": "failed",
                "code": "runtime-error",
                "message": secret,
                "retry": "same-run",
            },
        }

        code, result = apply_provision_result(str(self.instance), "sample-project", str(self.write_result(env)))

        rendered = json.dumps(result, sort_keys=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "result_invalid")
        self.assertNotIn(secret, rendered)
        self.assertNotIn(secret, self.draft_path.read_text(encoding="utf-8"))

    def test_partial_publication_failure_rolls_back_adapter(self):
        task = self.start()["task"]
        result_path = self.write_result(self.drafted_result(task))
        draft_before = self.draft_path.read_bytes()
        real_replace = os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(5, "injected")
            return real_replace(source, target)

        with mock.patch("secretary._fsutil.os.replace", side_effect=fail_second):
            code, result = apply_provision_result(str(self.instance), "sample-project", str(result_path))

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "publication_failed")
        self.assertFalse(self.adapter_path.exists())
        self.assertEqual(draft_before, self.draft_path.read_bytes())

    def test_cli_provision_commands_use_explicit_instance_root(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([
                "project",
                "provision-start",
                "sample-project",
                "--instance",
                str(self.instance),
            ])
        self.assertEqual(code, 0, output.getvalue())
        task = json.loads(output.getvalue())["task"]
        result_path = self.write_result(self.drafted_result(task))

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([
                "project",
                "provision-apply",
                "sample-project",
                "--instance",
                str(self.instance),
                "--result",
                str(result_path),
            ])

        self.assertEqual(code, 0, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "drafted")
        self.assertTrue(self.adapter_path.exists())


if __name__ == "__main__":
    unittest.main()
