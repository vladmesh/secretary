"""Immutable, backend-neutral values used by the board protocol.

The classes in this module are deliberately values rather than views of a
Kanboard task.  Adapters are responsible for translating their backend rows
before one of these objects crosses the protocol boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, TypeAlias


EntityRef: TypeAlias = str


class EntityKind(StrEnum):
    PRODUCT = "product"
    ISSUE = "issue"
    SPRINT = "sprint"
    CARD = "card"


class EventKind(StrEnum):
    """Every event kind the normalized board protocol may persist."""

    ENTITY_CREATED = "entity.created"
    ENTITY_UPDATED = "entity.updated"
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
    CARD_MOVED = "card.moved"


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
    if not isinstance(value, str) or not value.strip():
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
        if self.head_run_ref is not None:
            _non_empty(self.head_run_ref, "actor head-run ref")


@dataclass(frozen=True, slots=True)
class RelatedRefs:
    """Cross-entity links carried by a mutation and its event."""

    refs: tuple[EntityRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.refs, tuple):
            raise ValueError("related refs must be a tuple")
        unique: list[EntityRef] = []
        for ref in self.refs:
            _non_empty(ref, "related ref")
            if ref not in unique:
                unique.append(ref)
        object.__setattr__(self, "refs", tuple(unique))


@dataclass(frozen=True, slots=True)
class Event:
    """An immutable, schema-versioned board protocol occurrence.

    The journal representation is deliberately a compact top-level document so
    existing TaskAudit readers can still locate its request id and subject ref.
    ``subject`` remains explicit in the durable shape, rather than requiring a
    future reader to infer it from an event-kind prefix.
    """

    RECORD_TYPE: ClassVar[str] = "board.protocol_event"
    SCHEMA_VERSION: ClassVar[int] = 2

    event_id: str
    kind: EventKind
    entity_kind: EntityKind
    ref: EntityRef
    actor: Actor
    reason: str
    occurred_at: datetime
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)

    def __post_init__(self) -> None:
        _non_empty(self.event_id, "event id")
        if not isinstance(self.kind, EventKind):
            raise ValueError("event kind must be an EventKind")
        if not isinstance(self.entity_kind, EntityKind):
            raise ValueError("event entity kind must be an EntityKind")
        _non_empty(self.ref, "event ref")
        _non_empty(self.reason, "event reason")
        if not isinstance(self.actor, Actor):
            raise ValueError("event actor must be an Actor")
        if not isinstance(self.occurred_at, datetime) or self.occurred_at.tzinfo is None:
            raise ValueError("event occurred_at must be timezone-aware")
        if not isinstance(self.related_refs, RelatedRefs):
            raise ValueError("event related refs must be RelatedRefs")
        # The journal spelling is UTC.  Keep the value canonical too, so an
        # event read back from its own record compares equal to the value that
        # was written, even when its caller supplied another aware timezone.
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))

    def to_record(self, request_id: str) -> dict[str, Any]:
        """Serialize a canonical, deterministic TaskAudit journal record."""
        _non_empty(request_id, "event request id")
        actor: dict[str, str] = {"role": self.actor.role, "id": self.actor.id}
        if self.actor.head_run_ref is not None:
            actor["head_run_ref"] = self.actor.head_run_ref
        return {
            "schema_version": self.SCHEMA_VERSION,
            "record_type": self.RECORD_TYPE,
            "request_id": request_id,
            "event_id": self.event_id,
            "kind": self.kind.value,
            "subject": {"kind": self.entity_kind.value, "ref": self.ref},
            # Keep the old generic query surface useful without making it the
            # protocol representation of the subject.
            "ref": self.ref,
            "occurred_at": _format_time(self.occurred_at),
            "actor": actor,
            "reason": self.reason,
            "related_refs": list(self.related_refs.refs),
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "Event":
        """Read and validate one protocol record, rejecting generic audit rows."""
        if not isinstance(record, dict):
            raise ValueError("board event record must be an object")
        if record.get("record_type") != cls.RECORD_TYPE:
            raise ValueError("record is not a board protocol event")
        if record.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("unsupported board event schema version")
        subject = record.get("subject")
        actor = record.get("actor")
        related = record.get("related_refs")
        if not isinstance(subject, dict):
            raise ValueError("board event subject must be an object")
        if not isinstance(actor, dict):
            raise ValueError("board event actor must be an object")
        if not isinstance(related, list) or not all(isinstance(ref, str) for ref in related):
            raise ValueError("board event related_refs must be a string list")
        if len(set(related)) != len(related):
            raise ValueError("board event related_refs must be deduplicated")
        try:
            event = cls(
                event_id=_string_field(record, "event_id"),
                kind=EventKind(_string_field(record, "kind")),
                entity_kind=EntityKind(_string_field(subject, "kind")),
                ref=_string_field(subject, "ref"),
                actor=Actor(
                    _string_field(actor, "role"), _string_field(actor, "id"),
                    _optional_string_field(actor, "head_run_ref"),
                ),
                reason=_string_field(record, "reason"),
                occurred_at=_parse_time(_string_field(record, "occurred_at")),
                related_refs=RelatedRefs(tuple(related)),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid board event record: {exc}") from None
        legacy_ref = record.get("ref")
        if legacy_ref is not None and legacy_ref != event.ref:
            raise ValueError("board event ref disagrees with subject ref")
        _non_empty(_string_field(record, "request_id"), "event request id")
        return event


def _string_field(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str):
        raise ValueError(f"board event {name} must be a string")
    return value


def _optional_string_field(document: dict[str, Any], name: str) -> str | None:
    value = document.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"board event {name} must be a string")
    return value


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("board event occurred_at must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError("board event occurred_at must be timezone-aware")
    return parsed.astimezone(UTC)
