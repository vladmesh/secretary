"""Kanboard implementation of normalized BoardHost reads and lifecycle transitions."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from secretary.board.card_transitions import CardTransitionForbidden, card_transition
from secretary.board.events import BoardEventCanon, MutationEventTransaction
from secretary.board.host import Create, MutationResult, Replace, SprintSupplement, TransitionRequest
from secretary.board.models import (
    BoardEntity, Card, CardState, EntityKind, Event, EventKind, Issue, IssueState, Product,
    ProductState, RelatedRefs, Sprint, SprintState,
)
from secretary.board.transitions import BoardProtocolError, transition
from secretary.product_issues import ProductIssueStore
from secretary.sprints import SprintReader
from secretary.tasks import (
    KanboardClient, TaskReader, _positive_int, _target_column_id, all_project_cards,
    project_card_by_reference,
)


class KanboardBoardHost:
    """Translate current Kanboard readers and migrated lifecycle edges at the host seam.

    Card, Sprint and the released Product/Issue writer are migrated: each
    is the one authority for its backend mutation and owns a typed event
    transaction.  Other mutations remain unavailable until their migration can
    preserve the established writer's durable retry and audit semantics.
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
        if operation.entity.kind not in {EntityKind.PRODUCT, EntityKind.ISSUE}:
            self._migration_pending("create", operation.entity.kind)
        self._require_product_issue_configuration()
        entity = operation.entity
        if not isinstance(entity, (Product, Issue)):
            raise BoardProtocolError("Product/Issue create requires a normalized Product or Issue")
        request_id = self._request_id(operation.request_id, f"{entity.kind.value}-create")
        related = self._related(entity, operation.related_refs)
        existing = self._existing(request_id, entity, operation.actor, operation.reason)
        if existing is not None and self.canon.committed(request_id) is not None:
            return MutationResult(self.read(entity.kind, entity.ref), existing)
        # A pending occurrence is its own validated recovery evidence.  Do not
        # make its repair depend on mutable inputs such as the project registry
        # that may have changed after the original writer staged it.
        if existing is None:
            self._validate_create(entity)
        event = existing or self._entity_event(EventKind.ENTITY_CREATED, entity, operation.actor, operation.reason, related, request_id)

        def effect() -> None:
            if self._raw_by_ref(entity.ref) is not None:
                raise BoardProtocolError(f"{entity.kind.value} already exists")
            board_id, column_id = self._issues_board()
            # This is preparatory evidence, not part of the uncertain write
            # window.  A failure here proves that createTask was never issued,
            # so MutationEventTransaction must discard the staged occurrence.
            swimlane_id = self._issues_swimlane(board_id)
            try:
                reply = self.client.call(
                    "createTask", project_id=board_id, title=entity.title,
                    description=self._create_marker(request_id), column_id=column_id,
                    swimlane_id=swimlane_id, reference=entity.ref,
                )
            except Exception:
                # An exception after the RPC was issued is deliberately not a
                # refusal.  The confirming read either proves this exact
                # reference or leaves the staged event pending; a retry must
                # never issue a second unproven create.
                return
            task_id = _positive_int(reply)
            if task_id is None:
                # A false-ish reply is not enough to establish that the
                # backend refused the write.  Both reads must complete and
                # prove absence before this remains inside the transaction's
                # discard window.  A transport, malformed-response, or
                # ambiguous-marker failure is uncertain post-write evidence:
                # confirmation then leaves the exact staged occurrence pending
                # instead of authorizing a second create on retry.
                try:
                    reference_row = self._raw_by_ref(entity.ref)
                    marker_row = self._raw_by_marker(request_id)
                except Exception:
                    return
                if reference_row is not None or marker_row is not None:
                    return
                raise BoardProtocolError("Kanboard refused the Product/Issue row")

        def confirm() -> dict[str, Any]:
            row = self._row_for_create(entity, request_id)
            if row is None:
                raise BoardProtocolError("Product/Issue create is not proven on the Kanboard board")
            return row

        def finish(_created: dict[str, Any]) -> None:
            row = self._row_for_create(entity, request_id)
            if row is None:
                raise BoardProtocolError("created Product/Issue row was not found")
            task_id = self._row_id(row)
            if row.get("reference") != entity.ref or row.get("description") != entity.description:
                if not self.client.call("updateTask", id=task_id, reference=entity.ref, description=entity.description):
                    raise BoardProtocolError("Kanboard rejected Product/Issue details")
            if self.client.call("saveTaskMetadata", task_id=task_id, values=self._metadata_for(entity)) is not True:
                raise BoardProtocolError("Kanboard rejected Product/Issue metadata")
            confirmed = self._raw_by_ref(entity.ref)
            if confirmed is None or self._normalized_row(confirmed) != entity:
                raise BoardProtocolError("Product/Issue create remains incomplete")

        MutationEventTransaction(self.canon, request_id=request_id, event=event).execute(effect, confirm=confirm, finish=finish)
        return MutationResult(self.read(entity.kind, entity.ref), event)

    def replace(self, operation: Replace) -> MutationResult:
        if not isinstance(operation.entity, Issue):
            self._migration_pending("replace", operation.entity.kind)
        self._require_product_issue_configuration()
        entity = operation.entity
        request_id = self._request_id(operation.request_id, "issue-replace")
        related = self._related(entity, operation.related_refs)
        existing = self._existing(request_id, entity, operation.actor, operation.reason)
        if existing is not None and self.canon.committed(request_id) is not None:
            return MutationResult(self.read(EntityKind.ISSUE, entity.ref), existing)
        if existing is None:
            current = self.read(EntityKind.ISSUE, entity.ref)
            if not isinstance(current, Issue) or current.state is not IssueState.OPEN:
                raise BoardProtocolError("cannot replace a closed Issue")
            if (entity.title, entity.product_ref, entity.state, entity.issue_kind, entity.description) != (
                current.title, current.product_ref, current.state, current.issue_kind, current.description,
            ) or entity.priority not in {"P0", "P1", "P2", "P3"}:
                raise BoardProtocolError("Issue replace only supports a non-empty priority change")
        event = existing or self._entity_event(EventKind.ENTITY_UPDATED, entity, operation.actor, operation.reason, related, request_id)
        content = f"[issue:priority]\n{operation.reason}\n[request-id:{request_id}]"

        def effect() -> None:
            row = self._raw_by_ref(entity.ref)
            if row is None:
                raise BoardProtocolError("Issue was not found")
            task_id = self._row_id(row)
            comments = self.client.call("getAllComments", task_id=task_id) or []
            if not any(isinstance(comment, dict) and comment.get("comment") == content for comment in comments):
                try:
                    saved = self.client.call("createComment", task_id=task_id, user_id=0, content=content)
                except Exception:
                    # As with creates, the reply is not a proof that the write
                    # did not happen.  Confirmation decides whether this exact
                    # pending occurrence is recoverable.
                    return
                if not _comment_saved(saved):
                    raise BoardProtocolError("Kanboard rejected issue priority comment")

        def confirm() -> BoardEntity:
            row = self._raw_by_ref(entity.ref)
            if row is None:
                raise BoardProtocolError("Issue priority change is not proven")
            comments = self.client.call("getAllComments", task_id=self._row_id(row)) or []
            if not any(isinstance(comment, dict) and comment.get("comment") == content for comment in comments):
                raise BoardProtocolError("Issue priority comment is not proven")
            return self._normalized_row(row)

        def finish(_confirmed: BoardEntity) -> None:
            row = self._raw_by_ref(entity.ref)
            if row is None or self.client.call("saveTaskMetadata", task_id=self._row_id(row), values={"issue_priority": entity.priority}) is not True:
                raise BoardProtocolError("Kanboard rejected issue priority")
            row = self._raw_by_ref(entity.ref)
            confirmed = self._normalized_row(row) if row is not None else None
            if not isinstance(confirmed, Issue) or confirmed.priority != entity.priority:
                raise BoardProtocolError("Issue priority remains incomplete")

        MutationEventTransaction(self.canon, request_id=request_id, event=event).execute(effect, confirm=confirm, finish=finish)
        return MutationResult(self.read(EntityKind.ISSUE, entity.ref), event)

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
        if operation.kind is EntityKind.SPRINT:
            return self._transition_sprint(operation)
        if operation.kind is EntityKind.ISSUE:
            return self._transition_issue(operation)
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

    def _transition_sprint(self, operation: TransitionRequest) -> MutationResult:
        """Apply one checked Sprint status edge through the typed event canon.

        The explicit Sprint supplement is a small allow-list: an observer on a
        reopen or a computed budget on a hard stop.  It is never a backend
        metadata bag.  The adapter remains the sole owner of the Kanboard
        status mutation and its storage spelling.
        """
        if self.canon is None:
            raise BoardProtocolError("Sprint transitions require a configured data directory")
        if not isinstance(operation.target, SprintState):
            raise BoardProtocolError("Sprint transitions require a SprintState target")
        self._validate_sprint_supplement(operation)
        request_id = self._request_id(operation.request_id, "sprint-transition")
        current = self.read(EntityKind.SPRINT, operation.ref)
        if not isinstance(current, Sprint):
            raise BoardProtocolError("Sprint transition resolved a non-Sprint entity")
        existing = self.canon.event(request_id)
        if existing is not None:
            required = tuple(ref for ref in (current.product_ref, *current.issue_refs, *current.card_refs) if ref)
            related = RelatedRefs(operation.related_refs.refs + required)
            if (
                existing.entity_kind is not EntityKind.SPRINT or existing.ref != operation.ref
                or existing.actor != operation.actor or existing.reason != operation.reason
                or existing.target_state != operation.target.value or existing.data != self._sprint_data(operation)
                or existing.related_refs != related
                or not self._declared_sprint_event(existing, current)
            ):
                raise ValueError("request id belongs to another operation or payload")
            if self.canon.committed(request_id) is not None:
                return MutationResult(current, existing)
            event = existing
        else:
            successor, declaration = transition(current, operation.target)
            if not isinstance(successor, Sprint):
                raise BoardProtocolError("Sprint transition resolved an invalid successor")
            related = operation.related_refs
            required = tuple(ref for ref in (current.product_ref, *current.issue_refs, *current.card_refs) if ref)
            if any(ref not in related.refs for ref in required):
                related = RelatedRefs(related.refs + required)
            event = self._sprint_event(
                declaration.event_kind, successor, operation.actor, operation.reason, related, request_id,
                source=current.state.value, target=operation.target.value, data=self._sprint_data(operation),
            )

        def effect() -> None:
            live = self.read(EntityKind.SPRINT, operation.ref)
            if not isinstance(live, Sprint):
                raise BoardProtocolError("Sprint transition resolved a non-Sprint entity")
            transition(live, operation.target)
            task_id = self._sprint_task_id(operation.ref)
            supplement = operation.sprint
            # Reopen deliberately persists its observer while the row remains
            # closed.  If the following status write is refused, the command
            # facade can restore that recorded preimage; treating the two calls
            # as one would silently remove its released compensation boundary.
            if supplement is not None and supplement.observer is not None:
                if self.client.call(
                    "saveTaskMetadata", task_id=task_id, values={"sprint_observer": supplement.observer},
                ) is not True:
                    raise BoardProtocolError("Kanboard rejected Sprint transition")
                if not self._sprint_metadata_matches(task_id, {"sprint_observer": supplement.observer}):
                    raise BoardProtocolError("Sprint transition observer remains incomplete")
            values = {"sprint_status": operation.target.value}
            if supplement is not None and supplement.budget_by_type:
                values["sprint_budget"] = json.dumps(
                    {"by_type": dict(supplement.budget_by_type)}, separators=(",", ":"),
                )
            try:
                reply = self.client.call("saveTaskMetadata", task_id=task_id, values=values)
            except Exception:
                # A transport failure after issuing the state effect is not a
                # refusal.  Confirmation decides whether the staged event can
                # commit, and otherwise leaves it for recovery.
                return
            if reply is not True:
                raise BoardProtocolError("Kanboard rejected Sprint transition")

        def confirm() -> Sprint:
            entity = self.read(EntityKind.SPRINT, operation.ref)
            if not isinstance(entity, Sprint) or entity.state is not operation.target:
                raise BoardProtocolError("Sprint transition is not proven on the Kanboard board")
            return entity

        entity = MutationEventTransaction(self.canon, request_id=request_id, event=event).execute(
            effect, confirm=confirm,
        )
        return MutationResult(entity, event)

    def recover_sprint(self, request_id: str) -> MutationResult:
        """Commit a pending Sprint occurrence only after its exact state is live."""
        if self.canon is None:
            raise BoardProtocolError("Sprint recovery requires a configured data directory")
        event = self.canon.event(request_id)
        if event is None or event.entity_kind is not EntityKind.SPRINT:
            raise BoardProtocolError("pending event is not a recoverable Sprint occurrence")
        if event.target_state is None:
            raise BoardProtocolError("pending Sprint event has no target state")
        try:
            target = SprintState(event.target_state)
        except ValueError as exc:
            raise BoardProtocolError("pending Sprint event has an invalid target") from exc
        entity = self.read(EntityKind.SPRINT, event.ref)
        if not isinstance(entity, Sprint) or entity.state is not target:
            raise BoardProtocolError("pending Sprint transition is not proven on the Kanboard board")
        if not self._declared_sprint_event(event, entity):
            raise BoardProtocolError("pending Sprint event has an unsupported lifecycle edge")
        self.canon.commit(request_id, event)
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

    def recover_product_issue(self, request_id: str) -> MutationResult:
        """Resume one typed Product/Issue occurrence without consulting legacy journals."""
        self._require_product_issue_configuration()
        assert self.canon is not None
        event = self.canon.event(request_id)
        if event is None or event.entity_kind not in {EntityKind.PRODUCT, EntityKind.ISSUE}:
            raise BoardProtocolError("pending event is not a recoverable Product/Issue occurrence")
        if event.entity_kind is EntityKind.PRODUCT:
            entity = _product_from_payload(event.data)
        else:
            entity = _issue_from_payload(event.data)
        if event.kind is EventKind.ENTITY_CREATED:
            return self.create(Create(entity, event.actor, event.reason, event.related_refs, request_id))
        if event.kind is EventKind.ENTITY_UPDATED and isinstance(entity, Issue):
            return self.replace(Replace(entity, event.actor, event.reason, event.related_refs, request_id))
        if event.kind is EventKind.ISSUE_CLOSED and isinstance(entity, Issue):
            return self.transition(TransitionRequest(EntityKind.ISSUE, entity.ref, IssueState.CLOSED, event.actor, event.reason, event.related_refs, request_id))
        raise BoardProtocolError("pending event is not a recoverable Product/Issue occurrence")

    def _transition_issue(self, operation: TransitionRequest) -> MutationResult:
        self._require_product_issue_configuration()
        if not isinstance(operation.target, IssueState):
            raise BoardProtocolError("Issue transitions require an IssueState target")
        request_id = self._request_id(operation.request_id, "issue-transition")
        known = self.canon.event(request_id) if self.canon is not None else None
        if known is not None:
            if (
                known.entity_kind is not EntityKind.ISSUE or known.ref != operation.ref
                or known.actor != operation.actor or known.reason != operation.reason
                or known.target_state != operation.target.value
            ):
                raise ValueError("request id belongs to another operation or payload")
            successor = _issue_from_payload(known.data)
            current = self.read(EntityKind.ISSUE, operation.ref)
            if not isinstance(current, Issue):
                raise BoardProtocolError("Issue transition resolved a non-Issue entity")
            declaration = None
        else:
            current = self.read(EntityKind.ISSUE, operation.ref)
            if not isinstance(current, Issue):
                raise BoardProtocolError("Issue transition resolved a non-Issue entity")
            successor, declaration = transition(current, operation.target)
            if not isinstance(successor, Issue):
                raise BoardProtocolError("Issue transition resolved an invalid successor")
            successor = Issue(
                successor.ref, successor.title, successor.product_ref, successor.state, successor.priority,
                successor.issue_kind, successor.description, operation.reason,
            )
        related = self._related(current, operation.related_refs)
        existing = self._existing(request_id, successor, operation.actor, operation.reason, target=operation.target.value)
        if existing is not None and self.canon.committed(request_id) is not None:
            return MutationResult(self.read(EntityKind.ISSUE, operation.ref), existing)
        event = existing or self._entity_event(
            EventKind.ISSUE_CLOSED, successor, operation.actor, operation.reason, related, request_id,
            source=current.state.value, target=operation.target.value,
        )
        content = f"[issue:closed]\n{operation.reason}\n[request-id:{request_id}]"

        def effect() -> None:
            row = self._raw_by_ref(current.ref)
            if row is None:
                raise BoardProtocolError("Issue was not found")
            task_id = self._row_id(row)
            comments = self.client.call("getAllComments", task_id=task_id) or []
            if not any(isinstance(comment, dict) and comment.get("comment") == content for comment in comments):
                try:
                    saved = self.client.call("createComment", task_id=task_id, user_id=0, content=content)
                except Exception:
                    return
                if not _comment_saved(saved):
                    raise BoardProtocolError("Kanboard rejected issue close comment")

        def confirm() -> BoardEntity:
            row = self._raw_by_ref(current.ref)
            if row is None:
                raise BoardProtocolError("Issue close is not proven")
            comments = self.client.call("getAllComments", task_id=self._row_id(row)) or []
            if not any(isinstance(comment, dict) and comment.get("comment") == content for comment in comments):
                raise BoardProtocolError("Issue close comment is not proven")
            return self._normalized_row(row)

        def finish(_confirmed: BoardEntity) -> None:
            row = self._raw_by_ref(current.ref)
            if row is None:
                raise BoardProtocolError("Issue was not found")
            task_id = self._row_id(row)
            if self.client.call("saveTaskMetadata", task_id=task_id, values={"issue_closed_reason": operation.reason}) is not True:
                raise BoardProtocolError("Kanboard rejected issue close reason")
            row = self._raw_by_ref(current.ref)
            if row is None:
                raise BoardProtocolError("Issue was not found")
            if int(row.get("is_active", 1) or 0) != 0 and not self.client.call("closeTask", task_id=task_id):
                raise BoardProtocolError("Kanboard rejected issue closure")
            row = self._raw_by_ref(current.ref)
            if row is None or self._normalized_row(row) != successor:
                raise BoardProtocolError("Issue closure remains incomplete")

        MutationEventTransaction(self.canon, request_id=request_id, event=event).execute(effect, confirm=confirm, finish=finish)
        return MutationResult(self.read(EntityKind.ISSUE, operation.ref), event)

    @staticmethod
    def _sprint_data(operation: TransitionRequest) -> dict[str, object]:
        return operation.sprint.event_data() if operation.sprint is not None else {}

    @staticmethod
    def _validate_sprint_supplement(operation: TransitionRequest) -> None:
        supplement = operation.sprint
        if supplement is None:
            return
        if not isinstance(supplement, SprintSupplement):
            raise BoardProtocolError("Sprint transition supplement must be normalized")
        if operation.target is SprintState.OPEN:
            if supplement.budget_by_type:
                raise BoardProtocolError("Sprint reopen cannot persist a budget")
            return
        if operation.target is SprintState.STOPPED:
            if supplement.observer is not None or not supplement.budget_by_type:
                raise BoardProtocolError("Sprint hard stop requires its computed budget only")
            return
        raise BoardProtocolError("Sprint close cannot persist supplementary values")

    def _sprint_task_id(self, ref: str) -> int:
        record = SprintReader(self.client, data_dir=self.data_dir).show(ref, include_cards=False)
        task_id = _positive_int(str(record.get("id") or "").removeprefix("sprint_kanboard_"))
        if task_id is None:
            raise BoardProtocolError("Kanboard returned an invalid Sprint")
        return task_id

    def _sprint_metadata_matches(self, task_id: int, values: dict[str, str]) -> bool:
        actual = self.client.call("getTaskMetadata", task_id=task_id)
        if not isinstance(actual, dict):
            raise BoardProtocolError("Kanboard returned invalid Sprint metadata")
        return all(str(actual.get(key) or "") == value for key, value in values.items())

    @staticmethod
    def _declared_sprint_event(event: Event, entity: Sprint) -> bool:
        if event.source_state is None or event.target_state is None:
            return False
        try:
            source = SprintState(event.source_state)
            target = SprintState(event.target_state)
        except ValueError:
            return False
        try:
            _successor, declaration = transition(
                Sprint(entity.ref, entity.goal, source, entity.product_ref, entity.issue_refs, entity.card_refs), target,
            )
        except BoardProtocolError:
            return False
        return event.kind is declaration.event_kind

    @staticmethod
    def _sprint_event(
        kind: EventKind, entity: Sprint, actor, reason: str, related: RelatedRefs, request_id: str,
        *, source: str | None = None, target: str | None = None, data: dict[str, Any] | None = None,
    ) -> Event:
        payload = json.dumps({
            "request_id": request_id, "kind": kind.value,
            "entity": [entity.ref, entity.goal, entity.state.value, entity.product_ref, entity.issue_refs, entity.card_refs],
            "actor": [actor.role, actor.id, actor.head_run_ref], "reason": reason,
            "related_refs": list(related.refs), "source": source, "target": target, "data": data or {},
        }, sort_keys=True, separators=(",", ":"))
        return Event(
            "board-event-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32], kind,
            EntityKind.SPRINT, entity.ref, actor, reason, datetime.now(UTC), related, source, target,
            data or {},
        )

    def _require_product_issue_configuration(self) -> None:
        if self.canon is None or self.data_dir is None or self.instance is None:
            raise BoardProtocolError("Product/Issue mutations require configured data and instance directories")

    @staticmethod
    def _request_id(request_id: str | None, prefix: str) -> str:
        result = request_id or f"{prefix}-{uuid.uuid4().hex}"
        if not isinstance(result, str) or not result.strip():
            raise ValueError("request id must not be empty")
        return result

    def _existing(
        self, request_id: str, entity: Product | Issue, actor, reason: str, *, target: str | None = None,
    ) -> Event | None:
        assert self.canon is not None
        event = self.canon.event(request_id)
        if event is None:
            return None
        if (
            event.entity_kind is not entity.kind or event.ref != entity.ref or event.actor != actor
            or event.reason != reason or event.target_state != target
            or event.data != _entity_payload(entity)
        ):
            raise ValueError("request id belongs to another operation or payload")
        return event

    @staticmethod
    def _related(entity: Product | Issue, related: RelatedRefs) -> RelatedRefs:
        if isinstance(entity, Issue) and entity.product_ref not in related.refs:
            return RelatedRefs(related.refs + (entity.product_ref,))
        return related

    def _validate_create(self, entity: Product | Issue) -> None:
        if isinstance(entity, Product):
            if (
                entity.state is not ProductState.ACTIVE or not entity.projects
                or not entity.ref.startswith("product:") or not entity.ref.removeprefix("product:").strip()
            ):
                raise BoardProtocolError("Product create requires an active Product with projects")
            from secretary.product_issues import registered_projects
            unknown = sorted(set(entity.projects) - registered_projects(self.instance or ""))
            if unknown:
                raise BoardProtocolError("Product has unknown registered project(s): " + ", ".join(unknown))
            return
        if (
            entity.state is not IssueState.OPEN or not entity.ref.startswith("issue:")
            or entity.issue_kind not in {"bug", "feature", "question", "improvement"}
            or entity.priority not in {"P0", "P1", "P2", "P3"}
        ):
            raise BoardProtocolError("Issue create requires an open Issue with a valid kind and priority")
        product = self.read(EntityKind.PRODUCT, entity.product_ref)
        if not isinstance(product, Product) or product.state is not ProductState.ACTIVE:
            raise BoardProtocolError("Issue create requires an active Product")

    def _entity_event(
        self, kind: EventKind, entity: Product | Issue, actor, reason: str, related: RelatedRefs,
        request_id: str, *, source: str | None = None, target: str | None = None,
    ) -> Event:
        payload = json.dumps({
            "request_id": request_id, "kind": kind.value, "entity": _entity_payload(entity),
            "actor": [actor.role, actor.id, actor.head_run_ref], "reason": reason,
            "related_refs": list(related.refs), "source": source, "target": target,
        }, sort_keys=True, separators=(",", ":"))
        return Event(
            "board-event-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32], kind,
            entity.kind, entity.ref, actor, reason, datetime.now(UTC), related, source, target,
            _entity_payload(entity),
        )

    def _issues_board(self) -> tuple[int, int]:
        board = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(board, dict) or not isinstance(board.get("id"), int):
            raise BoardProtocolError("Pipeline board is unavailable")
        columns = self.client.call("getColumns", project_id=board["id"]) or []
        first = columns[0] if isinstance(columns, list) and columns else None
        if not isinstance(first, dict) or first.get("title") != "Issues" or not isinstance(first.get("id"), int):
            raise BoardProtocolError("Pipeline first column is not Issues")
        return board["id"], first["id"]

    def _issues_swimlane(self, board_id: int) -> int:
        lanes = self.client.call("getActiveSwimlanes", project_id=board_id) or []
        if not isinstance(lanes, list):
            raise BoardProtocolError("Kanboard returned invalid swimlanes")
        candidates = [(_nonnegative(lane.get("position")), identifier) for lane in lanes if isinstance(lane, dict) and (identifier := _positive_int(lane.get("id"))) is not None]
        return min(candidates)[1] if candidates else 0

    def _raw_by_ref(self, ref: str) -> dict[str, Any] | None:
        board_id, _ = self._issues_board()
        row = self.client.call("getTaskByReference", project_id=board_id, reference=ref)
        return row if isinstance(row, dict) else None

    def _raw_by_marker(self, request_id: str) -> dict[str, Any] | None:
        board_id, _ = self._issues_board()
        marker = self._create_marker(request_id)
        matches = [
            row for row in all_project_cards(self.client, board_id)
            if isinstance(row, dict) and row.get("description") == marker
        ]
        if len(matches) > 1:
            raise BoardProtocolError("Product/Issue create correlation is ambiguous")
        return matches[0] if matches else None

    def _row_for_create(self, entity: Product | Issue, request_id: str) -> dict[str, Any] | None:
        return self._raw_by_ref(entity.ref) or self._raw_by_marker(request_id)

    @staticmethod
    def _row_id(row: dict[str, Any]) -> int:
        task_id = _positive_int(row.get("id"))
        if task_id is None:
            raise BoardProtocolError("Kanboard returned an invalid Product/Issue row")
        return task_id

    def _normalized_row(self, row: dict[str, Any], *, allow_incomplete: bool = False) -> Product | Issue:
        task_id = self._row_id(row)
        metadata = self.client.call("getTaskMetadata", task_id=task_id) or {}
        if not isinstance(metadata, dict):
            raise BoardProtocolError("Kanboard returned invalid Product/Issue metadata")
        record_type = metadata.get("record_type")
        if record_type == "product":
            projects = json.loads(str(metadata.get("product_projects") or "[]"))
            if not allow_incomplete and (not isinstance(projects, list) or not projects):
                raise BoardProtocolError("Product metadata remains incomplete")
            return Product(str(row.get("reference") or ""), str(row.get("title") or ""), ProductState.ACTIVE, tuple(str(value) for value in projects), str(row.get("description") or ""))
        if record_type == "issue":
            return Issue(str(row.get("reference") or ""), str(row.get("title") or ""), f"product:{metadata.get('issue_product') or ''}", IssueState.CLOSED if int(row.get("is_active", 1) or 0) == 0 else IssueState.OPEN, str(metadata.get("issue_priority") or ""), str(metadata.get("issue_kind") or ""), str(row.get("description") or ""), str(metadata.get("issue_closed_reason") or "") or None)
        if allow_incomplete:
            # A staged create has not set metadata yet.  It is still sufficient evidence that
            # the uniquely referenced backend effect happened; finish supplies the typed shape.
            ref = str(row.get("reference") or "")
            if ref.startswith("product:"):
                return Product(ref, str(row.get("title") or ""), projects=("pending",), description=str(row.get("description") or ""))
            return Issue(ref, str(row.get("title") or ""), "product:pending", priority="pending", issue_kind="pending", description=str(row.get("description") or ""))
        raise BoardProtocolError("row is not a Product or Issue")

    @staticmethod
    def _metadata_for(entity: Product | Issue) -> dict[str, str]:
        if isinstance(entity, Product):
            return {"record_type": "product", "product_id": entity.ref.removeprefix("product:"), "product_projects": json.dumps(list(entity.projects), separators=(",", ":"))}
        return {"record_type": "issue", "issue_product": entity.product_ref.removeprefix("product:"), "issue_kind": entity.issue_kind, "issue_priority": entity.priority}

    @staticmethod
    def _create_marker(request_id: str) -> str:
        return "[secretary-product-issue-transaction:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest() + "]"

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


