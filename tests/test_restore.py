from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

import secretary.restore as restore_module
from secretary import restore_commands
from secretary.checkpoint import _validate_board
from secretary.cli import main as cli_main
from secretary.data import (
    export_board,
    init_layout,
    normalize_board_card,
)
from secretary.host import CollectResult, HostInventory, build_plan
from secretary.host_apply import resolve_packaged
from secretary.product_issues import (
    ProductIssueValidationError,
    validate_product_issue_records,
)
from secretary.restore import (
    RestoreError,
    _normalized_cards,
    _restored_order_mismatch,
    bootstrap_empty,
    import_normalized_board,
    mark_reconcile_applied,
    rebuild_memory_index,
    restore_findings,
    restore_state,
)
from secretary.tasks import TaskReader
from tests.fakes.sprints import SprintKanboard
from tests.orca_fixtures import legacy_orca_runtime
from tests.restore_fixtures import (
    _EmptyWriteKanboard,
    _restore_card,
    _seed_instance_facts,
    _write_instance_to,
)

_UNSET = object()


def main(argv: list[str], *, orca_executable: Path | object = _UNSET) -> int:
    """Run the CLI, relying on the suite-wide hermetic Orca default.

    Pass ``orca_executable`` only to model a deliberately alternate or
    unavailable executable; the default leaves the suite's fixture patch
    (tests/__init__.py) in place instead of shadowing it with the same value.
    """
    if orca_executable is _UNSET:
        return cli_main(argv)
    with mock.patch("secretary.host_apply.find_orca_executable", return_value=orca_executable):
        return cli_main(argv)


def _seed_legacy_facts(data_dir: Path) -> Path:
    """Seed the pre-flatten canon path: a plain directory, no nested journal."""
    facts = data_dir / "memory" / "facts" / "global"
    facts.mkdir(parents=True)
    (facts / "one.md").write_text("fact\n", encoding="utf-8")
    return data_dir / "memory" / "facts"


