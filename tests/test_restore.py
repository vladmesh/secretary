from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.backup import create_backup
from secretary.backup_policy import ARCHIVE_ROOT
from secretary._fsutil import sha256_file
from secretary.data import DataExport, export_memory, init_layout, normalize_board_card
from secretary.restore import (
    RestoreError,
    bootstrap_empty,
    import_normalized_board,
    rebuild_memory_index,
    restore_backup,
    restore_findings,
)
from tests.test_tasks import WriteKanboard


class RestoreTests(unittest.TestCase):
    def test_restored_board_uses_normalized_export_shape_idempotently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            exported = normalize_board_card(
                {
                    "id": 12, "reference": "secretary-1", "title": "Restore", "column": "Ready",
                    "swimlane": "Secretary", "position": 1, "task_type": "code", "project": "secretary",
                },
                {
                    "id": 12, "reference": "secretary-1", "title": "Restore", "description": "body",
                    "column": "Ready", "task_type": "code", "project": "secretary",
                    "claim": "worker", "blocked_by": "secretary-0",
                    "metadata": {"claim": "worker", "blocked_by": "secretary-0", "complexity": "hard"},
                    "comments": [],
                },
            )
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [exported]}), encoding="utf-8"
            )
            client = _EmptyWriteKanboard()

            self.assertEqual(import_normalized_board(data_dir, client=client), 1)
            self.assertEqual(import_normalized_board(data_dir, client=client), 1)

            self.assertEqual(len(client.tasks), 1)
            self.assertEqual(client.tasks[0]["reference"], "secretary-1")
            self.assertEqual(client.metadata[12]["claim"], "worker")
            self.assertEqual(client.metadata[12]["blocked_by"], "secretary-0")

    def test_reindex_and_restore_findings_are_derived_from_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            facts = data_dir / "memory" / "facts"
            (facts / "global").mkdir()
            (facts / "global" / "one.md").write_text("fact\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=facts, check=True)
            subprocess.run(["git", "commit", "-m", "fact"], cwd=facts, check=True, stdout=subprocess.DEVNULL)
            (data_dir / "memory" / "index.sqlite").write_bytes(b"broken")

            self.assertEqual(rebuild_memory_index(data_dir), 1)
            self.assertIn("board restore is incomplete", restore_findings(data_dir))

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
                        recipient="age1example",
                        backup_kind=kind,
                        encrypt=lambda source, destination, _recipient: shutil.copy2(source, destination),
                    )

                restore_backup(
                    backup.archive,
                    target_instance,
                    age_identity=None,
                    decrypt=lambda source, destination: shutil.copy2(source, destination),
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


class _EmptyWriteKanboard(WriteKanboard):
    def __init__(self) -> None:
        super().__init__()
        self.tasks = []
        self.metadata = {}

    def call(self, method: str, **params: object) -> object:
        if method == "createTask":
            self.calls.append((method, params))
            task_id = 12
            self.tasks.append(
                {
                    "id": task_id, "reference": "", "title": params["title"],
                    "description": params.get("description", ""), "column_id": params["column_id"],
                    "position": 1, "swimlane_id": params.get("swimlane_id") or 0,
                    "date_creation": "1720000200", "date_modification": "1720000200",
                }
            )
            self.metadata[task_id] = {}
            return task_id
        return super().call(method, **params)


def _write_instance(root: Path, name: str) -> Path:
    return _write_instance_to(root / "instance", name, root / "secretary-data")


def _write_instance_to(instance: Path, name: str, data_dir: Path) -> Path:
    instance.mkdir()
    (instance / "instance.yaml").write_text(
        "version: 1\n"
        f"name: {name}\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n  instance_remote: git@example.invalid:test/instance.git\n",
        encoding="utf-8",
    )
    return instance


def _prepare_producer_data(data_dir: Path) -> None:
    init_layout(data_dir)
    journal = data_dir / "memory" / "facts"
    (journal / "fact.md").write_text("# fact\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=journal, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=journal, check=True, stdout=subprocess.DEVNULL)
    export_memory(data_dir)
    board = data_dir / "board"
    cards = [{"reference": "secretary-1", "column": "Ready"}]
    (board / "cards.json").write_text(json.dumps({"cards": cards}), encoding="utf-8")
    (board / "cards.ndjson").write_text("".join(json.dumps(card) + "\n" for card in cards), encoding="utf-8")
    (board / "export.json").write_text("{}", encoding="utf-8")
    raw = board / "kanboard-raw-test"
    (raw / "data").mkdir(parents=True)
    (raw / "manifest.json").write_text("{}", encoding="utf-8")
    (raw / "data" / "db.sqlite").write_bytes(b"sqlite")
    runs = data_dir / "runs"
    for name in ("watermarks.json", "cards.json", "claims.json"):
        (runs / name).write_text("{}", encoding="utf-8")
    (runs / "runs.ndjson").write_text("", encoding="utf-8")
    for component in ("transcripts", "artifacts"):
        directory = data_dir / component
        (directory / "inventory.json").write_text("{}", encoding="utf-8")
    (data_dir / "artifacts" / "report.pdf").write_bytes(b"report")


def _producer_exports(data_dir: Path) -> dict[str, DataExport]:
    return {
        "board": DataExport(data_dir / "board" / "cards.json", 1, "test"),
        "memory": DataExport(data_dir / "memory" / "export.ndjson", 1, "test"),
        "runs": DataExport(data_dir / "runs" / "runs.ndjson", 0, "test"),
        "transcripts": DataExport(data_dir / "transcripts" / "inventory.json", 0, "test"),
        "artifacts": DataExport(data_dir / "artifacts" / "inventory.json", 1, "test"),
    }


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
