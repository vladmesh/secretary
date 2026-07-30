"""Whole-board export path (secretary-637).

The checkpoint writer regenerates the board on every dispatcher tick under `tick_lock`, so the
export has to cost one call, not one per card. These pin the batched transport and the single-pass
`ops.export_cards`: same per-card surface as `show_card`, two batched RPCs per task, no per-card
round trip.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from triggered_agents.agents.pipeline import ops
from triggered_agents.runtime import kanboard
from triggered_agents.runtime.kanboard import KanboardError, call_batch


class CallBatchTests(unittest.TestCase):
    def test_results_follow_call_order_not_response_order(self):
        def fake_post(payload, _label):
            return [
                {"jsonrpc": "2.0", "id": request["id"], "result": request["method"]}
                for request in reversed(payload)
            ]

        with mock.patch.object(kanboard, "_post", side_effect=fake_post):
            self.assertEqual(
                call_batch([("first", {}), ("second", {"task_id": 2})]),
                ["first", "second"],
            )

    def test_calls_are_chunked_and_ids_stay_unique_across_chunks(self):
        seen = []

        def fake_post(payload, _label):
            seen.append([request["id"] for request in payload])
            return [{"id": request["id"], "result": request["id"]} for request in payload]

        with mock.patch.object(kanboard, "_BATCH_CHUNK", 2), \
             mock.patch.object(kanboard, "_post", side_effect=fake_post):
            results = call_batch([("m", {"i": index}) for index in range(5)])

        self.assertEqual(results, [0, 1, 2, 3, 4])
        self.assertEqual(seen, [[0, 1], [2, 3], [4]])

    def test_empty_call_list_makes_no_request(self):
        with mock.patch.object(kanboard, "_post", side_effect=AssertionError("posted")):
            self.assertEqual(call_batch([]), [])

    def test_rpc_error_in_any_entry_fails_the_batch(self):
        def fake_post(payload, _label):
            entries = [{"id": request["id"], "result": None} for request in payload]
            entries[-1] = {"id": payload[-1]["id"], "error": {"code": -32601}}
            return entries

        with mock.patch.object(kanboard, "_post", side_effect=fake_post):
            with self.assertRaisesRegex(KanboardError, "rpc error"):
                call_batch([("a", {}), ("b", {})])

    def test_missing_response_fails_instead_of_returning_a_hole(self):
        def fake_post(payload, _label):
            return [{"id": payload[0]["id"], "result": "only"}]

        with mock.patch.object(kanboard, "_post", side_effect=fake_post):
            with self.assertRaisesRegex(KanboardError, "no response for b"):
                call_batch([("a", {}), ("b", {})])

    def test_server_without_batch_support_fails_loudly(self):
        with mock.patch.object(kanboard, "_post", return_value={"id": 0, "result": "single"}):
            with self.assertRaisesRegex(KanboardError, "single object"):
                call_batch([("a", {}), ("b", {})])


TASKS = [
    {
        "id": 7,
        "reference": "secretary-637",
        "title": "Checkpoint writer",
        "description": "spec body",
        "column_id": 3,
        "swimlane_id": 1,
        "position": 2,
        "date_moved": 1783635890,
    },
    {
        "id": 8,
        "reference": "secretary-638",
        "title": "Second",
        "description": "",
        "column_id": 3,
        "swimlane_id": 1,
        "position": 3,
        "date_moved": 1783635990,
    },
]


def _fake_board(calls):
    """Stub the board reads `export_cards` makes; `calls` collects the batched requests."""
    def fake_call(method, **params):
        if method == "getAllProjects":
            return [{"id": 2, "name": ops.model.BOARD_NAME}]
        if method == "getColumns":
            return [{"id": 3, "title": "In progress"}]
        if method == "getActiveSwimlanes":
            return [{"id": 1, "name": "secretary"}]
        if method == "getAllTasks":
            if params.get("status_id") == 1:
                return [TASKS[0], TASKS[0]]
            if params.get("status_id") == 0:
                return [TASKS[0], TASKS[1]]
            return []
        raise AssertionError(f"unbatched call {method} {params}")

    def fake_batch(requests):
        requests = list(requests)
        calls.append(requests)
        results = []
        for method, params in requests:
            if method == "getTaskMetadata":
                results.append({"project": "secretary", "task_id_echo": str(params["task_id"])})
            else:
                results.append([{"date_creation": "10", "comment": "[po]\nbody"}])
        return results

    return fake_call, fake_batch


class ExportCardsTests(unittest.TestCase):
    def test_one_batched_request_covers_every_card(self):
        calls = []
        fake_call, fake_batch = _fake_board(calls)
        with mock.patch.object(ops, "call", side_effect=fake_call) as board_call, \
            mock.patch.object(ops, "call_batch", side_effect=fake_batch):
            cards = ops.export_cards()

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            [
                ("getTaskMetadata", {"task_id": 7}),
                ("getAllComments", {"task_id": 7}),
                ("getTaskMetadata", {"task_id": 8}),
                ("getAllComments", {"task_id": 8}),
            ],
        )
        self.assertEqual([card["reference"] for card in cards], ["secretary-637", "secretary-638"])
        self.assertIn(mock.call("getAllTasks", project_id=2, status_id=1), board_call.call_args_list)
        self.assertIn(mock.call("getAllTasks", project_id=2, status_id=0), board_call.call_args_list)
        self.assertNotIn(mock.call("getAllTasks", project_id=2, status_id=2), board_call.call_args_list)

    def test_card_carries_both_the_list_and_the_show_surface(self):
        calls = []
        fake_call, fake_batch = _fake_board(calls)
        with mock.patch.object(ops, "call", side_effect=fake_call), \
             mock.patch.object(ops, "call_batch", side_effect=fake_batch):
            card = ops.export_cards()[0]

        # list view
        self.assertEqual(card["column"], "In progress")
        self.assertEqual(card["swimlane"], "secretary")
        self.assertEqual(card["position"], 2)
        self.assertEqual(card["date_moved"], 1783635890)
        self.assertEqual(card["project"], "secretary")
        # show view
        self.assertEqual(card["description"], "spec body")
        self.assertEqual(card["metadata"]["task_id_echo"], "7")
        self.assertEqual(card["comments"], [{"ts": "10", "text": "[po]\nbody"}])

    def test_metadata_and_comments_stay_paired_with_their_card(self):
        calls = []
        fake_call, fake_batch = _fake_board(calls)
        with mock.patch.object(ops, "call", side_effect=fake_call), \
             mock.patch.object(ops, "call_batch", side_effect=fake_batch):
            cards = ops.export_cards()

        # An off-by-one in the batch unpacking would hand card 8 the metadata of card 7.
        for card in cards:
            self.assertEqual(card["metadata"]["task_id_echo"], str(card["id"]))

    def test_output_is_json_serialisable_for_the_cli(self):
        calls = []
        fake_call, fake_batch = _fake_board(calls)
        with mock.patch.object(ops, "call", side_effect=fake_call), \
             mock.patch.object(ops, "call_batch", side_effect=fake_batch):
            cards = ops.export_cards()

        self.assertEqual(json.loads(json.dumps(cards)), cards)

    def test_closed_non_done_card_does_not_satisfy_dependency(self):
        def fake_call(method, **params):
            if method == "getColumns":
                return [{"id": 2, "title": "Ready"}, {"id": 6, "title": "Done"}]
            raise AssertionError(method)

        with mock.patch.object(ops, "call", side_effect=fake_call):
            self.assertFalse(ops._is_done({"column_id": 2, "is_active": 0}, 1))
            self.assertTrue(ops._is_done({"column_id": 6, "is_active": 0}, 1))


if __name__ == "__main__":
    unittest.main()
