"""Role-aware Card lifecycle authorization.

This leaf deliberately depends only on normalized board values and lifecycle declarations.
Legacy writers can therefore ask it for authority without importing a Kanboard adapter.
"""

from __future__ import annotations

from typing import TypeAlias

from secretary.board.models import CardState, EntityKind
from secretary.board.transitions import BoardProtocolError, Transition, transition_for


CardTransitionKey: TypeAlias = tuple[CardState, CardState]


class CardTransitionForbidden(BoardProtocolError):
    """A role is not authorized for a requested Card lifecycle edge."""


_CARD_STATES = tuple(CardState)

# This is the compatibility contract observed from TaskWriter's former matrix.  It is the one
# role authority registry; lifecycle declarations remain in ``board.transitions``.
CARD_TRANSITIONS: dict[str, frozenset[CardTransitionKey]] = {
    "po": frozenset((source, target) for source in _CARD_STATES for target in _CARD_STATES if source != target),
    "dispatcher": frozenset({
        (CardState.IN_PROGRESS, CardState.VALIDATE), (CardState.IN_PROGRESS, CardState.BLOCKED),
        (CardState.IN_PROGRESS, CardState.READY), (CardState.VALIDATE, CardState.IN_PROGRESS),
        (CardState.VALIDATE, CardState.BLOCKED), (CardState.VALIDATE, CardState.DONE),
        (CardState.VALIDATE, CardState.ASSESSMENT), (CardState.ASSESSMENT, CardState.IN_PROGRESS),
        (CardState.ASSESSMENT, CardState.DONE), (CardState.ASSESSMENT, CardState.BLOCKED),
    }),
    "observer": frozenset(
        (source, target)
        for source in _CARD_STATES for target in _CARD_STATES
        if source != target and source is not CardState.ASSESSMENT
    ),
    "worker": frozenset(),
    "reviewer": frozenset(),
    "retro": frozenset(),
    "steward": frozenset({
        (CardState.BLOCKED, CardState.READY), (CardState.BLOCKED, CardState.DONE),
        (CardState.IN_PROGRESS, CardState.DONE), (CardState.READY, CardState.BLOCKED),
        (CardState.IN_PROGRESS, CardState.BLOCKED), (CardState.VALIDATE, CardState.BLOCKED),
        (CardState.ASSESSMENT, CardState.BLOCKED),
    }),
}


def card_transition(role: str, source: CardState | str, target: CardState | str) -> Transition:
    """Return the declared lifecycle transition when ``role`` owns the Card edge."""
    try:
        source_state = CardState(source)
        target_state = CardState(target)
    except ValueError as exc:
        raise CardTransitionForbidden(f"{role} cannot transition a Card from {source} to {target}") from exc
    if (source_state, target_state) not in CARD_TRANSITIONS.get(role, frozenset()):
        raise CardTransitionForbidden(
            f"{role} cannot transition a Card from {source_state.value} to {target_state.value}"
        )
    return transition_for(EntityKind.CARD, source_state, target_state)
