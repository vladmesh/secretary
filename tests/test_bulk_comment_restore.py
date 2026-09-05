"""Restore-only comment batching, including wire ambiguity and production shape."""

from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.board_transport import BoardTransport
from secretary.task_restore import RestoreCommentOccurrence, restore_comments_batched
from secretary.tasks import KanboardClient, TaskAudit, TaskError


class _WireBoard:
    def __init__(self, task_ids: list[int]) -> None:
        self.comments = {task_id: [] for task_id in task_ids}
        self.posts: list[list[str]] = []
        self.logical: list[str] = []
        self.write_posts = 0
        self.lose_write: int | None = None
        self.reject_write: tuple[int, int] | None = None

    def post(self, payload):
        requests = payload if isinstance(payload, list) else [payload]
        methods = [str(request["method"]) for request in requests]
        self.posts.append(methods)
        self.logical.extend(methods)
        writing = bool(methods and methods[0] == "createComment")
        if writing:
            self.write_posts += 1
        answers = []
        for offset, request in enumerate(requests):
            method = request["method"]
            params = request.get("params") or {}
            if method == "getAllComments":
                result = [dict(value) for value in self.comments[int(params["task_id"])]]
            elif method == "createComment":
                if self.reject_write == (self.write_posts, offset):
                    answers.append({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000}})
                    continue
                values = self.comments[int(params["task_id"])]
                values.append({"comment": params["content"], "date_creation": "1720000000"})
                result = len(values)
            else:
                raise AssertionError(method)
            answers.append({"jsonrpc": "2.0", "id": request["id"], "result": result})
        if writing and self.lose_write == self.write_posts:
            raise TaskError("backend_unavailable", "lost aggregate reply", 1)
        return answers if isinstance(payload, list) else answers[0]


def _client(board: _WireBoard) -> KanboardClient:
    client = KanboardClient(BoardTransport("https://board.invalid", "user", "secret"), Path.cwd())
    client._post = board.post  # type: ignore[method-assign]
    return client


def _items(histories: dict[str, list[str]], *, entity: str = "card") -> list[RestoreCommentOccurrence]:
    result = []
    for task_id, reference in enumerate(sorted(histories), 1):
        seen: dict[str, int] = {}
        for index, body in enumerate(histories[reference]):
            occurrence = seen.get(body, 0)
            seen[body] = occurrence + 1
            result.append(
                RestoreCommentOccurrence(
                    reference,
                    task_id,
                    body,
                    occurrence,
                    f"restore:{entity}:{reference}:{index}",
                    entity=entity,
                )
            )
    return result


class BulkCommentRestoreTests(unittest.TestCase):
    def _writer(self, root: Path, board: _WireBoard):
        return SimpleNamespace(client=_client(board), audit=TaskAudit(root))

    def test_identical_occurrences_prefix_and_second_import_are_exact(self) -> None:
        target = {"secretary-1": ["same", "middle", "same"]}
        with tempfile.TemporaryDirectory() as tmp:
            board = _WireBoard([1])
            board.comments[1] = [{"comment": "same", "date_creation": "1"}]
            writer = self._writer(Path(tmp), board)
            restore_comments_batched(writer, _items(target))
            restore_comments_batched(writer, _items(target))
            self.assertEqual([row["comment"] for row in board.comments[1]], target["secretary-1"])
            events = writer.audit.events("secretary-1", kind="restored_comment")
            self.assertEqual([event["payload"]["restore_occurrence"] for event in events], [0, 0, 1])
            self.assertTrue(all("restore_body" not in event["payload"] for event in events))
            self.assertEqual(board.logical.count("createComment"), 2)

    def test_lost_reply_at_first_middle_and_last_wave_resumes_without_duplicates(self) -> None:
        target = {"secretary-1": ["first", "middle", "last"]}
        for lost in (1, 2, 3):
            with self.subTest(lost=lost), tempfile.TemporaryDirectory() as tmp:
                board = _WireBoard([1])
                board.lose_write = lost
                writer = self._writer(Path(tmp), board)
                with self.assertRaisesRegex(TaskError, "uncertain"):
                    restore_comments_batched(writer, _items(target))
                board.lose_write = None
                restore_comments_batched(writer, _items(target))
                self.assertEqual([row["comment"] for row in board.comments[1]], target["secretary-1"])
                self.assertEqual(len(writer.audit.events("secretary-1", kind="restored_comment")), 3)

    def test_partial_rejection_commits_applied_siblings_and_retries_only_the_missing_item(self) -> None:
        target = {"secretary-1": ["a"], "secretary-2": ["b"]}
        with tempfile.TemporaryDirectory() as tmp:
            board = _WireBoard([1, 2])
            board.reject_write = (1, 1)
            writer = self._writer(Path(tmp), board)
            with self.assertRaisesRegex(TaskError, "uncertain"):
                restore_comments_batched(writer, _items(target))
            self.assertEqual([row["comment"] for row in board.comments[1]], ["a"])
            self.assertEqual(board.comments[2], [])
            board.reject_write = None
            restore_comments_batched(writer, _items(target))
            self.assertEqual([row["comment"] for row in board.comments[1]], ["a"])
            self.assertEqual([row["comment"] for row in board.comments[2]], ["b"])
            self.assertEqual(board.logical.count("createComment"), 3)

    def test_append_failure_keeps_a_proven_body_free_pending_event(self) -> None:
        target = {"sprint:1": ["record"]}
        with tempfile.TemporaryDirectory() as tmp:
            board = _WireBoard([1])
            writer = self._writer(Path(tmp), board)
            original = writer.audit.append
            with (
                mock.patch.object(writer.audit, "append", side_effect=OSError("full")),
                self.assertRaisesRegex(TaskError, "audit repair"),
            ):
                restore_comments_batched(writer, _items(target, entity="sprint"))
            pending = writer.audit.pending_event("restore:sprint:sprint:1:0")
            self.assertNotIn("restore_body", pending["payload"])
            writer.audit.append = original
            restore_comments_batched(writer, _items(target, entity="sprint"))
            self.assertEqual([row["comment"] for row in board.comments[1]], ["record"])
            self.assertEqual(len(writer.audit.events("sprint:1", kind="restored_comment")), 1)

    def test_staging_failure_precedes_every_backend_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            board = _WireBoard([1])
            writer = self._writer(Path(tmp), board)
            with (
                mock.patch.object(writer.audit, "stage", side_effect=OSError("full")),
                self.assertRaises(OSError),
            ):
                restore_comments_batched(writer, _items({"secretary-1": ["record"]}))
            self.assertEqual(board.logical.count("createComment"), 0)
            self.assertEqual(board.comments[1], [])

    def test_non_prefix_destination_history_fails_before_a_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            board = _WireBoard([1])
            board.comments[1] = [{"comment": "foreign", "date_creation": "1"}]
            writer = self._writer(Path(tmp), board)
            with self.assertRaisesRegex(TaskError, "normalized prefix"):
                restore_comments_batched(writer, _items({"secretary-1": ["expected"]}))
            self.assertEqual(board.logical.count("createComment"), 0)


