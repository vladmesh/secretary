from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from triggered_agents.agents.retro import search_log


class RetroSearchLogTests(unittest.TestCase):
    def test_tail_ignores_non_search_memory_audit_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "search-log.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        {"ts": "2026-08-28T12:00:00+00:00", "action": "authenticate", "outcome": "allowed"},
                        {"ts": "2026-08-28T12:00:01+00:00", "action": "memory_list", "outcome": "allowed"},
                        {
                            "ts": "2026-08-28T12:00:02+00:00",
                            "action": "memory_search",
                            "outcome": "allowed",
                            "k": 5,
                            "hits": [{"id": 1, "score": 0.9}],
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(search_log, "SEARCH_LOG", path):
                entries = search_log.tail("2026-08-28T12:00:00Z", "2026-08-28T12:00:03Z", slack_s=0)

        self.assertEqual([entry["action"] for entry in entries], ["memory_search"])
        self.assertIn("hits=1", search_log.render_markdown(entries))


if __name__ == "__main__":
    unittest.main()
