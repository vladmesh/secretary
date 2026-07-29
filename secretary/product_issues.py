"""Durable Product and Issue records on the existing Pipeline board.

Products and issues use ordinary Kanboard rows with explicit record metadata.  This keeps the
board, its checkpoint export and its append-only audit as the only durable backend; products are
not mirrored into a local registry.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Callable

import yaml

from secretary.tasks import KanboardClient, TaskAudit, TaskError, _now


ISSUES_COLUMN = "Issues"
PRODUCT_TYPE = "product"
ISSUE_TYPE = "issue"
META_RECORD_TYPE = "record_type"
META_PRODUCT_ID = "product_id"
META_PRODUCT_PROJECTS = "product_projects"
META_ISSUE_PRODUCT = "issue_product"
META_ISSUE_KIND = "issue_kind"
META_ISSUE_PRIORITY = "issue_priority"
META_ISSUE_CLOSED_REASON = "issue_closed_reason"
ISSUE_KINDS = {"bug", "feature", "question", "improvement"}
ISSUE_PRIORITIES = {"P0", "P1", "P2", "P3"}
ISSUE_CLOSE_REASONS = {"resolved", "invalid", "duplicate", "wont_do"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ProductIssueValidationError(ValueError):
    """A normalized Product or Issue record is not durable canonical state."""


def validate_product_issue_records(
    records: list[dict[str, Any]], *, registered_project_ids: set[str] | None = None
) -> None:
    """Validate typed board records without classifying any legacy cards.

    Checkpoint has the instance project registry and passes it here. Restore is also
    used as a standalone recovery primitive, so it always validates the durable
    shape and cross-record Product references, while its callers may additionally
    supply the registry when it is available.
    """
    products: dict[str, dict[str, Any]] = {}
    issues: list[tuple[dict[str, Any], dict[str, str]]] = []
    for record in records:
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            # Older checkpoints and task-shaped test fixtures predate normalized
            # metadata. They are not typed Product/Issue records and remain legacy.
            continue
        kind = metadata.get(META_RECORD_TYPE)
        if kind not in {PRODUCT_TYPE, ISSUE_TYPE}:
            continue
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items()):
            raise ProductIssueValidationError("record metadata must contain strings")
        if record.get("column") != ISSUES_COLUMN:
            raise ProductIssueValidationError("Product or Issue record is outside the Issues column")
        if kind == PRODUCT_TYPE:
            product_id = metadata.get(META_PRODUCT_ID, "")
            if not _ID.fullmatch(product_id):
                raise ProductIssueValidationError("Product has an invalid product_id")
            if record.get("reference") != f"product:{product_id}":
                raise ProductIssueValidationError("Product reference does not match product_id")
            if not isinstance(record.get("title"), str) or not record["title"].strip():
                raise ProductIssueValidationError("Product has no title")
            projects = _validated_product_projects(metadata.get(META_PRODUCT_PROJECTS))
            if registered_project_ids is not None:
                unknown = sorted(set(projects) - registered_project_ids)
                if unknown:
                    raise ProductIssueValidationError(
                        "Product has unknown registered project(s): " + ", ".join(unknown)
                    )
            if product_id in products:
                raise ProductIssueValidationError(f"duplicate Product id: {product_id}")
            products[product_id] = record
        else:
            issues.append((record, metadata))
    for record, metadata in issues:
        product = metadata.get(META_ISSUE_PRODUCT, "")
        if product not in products:
            raise ProductIssueValidationError("Issue has no registered Product")
        if metadata.get(META_ISSUE_KIND) not in ISSUE_KINDS:
            raise ProductIssueValidationError("Issue has an invalid kind")
        if metadata.get(META_ISSUE_PRIORITY) not in ISSUE_PRIORITIES:
            raise ProductIssueValidationError("Issue has an invalid priority")
        reason = metadata.get(META_ISSUE_CLOSED_REASON, "")
        if bool(record.get("closed")):
            if reason not in ISSUE_CLOSE_REASONS:
                raise ProductIssueValidationError("closed Issue has an invalid close reason")
        elif reason:
            raise ProductIssueValidationError("open Issue has a close reason")


def _validated_product_projects(raw: str | None) -> list[str]:
    try:
        projects = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        raise ProductIssueValidationError("Product has invalid product_projects") from None
    if (
        not isinstance(projects, list)
        or not projects
        or any(not isinstance(project, str) or not _ID.fullmatch(project) for project in projects)
        or len(set(projects)) != len(projects)
    ):
        raise ProductIssueValidationError("Product needs a non-empty unique project set")
    return projects


def _comment_was_saved(result: Any) -> bool:
    """Kanboard returns a positive comment id, unlike its boolean metadata API."""
    return result is True or (isinstance(result, int) and not isinstance(result, bool) and result > 0)


def registered_projects(instance: str | Path) -> set[str]:
    root = Path(instance).expanduser()
    if root.name == "instance.yaml":
        root = root.parent
    projects = root / "projects"
    if not projects.is_dir():
        raise TaskError("validation", "project registry is unavailable", 2)
    result: set[str] = set()
    for path in projects.glob("*.yaml"):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raise TaskError("validation", f"cannot read project registry entry {path.name}", 2) from None
        project_id = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(project_id, str) and project_id:
            result.add(project_id)
    return result


class ProductIssueTransaction:
    """One durable, replayable Product/Issue operation.

    The pending audit record is the operation's intent and progress journal.  It is
    deliberately written before the first write to Kanboard.  A retry therefore
    drives the same intent forward instead of inventing a second repair protocol.
    """

    _KINDS = {
        "product.create": "product_created",
        "issue.create": "issue_created",
        "issue.priority": "issue_priority_changed",
        "issue.close": "issue_closed",
    }

    def __init__(self, store: "ProductIssueStore", *, operation: str, intent: dict[str, Any], request_id: str) -> None:
        self.store = store
        self.operation = operation
        self.intent = intent
        self.request_id = request_id

    @property
    def kind(self) -> str:
        return self._KINDS[self.operation]

    @property
    def entity(self) -> str:
        if self.operation == "product.create":
            return f"product:{self.intent['product_id']}"
        if self.operation == "issue.create":
            return f"product:{self.intent['product']}"
        return f"issue:{self.intent['reference']}"

    def _event(self, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.store._event(
            kind=self.kind,
            role="po",
            actor=str(self.intent["actor"]),
            reference=str(self.intent.get("reference") or ""),
            task_id=None,
            request_id=self.request_id,
            payload=self.store._audit_payload(self.operation, self.intent),
        ) | {
            "protocol": "product_issue_transaction",
            "operation": self.operation,
            "intent": self.intent,
            "entity": self.entity,
            "context": context or {},
            "phase": "staged",
        }

    def _validate_existing(self, event: dict[str, Any] | None) -> None:
        if event is None:
            return
        if (
            event.get("protocol") != "product_issue_transaction"
            or event.get("operation") != self.operation
            or event.get("intent") != self.intent
        ):
            raise TaskError("validation", "request id belongs to another operation or payload", 2)

    def _existing(self) -> tuple[dict[str, Any] | None, bool, bool]:
        committed = self.store.audit.committed_event(self.request_id)
        pending = self.store.audit.pending_event(self.request_id)
        for event in (committed, pending):
            self._validate_existing(event)
        if committed is not None:
            return committed, True, pending is not None
        return pending, False, False

    def _stage(self, event: dict[str, Any], phase: str) -> None:
        event["phase"] = phase
        self.store.audit.stage(self.request_id, event)

    def _backend(self, event: dict[str, Any], task_id: int, reference: str) -> None:
        event["ref"] = reference
        event["task_id"] = f"task_kanboard_{task_id}"
        event["backend"] = {"kind": "kanboard", "task_id": task_id, "revision": "product-issue"}

    def run(self, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        staged = self._event(context=context)
        event, _, _ = self.store.audit.claim(self.request_id, staged)
        self._validate_existing(event)
        with self.store.audit.request_lock(self.request_id):
            # Another retry with this same request may have completed while this
            # caller waited for the operation lock. Read the durable result again
            # before driving any backend sub-step.
            event, committed, pending = self._existing()
            if committed:
                if pending:
                    try:
                        # TaskAudit.append is idempotent: when the event line reached
                        # disk before its pending file could be removed, it only repairs
                        # that cleanup. Product/Issue intents stay out of generic audit
                        # reconciliation, so the owning retry must do this work.
                        self.store.audit.append(self.request_id, event)
                    except (OSError, TaskError, KeyError, TypeError, ValueError):
                        raise TaskError(
                            "audit_pending",
                            "Product/Issue operation is pending audit cleanup; retry with the same request id",
                            4,
                        ) from None
                return self.store._result(self.operation, event)
            if event is None:
                raise TaskError("backend_error", "pending Product/Issue operation is unavailable", 1)
            try:
                self._drive(event)
                self._stage(event, "ready_to_commit")
                event_id = self.store.audit.append(self.request_id, event)
                if not isinstance(event_id, str) or not event_id:
                    raise TaskError("backend_error", "Product/Issue audit append was rejected", 1)
            except (OSError, TaskError, KeyError, TypeError, ValueError):
                # The staged intent is the repair handle even when Kanboard accepted a
                # write and its reply was lost.  Never discard it from this point on.
                raise TaskError("audit_pending", "Product/Issue operation is pending repair; retry with the same request id", 4) from None
            return self.store._result(self.operation, event)

    def _drive(self, event: dict[str, Any]) -> None:
        if self.operation in {"product.create", "issue.create"}:
            self._drive_create(event)
        elif self.operation == "issue.priority":
            self._drive_priority(event)
        elif self.operation == "issue.close":
            self._drive_close(event)
        else:  # Defensive: a malformed pending file must remain unresolved.
            raise TaskError("backend_error", "pending Product/Issue operation is invalid", 1)

    def _pending_description(self) -> str:
        description = str(self.intent.get("description") or "")
        return description + "\n\n[product-issue-request:" + self.request_id + "]"

    def _create_card(self, event: dict[str, Any]) -> tuple[dict[str, Any], int]:
        backend = event.get("backend")
        task_id = backend.get("task_id") if isinstance(backend, dict) else None
        if isinstance(task_id, int):
            for card in self.store._cards():
                if card.get("id") == task_id:
                    return card, task_id
        marker = self._pending_description()
        for card in self.store._cards():
            if card.get("description") == marker:
                number = card.get("id")
                if isinstance(number, int):
                    self._backend(event, number, str(card.get("reference") or ""))
                    self._stage(event, "row_created")
                    return card, number
        board_id, column_id = self.store._board()
        provisional = (
            f"product:{self.intent['product_id']}"
            if self.operation == "product.create"
            else f"pending-issue:{self.request_id}"
        )
        task_id = self.store.client.call(
            "createTask", project_id=board_id, title=self.intent["title"],
            description=marker, column_id=column_id, swimlane_id=0, reference=provisional,
        )
        if not isinstance(task_id, int):
            raise TaskError("backend_error", "Kanboard rejected the Product/Issue row", 1)
        card = next((item for item in self.store._cards() if item.get("id") == task_id), None)
        if not isinstance(card, dict):
            raise TaskError("backend_error", "created Product/Issue row was not found", 1)
        self._backend(event, task_id, str(card.get("reference") or provisional))
        self._stage(event, "row_created")
        return card, task_id

    def _drive_create(self, event: dict[str, Any]) -> None:
        card, task_id = self._create_card(event)
        reference = (
            f"product:{self.intent['product_id']}"
            if self.operation == "product.create" else f"issue:{task_id}"
        )
        if str(card.get("reference") or "") != reference:
            if self.store.client.call("updateTask", id=task_id, reference=reference, description=self.intent["description"]) is not True:
                raise TaskError("backend_error", "Kanboard rejected Product/Issue reference", 1)
        else:
            # A production Kanboard accepts reference on create; the fixture and
            # older servers do not.  Normalize the marker once the row is known.
            if str(card.get("description") or "") != self.intent["description"]:
                if self.store.client.call("updateTask", id=task_id, description=self.intent["description"]) is not True:
                    raise TaskError("backend_error", "Kanboard rejected Product/Issue description", 1)
        self._backend(event, task_id, reference)
        self._stage(event, "reference_written")
        if self.operation == "product.create":
            metadata = {
                META_RECORD_TYPE: PRODUCT_TYPE,
                META_PRODUCT_ID: str(self.intent["product_id"]),
                META_PRODUCT_PROJECTS: str(self.intent["projects"]),
            }
        else:
            metadata = {
                META_RECORD_TYPE: ISSUE_TYPE,
                META_ISSUE_PRODUCT: str(self.intent["product"]),
                META_ISSUE_KIND: str(self.intent["issue_kind"]),
                META_ISSUE_PRIORITY: str(self.intent["priority"]),
            }
        actual = self.store._metadata(next(card for card in self.store._cards() if card.get("id") == task_id))
        if any(actual.get(key) != value for key, value in metadata.items()):
            if self.store.client.call("saveTaskMetadata", task_id=task_id, values=metadata) is not True:
                raise TaskError("backend_error", "Kanboard rejected Product/Issue metadata", 1)
        actual = self.store._metadata(next(card for card in self.store._cards() if card.get("id") == task_id))
        if any(actual.get(key) != value for key, value in metadata.items()):
            raise TaskError("backend_error", "Product/Issue metadata remains incomplete", 1)
        self._stage(event, "metadata_written")

    def _card_for_issue(self, event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], int]:
        reference = str(self.intent["reference"])
        card, metadata = self.store._find(reference, ISSUE_TYPE)
        task_id = int(card["id"])
        self._backend(event, task_id, reference)
        return card, metadata, task_id

    @staticmethod
    def _has_comment(comments: Any, content: str) -> bool:
        return isinstance(comments, list) and any(
            isinstance(comment, dict) and comment.get("comment") == content for comment in comments
        )

    def _ensure_comment(self, event: dict[str, Any], task_id: int, content: str, phase: str) -> None:
        comments = self.store.client.call("getAllComments", task_id=task_id) or []
        if not self._has_comment(comments, content):
            if not _comment_was_saved(self.store.client.call("createComment", task_id=task_id, user_id=0, content=content)):
                raise TaskError("backend_error", "Kanboard rejected required issue comment", 1)
        comments = self.store.client.call("getAllComments", task_id=task_id) or []
        if not self._has_comment(comments, content):
            raise TaskError("backend_error", "required issue comment remains incomplete", 1)
        self._stage(event, phase)

    def _comment(self, kind: str, reason: str) -> str:
        """Make every required comment a durable sub-step of this request."""
        return f"[issue:{kind}]\n{reason}\n[product-issue-request:{self.request_id}]"

    def _drive_priority(self, event: dict[str, Any]) -> None:
        card, metadata, task_id = self._card_for_issue(event)
        if int(card.get("is_active", 1) or 0) == 0:
            raise TaskError("closed", "cannot reprioritize a closed issue", 3)
        context = event.setdefault("context", {})
        previous = context.get("previous_priority")
        if not isinstance(previous, str):
            previous = metadata.get(META_ISSUE_PRIORITY, "")
            context["previous_priority"] = previous
            self._stage(event, "target_read")
        event["payload"] = {"from": previous, "to": self.intent["priority"], "reason": self.intent["reason"]}
        self._ensure_comment(event, task_id, self._comment("priority", str(self.intent["reason"])), "comment_written")
        target = str(self.intent["priority"])
        if metadata.get(META_ISSUE_PRIORITY) != target:
            if self.store.client.call("saveTaskMetadata", task_id=task_id, values={META_ISSUE_PRIORITY: target}) is not True:
                raise TaskError("backend_error", "Kanboard rejected issue priority", 1)
        actual = self.store._metadata(card)
        if actual.get(META_ISSUE_PRIORITY) != target:
            raise TaskError("backend_error", "issue priority remains incomplete", 1)
        self._stage(event, "priority_written")

    def _drive_close(self, event: dict[str, Any]) -> None:
        card, metadata, task_id = self._card_for_issue(event)
        reason = str(self.intent["reason"])
        existing_reason = metadata.get(META_ISSUE_CLOSED_REASON) or ""
        if existing_reason not in {"", reason}:
            raise TaskError("backend_error", "pending issue close reason no longer matches", 1)
        if int(card.get("is_active", 1) or 0) == 0 and existing_reason != reason:
            raise TaskError("closed", "issue is already closed", 3)
        self._ensure_comment(event, task_id, self._comment("closed", reason), "comment_written")
        if existing_reason != reason:
            if self.store.client.call("saveTaskMetadata", task_id=task_id, values={META_ISSUE_CLOSED_REASON: reason}) is not True:
                raise TaskError("backend_error", "Kanboard rejected issue closure metadata", 1)
        actual = self.store._metadata(card)
        if actual.get(META_ISSUE_CLOSED_REASON) != reason:
            raise TaskError("backend_error", "issue closure metadata remains incomplete", 1)
        self._stage(event, "reason_written")
        if int(card.get("is_active", 1) or 0) != 0:
            if not self.store.client.call("closeTask", task_id=task_id):
                raise TaskError("backend_error", "Kanboard rejected issue closure", 1)
        closed, closed_metadata = self.store._find(str(self.intent["reference"]), ISSUE_TYPE)
        if int(closed.get("is_active", 1) or 0) != 0 or closed_metadata.get(META_ISSUE_CLOSED_REASON) != reason:
            raise TaskError("backend_error", "issue closure remains incomplete", 1)
        self._stage(event, "closed")


class ProductIssueStore:
    def __init__(self, client: KanboardClient, *, data_dir: str | Path, instance: str | Path) -> None:
        self.client = client
        self.audit = TaskAudit(data_dir)
        self.instance = Path(instance)

    def _board(self) -> tuple[int, int]:
        board = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(board, dict) or not isinstance(board.get("id"), int):
            raise TaskError("backend_error", "Pipeline board is unavailable", 1)
        columns = self.client.call("getColumns", project_id=board["id"]) or []
        first = columns[0] if columns else None
        if isinstance(first, dict) and first.get("title") == ISSUES_COLUMN and isinstance(first.get("id"), int):
            return board["id"], first["id"]
        raise TaskError("legacy_layout", "Pipeline first column is not Issues; run the supported board migration", 2)

    def _cards(self) -> list[dict[str, Any]]:
        board_id, _ = self._board()
        # Kanboard status 2 is the complete set.  Status 0 means only closed cards.
        cards = self.client.call("getAllTasks", project_id=board_id, status_id=2) or []
        if not isinstance(cards, list):
            raise TaskError("backend_error", "Kanboard returned an invalid task list", 1)
        return [card for card in cards if isinstance(card, dict)]

    def _metadata(self, card: dict[str, Any]) -> dict[str, str]:
        number = card.get("id")
        if not isinstance(number, int):
            raise TaskError("backend_error", "Kanboard returned an invalid task", 1)
        raw = self.client.call("getTaskMetadata", task_id=number) or {}
        if not isinstance(raw, dict):
            raise TaskError("backend_error", "Kanboard returned invalid task metadata", 1)
        return {str(key): str(value) for key, value in raw.items()}

    def _find(self, reference: str, record_type: str) -> tuple[dict[str, Any], dict[str, str]]:
        board_id, _ = self._board()
        card = self.client.call("getTaskByReference", project_id=board_id, reference=reference)
        if not isinstance(card, dict):
            raise TaskError("not_found", f"{record_type} was not found", 2)
        metadata = self._metadata(card)
        if metadata.get(META_RECORD_TYPE) != record_type:
            raise TaskError("validation", f"{reference!r} is not a {record_type}", 2)
        return card, metadata

    @staticmethod
    def _view(card: dict[str, Any], metadata: dict[str, str]) -> dict[str, Any]:
        kind = metadata.get(META_RECORD_TYPE)
        view = {
            "ref": str(card.get("reference") or ""), "title": str(card.get("title") or ""),
            "description": str(card.get("description") or ""), "closed": int(card.get("is_active", 1) or 0) == 0,
        }
        if kind == PRODUCT_TYPE:
            try:
                projects = json.loads(metadata.get(META_PRODUCT_PROJECTS, "[]"))
            except json.JSONDecodeError:
                projects = []
            view.update({"id": metadata.get(META_PRODUCT_ID, ""), "projects": projects})
        else:
            view.update({"product": metadata.get(META_ISSUE_PRODUCT, ""), "kind": metadata.get(META_ISSUE_KIND, ""), "priority": metadata.get(META_ISSUE_PRIORITY, ""), "close_reason": metadata.get(META_ISSUE_CLOSED_REASON) or None})
        return view

    def _event(self, *, kind: str, role: str, actor: str, reference: str, task_id: int | None, request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"event_id": "evt_" + uuid.uuid4().hex, "schema_version": 1, "occurred_at": _now(), "actor": {"role": role, "id": actor}, "kind": kind, "outcome": "success", "task_id": f"task_kanboard_{task_id}" if task_id is not None else "", "ref": reference, "backend": {"kind": "kanboard", "task_id": task_id, "revision": "product-issue"}, "request_id": request_id, "payload": payload}

    @staticmethod
    def _audit_payload(operation: str, intent: dict[str, Any]) -> dict[str, Any]:
        if operation == "product.create":
            return {"record_type": PRODUCT_TYPE, "product_id": intent["product_id"], "product_projects": intent["projects"]}
        if operation == "issue.create":
            return {"record_type": ISSUE_TYPE, "product": intent["product"], "issue_kind": intent["issue_kind"], "priority": intent["priority"]}
        if operation == "issue.priority":
            return {"from": "", "to": intent["priority"], "reason": intent["reason"]}
        return {"reason": intent["reason"]}

    def _result(self, operation: str, event: dict[str, Any]) -> dict[str, Any]:
        if operation == "product.create":
            return self.show_product(str(event["intent"]["product_id"]))
        return self.show_issue(str(event.get("ref") or event["intent"]["reference"]))

    def _transaction(self, operation: str, intent: dict[str, Any], request_id: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        transaction = ProductIssueTransaction(self, operation=operation, intent=intent, request_id=request_id)
        return self._run_entity_transaction(transaction, context=context)

    @staticmethod
    def _pending_entity(event: dict[str, Any]) -> str | None:
        entity = event.get("entity")
        if isinstance(entity, str):
            return entity
        operation = event.get("operation")
        intent = event.get("intent")
        if not isinstance(operation, str) or not isinstance(intent, dict):
            return None
        try:
            return ProductIssueTransaction(
                None, operation=operation, intent=intent, request_id=str(event["request_id"])
            ).entity
        except (KeyError, TypeError):
            return None

    def _repair_entity_pending(self, entity: str, *, except_request_id: str) -> None:
        """Finish earlier work on an entity before a new mutation observes it."""
        for event in self.audit.pending_events():
            if event.get("protocol") != "product_issue_transaction":
                continue
            if event.get("request_id") == except_request_id or self._pending_entity(event) != entity:
                continue
            operation = event.get("operation")
            intent = event.get("intent")
            request_id = event.get("request_id")
            context = event.get("context")
            if not isinstance(operation, str) or not isinstance(intent, dict) or not isinstance(request_id, str):
                raise TaskError("audit_pending", "Product/Issue operation is pending repair", 4)
            if operation not in ProductIssueTransaction._KINDS:
                raise TaskError("audit_pending", "Product/Issue operation is pending repair", 4)
            ProductIssueTransaction(self, operation=operation, intent=intent, request_id=request_id).run(
                context=context if isinstance(context, dict) else None
            )

    def _run_entity_transaction(
        self,
        transaction: ProductIssueTransaction,
        *,
        context: dict[str, Any] | None = None,
        fresh: Callable[[], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        existing, _, _ = transaction._existing()
        if existing is not None:
            return transaction.run()
        with self.audit.entity_lock(transaction.entity):
            self._repair_entity_pending(transaction.entity, except_request_id=transaction.request_id)
            existing, _, _ = transaction._existing()
            if existing is not None:
                return transaction.run()
            if fresh is not None:
                context = fresh()
            return transaction.run(context=context)

    def create_product(self, *, product_id: str, projects: list[str], title: str, description: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if not _ID.fullmatch(product_id):
            raise TaskError("validation", "product id must match [a-z0-9][a-z0-9-]{0,62}", 2)
        if not title.strip() or not projects or len(set(projects)) != len(projects):
            raise TaskError("validation", "product needs a title and a non-empty unique project set", 2)
        request_id = request_id or str(uuid.uuid4())
        intent = {"product_id": product_id, "projects": json.dumps(sorted(projects), separators=(",", ":")), "title": title, "description": description, "actor": actor}
        transaction = ProductIssueTransaction(self, operation="product.create", intent=intent, request_id=request_id)

        def fresh() -> dict[str, Any]:
            unknown = sorted(set(projects) - registered_projects(self.instance))
            if unknown:
                raise TaskError("validation", "unknown registered project(s): " + ", ".join(unknown), 2)
            ref = f"product:{product_id}"
            board_id, column_id = self._board()
            if self.client.call("getTaskByReference", project_id=board_id, reference=ref):
                raise TaskError("validation", f"product {product_id!r} already exists", 2)
            return {"board_id": board_id, "column_id": column_id}

        return self._run_entity_transaction(transaction, fresh=fresh)

    def list_products(self) -> list[dict[str, Any]]:
        return sorted((self._view(card, meta) for card in self._cards() if (meta := self._metadata(card)).get(META_RECORD_TYPE) == PRODUCT_TYPE), key=lambda item: str(item["id"]))

    def show_product(self, product_id: str) -> dict[str, Any]:
        card, meta = self._find(f"product:{product_id}", PRODUCT_TYPE)
        return self._view(card, meta)

    def create_issue(self, *, product: str, issue_kind: str, priority: str, title: str, description: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if not product.strip() or issue_kind not in ISSUE_KINDS or priority not in ISSUE_PRIORITIES or not title.strip():
            raise TaskError("validation", "issue requires title, product, kind (bug|feature|question|improvement) and priority (P0-P3)", 2)
        request_id = request_id or str(uuid.uuid4())
        intent = {"product": product, "issue_kind": issue_kind, "priority": priority, "title": title, "description": description, "actor": actor}
        transaction = ProductIssueTransaction(self, operation="issue.create", intent=intent, request_id=request_id)

        def fresh() -> dict[str, Any]:
            self.show_product(product)
            board_id, column_id = self._board()
            return {"board_id": board_id, "column_id": column_id}

        return self._run_entity_transaction(transaction, fresh=fresh)

    def list_issues(self, *, product: str | None = None, include_closed: bool = False) -> list[dict[str, Any]]:
        result = []
        for card in self._cards():
            meta = self._metadata(card)
            closed = int(card.get("is_active", 1) or 0) == 0
            if (
                meta.get(META_RECORD_TYPE) == ISSUE_TYPE
                and (product is None or meta.get(META_ISSUE_PRODUCT) == product)
                and (include_closed or not closed)
            ):
                result.append(self._view(card, meta))
        return sorted(result, key=lambda item: (item["priority"], item["ref"]))

    def show_issue(self, reference: str) -> dict[str, Any]:
        card, meta = self._find(reference, ISSUE_TYPE)
        task_id = int(card["id"])
        comments = self.client.call("getAllComments", task_id=task_id) or []
        if not isinstance(comments, list):
            raise TaskError("backend_error", "Kanboard returned invalid issue comments", 1)
        history = [
            {
                "created_at": str(comment.get("date_creation") or ""),
                "text": str(comment.get("comment") or ""),
            }
            for comment in comments
            if isinstance(comment, dict)
        ]
        audit = [
            event for event in self.audit.events()
            if isinstance(event, dict) and event.get("ref") == reference
        ]
        view = self._view(card, meta)
        view["history"] = {"comments": history, "audit": audit}
        return view

    def update_priority(self, *, reference: str, priority: str, reason: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if priority not in ISSUE_PRIORITIES or not reason.strip():
            raise TaskError("validation", "priority update requires P0-P3 and a non-empty reason", 2)
        request_id = request_id or str(uuid.uuid4())
        intent = {"reference": reference, "priority": priority, "reason": reason, "actor": actor}
        transaction = ProductIssueTransaction(self, operation="issue.priority", intent=intent, request_id=request_id)

        def fresh() -> dict[str, Any]:
            card, metadata = self._find(reference, ISSUE_TYPE)
            if int(card.get("is_active", 1) or 0) == 0:
                raise TaskError("closed", "cannot reprioritize a closed issue", 3)
            return {"previous_priority": metadata.get(META_ISSUE_PRIORITY, "")}

        return self._run_entity_transaction(transaction, fresh=fresh)

    def close_issue(self, *, reference: str, reason: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if reason not in ISSUE_CLOSE_REASONS:
            raise TaskError("validation", "close reason must be one of: resolved, invalid, duplicate, wont_do", 2)
        request_id = request_id or str(uuid.uuid4())
        intent = {"reference": reference, "reason": reason, "actor": actor}
        transaction = ProductIssueTransaction(self, operation="issue.close", intent=intent, request_id=request_id)

        def fresh() -> dict[str, Any]:
            card, _ = self._find(reference, ISSUE_TYPE)
            if int(card.get("is_active", 1) or 0) == 0:
                raise TaskError("closed", "issue is already closed", 3)
            return {}

        return self._run_entity_transaction(transaction, fresh=fresh)
