"""Normalized board protocol foundation.

This package is additive in phase one.  Existing task, sprint, and Product/Issue
command paths retain their current writers until their dedicated migration cards.
"""

from secretary.board.fake import FakeBoardHost
from secretary.board.host import BoardHost, Create, MutationResult, Replace, TransitionRequest
from secretary.board.kanboard import KanboardBoardHost
from secretary.board.models import (
    Actor, BoardEntity, Card, CardState, EntityKind, Event, Issue, IssueState,
    Product, ProductState, RelatedRefs, Sprint, SprintState,
)
from secretary.board.transitions import BoardProtocolError, EventKind, InvalidTransition, TRANSITIONS, transition

__all__ = [
    "Actor", "BoardEntity", "BoardHost", "BoardProtocolError", "Card", "CardState",
    "Create", "EntityKind", "Event", "EventKind", "FakeBoardHost", "InvalidTransition",
    "Issue", "IssueState", "KanboardBoardHost", "MutationResult", "Product", "ProductState", "RelatedRefs",
    "Replace", "Sprint", "SprintState", "TRANSITIONS", "TransitionRequest", "transition",
]
