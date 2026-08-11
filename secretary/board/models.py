"""Immutable, backend-neutral values used by the board protocol.

The classes in this module are deliberately values rather than views of a
Kanboard task.  Adapters are responsible for translating their backend rows
before one of these objects crosses the protocol boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias


EntityRef: TypeAlias = str


class EntityKind(StrEnum):
    PRODUCT = "product"
    ISSUE = "issue"
    SPRINT = "sprint"
    CARD = "card"


class ProductState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class IssueState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class SprintState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    STOPPED = "stopped"


class CardState(StrEnum):
    ISSUES = "issues"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    VALIDATE = "validate"
    ASSESSMENT = "assessment"
    BLOCKED = "blocked"
    DONE = "done"


def _non_empty(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class Product:
    ref: EntityRef
    title: str
    state: ProductState = ProductState.ACTIVE
    projects: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        _non_empty(self.ref, "product ref")
        _non_empty(self.title, "product title")

    @property
    def kind(self) -> EntityKind:
        return EntityKind.PRODUCT


@dataclass(frozen=True, slots=True)
class Issue:
    ref: EntityRef
    title: str
    product_ref: EntityRef
    state: IssueState = IssueState.OPEN
    priority: str = ""
    issue_kind: str = ""
    description: str = ""
    close_reason: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.ref, "issue ref")
        _non_empty(self.title, "issue title")
        _non_empty(self.product_ref, "issue product ref")

    @property
    def kind(self) -> EntityKind:
        return EntityKind.ISSUE


@dataclass(frozen=True, slots=True)
class Sprint:
    ref: EntityRef
    goal: str
    state: SprintState = SprintState.OPEN
    product_ref: EntityRef | None = None
    issue_refs: tuple[EntityRef, ...] = ()
    card_refs: tuple[EntityRef, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.ref, "sprint ref")
        _non_empty(self.goal, "sprint goal")

    @property
    def kind(self) -> EntityKind:
        return EntityKind.SPRINT


@dataclass(frozen=True, slots=True)
class Card:
    ref: EntityRef
    title: str
    state: CardState
    sprint_ref: EntityRef | None = None
    issue_refs: tuple[EntityRef, ...] = ()
    product_ref: EntityRef | None = None
    description: str = ""

    def __post_init__(self) -> None:
        _non_empty(self.ref, "card ref")
        _non_empty(self.title, "card title")

    @property
    def kind(self) -> EntityKind:
        return EntityKind.CARD


BoardEntity: TypeAlias = Product | Issue | Sprint | Card


@dataclass(frozen=True, slots=True)
class Actor:
    """The protocol identity that made a board mutation."""

    role: str
    id: str
    head_run_ref: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.role, "actor role")
        _non_empty(self.id, "actor id")


@dataclass(frozen=True, slots=True)
class RelatedRefs:
    """Cross-entity links carried by a mutation and its event."""

    refs: tuple[EntityRef, ...] = ()

    def __post_init__(self) -> None:
        if any(not ref.strip() for ref in self.refs):
            raise ValueError("related refs must not contain empty values")


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    kind: str
    entity_kind: EntityKind
    ref: EntityRef
    actor: Actor
    reason: str
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)

    def __post_init__(self) -> None:
        _non_empty(self.event_id, "event id")
        _non_empty(self.kind, "event kind")
        _non_empty(self.ref, "event ref")
        _non_empty(self.reason, "event reason")
