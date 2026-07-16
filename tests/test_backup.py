from __future__ import annotations

import fcntl
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.backup import check_backup_health, create_backup, create_backups, verify_backup
from secretary.data import DataExport


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict(os.environ, {"BOARD_ROLE": ""})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.workspace_patch = mock.patch(
            "secretary.backup._claimed_workspace_from_cwd", return_value=None
        )
        self.workspace_patch.start()
        self.addCleanup(self.workspace_patch.stop)

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
                self.assertFalse(copy_transcripts)
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
                    "artifacts": DataExport(
                        data_dir_arg / "artifacts" / "inventory.json",
                        1,
                        "artifacts",
                    ),
                }

            def fake_encrypt(source, destination, _recipient):
                shutil.copy2(source, destination)

            with (
                mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
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
            self.assertIn("secretary-backup/secretary-data/board/kanboard-raw-20260710T000000Z/data/db.sqlite", names)
            self.assertIn("secretary-backup/secretary-data/runs/runs.ndjson", names)
            self.assertIn("secretary-backup/secretary-data/runs/cards.json", names)
            self.assertIn("secretary-backup/secretary-data/artifacts/inventory.json", names)
            self.assertIn("secretary-backup/debug/orca-state/inventory.json", names)
            self.assertNotIn("secretary-backup/secretary-data/memory/index.sqlite", names)
            self.assertNotIn("secretary-backup/secretary-data/backups/old.tar.age", names)
            self.assertNotIn("secretary-backup/instance/.env", names)

    def test_create_rejects_claimed_worker_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)

            with mock.patch.dict(os.environ, {"BOARD_ROLE": "worker"}):
                with self.assertRaisesRegex(RuntimeError, "claimed worker"):
                    create_backup(instance, recipient="age1example")

    def test_create_rejects_relative_instance_data_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            _write_instance(instance, Path("secretary-data"))

            with self.assertRaisesRegex(RuntimeError, "data_dir: value must match pattern"):
                create_backup(instance, recipient="age1example")

        self.assertFalse((root / "secretary-data").exists())

    def test_create_ignores_invalid_project_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)
            projects = instance / "projects"
            projects.mkdir()
            (projects / "broken.yaml").write_text("defualt_branch: main\n", encoding="utf-8")

            with (
                mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("secretary.backup._pipeline_action", return_value=None),
                mock.patch(
                    "secretary.backup.raw_kanboard_dump",
                    side_effect=RuntimeError("snapshot reached"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "snapshot reached"):
                    create_backup(instance, recipient="age1example")

    def test_create_rejects_claimed_workspace_when_board_role_is_removed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "orca" / "workspaces" / "secretary" / "380-backup"
            workspace.mkdir(parents=True)
            (workspace / "TASK.md").write_text("task\n", encoding="utf-8")
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)

            with (
                mock.patch.dict(os.environ, {"BOARD_ROLE": ""}),
                mock.patch(
                    "secretary.backup._claimed_workspace_from_cwd", return_value=workspace
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "claimed worker"):
                    create_backup(instance, recipient="age1example")

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
                mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
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

    def test_create_uses_pipeline_freeze_pause_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)
            calls: list[list[str]] = []

            def fake_run(args, **_kwargs):
                calls.append(list(args))
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "paused": True,
                            "mode": "freeze",
                            "internal_mode": "hard",
                            "reason": "secretary backup create",
                            "actor": "secretary-backup",
                        }
                    ),
                    stderr="",
                )

            with mock.patch("secretary.backup.subprocess.run", side_effect=fake_run):
                from secretary.backup import _pipeline_action, _pipeline_status

                _pipeline_status(pipeline_worktree=root, command=["pipeline"])
                _pipeline_action("pause", pipeline_worktree=root, command=["pipeline"])
                _pipeline_action("resume", pipeline_worktree=root, command=["pipeline"])

            self.assertEqual(
                calls[0],
                ["pipeline", "--role", "steward", "pause-status"],
            )
            self.assertEqual(
                calls[1],
                [
                    "pipeline",
                    "--role",
                    "steward",
                    "pause",
                    "freeze",
                    "--reason",
                    "secretary backup create",
                    "--actor",
                    "secretary-backup",
                ],
            )
            self.assertEqual(calls[2], ["pipeline", "--role", "steward", "resume"])
            self.assertNotIn("--exclude-workspace", calls[1])

    def test_pipeline_pause_appends_exclude_workspace(self):
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(list(args))
            return SimpleNamespace(stdout="{}", stderr="")

        with mock.patch("secretary.backup.subprocess.run", side_effect=fake_run):
            from secretary.backup import _pipeline_action

            _pipeline_action(
                "pause",
                pipeline_worktree=Path("/tmp"),
                command=["pipeline"],
                exclude_workspace=Path("/ws/backup"),
            )

        self.assertEqual(
            calls[0],
            [
                "pipeline",
                "--role",
                "steward",
                "pause",
                "freeze",
                "--reason",
                "secretary backup create",
                "--actor",
                "secretary-backup",
                "--exclude-workspace",
                "/ws/backup",
            ],
        )

    def test_pipeline_pause_reports_dispatcher_without_flag(self):
        import subprocess

        def fake_run(args, **_kwargs):
            raise subprocess.CalledProcessError(
                returncode=2,
                cmd=args,
                stderr="pipeline pause: error: unrecognized arguments: "
                "--exclude-workspace /ws/backup\n",
            )

        with mock.patch("secretary.backup.subprocess.run", side_effect=fake_run):
            from secretary.backup import _pipeline_action

            with self.assertRaisesRegex(RuntimeError, "refusing to pause"):
                _pipeline_action(
                    "pause",
                    pipeline_worktree=Path("/tmp"),
                    command=["pipeline"],
                    exclude_workspace=Path("/ws/backup"),
                )

    def test_create_claimed_worker_excludes_caller_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)
            calls: list[tuple[str, Path | None]] = []

            def fake_pipeline(action, *, exclude_workspace=None, **_kwargs):
                calls.append((action, exclude_workspace))

            with (
                mock.patch.dict(os.environ, {"BOARD_ROLE": "worker"}),
                mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("secretary.backup._pipeline_action", side_effect=fake_pipeline),
                mock.patch("secretary.backup.export_all", side_effect=RuntimeError("stop")),
                mock.patch(
                    "secretary.backup.raw_kanboard_dump",
                    return_value=SimpleNamespace(dump_dir=data_dir / "board" / "raw"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    create_backup(
                        instance,
                        recipient="age1example",
                        allow_claimed_worker=True,
                        caller_workspace=Path("/ws/backup"),
                    )

        self.assertEqual(calls[0], ("pause", Path("/ws/backup")))
        self.assertEqual(calls[1], ("resume", None))

    def test_create_claimed_worker_requires_caller_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)

            with self.assertRaisesRegex(RuntimeError, "caller_workspace"):
                create_backup(
                    instance,
                    recipient="age1example",
                    allow_claimed_worker=True,
                )

    def test_create_rejects_preexisting_freeze(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)

            with (
                mock.patch(
                    "secretary.backup._pipeline_status",
                    return_value={
                        "paused": True,
                        "mode": "freeze",
                        "internal_mode": "hard",
                        "reason": "maintenance",
                        "actor": "steward",
                    },
                ),
                mock.patch(
                    "secretary.backup._pipeline_action",
                    side_effect=AssertionError("pause action should not run"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "already paused"):
                    create_backup(instance, recipient="age1example")

    def test_create_releases_lock_when_preexisting_freeze_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)

            with (
                mock.patch(
                    "secretary.backup._pipeline_status",
                    return_value={
                        "paused": True,
                        "mode": "freeze",
                        "internal_mode": "hard",
                        "reason": "maintenance",
                        "actor": "steward",
                    },
                ),
                mock.patch("secretary.backup._pipeline_action") as pipeline_action,
            ):
                with self.assertRaisesRegex(RuntimeError, "already paused"):
                    create_backup(instance, recipient="age1example")

            pipeline_action.assert_not_called()
            fd = os.open(data_dir / "backups" / ".create.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def test_create_rejects_preexisting_drain_pause(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)

            with (
                mock.patch(
                    "secretary.backup._pipeline_status",
                    return_value={"paused": True, "mode": "drain", "internal_mode": "soft"},
                ),
                mock.patch("secretary.backup._pipeline_action") as pipeline_action,
            ):
                with self.assertRaisesRegex(RuntimeError, "already paused"):
                    create_backup(instance, recipient="age1example")

            pipeline_action.assert_not_called()

    def test_create_rejects_concurrent_create(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)
            lock_path = data_dir / "backups" / ".create.lock"
            lock_path.parent.mkdir(parents=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    create_backup(instance, recipient="age1example")
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            self.assertEqual(sorted(path.name for path in lock_path.parent.iterdir()), [".create.lock"])

    def test_create_publishes_archive_without_clobbering_existing_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)
            backups = data_dir / "backups"
            backups.mkdir(parents=True)
            existing = backups / "secretary-backup-full-20260710T000000Z.tar.age"
            existing.write_bytes(b"keep")

            def fake_raw(data_dir_arg):
                raw = data_dir_arg / "board" / "kanboard-raw-20260710T000000Z"
                (raw / "data").mkdir(parents=True)
                (raw / "data" / "db.sqlite").write_bytes(b"sqlite")
                (raw / "manifest.json").write_text("{}", encoding="utf-8")
                return SimpleNamespace(dump_dir=raw)

            with (
                mock.patch("secretary.backup.datetime") as fake_datetime,
                mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("secretary.backup._pipeline_action", return_value=None),
                mock.patch("secretary.backup.raw_kanboard_dump", side_effect=fake_raw),
                mock.patch(
                    "secretary.backup.export_all",
                    side_effect=lambda data_dir_arg, **_kwargs: _fake_exports(data_dir_arg),
                ),
            ):
                from datetime import datetime, UTC

                fake_datetime.now.return_value = datetime(2026, 7, 10, tzinfo=UTC)
                fake_datetime.strptime.side_effect = datetime.strptime
                fake_datetime.fromtimestamp.side_effect = datetime.fromtimestamp
                result = create_backup(
                    instance,
                    recipient="age1example",
                    encrypt=lambda source, destination, _recipient: shutil.copy2(
                        source, destination
                    ),
                )

            self.assertEqual(existing.read_bytes(), b"keep")
            self.assertNotEqual(result.archive, existing)
            self.assertEqual(result.archive.name, "secretary-backup-full-20260710T000000Z-2.tar.age")
            self.assertTrue(result.archive.is_file())

    def test_create_both_uses_one_pause_and_writes_core_and_full_archives(self):
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

            with (
                mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("secretary.backup._pipeline_action", side_effect=fake_pipeline),
                mock.patch("secretary.backup.raw_kanboard_dump", side_effect=fake_raw),
                mock.patch(
                    "secretary.backup.export_all",
                    side_effect=lambda data_dir_arg, **_kwargs: _fake_exports(data_dir_arg),
                ),
            ):
                results = create_backups(
                    instance,
                    recipient="age1example",
                    encrypt=lambda source, destination, _recipient: shutil.copy2(
                        source,
                        destination,
                    ),
                    backup_kinds=("core", "full"),
                )

            self.assertEqual(pipeline_calls, ["pause", "resume"])
            self.assertEqual([result.manifest["backup_kind"] for result in results], ["core", "full"])
            for result in results:
                verified = verify_backup(
                    result.archive,
                    decrypt=lambda source, destination: shutil.copy2(source, destination),
                )
                self.assertEqual(verified.code, 0, verified.findings)

            core = results[0].archive
            full = results[1].archive
            with tarfile.open(core, "r") as archive:
                core_names = set(archive.getnames())
            with tarfile.open(full, "r") as archive:
                full_names = set(archive.getnames())
            self.assertNotIn(
                "secretary-backup/secretary-data/board/kanboard-raw-20260710T000000Z/data/db.sqlite",
                core_names,
            )
            self.assertIn(
                "secretary-backup/secretary-data/board/kanboard-raw-20260710T000000Z/data/db.sqlite",
                full_names,
            )
            self.assertNotIn("secretary-backup/secretary-data/runs/runs.ndjson", core_names)
            self.assertIn("secretary-backup/secretary-data/runs/runs.ndjson", full_names)

    def test_failed_create_does_not_leave_zero_length_final_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)

            with (
                mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("secretary.backup._pipeline_action", side_effect=RuntimeError("pause failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "pause failed"):
                    create_backup(instance, recipient="age1example")

            self.assertEqual(list((data_dir / "backups").glob("*.tar.age")), [])

    def test_core_filters_done_cards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)

            def fake_raw(data_dir_arg):
                raw = data_dir_arg / "board" / "kanboard-raw-20260710T000000Z"
                (raw / "data").mkdir(parents=True)
                (raw / "data" / "db.sqlite").write_bytes(b"sqlite")
                (raw / "manifest.json").write_text("{}", encoding="utf-8")
                return SimpleNamespace(dump_dir=raw)

            with (
                mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("secretary.backup._pipeline_action", return_value=None),
                mock.patch("secretary.backup.raw_kanboard_dump", side_effect=fake_raw),
                mock.patch(
                    "secretary.backup.export_all",
                    side_effect=lambda data_dir_arg, **_kwargs: _fake_exports(
                        data_dir_arg,
                        include_done=True,
                    ),
                ),
            ):
                result = create_backup(
                    instance,
                    recipient="age1example",
                    encrypt=lambda source, destination, _recipient: shutil.copy2(
                        source,
                        destination,
                    ),
                    backup_kind="core",
                )

            with tarfile.open(result.archive, "r") as archive:
                cards = json.loads(
                    archive.extractfile(
                        "secretary-backup/secretary-data/board/cards.json"
                    ).read().decode("utf-8")
                )
                manifest = json.loads(
                    archive.extractfile("secretary-backup/versions.json").read().decode("utf-8")
                )
            self.assertEqual([card["reference"] for card in cards["cards"]], ["active-1"])
            self.assertEqual(manifest["components"]["board"]["count"], 1)

    def test_retention_keeps_one_core_and_removes_old_full(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "secretary-data"
            _write_instance(instance, data_dir)
            backups = data_dir / "backups"
            backups.mkdir(parents=True)
            old_core = backups / "secretary-backup-core-20260708T000000Z.tar.age"
            old_full = backups / "secretary-backup-full-20260708T000000Z.tar.age"
            recent_full = backups / "secretary-backup-full-20260710T230000Z.tar.age"
            for path in (old_core, old_full, recent_full):
                path.write_bytes(b"old")
            fresh_mtime = 2_000_000_000
            os.utime(old_core, (fresh_mtime, fresh_mtime))
            os.utime(old_full, (fresh_mtime, fresh_mtime))

            def fake_raw(data_dir_arg):
                raw = data_dir_arg / "board" / "kanboard-raw-20260710T000000Z"
                (raw / "data").mkdir(parents=True)
                (raw / "data" / "db.sqlite").write_bytes(b"sqlite")
                (raw / "manifest.json").write_text("{}", encoding="utf-8")
                return SimpleNamespace(dump_dir=raw)

            with (
                mock.patch("secretary.backup.datetime") as fake_datetime,
                mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("secretary.backup._pipeline_action", return_value=None),
                mock.patch("secretary.backup.raw_kanboard_dump", side_effect=fake_raw),
                mock.patch(
                    "secretary.backup.export_all",
                    side_effect=lambda data_dir_arg, **_kwargs: _fake_exports(data_dir_arg),
                ),
            ):
                fake_datetime.now.return_value = datetime(2026, 7, 11, tzinfo=UTC)
                fake_datetime.strptime.side_effect = datetime.strptime
                fake_datetime.fromtimestamp.side_effect = datetime.fromtimestamp
                result = create_backup(
                    instance,
                    recipient="age1example",
                    encrypt=lambda source, destination, _recipient: shutil.copy2(
                        source,
                        destination,
                    ),
                    backup_kind="core",
                )

            self.assertTrue(result.archive.exists())
            self.assertFalse(old_core.exists())
            self.assertFalse(old_full.exists())
            self.assertTrue(recent_full.exists())

    def test_backup_health_warns_for_stale_archives_and_large_directory(self):
        from datetime import UTC, datetime

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backups = root / "backups"
            backups.mkdir()
            core = backups / "secretary-backup-core-20260709T000000Z.tar.age"
            full = backups / "secretary-backup-full-20260708T000000Z.tar.age"
            core.write_bytes(b"core")
            full.write_bytes(b"full")
            fresh_mtime = datetime(2026, 7, 11, tzinfo=UTC).timestamp()
            os.utime(core, (fresh_mtime, fresh_mtime))
            os.utime(full, (fresh_mtime, fresh_mtime))

            status = check_backup_health(
                root,
                now=datetime(2026, 7, 11, tzinfo=UTC),
                max_bytes=1,
            )

        self.assertTrue(any("core archive is stale" in warning for warning in status.warnings))
        self.assertTrue(any("full archive is stale" in warning for warning in status.warnings))
        self.assertTrue(any("backup directory is large" in warning for warning in status.warnings))

    def test_backup_health_warns_when_core_or_full_is_missing(self):
        from datetime import UTC, datetime

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "backups").mkdir()

            status = check_backup_health(root, now=datetime(2026, 7, 11, tzinfo=UTC))

        self.assertIn("backup core archive is missing", status.warnings)
        self.assertIn("backup full archive is missing", status.warnings)

    def test_backup_health_warns_when_backup_dir_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status = check_backup_health(Path(tmpdir))

        self.assertIn("backup directory is unavailable", status.warnings)

    def test_backup_health_warns_when_backup_dir_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "backups").mkdir()

            with mock.patch("secretary.backup._backup_archives", side_effect=OSError("denied")):
                status = check_backup_health(root)

        self.assertIn("backup directory is unavailable", status.warnings)

    def test_verify_reports_missing_runs_state_sidecars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "core.tar.age"
            payload = root / "payload" / "secretary-backup"
            _write_core_payload(payload)
            (payload / "secretary-data" / "runs" / "claims.json").unlink()
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="secretary-backup")

            result = verify_backup(
                archive,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )

        self.assertEqual(result.code, 1)
        self.assertTrue(
            any("runs_state component path missing from archive: claims" in item for item in result.findings)
        )

    def test_verify_accepts_legacy_full_archive_without_runs_state_claims(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "legacy-full.tar.age"
            payload = root / "payload" / "secretary-backup"
            _write_complete_payload(payload)
            (payload / "secretary-data" / "runs" / "claims.json").unlink()
            raw_data = payload / "secretary-data" / "board" / "kanboard-raw-empty" / "data"
            raw_data.mkdir(parents=True)
            (raw_data / "db.sqlite").write_bytes(b"sqlite")
            (raw_data.parent / "manifest.json").write_text("{}", encoding="utf-8")
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="secretary-backup")

            result = verify_backup(
                archive,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )

        self.assertEqual(result.code, 0, result.findings)

    def test_verify_reports_missing_normalized_board_export_sidecars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "core.tar.age"
            payload = root / "payload" / "secretary-backup"
            _write_core_payload(payload)
            (payload / "secretary-data" / "board" / "cards.ndjson").unlink()
            (payload / "secretary-data" / "board" / "export.json").unlink()
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="secretary-backup")

            result = verify_backup(
                archive,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )

        self.assertEqual(result.code, 1)
        self.assertIn(
            "missing required archive entry: secretary-backup/secretary-data/board/cards.ndjson",
            result.findings,
        )
        self.assertIn(
            "missing required archive entry: secretary-backup/secretary-data/board/export.json",
            result.findings,
        )

    def test_verify_reports_unsupported_non_string_backup_kind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "invalid-kind.tar.age"
            payload = root / "payload" / "secretary-backup"
            _write_complete_payload(payload)
            manifest_path = payload / "versions.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["backup_kind"] = []
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            raw_data = payload / "secretary-data" / "board" / "kanboard-raw-empty" / "data"
            raw_data.mkdir(parents=True)
            (raw_data / "db.sqlite").write_bytes(b"sqlite")
            (raw_data.parent / "manifest.json").write_text("{}", encoding="utf-8")
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="secretary-backup")

            result = verify_backup(
                archive,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )

        self.assertEqual(result.code, 1)
        self.assertIn("unsupported backup kind", result.findings)

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

    def test_verify_returns_1_when_component_paths_are_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "incomplete.tar.age"
            payload = root / "payload" / "secretary-backup"
            (payload / "instance").mkdir(parents=True)
            (payload / "secretary-data" / "board").mkdir(parents=True)
            (payload / "secretary-data" / "memory").mkdir(parents=True)
            (payload / "secretary-data" / "runs").mkdir(parents=True)
            (payload / "secretary-data" / "transcripts").mkdir(parents=True)
            (payload / "debug" / "orca-state").mkdir(parents=True)
            (payload / "instance" / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            (payload / "secretary-data" / "data-manifest.json").write_text("{}", encoding="utf-8")
            (payload / "secretary-data" / "board" / "cards.json").write_text("{}", encoding="utf-8")
            (payload / "secretary-data" / "memory" / "export.ndjson").write_text("", encoding="utf-8")
            (payload / "secretary-data" / "runs" / "watermarks.json").write_text("{}", encoding="utf-8")
            (payload / "secretary-data" / "transcripts" / "inventory.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (payload / "debug" / "orca-state" / "inventory.json").write_text("{}", encoding="utf-8")
            (payload / "versions.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "components": {
                            "raw_board": {"path": "board/raw"},
                            "board": {"path": "board/cards.json"},
                            "memory": {"path": "memory/export.ndjson"},
                            "runs": {"path": "runs/runs.ndjson"},
                            "transcripts": {"path": "transcripts/inventory.json"},
                            "artifacts": {"path": "artifacts/inventory.json"},
                            "debug_orca_state": {"path": "debug/orca-state/inventory.json"},
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="secretary-backup")

            result = verify_backup(
                archive,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )

        self.assertEqual(result.code, 1)
        self.assertTrue(any("runs/runs.ndjson" in item for item in result.findings))
        self.assertTrue(any("artifacts/inventory.json" in item for item in result.findings))

    def test_verify_returns_1_when_raw_board_dump_has_no_data_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "incomplete.tar.age"
            payload = root / "payload" / "secretary-backup"
            _write_complete_payload(payload)
            raw = payload / "secretary-data" / "board" / "kanboard-raw-empty"
            (raw / "data").mkdir(parents=True)
            (raw / "manifest.json").write_text("{}", encoding="utf-8")
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="secretary-backup")

            result = verify_backup(
                archive,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )

        self.assertEqual(result.code, 1)
        self.assertIn("raw board dump has no data files", result.findings)

    def test_verify_returns_1_when_transcript_payload_copies_are_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "with-transcript-copies.tar.age"
            payload = root / "payload" / "secretary-backup"
            _write_complete_payload(payload)
            copy_path = payload / "secretary-data" / "transcripts" / "copies" / "session.jsonl"
            copy_path.parent.mkdir(parents=True)
            copy_path.write_text("{}\n", encoding="utf-8")
            raw = payload / "secretary-data" / "board" / "kanboard-raw-empty" / "data" / "db.sqlite"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"sqlite")
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="secretary-backup")

            result = verify_backup(
                archive,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )

        self.assertEqual(result.code, 1)
        self.assertTrue(
            any("unexpected transcript payload copy" in item for item in result.findings)
        )

    def test_git_commit_uses_product_repo_root(self):
        from secretary.backup import _git_commit

        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(stdout="abc123\n", stderr="")

        with mock.patch("secretary.backup.subprocess.run", side_effect=fake_run):
            self.assertEqual(_git_commit(Path("/product")), "abc123")

        self.assertEqual(calls[0][1]["cwd"], Path("/product"))


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


