"""The Codex reset's record in a real board audit: `requests`, its constraints, and the two reads.

The unit suite (`tests/test_web_codex_reset.py`) holds the operation over an in-memory audit; this
one proves the record it writes is one the PostgreSQL `requests` table accepts -- no entity, a
generic record-only row -- and that `/history` and `GET /api/history/<request_id>` read it back.
The provider is a recording fake: nothing here reaches the network.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from secretary.tasks import task_audit_for
from secretary.webproto.command_reads import CommandReadLayer
from secretary.webproto.provider_ops import CODEX_RESET_KIND, NO_CREDIT, ProviderOperationLayer
from tests.sql_backend_fixtures import CardStoreCase


class FakeUsage:
    """The provider layer as the operation calls it: a live reading, a consume and an invalidation."""

    def __init__(self, *, applicable: int, available: int = 1) -> None:
        self.applicable = applicable
        self.available = available
        self.consumed: list[str] = []
        self.invalidated = 0

    def codex_live(self) -> dict[str, Any]:
        return {"id": "codex", "reset_credits": {"available": self.available, "applicable": self.applicable}}

    def consume_codex_reset(self, redeem_request_id: str) -> tuple[str, str | None]:
        self.consumed.append(redeem_request_id)
        return "reset", None

    def invalidate(self) -> None:
        self.invalidated += 1


class CodexResetAuditTests(CardStoreCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.board = self.card_store()

    def layer(self, usage: FakeUsage) -> ProviderOperationLayer:
        return ProviderOperationLayer(self.tmp / "instance", usage=usage, board_client=self.board)

    def reads(self) -> CommandReadLayer:
        return CommandReadLayer(self.tmp / "instance", data_dir=self.tmp / "data", board_client=self.board)

    def test_a_reset_is_one_committed_row_that_both_reads_find(self) -> None:
        usage = FakeUsage(applicable=1)
        first = self.layer(usage).codex_reset_limit(request_id="web-codex-reset-live", actor="web")
        again = self.layer(usage).codex_reset_limit(request_id="web-codex-reset-live", actor="web")
        self.assertEqual((first["outcome"], again["replayed"]), ("reset", True))
        self.assertEqual(usage.consumed, ["web-codex-reset-live"])
        self.assertEqual(usage.invalidated, 1)
        audit = task_audit_for(self.board)
        self.assertIsNone(audit.pending_event("web-codex-reset-live"))
        self.assertEqual(audit.committed_event("web-codex-reset-live")["kind"], CODEX_RESET_KIND)

        found = self.reads().command_request("web-codex-reset-live")["operation"]
        self.assertEqual(found["state"], "committed")
        self.assertEqual(found["action"], CODEX_RESET_KIND)
        self.assertEqual(found["actor"], {"id": "web", "role": "po"})
        self.assertEqual(found["result"]["outcome"], "reset")
        self.assertEqual(found["entity"], {"ref": "", "kind": None})
        items = self.reads().command_history()["commands"]["items"]
        self.assertIn("web-codex-reset-live", [item["request_id"] for item in items])

    def test_a_refusal_is_recorded_and_sends_nothing(self) -> None:
        usage = FakeUsage(applicable=0, available=0)
        answer = self.layer(usage).codex_reset_limit(request_id="web-codex-reset-refused", actor="web")
        self.assertEqual((answer["outcome"], answer["reason"]), ("refused", NO_CREDIT))
        self.assertEqual(usage.consumed, [])
        found = self.reads().command_request("web-codex-reset-refused")["operation"]
        self.assertEqual((found["state"], found["result"]["outcome"]), ("committed", "refused"))


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
