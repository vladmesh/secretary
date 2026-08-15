from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from secretary.config import validate, validate_instance

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_INSTANCE = REPO_ROOT / "examples" / "instance"
ONBOARDING_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "onboarding"


VALID_INSTANCE = {
    "version": 1,
    "name": "example",
    "data_dir": "/var/lib/secretary-data",
    "offsite": {"instance_remote": "git@example.invalid:x/y.git"},
}

VALID_BINDING = {
    "id": "example-project",
    "repo": "/srv/projects/example-project",
    "orca_binding": "example-project",
    "enabled": True,
    "adapter": "example-project",
    "default_branch": "main",
}

VALID_ADAPTER = {
    "setup": {"commands": ["npm ci"]},
    "smoke": {"command": "node --check index.js"},
    "validation": {"ci": "github"},
    "artifact_policy": {"write_project_files": False},
}

VALID_MANIFEST = {
    "version": 1,
    "data_dir": "/var/lib/secretary-data",
    "components": {
        "board": {"path": "board"},
        "memory": {
            "path": "memory",
            "facts": "memory/facts",
            "export": "memory/export.ndjson",
            "index": "memory/index.sqlite",
        },
        "runs": {"path": "runs"},
        "transcripts": {"path": "transcripts"},
        "artifacts": {"path": "artifacts"},
        "backups": {"path": "backups"},
    },
}


