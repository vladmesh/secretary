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


@dataclass(frozen=True, slots=True)
class Replace:
    entity: BoardEntity
    actor: Actor
    reason: str
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)


@dataclass(frozen=True, slots=True)
class TransitionRequest:
    kind: EntityKind
    ref: EntityRef
    target: LifecycleState
    actor: Actor
    reason: str
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)


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
