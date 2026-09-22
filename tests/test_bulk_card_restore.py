"""Restore-only card batching, ambiguity recovery and production-shaped scaling."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from secretary.board_transport import BoardTransport
from secretary.task_restore import (
    commit_restored_cards,
    restore_cards_batched,
)
from secretary.tasks import KanboardClient, TaskAudit, TaskError
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
        self.reject_phase = ""
        self.failed_phases: set[str] = set()
        self.post_delay = 0.0
        self.columns = {
            1: "Issues",
            2: "Ready",
            3: "In progress",
            4: "Validate",
            5: "Assessment",
            6: "Blocked",
            7: "Done",
        }
        self.swimlanes: dict[int, str] = {}
        self.phase = "unclassified"
        self.phase_started = time.monotonic()
        self.phase_seconds: dict[str, float] = {}
        self.records: list[tuple[str, tuple[str, ...]]] = []
        self.scramble_closed_groups = False
        self.scrambled_groups: set[tuple[int, int]] = set()
        self.expected_closures = 0
        self.closure_count = 0

    def set_phase(self, phase: str) -> None:
        now = time.monotonic()
        self.phase_seconds[self.phase] = self.phase_seconds.get(self.phase, 0.0) + (now - self.phase_started)
        self.phase = phase
        self.phase_started = now

    def finish_phases(self) -> None:
        self.set_phase("finished")

    def post(self, payload):
        if self.post_delay:
            time.sleep(self.post_delay)
        requests = payload if isinstance(payload, list) else [payload]
        methods = [str(request["method"]) for request in requests]
        self.posts.append(methods)
        self.logical.extend(methods)
        self.records.append((self.phase, tuple(methods)))
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
            if phase and phase == self.reject_phase:
                answers.append(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32000, "message": "rejected"},
                    }
                )
                continue
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
        if method == "getProjectByName":
            return {"id": 7, "name": "Pipeline"}
        if method == "getColumns":
            return [{"id": key, "title": value} for key, value in self.columns.items()]
        if method == "getActiveSwimlanes":
            return [{"id": key, "name": value} for key, value in self.swimlanes.items()]
        if method == "addSwimlane":
            lane_id = max(self.swimlanes, default=3) + 1
            self.swimlanes[lane_id] = str(params["name"])
            return lane_id
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
                    "is_active": 1,
                    "date_creation": "1720000200",
                    "date_modification": "1720000200",
                }
            )
            self.metadata[task_id] = {}
            return task_id
        if method == "updateTask":
            row = next(row for row in self.tasks if int(row["id"]) == int(params["id"]))
            row.update({key: value for key, value in params.items() if key != "id"})
            return True
        if method == "getTaskMetadata":
            return dict(self.metadata[int(params["task_id"])])
        if method == "saveTaskMetadata":
            self.metadata[int(params["task_id"])].update(params["values"])
            return True
        if method == "moveTaskPosition":
            row = next(row for row in self.tasks if int(row["id"]) == int(params["task_id"]))
            siblings = sorted(
                (
                    candidate
                    for candidate in self.tasks
                    if candidate is not row
                    and int(candidate.get("is_active", 1) or 0) != 0
                    and candidate["column_id"] == params["column_id"]
                    and candidate.get("swimlane_id", 0) == params["swimlane_id"]
                ),
                key=lambda candidate: int(candidate.get("position") or 0),
            )
            position = min(max(1, int(params["position"])), len(siblings) + 1)
            row.update(column_id=params["column_id"], swimlane_id=params["swimlane_id"])
            siblings.insert(position - 1, row)
            for index, candidate in enumerate(siblings, 1):
                candidate["position"] = index
            return True
        if method == "getAllComments":
            return []
        if method == "closeTask":
            row = next(row for row in self.tasks if int(row["id"]) == int(params["task_id"]))
            row["is_active"] = 0
            group = (int(row["column_id"]), int(row.get("swimlane_id", 0) or 0))
            siblings = sorted(
                (
                    candidate
                    for candidate in self.tasks
                    if int(candidate.get("is_active", 1) or 0) != 0
                    and (int(candidate["column_id"]), int(candidate.get("swimlane_id", 0) or 0)) == group
                ),
                key=lambda candidate: (int(candidate.get("position") or 0), str(candidate["reference"])),
            )
            for index, candidate in enumerate(siblings, 1):
                candidate["position"] = index
            self.closure_count += 1
            if self.scramble_closed_groups and self.closure_count == self.expected_closures:
                self._scramble_four_recovery_groups()
            return True
        if method == "profile":
            return True
        raise AssertionError(method)

    def _scramble_four_recovery_groups(self) -> None:
        groups: dict[tuple[int, int], list[dict[str, object]]] = {}
        for candidate in self.tasks:
            if int(candidate.get("is_active", 1) or 0) == 0:
                continue
            key = (int(candidate["column_id"]), int(candidate.get("swimlane_id", 0) or 0))
            groups.setdefault(key, []).append(candidate)
        selected = [(key, values) for key, values in sorted(groups.items()) if len(values) >= 4][:4]
        for pattern, (key, values) in enumerate(selected):
            ordered = sorted(values, key=lambda value: int(value.get("position") or 0))
            if pattern == 0:
                ordered.reverse()
            elif pattern == 1:
                ordered[2:] = reversed(ordered[2:])
            elif pattern == 2:
                middle = len(ordered) // 2
                ordered[middle - 1], ordered[middle] = ordered[middle], ordered[middle - 1]
            else:
                ordered.reverse()
                middle = len(ordered) // 2
                ordered[middle - 1], ordered[middle] = ordered[middle], ordered[middle - 1]
            for position, candidate in enumerate(ordered, 1):
                candidate["position"] = position
            self.scrambled_groups.add(key)


def _client(board: _WireRestoreBoard) -> KanboardClient:
    client = KanboardClient(BoardTransport("https://board.invalid", "user", "secret"), Path.cwd())
    client._post = board.post  # type: ignore[method-assign]
    client.set_restore_phase = board.set_phase  # type: ignore[attr-defined]
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


def _production_cards() -> list[dict[str, object]]:
    projection = json.loads(
        (Path(__file__).parent / "fixtures" / "recovery-card-shape-1440.json").read_text(encoding="utf-8")
    )
    rows = projection["rows"]
    type_indexes = {"task": 0, "issue": 0, "product": 0}
    cards: list[dict[str, object]] = []
    for row in rows:
        record_type = str(row["record_type"])
        type_indexes[record_type] += 1
        index = type_indexes[record_type]
        if record_type == "task":
            reference = f"secretary-{index:04d}"
        elif record_type == "issue":
            reference = f"issue:{index:04d}"
        else:
            reference = f"product:p{index:02d}"
        card = _restore_card(
            task_id=int(row["ordinal"]),
            reference=reference,
            title=f"Sanitized {record_type} {index}",
            column=str(row["column"]),
            swimlane=str(row["swimlane"]),
            position=int(row["actual_position"]),
        )
        card["closed"] = bool(row["closed"])
        if record_type == "issue":
            card["fields"].update(task_type="", project="")
            card["metadata"] = {
                "record_type": "issue",
                "issue_product": f"p{(index - 1) % 8 + 1:02d}",
                "issue_kind": "bug",
                "issue_priority": "P1",
                **({"issue_closed_reason": "resolved"} if row["closed"] else {}),
            }
        elif record_type == "product":
            card["fields"].update(task_type="", project="")
            card["metadata"] = {
                "record_type": "product",
                "product_id": f"p{index:02d}",
                "product_projects": '["secretary"]',
            }
        cards.append(card)
    return cards


def _metrics_line(label: str, metrics: dict[str, dict[str, float | int]]) -> str:
    parts = [label]
    for phase in (
        "inventory",
        "create",
        "metadata_state",
        "proof",
        "audit",
        "closure",
        "order",
        "final_parity",
    ):
        values = metrics.get(phase, {"rpc": 0, "posts": 0, "seconds": 0.0})
        parts.append(
            f"{phase}_rpc={values['rpc']} {phase}_posts={values['posts']} "
            f"{phase}_seconds={float(values['seconds']):.3f}"
        )
    return " ".join(parts)


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

    def discard(self, request_id, _event=None):
        self.pending.pop(request_id, None)

    def pending_events(self):
        return list(self.pending.values())

    def events(self, *, kind=None):
        values = list(self.committed.values())
        return [event for event in values if kind is None or event.get("kind") == kind]

    def status(self):
        return {"pending": len(self.pending), "events": len(self.committed)}

    def pending_marker_owners(self, _items):
        return {}

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
                    duplicate = dict(board.tasks[0], id=99)
                    board.tasks.append(duplicate)
                    existing["duplicate-slot"] = duplicate
                audit = TaskAudit(tmp)
                writer = SimpleNamespace(client=_client(board), audit=audit)
                with self.assertRaisesRegex(TaskError, "duplicate reference|different content"):
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
                self.assertEqual(audit.pending_events(), [])

    def test_oversized_card_fails_preflight_with_reference_and_phase(self) -> None:
        for phase in ("create", "metadata/state"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                board = _WireRestoreBoard()
                audit = TaskAudit(tmp)
                writer = SimpleNamespace(client=_client(board), audit=audit)
                card = _mixed_cards(1)[0]
                if phase == "create":
                    card["description"] = "x" * 1_048_576
                else:
                    card["metadata"]["routing_reason"] = "x" * 1_048_576
                with self.assertRaises(TaskError) as raised:
                    restore_cards_batched(
                        writer,
                        [card],
                        board_id=7,
                        columns=self.columns,
                        swimlanes=self.swimlanes,
                        existing={},
                        request_prefix="restore:test:",
                    )
                self.assertEqual(raised.exception.code, "validation")
                self.assertIn("secretary-1", raised.exception.message)
                self.assertIn(f"{phase} payload", raised.exception.message)
                self.assertIn("byte limit", raised.exception.message)
                self.assertEqual(board.posts, [])
                self.assertEqual(audit.pending_events(), [])

    def test_definite_create_rejection_is_not_reported_as_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            board = _WireRestoreBoard()
            board.reject_phase = "create"
            audit = TaskAudit(tmp)
            writer = SimpleNamespace(client=_client(board), audit=audit)
            with self.assertRaises(TaskError) as raised:
                restore_cards_batched(
                    writer,
                    _mixed_cards(1),
                    board_id=7,
                    columns=self.columns,
                    swimlanes=self.swimlanes,
                    existing={},
                    request_prefix="restore:test:",
                )
            self.assertEqual(raised.exception.code, "backend_error")
            self.assertIn("create for secretary-1", raised.exception.message)
            self.assertNotIn("uncertain", raised.exception.message)
            self.assertEqual(audit.pending_events(), [])

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
    def test_real_task_audit_durability_sample(self) -> None:
        cards = _production_cards()[:40]
        board = _WireRestoreBoard()
        for lane in sorted({str(card["swimlane"]) for card in cards if card["swimlane"]}):
            board.swimlanes[max(board.swimlanes, default=3) + 1] = lane
        with tempfile.TemporaryDirectory() as tmp:
            writer = SimpleNamespace(client=_client(board), audit=TaskAudit(tmp))
            started = time.monotonic()
            restore_cards_batched(
                writer,
                cards,
                board_id=7,
                columns=board.columns,
                swimlanes=board.swimlanes,
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
