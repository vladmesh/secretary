"""Production rights: the production an `operation` card touches, and the sprint's rule (secretary-1764).

An `operation` card names the production it touches at create (`task create --touches-production
<project>|none`); no other kind carries it. The value is one typed field of the card's extension bag
(`extensions.extra`, docs/BOARD_STORE.md §8.2), so no column and no migration: `touches_production`,
a registered project id or `none`. Only create writes it, and it is read only through
:func:`touches_production`, which treats a malformed value as no value rather than a guess.

A sprint allows its operations some productions (`sprint create --allow-production`, stored as
`sprints.allowed_productions`). The PO service enforces the rule, in one place, before it queues a
dispatcher's input (`secretary.po.service.PoService.submit`): `none` or an allowed production is
queued, anything else is handed to the owner with :func:`refusal_reason`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from secretary.board.extension_bag import EXTENSION_BAG

TOUCHES_PRODUCTION = "touches_production"
#: The value of an operation card that touches no production.
NO_PRODUCTION = "none"
#: The one kind that names its production.
OPERATION_KIND = "operation"
# A registered project id as the registry writes one; anything else is not a production.
_PRODUCTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def touches_production(task: Mapping[str, Any]) -> str | None:
    """The production the card names (`none` included), or None when it carries no well-formed one."""
    extensions = task.get("extensions")
    bag = extensions.get(EXTENSION_BAG) if isinstance(extensions, Mapping) else None
    value = bag.get(TOUCHES_PRODUCTION) if isinstance(bag, Mapping) else None
    value = value.strip() if isinstance(value, str) else ""
    return value if _PRODUCTION.match(value) else None


def create_refusal(kind: str, value: str, registered: Iterable[str] | None) -> str:
    """Why `--touches-production value` cannot go on a new card of `kind`, or `""`.

    `registered` is the installation's project registry, read only for an operation card that names
    a project; `sprint create --allow-production` validates its projects against the same registry.
    """
    if kind != OPERATION_KIND:
        return (
            f"--touches-production names the production an operation card touches; a {kind} card takes none"
            if value
            else ""
        )
    if not value:
        return (
            "an operation card needs --touches-production <project>|none: the production it touches, "
            f"or {NO_PRODUCTION}"
        )
    if value == NO_PRODUCTION:
        return ""
    if not _PRODUCTION.match(value) or value not in set(registered or ()):
        return f"--touches-production names unknown registered project: {value}"
    return ""


#: What a dispatcher input is (`card_facts`'s `input`): the card itself, or the owner's answer to a
#: card handed to the owner, which is not checked again (the owner decided).
CARD_INPUT = "card"
OWNER_ANSWER_INPUT = "owner_answer"
INPUTS = (CARD_INPUT, OWNER_ANSWER_INPUT)
#: The kinds a dispatcher input may be about.
PO_CARD_KINDS = ("decision", OPERATION_KIND)


def card_facts(
    *, card_ref: str, kind: str, touches_production: str | None, sprint_ref: str, input: str = CARD_INPUT
) -> dict[str, Any]:
    """The structured facts a dispatcher input carries beside its text; a decision card names no production."""
    return {
        "card_ref": card_ref,
        "kind": kind,
        "touches_production": touches_production if kind == OPERATION_KIND else None,
        "sprint_ref": sprint_ref,
        "input": input,
    }


def facts_problem(card: Any) -> str:
    """What is missing or malformed in a dispatcher input's card facts, or `""` when nothing is."""
    if not isinstance(card, Mapping):
        return "the input carries no card facts"
    for field in ("card_ref", "sprint_ref"):
        if not isinstance(card.get(field), str) or not card[field].strip():
            return f"the card facts name no {field}"
    if card.get("kind") not in PO_CARD_KINDS:
        return f"the card facts name kind {card.get('kind')!r}, not {' or '.join(PO_CARD_KINDS)}"
    if card.get("input") not in INPUTS:
        return f"the card facts name input {card.get('input')!r}, not {' or '.join(INPUTS)}"
    production = card.get("touches_production")
    if card["kind"] != OPERATION_KIND:
        return "" if production is None else f"a {card['kind']} card names no production"
    # The owner's answer is not checked again, so it needs no production to check.
    if production is None and card["input"] == OWNER_ANSWER_INPUT:
        return ""
    if not isinstance(production, str) or not _PRODUCTION.match(production):
        return f"operation card {card['card_ref']} names no production it touches"
    return ""


def is_allowed(production: str, allowed: Iterable[str]) -> bool:
    """The sprint's rule: `none` always, a production only when the sprint allows it."""
    return production == NO_PRODUCTION or production in set(allowed)


def refusal_reason(production: str, sprint_ref: str, allowed: Iterable[str]) -> str:
    """The handover reason the PO service writes when the rule refuses an operation."""
    return f"operation touches production {production}; sprint {sprint_ref} allows [{', '.join(allowed)}]"


__all__ = [
    "CARD_INPUT",
    "INPUTS",
    "NO_PRODUCTION",
    "OPERATION_KIND",
    "OWNER_ANSWER_INPUT",
    "PO_CARD_KINDS",
    "TOUCHES_PRODUCTION",
    "card_facts",
    "create_refusal",
    "facts_problem",
    "is_allowed",
    "refusal_reason",
    "touches_production",
]
