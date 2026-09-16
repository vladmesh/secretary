from pathlib import Path

root = Path(__file__).resolve().parents[1]


def replace_region(text: str, start: str, end: str, replacement: str) -> str:
    first = text.find(start)
    if first < 0:
        raise RuntimeError(f"start marker not found: {start!r}")
    second = text.find(end, first + len(start))
    if second < 0:
        raise RuntimeError(f"end marker not found: {end!r}")
    return text[:first] + replacement + text[second:]


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
text = text.replace(old, new, 1)
old_decl = "def parse_close_decisions(text: str) -> SprintCloseDecisions:\n"
if old_decl not in text:
    raise RuntimeError("typed close parser declaration missing")
text = text.replace(
    old_decl,
    "def _parse_close_decisions_typed(text: str) -> SprintCloseDecisions:\n",
    1,
)
parser_boundary = "\n\ndef _check_names("
if parser_boundary not in text:
    raise RuntimeError("close parser boundary missing")
text = text.replace(
    parser_boundary,
    "\n\ndef parse_close_decisions(text: str) -> dict[str, list[dict[str, str]]]:\n"
    "    \"\"\"Released parser API: validate with typed values, project the historical dict.\"\"\"\n"
    "    return _parse_close_decisions_typed(text).to_document()\n"
    + parser_boundary,
    1,
)
close.write_text(text, encoding="utf-8")

sprints = root / "src/secretary/sprints.py"
sprint_text = sprints.read_text(encoding="utf-8")
sprint_text = replace_region(
    sprint_text,
    "    def _check_staged_decisions(\n",
    "\n    def _check_completed_close(\n",
    '''    def _check_staged_decisions(
        self,
        document: dict[str, Any],
        decisions: SprintCloseDecisions | None,
    ) -> None:
        """A retry may repeat the staged plan, or amend exactly a recorded conflict."""
        if decisions is None:
            return
        payload = (document.get("event") or {}).get("payload") or {}
        try:
            staged = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError:
            return
        offered = SprintCloseDecisions(
            issues=tuple(sorted(decisions.issues, key=lambda entry: entry.ref)),
            cards=tuple(sorted(decisions.cards, key=lambda entry: entry.ref)),
        )
        current = SprintCloseDecisions(
            issues=tuple(sorted(staged.issues, key=lambda entry: entry.ref)),
            cards=tuple(sorted(staged.cards, key=lambda entry: entry.ref)),
        )
        if offered == current:
            return
        from secretary.sprint_close import ALREADY_CLOSED, ALREADY_MOVED

        confirmation = {"issues": ALREADY_CLOSED, "cards": ALREADY_MOVED}
        conflicts: dict[str, SprintCloseConflict] = {}
        for item in payload.get("conflicts") or []:
            if not isinstance(item, Mapping):
                continue
            try:
                conflict = SprintCloseConflict.from_document(item)
            except ValueError:
                continue
            conflicts[conflict.ref] = conflict
        amended: list[SprintCloseConflict] = []
        for section, current_entries, offered_entries in (
            ("issues", current.issues, offered.issues),
            ("cards", current.cards, offered.cards),
        ):
            if len(offered_entries) != len(current_entries):
                self._refuse_restated_decisions()
            for was, now in zip(current_entries, offered_entries):
                if was == now:
                    continue
                conflict = conflicts.get(now.ref)
                if (
                    conflict is None
                    or was.ref != now.ref
                    or conflict.section != section
                    or now.verdict != confirmation[section]
                    or now.actual != conflict.actual
                ):
                    self._refuse_restated_decisions()
                amended.append(conflict)
        if not amended:
            self._refuse_restated_decisions()
        payload["decisions"] = offered.to_document()
        payload["conflicts"] = [
            item
            for item in (payload.get("conflicts") or [])
            if not (
                isinstance(item, Mapping)
                and any(
                    item.get("ref") == conflict.ref and item.get("section") == conflict.section
                    for conflict in amended
                )
            )
        ]
        self.transactions.save(document)
''',
)
sprint_text = replace_region(
    sprint_text,
    "    def _check_completed_close(\n",
    "\n    def _refuse_restated_decisions",
    '''    def _check_completed_close(
        self,
        committed: dict[str, Any],
        decisions: SprintCloseDecisions | None,
        *,
        reason: str,
        closeout: str,
    ) -> None:
        """A repeat of a finished close carries the same canonical typed plan, or is refused."""
        payload = committed.get("payload") if isinstance(committed.get("payload"), dict) else {}
        try:
            staged = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError as exc:
            raise TaskError("audit_pending", "committed sprint close has invalid decisions", 4) from exc
        if decisions is not None:
            offered = SprintCloseDecisions(
                issues=tuple(sorted(decisions.issues, key=lambda entry: entry.ref)),
                cards=tuple(sorted(decisions.cards, key=lambda entry: entry.ref)),
            )
            current = SprintCloseDecisions(
                issues=tuple(sorted(staged.issues, key=lambda entry: entry.ref)),
                cards=tuple(sorted(staged.cards, key=lambda entry: entry.ref)),
            )
            if offered != current:
                self._refuse_restated_decisions()
        self._check_staged_closeout({"event": committed}, reason=reason, closeout=closeout)
''',
)
sprints.write_text(sprint_text, encoding="utf-8")

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

    def test_parser_keeps_public_dict_and_planner_uses_typed_values(self) -> None:
        document = parse_close_decisions(
            "issues:\n  - ref: issue:1\n    verdict: open\n    reason: later\n"
            "cards:\n  - ref: task:2\n    verdict: drop\n    reason: descoped\n"
        )
        self.assertIsInstance(document, dict)
        parsed = SprintCloseDecisions.from_document(document)
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
