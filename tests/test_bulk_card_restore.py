"""Restore-only card batching, ambiguity recovery and production-shaped scaling."""

from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from secretary.board_transport import BoardTransport
from secretary.task_restore import _restore_inventory, commit_restored_cards, restore_cards_batched
from secretary.tasks import KanboardClient, TaskAudit, TaskError, TaskWriter
from tests.restore_fixtures import _restore_card


class _WireRestoreBoard:
    """A JSON-RPC peer which can apply only a prefix before losing a batch answer."""

    def __init__(self) -> None:
        self.tasks: list[dict[str, object]] = []
        self.metadata: dict[int, dict[str, str]] = {}
        self.posts: list[list[str]] = []
        self.logical: list[str] = []
        self.next_task_id = 1
        self.lose_phase = ""
        self.apply_prefix = 0
        self.malformed_phase = ""
        self.failed_phases: set[str] = set()
        self.post_delay = 0.0

    def post(self, payload):
        if self.post_delay:
            time.sleep(self.post_delay)
        requests = payload if isinstance(payload, list) else [payload]
        methods = [str(request["method"]) for request in requests]
        self.posts.append(methods)
        self.logical.extend(methods)
        phase = ""
        if methods and all(method == "createTask" for method in methods):
            phase = "create"
        elif any(method in {"saveTaskMetadata", "moveTaskPosition"} for method in methods):
            phase = "initialize"
        lose = bool(phase) and phase == self.lose_phase and phase not in self.failed_phases
        limit = self.apply_prefix if lose else len(requests)
        answers = []
        for index, request in enumerate(requests):
            if index >= limit:
                break
            result = self._call(str(request["method"]), request.get("params") or {})
            answers.append({"jsonrpc": "2.0", "id": request["id"], "result": result})
        if lose:
            self.failed_phases.add(phase)
            raise TaskError("backend_unavailable", "lost aggregate reply", 1)
        if phase and phase == self.malformed_phase and phase not in self.failed_phases:
            self.failed_phases.add(phase)
            if answers:
                answers.append(dict(answers[0]))
        return answers if isinstance(payload, list) else answers[0]

    def _call(self, method: str, params: dict[str, object]):
        if method == "getAllTasks":
            active = int(params["status_id"]) == 1
            return [dict(row) for row in self.tasks if (int(row.get("is_active", 1) or 0) != 0) == active]
        if method == "createTask":
            task_id = self.next_task_id
            self.next_task_id += 1
            siblings = [
                row
                for row in self.tasks
                if row["column_id"] == params["column_id"]
                and row.get("swimlane_id", 0) == params.get("swimlane_id", 0)
            ]
            self.tasks.append(
                {
                    "id": task_id,
                    "reference": params["reference"],
                    "title": params["title"],
                    "description": params.get("description", ""),
                    "column_id": params["column_id"],
                    "swimlane_id": params.get("swimlane_id", 0),
                    "position": len(siblings) + 1,
                }
            )
            self.metadata[task_id] = {}
            return task_id
        if method == "getTaskMetadata":
            return dict(self.metadata[int(params["task_id"])])
        if method == "saveTaskMetadata":
            self.metadata[int(params["task_id"])].update(params["values"])
            return True
        if method == "moveTaskPosition":
            row = next(row for row in self.tasks if int(row["id"]) == int(params["task_id"]))
            row.update(
                column_id=params["column_id"],
                swimlane_id=params["swimlane_id"],
                position=params["position"],
            )
            return True
        if method == "profile":
            return True
        raise AssertionError(method)


def _client(board: _WireRestoreBoard) -> KanboardClient:
    client = KanboardClient(BoardTransport("https://board.invalid", "user", "secret"), Path.cwd())
    client._post = board.post  # type: ignore[method-assign]
    return client


def _mixed_cards(count: int = 3) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for index in range(count):
        card = _restore_card(
            task_id=index + 1,
            reference=f"secretary-{index + 1}",
            title=f"Card {index + 1}",
            position=index * 3 + 1,
        )
        issue_row = count == 1_440 and 1_110 <= index < 1_426
        product_row = count == 1_440 and index >= 1_426
        if issue_row or (count != 1_440 and index % 10 == 8):
            card["reference"] = f"issue:{index}"
            card["column"] = "Issues"
            card["fields"].update(task_type="", project="")
            card["metadata"] = {
                "record_type": "issue",
                "issue_product": "secretary",
                "issue_kind": "bug",
                "issue_priority": "P1",
            }
        elif product_row or (count != 1_440 and index % 100 == 99):
            card["reference"] = f"product:p{index}"
            card["column"] = "Issues"
            card["fields"].update(task_type="", project="")
            card["metadata"] = {
                "record_type": "product",
                "product_id": f"p{index}",
                "product_projects": "secretary",
            }
        cards.append(card)
    return cards


