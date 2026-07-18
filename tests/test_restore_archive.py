from __future__ import annotations

import json
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import main
from secretary.backup import create_backup, verify_backup
from secretary.backup_policy import ARCHIVE_ROOT
from secretary._fsutil import sha256_file
from secretary.data import DataExport, export_memory, init_layout, normalize_board_card
from secretary.host import CollectResult, HostInventory, build_plan
import secretary.restore_commands as restore_commands
import secretary.restore as restore_module
import secretary.backup_verify as backup_verify_module
from secretary.restore import (
    RestoreError,
    bootstrap_empty,
    import_normalized_board,
    mark_reconcile_applied,
    rebuild_memory_index,
    restore_backup,
    restore_findings,
    restore_state,
)
from tests.test_tasks import WriteKanboard


from tests.restore_fixtures import (
    _core_archive, _full_archive, _git_history, _prepare_producer_data,
    _producer_exports, _write_checksums, _write_instance, _write_instance_to,
)


class RestoreArchiveTests(unittest.TestCase):
    def test_restore_validates_then_publishes_staged_core_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")

            plan = restore_backup(
                archive, instance,
            )

            data_dir = root / "secretary-data"
            self.assertEqual(plan.backup_kind, "core")
            self.assertEqual(
                plan.components,
                (
                    {"name": "board", "action": "restore"},
                    {"name": "memory", "action": "restore"},
                    {"name": "runs_state", "action": "restore"},
                    {"name": "memory_index", "action": "rebuild"},
                    {"name": "board_restore", "action": "handoff"},
                    {"name": "host_reconcile", "action": "handoff"},
                ),
            )
            self.assertTrue((data_dir / "board" / "cards.json").is_file())
            self.assertTrue((data_dir / "memory" / "facts").is_dir())

    def test_restore_runs_checksum_preflight_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            original = backup_verify_module.verify_restore_payload

            with mock.patch.object(
                backup_verify_module,
                "verify_restore_payload",
                wraps=original,
            ) as verify:
                restore_backup(
                    archive,
                    instance,
                )

        verify.assert_called_once()

    def test_restore_cli_publishes_and_returns_stable_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            output = io.StringIO()

            def restoring_archive(archive_arg, instance_arg, **kwargs):
                return restore_backup(
                    archive_arg,
                    instance_arg,
                    **kwargs,
                )

            with (
                mock.patch("secretary.restore_commands.restore_backup", side_effect=restoring_archive),
                mock.patch("sys.stdout", output),
            ):
                code = main(["restore", str(archive), "--instance", str(instance)])

            payload = json.loads(output.getvalue())
            self.assertTrue((root / "secretary-data").is_dir())

        self.assertEqual(code, 0)
        self.assertEqual(
            payload,
            {
                "action": "restore",
                "archive": str(archive),
                "backup_kind": "core",
                "backup_version": 1,
                "components": [
                    {"name": "board", "action": "restore"},
                    {"name": "memory", "action": "restore"},
                    {"name": "runs_state", "action": "restore"},
                    {"name": "memory_index", "action": "rebuild"},
                    {"name": "board_restore", "action": "handoff"},
                    {"name": "host_reconcile", "action": "handoff"},
                ],
                "data_dir": str(root / "secretary-data"),
                "dry_run": False,
                "instance_identity": {
                    "instance_remote": "git@example.invalid:test/instance.git",
                    "name": "test",
                },
                "next_steps": ["memory index rebuild", "board restore", "reconcile"],
                "ok": True,
            },
        )

    def test_restore_rejects_identity_before_creating_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "target")
            archive = _core_archive(root, "source")

            with self.assertRaisesRegex(RestoreError, "identity"):
                restore_backup(
                    archive, instance,
                )

            self.assertFalse((root / "secretary-data").exists())

    def test_restore_never_overwrites_existing_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            data_dir = root / "secretary-data"
            data_dir.mkdir()
            (data_dir / "keep").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(RestoreError, "already exists"):
                restore_backup(
                    archive, instance,
                )
            self.assertEqual((data_dir / "keep").read_text(encoding="utf-8"), "keep")

    def test_bootstrap_rejects_relative_data_root_before_creating_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            instance.mkdir()
            (instance / "instance.yaml").write_text(
                "version: 1\nname: test\ndata_dir: relative-data\n"
                "offsite:\n  instance_remote: git@example.invalid:test/instance.git\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RestoreError, "must match pattern"):
                bootstrap_empty(instance)
            self.assertFalse((root / "relative-data").exists())

    def test_restore_rejects_archive_without_memory_journal_before_staging(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            with tarfile.open(archive, "r") as source:
                members = [member for member in source.getmembers() if ".git" not in member.name]
                stripped = root / "without-journal.tar"
                with tarfile.open(stripped, "w") as destination:
                    for member in members:
                        handle = source.extractfile(member) if member.isfile() else None
                        destination.addfile(member, handle)

            with self.assertRaisesRegex(RestoreError, "memory/facts/.git/HEAD"):
                restore_backup(
                    stripped, instance,
                )
            self.assertFalse((root / "secretary-data").exists())

    def test_verify_rejects_archive_with_missing_memory_journal_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = _core_archive(root, "test")
            damaged = root / "without-journal-object.tar"
            with tarfile.open(archive, "r") as source, tarfile.open(damaged, "w") as destination:
                for member in source.getmembers():
                    if member.isfile() and ".git/objects/" in member.name:
                        continue
                    handle = source.extractfile(member) if member.isfile() else None
                    destination.addfile(member, handle)

            result = verify_backup(
                damaged,
            )

        self.assertEqual(result.code, 1)
        self.assertTrue(
            any("checksum manifest does not match archive" in finding for finding in result.findings)
        )

    def test_verify_and_restore_reject_journal_without_resolvable_head(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            extracted = root / "extracted"
            with tarfile.open(archive, "r") as source:
                source.extractall(extracted, filter="data")
            payload = extracted / ARCHIVE_ROOT
            head = payload / "secretary-data" / "memory" / "facts" / ".git" / "HEAD"
            head.write_text("ref: refs/heads/does-not-exist\n", encoding="utf-8")
            manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
            _write_checksums(payload, manifest)
            (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
            damaged = root / "broken-head.tar"
            with tarfile.open(damaged, "w") as destination:
                destination.add(payload, arcname=ARCHIVE_ROOT)

            verified = verify_backup(
                damaged,
            )

            with self.assertRaisesRegex(RestoreError, "memory journal has no valid HEAD commit"):
                restore_backup(
                    damaged,
                    instance,
                )

            self.assertFalse((root / "secretary-data").exists())
        self.assertEqual(verified.code, 1)
        self.assertIn("memory journal has no valid HEAD commit", verified.findings)

    def test_full_restore_marks_debug_excluded_and_restores_data_components(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _full_archive(root, "test")

            plan = restore_backup(
                archive, instance,
            )

            actions = {component["name"]: component["action"] for component in plan.components}
            data_dir = root / "secretary-data"
            self.assertEqual(actions["debug_orca_state"], "exclude")
            self.assertEqual(actions["memory_index"], "rebuild")
            self.assertTrue((data_dir / "transcripts" / "inventory.json").is_file())
            self.assertTrue((data_dir / "artifacts" / "inventory.json").is_file())
            self.assertFalse((data_dir / "debug").exists())

    def test_restore_rejects_checksum_mismatch_without_publishing_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            (payload / "secretary-data" / "board" / "cards.json").write_text('{"cards": ["changed"]}', encoding="utf-8")
            with tarfile.open(archive, "w") as bundle:
                bundle.add(payload, arcname=ARCHIVE_ROOT)

            with self.assertRaisesRegex(RestoreError, "checksum mismatch"):
                restore_backup(
                    archive,
                    instance,
                )
            self.assertFalse((root / "secretary-data").exists())

    def test_restore_rejects_unexpected_data_component_before_publishing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            extra = payload / "secretary-data" / "runs" / "untrusted.json"
            extra.write_text("{}", encoding="utf-8")
            manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
            _write_checksums(payload, manifest)
            (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
            with tarfile.open(archive, "w") as bundle:
                bundle.add(payload, arcname=ARCHIVE_ROOT)

            with self.assertRaisesRegex(RestoreError, "unexpected data component"):
                restore_backup(
                    archive,
                    instance,
                )
            self.assertFalse((root / "secretary-data").exists())

    def test_restore_discards_memory_journal_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            git_dir = payload / "secretary-data" / "memory" / "facts" / ".git"
            (git_dir / "hooks").mkdir(parents=True)
            (git_dir / "hooks" / "post-checkout").write_text("exit 1\n", encoding="utf-8")
            (git_dir / "config").write_text("[core]\nfsmonitor = bad\n", encoding="utf-8")
            (git_dir / "modules" / "nested").mkdir(parents=True)
            (git_dir / "modules" / "nested" / "config").write_text("[core]\npager = bad\n", encoding="utf-8")
            manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
            _write_checksums(payload, manifest)
            (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
            with tarfile.open(archive, "w") as bundle:
                bundle.add(payload, arcname=ARCHIVE_ROOT)

            restore_backup(
                archive,
                instance,
            )
            restored_git = root / "secretary-data" / "memory" / "facts" / ".git"
            self.assertFalse((restored_git / "hooks").exists())
            self.assertFalse((restored_git / "config").exists())
            self.assertFalse((restored_git / "modules").exists())
            self.assertEqual(len(_git_history(restored_git.parent)), 2)
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=restored_git.parent,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")
            tracked = subprocess.run(
                ["git", "ls-files"],
                cwd=restored_git.parent,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(tracked.stdout.splitlines(), ["fact.md", "second-fact.md"])

    def test_verify_and_restore_ignore_memory_journal_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            git_dir = payload / "secretary-data" / "memory" / "facts" / ".git"
            marker = root / "runtime-hook-ran"
            hooks = git_dir / "hooks"
            hooks.mkdir()
            hook = hooks / "post-checkout"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            hook.chmod(0o755)
            (git_dir / "config").write_text(
                "[core]\n\thooksPath = hooks\n",
                encoding="utf-8",
            )
            manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
            _write_checksums(payload, manifest)
            (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
            with tarfile.open(archive, "w") as bundle:
                bundle.add(payload, arcname=ARCHIVE_ROOT)

            verified = verify_backup(
                archive,
            )
            restore_backup(
                archive,
                instance,
            )

            restored_git = root / "secretary-data" / "memory" / "facts" / ".git"
            self.assertEqual(verified.code, 0, verified.findings)
            self.assertFalse(marker.exists())
            self.assertFalse((restored_git / "config").exists())
            self.assertFalse((restored_git / "hooks").exists())

    def test_verify_and_restore_reject_journal_object_alternates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            objects = payload / "secretary-data" / "memory" / "facts" / ".git" / "objects"
            alternate_store = root / "alternate-objects"
            shutil.copytree(objects, alternate_store)
            shutil.rmtree(objects)
            (objects / "info").mkdir(parents=True)
            (objects / "info" / "alternates").write_text(
                f"{alternate_store}\n",
                encoding="utf-8",
            )
            manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
            _write_checksums(payload, manifest)
            (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
            with tarfile.open(archive, "w") as bundle:
                bundle.add(payload, arcname=ARCHIVE_ROOT)

            with mock.patch("secretary.backup_verify.subprocess.run") as git:
                verified = verify_backup(
                    archive,
                )
                with self.assertRaisesRegex(RestoreError, "unexpected data component"):
                    restore_backup(
                        archive,
                        instance,
                    )
            git.assert_not_called()

            self.assertEqual(verified.code, 1)
            self.assertIn(
                "unexpected data component: memory/facts/.git/objects/info/alternates",
                verified.findings,
            )
            self.assertFalse((root / "secretary-data").exists())

    def test_restore_publish_failure_leaves_target_unpublished(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            from unittest import mock
            real_replace = os.replace

            def fail_publish(source, destination):
                if Path(destination) == root / "secretary-data":
                    raise OSError("disk error")
                return real_replace(source, destination)

            with mock.patch("secretary.restore.os.replace", side_effect=fail_publish):
                with self.assertRaisesRegex(RestoreError, "restore staging failed"):
                    restore_backup(
                        archive,
                        instance,
                    )
            self.assertFalse((root / "secretary-data").exists())

    def test_restore_state_write_failure_leaves_target_unpublished(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")

            with mock.patch(
                "secretary.restore.write_text_atomic",
                side_effect=RuntimeError("disk error"),
            ):
                with self.assertRaisesRegex(RestoreError, "could not record restore progress"):
                    restore_backup(
                        archive,
                        instance,
                    )

            self.assertFalse((root / "secretary-data").exists())

    def test_restore_preserves_memory_history_and_manifest_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")

            restore_backup(
                archive,
                instance,
            )

            data_dir = root / "secretary-data"
            manifest = json.loads((root / ARCHIVE_ROOT / "versions.json").read_text(encoding="utf-8"))
            source_journal = root / ARCHIVE_ROOT / "secretary-data" / "memory" / "facts"
            expected_history = _git_history(source_journal)
            cards = json.loads((data_dir / "board" / "cards.json").read_text())["cards"]
            self.assertEqual(len(cards), manifest["components"]["board"]["count"])
            facts = (data_dir / "memory" / "export.ndjson").read_text().splitlines()
            self.assertEqual(len(facts), manifest["components"]["memory"]["count"])
            self.assertEqual(_git_history(data_dir / "memory" / "facts"), expected_history)

    def test_create_backups_round_trip_through_restore(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_data = root / "source-data"
            source_instance = _write_instance_to(root / "source-instance", "test", source_data)
            _prepare_producer_data(source_data)

            for kind in ("core", "full"):
                target_data = root / f"target-{kind}-data"
                target_instance = _write_instance_to(
                    root / f"target-{kind}-instance", "test", target_data
                )
                with (
                    mock.patch("secretary.backup._reject_claimed_worker_context"),
                    mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
                    mock.patch("secretary.backup._pipeline_action", return_value=None),
                    mock.patch(
                        "secretary.backup.raw_kanboard_dump",
                        return_value=type("Dump", (), {"dump_dir": source_data / "board" / "kanboard-raw-test"})(),
                    ),
                    mock.patch(
                        "secretary.backup.export_all",
                        return_value=_producer_exports(source_data),
                    ),
                ):
                    backup = create_backup(
                        source_instance,
                        backup_kind=kind,
                    )

                restore_backup(
                    backup.archive,
                    target_instance,
                )
                manifest = backup.manifest
                cards = json.loads((target_data / "board" / "cards.json").read_text())["cards"]
                facts = (target_data / "memory" / "export.ndjson").read_text().splitlines()
                self.assertEqual(len(cards), manifest["components"]["board"]["count"])
                self.assertEqual(len(facts), manifest["components"]["memory"]["count"])
                self.assertEqual(
                    _git_history(target_data / "memory" / "facts"),
                    _git_history(source_data / "memory" / "facts"),
                )
