"""Backend-neutral host contract for normalized board entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from secretary.board.models import Actor, BoardEntity, EntityKind, EntityRef, Event, RelatedRefs
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
class MutationResult:
    entity: BoardEntity
    event: Event


class BoardHost(Protocol):
    """The protocol seam.  Its values never expose backend row dictionaries."""

    def read(self, kind: EntityKind, ref: EntityRef) -> BoardEntity: ...

    def list(self, kind: EntityKind) -> Sequence[BoardEntity]: ...

    def create(self, operation: Create) -> MutationResult: ...

    def replace(self, operation: Replace) -> MutationResult: ...

    def transition(self, operation: TransitionRequest) -> MutationResult: ...
