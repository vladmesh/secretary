"""Backend-neutral host contract for normalized board entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Protocol, Sequence

from secretary.board.models import Actor, BoardEntity, EntityKind, EntityRef, Event, EventKind, RelatedRefs
from secretary.board.transitions import LifecycleState


@dataclass(frozen=True, slots=True)
class Create:
    entity: BoardEntity
    actor: Actor
    reason: str
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class Replace:
    entity: BoardEntity
    actor: Actor
    reason: str
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class SprintSupplement:
    """The only non-state values a migrated Sprint edge may persist.

    These are domain values, not a Kanboard metadata bag.  The adapter owns
    their storage spelling and rejects combinations that do not belong to an
    edge, so callers cannot tunnel unrelated sprint fields through a lifecycle
    transition.
    """

    observer: str | None = None
    budget_by_type: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if self.observer is not None and (not isinstance(self.observer, str) or not self.observer):
            raise ValueError("Sprint observer must be a non-empty string")
        seen: set[str] = set()
        for name, count in self.budget_by_type:
            if not isinstance(name, str) or not name or name in seen:
                raise ValueError("Sprint budget types must be unique non-empty strings")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("Sprint budget counts must be non-negative integers")
            seen.add(name)

    def event_data(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.observer is not None:
            result["observer"] = self.observer
        if self.budget_by_type:
            result["budget_by_type"] = dict(self.budget_by_type)
        return result


@dataclass(frozen=True, slots=True)
class TransitionRequest:
    kind: EntityKind
    ref: EntityRef
    target: LifecycleState
    actor: Actor
    reason: str
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)
    request_id: str | None = None
    sprint: SprintSupplement | None = None


@dataclass(frozen=True, slots=True)
class MarkerComment:
    """One control-plane comment expressed as a complete typed occurrence.

    ``data`` is deliberately the complete marker payload, including the text
    that will appear on the board.  The adapter renders it only after staging;
    callers never hand it a separately composed comment body.
    """

    ref: EntityRef
    kind: EventKind
    actor: Actor
    reason: str
    data: dict[str, object]
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)
    request_id: str | None = None
    # A command-level admission that is only relevant to a fresh occurrence.
    # The host calls it after resolving request ownership and before staging or
    # issuing the one Kanboard effect, so exact replay never re-runs it.
    fresh_admission: Callable[[], None] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.ref, str) or not self.ref.strip():
            raise ValueError("marker Card ref must be a non-empty string")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("marker reason must be a non-empty string")
        if self.kind not in {EventKind.CARD_REPORTED, EventKind.CARD_VERDICTED, EventKind.CARD_DECIDED}:
            raise ValueError("marker comment kind must be a declared control-plane event kind")
        if not isinstance(self.data, dict):
            raise ValueError("marker comment data must be an object")


@dataclass(frozen=True, slots=True)
class MutationResult:
    entity: BoardEntity
    event: Event
    replayed: bool = False


class BoardHost(Protocol):
    """The protocol seam.  Its values never expose backend row dictionaries."""

    def read(self, kind: EntityKind, ref: EntityRef) -> BoardEntity: ...

    def list(self, kind: EntityKind) -> Sequence[BoardEntity]: ...

    def create(self, operation: Create) -> MutationResult: ...

    def replace(self, operation: Replace) -> MutationResult: ...

    def transition(self, operation: TransitionRequest) -> MutationResult: ...

    def marker_comment(self, operation: MarkerComment) -> MutationResult: ...
