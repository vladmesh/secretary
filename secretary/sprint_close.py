"""The decisions a sprint close carries, and the one file they arrive in.

A close used to be silent about two things it was in fact deciding: the issues the sprint
declared, and the cards it left behind in a working state.  Neither is derivable.  A sprint
can close with its Definition of Done only partly reached, so "the sprint closed" is not
"the issue is done", and a card left in Ready under a closed contract is not a disposition
either.  Both are stated by the closing operator, in prose, before anything is written.

The verdicts arrive as one file rather than as a row of flags: the reasons are prose, and
prose is written before the command runs, not typed into a shell.
"""

from __future__ import annotations

from typing import Any

import yaml

from secretary.product_issues import ISSUE_CLOSE_REASONS
from secretary.tasks import TaskError


# The verdict that leaves an issue open. The four closing verdicts are the released close
# reasons and this card does not add to them.
KEEP_OPEN = "open"
ISSUE_VERDICTS = tuple(sorted(ISSUE_CLOSE_REASONS)) + (KEEP_OPEN,)
# What a disposition says about a card that is not done: the work landed, or it will not be
# done under this contract. Both end with the card archived; they differ in the state the
# board records before that.
CARD_DISPOSITIONS = ("done", "drop")
# Where a disposition sends the card before the close archives it. `drop` goes through Ready
# and not through Blocked because Ready is the released edge that releases a retained worker,
# and archiving a card that still holds a claim is refused.
DISPOSITION_TARGETS = {"done": "done", "drop": "ready"}

_SECTIONS = ("issues", "cards")
_ENTRY_FIELDS = {"ref", "verdict", "reason"}
_SHAPE = (
    "sprint close decisions file must be a mapping with the optional keys 'issues' and 'cards', "
    "each a list of {ref, verdict, reason} entries"
)


def parse_close_decisions(text: str) -> dict[str, list[dict[str, str]]]:
    """Read the decisions file into its normalized shape, or refuse it.

    Every refusal here is a `validation` refusal raised before the close is entered, so a
    malformed file never reaches a backend write.
    """
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        raise TaskError("validation", "sprint close decisions file is not valid YAML", 2) from None
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise TaskError("validation", _SHAPE, 2)
    unknown = sorted(key for key in document if key not in _SECTIONS)
    if unknown:
        raise TaskError(
            "validation", "sprint close decisions file has unknown section(s): " + ", ".join(map(str, unknown)), 2,
        )
    return {
        "issues": _entries(document.get("issues"), "issue", ISSUE_VERDICTS),
        "cards": _entries(document.get("cards"), "card", CARD_DISPOSITIONS),
    }


def _entries(raw: Any, kind: str, verdicts: tuple[str, ...]) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TaskError("validation", _SHAPE, 2)
    seen: set[str] = set()
    entries: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise TaskError("validation", _SHAPE, 2)
        extra = sorted(key for key in entry if key not in _ENTRY_FIELDS)
        if extra:
            raise TaskError(
                "validation", f"{kind} decision has unknown field(s): " + ", ".join(map(str, extra)), 2,
            )
        reference = entry.get("ref")
        if not isinstance(reference, str) or not reference.strip():
            raise TaskError("validation", f"every {kind} decision needs a ref", 2)
        reference = reference.strip()
        if reference in seen:
            raise TaskError("validation", f"{kind} {reference} has more than one decision", 2)
        seen.add(reference)
        verdict = entry.get("verdict")
        if not isinstance(verdict, str) or verdict not in verdicts:
            raise TaskError(
                "validation",
                f"{kind} decision for {reference} needs a verdict, one of: " + ", ".join(verdicts),
                2,
            )
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise TaskError(
                "validation", f"{kind} decision for {reference} requires a non-empty reason", 2,
            )
        entries.append({"ref": reference, "verdict": verdict, "reason": reason.strip()})
    return entries


def plan_close_decisions(
    decisions: dict[str, list[dict[str, str]]] | None,
    *,
    declared_issues: list[str],
    remaining: list[str],
    states: dict[str, str],
) -> dict[str, list[dict[str, str]]]:
    """Match the decisions against what this sprint actually declared and holds.

    Returns the plan the close stages in its payload: one decision per declared issue and one
    disposition per card that is not done, each with the prose that justifies it.  A missing,
    unknown or contradictory decision is a `validation` refusal, and this runs before the
    transaction is opened, so a refused close writes nothing at all.
    """
    parsed = decisions or {"issues": [], "cards": []}
    issues = list(parsed.get("issues") or [])
    cards = list(parsed.get("cards") or [])
    declared = list(declared_issues)
    unknown_issues = sorted({entry["ref"] for entry in issues} - set(declared))
    if unknown_issues:
        raise TaskError(
            "validation",
            "sprint close was given a decision for issue(s) the sprint did not declare: "
            + ", ".join(unknown_issues),
            2,
        )
    missing_issues = [reference for reference in declared if reference not in {entry["ref"] for entry in issues}]
    if missing_issues:
        raise TaskError(
            "validation",
            "sprint close needs an explicit decision for every declared issue; none was given for: "
            + ", ".join(missing_issues),
            2,
        )
    unknown_cards = sorted({entry["ref"] for entry in cards} - set(remaining))
    if unknown_cards:
        raise TaskError(
            "validation",
            "sprint close was given a disposition for card(s) that are not open work of this sprint: "
            + ", ".join(unknown_cards),
            2,
        )
    undisposed = [reference for reference in remaining if reference not in {entry["ref"] for entry in cards}]
    if undisposed:
        raise TaskError(
            "validation",
            "sprint close refuses to leave cards on a closed contract; dispose of each of them: "
            + ", ".join(f"{reference} ({states.get(reference, 'unknown')})" for reference in undisposed),
            2,
        )
    return {
        "issues": sorted(issues, key=lambda entry: entry["ref"]),
        "cards": sorted(cards, key=lambda entry: entry["ref"]),
    }
