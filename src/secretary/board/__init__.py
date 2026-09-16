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
    AnalyticsOutcomeConflict,
    AttemptOutcomeOccurrence,
    AttemptUsageOccurrence,
    BoardEventCanon,
    BoardEventPending,
    MutationEventTransaction,
)
from secretary.board.fake import FakeBoardHost
from secretary.board.host import (
    BoardHost,
    Create,
    DescriptionAppend,
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
    IssueCloseReason,
    IssueKind,
    IssuePriority,
    IssueState,
    Product,
    ProductState,
    RelatedRefs,
    Sprint,
    SprintState,
)
from secretary.board.roles import Role
from secretary.board.task_routing import (
    BlockClassification,
    FamilyPreference,
    RoutingPhase,
    TaskComplexity,
    TaskDecision,
    TaskMetadata,
    TaskRouting,
    TaskType,
)
from secretary.board.transitions import (
    TRANSITIONS,
    BoardProtocolError,
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
    "AnalyticsOutcomeConflict",
    "AttemptOutcomeOccurrence",
    "AttemptUsageOccurrence",
    "BoardEntity",
    "BoardEventCanon",
    "BoardEventPending",
    "BoardHost",
    "BoardProtocolError",
    "BlockClassification",
    "Card",
    "CardState",
    "CardTransitionForbidden",
    "Create",
    "DescriptionAppend",
    "EntityKind",
    "Event",
    "EventKind",
    "FakeBoardHost",
    "FamilyPreference",
    "InvalidTransition",
    "Issue",
    "IssueCloseReason",
    "IssueKind",
    "IssuePriority",
    "IssueState",
    "KanboardBoardHost",
    "MarkerComment",
    "MutationEventTransaction",
    "MutationResult",
    "Product",
    "ProductState",
    "RelatedRefs",
    "Replace",
    "Role",
    "RoutingPhase",
    "Sprint",
    "SprintState",
    "SprintSupplement",
    "TaskComplexity",
    "TaskDecision",
    "TaskMetadata",
    "TaskRouting",
    "TaskType",
    "TransitionRequest",
    "card_transition",
    "transition",
]