class _MemoryAudit:
    def __init__(self) -> None:
        self.pending: dict[str, dict] = {}
        self.committed: dict[str, dict] = {}

    def marker_comment_lock(self, _reference):
        return nullcontext()

    def pending_marker_owner(self, _reference, _body, *, request_id=None):
        return None

    def committed_event(self, request_id):
        return self.committed.get(request_id)

    def pending_event(self, request_id):
        return self.pending.get(request_id)

    def require_claim(self, event, *, kind, reference, identity):
        if event["kind"] != kind or event["ref"] != reference:
            raise AssertionError("claim mismatch")
        for key, value in identity.items():
            if event["payload"].get(key) != value:
                raise AssertionError("claim mismatch")

    def stage(self, request_id, event):
        if request_id not in self.committed:
            self.pending[request_id] = event

    def append(self, request_id, event):
        self.committed.setdefault(request_id, event)
        self.pending.pop(request_id, None)
        return event["event_id"]


class ProductionShapeBenchmark(unittest.TestCase):
    @staticmethod
    def _fixture(subjects: int, comments: int, prefix: str):
        base, extra = divmod(comments, subjects)
        return {
            f"{prefix}{index}": [f"record-{offset}" for offset in range(base + (index < extra))]
            for index in range(subjects)
        }

    def _measure(self, histories: dict[str, list[str]], entity: str):
        board = _WireBoard(list(range(1, len(histories) + 1)))
        writer = SimpleNamespace(client=_client(board), audit=_MemoryAudit())
        started = time.monotonic()
        restore_comments_batched(writer, _items(histories, entity=entity))
        duration = time.monotonic() - started
        return board, duration

    def test_production_shape_transport_scales_by_bounded_waves(self) -> None:
        cards = self._fixture(1_429, 14_174, "secretary-")
        sprints = self._fixture(93, 1_987, "sprint:")
        card_board, card_seconds = self._measure(cards, "card")
        sprint_board, sprint_seconds = self._measure(sprints, "sprint")
        card_posts = len(card_board.posts)
        sprint_posts = len(sprint_board.posts)
        self.assertEqual(card_board.logical.count("createComment"), 14_174)
        self.assertEqual(sprint_board.logical.count("createComment"), 1_987)
        self.assertLess(card_posts, 200)
        self.assertLess(sprint_posts, 60)
        self.assertEqual(card_board.logical.count("getAllComments"), 14_174)
        self.assertEqual(sprint_board.logical.count("getAllComments"), 1_987)
        print(
            "BULK_RESTORE_BENCHMARK "
            f"cards=1429 card_comments=14174 card_posts={card_posts} "
            f"card_logical={len(card_board.logical)} card_seconds={card_seconds:.3f} "
            f"sprints=93 sprint_comments=1987 sprint_posts={sprint_posts} "
            f"sprint_logical={len(sprint_board.logical)} sprint_seconds={sprint_seconds:.3f} "
            "batch_count=200 batch_bytes=1048576 legacy_card_posts=184262..212610"
        )


if __name__ == "__main__":
    unittest.main()