def _comment_saved(result: Any) -> bool:
    return result is True or (isinstance(result, int) and not isinstance(result, bool) and result > 0)


def _nonnegative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _entity_payload(entity: Product | Issue) -> dict[str, Any]:
    if isinstance(entity, Product):
        return {"ref": entity.ref, "title": entity.title, "state": entity.state.value, "projects": list(entity.projects), "description": entity.description}
    return {"ref": entity.ref, "title": entity.title, "product_ref": entity.product_ref, "state": entity.state.value, "priority": entity.priority, "issue_kind": entity.issue_kind, "description": entity.description, "close_reason": entity.close_reason}


def _issue_from_payload(data: dict[str, Any]) -> Issue:
    try:
        return Issue(
            str(data["ref"]), str(data["title"]), str(data["product_ref"]), IssueState(str(data["state"])),
            str(data["priority"]), str(data["issue_kind"]), str(data.get("description") or ""),
            str(data.get("close_reason") or "") or None,
        )
    except (KeyError, ValueError) as exc:
        raise BoardProtocolError("pending Issue event has invalid normalized evidence") from exc


def _product_from_payload(data: dict[str, Any]) -> Product:
    try:
        projects = data["projects"]
        if not isinstance(projects, list):
            raise ValueError("projects")
        return Product(
            str(data["ref"]), str(data["title"]), ProductState(str(data["state"])),
            tuple(str(project) for project in projects), str(data.get("description") or ""),
        )
    except (KeyError, ValueError) as exc:
        raise BoardProtocolError("pending Product event has invalid normalized evidence") from exc
