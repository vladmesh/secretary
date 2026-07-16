from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from secretary.backup_policy import ARCHIVE_ROOT
from secretary._fsutil import sha256_file
from secretary.restore import RestoreError, bootstrap_empty, restore_backup


class RestoreTests(unittest.TestCase):
    def test_restore_validates_then_publishes_staged_core_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")

            plan = restore_backup(
                archive, instance, age_identity=None,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
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

    def test_restore_rejects_identity_before_creating_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "target")
            archive = _core_archive(root, "source")

            with self.assertRaisesRegex(RestoreError, "identity"):
                restore_backup(
                    archive, instance, age_identity=None,
                    decrypt=lambda source, destination: shutil.copy2(source, destination),
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
                    archive, instance, age_identity=None,
                    decrypt=lambda source, destination: shutil.copy2(source, destination),
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

            with self.assertRaisesRegex(RestoreError, "must be absolute"):
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

            with self.assertRaisesRegex(RestoreError, "memory journal"):
                restore_backup(
                    stripped, instance, age_identity=None,
                    decrypt=lambda source, destination: shutil.copy2(source, destination),
                )
            self.assertFalse((root / "secretary-data").exists())

    def test_full_restore_marks_debug_excluded_and_restores_data_components(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _full_archive(root, "test")

            plan = restore_backup(
                archive, instance, age_identity=None,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
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
                    age_identity=None,
                    decrypt=lambda source, destination: shutil.copy2(source, destination),
                )
            self.assertFalse((root / "secretary-data").exists())

    def test_restore_rejects_unexpected_data_component_before_publishing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            extra = payload / "secretary-data" / "board" / "untrusted.json"
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
                    age_identity=None,
                    decrypt=lambda source, destination: shutil.copy2(source, destination),
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
                        age_identity=None,
                        decrypt=lambda source, destination: shutil.copy2(source, destination),
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
                age_identity=None,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
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


def _write_instance(root: Path, name: str) -> Path:
    instance = root / "instance"
    instance.mkdir()
    (instance / "instance.yaml").write_text(
        "version: 1\n"
        f"name: {name}\n"
        f"data_dir: {root / 'secretary-data'}\n"
        "offsite:\n  instance_remote: git@example.invalid:test/instance.git\n",
        encoding="utf-8",
    )
    return instance


def _core_archive(root: Path, name: str) -> Path:
    payload = root / ARCHIVE_ROOT
    board = payload / "secretary-data" / "board"
    memory = payload / "secretary-data" / "memory" / "facts"
    runs = payload / "secretary-data" / "runs"
    board.mkdir(parents=True)
    memory.mkdir(parents=True)
    (memory / "fact.md").write_text("# fact\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=memory, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=memory, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=memory, check=True)
    subprocess.run(["git", "add", "."], cwd=memory, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=memory, check=True, stdout=subprocess.DEVNULL)
    (memory / "second-fact.md").write_text("# second fact\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=memory, check=True)
    subprocess.run(["git", "commit", "-m", "second fact"], cwd=memory, check=True, stdout=subprocess.DEVNULL)
    runs.mkdir(parents=True)
    (payload / "instance").mkdir()
    (payload / "instance" / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
    (payload / "secretary-data" / "data-manifest.json").write_text("{}", encoding="utf-8")
    cards = [{"reference": "secretary-1", "column": "Ready"}]
    (board / "cards.json").write_text(json.dumps({"cards": cards}), encoding="utf-8")
    (board / "cards.ndjson").write_text(
        "".join(json.dumps(card) + "\n" for card in cards), encoding="utf-8"
    )
    (board / "export.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "memory" / "export.ndjson").write_text(
        '{"id":"fact"}\n{"id":"second-fact"}\n', encoding="utf-8"
    )
    for filename in ("watermarks.json", "cards.json", "claims.json"):
        (runs / filename).write_text("{}", encoding="utf-8")
    manifest = {
        "version": 1,
        "backup_kind": "core",
        "instance": {"identity": {"name": name, "instance_remote": "git@example.invalid:test/instance.git"}},
        "components": {
            "board": {"path": "board/cards.json", "count": len(cards)},
            "memory": {"path": "memory/export.ndjson", "count": 2},
            "runs_state": {
                "path": "runs/watermarks.json",
                "cards": "runs/cards.json",
                "claims": "runs/claims.json",
            },
        },
    }
    _write_checksums(payload, manifest)
    (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive = root / "backup.tar"
    with tarfile.open(archive, "w") as bundle:
        bundle.add(payload, arcname=ARCHIVE_ROOT)
    return archive


def _full_archive(root: Path, name: str) -> Path:
    _core_archive(root, name)
    payload = root / ARCHIVE_ROOT
    raw = payload / "secretary-data" / "board" / "kanboard-raw-test"
    (raw / "data").mkdir(parents=True)
    (raw / "manifest.json").write_text("{}", encoding="utf-8")
    (raw / "data" / "db.sqlite").write_bytes(b"sqlite")
    runs = payload / "secretary-data" / "runs"
    (runs / "runs.ndjson").write_text("{}\n", encoding="utf-8")
    for component in ("transcripts", "artifacts"):
        directory = payload / "secretary-data" / component
        directory.mkdir()
        (directory / "inventory.json").write_text("{}", encoding="utf-8")
    debug = payload / "debug" / "orca-state"
    debug.mkdir(parents=True)
    (debug / "inventory.json").write_text("{}", encoding="utf-8")
    manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
    manifest["backup_kind"] = "full"
    manifest["components"] = {
        "raw_board": {"path": "board/kanboard-raw-test"},
        "board": {"path": "board/cards.json"},
        "memory": {"path": "memory/export.ndjson"},
        "runs_state": {"path": "runs/watermarks.json", "cards": "runs/cards.json", "claims": "runs/claims.json"},
        "runs": {"path": "runs/runs.ndjson"},
        "transcripts": {"path": "transcripts/inventory.json"},
        "artifacts": {"path": "artifacts/inventory.json"},
        "debug_orca_state": {"path": "debug/orca-state/inventory.json"},
    }
    _write_checksums(payload, manifest)
    (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive = root / "full.tar"
    with tarfile.open(archive, "w") as bundle:
        bundle.add(payload, arcname=ARCHIVE_ROOT)
    return archive


def _write_checksums(payload: Path, manifest: dict[str, object]) -> None:
    checksums: dict[str, str] = {}
    for path in sorted(payload.rglob("*")):
        if path.is_file() and path.name != "versions.json":
            checksums[path.relative_to(payload).as_posix()] = sha256_file(path)
    manifest["checksums"] = checksums


def _git_history(journal: Path) -> list[str]:
    result = subprocess.run(
        ["git", "rev-list", "HEAD"],
        cwd=journal,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.splitlines()
