"""Normalized board protocol foundation.

This package is additive in phase one.  Existing task, sprint, and Product/Issue
command paths retain their current writers until their dedicated migration cards.
"""

from secretary.board.fake import FakeBoardHost
from secretary.board.host import BoardHost, Create, MarkerComment, MutationResult, Replace, SprintSupplement, TransitionRequest
from secretary.board.models import (
    Actor, BoardEntity, Card, CardState, EntityKind, Event, EventKind, Issue, IssueState,
    Product, ProductState, RelatedRefs, Sprint, SprintState,
)
from secretary.board.events import BoardEventCanon, BoardEventPending, MutationEventTransaction
from secretary.board.card_transitions import CARD_TRANSITIONS, CardTransitionForbidden, card_transition
from secretary.board.transitions import BoardProtocolError, EventKind, InvalidTransition, TRANSITIONS, transition


def __getattr__(name: str):
    """Keep legacy adapters out of imports of board's protocol leaves."""
    if name == "KanboardBoardHost":
        from secretary.board.kanboard import KanboardBoardHost
        return KanboardBoardHost
    raise AttributeError(name)

__all__ = [
    "Actor", "BoardEntity", "BoardEventCanon", "BoardEventPending", "BoardHost", "BoardProtocolError", "CARD_TRANSITIONS", "Card", "CardState",
    "CardTransitionForbidden", "Create", "EntityKind", "Event", "EventKind", "FakeBoardHost", "InvalidTransition",
    "Issue", "IssueState", "KanboardBoardHost", "MutationEventTransaction", "MutationResult", "Product", "ProductState", "RelatedRefs",
    "MarkerComment", "Replace", "Sprint", "SprintState", "SprintSupplement", "TRANSITIONS", "TransitionRequest", "card_transition", "transition",
]
