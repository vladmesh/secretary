"""What a batched board read costs on the wire, and what a mass status spends it on.

`secretary status --json` took about 110 seconds on a live installation because reading many
sprints paid one HTTP round trip per sprint and per card (issue:0957f14b24d9c5c3cc08). These tests
count posts at the transport, not calls at a fake: a `call_batch` that quietly went back to one
request per call would still satisfy any fake that answers a batch by looping over `call`.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.board_transport import BoardTransport
from secretary.sprints import SPRINT_BOARD_NAME, SprintReader
from secretary.tasks import KanboardClient, TaskError

PIPELINE_BOARD = 7
SPRINT_BOARD = 8
COLUMNS = [
    {"id": 1, "title": "Issues"},
    {"id": 2, "title": "Ready"},
    {"id": 3, "title": "In progress"},
    {"id": 4, "title": "Validate"},
    {"id": 7, "title": "Assessment"},
    {"id": 5, "title": "Blocked"},
    {"id": 6, "title": "Done"},
]


def _client() -> KanboardClient:
    return KanboardClient(BoardTransport("https://board.invalid", "user", "secret"), Path.cwd())


class _Board:
    """A board that answers whole JSON-RPC documents, single and batched.

    Standing in one level lower than the usual fake is the point: the client's own encoding,
    chunking and result ordering run for real, and every post is one round trip on the wire.
    """

    def __init__(self, *, sprints: int, cards: int) -> None:
        self.posts: list[list[str]] = []
        self.sprint_rows = [
            {
                "id": 100 + index,
                "reference": f"sprint:{100 + index}",
                "title": "sprint",
                "description": "",
                "column_id": 1,
                "position": index,
                "swimlane_id": 0,
                "date_creation": "1720000000",
                "date_modification": "1720000000",
            }
            for index in range(sprints)
        ]
        self.card_rows = [
            {
                "id": 200 + index,
                "reference": f"secretary-{200 + index}",
                "title": "card",
                "description": "",
                "column_id": 2,
                "position": index,
                "swimlane_id": 4,
                "date_creation": "1720000000",
                "date_modification": "1720000000",
            }
            for index in range(cards)
        ]
        self.metadata = {
            **{
                row["id"]: {
                    "sprint_goal": "goal",
                    "sprint_status": "open",
                    "sprint_repositories": json.dumps(["secretary"]),
                }
                for row in self.sprint_rows
            },
            **{
                row["id"]: {
                    "project": "secretary",
                    "task_type": "code",
                    "sprint": f"sprint:{100 + (index % max(sprints, 1))}",
                }
                for index, row in enumerate(self.card_rows)
            },
        }

    def post(self, payload):
        requests = payload if isinstance(payload, list) else [payload]
        self.posts.append([str(request["method"]) for request in requests])
        answers = [
            {"jsonrpc": "2.0", "id": request["id"], "result": self._answer(request)} for request in requests
        ]
        return answers if isinstance(payload, list) else answers[0]

    def _answer(self, request):
        method, params = str(request["method"]), request.get("params") or {}
        if method == "getProjectByName":
            return {"id": SPRINT_BOARD if params["name"] == SPRINT_BOARD_NAME else PIPELINE_BOARD}
        if method == "getColumns":
            return COLUMNS
        if method == "getActiveSwimlanes":
            return [{"id": 4, "name": "Secretary"}]
        if method == "getAllTasks":
            if int(params["status_id"]) != 1:
                return []
            return self.sprint_rows if int(params["project_id"]) == SPRINT_BOARD else self.card_rows
        if method == "getTaskMetadata":
            return self.metadata[int(params["task_id"])]
        if method == "getAllComments":
            return []
        raise AssertionError(f"unexpected method {method}")


class CallBatchTransportTests(unittest.TestCase):
    def test_a_batch_is_one_post_and_results_follow_call_order(self) -> None:
        posts = []

        def post(payload):
            posts.append(payload)
            return [
                {"jsonrpc": "2.0", "id": request["id"], "result": request["method"]}
                for request in reversed(payload)
            ]

        with mock.patch.object(KanboardClient, "_post", lambda _self, payload: post(payload)):
            self.assertEqual(
                _client().call_batch([("first", {}), ("second", {"task_id": 2})]),
                ["first", "second"],
            )

        self.assertEqual(len(posts), 1)
        self.assertEqual(
            posts[0],
            [
                {"jsonrpc": "2.0", "id": 0, "method": "first"},
                {"jsonrpc": "2.0", "id": 1, "method": "second", "params": {"task_id": 2}},
            ],
        )

    def test_calls_are_chunked_with_ids_unique_across_chunks(self) -> None:
        seen: list[list[int]] = []

        def post(payload):
            seen.append([request["id"] for request in payload])
            return [{"jsonrpc": "2.0", "id": request["id"], "result": request["id"]} for request in payload]

        with (
            mock.patch("secretary.tasks._BATCH_CHUNK", 2),
            mock.patch.object(KanboardClient, "_post", lambda _self, payload: post(payload)),
        ):
            results = _client().call_batch([("m", {"i": index}) for index in range(5)])

        self.assertEqual(results, [0, 1, 2, 3, 4])
        self.assertEqual(seen, [[0, 1], [2, 3], [4]])

    def test_an_empty_batch_makes_no_request(self) -> None:
        def post(_payload):
            raise AssertionError("posted")

        with mock.patch.object(KanboardClient, "_post", lambda _self, payload: post(payload)):
            self.assertEqual(_client().call_batch([]), [])

    def test_a_missing_or_failed_entry_fails_the_batch_instead_of_leaving_a_hole(self) -> None:
        for answer, description in (
            ([{"jsonrpc": "2.0", "id": 0, "result": "only"}], "one answer for two calls"),
            (
                [
                    {"jsonrpc": "2.0", "id": 0, "result": None},
                    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601}},
                ],
                "an rpc error",
            ),
            ({"jsonrpc": "2.0", "id": 0, "result": "single"}, "a server without batch support"),
        ):
            with self.subTest(answer=description):
                with (
                    mock.patch.object(KanboardClient, "_post", lambda _self, _payload, answer=answer: answer),
                    self.assertRaises(TaskError) as raised,
                ):
                    _client().call_batch([("a", {}), ("b", {})])
                self.assertEqual(raised.exception.code, "backend_error")


class MassStatusTransportTests(unittest.TestCase):
    """The baseline this change exists to hold: posts do not grow with the sprints."""

    def _posts(self, *, sprints: int, cards: int) -> list[list[str]]:
        board = _Board(sprints=sprints, cards=cards)
        with tempfile.TemporaryDirectory() as data_dir:
            with mock.patch.object(KanboardClient, "_post", lambda _self, payload: board.post(payload)):
                statuses = SprintReader(_client(), data_dir=data_dir).statuses()
        self.assertEqual(len(statuses), sprints)
        return board.posts

    def test_one_sprint_and_forty_sprints_cost_the_same_posts(self) -> None:
        one = self._posts(sprints=1, cards=3)
        many = self._posts(sprints=40, cards=120)

        # One row per post: the method it carried, and how many calls travelled in it.
        self.assertEqual(
            [(post[0], len(post)) for post in one],
            [
                ("getProjectByName", 1),  # the sprint board
                ("getAllTasks", 1),  # its rows
                ("getTaskMetadata", 1),  # their metadata, batched into one post
                ("getProjectByName", 1),  # the Pipeline, read once for all sprints together
                ("getColumns", 1),
                ("getActiveSwimlanes", 1),
                ("getAllTasks", 1),
                ("getTaskMetadata", 3),  # every card's metadata, batched into one post
            ],
        )
        self.assertEqual([post[0] for post in many], [post[0] for post in one])
        self.assertEqual(
            [(post[0], len(post)) for post in many],
            [
                ("getProjectByName", 1),
                ("getAllTasks", 1),
                ("getTaskMetadata", 40),
                ("getProjectByName", 1),
                ("getColumns", 1),
                ("getActiveSwimlanes", 1),
                ("getAllTasks", 1),
                ("getTaskMetadata", 120),
            ],
        )
        self.assertTrue(all(len(set(post)) == 1 for post in many))

    def test_a_board_larger_than_one_chunk_splits_into_bounded_posts(self) -> None:
        with mock.patch("secretary.tasks._BATCH_CHUNK", 50):
            posts = self._posts(sprints=1, cards=120)

        metadata_posts = [post for post in posts if post[0] == "getTaskMetadata"]
        self.assertEqual([len(post) for post in metadata_posts], [1, 50, 50, 20])


if __name__ == "__main__":
    unittest.main()
