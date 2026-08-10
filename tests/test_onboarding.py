from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from secretary.config import load_config, validate, validate_instance
from secretary.cli import main
from secretary.onboarding import ScannerError, project_add


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return result.stdout.strip()


def make_repo(root: Path, *, coverage: bool = True) -> Path:
    repo = root / "Sample_Project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("sample\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='sample'\nversion='0'\n", encoding="utf-8")
    (repo / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    if coverage:
        (repo / "tests").mkdir()
        (repo / "tests" / "test_sample.py").write_text("def test_sample(): pass\n", encoding="utf-8")
        workflow = repo / ".github" / "workflows"
        workflow.mkdir(parents=True)
        (workflow / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Initial")
    return repo


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = make_repo(self.root)
        self.instance = self.root / "instance"
        self.instance.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def binding(self) -> Path:
        return self.instance / "projects" / "sample-project.yaml"

    @property
    def draft(self) -> Path:
        return self.instance / "adapter-drafts" / "sample-project.yaml"

    def test_dry_run_is_deterministic_inventory_without_writes(self):
        first = project_add(str(self.repo), str(self.instance), dry_run=True)
        second = project_add(str(self.repo), str(self.instance), dry_run=True)

        self.assertEqual(first, second)
        code, artifact = first
        self.assertEqual(code, 0)
        self.assertEqual(artifact["scanner"]["facts"]["docs_files"], ["README.md"])
        self.assertEqual(artifact["scanner"]["facts"]["setup_files"], ["pyproject.toml"])
        self.assertFalse(self.binding.exists())
        self.assertFalse(self.draft.exists())
        self.assertEqual(validate(artifact, "onboarding-contract", "output"), [])

    def test_cli_dry_run_prints_valid_contract(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "project",
                    "add",
                    str(self.repo),
                    "--instance",
                    str(self.instance),
                    "--dry-run",
                ]
            )

        artifact = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(validate(artifact, "onboarding-contract", "stdout"), [])
        self.assertFalse(self.binding.exists())

    def test_dirty_worktree_is_only_an_observed_fact(self):
        (self.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=True)

        self.assertEqual(code, 0)
        self.assertFalse(artifact["scanner"]["repo"]["worktree_clean"])
        self.assertNotIn("setup", artifact["draft"]["adapter"])

    def test_publish_creates_disabled_binding_and_separate_valid_draft(self):
        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0)
        binding = load_config(self.binding)
        stored = load_config(self.draft)
        self.assertFalse(binding["enabled"])
        self.assertEqual(stored, artifact)
        self.assertFalse((self.instance / "adapters" / "sample-project.yaml").exists())
        self.assertEqual(validate(binding, "project-binding", self.binding.name), [])
        self.assertEqual(validate(stored, "onboarding-contract", self.draft.name), [])

    def test_doctor_validates_draft_separately_from_canonical_adapters(self):
        data_dir = self.root / "data"
        (self.instance / "instance.yaml").write_text(
            "version: 1\n"
            "name: test\n"
            f"data_dir: {data_dir}\n"
            "offsite:\n"
            "  instance_remote: git@example.invalid:instance.git\n",
            encoding="utf-8",
        )
        project_add(str(self.repo), str(self.instance), dry_run=False)

        report = validate_instance(self.instance)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.adapters, 0)
        self.assertEqual(report.adapter_drafts, 1)

    def test_repeat_is_idempotent_and_preserves_operator_policy(self):
        project_add(str(self.repo), str(self.instance), dry_run=False)
        binding = load_config(self.binding)
        binding["plane"] = "project"
        binding["policy"] = {"code_concurrency": 3}
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")

        second = project_add(str(self.repo), str(self.instance), dry_run=False)
        binding_bytes = self.binding.read_bytes()
        draft_bytes = self.draft.read_bytes()
        third = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(second, third)
        self.assertEqual(binding_bytes, self.binding.read_bytes())
        self.assertEqual(draft_bytes, self.draft.read_bytes())
        self.assertEqual(load_config(self.binding)["policy"], {"code_concurrency": 3})

    def test_new_head_updates_scanner_without_changing_policy(self):
        project_add(str(self.repo), str(self.instance), dry_run=False)
        binding = load_config(self.binding)
        binding["policy"] = {"code_concurrency": 2}
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        draft = load_config(self.draft)
        old_head = draft["scanner"]["repo"]["head"]
        (self.repo / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "Change")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0)
        self.assertNotEqual(artifact["scanner"]["repo"]["head"], old_head)
        self.assertNotIn("policy", artifact["identity"])
        self.assertEqual(load_config(self.binding)["policy"], {"code_concurrency": 2})

    def test_binding_field_in_draft_identity_is_rejected_not_migrated(self):
        """`plane` and `policy` belong to the binding. A draft that also declares them is a
        corrupt contract the schema refuses, and the binding keeps the operator's values."""
        project_add(str(self.repo), str(self.instance), dry_run=False)
        binding = load_config(self.binding)
        binding["plane"] = "project"
        binding["policy"] = {"code_concurrency": 1}
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        draft = load_config(self.draft)
        draft["identity"]["plane"] = "project"
        draft["identity"]["policy"] = {"code_concurrency": 1}
        self.draft.write_text(yaml.safe_dump(draft), encoding="utf-8")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(sorted(load_config(self.draft)["identity"]),
                         ["adapter", "default_branch", "id", "plane", "policy", "repo"])
        stored = load_config(self.binding)
        self.assertEqual(stored["plane"], "project")
        self.assertEqual(stored["policy"], {"code_concurrency": 1})

    def test_unexpected_identity_field_is_rejected_instead_of_migrated(self):
        project_add(str(self.repo), str(self.instance), dry_run=False)
        draft = load_config(self.draft)
        draft["identity"]["unexpected"] = "value"
        self.draft.write_text(yaml.safe_dump(draft), encoding="utf-8")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertIn("unexpected", load_config(self.draft)["identity"])

    def test_missing_and_scanner_failure_are_valid_and_write_nothing(self):
        code, artifact = project_add(str(self.root / "missing"), str(self.instance), dry_run=False)
        self.assertEqual(code, 1)
        self.assertEqual(artifact["scanner"]["findings"][0]["code"], "repo.missing")
        self.assertEqual(validate(artifact, "onboarding-contract", "missing"), [])
        self.assertFalse((self.instance / "projects").exists())

        not_git = self.root / "not-git"
        not_git.mkdir()
        code, artifact = project_add(str(not_git), str(self.instance), dry_run=False)
        self.assertEqual(code, 1)
        self.assertEqual(artifact["scanner"]["findings"][0]["code"], "scanner.failed")
        self.assertEqual(validate(artifact, "onboarding-contract", "failed"), [])
        self.assertFalse((self.instance / "adapter-drafts").exists())

    def test_repo_without_tests_or_ci_records_findings_not_policy(self):
        other = self.root / "other"
        other.mkdir()
        repo = make_repo(other, coverage=False)
        code, artifact = project_add(str(repo), str(self.instance), dry_run=True)

        self.assertEqual(code, 0)
        self.assertEqual(artifact["scanner"]["facts"]["test_files"], [])
        self.assertEqual(artifact["scanner"]["facts"]["ci_files"], [])
        self.assertEqual(
            [item["code"] for item in artifact["scanner"]["findings"]],
            ["tests.not-observed", "ci.not-observed"],
        )
        self.assertEqual(artifact["draft"]["adapter"]["status"], "unresolved")
        self.assertNotIn("validation", artifact["draft"]["adapter"])

    def test_identity_mismatch_and_enabled_binding_do_not_mutate(self):
        project_add(str(self.repo), str(self.instance), dry_run=False)
        for field, value in (("repo", "/different/repo"), ("enabled", True)):
            binding = load_config(self.binding)
            binding[field] = value
            self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
            before = self.binding.read_bytes(), self.draft.read_bytes()

            code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

            self.assertEqual(code, 1)
            self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
            self.assertEqual(validate(artifact, "onboarding-contract", "failure"), [])
            self.assertEqual(before, (self.binding.read_bytes(), self.draft.read_bytes()))
            binding[field] = str(self.repo.resolve()) if field == "repo" else False
            self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")

    def test_conflicting_draft_does_not_mutate(self):
        project_add(str(self.repo), str(self.instance), dry_run=False)
        self.draft.write_text("identity: [broken\n", encoding="utf-8")
        before = self.binding.read_bytes(), self.draft.read_bytes()

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(validate(artifact, "onboarding-contract", "failure"), [])
        self.assertEqual(before, (self.binding.read_bytes(), self.draft.read_bytes()))

    def test_second_replace_failure_rolls_back_first_file(self):
        real_replace = os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(5, "injected")
            return real_replace(source, target)

        with mock.patch("secretary._fsutil.os.replace", side_effect=fail_second):
            code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(validate(artifact, "onboarding-contract", "failure"), [])
        self.assertFalse(self.binding.exists())
        self.assertFalse(self.draft.exists())

    def test_schema_failure_writes_nothing(self):
        problem = mock.Mock()
        problem.__str__ = mock.Mock(return_value="injected schema failure")
        with mock.patch("secretary.onboarding.validate", return_value=[problem]):
            code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(validate(artifact, "onboarding-contract", "failure"), [])
        self.assertFalse(self.binding.exists())
        self.assertFalse(self.draft.exists())

    def legacy_enabled_project(self) -> Path:
        """A binding as onboarding left it before drafts existed: enabled, with a canonical
        adapter, and with no draft, provision run or gate result to justify the enable."""
        project_add(str(self.repo), str(self.instance), dry_run=False)
        binding = load_config(self.binding)
        binding["enabled"] = True
        binding["plane"] = "project"
        binding["policy"] = {"code_concurrency": 3}
        binding["remote"] = "git@example.invalid:sample.git"
        binding["orca_binding"] = "Sample_Project"
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        self.draft.unlink()
        adapter = self.instance / "adapters" / "sample-project.yaml"
        adapter.parent.mkdir(parents=True, exist_ok=True)
        adapter.write_text("version: 1\nid: sample-project\n", encoding="utf-8")
        return adapter

    def test_re_onboard_disables_legacy_binding_and_drops_its_adapter(self):
        adapter = self.legacy_enabled_project()

        code, artifact = project_add(
            str(self.repo), str(self.instance), dry_run=False, re_onboard=True
        )

        self.assertEqual(code, 0)
        binding = load_config(self.binding)
        self.assertFalse(binding["enabled"])
        self.assertFalse(adapter.exists())
        self.assertEqual(binding["plane"], "project")
        self.assertEqual(binding["policy"], {"code_concurrency": 3})
        self.assertEqual(binding["remote"], "git@example.invalid:sample.git")
        self.assertEqual(binding["orca_binding"], "Sample_Project")
        self.assertEqual(artifact["provision"]["status"], "pending")
        self.assertEqual(artifact["gate"]["status"], "pending")
        self.assertEqual(load_config(self.draft), artifact)
        self.assertEqual(validate(binding, "project-binding", self.binding.name), [])
        self.assertEqual(validate(artifact, "onboarding-contract", self.draft.name), [])

    def test_re_onboard_is_idempotent_and_leaves_a_disabled_binding_alone(self):
        self.legacy_enabled_project()
        project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)
        binding_bytes = self.binding.read_bytes()
        draft_bytes = self.draft.read_bytes()

        code, _ = project_add(
            str(self.repo), str(self.instance), dry_run=False, re_onboard=True
        )

        self.assertEqual(code, 0)
        self.assertEqual(binding_bytes, self.binding.read_bytes())
        self.assertEqual(draft_bytes, self.draft.read_bytes())

    def test_re_onboard_fails_closed_on_identity_mismatch(self):
        self.legacy_enabled_project()
        binding = load_config(self.binding)
        binding["repo"] = "/different/repo"
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        adapter = self.instance / "adapters" / "sample-project.yaml"
        before = self.binding.read_bytes(), adapter.read_bytes()

        code, artifact = project_add(
            str(self.repo), str(self.instance), dry_run=False, re_onboard=True
        )

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(validate(artifact, "onboarding-contract", "failure"), [])
        self.assertEqual(before, (self.binding.read_bytes(), adapter.read_bytes()))
        self.assertFalse(self.draft.exists())

    def test_re_onboard_fails_closed_on_a_schema_invalid_binding(self):
        self.legacy_enabled_project()
        binding = load_config(self.binding)
        binding["unexpected"] = "value"
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        adapter = self.instance / "adapters" / "sample-project.yaml"
        before = self.binding.read_bytes(), adapter.read_bytes()

        code, artifact = project_add(
            str(self.repo), str(self.instance), dry_run=False, re_onboard=True
        )

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(before, (self.binding.read_bytes(), adapter.read_bytes()))

    def test_re_onboard_on_a_stale_head_publishes_the_current_one(self):
        self.legacy_enabled_project()
        (self.repo / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "Change")
        head = git(self.repo, "rev-parse", "HEAD")

        code, artifact = project_add(
            str(self.repo), str(self.instance), dry_run=False, re_onboard=True
        )

        self.assertEqual(code, 0)
        self.assertEqual(artifact["scanner"]["repo"]["head"], head)
        self.assertEqual(artifact["provision"]["status"], "pending")

    def test_re_onboard_scanner_failure_leaves_the_enable_in_place(self):
        adapter = self.legacy_enabled_project()
        before = self.binding.read_bytes(), adapter.read_bytes()

        with mock.patch(
            "secretary.onboarding.scan_repo", side_effect=ScannerError("injected")
        ):
            code, artifact = project_add(
                str(self.repo), str(self.instance), dry_run=False, re_onboard=True
            )

        self.assertEqual(code, 1)
        self.assertEqual(artifact["scanner"]["findings"][0]["code"], "scanner.failed")
        self.assertEqual(before, (self.binding.read_bytes(), adapter.read_bytes()))
        self.assertFalse(self.draft.exists())

    def test_re_onboard_rollback_leaves_the_enabled_binding_untouched(self):
        adapter = self.legacy_enabled_project()
        before = self.binding.read_bytes(), adapter.read_bytes()
        real_replace = os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(5, "injected")
            return real_replace(source, target)

        with mock.patch("secretary._fsutil.os.replace", side_effect=fail_second):
            code, artifact = project_add(
                str(self.repo), str(self.instance), dry_run=False, re_onboard=True
            )

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(before, (self.binding.read_bytes(), adapter.read_bytes()))
        self.assertFalse(self.draft.exists())

    def test_cli_re_onboard_publishes_a_disabled_binding(self):
        adapter = self.legacy_enabled_project()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "project",
                    "add",
                    str(self.repo),
                    "--instance",
                    str(self.instance),
                    "--re-onboard",
                ]
            )

        artifact = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(validate(artifact, "onboarding-contract", "stdout"), [])
        self.assertFalse(load_config(self.binding)["enabled"])
        self.assertFalse(adapter.exists())

    def test_add_without_the_flag_still_refuses_an_enabled_binding(self):
        adapter = self.legacy_enabled_project()
        before = self.binding.read_bytes(), adapter.read_bytes()

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(
            artifact["draft"]["findings"][-1]["message"], "existing binding is enabled"
        )
        self.assertEqual(before, (self.binding.read_bytes(), adapter.read_bytes()))

    def test_scanner_cannot_add_a_target(self):
        _, artifact = project_add(str(self.repo), str(self.instance), dry_run=True)
        artifact["scanner"]["repo"]["path"] = "/different/repo"
        self.assertTrue(validate(artifact, "onboarding-contract", "foreign-target"))


if __name__ == "__main__":
    unittest.main()
