from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from secretary.cli import main


FIXTURE = Path(__file__).parent / "fixtures" / "mock-instance.yaml"


class CliTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()

    def test_doctor_dry_run_reads_mock_instance(self):
        code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(FIXTURE)])

        self.assertEqual(code, 0)
        self.assertIn("Secretary doctor report", output)
        self.assertIn("mode: dry-run", output)
        self.assertIn("name: mock-instance", output)
        self.assertIn("projects: 1", output)
        self.assertIn("host changes: none", output)

    def test_doctor_requires_dry_run(self):
        code, output = self.run_cli(["doctor", "--instance", str(FIXTURE)])

        self.assertEqual(code, 2)
        self.assertIn("requires --dry-run", output)

    def test_doctor_reports_unreadable_instance_config(self):
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(FIXTURE)])

        self.assertEqual(code, 1)
        self.assertIn("secretary doctor: cannot read instance config", output)
        self.assertNotIn("Traceback", output)

    def test_doctor_reports_non_utf8_instance_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            instance.write_bytes(b"\xff")

            code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(instance)])

        self.assertEqual(code, 1)
        self.assertIn("secretary doctor: cannot decode instance config as UTF-8", output)
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
