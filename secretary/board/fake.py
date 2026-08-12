"""In-memory BoardHost implementation for contract tests."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from secretary.board.events import BoardEventCanon, MutationEventTransaction
from secretary.board.host import Create, MutationResult, Replace, TransitionRequest
from secretary.board.models import BoardEntity, EntityKind, Event, EventKind
from secretary.board.transitions import BoardProtocolError, InvalidTransition, transition


T = TypeVar("T", bound=BoardEntity)


class FakeBoardHost:
    """A deterministic host with no Kanboard, container, or network dependency.

    Passing ``data_dir`` exercises the same durable event canon future adapters
    use.  The no-data-dir mode remains a compact in-memory contract double, but
    still refuses to complete a mutation without creating its event.
    """

    def __init__(
        self, entities: Sequence[BoardEntity] = (), *, data_dir: str | Path | None = None,
    ) -> None:
        self._entities = {(entity.kind, entity.ref): entity for entity in entities}
        self.events: list[Event] = []
        self._requests: dict[str, Event] = {}
        self.canon = BoardEventCanon(data_dir) if data_dir is not None else None

    def read(self, kind: EntityKind, ref: str) -> BoardEntity:
        try:
            return self._entities[(kind, ref)]
        except KeyError as exc:
            raise BoardProtocolError(f"{kind.value} {ref!r} was not found") from exc

    def list(self, kind: EntityKind) -> Sequence[BoardEntity]:
        return tuple(sorted((entity for (entity_kind, _), entity in self._entities.items() if entity_kind == kind), key=lambda entity: entity.ref))

    def create(self, operation: Create) -> MutationResult:
        entity = operation.entity

        def effect() -> BoardEntity:
            key = (entity.kind, entity.ref)
            if key in self._entities:
                raise BoardProtocolError(f"{entity.kind.value} {entity.ref!r} already exists")
            self._entities[key] = entity
            return entity

        return self._mutate(entity, EventKind.ENTITY_CREATED, operation, effect)

    def replace(self, operation: Replace) -> MutationResult:
        entity = operation.entity

        def effect() -> BoardEntity:
            key = (entity.kind, entity.ref)
            try:
                current = self._entities[key]
            except KeyError:
                raise BoardProtocolError(f"{entity.kind.value} {entity.ref!r} was not found") from None
            if current.state != entity.state:
                raise InvalidTransition(
                    f"replace cannot change {entity.kind.value} lifecycle state; use transition"
                )
            self._entities[key] = entity
            return entity

        return self._mutate(entity, EventKind.ENTITY_UPDATED, operation, effect)

    def transition(self, operation: TransitionRequest) -> MutationResult:
        entity = self.read(operation.kind, operation.ref)
        successor, declaration = transition(entity, operation.target)

        def effect() -> BoardEntity:
            # The transition registry was checked before staging.  Re-read at
            # effect time so a concurrent fake mutation cannot be overwritten.
            current = self.read(operation.kind, operation.ref)
            checked, _ = transition(current, operation.target)
            self._entities[(checked.kind, checked.ref)] = checked
            return checked

        return self._mutate(successor, declaration.event_kind, operation, effect)

    def _mutate(
        self,
        entity: T,
        kind: EventKind,
        operation: Create | Replace | TransitionRequest,
        effect: Callable[[], T],
    ) -> MutationResult:
        event, request_id = self._event(entity, kind, operation)
        if self.canon is not None:
            result = MutationEventTransaction(
                self.canon, request_id=request_id, event=event,
            ).execute(effect, confirm=lambda: self.read(entity.kind, entity.ref))
        else:
            # Keep the lightweight test double deterministic while using the
            # same request-id ownership rule as its durable counterpart.
            existing = self._requests.get(request_id)
            if existing is not None:
                if existing != event:
                    raise ValueError("request id belongs to another operation or payload")
                result = self.read(entity.kind, entity.ref)
            else:
                result = effect()
                self._requests[request_id] = event
        if event not in self.events:
            self.events.append(event)
        return MutationResult(result, event)

    def _event(
        self,
        entity: BoardEntity,
        kind: EventKind,
        operation: Create | Replace | TransitionRequest,
    ) -> tuple[Event, str]:
        request_id = operation.request_id
        if request_id and self.canon is not None:
            existing = self.canon.event(request_id)
            if existing is not None:
                self._require_same_operation(existing, entity, kind, operation)
                return existing, request_id
        if request_id and request_id in self._requests:
            existing = self._requests[request_id]
            self._require_same_operation(existing, entity, kind, operation)
            return existing, request_id
        # A caller that declares no request id still gets its own idempotency key, instead
        # of borrowing one from the generated event id.
        request_id = request_id or f"fake-request-{uuid.uuid4().hex}"
        event = Event(
            self._event_id(request_id, entity, kind, operation), kind, entity.kind, entity.ref,
            operation.actor, operation.reason, datetime.now(UTC).replace(microsecond=0),
            operation.related_refs,
        )
        return event, request_id

    @staticmethod
    def _event_id(
        request_id: str,
        entity: BoardEntity,
        kind: EventKind,
        operation: Create | Replace | TransitionRequest,
    ) -> str:
        """Derive a collision-resistant id from what the occurrence durably is.

        A per-host counter cannot do this: the durable journal outlives the host, so a
        recreated host would restart at one and publish a second `board-event-1` for an
        unrelated occurrence.  Digesting the request id together with the operation keeps a
        genuine same-request replay on its original id and separates everything else.
        """
        payload = json.dumps(
            {
                "request_id": request_id,
                "kind": kind.value,
                "entity_kind": entity.kind.value,
                "ref": entity.ref,
                "actor": [operation.actor.role, operation.actor.id, operation.actor.head_run_ref],
                "reason": operation.reason,
                "related_refs": list(operation.related_refs.refs),
            },
            sort_keys=True, separators=(",", ":"),
        )
        return "board-event-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _require_same_operation(
        existing: Event,
        entity: BoardEntity,
        kind: EventKind,
        operation: Create | Replace | TransitionRequest,
    ) -> None:
        if (
            existing.kind is not kind
            or existing.entity_kind is not entity.kind
            or existing.ref != entity.ref
            or existing.actor != operation.actor
            or existing.reason != operation.reason
            or existing.related_refs != operation.related_refs
        ):
            raise ValueError("request id belongs to another operation or payload")
