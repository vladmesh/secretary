from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import secretary.data as data_module
import secretary.memory_journal as memory_journal
from secretary.data import (
    export_board,
    export_all,
    export_artifacts,
    export_memory,
    export_runs,
    export_transcripts,
    import_memory_journal,
    init_layout,
    manifest_for,
    normalize_board_card,
    raw_kanboard_dump,
)
from secretary.memory_write import (
    MEMORY_PROPOSAL_ACTIVE_MARKER,
    MemoryExportPublishError,
    MemoryLockError,
    MemoryPermissionError,
    MemoryProtocolError,
    MemoryValidationError,
    commit_memory_proposal,
    gc_memory_proposals,
    propose_memory_fact,
    supersede_memory_fact,
)


class DataLayoutTests(unittest.TestCase):
    def test_init_layout_creates_target_dirs_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"

            layout = init_layout(data_dir)

            for name in ("board", "memory", "runs", "transcripts", "artifacts", "backups"):
                self.assertTrue((data_dir / name).is_dir(), name)
            self.assertTrue((data_dir / "memory" / "facts" / ".git").is_dir())
            self.assertEqual(layout.manifest_path, data_dir / "data-manifest.json")
            self.assertIn('"data_dir"', layout.manifest_path.read_text(encoding="utf-8"))
            remotes = git(data_dir / "memory" / "facts", "remote")
            self.assertEqual(remotes, "")

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

    def test_export_board_records_matching_active_raw_count_when_dump_exists(self):
        def fake_run(command, **_kwargs):
            if command[-1] == "list":
                stdout = json.dumps(
                    [
                        {"id": 1, "reference": "secretary-1", "title": "One"},
                        {"id": 2, "reference": "secretary-2", "title": "Two"},
                    ]
                )
            else:
                stdout = json.dumps(
                    {
                        "id": 1,
                        "reference": command[-1],
                        "title": command[-1],
                        "metadata": {},
                        "comments": [],
                    }
                )
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

    def test_export_board_skips_raw_count_when_board_project_missing(self):
        def fake_run(command, **_kwargs):
            if command[-1] == "list":
                stdout = json.dumps([{"id": 1, "reference": "secretary-1", "title": "One"}])
            else:
                stdout = json.dumps(
                    {
                        "id": 1,
                        "reference": command[-1],
                        "title": command[-1],
                        "metadata": {},
                        "comments": [],
                    }
                )
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
                conn.execute("insert into projects (id, name) values (1, 'Other')")
                conn.execute(
                    "insert into tasks (project_id, is_active) values (1, 1), (1, 1), (1, 1)"
                )
            with mock.patch("secretary.data.subprocess.run", side_effect=fake_run):
                export_board(data_dir, command=["pipeline"])
            summary = json.loads((data_dir / "board" / "export.json").read_text(encoding="utf-8"))

        # Дамп без доски Pipeline: чужие 3 active tasks не должны ни попасть в счёт,
        # ни уронить экспорт mismatch'ем — сверка пропускается явно.
        self.assertIsNone(summary["raw_active_task_count"])
        self.assertEqual(summary["card_count"], 1)

    def test_export_board_fails_when_raw_count_disagrees(self):
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
                conn.execute("insert into projects (id, name) values (1, 'Pipeline')")
                conn.execute("insert into tasks (project_id, is_active) values (1, 1), (1, 1)")
            with mock.patch("secretary.data.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "board export count mismatch"):
                    export_board(data_dir, command=["pipeline"])

    def test_export_board_preserves_previous_snapshot_on_publish_error(self):
        cards_by_run = [
            [{"id": 1, "reference": "secretary-1", "title": "One"}],
            [
                {"id": 1, "reference": "secretary-1", "title": "One"},
                {"id": 2, "reference": "secretary-2", "title": "Two"},
            ],
        ]
        run_index = 0

        def fake_run(command, **_kwargs):
            nonlocal run_index
            if command[-1] == "list":
                stdout = json.dumps(cards_by_run[run_index])
                run_index += 1
            else:
                stdout = json.dumps(
                    {
                        "id": 1,
                        "reference": command[-1],
                        "title": command[-1],
                        "metadata": {},
                        "comments": [],
                    }
                )
            return subprocess_completed(stdout)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            board_dir = data_dir / "board"
            raw_marker = board_dir / "kanboard-raw-20260710T000000Z" / "data" / "db.sqlite"
            raw_marker.parent.mkdir(parents=True)
            raw_marker.write_bytes(b"not sqlite")

            with mock.patch("secretary.data.subprocess.run", side_effect=fake_run):
                export_board(data_dir, command=["pipeline"])
                old_cards = (board_dir / "cards.json").read_text(encoding="utf-8")
                old_ndjson = (board_dir / "cards.ndjson").read_text(encoding="utf-8")
                old_summary = (board_dir / "export.json").read_text(encoding="utf-8")

                original_replace = os.replace
                failed = False

                def fail_on_ndjson_publish(source, destination):
                    nonlocal failed
                    if not failed and Path(destination) == board_dir / "cards.ndjson":
                        failed = True
                        raise OSError("full")
                    return original_replace(source, destination)

                with mock.patch("secretary.data.os.replace", side_effect=fail_on_ndjson_publish):
                    with self.assertRaisesRegex(RuntimeError, "could not publish board export"):
                        export_board(data_dir, command=["pipeline"])

            current_cards = (board_dir / "cards.json").read_text(encoding="utf-8")
            current_ndjson = (board_dir / "cards.ndjson").read_text(encoding="utf-8")
            current_summary = (board_dir / "export.json").read_text(encoding="utf-8")
            raw_dump_preserved = raw_marker.is_file()

        self.assertEqual(current_cards, old_cards)
        self.assertEqual(current_ndjson, old_ndjson)
        self.assertEqual(current_summary, old_summary)
        self.assertTrue(raw_dump_preserved)

    def test_export_memory_writes_readonly_ndjson_from_fallback_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            (source / "memory" / "secretary").mkdir(parents=True)
            (source / "memory" / "secretary" / "one.md").write_text(
                "---\n"
                "tags: [secretary, memory]\n"
                "source: test\n"
                "created: 2026-07-11\n"
                "---\n"
                "fact one\n",
                encoding="utf-8",
            )
            (source / "memory" / "global").mkdir()
            (source / "memory" / "global" / "two.md").write_text("fact two\n", encoding="utf-8")
            init_git_repo(source)
            data_dir = root / "secretary-data"

            first = export_memory(data_dir, source_dir=source)
            first_payload = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")
            second = export_memory(data_dir, source_dir=source)
            facts_dir_exists = (data_dir / "memory" / "facts").exists()
            manifest = json.loads((data_dir / "memory" / "manifest.json").read_text(encoding="utf-8"))
            source_head = git(source, "rev-parse", "HEAD")

        self.assertEqual(first.count, 2)
        self.assertEqual(second.count, 2)
        self.assertIn("secretary/one.md", first_payload)
        self.assertIn('"metadata": {"created": "2026-07-11"', first_payload)
        self.assertFalse(facts_dir_exists)
        self.assertEqual(manifest["source"]["head"], source_head)
        self.assertTrue(manifest["source"]["readonly_fallback"])

    def test_export_memory_ndjson_comes_from_readonly_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            fact = source / "memory" / "secretary" / "one.md"
            fact.parent.mkdir(parents=True)
            fact.write_text("fact one\n", encoding="utf-8")
            original_copy_tree = memory_journal._copy_tree

            def copy_then_mutate(source_memory, snapshot):
                original_copy_tree(source_memory, snapshot)
                fact.write_text("changed after snapshot\n", encoding="utf-8")

            with mock.patch("secretary.memory_journal._copy_tree", side_effect=copy_then_mutate):
                export_memory(root / "secretary-data", source_dir=source)
            exported = (root / "secretary-data" / "memory" / "export.ndjson").read_text(
                encoding="utf-8"
            )

        self.assertIn("fact one", exported)
        self.assertNotIn("changed after snapshot", exported)

    def test_export_memory_skips_symlinked_facts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            secret = root / "secret.env"
            secret.write_text("TOKEN=do-not-export\n", encoding="utf-8")
            facts = source / "memory" / "secretary"
            facts.mkdir(parents=True)
            (facts / "one.md").write_text("fact one\n", encoding="utf-8")
            (facts / "secret.md").symlink_to(secret)

            result = export_memory(root / "secretary-data", source_dir=source)
            exported = (root / "secretary-data" / "memory" / "export.ndjson").read_text(
                encoding="utf-8"
            )
            mirrored_secret = root / "secretary-data" / "memory" / "facts" / "secretary" / "secret.md"

        self.assertEqual(result.count, 1)
        self.assertIn("fact one", exported)
        self.assertNotIn("TOKEN=do-not-export", exported)
        self.assertFalse(mirrored_secret.exists())

    def test_export_memory_wraps_decode_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            fact = source / "memory" / "secretary" / "bad.md"
            fact.parent.mkdir(parents=True)
            fact.write_bytes(b"\xff\xfe")

            with self.assertRaisesRegex(RuntimeError, "could not decode memory fact"):
                export_memory(root / "secretary-data", source_dir=source)

    def test_export_memory_preserves_previous_snapshot_on_decode_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            facts = source / "memory" / "secretary"
            facts.mkdir(parents=True)
            (facts / "good.md").write_text("good fact\n", encoding="utf-8")
            data_dir = root / "secretary-data"
            export_memory(data_dir, source_dir=source)
            old_export = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")

            (facts / "bad.md").write_bytes(b"\xff\xfe")

            with self.assertRaisesRegex(RuntimeError, "could not decode memory fact"):
                export_memory(data_dir, source_dir=source)

            current_export = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")
            facts_dir_exists = (data_dir / "memory" / "facts").exists()

        self.assertEqual(current_export, old_export)
        self.assertFalse(facts_dir_exists)

    def test_memory_import_repeated_sync_adds_new_source_fact_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            fact_dir = source / "memory" / "secretary"
            fact_dir.mkdir(parents=True)
            (fact_dir / "one.md").write_text("fact one\n", encoding="utf-8")
            init_git_repo(source)
            data_dir = root / "secretary-data"

            first = import_memory_journal(data_dir, source_dir=source)
            second = import_memory_journal(data_dir, source_dir=source)
            (fact_dir / "two.md").write_text("fact two\n", encoding="utf-8")
            git(source, "add", "-A", ".")
            git(source, "commit", "-m", "Add second fact")
            third = import_memory_journal(data_dir, source_dir=source)
            fourth = import_memory_journal(data_dir, source_dir=source)
            facts_dir = data_dir / "memory" / "facts"
            tracked = git(facts_dir, "ls-files").splitlines()
            log_count = git(facts_dir, "rev-list", "--count", "HEAD")
            manifest = json.loads((data_dir / "memory" / "manifest.json").read_text(encoding="utf-8"))

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertTrue(third.changed)
        self.assertFalse(fourth.changed)
        self.assertEqual(tracked, ["secretary/one.md", "secretary/two.md"])
        self.assertEqual(log_count, "2")
        self.assertEqual(manifest["source"]["head"], third.source_head)
        self.assertEqual(len(manifest["imports"]), 2)

    def test_memory_import_refuses_after_non_import_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            (source / "memory" / "secretary").mkdir(parents=True)
            (source / "memory" / "secretary" / "one.md").write_text("fact one\n", encoding="utf-8")
            init_git_repo(source)
            data_dir = root / "secretary-data"
            import_memory_journal(data_dir, source_dir=source)
            facts_dir = data_dir / "memory" / "facts"
            (facts_dir / "secretary" / "manual.md").write_text("manual\n", encoding="utf-8")
            git(facts_dir, "add", "-A", ".")
            git(facts_dir, "commit", "-m", "Manual protocol write")

            (source / "memory" / "secretary" / "two.md").write_text("fact two\n", encoding="utf-8")
            git(source, "add", "-A", ".")
            git(source, "commit", "-m", "Add second fact")
            with self.assertRaisesRegex(RuntimeError, "refused after non-import"):
                import_memory_journal(data_dir, source_dir=source)

    def test_memory_import_lock_conflict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            (source / "memory" / "secretary").mkdir(parents=True)
            (source / "memory" / "secretary" / "one.md").write_text("fact one\n", encoding="utf-8")
            data_dir = root / "secretary-data"
            (data_dir / "memory").mkdir(parents=True)
            (data_dir / "memory" / memory_journal.MEMORY_LOCK_NAME).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": memory_journal.socket.gethostname(),
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "journal is locked"):
                import_memory_journal(data_dir, source_dir=source)

    def test_memory_import_removes_proven_stale_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            (source / "memory" / "secretary").mkdir(parents=True)
            (source / "memory" / "secretary" / "one.md").write_text("fact one\n", encoding="utf-8")
            data_dir = root / "secretary-data"
            (data_dir / "memory").mkdir(parents=True)
            (data_dir / "memory" / memory_journal.MEMORY_LOCK_NAME).write_text(
                json.dumps(
                    {
                        "pid": 99999999,
                        "host": memory_journal.socket.gethostname(),
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            result = import_memory_journal(data_dir, source_dir=source)

        self.assertEqual(result.count, 1)

    def test_memory_import_restores_journal_on_copy_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            facts = source / "memory" / "secretary"
            facts.mkdir(parents=True)
            (facts / "one.md").write_text("fact one\n", encoding="utf-8")
            data_dir = root / "secretary-data"
            import_memory_journal(data_dir, source_dir=source)
            facts_dir = data_dir / "memory" / "facts"
            old_tracked = git(facts_dir, "ls-files")
            old_head = git(facts_dir, "rev-parse", "HEAD")
            (facts / "two.md").write_text("fact two\n", encoding="utf-8")

            original_copy_tree = data_module._copy_tree
            failed = False

            def fail_when_publishing(source_path, destination):
                nonlocal failed
                if destination == facts_dir:
                    if failed:
                        return original_copy_tree(source_path, destination)
                    failed = True
                    raise RuntimeError("copy failed")
                return original_copy_tree(source_path, destination)

            with mock.patch("secretary.memory_journal._copy_tree", side_effect=fail_when_publishing):
                with self.assertRaisesRegex(RuntimeError, "copy failed"):
                    import_memory_journal(data_dir, source_dir=source)

            self.assertEqual(git(facts_dir, "ls-files"), old_tracked)
            self.assertEqual(git(facts_dir, "rev-parse", "HEAD"), old_head)
            self.assertEqual(git(facts_dir, "status", "--porcelain"), "")

    def test_memory_protocol_commit_writes_one_journal_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            fact = root / "fact.md"
            fact.write_text("new durable fact\n", encoding="utf-8")

            proposal = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:secretary",
                slug="new-fact",
                fact_file=fact,
                source="curator:claude/session",
                tags=["secretary", "memory"],
            )
            result = commit_memory_proposal(
                data_dir,
                actor="curator:claude/session",
                propose_id=proposal.propose_id,
            )
            facts_dir = data_dir / "memory" / "facts"
            log_count = git(facts_dir, "rev-list", "--count", "HEAD")
            message = git(facts_dir, "log", "-1", "--format=%B")
            status = git(facts_dir, "status", "--porcelain")
            exported = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")

        self.assertEqual(result.fact, "secretary/new-fact")
        self.assertEqual(log_count, "1")
        self.assertIn("Op: commit", message)
        self.assertIn("Principal: curator:claude/session", message)
        self.assertIn("Source: curator:claude/session", message)
        self.assertIn("Changed-Facts: secretary/new-fact", message)
        self.assertEqual(status, "")
        self.assertIn("new durable fact", exported)

    def test_memory_protocol_export_failure_after_commit_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            fact = root / "fact.md"
            fact.write_text("retryable fact\n", encoding="utf-8")
            proposal = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:secretary",
                slug="retryable",
                fact_file=fact,
                source="curator:claude/session",
            )

            with mock.patch(
                "secretary.memory_write._publish_memory_export",
                side_effect=RuntimeError("disk full"),
            ):
                with self.assertRaises(MemoryExportPublishError) as raised:
                    commit_memory_proposal(
                        data_dir,
                        actor="curator:claude/session",
                        propose_id=proposal.propose_id,
                    )

            failed_result = raised.exception.result
            facts_dir = data_dir / "memory" / "facts"
            after_failure_head = git(facts_dir, "rev-parse", "HEAD")
            log_count_after_failure = git(facts_dir, "rev-list", "--count", "HEAD")
            completed_marker = data_dir / "memory" / ".staging" / proposal.propose_id / "committed.json"
            completed_exists_after_failure = completed_marker.is_file()
            export_exists_after_failure = (data_dir / "memory" / "export.ndjson").exists()

            retry_result = commit_memory_proposal(
                data_dir,
                actor="curator:claude/session",
                propose_id=proposal.propose_id,
            )
            retry_head = git(facts_dir, "rev-parse", "HEAD")
            retry_log_count = git(facts_dir, "rev-list", "--count", "HEAD")
            exported = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")
            staging_exists_after_retry = completed_marker.parent.exists()

        self.assertEqual(failed_result.commit, after_failure_head)
        self.assertEqual(failed_result.fact, "secretary/retryable")
        self.assertEqual(log_count_after_failure, "1")
        self.assertTrue(completed_exists_after_failure)
        self.assertFalse(export_exists_after_failure)
        self.assertEqual(retry_result.commit, after_failure_head)
        self.assertEqual(retry_head, after_failure_head)
        self.assertEqual(retry_log_count, "1")
        self.assertIn("retryable fact", exported)
        self.assertFalse(staging_exists_after_retry)

    def test_memory_protocol_errors_share_base_class(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            fact = root / "fact.md"
            fact.write_text("fact\n", encoding="utf-8")
            memory_dir = data_dir / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / memory_journal.MEMORY_LOCK_NAME).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": memory_journal.socket.gethostname(),
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            errors: list[MemoryProtocolError] = []
            for actor, scope, source in (
                ("curator:claude/session", "bad", "curator:claude/session"),
                ("worker:codex/session", "project:secretary", "worker:codex/session"),
                ("curator:claude/session", "project:secretary", "curator:claude/session"),
            ):
                try:
                    propose_memory_fact(
                        data_dir,
                        actor=actor,
                        scope=scope,
                        slug="new-fact",
                        fact_file=fact,
                        source=source,
                    )
                except MemoryProtocolError as exc:
                    errors.append(exc)

        self.assertIsInstance(errors[0], MemoryValidationError)
        self.assertIsInstance(errors[1], MemoryPermissionError)
        self.assertIsInstance(errors[2], MemoryLockError)
        self.assertTrue(all(isinstance(error, MemoryProtocolError) for error in errors))

    def test_memory_protocol_gc_removes_only_stale_uncommitted_proposals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            fact = root / "fact.md"
            fact.write_text("fact\n", encoding="utf-8")
            stale = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:secretary",
                slug="stale",
                fact_file=fact,
                source="curator:claude/session",
            )
            fresh = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:secretary",
                slug="fresh",
                fact_file=fact,
                source="curator:claude/session",
            )
            active = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:secretary",
                slug="active",
                fact_file=fact,
                source="curator:claude/session",
            )
            for proposal in (stale, active):
                proposal_path = proposal.path / "proposal.json"
                payload = json.loads(proposal_path.read_text(encoding="utf-8"))
                payload["created_at"] = 1
                proposal_path.write_text(json.dumps(payload), encoding="utf-8")
            (active.path / MEMORY_PROPOSAL_ACTIVE_MARKER).write_text("{}", encoding="utf-8")

            result = gc_memory_proposals(
                data_dir,
                max_age_seconds=60,
                active_grace_seconds=3600,
            )
            fresh_exists = fresh.path.is_dir()
            active_exists = active.path.is_dir()
            stale_exists = stale.path.exists()

        self.assertEqual(result.removed, (stale.propose_id,))
        self.assertTrue(fresh_exists)
        self.assertTrue(active_exists)
        self.assertFalse(stale_exists)

    def test_memory_protocol_rejects_invalid_input_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fact = root / "fact.md"
            fact.write_text("fact\n", encoding="utf-8")

            with self.assertRaises(MemoryValidationError):
                propose_memory_fact(
                    root / "secretary-data",
                    actor="curator:claude/session",
                    scope="bad",
                    slug="new-fact",
                    fact_file=fact,
                    source="curator:claude/session",
                )
            with self.assertRaises(MemoryPermissionError):
                propose_memory_fact(
                    root / "secretary-data",
                    actor="worker:codex/session",
                    scope="project:secretary",
                    slug="new-fact",
                    fact_file=fact,
                    source="worker:codex/session",
                )

    def test_memory_protocol_supersede_unknown_fact_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            fact = root / "fact.md"
            fact.write_text("replacement\n", encoding="utf-8")

            with self.assertRaisesRegex(MemoryValidationError, "not found"):
                supersede_memory_fact(
                    data_dir,
                    actor="curator:claude/session",
                    scope="project:secretary",
                    slug="replacement",
                    fact_file=fact,
                    supersedes=["missing"],
                    source="curator:claude/session",
                )
            facts_dir = data_dir / "memory" / "facts"
            status = git(facts_dir, "status", "--porcelain")

        self.assertEqual(status, "")

    def test_memory_protocol_supersede_removes_old_fact_in_one_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            old_fact = root / "old.md"
            old_fact.write_text("old fact\n", encoding="utf-8")
            proposal = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:secretary",
                slug="old",
                fact_file=old_fact,
                source="curator:claude/session",
            )
            commit_memory_proposal(
                data_dir,
                actor="curator:claude/session",
                propose_id=proposal.propose_id,
            )
            new_fact = root / "new.md"
            new_fact.write_text("current fact\n", encoding="utf-8")

            result = supersede_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:secretary",
                slug="new",
                fact_file=new_fact,
                supersedes=["old"],
                source="curator:claude/session",
            )
            facts_dir = data_dir / "memory" / "facts"
            tracked = git(facts_dir, "ls-files").splitlines()
            log_count = git(facts_dir, "rev-list", "--count", "HEAD")
            message = git(facts_dir, "log", "-1", "--format=%B")

        self.assertEqual(result.changed_facts, ("secretary/new", "secretary/old"))
        self.assertEqual(tracked, ["secretary/new.md"])
        self.assertEqual(log_count, "2")
        self.assertIn("Op: supersede", message)
        self.assertIn("Supersedes: secretary/old", message)

    def test_memory_protocol_live_lock_rejects_concurrent_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            fact = root / "fact.md"
            fact.write_text("fact\n", encoding="utf-8")
            memory_dir = data_dir / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / memory_journal.MEMORY_LOCK_NAME).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": memory_journal.socket.gethostname(),
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(MemoryLockError):
                propose_memory_fact(
                    data_dir,
                    actor="curator:claude/session",
                    scope="project:secretary",
                    slug="new-fact",
                    fact_file=fact,
                    source="curator:claude/session",
                )

    def test_memory_protocol_recovers_dirty_worktree_before_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            first_fact = root / "first.md"
            first_fact.write_text("first fact\n", encoding="utf-8")
            first = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:secretary",
                slug="first",
                fact_file=first_fact,
                source="curator:claude/session",
            )
            commit_memory_proposal(
                data_dir,
                actor="curator:claude/session",
                propose_id=first.propose_id,
            )
            facts_dir = data_dir / "memory" / "facts"
            (facts_dir / "secretary" / "first.md").write_text("dirty edit\n", encoding="utf-8")
            (facts_dir / "secretary" / "residue.md").write_text("residue\n", encoding="utf-8")
            second_fact = root / "second.md"
            second_fact.write_text("second fact\n", encoding="utf-8")

            second = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:secretary",
                slug="second",
                fact_file=second_fact,
                source="curator:claude/session",
            )
            commit_memory_proposal(
                data_dir,
                actor="curator:claude/session",
                propose_id=second.propose_id,
            )
            first_text = (facts_dir / "secretary" / "first.md").read_text(encoding="utf-8")
            tracked = git(facts_dir, "ls-files").splitlines()
            status = git(facts_dir, "status", "--porcelain")

        self.assertIn("first fact", first_text)
        self.assertEqual(tracked, ["secretary/first.md", "secretary/second.md"])
        self.assertEqual(status, "")

    def test_export_memory_after_protocol_commit_is_readonly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            source = root / "panelmem-kb"
            (source / "memory" / "secretary").mkdir(parents=True)
            (source / "memory" / "secretary" / "external.md").write_text(
                "external fact\n",
                encoding="utf-8",
            )
            protocol_fact = root / "protocol.md"
            protocol_fact.write_text("protocol fact\n", encoding="utf-8")
            proposal = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:secretary",
                slug="protocol",
                fact_file=protocol_fact,
                source="curator:claude/session",
            )
            commit_memory_proposal(
                data_dir,
                actor="curator:claude/session",
                propose_id=proposal.propose_id,
            )
            facts_dir = data_dir / "memory" / "facts"
            before_head = git(facts_dir, "rev-parse", "HEAD")
            before_count = git(facts_dir, "rev-list", "--count", "HEAD")

            result = export_memory(data_dir, source_dir=source)
            after_head = git(facts_dir, "rev-parse", "HEAD")
            after_count = git(facts_dir, "rev-list", "--count", "HEAD")
            exported = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")
            status = git(facts_dir, "status", "--porcelain")

        self.assertEqual(result.count, 1)
        self.assertEqual(after_head, before_head)
        self.assertEqual(after_count, before_count)
        self.assertIn("protocol fact", exported)
        self.assertNotIn("external fact", exported)
        self.assertEqual(status, "")

    def test_export_memory_respects_live_journal_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            source = root / "panelmem-kb"
            (source / "memory" / "secretary").mkdir(parents=True)
            (source / "memory" / "secretary" / "external.md").write_text(
                "external fact\n",
                encoding="utf-8",
            )
            memory_dir = data_dir / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / memory_journal.MEMORY_LOCK_NAME).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": memory_journal.socket.gethostname(),
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(MemoryLockError):
                export_memory(data_dir, source_dir=source)

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

    def test_export_runs_fails_on_invalid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = root / "state"
            (state / "pipeline").mkdir(parents=True)
            (state / "pipeline" / "runs.jsonl").write_text('{"event":', encoding="utf-8")
            (state / "pipeline" / "cards.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "invalid JSONL"):
                export_runs(root / "secretary-data", state_dir=state)

    def test_export_runs_fails_on_invalid_card_mapping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = root / "state"
            (state / "pipeline").mkdir(parents=True)
            (state / "pipeline" / "runs.jsonl").write_text("{}\n", encoding="utf-8")
            (state / "pipeline" / "cards.json").write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                export_runs(root / "secretary-data", state_dir=state)

    def test_export_runs_preserves_previous_snapshot_on_publish_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = root / "state"
            runs_dir = root / "secretary-data" / "runs"
            (state / "pipeline").mkdir(parents=True)
            (state / "pipeline" / "runs.jsonl").write_text('{"event":"one"}\n', encoding="utf-8")
            (state / "pipeline" / "cards.json").write_text("{}", encoding="utf-8")

            export_runs(root / "secretary-data", state_dir=state)
            old_runs = (runs_dir / "runs.ndjson").read_text(encoding="utf-8")
            old_watermarks = (runs_dir / "watermarks.json").read_text(encoding="utf-8")
            old_cards = (runs_dir / "cards.json").read_text(encoding="utf-8")
            old_summary = (runs_dir / "export.json").read_text(encoding="utf-8")

            (state / "pipeline" / "runs.jsonl").write_text(
                '{"event":"one"}\n{"event":"two"}\n',
                encoding="utf-8",
            )
            original_replace = os.replace
            failed = False

            def fail_on_watermarks_publish(source, destination):
                nonlocal failed
                if not failed and Path(destination) == runs_dir / "watermarks.json":
                    failed = True
                    raise OSError("full")
                return original_replace(source, destination)

            with mock.patch("secretary.data.os.replace", side_effect=fail_on_watermarks_publish):
                with self.assertRaisesRegex(RuntimeError, "could not publish runs export"):
                    export_runs(root / "secretary-data", state_dir=state)

            current_runs = (runs_dir / "runs.ndjson").read_text(encoding="utf-8")
            current_watermarks = (runs_dir / "watermarks.json").read_text(encoding="utf-8")
            current_cards = (runs_dir / "cards.json").read_text(encoding="utf-8")
            current_summary = (runs_dir / "export.json").read_text(encoding="utf-8")

        self.assertEqual(current_runs, old_runs)
        self.assertEqual(current_watermarks, old_watermarks)
        self.assertEqual(current_cards, old_cards)
        self.assertEqual(current_summary, old_summary)

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

    def test_export_transcripts_skips_symlinks_when_copying(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            transcripts = root / "claude"
            transcripts.mkdir()
            secret = root / "secret.txt"
            secret.write_text("TOKEN=do-not-copy\n", encoding="utf-8")
            (transcripts / "session.jsonl").write_text("{}\n", encoding="utf-8")
            (transcripts / "linked.jsonl").symlink_to(secret)

            result = export_transcripts(root / "secretary-data", roots=[transcripts], copy=True)
            copy_root = root / "secretary-data" / "transcripts" / "copies"
            copied_files = [path.name for path in copy_root.rglob("*.jsonl")]
            copied_payload = "\n".join(
                path.read_text(encoding="utf-8") for path in copy_root.rglob("*.jsonl")
            )

        self.assertEqual(result.count, 1)
        self.assertEqual(copied_files, ["session.jsonl"])
        self.assertNotIn("TOKEN=do-not-copy", copied_payload)

    def test_export_transcripts_preserves_previous_snapshot_on_copy_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            transcripts = root / "claude"
            transcripts_dir = root / "secretary-data" / "transcripts"
            (transcripts / "project").mkdir(parents=True)
            (transcripts / "project" / "session.jsonl").write_text("{}\n", encoding="utf-8")

            export_transcripts(root / "secretary-data", roots=[transcripts], copy=True)
            old_inventory = (transcripts_dir / "inventory.json").read_text(encoding="utf-8")
            old_ndjson = (transcripts_dir / "inventory.ndjson").read_text(encoding="utf-8")
            old_copies = sorted(
                path.relative_to(transcripts_dir / "copies").as_posix()
                for path in (transcripts_dir / "copies").rglob("*.jsonl")
            )
            (transcripts / "project" / "second.jsonl").write_text("{}\n", encoding="utf-8")

            with mock.patch("secretary.data.shutil.copy2", side_effect=OSError("full")):
                with self.assertRaisesRegex(RuntimeError, "could not copy transcripts"):
                    export_transcripts(root / "secretary-data", roots=[transcripts], copy=True)

            current_inventory = (transcripts_dir / "inventory.json").read_text(encoding="utf-8")
            current_ndjson = (transcripts_dir / "inventory.ndjson").read_text(encoding="utf-8")
            current_copies = sorted(
                path.relative_to(transcripts_dir / "copies").as_posix()
                for path in (transcripts_dir / "copies").rglob("*.jsonl")
            )

        self.assertEqual(current_inventory, old_inventory)
        self.assertEqual(current_ndjson, old_ndjson)
        self.assertEqual(current_copies, old_copies)

    def test_export_artifacts_inventories_existing_files_and_task_docs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            artifacts_dir = data_dir / "artifacts"
            (artifacts_dir / "report.txt").parent.mkdir(parents=True)
            (artifacts_dir / "report.txt").write_text("report\n", encoding="utf-8")
            (artifacts_dir / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")
            workspaces = root / "workspaces"
            workspace = workspaces / "secretary" / "354-backup-create-verify"
            workspace.mkdir(parents=True)
            (workspace / "TASK.md").write_text("task\n", encoding="utf-8")
            (workspace / "REVIEW.md").write_text("review\n", encoding="utf-8")
            (workspace / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")

            result = export_artifacts(data_dir, workspaces_root=workspaces)
            second = export_artifacts(data_dir, workspaces_root=workspaces)
            inventory = json.loads((artifacts_dir / "inventory.json").read_text(encoding="utf-8"))
            copied_docs = sorted(
                path.relative_to(artifacts_dir / "task-docs").as_posix()
                for path in (artifacts_dir / "task-docs").rglob("*.md")
            )

        self.assertEqual(result.count, 3)
        self.assertEqual(second.count, 3)
        paths = {entry["relative_path"] for entry in inventory["artifacts"]}
        self.assertIn("report.txt", paths)
        self.assertIn("secretary/354-backup-create-verify/TASK.md", paths)
        self.assertIn("secretary/354-backup-create-verify/REVIEW.md", paths)
        self.assertNotIn(".env", paths)
        self.assertEqual(
            copied_docs,
            [
                "secretary/354-backup-create-verify/REVIEW.md",
                "secretary/354-backup-create-verify/TASK.md",
            ],
        )

    def test_export_all_passes_copy_transcripts_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"

            with (
                mock.patch(
                    "secretary.data.export_board",
                    return_value=data_module.DataExport(data_dir / "board.json", 1, "board"),
                ),
                mock.patch(
                    "secretary.data.export_memory",
                    return_value=data_module.DataExport(data_dir / "memory.ndjson", 1, "memory"),
                ),
                mock.patch(
                    "secretary.data.export_runs",
                    return_value=data_module.DataExport(data_dir / "runs.ndjson", 1, "runs"),
                ),
                mock.patch(
                    "secretary.data.export_transcripts",
                    return_value=data_module.DataExport(data_dir / "inventory.json", 1, "transcripts"),
                ) as transcripts,
                mock.patch(
                    "secretary.data.export_artifacts",
                    return_value=data_module.DataExport(data_dir / "artifacts.json", 1, "artifacts"),
                ) as artifacts,
            ):
                export_all(data_dir, copy_transcripts=True)

        transcripts.assert_called_once_with(data_dir, copy=True)
        artifacts.assert_called_once_with(data_dir)


def subprocess_completed(stdout: str):
    return mock.Mock(stdout=stdout, stderr="", returncode=0)


def init_git_repo(path: Path) -> None:
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "add", "-A", ".")
    git(path, "commit", "-m", "Initial facts")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
