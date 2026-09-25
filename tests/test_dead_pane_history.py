"""Historical pane-era records stay readable after their producer is removed."""

from __future__ import annotations

import unittest
from unittest import mock

from secretary.board.terminal_taxonomy import normalize_terminal_taxonomy
from secretary.dispatch.production import _budget_event_type
from secretary.dispatch.state import DispatcherRecord
from secretary.tasks import TaskReader
from secretary.webproto.reads import _events_document


class DeadPaneHistoryTests(unittest.TestCase):
    def test_old_record_drops_launch_counters(self) -> None:
        record = DispatcherRecord.from_json(
            {"state": "claimed", "worker_launch_attempts": 3, "review_launch_attempts": 2}
        )
        self.assertEqual(record.state, "claimed")
        self.assertNotIn("worker_launch_attempts", record.to_json())
        self.assertNotIn("review_launch_attempts", record.to_json())

    def test_historical_block_reads_through_card_and_budget_readers(self) -> None:
        old_cause = "pane_" + "never_ready"
        reason = f"[bring-up outcome: class=infrastructure, cause={old_cause}, stage=claim]"
        client = mock.Mock()
        client.call.side_effect = lambda method, **kwargs: (
            [{"comment": reason, "date_creation": 0}] if method == "getAllComments" else {}
        )
        card = TaskReader(client)._show_card(
            {"id": 1, "column_id": 1, "reference": "secretary-old"},
            {1: "Blocked"},
            {},
        )
        self.assertEqual(card["state"], "blocked")
        self.assertEqual(card["comments"][0]["body"], reason)

        event = {
            "record_type": "board.protocol_event",
            "transition": {"source": "in_progress", "target": "blocked"},
            "data": {
                "failure_class": "infrastructure",
                "failure_cause": old_cause,
                "terminal_taxonomy": normalize_terminal_taxonomy(
                    disposition="blocked", blocked_reason="infrastructure"
                ).to_record(),
            },
        }
        self.assertEqual(_budget_event_type(event), "infrastructure_blocked")
        page = mock.Mock(items=(event,), has_more=False)
        page.source.to_json.return_value = {"state": "available"}
        page.next_cursor.encode.return_value = "cursor"
        shown = _events_document("secretary-old", page, now=0, cursor=None)
        self.assertEqual(shown["items"][0]["data"]["failure_cause"], old_cause)
