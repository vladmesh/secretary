"""The board protocol's one lifecycle transition registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from secretary.board.models import (
    BoardEntity, Card, CardState, EntityKind, Issue, IssueState, Product,
    ProductState, Sprint, SprintState,
)


class BoardProtocolError(Exception):
    """A requested board-protocol operation is not valid."""


class InvalidTransition(BoardProtocolError):
    """The lifecycle registry has no edge for the requested move."""


class EventKind(StrEnum):
    PRODUCT_ARCHIVED = "product.archived"
    ISSUE_CLOSED = "issue.closed"
    SPRINT_CLOSED = "sprint.closed"
    SPRINT_STOPPED = "sprint.stopped"
    SPRINT_REOPENED = "sprint.reopened"
    CARD_READY = "card.readied"
    CARD_STARTED = "card.started"
    CARD_SUBMITTED = "card.submitted"
    CARD_ASSESSED = "card.assessed"
    CARD_REWORKED = "card.reworked"
    CARD_RELEASED = "card.released"
    CARD_BLOCKED = "card.blocked"
    CARD_UNBLOCKED = "card.unblocked"
    CARD_RETURNED = "card.returned"


LifecycleState: TypeAlias = ProductState | IssueState | SprintState | CardState
TransitionKey: TypeAlias = tuple[LifecycleState, LifecycleState]


@dataclass(frozen=True, slots=True)
class Transition:
    source: LifecycleState
    target: LifecycleState
    event_kind: EventKind


# This is intentionally a literal registry, not an inferred graph.  Adding an edge
# requires naming the semantic event it emits, which is the phase-one completeness
# check the later event canon will rely on.
TRANSITIONS: dict[EntityKind, dict[TransitionKey, Transition]] = {
    EntityKind.PRODUCT: {
        (ProductState.ACTIVE, ProductState.ARCHIVED): Transition(
            ProductState.ACTIVE, ProductState.ARCHIVED, EventKind.PRODUCT_ARCHIVED
        ),
    },
    EntityKind.ISSUE: {
        (IssueState.OPEN, IssueState.CLOSED): Transition(
            IssueState.OPEN, IssueState.CLOSED, EventKind.ISSUE_CLOSED
        ),
    },
    EntityKind.SPRINT: {
        (SprintState.OPEN, SprintState.CLOSED): Transition(
            SprintState.OPEN, SprintState.CLOSED, EventKind.SPRINT_CLOSED
        ),
        (SprintState.OPEN, SprintState.STOPPED): Transition(
            SprintState.OPEN, SprintState.STOPPED, EventKind.SPRINT_STOPPED
        ),
        (SprintState.STOPPED, SprintState.OPEN): Transition(
            SprintState.STOPPED, SprintState.OPEN, EventKind.SPRINT_REOPENED
        ),
    },
    EntityKind.CARD: {
        (CardState.ISSUES, CardState.READY): Transition(CardState.ISSUES, CardState.READY, EventKind.CARD_READY),
        (CardState.READY, CardState.IN_PROGRESS): Transition(CardState.READY, CardState.IN_PROGRESS, EventKind.CARD_STARTED),
        (CardState.IN_PROGRESS, CardState.VALIDATE): Transition(CardState.IN_PROGRESS, CardState.VALIDATE, EventKind.CARD_SUBMITTED),
        (CardState.VALIDATE, CardState.ASSESSMENT): Transition(CardState.VALIDATE, CardState.ASSESSMENT, EventKind.CARD_ASSESSED),
        (CardState.ASSESSMENT, CardState.IN_PROGRESS): Transition(CardState.ASSESSMENT, CardState.IN_PROGRESS, EventKind.CARD_REWORKED),
        (CardState.ASSESSMENT, CardState.DONE): Transition(CardState.ASSESSMENT, CardState.DONE, EventKind.CARD_RELEASED),
        (CardState.READY, CardState.BLOCKED): Transition(CardState.READY, CardState.BLOCKED, EventKind.CARD_BLOCKED),
        (CardState.IN_PROGRESS, CardState.BLOCKED): Transition(CardState.IN_PROGRESS, CardState.BLOCKED, EventKind.CARD_BLOCKED),
        (CardState.VALIDATE, CardState.BLOCKED): Transition(CardState.VALIDATE, CardState.BLOCKED, EventKind.CARD_BLOCKED),
        (CardState.ASSESSMENT, CardState.BLOCKED): Transition(CardState.ASSESSMENT, CardState.BLOCKED, EventKind.CARD_BLOCKED),
        (CardState.BLOCKED, CardState.READY): Transition(CardState.BLOCKED, CardState.READY, EventKind.CARD_UNBLOCKED),
        (CardState.IN_PROGRESS, CardState.READY): Transition(CardState.IN_PROGRESS, CardState.READY, EventKind.CARD_RETURNED),
        (CardState.VALIDATE, CardState.IN_PROGRESS): Transition(CardState.VALIDATE, CardState.IN_PROGRESS, EventKind.CARD_RETURNED),
    },
}


def lifecycle_state(entity: BoardEntity) -> LifecycleState:
    return entity.state


def transition_for(kind: EntityKind, source: LifecycleState, target: LifecycleState) -> Transition:
    try:
        return TRANSITIONS[kind][(source, target)]
    except KeyError as exc:
        raise InvalidTransition(f"{kind.value} cannot transition from {source.value} to {target.value}") from exc


def transition(entity: BoardEntity, target: LifecycleState) -> tuple[BoardEntity, Transition]:
    """Return the checked successor and the event declaration for its edge."""
    declaration = transition_for(entity.kind, lifecycle_state(entity), target)
    if isinstance(entity, Product) and isinstance(target, ProductState):
        return Product(entity.ref, entity.title, target, entity.projects, entity.description), declaration
    if isinstance(entity, Issue) and isinstance(target, IssueState):
        return Issue(entity.ref, entity.title, entity.product_ref, target, entity.priority, entity.issue_kind, entity.description, entity.close_reason), declaration
    if isinstance(entity, Sprint) and isinstance(target, SprintState):
        return Sprint(entity.ref, entity.goal, target, entity.product_ref, entity.issue_refs, entity.card_refs), declaration
    if isinstance(entity, Card) and isinstance(target, CardState):
        return Card(entity.ref, entity.title, target, entity.sprint_ref, entity.issue_refs, entity.product_ref, entity.description), declaration
    raise InvalidTransition(f"{entity.kind.value} cannot transition to {target!r}")
