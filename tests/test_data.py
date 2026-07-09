from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.data import init_layout, manifest_for, raw_kanboard_dump


class DataLayoutTests(unittest.TestCase):
    def test_init_layout_creates_target_dirs_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"

            layout = init_layout(data_dir)

            for name in ("board", "memory", "runs", "transcripts", "artifacts", "backups"):
                self.assertTrue((data_dir / name).is_dir(), name)
            self.assertEqual(layout.manifest_path, data_dir / "data-manifest.json")
            self.assertIn('"data_dir"', layout.manifest_path.read_text(encoding="utf-8"))

    def test_init_layout_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"

            first = init_layout(data_dir)
            second = init_layout(data_dir)

            self.assertTrue(first.created_dirs)
            self.assertEqual(second.created_dirs, [])
            self.assertEqual(manifest_for(data_dir), manifest_for(data_dir))

    def test_init_layout_preserves_manifest_when_publish_short_writes(self):
        original_write_text = Path.write_text

        def partial_manifest_write(path, text, *args, **kwargs):
            if path.name.startswith(".data-manifest.json."):
                return original_write_text(path, "{partial", *args, **kwargs)
            return original_write_text(path, text, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            manifest_path = data_dir / "data-manifest.json"
            original_manifest = manifest_path.read_text(encoding="utf-8")

            with mock.patch("secretary.data.Path.write_text", new=partial_manifest_write):
                with self.assertRaises(RuntimeError):
                    init_layout(data_dir)

            self.assertEqual(manifest_path.read_text(encoding="utf-8"), original_manifest)
            self.assertEqual(list(data_dir.glob(".data-manifest.json.*.tmp")), [])

    def test_init_layout_preserves_manifest_when_publish_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            manifest_path = data_dir / "data-manifest.json"
            original_manifest = manifest_path.read_text(encoding="utf-8")

            with mock.patch("secretary.data.Path.write_text", side_effect=OSError("full")):
                with self.assertRaises(RuntimeError):
                    init_layout(data_dir)

            self.assertEqual(manifest_path.read_text(encoding="utf-8"), original_manifest)
            self.assertEqual(list(data_dir.glob(".data-manifest.json.*.tmp")), [])

    def test_init_layout_wraps_directory_prepare_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            data_dir.write_text("not a directory", encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError, "cannot prepare secretary-data layout"
            ):
                init_layout(data_dir)

    def test_init_layout_wraps_manifest_tempfile_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"

            with mock.patch(
                "secretary.data.tempfile.mkstemp", side_effect=PermissionError("denied")
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "could not write data manifest"
                ):
                    init_layout(data_dir)


class RawKanboardDumpTests(unittest.TestCase):
    @mock.patch("secretary.data.subprocess.run")
    def test_raw_kanboard_dump_copies_into_unique_board_dir(self, run):
        def fake_run(command, **_kwargs):
            destination = Path(command[-1])
            destination.mkdir(parents=True)
            (destination / "db.sqlite").write_bytes(b"sqlite")

        run.side_effect = fake_run
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"

            first = raw_kanboard_dump(data_dir)
            second = raw_kanboard_dump(data_dir)

            self.assertNotEqual(first.dump_dir, second.dump_dir)
            self.assertTrue((first.dump_dir / "data" / "db.sqlite").is_file())
            self.assertTrue((first.dump_dir / "manifest.json").is_file())
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0][0:2], ["docker", "cp"])

    @mock.patch("secretary.data.subprocess.run")
    def test_raw_kanboard_dump_cleans_staging_when_manifest_write_fails(self, run):
        def fake_run(command, **_kwargs):
            destination = Path(command[-1])
            destination.mkdir(parents=True)
            (destination / "db.sqlite").write_bytes(b"sqlite")

        run.side_effect = fake_run
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"

            with mock.patch("secretary.data.Path.write_text", side_effect=OSError("full")):
                with self.assertRaises(RuntimeError):
                    raw_kanboard_dump(data_dir)

            board_entries = list((data_dir / "board").iterdir())

        self.assertEqual(board_entries, [])

    @mock.patch("secretary.data.subprocess.run")
    def test_raw_kanboard_dump_retries_publish_name_collision(self, run):
        def fake_run(command, **_kwargs):
            destination = Path(command[-1])
            destination.mkdir(parents=True)
            (destination / "db.sqlite").write_bytes(b"sqlite")

        original_rename = os.rename
        rename_calls = []

        def fake_rename(source, destination):
            rename_calls.append(Path(destination).name)
            if len(rename_calls) == 1:
                raise FileExistsError
            original_rename(source, destination)

        run.side_effect = fake_run
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"

            with mock.patch("secretary.data.os.rename", side_effect=fake_rename):
                dump = raw_kanboard_dump(data_dir)

            self.assertTrue((dump.dump_dir / "manifest.json").is_file())
            self.assertTrue((dump.dump_dir / "data" / "db.sqlite").is_file())

        self.assertEqual(len(rename_calls), 2)
        self.assertTrue(rename_calls[-1].endswith("-1"))

    def test_raw_kanboard_dump_wraps_staging_prepare_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"

            with mock.patch(
                "secretary.data.tempfile.mkdtemp", side_effect=PermissionError("denied")
            ):
                with self.assertRaisesRegex(RuntimeError, "could not create raw dump"):
                    raw_kanboard_dump(data_dir)

            self.assertEqual(list((data_dir / "board").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
