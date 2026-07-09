from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