def _write_export_surface(data_dir: Path, *, include_done: bool = False) -> None:
    cards = [{"reference": "active-1", "column": "Ready"}]
    if include_done:
        cards.append({"reference": "done-1", "column": "Done"})
    (data_dir / "board").mkdir(parents=True, exist_ok=True)
    (data_dir / "board" / "cards.json").write_text(
        json.dumps({"version": 1, "cards": cards}) + "\n",
        encoding="utf-8",
    )
    (data_dir / "board" / "cards.ndjson").write_text(
        "".join(json.dumps(card) + "\n" for card in cards),
        encoding="utf-8",
    )
    (data_dir / "board" / "export.json").write_text('{"version":1}\n', encoding="utf-8")
    (data_dir / "memory").mkdir(parents=True, exist_ok=True)
    (data_dir / "memory" / "export.ndjson").write_text("{}\n", encoding="utf-8")
    (data_dir / "memory" / "index.sqlite").write_bytes(b"index")
    (data_dir / "runs").mkdir(parents=True, exist_ok=True)
    (data_dir / "runs" / "runs.ndjson").write_text("{}\n", encoding="utf-8")
    (data_dir / "runs" / "watermarks.json").write_text('{"version":1,"files":[]}\n', encoding="utf-8")
    (data_dir / "runs" / "cards.json").write_text('{"version":1,"cards":{}}\n', encoding="utf-8")
    (data_dir / "runs" / "claims.json").write_text('{"version":1,"claims":{}}\n', encoding="utf-8")
    (data_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    (data_dir / "transcripts" / "inventory.json").write_text(
        '{"version":1,"transcripts":[]}\n',
        encoding="utf-8",
    )
    (data_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (data_dir / "artifacts" / "inventory.json").write_text(
        '{"version":1,"artifacts":[]}\n',
        encoding="utf-8",
    )
    (data_dir / "backups").mkdir(parents=True, exist_ok=True)
    (data_dir / "backups" / "old.tar.age").write_bytes(b"old")


def _write_complete_payload(payload: Path) -> None:
    (payload / "instance").mkdir(parents=True)
    (payload / "secretary-data" / "board").mkdir(parents=True)
    (payload / "secretary-data" / "memory").mkdir(parents=True)
    (payload / "secretary-data" / "runs").mkdir(parents=True)
    (payload / "secretary-data" / "transcripts").mkdir(parents=True)
    (payload / "secretary-data" / "artifacts").mkdir(parents=True)
    (payload / "debug" / "orca-state").mkdir(parents=True)
    (payload / "instance" / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
    (payload / "secretary-data" / "data-manifest.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "board" / "cards.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "board" / "cards.ndjson").write_text("", encoding="utf-8")
    (payload / "secretary-data" / "board" / "export.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "memory" / "export.ndjson").write_text("{}\n", encoding="utf-8")
    (payload / "secretary-data" / "runs" / "runs.ndjson").write_text("{}\n", encoding="utf-8")
    (payload / "secretary-data" / "runs" / "watermarks.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "runs" / "cards.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "runs" / "claims.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "transcripts" / "inventory.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (payload / "secretary-data" / "artifacts" / "inventory.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (payload / "debug" / "orca-state" / "inventory.json").write_text("{}", encoding="utf-8")
    (payload / "versions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "components": {
                    "raw_board": {"path": "board/kanboard-raw-empty"},
                    "board": {"path": "board/cards.json"},
                    "memory": {"path": "memory/export.ndjson"},
                    "runs": {"path": "runs/runs.ndjson"},
                    "transcripts": {"path": "transcripts/inventory.json"},
                    "artifacts": {"path": "artifacts/inventory.json"},
                    "debug_orca_state": {"path": "debug/orca-state/inventory.json"},
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_core_payload(payload: Path) -> None:
    (payload / "instance").mkdir(parents=True)
    (payload / "secretary-data" / "board").mkdir(parents=True)
    (payload / "secretary-data" / "memory").mkdir(parents=True)
    (payload / "secretary-data" / "runs").mkdir(parents=True)
    (payload / "instance" / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
    (payload / "secretary-data" / "data-manifest.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "board" / "cards.json").write_text(
        '{"version":1,"cards":[]}\n',
        encoding="utf-8",
    )
    (payload / "secretary-data" / "board" / "cards.ndjson").write_text("", encoding="utf-8")
    (payload / "secretary-data" / "board" / "export.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "memory" / "export.ndjson").write_text("{}\n", encoding="utf-8")
    (payload / "secretary-data" / "runs" / "watermarks.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "runs" / "cards.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "runs" / "claims.json").write_text("{}", encoding="utf-8")
    (payload / "versions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "backup_kind": "core",
                "components": {
                    "board": {"path": "board/cards.json"},
                    "memory": {"path": "memory/export.ndjson"},
                    "runs_state": {
                        "path": "runs/watermarks.json",
                        "cards": "runs/cards.json",
                        "claims": "runs/claims.json",
                    },
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _fake_exports(data_dir: Path, *, include_done: bool = False) -> dict[str, DataExport]:
    _write_export_surface(data_dir, include_done=include_done)
    return {
        "board": DataExport(data_dir / "board" / "cards.json", 1, "board"),
        "memory": DataExport(data_dir / "memory" / "export.ndjson", 1, "memory"),
        "runs": DataExport(data_dir / "runs" / "runs.ndjson", 1, "runs"),
        "transcripts": DataExport(data_dir / "transcripts" / "inventory.json", 1, "transcripts"),
        "artifacts": DataExport(data_dir / "artifacts" / "inventory.json", 1, "artifacts"),
    }


if __name__ == "__main__":
    unittest.main()
