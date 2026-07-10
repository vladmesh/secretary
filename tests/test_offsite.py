from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from secretary.offsite import check_last_fetch, last_fetch_path


class OffsiteTests(unittest.TestCase):
    def test_missing_marker_is_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = _instance(Path(tmpdir), max_age_days=7)

            status = check_last_fetch(instance)

        self.assertEqual(status.findings, [])
        self.assertIn("last_fetch missing", status.warnings[0])

    def test_fresh_marker_is_ok(self):
        now = datetime(2026, 7, 10, 12, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = _instance(Path(tmpdir), max_age_days=7)
            marker = last_fetch_path(Path(instance["data_dir"]))
            marker.parent.mkdir(parents=True)
            marker.write_text("2026-07-09T12:00:00Z\n", encoding="utf-8")

            status = check_last_fetch(instance, now=now)

        self.assertEqual(status.warnings, [])
        self.assertEqual(status.findings, [])

    def test_stale_marker_is_finding(self):
        now = datetime(2026, 7, 10, 12, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = _instance(Path(tmpdir), max_age_days=7)
            marker = last_fetch_path(Path(instance["data_dir"]))
            marker.parent.mkdir(parents=True)
            marker.write_text("2026-07-01T11:00:00Z\n", encoding="utf-8")

            status = check_last_fetch(instance, now=now)

        self.assertEqual(status.warnings, [])
        self.assertIn("stale", status.findings[0])
        self.assertIn("exceeds 7d", status.findings[0])

    def test_invalid_marker_is_finding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = _instance(Path(tmpdir), max_age_days=7)
            marker = last_fetch_path(Path(instance["data_dir"]))
            marker.parent.mkdir(parents=True)
            marker.write_text("not a timestamp\n", encoding="utf-8")

            status = check_last_fetch(instance)

        self.assertEqual(status.warnings, [])
        self.assertIn("invalid", status.findings[0])


def _instance(root: Path, *, max_age_days: int) -> dict[str, object]:
    return {
        "version": 1,
        "name": "test",
        "data_dir": str(root / "secretary-data"),
        "offsite": {
            "instance_remote": "git@example.invalid:test/secretary-instance.git",
            "backup_pull_max_age_days": max_age_days,
        },
    }


if __name__ == "__main__":
    unittest.main()
