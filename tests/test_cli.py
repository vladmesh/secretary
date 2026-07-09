from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "secretary", *args],
            cwd=ROOT,
            check=False,
            text=True,
            capture_output=True,
        )

    def test_help_smoke(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("doctor", result.stdout)

    def test_doctor_dry_run_reads_mock_instance(self) -> None:
        result = self.run_cli("doctor", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("host changes: none", result.stdout)


if __name__ == "__main__":
    unittest.main()
