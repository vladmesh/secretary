"""End-to-end restore of a fixture backup onto an empty target.

These tests own the Phase 8 chain as a whole: a real age-encrypted archive is
produced, the producer host is destroyed, and the archive is the only input to
the restore. Component-level behaviour stays in test_restore.py and
test_restore_archive.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple
from unittest import mock

from secretary.backup import create_backup
from secretary.backup_policy import ARCHIVE_ROOT
from secretary.cli import main
from secretary.data import export_memory, init_layout
from secretary.host import CollectResult, HostInventory, build_plan
import secretary.restore_commands as restore_commands
from secretary.restore import (
    RestoreError,
    import_normalized_board,
    restore_backup,
    restore_findings,
    restore_state,
)
from tests.restore_fixtures import (
    _EmptyWriteKanboard,
    _git_history,
    _producer_exports,
    _restore_card,
    _write_checksums,
    _write_instance_to,
)


AGE_AVAILABLE = bool(shutil.which("age") and shutil.which("age-keygen"))
AGE_REASON = "age and age-keygen are required for the restore e2e"

REINDEX_SCRIPT = '''
import argparse, hashlib, json, sqlite3, sys
from pathlib import Path

parser = argparse.ArgumentParser()
for name in ("--canon", "--export", "--target-db", "--model"):
    parser.add_argument(name, required=True)
parser.add_argument("--dim", type=int, required=True)
args = parser.parse_args()

canon = Path(args.canon)
if not (canon / ".git").is_dir():
    print(json.dumps({"ok": False, "error": "canon journal is unavailable"}))
    sys.exit(1)
facts = [json.loads(line) for line in Path(args.export).read_text().splitlines() if line]
database = Path(args.target_db)
database.unlink(missing_ok=True)
with sqlite3.connect(database) as conn:
    conn.execute("CREATE TABLE facts (id TEXT PRIMARY KEY, model TEXT, vector BLOB)")
    for fact in facts:
        digest = hashlib.sha256(json.dumps(fact, sort_keys=True).encode()).digest()
        conn.execute(
            "INSERT INTO facts VALUES (?, ?, ?)",
            (str(fact["id"]), args.model, digest[: args.dim]),
        )
print(json.dumps({"ok": True, "parity": {"indexed": len(facts)}}))
'''

FACT_BODY = "---\ntags: [restore]\nsource: e2e\ncreated: 2026-07-16\npinned: false\n---\n"


def _age_keypair(root: Path, name: str) -> tuple[str, Path]:
    """Generate a real age identity and return its recipient and key file."""
    identity = root / f"{name}.agekey"
    subprocess.run(["age-keygen", "-o", str(identity)], capture_output=True, check=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(identity)], text=True, capture_output=True, check=True
    )
    return recipient.stdout.strip(), identity


def _seed_producer(data_dir: Path) -> tuple[list[dict[str, object]], int]:
    """Fill a producer data root with restorable canon and derived state."""
    init_layout(data_dir)
    cards = [
        _restore_card(task_id=12, reference="secretary-1", title="First", column="Ready"),
        _restore_card(
            task_id=13, reference="secretary-2", title="Second", column="Ready", position=2,
            comments=[{"ts": "2026-07-16T09:00:00Z", "text": "[worker]\nprogress"}],
        ),
        # Core archives exclude Done cards, so this card only survives a full restore.
        _restore_card(task_id=14, reference="secretary-3", title="Third", column="Done"),
    ]
    board = data_dir / "board"
    (board / "cards.json").write_text(json.dumps({"version": 1, "cards": cards}), encoding="utf-8")
    (board / "cards.ndjson").write_text(
        "".join(json.dumps(card) + "\n" for card in cards), encoding="utf-8"
    )
    (board / "export.json").write_text("{}", encoding="utf-8")
    raw = board / "kanboard-raw-e2e"
    (raw / "data").mkdir(parents=True)
    (raw / "manifest.json").write_text("{}", encoding="utf-8")
    (raw / "data" / "db.sqlite").write_bytes(b"sqlite")

    journal = data_dir / "memory" / "facts"
    (journal / "global").mkdir()
    for index, slug in enumerate(("alpha", "beta"), start=1):
        (journal / "global" / f"{slug}.md").write_text(
            FACT_BODY + f"{slug.title()} fact survives restore.\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "."], cwd=journal, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"fact {index}"], cwd=journal, check=True, stdout=subprocess.DEVNULL
        )
    facts = export_memory(data_dir, source_dir=journal).count

    runs = data_dir / "runs"
    for name in ("watermarks.json", "cards.json", "claims.json"):
        (runs / name).write_text("{}", encoding="utf-8")
    (runs / "runs.ndjson").write_text("", encoding="utf-8")
    for component in ("transcripts", "artifacts"):
        (data_dir / component / "inventory.json").write_text("{}", encoding="utf-8")

    # Derived state a restore must never treat as canon.
    (data_dir / "memory" / "index.sqlite").write_bytes(b"stale-vector-index")
    debug = data_dir / "debug" / "orca-state"
    debug.mkdir(parents=True)
    (debug / "inventory.json").write_text('{"sessions": ["live-session"]}', encoding="utf-8")
    worktrees = data_dir / "worktrees" / "secretary-1"
    worktrees.mkdir(parents=True)
    (worktrees / "checkout.txt").write_text("live worktree", encoding="utf-8")
    units = data_dir / "generated" / "units"
    units.mkdir(parents=True)
    (units / "secretary-dispatcher.service").write_text("[Unit]\n", encoding="utf-8")
    return cards, facts


class _Fixture(NamedTuple):
    archive: Path
    cards: list[dict[str, object]]
    facts: int
    manifest: dict[str, object]
    history: list[str]


def _create_fixture_backup(root: Path, *, kind: str, recipient: str) -> _Fixture:
    """Produce an encrypted archive the way the nightly backup timer does."""
    source_data = root / "source-data"
    source_instance = _write_instance_to(root / "source-instance", "e2e", source_data)
    cards, facts = _seed_producer(source_data)
    history = _git_history(source_data / "memory" / "facts")
    with (
        mock.patch("secretary.backup._reject_claimed_worker_context"),
        mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
        mock.patch("secretary.backup._pipeline_action", return_value=None),
        mock.patch(
            "secretary.backup.raw_kanboard_dump",
            return_value=type("Dump", (), {"dump_dir": source_data / "board" / "kanboard-raw-e2e"})(),
        ),
        mock.patch(
            "secretary.backup.export_all",
            return_value=_producer_exports(
                source_data, board=len(cards), memory=facts, artifacts=0, source="e2e"
            ),
        ),
    ):
        backup = create_backup(source_instance, recipient=recipient, backup_kind=kind)

    # The pull to vladmesh's machine is the only artefact that leaves the host.
    offsite = root / "offsite"
    offsite.mkdir(exist_ok=True)
    archive = offsite / backup.archive.name
    shutil.copy2(backup.archive, archive)
    shutil.rmtree(source_data)
    shutil.rmtree(source_instance)
    return _Fixture(archive, cards, facts, backup.manifest, history)


def _target_instance(root: Path, name: str, script: Path) -> tuple[Path, Path]:
    data_dir = root / f"{name}-data"
    instance = _write_instance_to(
        root / f"{name}-instance", "e2e", data_dir,
        reindex={
            "memory_reindex_python": sys.executable,
            "memory_reindex_script": str(script),
            "memory_model": "e2e-model",
            "memory_dim": 4,
        },
    )
    return instance, data_dir


def _reindex_script(root: Path) -> Path:
    script = root / "reindex.py"
    script.write_text(REINDEX_SCRIPT, encoding="utf-8")
    return script


def _apply_reconcile(instance: Path, data_dir: Path, root: Path) -> int:
    """Run the reconcile handoff against a host that already matches desired state."""
    report = restore_commands.validate_instance(instance)
    desired = build_plan(report.instance, report.bindings)
    (data_dir / "host-managed.json").write_text(
        json.dumps({"version": 1, "resources": [resource.__dict__ for resource in desired]}),
        encoding="utf-8",
    )
    host_fixture = root / "host-fixture"
    host_fixture.mkdir(exist_ok=True)
    (host_fixture / "units.txt").write_text(
        "\n".join(resource.name for resource in desired if resource.kind == "unit"), encoding="utf-8"
    )
    if main(["reconcile", "plan", "--instance", str(instance), "--host-fixture", str(host_fixture)]):
        raise AssertionError("reconcile plan rejected the restored desired state")
    inventory = HostInventory(units={resource.name for resource in desired if resource.kind == "unit"})
    live = mock.Mock()
    live.collect.return_value = CollectResult(inventory=inventory)
    with mock.patch.object(restore_commands, "LiveHostSource", return_value=live):
        return main(["restore-reconcile", "--instance", str(instance)])


@unittest.skipUnless(AGE_AVAILABLE, AGE_REASON)
class RestoreEndToEndTests(unittest.TestCase):
    def test_fixture_backup_restores_to_green_doctor_without_the_source_data_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            recipient, identity = _age_keypair(root, "operator")
            fixture = _create_fixture_backup(root, kind="full", recipient=recipient)
            script = _reindex_script(root)

            # An empty target is a supported install on its own.
            empty_instance, empty_data = _target_instance(root, "empty", script)
            self.assertEqual(main(["bootstrap", "--empty", "--instance", str(empty_instance)]), 0)
            self.assertEqual(main(["doctor", "--offline", "--instance", str(empty_instance)]), 0)
            self.assertFalse((empty_data / "board" / "cards.json").exists())

            # The restored target sees only the archive and the age key.
            instance, data_dir = _target_instance(root, "target", script)
            self.assertFalse(data_dir.exists())
            self.assertFalse((root / "source-data").exists())
            self.assertEqual(main([
                "restore", str(fixture.archive), "--instance", str(instance),
                "--age-identity", str(identity),
            ]), 0)

            # A full archive carries the raw dump and every card, done ones included.
            self.assertTrue(list((data_dir / "board").glob("kanboard-raw-*")))
            self.assertEqual(
                import_normalized_board(data_dir, client=_EmptyWriteKanboard()), len(fixture.cards)
            )
            self.assertEqual(main(["memory", "reindex", "--instance", str(instance)]), 0)
            self.assertEqual(_apply_reconcile(instance, data_dir, root), 0)

            self.assertEqual(restore_findings(data_dir), [])
            self.assertEqual(main(["doctor", "--offline", "--instance", str(instance)]), 0)
            state = restore_state(data_dir)
            self.assertEqual(state["board_count"], fixture.manifest["components"]["board"]["count"])
            self.assertEqual(state["memory_index_count"], fixture.facts)
            self.assertEqual(_git_history(data_dir / "memory" / "facts"), fixture.history)

    def test_core_archive_restores_normalized_board_without_a_raw_dump(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            recipient, identity = _age_keypair(root, "operator")
            fixture = _create_fixture_backup(root, kind="core", recipient=recipient)
            instance, data_dir = _target_instance(root, "target", _reindex_script(root))

            self.assertEqual(main([
                "restore", str(fixture.archive), "--instance", str(instance),
                "--age-identity", str(identity),
            ]), 0)

            self.assertEqual(list((data_dir / "board").glob("kanboard-raw-*")), [])
            restored = json.loads((data_dir / "board" / "cards.json").read_text())["cards"]
            # Core keeps every non-done card and says so in its export policy.
            expected = [card for card in fixture.cards if card["column"] != "Done"]
            self.assertEqual(len(restored), fixture.manifest["components"]["board"]["count"])
            self.assertEqual([card["reference"] for card in restored], [card["reference"] for card in expected])
            self.assertEqual(
                json.loads((data_dir / "board" / "export.json").read_text())["policy"]["done_cards"],
                "excluded",
            )
            exported = (data_dir / "memory" / "export.ndjson").read_text().splitlines()
            self.assertEqual(len(exported), fixture.manifest["components"]["memory"]["count"])
            self.assertEqual(len(exported), fixture.facts)
            self.assertEqual(_git_history(data_dir / "memory" / "facts"), fixture.history)
            self.assertGreater(len(fixture.history), 1)

            client = _EmptyWriteKanboard()
            self.assertEqual(import_normalized_board(data_dir, client=client), len(expected))
            self.assertEqual(sorted(task["title"] for task in client.tasks), ["First", "Second"])
            self.assertEqual(restore_state(data_dir)["board_parity"], "complete")

    def test_restore_rejects_a_foreign_age_key_without_creating_the_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            recipient, _ = _age_keypair(root, "operator")
            _, foreign = _age_keypair(root, "foreign")
            fixture = _create_fixture_backup(root, kind="core", recipient=recipient)
            instance, data_dir = _target_instance(root, "target", _reindex_script(root))

            self.assertEqual(main([
                "restore", str(fixture.archive), "--instance", str(instance),
                "--age-identity", str(foreign),
            ]), 2)
            self.assertFalse(data_dir.exists())

    def test_restore_rejects_a_corrupted_archive_without_creating_the_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            recipient, identity = _age_keypair(root, "operator")
            fixture = _create_fixture_backup(root, kind="core", recipient=recipient)
            payload = bytearray(fixture.archive.read_bytes())
            payload[-1] ^= 0xFF
            fixture.archive.write_bytes(payload)
            instance, data_dir = _target_instance(root, "target", _reindex_script(root))

            self.assertEqual(main([
                "restore", str(fixture.archive), "--instance", str(instance),
                "--age-identity", str(identity),
            ]), 2)
            self.assertFalse(data_dir.exists())


class RestoreEndToEndOfflineTests(unittest.TestCase):
    """Chain-level negatives that do not need real age tooling."""

    def test_restore_rejects_an_unsupported_archive_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = _repacked_archive(root, {"version": 99})
            instance, data_dir = _target_instance(root, "target", _reindex_script(root))

            with self.assertRaisesRegex(RestoreError, "unsupported backup version"):
                restore_backup(
                    archive, instance, age_identity=None,
                    decrypt=lambda source, destination: shutil.copy2(source, destination),
                )
            self.assertFalse(data_dir.exists())

    def test_restore_refuses_a_non_empty_target_and_leaves_it_untouched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = _plain_archive(root)
            instance, data_dir = _target_instance(root, "target", _reindex_script(root))
            init_layout(data_dir)
            marker = data_dir / "board" / "cards.json"
            marker.write_text('{"version": 1, "cards": []}', encoding="utf-8")

            with self.assertRaisesRegex(RestoreError, "target data root already exists"):
                restore_backup(
                    archive, instance, age_identity=None,
                    decrypt=lambda source, destination: shutil.copy2(source, destination),
                )
            self.assertEqual(marker.read_text(), '{"version": 1, "cards": []}')

    def test_board_failure_keeps_the_chain_red_until_it_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = _plain_archive(root)
            instance, data_dir = _target_instance(root, "target", _reindex_script(root))
            restore_backup(
                archive, instance, age_identity=None,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )
            client = _EmptyWriteKanboard()
            client.tasks.append({
                "id": 99, "reference": "secretary-stale", "title": "Stale", "description": "",
                "column_id": 2, "position": 1, "swimlane_id": 4,
                "date_creation": "1720000200", "date_modification": "1720000200",
            })
            client.metadata[99] = {}
            client.comments[99] = []

            with self.assertRaisesRegex(RestoreError, "board is not empty"):
                import_normalized_board(data_dir, client=client)

            self.assertIn("board restore is incomplete", restore_findings(data_dir))
            self.assertEqual(main(["doctor", "--offline", "--instance", str(instance)]), 1)

    def test_reconcile_failure_keeps_the_chain_red(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = _plain_archive(root)
            instance, data_dir = _target_instance(root, "target", _reindex_script(root))
            restore_backup(
                archive, instance, age_identity=None,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )
            # A core archive carries the two non-done cards.
            self.assertEqual(import_normalized_board(data_dir, client=_EmptyWriteKanboard()), 2)
            self.assertEqual(main(["memory", "reindex", "--instance", str(instance)]), 0)

            source = mock.Mock()
            source.collect.return_value = CollectResult(inventory=HostInventory(units=set()))
            with mock.patch.object(restore_commands, "LiveHostSource", return_value=source):
                self.assertEqual(main(["restore-reconcile", "--instance", str(instance)]), 1)

            self.assertIn("managed reconcile has not been applied", restore_findings(data_dir))
            self.assertEqual(main(["doctor", "--offline", "--instance", str(instance)]), 1)

    def test_derived_host_state_is_never_restored_as_canon(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = _plain_archive(root, kind="full")
            instance, data_dir = _target_instance(root, "target", _reindex_script(root))

            plan = restore_backup(
                archive, instance, age_identity=None,
                decrypt=lambda source, destination: shutil.copy2(source, destination),
            )

            actions = {component["name"]: component["action"] for component in plan.components}
            self.assertEqual(actions["debug_orca_state"], "exclude")
            self.assertEqual(actions["memory_index"], "rebuild")
            for derived in ("debug", "worktrees", "generated"):
                self.assertFalse((data_dir / derived).exists(), derived)
            # The layout owns backups/; the producer's own archives are not canon.
            self.assertEqual(list((data_dir / "backups").iterdir()), [])
            self.assertFalse((data_dir / "memory" / "index.sqlite").exists())
            with tarfile.open(archive) as bundle:
                names = bundle.getnames()
            self.assertFalse([name for name in names if "index.sqlite" in name])
            self.assertFalse([name for name in names if "/worktrees/" in name])
            self.assertFalse([name for name in names if "/generated/" in name])
            # The Orca snapshot travels as debug only, never as restorable data.
            self.assertIn(f"{ARCHIVE_ROOT}/debug/orca-state/inventory.json", names)
            self.assertFalse([
                name for name in names
                if "orca-state" in name and name.startswith(f"{ARCHIVE_ROOT}/secretary-data/")
            ])


def _repacked_archive(root: Path, manifest_changes: dict[str, object], *, kind: str = "core") -> Path:
    """Rewrite a fixture archive's manifest, keeping its checksums consistent."""
    payload = root / ARCHIVE_ROOT
    with tarfile.open(_plain_archive(root, kind=kind)) as bundle:
        bundle.extractall(root, filter="data")
    manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
    manifest.update(manifest_changes)
    _write_checksums(payload, manifest)
    (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
    repacked = root / "repacked.tar"
    with tarfile.open(repacked, "w") as bundle:
        bundle.add(payload, arcname=ARCHIVE_ROOT)
    shutil.rmtree(payload)
    return repacked


def _plain_archive(root: Path, *, kind: str = "core") -> Path:
    """Build a fixture archive without paying for age on every negative."""
    with mock.patch("secretary.backup._encrypt_with_age", _copy_instead_of_age):
        return _create_fixture_backup(root, kind=kind, recipient="age1e2e").archive


def _copy_instead_of_age(source: Path, destination: Path, recipient: str, *, age_command: str) -> None:
    shutil.copy2(source, destination)
