"""Kanboard implementation of normalized BoardHost reads and Card transitions."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from secretary.board.card_transitions import CardTransitionForbidden, card_transition
from secretary.board.events import BoardEventCanon, MutationEventTransaction
from secretary.board.host import Create, MutationResult, Replace, TransitionRequest
from secretary.board.models import (
    BoardEntity, Card, CardState, EntityKind, Event, EventKind, Issue, IssueState, Product,
    ProductState, RelatedRefs, Sprint, SprintState,
)
from secretary.board.transitions import BoardProtocolError
from secretary.product_issues import ProductIssueStore
from secretary.sprints import SprintReader
from secretary.tasks import KanboardClient, TaskReader, _positive_int, _target_column_id, project_card_by_reference


class KanboardBoardHost:
    """Translate the current Kanboard-backed readers and Card state edges at the BoardHost seam.

    Card ``transition`` is the migrated mutation: it is the one authority for a
    Card state change and owns its typed event transaction.  The remaining
    mutations stay unavailable until their own migration can preserve each
    legacy writer's durable retry and audit semantics, so a caller cannot bypass
    the contract accidentally.
    """

    def __init__(
        self, client: KanboardClient, *, data_dir: str | None = None, instance: str | None = None,
        audit: Any | None = None,
    ) -> None:
        self.client = client
        self.data_dir = data_dir
        self.instance = instance
        self.canon = BoardEventCanon(data_dir, audit=audit) if data_dir is not None else None

    def read(self, kind: EntityKind, ref: str) -> BoardEntity:
        if kind is EntityKind.CARD:
            return _card(TaskReader(self.client).show(ref))
        if kind is EntityKind.SPRINT:
            return _sprint(SprintReader(self.client, data_dir=self.data_dir).show(ref))
        store = self._product_issues()
        if kind is EntityKind.PRODUCT:
            return _product(store.show_product(_product_id(ref)))
        if kind is EntityKind.ISSUE:
            return _issue(store.show_issue(ref))
        raise BoardProtocolError(f"unknown entity kind {kind!r}")

    def list(self, kind: EntityKind) -> Sequence[BoardEntity]:
        if kind is EntityKind.CARD:
            return tuple(
                _card(record)
                for record in TaskReader(self.client).list()
                if record.get("record_type") not in {"issue", "product"}
            )
        if kind is EntityKind.SPRINT:
            return tuple(_sprint(record) for record in SprintReader(self.client, data_dir=self.data_dir).list(create=False))
        store = self._product_issues()
        if kind is EntityKind.PRODUCT:
            return tuple(_product(record) for record in store.list_products())
        if kind is EntityKind.ISSUE:
            return tuple(_issue(record) for record in store.list_issues(include_closed=True))
        raise BoardProtocolError(f"unknown entity kind {kind!r}")

    def create(self, operation: Create) -> MutationResult:
        self._migration_pending("create", operation.entity.kind)

    def replace(self, operation: Replace) -> MutationResult:
        self._migration_pending("replace", operation.entity.kind)

    def transition(
        self, operation: TransitionRequest, *, finish: Callable[[Card], None] | None = None,
    ) -> MutationResult:
        """Move one Card along a declared, role-authorized lifecycle edge.

        The order is the contract: validate the live Card and the caller's authority for its
        edge, stage the exact event this occurrence will publish, perform the single column
        operation, confirm it on the board, then commit that event.  Only a failure before the
        column operation is issued owes the journal nothing; once it has returned, every later
        failure - the confirming read, ``finish``, the commit - owes the caller a repair and
        keeps the pending event that names it.  ``MutationEventTransaction`` is what enforces
        that, for every path through this method.

        ``finish`` is the caller's own idempotent board work for this same edge - the card
        fields the state change resets or fills in.  The adapter still maps exactly one column
        operation; it only guarantees that a caller's remaining writes are inside this
        transaction, so an incomplete one keeps the pending event rather than reporting a clean
        journal over a half-written card.  It runs once the target is proven, never on a replay
        of an already committed occurrence, and never before the column effect.
        """
        if operation.kind is not EntityKind.CARD:
            self._migration_pending("transition", operation.kind)
        if self.canon is None:
            raise BoardProtocolError("Card transitions require a configured data directory")
        if not isinstance(operation.target, CardState):
            raise BoardProtocolError("Card transitions require a CardState target")
        request_id = operation.request_id or f"card-transition-{uuid.uuid4().hex}"
        existing = self.canon.event(request_id)
        if existing is not None:
            if (
                existing.entity_kind is not EntityKind.CARD
                or existing.ref != operation.ref
                or existing.actor != operation.actor
                or existing.reason != operation.reason
                or existing.target_state != operation.target.value
            ):
                raise ValueError("request id belongs to another operation or payload")
            event = existing
        else:
            event = None
        current = self.read(EntityKind.CARD, operation.ref)
        if not isinstance(current, Card):
            raise BoardProtocolError("Card transition resolved a non-Card entity")
        # A committed occurrence is historical evidence, not a lease on the
        # Card's current state.  Return the current normalized Card without a
        # second backend write even when later work has moved it onward.
        if event is not None and self.canon.committed(request_id) is not None:
            return MutationResult(current, event)
        if event is None:
            declaration = card_transition(operation.actor.role, current.state, operation.target)
            related = operation.related_refs
            if current.sprint_ref and current.sprint_ref not in related.refs:
                # The sprint the Card belongs to is a related ref of every one of its
                # transitions, whether or not the caller happened to pass it.
                related = RelatedRefs(related.refs + (current.sprint_ref,))
            event = self._event(current, declaration.event_kind, operation, related, request_id)

        def confirm() -> Card:
            entity = self.read(EntityKind.CARD, operation.ref)
            if not isinstance(entity, Card) or entity.state is not operation.target:
                raise BoardProtocolError("Card transition is not proven on the Kanboard board")
            return entity

        def effect() -> None:
            # Everything here is before the column operation is issued, which is
            # what makes a failure in it a discard rather than a recovery
            # obligation.  The re-read is one of those: staging does not grant a
            # stale caller permission to overwrite a newer Card state.  The
            # confirming read back is deliberately *not* here - a read that
            # fails after moveTaskPosition returned is not evidence the move did
            # not happen, so the transaction runs it outside the discard window.
            entity = self.read(EntityKind.CARD, operation.ref)
            if not isinstance(entity, Card):
                raise BoardProtocolError("Card transition resolved a non-Card entity")
            card_transition(operation.actor.role, entity.state, operation.target)
            self._move_card(entity, operation.target)

        entity = MutationEventTransaction(
            self.canon, request_id=request_id, event=event,
        ).execute(effect, confirm=confirm, finish=finish)
        return MutationResult(entity, event)

    def recover_transition(self, request_id: str) -> MutationResult:
        """Commit a pending Card event only after its exact target is live.

        This deliberately never calls ``moveTaskPosition``.  A pending event is
        evidence of an attempted effect, not authority to attempt it again.
        """
        if self.canon is None:
            raise BoardProtocolError("Card transition recovery requires a configured data directory")
        event = self.canon.event(request_id)
        if event is None:
            raise BoardProtocolError("pending Card transition was not found")
        if event.entity_kind is not EntityKind.CARD or event.target_state is None:
            raise BoardProtocolError("pending event is not a recoverable Card transition")
        try:
            target = CardState(event.target_state)
        except ValueError as exc:
            raise BoardProtocolError("pending Card transition has an invalid target") from exc
        entity = self.read(EntityKind.CARD, event.ref)
        if not isinstance(entity, Card) or entity.state is not target:
            raise BoardProtocolError("pending Card transition is not proven on the Kanboard board")
        self.canon.commit(request_id, event)
        return MutationResult(entity, event)

    def _move_card(self, card: Card, target: CardState) -> None:
        reader = TaskReader(self.client)
        board_id, columns, _ = reader._board()
        column_id = _target_column_id(columns, target.value)
        if column_id is None:
            raise BoardProtocolError("Kanboard board schema is invalid")
        raw = project_card_by_reference(self.client, board_id, card.ref)
        if not isinstance(raw, dict):
            raise BoardProtocolError("Card was not found")
        swimlane_id = _positive_int(raw.get("swimlane_id")) or 0
        task_id = _positive_int(raw.get("id"))
        if task_id is None:
            raise BoardProtocolError("Kanboard returned an invalid Card")
        if not self.client.call(
            "moveTaskPosition", project_id=board_id, task_id=task_id,
            column_id=column_id, position=1, swimlane_id=swimlane_id,
        ):
            raise BoardProtocolError("Kanboard rejected the Card transition")

    @staticmethod
    def _event(
        card: Card, kind: EventKind, operation: TransitionRequest, related: RelatedRefs,
        request_id: str,
    ) -> Event:
        """Build the one complete occurrence this request publishes.

        The id is derived from the request and its exact payload, so a retry of the same request
        names the same occurrence and a different payload can never borrow it.
        """
        payload = json.dumps({
            "request_id": request_id, "kind": kind.value, "ref": card.ref,
            "actor": [operation.actor.role, operation.actor.id, operation.actor.head_run_ref],
            "reason": operation.reason, "related_refs": list(related.refs),
            "source": card.state.value, "target": operation.target.value,
        }, sort_keys=True, separators=(",", ":"))
        return Event(
            "board-event-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32],
            kind, EntityKind.CARD, card.ref, operation.actor, operation.reason,
            datetime.now(UTC), related, card.state.value, operation.target.value,
        )

    def _product_issues(self) -> ProductIssueStore:
        if self.data_dir is None or self.instance is None:
            raise BoardProtocolError("Product/Issue reads require the configured data and instance directories")
        return ProductIssueStore(self.client, data_dir=self.data_dir, instance=self.instance)

    @staticmethod
    def _migration_pending(operation: str, kind: EntityKind) -> None:
        raise BoardProtocolError(
            f"KanboardBoardHost {operation} for {kind.value} is not migrated; "
            "use the established writer until its migration card preserves its audit semantics"
        )


def _product_id(ref: str) -> str:
    prefix = "product:"
    if not ref.startswith(prefix) or not ref[len(prefix):]:
        raise BoardProtocolError(f"invalid Product ref {ref!r}")
    return ref[len(prefix):]


def _product(record: dict[str, Any]) -> Product:
    return Product(
        ref=str(record["ref"]), title=str(record["title"]),
        state=ProductState.ARCHIVED if bool(record.get("closed")) else ProductState.ACTIVE,
        projects=tuple(str(project) for project in record.get("projects") or ()),
        description=str(record.get("description") or ""),
    )


def _issue(record: dict[str, Any]) -> Issue:
    return Issue(
        ref=str(record["ref"]), title=str(record["title"]), product_ref=f"product:{record['product']}",
        state=IssueState.CLOSED if bool(record.get("closed")) else IssueState.OPEN,
        priority=str(record.get("priority") or ""), issue_kind=str(record.get("kind") or ""),
        description=str(record.get("description") or ""), close_reason=record.get("close_reason"),
    )


def _sprint(record: dict[str, Any]) -> Sprint:
    status = str(record.get("status") or "open")
    try:
        state = SprintState(status)
    except ValueError as exc:
        raise BoardProtocolError(f"invalid Sprint lifecycle state {status!r}") from exc
    return Sprint(
        ref=str(record["ref"]), goal=str(record.get("goal") or ""), state=state,
        product_ref=f"product:{record['product']}" if record.get("product") else None,
        issue_refs=tuple(str(ref) for ref in record.get("issues") or ()),
        card_refs=tuple(str(card.get("ref")) for card in record.get("cards") or () if isinstance(card, dict)),
    )


def _card(record: dict[str, Any]) -> Card:
    if record.get("record_type") in {"issue", "product"}:
        raise BoardProtocolError(f"{record.get('ref')!r} is not an execution Card")
    state = str(record.get("state") or "")
    try:
        lifecycle = CardState(state)
    except ValueError as exc:
        raise BoardProtocolError(f"invalid Card lifecycle state {state!r}") from exc
    return Card(
        ref=str(record["ref"]), title=str(record.get("title") or ""), state=lifecycle,
        sprint_ref=str(record["sprint"]) if record.get("sprint") else None,
        description=str(record.get("description") or ""),
    )