class RestoreTests(unittest.TestCase):
    @staticmethod
    def _product_card(*, projects: str = '["secretary"]') -> dict[str, object]:
        card = _restore_card(reference="product:secretary", title="Secretary", column="Issues", position=1)
        card["fields"]["task_type"] = ""
        card["fields"]["project"] = ""
        card["metadata"] = {
            "record_type": "product",
            "product_id": "secretary",
            "product_projects": projects,
        }
        return card

    def test_assessment_card_round_trips_through_export_validation_and_restore(self):
        """secretary-1025: a card parked in Assessment survives the durability path intact."""
        live_card = {
            "id": 42,
            "reference": "secretary-1025",
            "title": "Parked",
            "description": "waiting for the observer",
            "column": "Assessment",
            "swimlane": "Secretary",
            "position": 1,
            "task_type": "code",
            "project": "secretary",
            "metadata": {"record_type": "task", "project": "secretary", "task_type": "code"},
            "comments": [{"ts": "10", "text": "[reviewer]\nverdict"}],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            export = export_board(
                data_dir,
                instance_dir=Path(tmpdir),
                reader=mock.Mock(export=mock.Mock(return_value=[live_card])),
                sprint_client=SprintKanboard(),
            )

            self.assertEqual(export.count, 1)
            exported = json.loads((data_dir / "board" / "cards.json").read_text(encoding="utf-8"))
            self.assertEqual(exported["cards"][0]["column"], "Assessment")

            # The checkpoint validates the staged export before it is published.
            _validate_board(data_dir / "board", registered_project_ids=set())

            client = _EmptyWriteKanboard()
            self.assertEqual(import_normalized_board(data_dir, client=client), 1)

            restored = TaskReader(client).show("secretary-1025")
            self.assertEqual(restored["state"], "assessment")
            self.assertEqual(restored["title"], "Parked")
            self.assertEqual(restored["comments"][0]["body"], "[reviewer]\nverdict")
            # A second run is the retry path: parity, not a duplicate card.
            self.assertEqual(import_normalized_board(data_dir, client=client), 1)
            self.assertEqual(len(client.tasks), 1)

    def test_import_never_enters_the_interactive_comment_writers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            card = _restore_card()
            card["comments"] = [
                {"ts": "1", "text": "first"},
                {"ts": "2", "text": "second"},
            ]
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [card]}), encoding="utf-8"
            )
            client = _EmptyWriteKanboard()
            with (
                mock.patch(
                    "secretary.tasks.TaskWriter.restore_comment",
                    side_effect=AssertionError("interactive Card writer entered"),
                ),
            ):
                self.assertEqual(import_normalized_board(data_dir, client=client), 1)
            self.assertEqual([comment["comment"] for comment in client.comments[12]], ["first", "second"])

    def test_missing_card_from_fresh_snapshot_is_a_recorded_parity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [_restore_card()]}), encoding="utf-8"
            )
            client = _EmptyWriteKanboard()
            original = TaskReader.restore_snapshot
            reads = 0

            def disappearing(reader):
                nonlocal reads
                reads += 1
                snapshot = original(reader)
                return snapshot if reads == 1 else {}

            with (
                mock.patch.object(TaskReader, "restore_snapshot", disappearing),
                self.assertRaisesRegex(RestoreError, "board parity check failed"),
            ):
                import_normalized_board(data_dir, client=client)
            self.assertEqual(restore_state(data_dir)["board"], "failed")
            self.assertEqual(restore_state(data_dir)["board_parity"], "failed")

    def test_duplicate_export_is_rejected_before_replacing_the_prior_good_pair(self):
        card = {
            "id": 193,
            "reference": "secretary-784",
            "title": "Original",
            "description": "",
            "column": "Done",
            "swimlane": "Secretary",
            "position": 1,
            "closed": True,
            "task_type": "code",
            "project": "secretary",
            "metadata": {"record_type": "task", "project": "secretary", "task_type": "code"},
            "comments": [],
        }
        duplicate = {**card, "id": 784, "title": "Collision", "position": 2}
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": []}) + "\n", encoding="utf-8"
            )
            (data_dir / "board" / "cards.ndjson").write_text("", encoding="utf-8")
            good_json = (data_dir / "board" / "cards.json").read_bytes()
            good_ndjson = (data_dir / "board" / "cards.ndjson").read_bytes()

            with self.assertRaisesRegex(RuntimeError, "duplicate references secretary-784"):
                export_board(
                    data_dir,
                    instance_dir=Path(tmpdir),
                    reader=mock.Mock(export=mock.Mock(return_value=[card, duplicate])),
                    sprint_client=SprintKanboard(),
                )

            self.assertEqual((data_dir / "board" / "cards.json").read_bytes(), good_json)
            self.assertEqual((data_dir / "board" / "cards.ndjson").read_bytes(), good_ndjson)

    def test_restore_rejects_json_ndjson_parity_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            card = _restore_card()
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [card]}), encoding="utf-8"
            )
            (data_dir / "board" / "cards.ndjson").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RestoreError, "parity mismatch"):
                import_normalized_board(data_dir, client=_EmptyWriteKanboard())

    def test_restore_refuses_a_card_without_a_record_type(self):
        """A card with no kind cannot be placed, so the export is refused by reference."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            card = _restore_card(column="Issues")
            card["metadata"] = {}
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [card]}), encoding="utf-8"
            )
            client = _EmptyWriteKanboard()

            with self.assertRaisesRegex(RestoreError, "secretary-1"):
                import_normalized_board(data_dir, client=client)

            self.assertEqual(client.tasks, [])

    def test_restore_preserves_closed_issue_metadata_and_history(self):
        class IssuesBoard(_EmptyWriteKanboard):
            def call(self, method: str, **params: object) -> object:
                if method == "getColumns":
                    return [
                        {"id": 1, "title": "Issues"},
                        {"id": 2, "title": "Ready"},
                        {"id": 3, "title": "In progress"},
                        {"id": 4, "title": "Validate"},
                        {"id": 5, "title": "Blocked"},
                        {"id": 6, "title": "Done"},
                    ]
                return super().call(method, **params)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            card = _restore_card(
                reference="issue:12",
                column="Issues",
                position=2,
                comments=[{"text": "[issue:closed]\nresolved"}],
            )
            card["closed"] = True
            card["fields"]["task_type"] = ""
            card["fields"]["project"] = ""
            card["metadata"] = {
                "record_type": "issue",
                "issue_product": "secretary",
                "issue_kind": "bug",
                "issue_priority": "P0",
                "issue_closed_reason": "resolved",
            }
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [self._product_card(), card]}), encoding="utf-8"
            )
            client = IssuesBoard()

            self.assertEqual(import_normalized_board(data_dir, client=client), 2)
            self.assertEqual(client.metadata[13]["record_type"], "issue")
            self.assertEqual(client.metadata[13]["issue_closed_reason"], "resolved")
            self.assertEqual(client.comments[13][0]["comment"], "[issue:closed]\nresolved")
            self.assertEqual(import_normalized_board(data_dir, client=client), 2)
            self.assertEqual(len(client.tasks), 2)

    def test_restore_parity_rejects_missing_issue_metadata(self):
        class IssuesBoard(_EmptyWriteKanboard):
            def call(self, method: str, **params: object) -> object:
                if method == "getColumns":
                    return [
                        {"id": 1, "title": "Issues"},
                        {"id": 2, "title": "Ready"},
                        {"id": 3, "title": "In progress"},
                        {"id": 4, "title": "Validate"},
                        {"id": 5, "title": "Blocked"},
                        {"id": 6, "title": "Done"},
                    ]
                if method == "saveTaskMetadata" and "issue_priority" in params.get("values", {}):
                    values = dict(params["values"])
                    values.pop("issue_priority")
                    return super().call(method, task_id=params["task_id"], values=values)
                return super().call(method, **params)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            card = _restore_card(reference="issue:12", column="Issues", position=2)
            card["fields"]["task_type"] = ""
            card["fields"]["project"] = ""
            card["metadata"] = {
                "record_type": "issue",
                "issue_product": "secretary",
                "issue_kind": "bug",
                "issue_priority": "P0",
            }
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [self._product_card(), card]}), encoding="utf-8"
            )

            with self.assertRaisesRegex(RestoreError, "board parity check failed"):
                import_normalized_board(data_dir, client=IssuesBoard())

    def test_restore_rejects_a_closed_card_missing_from_the_export(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            card = _restore_card(reference="secretary-1")
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [card]}), encoding="utf-8"
            )
            client = _EmptyWriteKanboard()
            client.tasks.append(
                {
                    "id": 99,
                    "reference": "secretary-closed",
                    "title": "Old closed card",
                    "description": "",
                    "column_id": 2,
                    "position": 1,
                    "swimlane_id": 0,
                    "is_active": 0,
                    "date_creation": "1720000000",
                    "date_modification": "1720000000",
                }
            )
            client.metadata[99] = {"project": "secretary", "task_type": "code"}
            client.comments[99] = []

            with self.assertRaisesRegex(RestoreError, "board is not empty"):
                import_normalized_board(data_dir, client=client)

            status_ids = [
                params.get("status_id") for method, params in client.calls if method == "getAllTasks"
            ]
            self.assertEqual(status_ids, [1, 0])
            self.assertFalse(any(method == "createTask" for method, _params in client.calls))

    def test_normalized_records_reject_duplicate_or_unknown_product_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            duplicate = self._product_card()
            duplicate["reference"] = "product:other"
            duplicate["metadata"] = {
                "record_type": "product",
                "product_id": "secretary",
                "product_projects": '["secretary"]',
            }
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [self._product_card(projects='["unknown"]'), duplicate]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RestoreError, "unknown registered project"):
                _normalized_cards(data_dir, registered_project_ids={"secretary"})

            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [self._product_card(), duplicate]}), encoding="utf-8"
            )
            duplicate["reference"] = "product:secretary"
            with self.assertRaisesRegex(ProductIssueValidationError, "duplicate Product id"):
                validate_product_issue_records(
                    [self._product_card(), duplicate], registered_project_ids={"secretary"}
                )

    def test_normalized_records_reject_missing_product_kind_and_priority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            issue = _restore_card(reference="issue:12", column="Issues")
            issue["metadata"] = {
                "record_type": "issue",
                "issue_product": "missing",
                "issue_kind": "invalid",
                "issue_priority": "P4",
            }
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [issue]}), encoding="utf-8"
            )

            with self.assertRaisesRegex(RestoreError, "no registered Product"):
                _normalized_cards(data_dir)

            issue["metadata"]["issue_product"] = "secretary"
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [self._product_card(), issue]}), encoding="utf-8"
            )
            with self.assertRaisesRegex(RestoreError, "invalid kind"):
                _normalized_cards(data_dir)

            issue["metadata"]["issue_kind"] = "bug"
            issue["metadata"]["issue_priority"] = "P4"
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [self._product_card(), issue]}), encoding="utf-8"
            )
            with self.assertRaisesRegex(RestoreError, "invalid priority"):
                _normalized_cards(data_dir)

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
                    "id": 12,
                    "reference": "secretary-1",
                    "title": "Restore",
                    "column": "Ready",
                    "swimlane": "Secretary",
                    "position": 1,
                    "task_type": "code",
                    "project": "secretary",
                },
                {
                    "id": 12,
                    "reference": "secretary-1",
                    "title": "Restore",
                    "description": "body",
                    "column": "Ready",
                    "task_type": "code",
                    "project": "secretary",
                    "claim": "worker",
                    "blocked_by": "secretary-0",
                    "metadata": {
                        "record_type": "task",
                        "claim": "worker",
                        "blocked_by": "secretary-0",
                        "complexity": "hard",
                        "resolved_head": "",
                        "resolved_review_head": "",
                    },
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
                rebuild_memory_index(data_dir, None, runner=lambda *_: {"parity": {"indexed": 1}}),
                1,
            )
            self.assertIn("board restore is incomplete", restore_findings(data_dir))

    def test_completed_legacy_restore_state_stays_green_without_sprint_progress(self):
        # A recovery finished before sprint entities joined the checkpoint. Its
        # `restore-state.json` has no sprint keys, and there is nothing left for it to
        # restore, so the new sprint step must not turn doctor red on it.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            init_layout(data_dir)
            instance = _write_instance_to(root / "instance", "test", data_dir, heads=True)
            (data_dir / "restore-state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "board": "complete",
                        "board_parity": "complete",
                        "board_count": 2,
                        "memory_index": "complete",
                        "memory_index_count": 5,
                        "reconcile": "complete",
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(restore_findings(data_dir), [])
            self.assertEqual(main(["doctor", "--offline", "--instance", str(instance)]), 0)

    def test_recovery_that_records_sprint_progress_still_reports_it_unfinished(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            (data_dir / "restore-state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "board": "complete",
                        "board_parity": "complete",
                        "sprints": "pending",
                        "memory_index": "complete",
                        "reconcile": "complete",
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(restore_findings(data_dir), ["sprint restore is incomplete"])

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
                data_dir / "memory" / "fastembed-cache",
                1,
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
                (
                    "moveTaskPosition",
                    {
                        "project_id": 7,
                        "task_id": 12,
                        "column_id": 2,
                        "position": 1,
                        "swimlane_id": 0,
                    },
                ),
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

            def paused_create(writer, card, prefix):
                entered.set()
                self.assertTrue(release.wait(timeout=5))
                original(writer, card, prefix)

            def run_restore() -> None:
                try:
                    results.append(("ok", import_normalized_board(data_dir, client=client)))
                except Exception as exc:  # noqa: BLE001 - the thread returns any restore failure.
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
                        data_dir,
                        instance,
                        python=venv_python,
                        script=script,
                        model="test",
                        dim=4,
                        threads=2,
                    ),
                    2,
                )
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], str(venv_python.absolute()))
            # Canon is the private repo's state/memory/facts, not the data dir.
            self.assertEqual(argv[argv.index("--canon") + 1], str(facts))
            self.assertEqual(
                run.call_args.kwargs["env"]["MEMORY_CACHE_DIR"],
                str(data_dir / "memory" / "fastembed-cache"),
            )
            self.assertEqual(run.call_args.kwargs["env"]["MEMORY_THREADS"], "2")
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
            with (
                mock.patch("secretary.restore.subprocess.run", return_value=completed),
                self.assertRaisesRegex(RestoreError, "index parity failed"),
            ):
                rebuild_memory_index(
                    data_dir,
                    instance,
                    python=Path(sys.executable),
                    script=script,
                    model="test",
                    dim=4,
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
            with (
                mock.patch("secretary.restore.subprocess.run", side_effect=subprocess.TimeoutExpired([], 1)),
                self.assertRaisesRegex(RestoreError, "could not rebuild memory index"),
            ):
                rebuild_memory_index(
                    data_dir,
                    instance,
                    python=Path(sys.executable),
                    script=script,
                    model="test",
                    dim=4,
                )

    def test_restore_board_wraps_missing_backend_configuration(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, {}, clear=True):
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            (data_dir / "board" / "cards.json").write_text(
                json.dumps({"version": 1, "cards": [_restore_card()]}), encoding="utf-8"
            )
            with self.assertRaisesRegex(RestoreError, "requires the target instance"):
                import_normalized_board(data_dir)

    def test_board_restore_normalizes_legacy_routing_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            card = _restore_card()
            card["metadata"].update(
                {"complexity": "legacy", "family_preference": "", "blocked_by": "secretary-0"}
            )
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
                rebuild_memory_index(data_dir, instance, runner=lambda *_: {"parity": {"indexed": 1}}),
                1,
            )

            with legacy_orca_runtime(root) as legacy_orca:
                report = restore_commands.validate_instance(instance)
                with unittest.mock.patch(
                    "secretary.host_apply.find_orca_executable", return_value=None
                ) as find_executable:
                    packaged = resolve_packaged(
                        report.instance,
                        instance_path=report.instance_path.parent,
                        data_dir=report.data_dir,
                        orca_executable=legacy_orca,
                    )
                find_executable.assert_not_called()
                desired = build_plan(report.instance, report.bindings, packaged=packaged)
                (data_dir / "host-managed.json").write_text(
                    json.dumps({"version": 1, "resources": [resource.__dict__ for resource in desired]}),
                    encoding="utf-8",
                )
                fixture = root / "host"
                fixture.mkdir()
                live_units = {resource.name for resource in desired if resource.kind == "unit"}
                live_units.add("secretary-supervisor.timer")
                (fixture / "units.txt").write_text("\n".join(sorted(live_units)), encoding="utf-8")
                self.assertEqual(
                    main(
                        [
                            "reconcile",
                            "plan",
                            "--instance",
                            str(instance),
                            "--host-fixture",
                            str(fixture),
                        ],
                        orca_executable=legacy_orca,
                    ),
                    0,
                )

                inventory = HostInventory(units=live_units)
                source = mock.Mock()
                source.collect.return_value = CollectResult(inventory=inventory)
                with mock.patch.object(restore_commands, "LiveHostSource", return_value=source):
                    self.assertEqual(
                        main(["restore-reconcile", "--instance", str(instance)], orca_executable=legacy_orca),
                        0,
                    )
            self.assertEqual(
                main(["doctor", "--offline", "--instance", str(instance)], orca_executable=legacy_orca), 0
            )
            self.assertEqual(restore_findings(data_dir), [])

    def test_restore_reconcile_fails_closed_before_marking_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "secretary-data"
            instance = _write_instance_to(root / "instance", "test", data_dir, heads=True)
            bootstrap_empty(instance)

            self.assertEqual(main(["restore-reconcile", "--instance", str(instance)]), 2)
            self.assertEqual(restore_findings(data_dir), ["restore is incomplete"])


class RestoredNonTaskSwimlaneTests(unittest.TestCase):
    """Product и Issue восстанавливаются в свой свимлейн, а отказ борда не маскируется.

    `_create_restored_non_task` слал `swimlane_id=0`: живой Kanboard отвечает на это
    `false`, а не идентификатором. Проверка `isinstance(task_id, int)` пропускала
    `false` (bool — подкласс int), поэтому восстановление падало на следующем шаге с
    сообщением про reference, хотя не создавалась сама запись. Тестовый борд ноль
    принимал, и баг доехал до живого восстановления 745 карточек — здесь отказ
    моделируется явно.
    """

    COLUMNS: ClassVar[list[dict[str, object]]] = [
        {"id": 1, "title": "Issues"},
        {"id": 2, "title": "Ready"},
        {"id": 3, "title": "In progress"},
        {"id": 4, "title": "Validate"},
        {"id": 5, "title": "Blocked"},
        {"id": 6, "title": "Done"},
    ]

    def _seed(self, data_dir: Path) -> None:
        init_layout(data_dir)
        issue = _restore_card(reference="issue:12", column="Issues", position=2)
        issue["fields"]["task_type"] = ""
        issue["fields"]["project"] = ""
        issue["metadata"] = {
            "record_type": "issue",
            "issue_product": "secretary",
            "issue_kind": "bug",
            "issue_priority": "P0",
        }
        (data_dir / "board" / "cards.json").write_text(
            json.dumps({"version": 1, "cards": [RestoreTests._product_card(), issue]}), encoding="utf-8"
        )

    def test_record_lands_in_the_swimlane_the_export_names(self) -> None:
        class StrictSwimlaneBoard(_EmptyWriteKanboard):
            """Как настоящий борд: swimlane_id=0 не идентификатор, а отказ."""

            def call(self, method: str, **params: object) -> object:
                if method == "getColumns":
                    return RestoredNonTaskSwimlaneTests.COLUMNS
                if method == "createTask" and params.get("swimlane_id") == 0:
                    return False
                return super().call(method, **params)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            self._seed(data_dir)
            client = StrictSwimlaneBoard()

            self.assertEqual(import_normalized_board(data_dir, client=client), 2)

            # 4 — идентификатор свимлейна "Secretary" в фикстуре борда
            self.assertEqual([task["swimlane_id"] for task in client.tasks], [4, 4])

    def test_a_product_lane_the_board_lacks_is_created_by_the_restore(self) -> None:
        """Запись возвращается в свою продуктовую дорожку, даже если борд её ещё не завёл.

        Запись создаётся в дорожке своего продукта, а чистый борд после bootstrap знает только
        дорожки проектов. Без создания по имени восстановление уронило бы обе записи в чужую
        дорожку — то есть перенесло бы их, чего восстановление делать не должно.
        """

        class ProjectLanesOnlyBoard(_EmptyWriteKanboard):
            def __init__(self) -> None:
                super().__init__()
                self.swimlanes = [{"id": 1, "name": "Default swimlane", "position": 1}]

            def call(self, method: str, **params: object) -> object:
                if method == "getColumns":
                    return RestoredNonTaskSwimlaneTests.COLUMNS
                if method == "createTask" and params.get("swimlane_id") == 0:
                    return False
                return super().call(method, **params)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            self._seed(data_dir)
            cards = json.loads((data_dir / "board" / "cards.json").read_text(encoding="utf-8"))
            for card in cards["cards"]:
                card["swimlane"] = "secretary"
            (data_dir / "board" / "cards.json").write_text(json.dumps(cards), encoding="utf-8")
            client = ProjectLanesOnlyBoard()

            self.assertEqual(import_normalized_board(data_dir, client=client), 2)

            created = [params["name"] for method, params in client.calls if method == "addSwimlane"]
            self.assertEqual(created, ["secretary"])
            lanes = {int(lane["id"]): str(lane["name"]) for lane in client.swimlanes}
            self.assertEqual([lanes[int(task["swimlane_id"])] for task in client.tasks], ["secretary"] * 2)

    def test_a_refused_create_is_reported_as_a_create_failure(self) -> None:
        class RefusingBoard(_EmptyWriteKanboard):
            def call(self, method: str, **params: object) -> object:
                if method == "getColumns":
                    return RestoredNonTaskSwimlaneTests.COLUMNS
                if method == "createTask" and params.get("column_id") == 1:
                    return False
                return super().call(method, **params)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            self._seed(data_dir)

            with self.assertRaisesRegex(RestoreError, "could not create restored Product or Issue record"):
                import_normalized_board(data_dir, client=RefusingBoard())


class RestoredCodexLaunchModeTests(unittest.TestCase):
    """A checkpoint older than the TUI-only rule still restores, and restores nothing exec.

    The export is read without failing — a restore must never stop on a field an old board
    legitimately carried — but the mode is not put back on the live card: `TaskWriter.create`
    refuses it, and a card carrying it would be claiming a launch shape the product removed.
    """

    @staticmethod
    def _card(mode: str) -> dict[str, object]:
        return {
            "fields": {},
            "metadata": {"project": "secretary", "task_type": "code", "codex_launch_mode": mode},
        }

    def test_a_legacy_exec_card_restores_with_no_launch_mode(self) -> None:
        self.assertEqual(restore_module._restore_fields(self._card("exec"))["codex_launch_mode"], "")

    def test_the_interactive_mode_round_trips_unchanged(self) -> None:
        self.assertEqual(restore_module._restore_fields(self._card("tui"))["codex_launch_mode"], "tui")

    def _import_legacy_card(self, mode: str):
        """Restore one card carrying `mode` and return the backend it was restored into.

        The normalized views are not the boundary this asks about: `_restore_board_metadata`
        starts from the raw export, so what `saveTaskMetadata` writes is the only place the
        retirement can be proved.
        """
        card = _restore_card()
        card["metadata"]["codex_launch_mode"] = mode

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"
            init_layout(data_dir)
            board = data_dir / "board"
            (board / "cards.json").write_text(json.dumps({"version": 1, "cards": [card]}), encoding="utf-8")
            (board / "cards.ndjson").write_text(json.dumps(card) + "\n", encoding="utf-8")
            (board / "export.json").write_text("{}", encoding="utf-8")

            client = _EmptyWriteKanboard()
            self.assertEqual(import_normalized_board(data_dir, client=client), 1)
            return client

    def test_a_restored_legacy_card_persists_no_retired_mode_in_the_backend(self) -> None:
        """End to end: import an `exec` card and read the metadata the restore wrote."""
        client = self._import_legacy_card("exec")

        stored = client.metadata[12].get("codex_launch_mode")
        self.assertNotEqual(stored, "exec")
        self.assertFalse(stored)
        self.assertIsNone(TaskReader(client).show("secretary-1")["routing"]["codex_launch_mode"])

    def test_a_restored_interactive_card_keeps_its_mode_in_the_backend(self) -> None:
        """Only the retired value is removed; compatible routing data restores untouched."""
        client = self._import_legacy_card("tui")

        self.assertEqual(client.metadata[12].get("codex_launch_mode"), "tui")
        self.assertEqual(TaskReader(client).show("secretary-1")["routing"]["codex_launch_mode"], "tui")

    def test_the_export_and_live_views_of_a_legacy_card_agree(self) -> None:
        """Both sides of the restore comparison read that card as carrying no mode, so a
        legitimately restored card is never reported as a parity mismatch."""
        exported = restore_module._core_from_export(
            {
                "reference": "secretary-1",
                "title": "t",
                "description": "d",
                "column": "Ready",
                "fields": {},
                "metadata": {"project": "secretary", "task_type": "code", "codex_launch_mode": "exec"},
            }
        )
        live = restore_module._core_from_live(
            {
                "ref": "secretary-1",
                "title": "t",
                "description": "d",
                "state": "ready",
                "project": "secretary",
                "type": "code",
                "routing": {"complexity": "standard", "family_preference": "auto", "codex_launch_mode": None},
            }
        )

        self.assertIsNone(exported["routing"]["codex_launch_mode"])
        self.assertEqual(exported["routing"]["codex_launch_mode"], live["routing"]["codex_launch_mode"])


class RestoredOrderParityTests(unittest.TestCase):
    """Паритет расположения сверяет порядок, а не абсолютные номера позиций.

    Kanboard держит позиции плотными среди активных задач, а закрытая задача
    сохраняет устаревшее значение и слот не занимает. Поэтому экспорт живой доски
    приходит с дырами и повторами: на восстановлении 745 карточек экспорт дал
    [.. 13, 15, 16 ..] и пары 126,126 / 141,141 против плотных 1..142 на
    восстановленной доске. Гейт по номерам делал восстановление любой установки с
    закрытыми карточками принципиально непроходимым.
    """

    @staticmethod
    def _card(
        reference: str,
        position: int,
        *,
        column: str = "Issues",
        swimlane: str = "secretary",
        closed: bool = False,
    ) -> dict[str, object]:
        return {
            "reference": reference,
            "column": column,
            "swimlane": swimlane,
            "position": position,
            "closed": closed,
        }

    def test_gaps_and_duplicates_pass_when_the_order_holds(self) -> None:
        cards = [
            self._card("a", 13),
            self._card("b", 15),
            self._card("c", 126),
            self._card("d", 126),
            self._card("e", 141),
        ]
        actual = {
            "a": {"position": 1},
            "b": {"position": 2},
            "c": {"position": 3},
            "d": {"position": 4},
            "e": {"position": 5},
        }

        self.assertFalse(_restored_order_mismatch(cards, actual))

    def test_a_swapped_pair_is_a_mismatch(self) -> None:
        cards = [self._card("a", 1), self._card("b", 2)]
        actual = {"a": {"position": 2}, "b": {"position": 1}}

        self.assertTrue(_restored_order_mismatch(cards, actual))

    def test_closed_cards_are_not_compared(self) -> None:
        cards = [self._card("a", 1), self._card("gone", 2, closed=True), self._card("b", 3)]
        # закрытая карточка получает от Kanboard произвольную позицию — это не расхождение
        actual = {"a": {"position": 1}, "gone": {"position": 99}, "b": {"position": 2}}

        self.assertFalse(_restored_order_mismatch(cards, actual))

    def test_swimlanes_are_ordered_independently(self) -> None:
        cards = [
            self._card("a", 1, swimlane="secretary"),
            self._card("b", 2, swimlane="secretary"),
            self._card("c", 1, swimlane="codegen"),
            self._card("d", 2, swimlane="codegen"),
        ]
        actual = {"a": {"position": 1}, "b": {"position": 2}, "c": {"position": 1}, "d": {"position": 2}}

        self.assertFalse(_restored_order_mismatch(cards, actual))

    def test_a_duplicate_pair_keeps_the_reference_tiebreak(self) -> None:
        """Повтор позиции разводится тем же тайбрейком, каким карточки раскладывались."""
        cards = [self._card("zulu", 126), self._card("alpha", 126)]
        actual = {"alpha": {"position": 1}, "zulu": {"position": 2}}

        self.assertFalse(_restored_order_mismatch(cards, actual))
        self.assertTrue(_restored_order_mismatch(cards, {"alpha": {"position": 2}, "zulu": {"position": 1}}))