class _MemoryAudit:
    def __init__(self) -> None:
        self.pending: dict[str, dict] = {}
        self.committed: dict[str, dict] = {}

    def committed_event(self, request_id):
        return self.committed.get(request_id)

    def pending_event(self, request_id):
        return self.pending.get(request_id)

    def require_claim(self, event, *, kind, reference, identity):
        if event["kind"] != kind or event["ref"] != reference:
            raise AssertionError("claim mismatch")
        if any(event["payload"].get(key) != value for key, value in identity.items()):
            raise AssertionError("claim mismatch")

    def stage(self, request_id, event):
        self.pending[request_id] = event

    def append(self, request_id, event):
        self.committed[request_id] = event
        self.pending.pop(request_id, None)
        return event["event_id"]

    def marker_comment_lock(self, _reference):
        return nullcontext()


class BulkCardRestoreTests(unittest.TestCase):
    columns: ClassVar[dict[int, str]] = {1: "Issues", 2: "Ready"}
    swimlanes: ClassVar[dict[int, str]] = {4: "Secretary"}

    def _restore(self, root: Path, board: _WireRestoreBoard, cards):
        client = _client(board)
        writer = SimpleNamespace(client=client, audit=TaskAudit(root))
        existing = {str(row["reference"]): row for row in board.tasks}
        restore_cards_batched(
            writer,
            cards,
            board_id=7,
            columns=self.columns,
            swimlanes=self.swimlanes,
            existing=existing,
            request_prefix="restore:test:",
        )
        live = {str(row["reference"]): {"id": f"task_kanboard_{row['id']}"} for row in board.tasks}
        commit_restored_cards(writer, cards, live, request_prefix="restore:test:")
        return writer

    def test_lost_create_prefixes_resume_one_row_and_occurrence_per_card(self) -> None:
        for applied in (0, 1, 2):
            with self.subTest(applied=applied), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                board = _WireRestoreBoard()
                board.lose_phase = "create"
                board.apply_prefix = applied
                cards = _mixed_cards()
                if applied < len(cards):
                    with self.assertRaisesRegex(TaskError, "absent reference"):
                        self._restore(root, board, cards)
                    self.assertEqual(TaskWriter(_client(board), data_dir=root).reconcile(), (0, 3))
                writer = self._restore(root, board, cards)
                self.assertEqual(len(board.tasks), len(cards))
                self.assertEqual(len({row["reference"] for row in board.tasks}), len(cards))
                self.assertEqual(len(writer.audit.events(kind="restored_bulk")), len(cards))

                mutations = sum(
                    method in {"createTask", "saveTaskMetadata", "moveTaskPosition"}
                    for method in board.logical
                )
                self._restore(root, board, cards)
                self.assertEqual(
                    mutations,
                    sum(
                        method in {"createTask", "saveTaskMetadata", "moveTaskPosition"}
                        for method in board.logical
                    ),
                )

    def test_lost_and_malformed_initialization_are_proved_then_retried(self) -> None:
        for mode in ("lost", "malformed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                board = _WireRestoreBoard()
                cards = _mixed_cards()
                if mode == "lost":
                    board.lose_phase = "initialize"
                    board.apply_prefix = 2
                else:
                    board.malformed_phase = "initialize"
                try:
                    writer = self._restore(root, board, cards)
                except TaskError:
                    writer = self._restore(root, board, cards)
                self.assertEqual(len(board.tasks), len(cards))
                self.assertEqual(len(writer.audit.events(kind="restored_bulk")), len(cards))
                self.assertEqual(writer.audit.pending_events(), [])

    def test_every_obligation_is_staged_before_the_create_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            board = _WireRestoreBoard()
            audit = TaskAudit(root)
            writer = SimpleNamespace(client=_client(board), audit=audit)
            cards = _mixed_cards()
            original_post = board.post

            def assert_staged(payload):
                requests = payload if isinstance(payload, list) else [payload]
                if requests and requests[0]["method"] == "createTask":
                    self.assertEqual(len(audit.pending_events()), len(cards))
                return original_post(payload)

            board.post = assert_staged
            restore_cards_batched(
                writer,
                cards,
                board_id=7,
                columns=self.columns,
                swimlanes=self.swimlanes,
                existing={},
                request_prefix="restore:test:",
            )

    def test_duplicate_or_wrong_existing_reference_fails_closed_without_mutation(self) -> None:
        cards = _mixed_cards(1)
        for defect in ("duplicate", "content"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                board = _WireRestoreBoard()
                board._call(
                    "createTask",
                    {
                        "reference": "secretary-1",
                        "title": "wrong" if defect == "content" else "Card 1",
                        "description": "body",
                        "column_id": 2,
                        "swimlane_id": 4,
                    },
                )
                existing = {"secretary-1": board.tasks[0]}
                if defect == "duplicate":
                    board.tasks.append(dict(board.tasks[0], id=99))
                writer = SimpleNamespace(client=_client(board), audit=TaskAudit(tmp))
                with self.assertRaisesRegex(TaskError, "duplicate reference|different content"):
                    if defect == "duplicate":
                        _restore_inventory(writer.client, 7)
                    else:
                        restore_cards_batched(
                            writer,
                            cards,
                            board_id=7,
                            columns=self.columns,
                            swimlanes=self.swimlanes,
                            existing=existing,
                            request_prefix="restore:test:",
                        )
                self.assertFalse(
                    any(method in {"saveTaskMetadata", "moveTaskPosition"} for method in board.logical)
                )

    def test_audit_append_failure_replays_without_backend_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            board = _WireRestoreBoard()
            cards = _mixed_cards()
            client = _client(board)
            writer = SimpleNamespace(client=client, audit=TaskAudit(root))
            restore_cards_batched(
                writer,
                cards,
                board_id=7,
                columns=self.columns,
                swimlanes=self.swimlanes,
                existing={},
                request_prefix="restore:test:",
            )
            live = {str(row["reference"]): {"id": f"task_kanboard_{row['id']}"} for row in board.tasks}
            with (
                mock.patch.object(writer.audit, "append", side_effect=OSError("full")),
                self.assertRaisesRegex(TaskError, "audit repair"),
            ):
                commit_restored_cards(writer, cards, live, request_prefix="restore:test:")
            mutations = sum(
                method in {"createTask", "saveTaskMetadata", "moveTaskPosition"} for method in board.logical
            )
            self._restore(root, board, cards)
            self.assertEqual(
                mutations,
                sum(
                    method in {"createTask", "saveTaskMetadata", "moveTaskPosition"}
                    for method in board.logical
                ),
            )


class ProductionShapeCardBenchmark(unittest.TestCase):
    def test_1440_mixed_sparse_cards_remove_interactive_read_amplification(self) -> None:
        cards = _mixed_cards(1_440)
        sparse_positions = [int(card["position"]) for card in cards]
        self.assertEqual(len(set(sparse_positions)), 1_440)
        self.assertGreater(sparse_positions[-1], len(cards) * 2)
        board = _WireRestoreBoard()
        board.post_delay = 0.0001
        with tempfile.TemporaryDirectory():
            writer = SimpleNamespace(client=_client(board), audit=_MemoryAudit())
            started = time.monotonic()
            restore_cards_batched(
                writer,
                cards,
                board_id=7,
                columns={1: "Issues", 2: "Ready"},
                swimlanes={4: "Secretary"},
                existing={},
                request_prefix="restore:benchmark:",
            )
            seconds = time.monotonic() - started
        legacy = _WireRestoreBoard()
        legacy.post_delay = board.post_delay
        legacy_client = _client(legacy)
        legacy_started = time.monotonic()
        for _card in cards:
            for _ in range(7):
                legacy_client.call("profile")
        legacy_seconds = time.monotonic() - legacy_started
        create_posts = sum(
            bool(post) and all(method == "createTask" for method in post) for post in board.posts
        )
        initialize_posts = sum(
            any(method in {"saveTaskMetadata", "moveTaskPosition"} for method in post) for post in board.posts
        )
        proof_posts = sum(
            bool(post) and all(method == "getTaskMetadata" for method in post) for post in board.posts
        )
        self.assertEqual(board.logical.count("createTask"), 1_440)
        self.assertEqual(board.logical.count("saveTaskMetadata"), 1_440)
        self.assertEqual(board.logical.count("moveTaskPosition"), 1_440)
        self.assertLessEqual(create_posts, 8)
        self.assertLessEqual(initialize_posts, 15)
        self.assertLessEqual(proof_posts, 16)
        self.assertNotIn("getAllComments", board.logical)
        self.assertLess(seconds, legacy_seconds)
        print(
            "BULK_CARD_RESTORE durability=excluded simulated_post_latency_ms=0.1 "
            "cards=1440 task=1110 issue=316 product=14 "
            f"legacy_seconds={legacy_seconds:.3f} bulk_seconds={seconds:.3f} "
            "inventory_rpc=4 inventory_posts=4 "
            f"create_rpc=1440 create_posts={create_posts} metadata_state_rpc=2880 "
            f"metadata_state_posts={initialize_posts} audit_proof_rpc=2880 proof_posts={proof_posts} "
            "order_rpc=deferred order_posts=deferred final_parity_rpc=deferred "
            "final_parity_posts=deferred legacy_clean_rpc_min=10080 legacy_clean_posts_min=10080 "
            "batch_count=200 batch_bytes=1048576 positions=sanitized_sparse"
        )

    def test_real_task_audit_durability_sample(self) -> None:
        cards = _mixed_cards(40)
        board = _WireRestoreBoard()
        with tempfile.TemporaryDirectory() as tmp:
            writer = SimpleNamespace(client=_client(board), audit=TaskAudit(tmp))
            started = time.monotonic()
            restore_cards_batched(
                writer,
                cards,
                board_id=7,
                columns={1: "Issues", 2: "Ready"},
                swimlanes={4: "Secretary"},
                existing={},
                request_prefix="restore:durable:",
            )
            seconds = time.monotonic() - started
            self.assertEqual(len(writer.audit.pending_events()), 40)
        print(
            "BULK_CARD_RESTORE_DURABLE_SAMPLE durability=TaskAudit cards=40 "
            f"seconds={seconds:.3f} per_card_ms={seconds / 40 * 1000:.3f}"
        )


if __name__ == "__main__":
    unittest.main()
