"""Restore-only comment batching over a real card store, and a durable-audit sample.

Each case restores into a store of its own (`tests/sql_backend_fixtures.py`) and records its
occurrences in that store's audit (`SqlTaskAudit`).

The wire-ambiguity cases the JSON-RPC board had -- a comment batch whose aggregate reply is lost at
the first, a middle or the last wave, or partly rejected after its siblings landed -- are not asked
here: the store restore runs in one enclosing transaction (`import_normalized_board`), so a failed
wave leaves nothing applied. Nor is the transport benchmark of bounded JSON-RPC posts: the store
posts no documents, so the economy it measured has no equivalent.
"""

from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from secretary.board.sql_audit import SqlTaskAudit
from secretary.task_restore import RestoreCommentOccurrence, restore_comments_batched
from secretary.tasks import TaskError
from tests.fakes.tasks import CardSeed
from tests.sql_backend_fixtures import card_store


def _seed(references: list[str]) -> CardSeed:
    """One card per reference, under keys 1..n in reference order, as `_items` numbers them."""
    tasks = [
        {"id": key, "reference": reference, "title": reference, "column_id": 2}
        for key, reference in enumerate(sorted(references), 1)
    ]
    return CardSeed(tasks, {int(task["id"]): {"project": "secretary"} for task in tasks})


def _items(histories: dict[str, list[str]]) -> list[RestoreCommentOccurrence]:
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
                    f"restore:card:{reference}:{index}",
                )
            )
    return result


class BulkCommentRestoreTests(unittest.TestCase):
    def _writer(self, histories: dict[str, list[str]]):
        store = card_store(self, _seed(list(histories)))
        return SimpleNamespace(client=store, audit=SqlTaskAudit(store))

    @staticmethod
    def _comments(writer, key: int) -> list[str]:
        return [str(row["comment"]) for row in writer.client.comments(key)]

    @staticmethod
    def _creates(writer) -> int:
        return sum(method == "createComment" for method, _params in writer.client.calls)

    def test_identical_occurrences_prefix_and_second_import_are_exact(self) -> None:
        target = {"secretary-1": ["same", "middle", "same"]}
        writer = self._writer(target)
        writer.client.add_comment(1, "same", created=1)
        restore_comments_batched(writer, _items(target))
        restore_comments_batched(writer, _items(target))
        self.assertEqual(self._comments(writer, 1), target["secretary-1"])
        events = writer.audit.events("secretary-1", kind="restored_comment")
        self.assertEqual([event["payload"]["restore_occurrence"] for event in events], [0, 0, 1])
        self.assertTrue(all("restore_body" not in event["payload"] for event in events))
        self.assertEqual(self._creates(writer), 2)
        for event in events:
            with self.subTest(event=event["request_id"]):
                self.assertEqual(event["task_id"], "task_postgres_1")
                self.assertEqual(event["backend"]["kind"], "postgres")

    def test_staging_failure_precedes_every_backend_mutation(self) -> None:
        writer = self._writer({"secretary-1": ["record"]})
        with (
            mock.patch.object(writer.audit, "stage", side_effect=OSError("full")),
            self.assertRaises(OSError),
        ):
            restore_comments_batched(writer, _items({"secretary-1": ["record"]}))
        self.assertEqual(self._creates(writer), 0)
        self.assertEqual(self._comments(writer, 1), [])

    def test_non_prefix_destination_history_fails_before_a_write(self) -> None:
        writer = self._writer({"secretary-1": ["expected"]})
        writer.client.add_comment(1, "foreign", created=1)
        with self.assertRaisesRegex(TaskError, "normalized prefix"):
            restore_comments_batched(writer, _items({"secretary-1": ["expected"]}))
        self.assertEqual(self._creates(writer), 0)


class DurableAuditBenchmark(unittest.TestCase):
    @staticmethod
    def _fixture(subjects: int, comments: int, prefix: str) -> dict[str, list[str]]:
        base, extra = divmod(comments, subjects)
        return {
            f"{prefix}{index}": [f"record-{offset}" for offset in range(base + (index < extra))]
            for index in range(subjects)
        }

    def _measure_durable(self, histories: dict[str, list[str]]):
        store = card_store(self, _seed(list(histories)))
        writer = SimpleNamespace(client=store, audit=SqlTaskAudit(store))
        started = time.monotonic()
        restore_comments_batched(writer, _items(histories))
        duration = time.monotonic() - started
        events = len(writer.audit.events(kind="restored_comment"))
        creates = sum(method == "createComment" for method, _params in store.calls)
        return duration, events, creates

    def test_real_audit_cost_per_occurrence(self) -> None:
        histories = self._fixture(40, 120, "secretary-")
        duration, events, creates = self._measure_durable(histories)
        self.assertEqual(events, 120)
        self.assertEqual(creates, 120)
        print(
            "BULK_RESTORE_DURABLE_SAMPLE durability=SqlTaskAudit "
            f"cards=40 comments=120 seconds={duration:.3f} per_occurrence_ms={duration / 120 * 1000:.3f}"
        )

    @unittest.skipUnless(
        os.environ.get("SECRETARY_FULL_BULK_BENCHMARK") == "1",
        "full durable benchmark is an explicit receipt, not a routine shard cost",
    )
    def test_full_production_shape_real_audit(self) -> None:
        cards = self._fixture(1_429, 14_174, "secretary-")
        card_seconds, card_events, _creates = self._measure_durable(cards)
        self.assertEqual(card_events, 14_174)
        print(
            "BULK_RESTORE_DURABLE_FULL durability=SqlTaskAudit "
            f"cards=1429 card_comments=14174 card_seconds={card_seconds:.3f}"
        )


if __name__ == "__main__":
    unittest.main()
