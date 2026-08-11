"""Kanboard implementation of the normalized board-host read boundary.

This is the deliberately narrow phase-one adapter boundary.  It translates the
existing readers' normalized records to immutable protocol values, but does not
move existing command paths behind ``BoardHost``.  In particular, TaskWriter,
SprintWriter and ProductIssueStore still own their present audit/retry behavior;
their migration is a later card, not a compatibility path hidden here.

All direct Kanboard/JSON-RPC use for BoardHost reads is consequently confined to
the legacy clients these methods delegate to.  No caller of this class receives a
Kanboard row dictionary.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from secretary.board.host import Create, MutationResult, Replace, TransitionRequest
from secretary.board.models import (
    BoardEntity, Card, CardState, EntityKind, Issue, IssueState, Product,
    ProductState, Sprint, SprintState,
)
from secretary.board.transitions import BoardProtocolError
from secretary.product_issues import ProductIssueStore
from secretary.sprints import SprintReader
from secretary.tasks import KanboardClient, TaskReader


class KanboardBoardHost:
    """Translate the current Kanboard-backed readers at the BoardHost seam.

    Mutations intentionally remain unavailable until the dedicated migration can
    preserve each legacy writer's durable retry and audit semantics.  The typed
    methods exist now so a caller cannot bypass the contract accidentally.
    """

    def __init__(
        self, client: KanboardClient, *, data_dir: str | None = None, instance: str | None = None,
    ) -> None:
        self.client = client
        self.data_dir = data_dir
        self.instance = instance

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
                if record.get("record_type") == "task"
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

    def transition(self, operation: TransitionRequest) -> MutationResult:
        self._migration_pending("transition", operation.kind)

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
    if record.get("record_type") != "task":
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
