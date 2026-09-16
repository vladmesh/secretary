from pathlib import Path

root = Path(__file__).resolve().parents[1]

typed = root / "src/secretary/board/sprint_close.py"
typed_text = typed.read_text(encoding="utf-8")
marker = 'CloseSection = Literal["issues", "cards"]\n\n\n'
if marker not in typed_text:
    raise RuntimeError("typed close section marker missing")
typed_text = typed_text.replace(
    marker,
    marker
    + 'class SprintCloseDocumentError(ValueError):\n'
    + '    """A durable Sprint-close document does not match its closed domain shape."""\n\n\n',
    1,
)
typed_text = typed_text.replace("raise ValueError(", "raise SprintCloseDocumentError(")
typed.write_text(typed_text, encoding="utf-8")

close = root / "src/secretary/sprint_close.py"
text = close.read_text(encoding="utf-8")
old = 'return "\n".join(lines).rstrip() + "\n"'
new = 'return "\\n".join(lines).rstrip() + "\\n"'
if old not in text:
    raise RuntimeError("generated closeout join marker missing")
close.write_text(text.replace(old, new, 1), encoding="utf-8")

test = root / "tests/test_sprint_close_model.py"
test.write_text(r'''from __future__ import annotations

import unittest

from secretary.board.roles import Role
from secretary.board.sprint_close import (
    SprintCloseConflict,
    SprintCloseDecision,
    SprintCloseDecisions,
    SprintCloseIntent,
    SprintCloseSnapshot,
    SprintCloseTargets,
    SprintCloseoutPlan,
)
from secretary.sprint_close import parse_close_decisions, plan_close_decisions


class SprintCloseModelTests(unittest.TestCase):
    def test_decisions_round_trip_released_document(self) -> None:
        document = {
            "issues": [{"ref": "issue:7", "verdict": "open", "reason": "carry it"}],
            "cards": [
                {
                    "ref": "task:9",
                    "verdict": "already_moved",
                    "reason": "raced",
                    "actual": "ready",
                }
            ],
        }
        typed = SprintCloseDecisions.from_document(document)
        self.assertEqual(typed.to_document(), document)
        self.assertEqual(typed.issues[0].ref, "issue:7")
        self.assertEqual(typed.cards[0].actual, "ready")

    def test_intent_targets_conflict_and_closeout_round_trip(self) -> None:
        intent = SprintCloseIntent(Role.PO, "owner", "sprint:5")
        self.assertEqual(SprintCloseIntent.from_document(intent.to_document()), intent)
        targets = SprintCloseTargets(
            archive=("task:1",),
            remaining=("task:2",),
            remaining_states=(("task:2", "doing"),),
        )
        self.assertEqual(SprintCloseTargets.from_document(targets.to_document()), targets)
        conflict = SprintCloseConflict("cards", "task:2", "drop", "ready")
        self.assertEqual(SprintCloseConflict.from_document(conflict.to_document()), conflict)
        closeout = SprintCloseoutPlan("closeouts/x.md", "body", "sha")
        self.assertEqual(SprintCloseoutPlan.from_document(closeout.to_document()), closeout)
        self.assertEqual(closeout.mark_written("abc").to_result()["commit"], "abc")

    def test_snapshot_preserves_legacy_absence_of_reservations(self) -> None:
        old = SprintCloseSnapshot.from_document({"ref": "sprint:1", "goal": "g", "issues": []})
        current = SprintCloseSnapshot.from_document(
            {"ref": "sprint:2", "goal": "g", "issues": [], "reservations": []}
        )
        self.assertFalse(old.has_reservations)
        self.assertTrue(current.has_reservations)

    def test_parser_and_planner_return_typed_values(self) -> None:
        parsed = parse_close_decisions(
            "issues:\n  - ref: issue:1\n    verdict: open\n    reason: later\n"
            "cards:\n  - ref: task:2\n    verdict: drop\n    reason: descoped\n"
        )
        self.assertIsInstance(parsed, SprintCloseDecisions)
        planned = plan_close_decisions(
            parsed,
            declared_issues=["issue:1"],
            remaining=["task:2"],
            states={"task:2": "doing"},
        )
        self.assertIsInstance(planned, SprintCloseDecisions)
        self.assertEqual(planned.cards, (SprintCloseDecision("task:2", "drop", "descoped"),))


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")
