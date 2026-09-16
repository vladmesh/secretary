from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from secretary.board.models import SprintState
from secretary.board.sprint_read import (
    BUDGET_EVENT_TYPES,
    SprintReadMetadata,
    SprintResume,
)


class SprintReadModelTests(unittest.TestCase):
    def test_compound_metadata_is_typed_once_and_renders_the_released_shape(self) -> None:
        resume = {
            "selected_step": " next ",
            "selected_why": " because ",
            "rejected_alternatives": " other ",
            "current_task": " task:1 ",
            "dod_state": " pending ",
            "next_safe_step": " continue ",
            "recorded_at": "2026-09-16T12:00:00Z",
        }
        meta = {
            "sprint_repositories": json.dumps([" /repo/a ", "/repo/a", "/repo/b", ""]),
            "sprint_status": "closed",
            "sprint_budget": json.dumps({"by_type": {"red_review": "2", "blocked": -4, "hotfix": "bad"}}),
            "sprint_budget_uncharged": json.dumps({"infrastructure_blocked": "3"}),
            "sprint_resume": json.dumps(resume),
            "sprint_source_audit": json.dumps(
                {
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-02T00:00:00Z",
                    "board": "old",
                    "ignored": "not part of the contract",
                }
            ),
        }

        read = SprintReadMetadata.from_legacy(meta, thresholds={"signal": 2, "hard": 5})

        self.assertEqual(read.repositories, ("/repo/a", "/repo/b"))
        self.assertIs(read.state, SprintState.CLOSED)
        self.assertEqual(read.budget.total, 2)
        self.assertEqual(
            read.budget.by_type,
            {event: (2 if event == "red_review" else 0) for event in BUDGET_EVENT_TYPES},
        )
        self.assertEqual(read.budget.uncharged, {"infrastructure_blocked": 3})
        self.assertTrue(read.budget.signal_reached)
        self.assertFalse(read.budget.hard_reached)
        self.assertEqual(
            read.resume.to_document() if read.resume else None,
            {
                **{key: value.strip() for key, value in resume.items() if key != "recorded_at"},
                "recorded_at": resume["recorded_at"],
            },
        )
        self.assertEqual(
            read.source_audit.to_document() if read.source_audit else None,
            {
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "board": "old",
            },
        )

    def test_invalid_legacy_values_keep_the_old_safe_defaults(self) -> None:
        read = SprintReadMetadata.from_legacy(
            {
                "sprint_repositories": "not-json",
                "sprint_status": "future-state",
                "sprint_budget": "{",
                "sprint_budget_uncharged": "{",
                "sprint_resume": json.dumps({"selected_step": "only one field"}),
                "sprint_source_audit": json.dumps({"other": "ignored"}),
            }
        )

        self.assertEqual(read.repositories, ())
        self.assertIs(read.state, SprintState.OPEN)
        self.assertEqual(read.budget.total, 0)
        self.assertEqual(read.budget.thresholds, {"signal": 3, "hard": 6})
        self.assertFalse(read.budget.signal_reached)
        self.assertFalse(read.budget.hard_reached)
        self.assertIsNone(read.resume)
        self.assertIsNone(read.source_audit)

    def test_required_resume_reports_the_same_missing_field_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "resume entry is missing required fields"):
            SprintResume.from_legacy({"selected_step": "x"}, required=True)
        with self.assertRaisesRegex(ValueError, "resume entry must be a JSON object"):
            SprintResume.from_legacy("not-json", required=True)

    def test_board_importer_no_longer_imports_private_sprint_helpers(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src" / "secretary" / "board" / "import_board.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        private = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "secretary.sprints"
            for alias in node.names
            if alias.name.startswith("_")
        ]
        self.assertEqual(private, [])


if __name__ == "__main__":
    unittest.main()
