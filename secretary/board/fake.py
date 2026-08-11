"""In-memory BoardHost implementation for contract tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from itertools import count

from secretary.board.host import Create, MutationResult, Replace, TransitionRequest
from secretary.board.models import BoardEntity, EntityKind, Event
from secretary.board.transitions import BoardProtocolError, transition


class FakeBoardHost:
    """A deterministic host with no Kanboard, container, or network dependency."""

    def __init__(self, entities: Sequence[BoardEntity] = ()) -> None:
        self._entities = {(entity.kind, entity.ref): entity for entity in entities}
        self.events: list[Event] = []
        self._ids = count(1)

    def read(self, kind: EntityKind, ref: str) -> BoardEntity:
        try:
            return self._entities[(kind, ref)]
        except KeyError as exc:
            raise BoardProtocolError(f"{kind.value} {ref!r} was not found") from exc

    def list(self, kind: EntityKind) -> Sequence[BoardEntity]:
        return tuple(sorted((entity for (entity_kind, _), entity in self._entities.items() if entity_kind == kind), key=lambda entity: entity.ref))

    def create(self, operation: Create) -> MutationResult:
        key = (operation.entity.kind, operation.entity.ref)
        if key in self._entities:
            raise BoardProtocolError(f"{operation.entity.kind.value} {operation.entity.ref!r} already exists")
        self._entities[key] = operation.entity
        return self._result(operation.entity, "entity.created", operation.actor, operation.reason, operation.related_refs)

    def replace(self, operation: Replace) -> MutationResult:
        key = (operation.entity.kind, operation.entity.ref)
        if key not in self._entities:
            raise BoardProtocolError(f"{operation.entity.kind.value} {operation.entity.ref!r} was not found")
        self._entities[key] = operation.entity
        return self._result(operation.entity, "entity.updated", operation.actor, operation.reason, operation.related_refs)

    def transition(self, operation: TransitionRequest) -> MutationResult:
        entity = self.read(operation.kind, operation.ref)
        successor, declaration = transition(entity, operation.target)
        self._entities[(successor.kind, successor.ref)] = successor
        return self._result(successor, declaration.event_kind.value, operation.actor, operation.reason, operation.related_refs)

    def _result(self, entity: BoardEntity, kind: str, actor, reason: str, related_refs) -> MutationResult:
        event = Event(f"board-event-{next(self._ids)}", kind, entity.kind, entity.ref, actor, reason, related_refs)
        self.events.append(event)
        return MutationResult(entity, event)
