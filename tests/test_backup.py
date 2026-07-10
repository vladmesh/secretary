from __future__ import annotations

import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.backup import create_backup, verify_backup
from secretary.data import DataExport


class BackupTests(unittest.TestCase):
    def test_create_writes_encrypted_archive_with_expected_structure_and_verify_is_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)

            pipeline_calls: list[str] = []

            def fake_pipeline(action, **_kwargs):
                pipeline_calls.append(action)

            def fake_raw(data_dir_arg):
                raw = data_dir_arg / "board" / "kanboard-raw-20260710T000000Z"
                (raw / "data").mkdir(parents=True)
                (raw / "data" / "db.sqlite").write_bytes(b"sqlite")
                (raw / "manifest.json").write_text("{}", encoding="utf-8")
                return SimpleNamespace(dump_dir=raw)

            def fake_export_all(data_dir_arg, *, copy_transcripts):
                self.assertTrue(copy_transcripts)
                _write_export_surface(data_dir_arg)
                return {
                    "board": DataExport(data_dir_arg / "board" / "cards.json", 1, "board"),
                    "memory": DataExport(data_dir_arg / "memory" / "export.ndjson", 1, "memory"),
                    "runs": DataExport(data_dir_arg / "runs" / "runs.ndjson", 1, "runs"),
                    "transcripts": DataExport(
                        data_dir_arg / "transcripts" / "inventory.json",
                        1,
                        "transcripts",
                    ),
                }

            def fake_encrypt(source, destination, _recipient):
                shutil.copy2(source, destination)

            with (
                mock.patch("secretary.backup._pipeline_action", side_effect=fake_pipeline),
                mock.patch("secretary.backup.raw_kanboard_dump", side_effect=fake_raw),
                mock.patch("secretary.backup.export_all", side_effect=fake_export_all),
            ):
                result = create_backup(
                    instance,
                    recipient="age1example",
                    encrypt=fake_encrypt,
                )

            self.assertEqual(pipeline_calls, ["pause", "resume"])
            self.assertTrue(result.archive.is_file())
            verified = verify_backup(
                result.archive,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )

            self.assertEqual(verified.code, 0, verified.findings)
            self.assertEqual(verified.manifest["version"], 1)
            with tarfile.open(result.archive, "r") as archive:
                names = set(archive.getnames())
            self.assertIn("secretary-backup/versions.json", names)
            self.assertIn("secretary-backup/instance/instance.yaml", names)
            self.assertIn("secretary-backup/secretary-data/board/cards.json", names)
            self.assertIn("secretary-backup/debug/orca-state/inventory.json", names)
            self.assertNotIn("secretary-backup/secretary-data/memory/index.sqlite", names)
            self.assertNotIn("secretary-backup/secretary-data/backups/old.tar.age", names)
            self.assertNotIn("secretary-backup/instance/.env", names)

    def test_create_resumes_pipeline_when_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)
            pipeline_calls: list[str] = []

            def fake_pipeline(action, **_kwargs):
                pipeline_calls.append(action)

            with (
                mock.patch("secretary.backup._pipeline_action", side_effect=fake_pipeline),
                mock.patch(
                    "secretary.backup.raw_kanboard_dump",
                    return_value=SimpleNamespace(dump_dir=data_dir / "board" / "raw"),
                ),
                mock.patch("secretary.backup.export_all", side_effect=RuntimeError("boom")),
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    create_backup(instance, recipient="age1example")

            self.assertEqual(pipeline_calls, ["pause", "resume"])

    def test_verify_returns_2_when_archive_or_key_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing = verify_backup(root / "missing.tar.age")
            archive = root / "archive.tar.age"
            archive.write_bytes(b"not checked")
            no_key = verify_backup(archive)

        self.assertEqual(missing.code, 2)
        self.assertIn("archive not found", missing.findings[0])
        self.assertEqual(no_key.code, 2)
        self.assertIn("age identity is not configured", no_key.findings[0])

    def test_verify_returns_1_for_incomplete_plain_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "broken.tar.age"
            payload = root / "payload"
            (payload / "secretary-backup").mkdir(parents=True)
            with tarfile.open(archive, "w") as tar:
                tar.add(payload / "secretary-backup", arcname="secretary-backup")

            result = verify_backup(
                archive,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )

        self.assertEqual(result.code, 1)
        self.assertTrue(any("missing required archive entry" in item for item in result.findings))


def _write_instance(instance: Path, data_dir: Path) -> None:
    instance.mkdir()
    (instance / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")
    (instance / "instance.yaml").write_text(
        "version: 1\n"
        "name: test\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n"
        "  instance_remote: git@example.invalid:test/secretary-instance.git\n",
        encoding="utf-8",
    )


def _write_export_surface(data_dir: Path) -> None:
    (data_dir / "board").mkdir(parents=True, exist_ok=True)
    (data_dir / "board" / "cards.json").write_text('{"version":1,"cards":[]}\n', encoding="utf-8")
    (data_dir / "board" / "cards.ndjson").write_text("", encoding="utf-8")
    (data_dir / "board" / "export.json").write_text('{"version":1}\n', encoding="utf-8")
    (data_dir / "memory").mkdir(parents=True, exist_ok=True)
    (data_dir / "memory" / "export.ndjson").write_text("{}\n", encoding="utf-8")
    (data_dir / "memory" / "index.sqlite").write_bytes(b"index")
    (data_dir / "runs").mkdir(parents=True, exist_ok=True)
    (data_dir / "runs" / "runs.ndjson").write_text("{}\n", encoding="utf-8")
    (data_dir / "runs" / "watermarks.json").write_text('{"version":1,"files":[]}\n', encoding="utf-8")
    (data_dir / "runs" / "cards.json").write_text('{"version":1,"cards":{}}\n', encoding="utf-8")
    (data_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    (data_dir / "transcripts" / "inventory.json").write_text(
        '{"version":1,"transcripts":[]}\n',
        encoding="utf-8",
    )
    (data_dir / "backups").mkdir(parents=True, exist_ok=True)
    (data_dir / "backups" / "old.tar.age").write_bytes(b"old")


if __name__ == "__main__":
    unittest.main()
