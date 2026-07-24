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
from secretary.host_apply import resolve_packaged
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
    _EmptyWriteKanboard,
    _restore_card,
    _seed_instance_facts,
    _write_instance_to,
)


LEGACY_ORCA = Path(__file__).resolve().parent / "fixtures" / "legacy-orca"


def _seed_legacy_facts(data_dir: Path) -> Path:
    """Seed the pre-flatten canon path: a plain directory, no nested journal."""
    facts = data_dir / "memory" / "facts" / "global"
    facts.mkdir(parents=True)
    (facts / "one.md").write_text("fact\n", encoding="utf-8")
    return data_dir / "memory" / "facts"


class RestoreTests(unittest.TestCase):
    def test_empty_bootstrap_stays_outside_restore_doctor_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            instance = _write_instance_to(root / "instance", "test", data_dir)
            bootstrap_empty(instance)

            self.assertFalse((data_dir / "restore-state.json").exists())
            self.assertEqual(main(["doctor", "--offline", "--instance", str(instance)]), 0)

    def test_empty_bootstrap_plan_has_no_handoffs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            instance = _write_instance_to(root / "instance", "test", data_dir)

            plan = bootstrap_empty(instance, dry_run=True)

        self.assertEqual(
            plan.components,
            (
                {"name": "board", "action": "initialized"},
                {"name": "memory", "action": "initialized"},
                {"name": "runs_state", "action": "initialized"},
            ),
        )

    def test_reconcile_marker_does_not_create_restore_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            mark_reconcile_applied(data_dir)
            self.assertFalse((data_dir / "restore-state.json").exists())

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
                    "metadata": {"claim": "worker", "blocked_by": "secretary-0", "complexity": "hard", "resolved_head": "", "resolved_review_head": ""},
                    "comments": [
                        {"ts": "2024-07-03T09:47:00Z", "text": "[worker]\\nfirst"},
                        {"ts": "2024-07-03T09:48:00Z", "text": "[report:done]\\nrestored"},
                    ],
                },
            )
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [exported]}), encoding="utf-8"
            )
            client = _EmptyWriteKanboard()

            self.assertEqual(import_normalized_board(data_dir, client=client), 1)
            # A recovery retry rematerializes canonical events.ndjson, which does
            # not contain the derived restore audit records from the failed host.
            (data_dir / "board" / "events.ndjson").write_text("", encoding="utf-8")
            self.assertEqual(import_normalized_board(data_dir, client=client), 1)

            self.assertEqual(len(client.tasks), 1)
            self.assertEqual(client.tasks[0]["reference"], "secretary-1")
            self.assertEqual(client.metadata[12]["claim"], "worker")
            self.assertEqual(client.metadata[12]["blocked_by"], "secretary-0")
            self.assertEqual(client.metadata[12]["resolved_head"], "")
            self.assertEqual(client.metadata[12]["resolved_review_head"], "")
            self.assertEqual(client.tasks[0]["position"], 1)
            self.assertEqual(client.tasks[0]["swimlane_id"], 4)
            self.assertEqual(
                [call[1]["content"] for call in client.calls if call[0] == "createComment"],
                ["[worker]\\nfirst", "[report:done]\\nrestored"],
            )

    def test_reindex_and_restore_findings_are_derived_from_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            # No instance dir here, so the rebuild falls back to legacy canon.
            _seed_legacy_facts(data_dir)
            (data_dir / "memory" / "index.sqlite").write_bytes(b"broken")

            self.assertEqual(
                rebuild_memory_index(
                    data_dir, None, runner=lambda *_: {"parity": {"indexed": 1}}
                ),
                1,
            )
            self.assertIn("board restore is incomplete", restore_findings(data_dir))

    def test_default_reindex_keeps_the_model_cache_out_of_tmp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            init_layout(data_dir)
            instance = _write_instance_to(root / "instance", "test", data_dir)
            _seed_instance_facts(instance, {"global/one.md": "fact\n"})
            embed = object()
            memory_service = mock.Mock()
            memory_service.build_document_embedder.return_value = embed
            memory_reindex = mock.Mock()
            memory_reindex.rebuild.return_value = {"parity": {"indexed": 1}}
            with mock.patch.dict(
                sys.modules,
                {
                    "secretary.memory_service": memory_service,
                    "secretary.memory_reindex": memory_reindex,
                },
            ):
                self.assertEqual(rebuild_memory_index(data_dir, instance), 1)

            memory_service.build_document_embedder.assert_called_once_with(
                restore_module.DEFAULT_MEMORY_MODEL,
                data_dir / "memory" / ".fastembed-cache",
            )
            self.assertIs(memory_reindex.rebuild.call_args.kwargs["document_embed"], embed)

    def test_board_restore_orders_positions_within_each_column_and_swimlane(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            cards = []
            for reference, position in (("secretary-c", 1), ("secretary-a", 2), ("secretary-b", 3)):
                card = _restore_card()
                card["reference"] = reference
                card["title"] = reference
                card["position"] = position
                cards.append(card)
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": cards}), encoding="utf-8"
            )
            client = _EmptyWriteKanboard()

            self.assertEqual(import_normalized_board(data_dir, client=client), 3)
            restored = sorted(client.tasks, key=lambda task: int(task["position"]))
            self.assertEqual(
                [task["reference"] for task in restored],
                ["secretary-c", "secretary-a", "secretary-b"],
            )
            self.assertEqual([task["position"] for task in restored], [1, 2, 3])

    def test_board_restore_moves_empty_swimlane_to_default_lane(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            card = _restore_card()
            card["swimlane"] = ""
            card["position"] = 1
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [card]}), encoding="utf-8"
            )
            client = _EmptyWriteKanboard()

            self.assertEqual(import_normalized_board(data_dir, client=client), 1)
            self.assertEqual(client.tasks[0]["swimlane_id"], 0)
            self.assertEqual(client.tasks[0]["position"], 1)
            self.assertEqual(restore_state(data_dir)["board_parity"], "complete")
            self.assertIn(
                ("moveTaskPosition", {
                    "project_id": 7, "task_id": 12, "column_id": 2,
                    "position": 1, "swimlane_id": 0,
                }),
                client.calls,
            )

    def test_board_restore_serializes_concurrent_imports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [_restore_card()]}), encoding="utf-8"
            )
            client = _EmptyWriteKanboard()
            entered, release = threading.Event(), threading.Event()
            results: list[tuple[str, object]] = []
            original = restore_module._create_restored_card

            def paused_create(writer, card):
                entered.set()
                self.assertTrue(release.wait(timeout=5))
                original(writer, card)

            def run_restore() -> None:
                try:
                    results.append(("ok", import_normalized_board(data_dir, client=client)))
                except Exception as exc:
                    results.append(("error", exc))

            with mock.patch("secretary.restore._create_restored_card", side_effect=paused_create):
                first = threading.Thread(target=run_restore)
                first.start()
                self.assertTrue(entered.wait(timeout=5))
                second = threading.Thread(target=run_restore)
                second.start()
                release.set()
                first.join(timeout=5)
                second.join(timeout=5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(sorted(results), [("ok", 1), ("ok", 1)])
            self.assertEqual([task["reference"] for task in client.tasks], ["secretary-1"])
            self.assertEqual(len([call for call in client.calls if call[0] == "createTask"]), 1)
            self.assertEqual(restore_state(data_dir)["board_parity"], "complete")

    def test_reindex_cli_uses_published_parity_not_sqlite_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            init_layout(data_dir)
            instance = _write_instance_to(root / "instance", "test", data_dir)
            facts = _seed_instance_facts(instance, {"global/one.md": "fact\n"})
            script = Path(tmpdir) / "reindex.py"
            script.write_text("", encoding="utf-8")
            script.chmod(0o755)
            venv_python = Path(tmpdir) / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(sys.executable)
            completed = subprocess.CompletedProcess([], 0, '{"ok":true,"parity":{"indexed":2}}', "")
            with mock.patch("secretary.restore.subprocess.run", return_value=completed) as run:
                self.assertEqual(
                    rebuild_memory_index(
                        data_dir, instance, python=venv_python, script=script, model="test", dim=4
                    ),
                    2,
                )
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], str(venv_python.absolute()))
            # Canon is the private repo's state/memory/facts, not the data dir.
            self.assertEqual(argv[argv.index("--canon") + 1], str(facts))
            self.assertEqual(restore_state(data_dir)["memory_index_count"], 2)

    def test_reindex_cli_reports_public_contract_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            init_layout(data_dir)
            instance = _write_instance_to(root / "instance", "test", data_dir)
            _seed_instance_facts(instance, {"global/one.md": "fact\n"})
            script = Path(tmpdir) / "reindex.py"
            script.write_text("", encoding="utf-8")
            script.chmod(0o755)
            completed = subprocess.CompletedProcess([], 1, '{"ok":false,"error":"index parity failed"}', "")
            with mock.patch("secretary.restore.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(RestoreError, "index parity failed"):
                    rebuild_memory_index(
                        data_dir, instance, python=Path(sys.executable), script=script,
                        model="test", dim=4,
                    )

    def test_reindex_timeout_is_a_restore_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            init_layout(data_dir)
            instance = _write_instance_to(root / "instance", "test", data_dir)
            _seed_instance_facts(instance, {"global/one.md": "fact\n"})
            script = Path(tmpdir) / "reindex.py"
            script.write_text("", encoding="utf-8")
            script.chmod(0o755)
            with mock.patch(
                "secretary.restore.subprocess.run", side_effect=subprocess.TimeoutExpired([], 1)
            ):
                with self.assertRaisesRegex(RestoreError, "could not rebuild memory index"):
                    rebuild_memory_index(
                        data_dir, instance, python=Path(sys.executable), script=script,
                        model="test", dim=4,
                    )

    def test_restore_board_wraps_missing_backend_configuration(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, {}, clear=True):
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [_restore_card()]}), encoding="utf-8"
            )
            with self.assertRaisesRegex(RestoreError, "Kanboard runtime configuration"):
                import_normalized_board(data_dir)

    def test_board_restore_normalizes_legacy_routing_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            card = _restore_card()
            card["metadata"].update({"complexity": "legacy", "family_preference": "", "blocked_by": "secretary-0"})
            card["fields"]["blocked_by"] = ""
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [card]}), encoding="utf-8"
            )
            client = _EmptyWriteKanboard()
            self.assertEqual(import_normalized_board(data_dir, client=client), 1)
            self.assertEqual(client.metadata[12]["blocked_by"], "secretary-0")
            self.assertEqual(restore_state(data_dir)["board_parity"], "complete")

    def test_restore_handoff_reaches_green_doctor_only_after_reconcile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            instance = _write_instance_to(root / "instance", "test", data_dir, host=True)
            with (instance / "instance.yaml").open("a", encoding="utf-8") as stream:
                stream.write("  foreign_units:\n    - secretary-supervisor.timer\n")
            bootstrap_empty(instance)
            card = _restore_card()
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [card]}), encoding="utf-8"
            )
            _seed_instance_facts(instance, {"global/one.md": "fact\n"})

            self.assertEqual(import_normalized_board(data_dir, client=_EmptyWriteKanboard()), 1)
            self.assertEqual(
                rebuild_memory_index(
                    data_dir, instance, runner=lambda *_: {"parity": {"indexed": 1}}
                ),
                1,
            )

            with mock.patch(
                "secretary.host_apply.find_orca_executable", return_value=LEGACY_ORCA
            ):
                report = restore_commands.validate_instance(instance)
                packaged = resolve_packaged(report.instance, instance_path=report.instance_path.parent)
                desired = build_plan(report.instance, report.bindings, packaged=packaged)
                (data_dir / "host-managed.json").write_text(
                    json.dumps({"version": 1, "resources": [resource.__dict__ for resource in desired]}),
                    encoding="utf-8",
                )
                fixture = root / "host"
                fixture.mkdir()
                live_units = {resource.name for resource in desired if resource.kind == "unit"}
                live_units.add("secretary-supervisor.timer")
                (fixture / "units.txt").write_text(
                    "\n".join(sorted(live_units)), encoding="utf-8"
                )
                self.assertEqual(main([
                    "reconcile", "plan", "--instance", str(instance), "--host-fixture", str(fixture),
                ]), 0)

                inventory = HostInventory(units=live_units)
                source = mock.Mock()
                source.collect.return_value = CollectResult(inventory=inventory)
                with mock.patch.object(restore_commands, "LiveHostSource", return_value=source):
                    self.assertEqual(main(["restore-reconcile", "--instance", str(instance)]), 0)
            self.assertEqual(main(["doctor", "--offline", "--instance", str(instance)]), 0)
            self.assertEqual(restore_findings(data_dir), [])

    def test_restore_reconcile_fails_closed_before_marking_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            instance = _write_instance_to(root / "instance", "test", data_dir, heads=True)
            bootstrap_empty(instance)

            self.assertEqual(main(["restore-reconcile", "--instance", str(instance)]), 2)
            self.assertEqual(restore_findings(data_dir), ["restore is incomplete"])
