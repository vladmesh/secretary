from __future__ import annotations

import copy
import unittest
from pathlib import Path

from secretary.config import validate, validate_instance


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_INSTANCE = REPO_ROOT / "examples" / "instance"


VALID_INSTANCE = {
    "version": 1,
    "name": "example",
    "data_dir": "/var/lib/secretary-data",
    "offsite": {"instance_remote": "git@example.invalid:x/y.git"},
}

VALID_BINDING = {
    "id": "example-project",
    "repo": "/srv/projects/example-project",
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

    def test_adapter_local_ci_with_command_passes(self):
        data = copy.deepcopy(VALID_ADAPTER)
        data["validation"] = {"ci": "local", "command": "make test"}
        self.assertEqual(validate(data, "adapter", "a.yaml"), [])

    def test_adapter_none_ci_with_missing_passes(self):
        data = copy.deepcopy(VALID_ADAPTER)
        data["validation"] = {"ci": "none", "missing": ["tests"]}
        self.assertEqual(validate(data, "adapter", "a.yaml"), [])


class SchemaInvalidTests(unittest.TestCase):
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


class ErrorLeakTests(unittest.TestCase):
    def test_message_does_not_echo_offending_value(self):
        secret = "sk-live-do-not-print-3f9a"
        data = copy.deepcopy(VALID_INSTANCE)
        data["offsite"]["backup_pull_max_age_days"] = secret  # wrong type
        errors = validate(data, "instance", "instance.yaml")
        self.assertTrue(errors)
        blob = "\n".join(str(e) for e in errors)
        self.assertNotIn(secret, blob, blob)
        # path and keyword are still present
        self.assertTrue(any("backup_pull_max_age_days" in e.path for e in errors))
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

    def test_examples_have_no_live_vladmesh_bindings(self):
        blob = "\n".join(
            p.read_text(encoding="utf-8")
            for p in EXAMPLE_INSTANCE.rglob("*")
            if p.is_file()
        ).lower()
        for needle in ("vladmesh", "/home/dev", "dnd-simulator", "personal_site"):
            self.assertNotIn(needle, blob, f"example config leaks {needle!r}")


if __name__ == "__main__":
    unittest.main()
