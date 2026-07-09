from __future__ import annotations

import unittest
from pathlib import Path

from secretary.schema_checks import (
    load_json,
    validate_adapter,
    validate_data_manifest,
    validate_instance_config,
    validate_project_binding,
)


ROOT = Path(__file__).resolve().parents[1]


class SchemaTests(unittest.TestCase):
    def test_schema_files_are_json_objects(self) -> None:
        for path in sorted((ROOT / "schemas").glob("*.schema.json")):
            data = load_json(path)
            self.assertEqual(data.get("$schema"), "https://json-schema.org/draft/2020-12/schema")
            self.assertEqual(data.get("type"), "object")

    def test_example_configs_match_required_shape(self) -> None:
        examples = ROOT / "config" / "examples"
        checks = {
            "instance.example.json": validate_instance_config,
            "project-binding.example.json": validate_project_binding,
            "adapter.example.json": validate_adapter,
            "data-manifest.example.json": validate_data_manifest,
        }
        for filename, check in checks.items():
            with self.subTest(filename=filename):
                errors = check(load_json(examples / filename))
                self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
