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
# The confirmations. Neither is a new way to close anything: each one states that somebody
# else already did it, names the fact it is confirming, and is checked against reality before
# the close writes anything. They exist because the alternative to confirming a conflict is
# either a silent agreement with somebody else's verdict or a step nobody can resolve.
ALREADY_CLOSED = "already_closed"
ALREADY_MOVED = "already_moved"
ISSUE_VERDICTS = tuple(sorted(ISSUE_CLOSE_REASONS)) + (KEEP_OPEN, ALREADY_CLOSED)
# What a disposition says about a card that is not done: the work landed, or it will not be
# done under this contract. Both end with the card archived; they differ in the state the
# board records before that.
CARD_DISPOSITIONS = ("done", "drop", ALREADY_MOVED)
# Where a disposition sends the card before the close archives it. `drop` goes through Ready
# and not through Blocked because Ready is the released edge that releases a retained worker,
# and archiving a card that still holds a claim is refused.
DISPOSITION_TARGETS = {"done": "done", "drop": "ready"}
# The states a card somebody else moved may be confirmed in: the ends a disposition of this
# close would have taken it to, and no others, so a confirmation cannot archive a card that is
# still in a working state.
CONFIRMABLE_CARD_STATES = tuple(sorted(set(DISPOSITION_TARGETS.values())))
# Which fact each confirmation has to name in its `actual` field.
CONFIRMATIONS = {
    "issue": (ALREADY_CLOSED, tuple(sorted(ISSUE_CLOSE_REASONS))),
    "card": (ALREADY_MOVED, CONFIRMABLE_CARD_STATES),
}

_SECTIONS = ("issues", "cards")
_ENTRY_FIELDS = {"ref", "verdict", "reason", "actual"}
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
    _check_names(document, "sprint close decisions file")
    unknown = sorted(key for key in document if key not in _SECTIONS)
    if unknown:
        raise TaskError(
            "validation", "sprint close decisions file has unknown section(s): " + ", ".join(map(str, unknown)), 2,
        )
    return {
        "issues": _entries(document.get("issues"), "issue", ISSUE_VERDICTS),
        "cards": _entries(document.get("cards"), "card", CARD_DISPOSITIONS),
    }


def _check_names(mapping: dict[Any, Any], what: str) -> None:
    """A key that is not a name is a refusal, and it is one before anything sorts the keys.

    YAML types its scalars, so `1: x` is an integer key sitting next to string ones.  Naming
    the offending keys is the documented `validation` refusal; sorting them together first
    would be a `TypeError` out of the parser instead, which is not an answer at all.
    """
    unnamed = [key for key in mapping if not isinstance(key, str)]
    if unnamed:
        raise TaskError(
            "validation",
            f"{what} has non-string key(s): " + ", ".join(sorted(repr(key) for key in unnamed)),
            2,
        )


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
        _check_names(entry, f"{kind} decision")
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
        confirmation, facts = CONFIRMATIONS[kind]
        actual = entry.get("actual")
        if verdict == confirmation:
            if not isinstance(actual, str) or actual not in facts:
                raise TaskError(
                    "validation",
                    f"{kind} decision for {reference} confirms what somebody else did, so it must "
                    f"name it in 'actual', one of: " + ", ".join(facts),
                    2,
                )
        elif actual is not None:
            raise TaskError(
                "validation",
                f"{kind} decision for {reference} states 'actual', which only a {confirmation} "
                "decision carries",
                2,
            )
        decided = {"ref": reference, "verdict": verdict, "reason": reason.strip()}
        if verdict == confirmation:
            decided["actual"] = str(actual)
        entries.append(decided)
    return entries


def plan_close_decisions(
    decisions: dict[str, list[dict[str, str]]] | None,
    *,
    declared_issues: list[str],
    remaining: list[str],
    states: dict[str, str],
    issue_states: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Match the decisions against what this sprint actually declared and holds.

    Returns the plan the close stages in its payload: one decision per declared issue and one
    disposition per card that is not done, each with the prose that justifies it.  A missing,
    unknown or contradictory decision is a `validation` refusal, and this runs before the
    transaction is opened, so a refused close writes nothing at all.

    A decision is also matched against the issue it decides.  A closing verdict for an issue
    somebody else has already closed is not this sprint's verdict — the issue carries their
    reason, not the stated one — so it is refused here, before the transaction, and the closer
    either confirms what happened with `already_closed` naming that reason or writes a
    different decision.  The same holds the other way: `already_closed` for an issue that is
    open, or naming a reason the issue does not carry, is refused too.
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
    _check_issue_decisions_match_reality(issues, issue_states or {})
    _check_card_confirmations_match_reality(cards, states)
    return {
        "issues": sorted(issues, key=lambda entry: entry["ref"]),
        "cards": sorted(cards, key=lambda entry: entry["ref"]),
    }


def _check_issue_decisions_match_reality(
    issues: list[dict[str, str]], issue_states: dict[str, dict[str, Any]],
) -> None:
    """Refuse a decision the issue itself contradicts, before the transaction is opened."""
    conflicting: list[str] = []
    for entry in issues:
        state = issue_states.get(entry["ref"])
        if not isinstance(state, dict):
            continue
        closed = bool(state.get("closed"))
        carried = str(state.get("close_reason") or "")
        if entry["verdict"] == ALREADY_CLOSED:
            if not closed:
                raise TaskError(
                    "validation",
                    f"issue {entry['ref']} is open, so there is nothing to confirm; decide it "
                    "with a closing verdict or leave it open",
                    2,
                )
            if entry["actual"] != carried:
                raise TaskError(
                    "validation",
                    f"issue {entry['ref']} is closed as {carried or 'unknown'}, not as "
                    f"{entry['actual']}",
                    2,
                )
        elif closed:
            conflicting.append(f"{entry['ref']} ({carried or 'unknown'})")
    if conflicting:
        raise TaskError(
            "validation",
            "sprint close cannot decide issue(s) somebody else has already closed; confirm each "
            "with already_closed naming the reason it carries: " + ", ".join(sorted(conflicting)),
            2,
        )


def _check_card_confirmations_match_reality(
    cards: list[dict[str, str]], states: dict[str, str],
) -> None:
    """A card confirmation names the state the card actually carries, or it is refused."""
    for entry in cards:
        if entry["verdict"] != ALREADY_MOVED:
            continue
        carried = states.get(entry["ref"], "unknown")
        if entry["actual"] != carried:
            raise TaskError(
                "validation",
                f"card {entry['ref']} is in {carried}, not in {entry['actual']}",
                2,
            )