class SchemaValidTests(unittest.TestCase):
    def test_valid_configs_pass(self):
        cases = [
            (VALID_INSTANCE, "instance"),
            (VALID_BINDING, "project-binding"),
            (VALID_ADAPTER, "adapter"),
            (VALID_MANIFEST, "data-manifest"),
        ]
        for data, schema in cases:
            with self.subTest(schema=schema):
                self.assertEqual(validate(data, schema, schema), [])

    def test_onboarding_happy_path_fixture_passes(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        self.assertEqual(validate(data, "onboarding-contract", "happy-path.json"), [])

    def test_adapter_local_ci_with_command_passes(self):
        data = copy.deepcopy(VALID_ADAPTER)
        data["validation"] = {"ci": "local", "command": "make test"}
        self.assertEqual(validate(data, "adapter", "a.yaml"), [])

    def test_adapter_none_ci_with_missing_passes(self):
        data = copy.deepcopy(VALID_ADAPTER)
        data["validation"] = {"ci": "none", "missing": ["tests"]}
        self.assertEqual(validate(data, "adapter", "a.yaml"), [])

    def test_adapter_github_ci_with_required_checks_passes(self):
        data = copy.deepcopy(VALID_ADAPTER)
        data["validation"] = {"ci": "github", "required_checks": ["test"]}
        self.assertEqual(validate(data, "adapter", "a.yaml"), [])

    def test_adapter_github_ci_without_required_checks_stays_valid(self):
        """secretary-841: adapters that have not migrated keep validating; the gate falls back to
        judging by every check on the sha."""
        data = copy.deepcopy(VALID_ADAPTER)
        data["validation"] = {"ci": "github"}
        self.assertEqual(validate(data, "adapter", "a.yaml"), [])

    def test_onboarding_draft_accepts_required_checks(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["provision"]["adapter"]["validation"] = {"ci": "github", "required_checks": ["test"]}
        self.assertEqual(validate(data, "onboarding-contract", "required-checks.json"), [])


class SchemaInvalidTests(unittest.TestCase):
    def test_instance_rejects_relative_data_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            instance.write_text(
                "version: 1\n"
                "name: example\n"
                "data_dir: secretary-data\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )

            report = validate_instance(instance)

        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.path == "data_dir" and "pattern" in error.message for error in report.errors),
            report.errors,
        )

    def test_instance_missing_offsite_remote(self):
        data = copy.deepcopy(VALID_INSTANCE)
        del data["offsite"]["instance_remote"]
        errors = validate(data, "instance", "instance.yaml")
        self.assertTrue(errors)
        self.assertTrue(any("offsite" in e.path for e in errors), errors)

    def test_instance_rejects_unknown_field(self):
        data = copy.deepcopy(VALID_INSTANCE)
        data["mystery"] = True
        errors = validate(data, "instance", "instance.yaml")
        self.assertTrue(errors)

    def test_instance_rejects_partial_budget_below_resolved_default(self):
        for budget in ({"hard": 2}, {"signal": 10}):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as tmpdir:
                instance = Path(tmpdir) / "instance.yaml"
                document = copy.deepcopy(VALID_INSTANCE)
                document["sprint_budget"] = budget
                instance.write_text(json.dumps(document), encoding="utf-8")
                report = validate_instance(instance)
                self.assertTrue(any(error.path == "sprint_budget" for error in report.errors), report.errors)

    def test_open_sprint_limit_accepts_only_one_or_two(self):
        for limit in (1, 2):
            data = copy.deepcopy(VALID_INSTANCE)
            data["open_sprint_limit"] = limit
            self.assertEqual(validate(data, "instance", "instance.yaml"), [], limit)

    def test_an_invalid_open_sprint_limit_is_reported_with_its_fallback(self):
        """A value nobody can honour has to be visible: it silently keeps the limit at one."""
        for limit in (0, 3, -1, 1.5, "", "2", True, None):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as tmpdir:
                instance = Path(tmpdir) / "instance.yaml"
                document = copy.deepcopy(VALID_INSTANCE)
                document["open_sprint_limit"] = limit
                instance.write_text(json.dumps(document), encoding="utf-8")

                report = validate_instance(instance)

                self.assertTrue(
                    any(
                        error.path == "open_sprint_limit" and "one open sprint" in error.message
                        for error in report.errors
                    ),
                    report.errors,
                )

    def test_host_units_require_unit_prefix(self):
        data = copy.deepcopy(VALID_INSTANCE)
        # units with no unit_prefix cannot yield unmanaged-on-host, so reject it.
        data["host"] = {"units": ["secretary-pipeline.service"]}
        errors = validate(data, "instance", "instance.yaml")
        self.assertTrue(errors)
        self.assertTrue(any("unit_prefix" in e.message for e in errors), errors)

    def test_host_units_with_prefix_pass(self):
        data = copy.deepcopy(VALID_INSTANCE)
        data["host"] = {
            "units": ["secretary-pipeline.service", "secretary-pipeline.timer"],
            "unit_prefix": "secretary-",
        }
        self.assertEqual(validate(data, "instance", "instance.yaml"), [])

    def test_host_units_require_type_suffix(self):
        # A suffixless name never matches systemctl output, so reject it up front.
        data = copy.deepcopy(VALID_INSTANCE)
        data["host"] = {"units": ["secretary-pipeline"], "unit_prefix": "secretary-"}
        errors = validate(data, "instance", "instance.yaml")
        self.assertTrue(errors)
        self.assertTrue(any(e.path.startswith("host.units") for e in errors), errors)

    def test_host_empty_units_need_no_prefix(self):
        data = copy.deepcopy(VALID_INSTANCE)
        data["host"] = {"units": []}
        self.assertEqual(validate(data, "instance", "instance.yaml"), [])

    def test_binding_bad_id_pattern(self):
        data = copy.deepcopy(VALID_BINDING)
        data["id"] = "Bad_Id"
        errors = validate(data, "project-binding", "b.yaml")
        self.assertTrue(errors)
        self.assertTrue(any(e.path == "id" for e in errors), errors)

    def test_binding_wrong_type(self):
        data = copy.deepcopy(VALID_BINDING)
        data["enabled"] = "yes"
        errors = validate(data, "project-binding", "b.yaml")
        self.assertTrue(any(e.path == "enabled" for e in errors), errors)

    def test_adapter_missing_artifact_policy(self):
        data = copy.deepcopy(VALID_ADAPTER)
        del data["artifact_policy"]
        errors = validate(data, "adapter", "a.yaml")
        self.assertTrue(errors)

    def test_adapter_bad_ci_enum(self):
        data = copy.deepcopy(VALID_ADAPTER)
        data["validation"]["ci"] = "jenkins"
        errors = validate(data, "adapter", "a.yaml")
        self.assertTrue(any("validation" in e.path for e in errors), errors)

    def test_adapter_local_ci_requires_command(self):
        data = copy.deepcopy(VALID_ADAPTER)
        data["validation"] = {"ci": "local"}
        errors = validate(data, "adapter", "a.yaml")
        self.assertTrue(errors)
        self.assertTrue(any("command" in e.message for e in errors), errors)

    def test_adapter_none_ci_requires_missing(self):
        data = copy.deepcopy(VALID_ADAPTER)
        data["validation"] = {"ci": "none"}
        errors = validate(data, "adapter", "a.yaml")
        self.assertTrue(errors)
        self.assertTrue(any("missing" in e.message for e in errors), errors)

    def test_adapter_rejects_empty_or_duplicate_required_checks(self):
        for value in ([], [""], ["test", "test"]):
            with self.subTest(required_checks=value):
                data = copy.deepcopy(VALID_ADAPTER)
                data["validation"] = {"ci": "github", "required_checks": value}
                self.assertTrue(validate(data, "adapter", "a.yaml"))

    def test_onboarding_draft_rejects_duplicate_required_checks(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["provision"]["adapter"]["validation"] = {"ci": "github", "required_checks": ["test", "test"]}
        errors = validate(data, "onboarding-contract", "dup-required-checks.json")
        self.assertTrue(any("required_checks" in e.path for e in errors), errors)

    def test_adapter_none_ci_rejects_empty_missing(self):
        data = copy.deepcopy(VALID_ADAPTER)
        data["validation"] = {"ci": "none", "missing": []}
        errors = validate(data, "adapter", "a.yaml")
        self.assertTrue(errors)

    def test_manifest_missing_component(self):
        data = copy.deepcopy(VALID_MANIFEST)
        del data["components"]["memory"]
        errors = validate(data, "data-manifest", "m.json")
        self.assertTrue(any("components" in e.path for e in errors), errors)

    def test_onboarding_rejects_enabled_binding_before_gate(self):
        data = json.loads((ONBOARDING_FIXTURES / "enabled-before-gate.json").read_text(encoding="utf-8"))
        errors = validate(data, "onboarding-contract", "enabled-before-gate.json")
        self.assertTrue(any(e.path in {"draft.binding.enabled", "provision.binding.enabled"} for e in errors), errors)

    def test_onboarding_rejects_passed_gate_with_failed_validation(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["gate"]["checks"]["validation"] = "failed"
        errors = validate(data, "onboarding-contract", "failed-validation.json")
        self.assertTrue(any(e.path == "gate.checks.validation" for e in errors), errors)

    def test_onboarding_rejects_passed_gate_with_error_finding(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["gate"]["findings"] = [{"code": "gate.failed", "severity": "error"}]
        errors = validate(data, "onboarding-contract", "gate-error.json")
        self.assertTrue(any(e.path == "gate.findings" for e in errors), errors)

    def test_onboarding_accepts_missing_repo_scanner_failure(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["scanner"]["status"] = "failed"
        data["scanner"]["repo"] = {"exists": False}
        data["scanner"]["findings"] = [{"code": "repo.missing", "severity": "error"}]
        data["gate"]["status"] = "failed"
        data["gate"]["binding"]["enabled"] = False
        data["gate"]["checks"]["setup"] = "not-run"
        data["gate"]["checks"]["smoke"] = "not-run"
        data["gate"]["checks"]["validation"] = "not-run"
        data["gate"]["findings"] = [{"code": "repo.missing", "severity": "error"}]
        self.assertEqual(validate(data, "onboarding-contract", "missing-repo.json"), [])

    def test_onboarding_accepts_declared_missing_only_for_no_ci(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["provision"]["adapter"]["validation"] = {"ci": "none", "missing": ["tests"]}
        data["gate"]["checks"]["validation"] = "declared-missing"
        self.assertEqual(validate(data, "onboarding-contract", "no-ci-declared-missing.json"), [])

    def test_onboarding_rejects_declared_missing_with_github_ci(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["gate"]["checks"]["validation"] = "declared-missing"
        errors = validate(data, "onboarding-contract", "github-declared-missing.json")
        self.assertTrue(any(e.path == "provision.adapter.validation.ci" for e in errors), errors)

    def test_onboarding_rejects_declared_missing_with_local_ci(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["provision"]["adapter"]["validation"] = {"ci": "local", "command": "make test"}
        data["gate"]["checks"]["validation"] = "declared-missing"
        errors = validate(data, "onboarding-contract", "local-declared-missing.json")
        self.assertTrue(any(e.path == "provision.adapter.validation.ci" for e in errors), errors)

    def test_onboarding_rejects_passed_validation_with_no_ci(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["provision"]["adapter"]["validation"] = {"ci": "none", "missing": ["tests"]}
        errors = validate(data, "onboarding-contract", "no-ci-passed-validation.json")
        self.assertTrue(any(e.path == "gate.checks.validation" for e in errors), errors)

    def test_onboarding_rejects_passed_gate_with_failed_scanner(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["scanner"]["status"] = "failed"
        data["scanner"]["findings"] = [{"code": "scanner.failed", "severity": "error"}]
        errors = validate(data, "onboarding-contract", "failed-scanner-passed-gate.json")
        self.assertTrue(any(e.path in {"scanner.status", "scanner.findings"} for e in errors), errors)

    def test_onboarding_rejects_passed_gate_with_missing_repo(self):
        # A passed gate must sit on a green scanner chain: repo.exists = true.
        # An "ok" status with exists = false is still a missing-repo error path.
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["scanner"]["repo"] = {"exists": False}
        errors = validate(data, "onboarding-contract", "missing-repo-passed-gate.json")
        self.assertTrue(any(e.path == "scanner.repo.exists" for e in errors), errors)

    def test_onboarding_rejects_passed_gate_with_failed_provision(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        data["provision"]["status"] = "failed"
        data["provision"]["findings"] = [{"code": "draft.invalid", "severity": "error"}]
        errors = validate(data, "onboarding-contract", "failed-provision-passed-gate.json")
        self.assertTrue(any(e.path in {"provision.status", "provision.findings"} for e in errors), errors)

    def test_onboarding_rejects_passed_gate_with_upstream_error_findings(self):
        cases = [
            ("scanner", {"code": "scanner.failed", "severity": "error"}),
            ("provision", {"code": "ci.undeclared", "severity": "error"}),
        ]
        for section, finding in cases:
            with self.subTest(section=section):
                data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
                data[section]["findings"] = [finding]
                errors = validate(data, "onboarding-contract", f"{section}-error-finding-passed-gate.json")
                self.assertTrue(any(e.path == f"{section}.findings" for e in errors), errors)

    def test_onboarding_rejects_undeclared_ci(self):
        data = json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))
        del data["provision"]["adapter"]["validation"]["ci"]
        data["provision"]["findings"] = [{"code": "ci.undeclared", "severity": "error"}]
        errors = validate(data, "onboarding-contract", "undeclared-ci.json")
        self.assertTrue(any(e.path == "provision.adapter.validation" for e in errors), errors)


class OnboardingIdentityTests(unittest.TestCase):
    """Binding identity has one source; divergence must be unrepresentable."""

    IDENTITY_FIELDS = {
        "id": "other-project",
        "repo": "/srv/projects/other-project",
        "adapter": "other-adapter",
        "default_branch": "release",
    }

    def _happy(self):
        return json.loads((ONBOARDING_FIXTURES / "happy-path.json").read_text(encoding="utf-8"))

    def test_single_identity_is_required(self):
        data = self._happy()
        del data["identity"]
        errors = validate(data, "onboarding-contract", "no-identity.json")
        self.assertTrue(any(e.path == "<root>" and "identity" in e.message for e in errors), errors)

    def test_stage_binding_carries_only_enabled(self):
        # The happy path binding is exactly {"enabled": ...}; nothing else.
        data = self._happy()
        for stage in ("draft", "provision", "gate"):
            self.assertEqual(list(data[stage]["binding"]), ["enabled"], stage)

    def test_stage_binding_rejects_any_identity_field(self):
        # A second copy of any identity fact cannot even be attached to a stage,
        # so a draft/provision/gate binding can never point at another project.
        for stage in ("draft", "provision", "gate"):
            for field, value in self.IDENTITY_FIELDS.items():
                with self.subTest(stage=stage, field=field):
                    data = self._happy()
                    data[stage]["binding"][field] = value
                    errors = validate(data, "onboarding-contract", f"{stage}-{field}.json")
                    self.assertTrue(
                        any(e.path == f"{stage}.binding" for e in errors), errors
                    )

    def test_scanner_cannot_name_its_own_repo_or_branch(self):
        # The scanned target is identity.repo at identity.default_branch. The
        # scanner reports observations only; it cannot carry a repo path or
        # branch that disagrees with identity, in either direction.
        for field, value in (("input", "/srv/projects/other-project"),
                             ("default_branch", "release")):
            with self.subTest(field=field):
                data = self._happy()
                data["scanner"]["repo"][field] = value
                errors = validate(data, "onboarding-contract", f"scanner-repo-{field}.json")
                self.assertTrue(any(e.path == "scanner.repo" for e in errors), errors)

    def test_passed_gate_cannot_enable_a_foreign_binding(self):
        # The prior review's escape: passed gate carrying another project's
        # id/repo/adapter. There is nowhere to put those fields now.
        data = self._happy()
        data["gate"]["binding"].update(
            {
                "id": "other-project",
                "repo": "/srv/projects/other-project",
                "adapter": "other-adapter",
                "default_branch": "release",
            }
        )
        errors = validate(data, "onboarding-contract", "gate-foreign-binding.json")
        self.assertTrue(any(e.path == "gate.binding" for e in errors), errors)


class ErrorLeakTests(unittest.TestCase):
    def test_message_does_not_echo_offending_value(self):
        secret = "sk-live-do-not-print-3f9a"
        data = copy.deepcopy(VALID_INSTANCE)
        data["host"] = {"memory_dim": secret}  # wrong type
        errors = validate(data, "instance", "instance.yaml")
        self.assertTrue(errors)
        blob = "\n".join(str(e) for e in errors)
        self.assertNotIn(secret, blob, blob)
        # path and keyword are still present
        self.assertTrue(any("memory_dim" in e.path for e in errors))
        self.assertIn("expected type integer", blob)

    def test_pattern_error_does_not_echo_value(self):
        data = copy.deepcopy(VALID_BINDING)
        data["id"] = "SECRET_ID_LEAK"
        errors = validate(data, "project-binding", "b.yaml")
        blob = "\n".join(str(e) for e in errors)
        self.assertNotIn("SECRET_ID_LEAK", blob, blob)

    def test_unknown_field_name_never_echoed(self):
        # Even a lowercase, identifier-shaped token must not reach the output.
        for name in ("sk_live_abcd1234efgh5678ijkl9012mnop", "mystery"):
            with self.subTest(name=name):
                data = copy.deepcopy(VALID_INSTANCE)
                data[name] = True
                errors = validate(data, "instance", "instance.yaml")
                blob = "\n".join(str(e) for e in errors)
                self.assertNotIn(name, blob, blob)
                self.assertIn("unexpected propert", blob)


class ExampleInstanceTests(unittest.TestCase):
    def test_example_instance_is_valid(self):
        report = validate_instance(EXAMPLE_INSTANCE)
        self.assertTrue(report.ok, [str(e) for e in report.errors])
        self.assertEqual(report.projects, 1)
        self.assertEqual(report.adapters, 1)
        self.assertTrue(report.has_manifest)

    def test_examples_have_no_live_owner_bindings(self):
        blob = "\n".join(
            p.read_text(encoding="utf-8")
            for p in EXAMPLE_INSTANCE.rglob("*")
            if p.is_file()
        ).lower()
        for needle in ("/home/dev", "dnd-simulator", "personal_site"):
            self.assertNotIn(needle, blob, f"example config leaks {needle!r}")


if __name__ == "__main__":
    unittest.main()
