from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.data import (
    export_board,
    export_memory,
    export_runs,
    export_transcripts,
    init_layout,
    manifest_for,
    normalize_board_card,
    raw_kanboard_dump,
)


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


class ExportTests(unittest.TestCase):
    def test_normalize_board_card_keeps_required_surface(self):
        card = {
            "id": "7",
            "reference": "secretary-353",
            "title": "Export data",
            "column": "In progress",
            "swimlane": "secretary",
            "position": "2",
            "date_moved": "1783635890",
            "task_type": "code",
            "project": "secretary",
        }
        shown = {
            "id": 7,
            "reference": "secretary-353",
            "title": "Export data",
            "description": "body",
            "column": "In progress",
            "metadata": {"project": "secretary", "task_type": "code"},
            "comments": [{"ts": "1", "text": "[worker]\nok"}],
        }

        normalized = normalize_board_card(card, shown)

        self.assertEqual(normalized["reference"], "secretary-353")
        self.assertEqual(normalized["swimlane"], "secretary")
        self.assertEqual(normalized["column"], "In progress")
        self.assertEqual(normalized["metadata"], {"project": "secretary", "task_type": "code"})
        self.assertEqual(normalized["comments"], [{"ts": "1", "text": "[worker]\nok"}])

    def test_export_board_writes_normalized_cards_and_is_idempotent(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if command[-1] == "list":
                stdout = json.dumps(
                    [
                        {
                            "id": 1,
                            "reference": "secretary-353",
                            "title": "Export",
                            "column": "Ready",
                            "swimlane": "secretary",
                            "position": 1,
                        }
                    ]
                )
            else:
                stdout = json.dumps(
                    {
                        "id": 1,
                        "reference": "secretary-353",
                        "title": "Export",
                        "description": "spec",
                        "column": "Ready",
                        "metadata": {"project": "secretary"},
                        "comments": [{"ts": "10", "text": "[po]\nbody"}],
                    }
                )
            return subprocess_completed(stdout)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            with mock.patch("secretary.data.subprocess.run", side_effect=fake_run):
                first = export_board(data_dir, command=["pipeline"])
                first_payload = (data_dir / "board" / "cards.json").read_text(encoding="utf-8")
                second = export_board(data_dir, command=["pipeline"])
                second_payload = (data_dir / "board" / "cards.json").read_text(encoding="utf-8")

        self.assertEqual(first.count, 1)
        self.assertEqual(second.count, 1)
        self.assertEqual(first_payload, second_payload)
        self.assertEqual(len(calls), 4)

    def test_export_board_records_active_raw_count_when_dump_exists(self):
        def fake_run(command, **_kwargs):
            if command[-1] == "list":
                stdout = json.dumps([])
            else:
                stdout = json.dumps({})
            return subprocess_completed(stdout)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            database = data_dir / "board" / "kanboard-raw-20260710T000000Z" / "data" / "db.sqlite"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as conn:
                conn.execute("create table projects (id integer primary key, name text)")
                conn.execute(
                    "create table tasks (id integer primary key, project_id integer, is_active integer)"
                )
                conn.execute("insert into projects (id, name) values (1, 'Other'), (2, 'Pipeline')")
                conn.execute(
                    "insert into tasks (project_id, is_active) values (2, 1), (2, 0), (2, 1), (1, 1)"
                )
            with mock.patch("secretary.data.subprocess.run", side_effect=fake_run):
                export_board(data_dir, command=["pipeline"])
            summary = json.loads((data_dir / "board" / "export.json").read_text(encoding="utf-8"))

        self.assertEqual(summary["raw_active_task_count"], 2)

    def test_export_memory_mirrors_facts_and_ndjson(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            (source / "memory" / "secretary").mkdir(parents=True)
            (source / "memory" / "secretary" / "one.md").write_text("fact one\n", encoding="utf-8")
            (source / "memory" / "global").mkdir()
            (source / "memory" / "global" / "two.md").write_text("fact two\n", encoding="utf-8")
            data_dir = root / "secretary-data"

            first = export_memory(data_dir, source_dir=source)
            first_payload = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")
            second = export_memory(data_dir, source_dir=source)
            mirrored = (data_dir / "memory" / "facts" / "global" / "two.md").is_file()

        self.assertEqual(first.count, 2)
        self.assertEqual(second.count, 2)
        self.assertIn("secretary/one.md", first_payload)
        self.assertTrue(mirrored)

    def test_export_runs_writes_records_watermarks_and_card_mapping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = root / "state"
            (state / "pipeline").mkdir(parents=True)
            (state / "pipeline" / "runs.jsonl").write_text(
                '{"event":"claim","reference":"secretary-353"}\n',
                encoding="utf-8",
            )
            (state / "pipeline" / "cards.json").write_text(
                '{"secretary-353":{"worker":"353-system-exports"}}\n',
                encoding="utf-8",
            )

            result = export_runs(root / "secretary-data", state_dir=state)
            runs = (root / "secretary-data" / "runs" / "runs.ndjson").read_text(encoding="utf-8")
            watermarks = json.loads(
                (root / "secretary-data" / "runs" / "watermarks.json").read_text(encoding="utf-8")
            )
            cards = json.loads((root / "secretary-data" / "runs" / "cards.json").read_text(encoding="utf-8"))

        self.assertEqual(result.count, 1)
        self.assertIn('"event": "claim"', runs)
        self.assertEqual(watermarks["files"][0]["path"], "pipeline/cards.json")
        self.assertIn("secretary-353", cards["cards"])

    def test_export_transcripts_inventory_and_optional_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            transcripts = root / "claude"
            (transcripts / "project").mkdir(parents=True)
            (transcripts / "project" / "session.jsonl").write_text("{}\n", encoding="utf-8")

            inventory = export_transcripts(root / "secretary-data", roots=[transcripts])
            no_copy = (root / "secretary-data" / "transcripts" / "copies").exists()
            copied = export_transcripts(root / "secretary-data", roots=[transcripts], copy=True)
            copied_dir = (root / "secretary-data" / "transcripts" / "copies").is_dir()

        self.assertEqual(inventory.count, 1)
        self.assertFalse(no_copy)
        self.assertEqual(copied.count, 1)
        self.assertTrue(copied_dir)


def subprocess_completed(stdout: str):
    return mock.Mock(stdout=stdout, stderr="", returncode=0)


if __name__ == "__main__":
    unittest.main()
