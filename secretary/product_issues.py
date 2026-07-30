"""Durable Product and Issue records on the existing Pipeline board.

Products and issues use ordinary Kanboard rows with explicit record metadata.  This keeps the
board, its checkpoint export and its append-only audit as the only durable backend; products are
not mirrored into a local registry.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

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
    """The private staged journal for Product/Issue writes.

    It is deliberately separate from TaskAudit's generic pending records.  Generic task
    reconciliation must not turn a partially applied Product/Issue intent into an audit event.
    """

    def __init__(self, data_dir: str | Path, audit: TaskAudit) -> None:
        board = Path(data_dir) / "board"
        self.directory = board / "product-issue-transactions"
        self.lock_path = board / ".product-issue-transactions.lock"
        self.audit = audit

    def _path(self, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self.directory / f"v1-{digest}.json"

    def _lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        return open(self.lock_path, "a+", encoding="utf-8")

    @staticmethod
    def _atomic(path: Path, document: dict[str, Any]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".product-issue-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(document, output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _matches(event: dict[str, Any], *, kind: str, intent: dict[str, Any]) -> bool:
        payload = event.get("payload")
        return event.get("kind") == kind and isinstance(payload, dict) and payload.get("intent") == intent

    def existing(self, request_id: str, *, kind: str, intent: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            if not self._matches(committed, kind=kind, intent=intent):
                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            try:
                self._path(request_id).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise TaskError("audit_pending", "Product/Issue audit cleanup is pending repair", 4) from None
            return None, committed
        path = self._path(request_id)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, None
        except (OSError, ValueError):
            raise TaskError("audit_pending", "Product/Issue transaction journal is unreadable", 4) from None
        if document.get("kind") != kind or document.get("intent") != intent:
            raise TaskError("validation", "request id belongs to another operation or payload", 2)
        return document, None

    def begin(self, request_id: str, *, kind: str, intent: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        path = self._path(request_id)
        with self._lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    document = {"version": 1, "request_id": request_id, "kind": kind, "intent": intent, "event": event, "progress": {}}
                    self._atomic(path, document)
                except (OSError, ValueError):
                    raise TaskError("audit_pending", "Product/Issue transaction journal is unreadable", 4) from None
                if document.get("kind") != kind or document.get("intent") != intent:
                    raise TaskError("validation", "request id belongs to another operation or payload", 2)
                return document
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def save(self, document: dict[str, Any]) -> None:
        request_id = document.get("request_id")
        if not isinstance(request_id, str):
            raise TaskError("audit_pending", "Product/Issue transaction has no request id", 4)
        with self._lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._atomic(self._path(request_id), document)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def complete(self, document: dict[str, Any]) -> None:
        request_id = str(document["request_id"])
        event = document.get("event")
        if not isinstance(event, dict):
            raise TaskError("audit_pending", "Product/Issue transaction has no audit event", 4)
        self.audit.append(request_id, event)
        try:
            self._path(request_id).unlink()
        except FileNotFoundError:
            pass


class ProductIssueStore:
    def __init__(self, client: KanboardClient, *, data_dir: str | Path, instance: str | Path) -> None:
        self.client = client
        self.audit = TaskAudit(data_dir)
        self.transactions = ProductIssueTransaction(data_dir, self.audit)
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

    def list_products(self) -> list[dict[str, Any]]:
        return sorted((self._view(card, meta) for card in self._cards() if (meta := self._metadata(card)).get(META_RECORD_TYPE) == PRODUCT_TYPE), key=lambda item: str(item["id"]))

    def show_product(self, product_id: str) -> dict[str, Any]:
        card, meta = self._find(f"product:{product_id}", PRODUCT_TYPE)
        return self._view(card, meta)

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

    def _transaction_event(self, *, kind: str, actor: str, reference: str, request_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        return self._event(
            kind=kind, role="po", actor=actor, reference=reference, task_id=None,
            request_id=request_id, payload={"intent": intent},
        )

    def _transaction_card(self, document: dict[str, Any]) -> dict[str, Any]:
        event = document["event"]
        backend = event.get("backend") if isinstance(event, dict) else None
        task_id = backend.get("task_id") if isinstance(backend, dict) else None
        if isinstance(task_id, int):
            for card in self._cards():
                if card.get("id") == task_id:
                    return card
        reference = str(event.get("ref") or "") if isinstance(event, dict) else ""
        if reference:
            board_id, _ = self._board()
            card = self.client.call("getTaskByReference", project_id=board_id, reference=reference)
            if isinstance(card, dict):
                return card
        raise TaskError("backend_error", "pending Product/Issue row was not found", 1)

    def _remember_card(self, document: dict[str, Any], card: dict[str, Any]) -> int:
        task_id = card.get("id")
        if not isinstance(task_id, int):
            raise TaskError("backend_error", "Kanboard returned an invalid task", 1)
        event = document["event"]
        event["task_id"] = f"task_kanboard_{task_id}"
        event["backend"] = {"kind": "kanboard", "task_id": task_id, "revision": "product-issue"}
        document.setdefault("progress", {})["task_id"] = task_id
        self.transactions.save(document)
        return task_id

    def _ensure_created(self, document: dict[str, Any], *, title: str, description: str) -> tuple[dict[str, Any], int]:
        try:
            card = self._transaction_card(document)
        except TaskError:
            event = document["event"]
            reference = str(event.get("ref") or "")
            if not reference:
                raise
            board_id, column_id = self._board()
            document.setdefault("progress", {})["create_started"] = True
            self.transactions.save(document)
            task_id = self.client.call(
                "createTask", project_id=board_id, title=title, description=description,
                column_id=column_id, swimlane_id=0, reference=reference,
            )
            if not isinstance(task_id, int):
                raise TaskError("backend_error", "Kanboard rejected the Product/Issue row", 1)
            card = next((row for row in self._cards() if row.get("id") == task_id), None)
            if not isinstance(card, dict):
                raise TaskError("backend_error", "created Product/Issue row was not found", 1)
        reference = str(document["event"].get("ref") or "")
        if str(card.get("reference") or "") != reference:
            document.setdefault("progress", {})["reference_started"] = True
            self.transactions.save(document)
            if not self.client.call("updateTask", id=int(card["id"]), reference=reference):
                raise TaskError("backend_error", "Kanboard rejected Product/Issue reference", 1)
            card = self._transaction_card(document) if document.get("progress", {}).get("task_id") else next(
                (row for row in self._cards() if row.get("id") == task_id), None
            )
            if not isinstance(card, dict) or str(card.get("reference") or "") != reference:
                raise TaskError("backend_error", "Product/Issue reference remains incomplete", 1)
        return card, self._remember_card(document, card)

    def _ensure_metadata(self, document: dict[str, Any], task_id: int, values: dict[str, str]) -> None:
        actual = self._metadata(self._transaction_card(document))
        if all(actual.get(key) == value for key, value in values.items()):
            return
        document.setdefault("progress", {})["metadata_started"] = True
        self.transactions.save(document)
        if self.client.call("saveTaskMetadata", task_id=task_id, values=values) is not True:
            raise TaskError("backend_error", "Kanboard rejected Product/Issue metadata", 1)
        actual = self._metadata(self._transaction_card(document))
        if any(actual.get(key) != value for key, value in values.items()):
            raise TaskError("backend_error", "Product/Issue metadata remains incomplete", 1)
        document["progress"]["metadata_done"] = True
        self.transactions.save(document)

    def _ensure_comment(self, document: dict[str, Any], task_id: int, content: str, label: str) -> None:
        comments = self.client.call("getAllComments", task_id=task_id) or []
        if any(isinstance(comment, dict) and comment.get("comment") == content for comment in comments):
            return
        document.setdefault("progress", {})[f"{label}_started"] = True
        self.transactions.save(document)
        if not _comment_was_saved(self.client.call("createComment", task_id=task_id, user_id=0, content=content)):
            raise TaskError("backend_error", f"Kanboard rejected issue {label} comment", 1)
        comments = self.client.call("getAllComments", task_id=task_id) or []
        if not any(isinstance(comment, dict) and comment.get("comment") == content for comment in comments):
            raise TaskError("backend_error", f"issue {label} comment remains incomplete", 1)
        document["progress"][f"{label}_done"] = True
        self.transactions.save(document)

    def _finish_create(self, document: dict[str, Any]) -> None:
        intent = document["intent"]
        card, task_id = self._ensure_created(document, title=str(intent["title"]), description=str(intent["description"]))
        if intent["record_type"] == PRODUCT_TYPE:
            values = {
                META_RECORD_TYPE: PRODUCT_TYPE, META_PRODUCT_ID: str(intent["product_id"]),
                META_PRODUCT_PROJECTS: str(intent["product_projects"]),
            }
        else:
            values = {
                META_RECORD_TYPE: ISSUE_TYPE, META_ISSUE_PRODUCT: str(intent["product"]),
                META_ISSUE_KIND: str(intent["issue_kind"]), META_ISSUE_PRIORITY: str(intent["priority"]),
            }
        self._ensure_metadata(document, task_id, values)
        if not isinstance(card, dict):
            raise TaskError("backend_error", "Product/Issue row was not found", 1)

    def _finish_priority(self, document: dict[str, Any]) -> None:
        intent = document["intent"]
        card, metadata = self._find(str(intent["reference"]), ISSUE_TYPE)
        if int(card.get("is_active", 1) or 0) == 0:
            raise TaskError("closed", "cannot reprioritize a closed issue", 3)
        task_id = self._remember_card(document, card)
        previous = document.setdefault("progress", {}).get("previous")
        if not isinstance(previous, str):
            previous = metadata.get(META_ISSUE_PRIORITY, "")
            document["progress"]["previous"] = previous
            document["event"]["payload"] = {"intent": intent, "from": previous, "to": intent["priority"], "reason": intent["reason"]}
            self.transactions.save(document)
        content = f"[issue:priority]\n{intent['reason']}\n[request-id:{document['request_id']}]"
        self._ensure_comment(document, task_id, content, "priority")
        self._ensure_metadata(document, task_id, {META_ISSUE_PRIORITY: str(intent["priority"])})

    def _finish_close(self, document: dict[str, Any]) -> None:
        intent = document["intent"]
        card, _ = self._find(str(intent["reference"]), ISSUE_TYPE)
        task_id = self._remember_card(document, card)
        content = f"[issue:closed]\n{intent['reason']}\n[request-id:{document['request_id']}]"
        self._ensure_comment(document, task_id, content, "close")
        self._ensure_metadata(document, task_id, {META_ISSUE_CLOSED_REASON: str(intent["reason"])})
        current, _ = self._find(str(intent["reference"]), ISSUE_TYPE)
        if int(current.get("is_active", 1) or 0) != 0:
            document.setdefault("progress", {})["close_started"] = True
            self.transactions.save(document)
            if not self.client.call("closeTask", task_id=task_id):
                raise TaskError("backend_error", "Kanboard rejected issue closure", 1)
        closed, closed_metadata = self._find(str(intent["reference"]), ISSUE_TYPE)
        if int(closed.get("is_active", 1) or 0) != 0 or closed_metadata.get(META_ISSUE_CLOSED_REASON) != intent["reason"]:
            raise TaskError("backend_error", "issue closure remains incomplete", 1)
        document["progress"]["close_done"] = True
        self.transactions.save(document)

    def _complete_transaction(self, document: dict[str, Any], finish) -> None:
        try:
            finish(document)
            self.transactions.complete(document)
        except TaskError as exc:
            if exc.code in {"validation", "closed"} and not document.get("progress"):
                raise
            raise TaskError("audit_pending", "Product/Issue write is pending repair; retry with the same request id", 4) from None
        except (OSError, KeyError, TypeError):
            raise TaskError("audit_pending", "Product/Issue write is pending repair; retry with the same request id", 4) from None

    def create_product(self, *, product_id: str, projects: list[str], title: str, description: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if not _ID.fullmatch(product_id) or not title.strip() or not projects or len(set(projects)) != len(projects):
            raise TaskError("validation", "product needs a valid id, title and non-empty unique project set", 2)
        request_id = request_id or str(uuid.uuid4())
        intent = {"record_type": PRODUCT_TYPE, "product_id": product_id, "product_projects": json.dumps(sorted(projects), separators=(",", ":")), "title": title, "description": description, "actor": actor}
        document, completed = self.transactions.existing(request_id, kind="product_created", intent=intent)
        if completed is not None:
            return self.show_product(product_id)
        if document is None:
            unknown = sorted(set(projects) - registered_projects(self.instance))
            if unknown:
                raise TaskError("validation", "unknown registered project(s): " + ", ".join(unknown), 2)
            board_id, _ = self._board()
            reference = f"product:{product_id}"
            if self.client.call("getTaskByReference", project_id=board_id, reference=reference):
                raise TaskError("validation", f"product {product_id!r} already exists", 2)
            event = self._transaction_event(kind="product_created", actor=actor, reference=reference, request_id=request_id, intent=intent)
            document = self.transactions.begin(request_id, kind="product_created", intent=intent, event=event)
        self._complete_transaction(document, self._finish_create)
        return self.show_product(product_id)

    def create_issue(self, *, product: str, issue_kind: str, priority: str, title: str, description: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if not product.strip() or issue_kind not in ISSUE_KINDS or priority not in ISSUE_PRIORITIES or not title.strip():
            raise TaskError("validation", "issue requires title, product, kind (bug|feature|question|improvement) and priority (P0-P3)", 2)
        request_id = request_id or str(uuid.uuid4())
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:20]
        reference = f"issue:{digest}"
        intent = {"record_type": ISSUE_TYPE, "product": product, "issue_kind": issue_kind, "priority": priority, "title": title, "description": description, "actor": actor}
        document, completed = self.transactions.existing(request_id, kind="issue_created", intent=intent)
        if completed is not None:
            return self.show_issue(str(completed["ref"]))
        if document is None:
            self.show_product(product)
            self._board()
            event = self._transaction_event(kind="issue_created", actor=actor, reference=reference, request_id=request_id, intent=intent)
            document = self.transactions.begin(request_id, kind="issue_created", intent=intent, event=event)
        self._complete_transaction(document, self._finish_create)
        return self.show_issue(str(document["event"]["ref"]))

    def update_priority(self, *, reference: str, priority: str, reason: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if priority not in ISSUE_PRIORITIES or not reason.strip():
            raise TaskError("validation", "priority update requires P0-P3 and a non-empty reason", 2)
        request_id = request_id or str(uuid.uuid4())
        intent = {"reference": reference, "priority": priority, "reason": reason, "actor": actor}
        document, completed = self.transactions.existing(request_id, kind="issue_priority_changed", intent=intent)
        if completed is not None:
            return self.show_issue(reference)
        if document is None:
            card, metadata = self._find(reference, ISSUE_TYPE)
            if int(card.get("is_active", 1) or 0) == 0:
                raise TaskError("closed", "cannot reprioritize a closed issue", 3)
            event = self._transaction_event(kind="issue_priority_changed", actor=actor, reference=reference, request_id=request_id, intent=intent)
            event["payload"] = {"intent": intent, "from": metadata.get(META_ISSUE_PRIORITY, ""), "to": priority, "reason": reason}
            document = self.transactions.begin(request_id, kind="issue_priority_changed", intent=intent, event=event)
        self._complete_transaction(document, self._finish_priority)
        return self.show_issue(reference)

    def close_issue(self, *, reference: str, reason: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if reason not in ISSUE_CLOSE_REASONS:
            raise TaskError("validation", "close reason must be one of: resolved, invalid, duplicate, wont_do", 2)
        request_id = request_id or str(uuid.uuid4())
        intent = {"reference": reference, "reason": reason, "actor": actor}
        document, completed = self.transactions.existing(request_id, kind="issue_closed", intent=intent)
        if completed is not None:
            return self.show_issue(reference)
        if document is None:
            card, _ = self._find(reference, ISSUE_TYPE)
            if int(card.get("is_active", 1) or 0) == 0:
                raise TaskError("closed", "issue is already closed", 3)
            event = self._transaction_event(kind="issue_closed", actor=actor, reference=reference, request_id=request_id, intent=intent)
            document = self.transactions.begin(request_id, kind="issue_closed", intent=intent, event=event)
        self._complete_transaction(document, self._finish_close)
        return self.show_issue(reference)
