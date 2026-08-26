"""Normalized board protocol foundation.

This package is additive in phase one.  Existing task, sprint, and Product/Issue
command paths retain their current writers until their dedicated migration cards.
"""

from secretary.board.card_transitions import (
    CARD_TRANSITIONS,
    CardTransitionForbidden,
    card_transition,
)
from secretary.board.events import (
    BoardEventCanon,
    BoardEventPending,
    MutationEventTransaction,
)
from secretary.board.fake import FakeBoardHost
from secretary.board.host import (
    BoardHost,
    Create,
    MarkerComment,
    MutationResult,
    Replace,
    SprintSupplement,
    TransitionRequest,
)
from secretary.board.models import (
    Actor,
    BoardEntity,
    Card,
    CardState,
    EntityKind,
    Event,
    EventKind,
    Issue,
    IssueState,
    Product,
    ProductState,
    RelatedRefs,
    Sprint,
    SprintState,
)
from secretary.board.transitions import (
    TRANSITIONS,
    BoardProtocolError,
    EventKind,
    InvalidTransition,
    transition,
)


def __getattr__(name: str):
    """Keep legacy adapters out of imports of board's protocol leaves."""
    if name == "KanboardBoardHost":
        from secretary.board.kanboard import KanboardBoardHost

        return KanboardBoardHost
    raise AttributeError(name)


__all__ = [
    "CARD_TRANSITIONS",
    "TRANSITIONS",
    "Actor",
    "BoardEntity",
    "BoardEventCanon",
    "BoardEventPending",
    "BoardHost",
    "BoardProtocolError",
    "Card",
    "CardState",
    "CardTransitionForbidden",
    "Create",
    "EntityKind",
    "Event",
    "EventKind",
    "FakeBoardHost",
    "InvalidTransition",
    "Issue",
    "IssueState",
    "KanboardBoardHost",
    "MarkerComment",
    "MutationEventTransaction",
    "MutationResult",
    "Product",
    "ProductState",
    "RelatedRefs",
    "Replace",
    "Sprint",
    "SprintState",
    "SprintSupplement",
    "TransitionRequest",
    "card_transition",
    "transition",
]
