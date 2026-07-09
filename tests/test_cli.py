from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from secretary.cli import main


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_INSTANCE = REPO_ROOT / "examples" / "instance"


class CliTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()

    def test_doctor_dry_run_validates_example_instance(self):
        code, output = self.run_cli(
            ["doctor", "--dry-run", "--instance", str(EXAMPLE_INSTANCE)]
        )

        self.assertEqual(code, 0, output)
        self.assertIn("Secretary doctor report", output)
        self.assertIn("mode: dry-run", output)
        self.assertIn("name: example-secretary", output)
        self.assertIn("projects: 1", output)
        self.assertIn("adapters: 1", output)
        self.assertIn("data manifest: present", output)
        self.assertIn("host changes: none", output)
        self.assertIn("status: ok", output)

    def test_doctor_accepts_instance_file_path(self):
        code, output = self.run_cli(
            ["doctor", "--dry-run", "--instance", str(EXAMPLE_INSTANCE / "instance.yaml")]
        )

        self.assertEqual(code, 0, output)
        self.assertIn("status: ok", output)

    def test_doctor_requires_dry_run(self):
        code, output = self.run_cli(
            ["doctor", "--instance", str(EXAMPLE_INSTANCE)]
        )

        self.assertEqual(code, 2)
        self.assertIn("requires --dry-run", output)

    def test_doctor_reports_missing_field_with_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            instance.write_text("version: 1\nname: broken\n", encoding="utf-8")

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance)]
            )

        self.assertEqual(code, 1)
        self.assertIn("config problem", output)
        # Missing required 'data_dir' / 'offsite' surfaced at the root.
        self.assertIn("data_dir", output)
        self.assertNotIn("Traceback", output)

    def test_doctor_reports_bad_nested_field_with_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            shutil.copytree(EXAMPLE_INSTANCE, instance_dir)
            binding = instance_dir / "projects" / "example-project.yaml"
            binding.write_text(
                "id: Bad_Id\nrepo: /srv/x\nenabled: true\n"
                "adapter: example-project\ndefault_branch: main\n",
                encoding="utf-8",
            )

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance_dir)]
            )

        self.assertEqual(code, 1)
        self.assertIn("example-project.yaml: id:", output)
        self.assertNotIn("Traceback", output)

    def test_doctor_reports_unreadable_instance_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "does-not-exist.yaml"
            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(missing)]
            )

        self.assertEqual(code, 1)
        self.assertIn("config not found", output)
        self.assertNotIn("Traceback", output)

    def test_doctor_yaml_parse_error_does_not_leak_source(self):
        secret = "sk-live-DO-NOT-LEAK-9f3a2b"
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            # Unterminated quoted scalar: PyYAML's raw message would echo the line.
            instance.write_text(f'name: "{secret}\n', encoding="utf-8")

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance)]
            )

        self.assertEqual(code, 1)
        self.assertIn("cannot parse config", output)
        self.assertIn("line", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("Traceback", output)

    def test_doctor_reports_non_utf8_instance_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            instance.write_bytes(b"\xff")

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance)]
            )

        self.assertEqual(code, 1)
        self.assertIn("cannot decode config as UTF-8", output)
        self.assertNotIn("Traceback", output)

    def test_target_command_stubs_are_explicit(self):
        for command in (
            ["reconcile"],
            ["backup"],
            ["restore"],
            ["project", "add", "example"],
            ["task", "list"],
            ["memory", "commit"],
        ):
            with self.subTest(command=command):
                code, output = self.run_cli(command)
                self.assertEqual(code, 1)
                self.assertIn("not implemented", output)


if __name__ == "__main__":
    unittest.main()
