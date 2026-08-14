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

from secretary.tasks import (
    KanboardClient,
    TaskAudit,
    TaskError,
    all_project_cards,
    _now,
    _positive_int,
)


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
# Failure codes a repeat of the same request cannot turn into a success.
_TERMINAL_CODES = {"validation", "closed", "backend_rejected"}
_TRANSACTION_KINDS = {"product_created", "issue_created", "issue_priority_changed", "issue_closed"}


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
        # Product/Issue claims and generic audit claims share one request-id namespace.
        self.lock_path = board / ".audit.lock"
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
        with self._lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return self._existing_locked(request_id, kind=kind, intent=intent)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _existing_locked(self, request_id: str, *, kind: str, intent: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
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
        # A generic record is a live claim in the same public request-id namespace.
        if self.audit.pending_event(request_id) is not None:
            raise TaskError("validation", "request id belongs to another operation or payload", 2)
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

    def begin(self, request_id: str, *, kind: str, intent: dict[str, Any], event: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        path = self._path(request_id)
        with self._lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                existing, committed = self._existing_locked(request_id, kind=kind, intent=intent)
                if existing is not None or committed is not None:
                    return existing, committed
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    document = {"version": 1, "request_id": request_id, "kind": kind, "intent": intent, "event": event, "progress": {}}
                    self._atomic(path, document)
                except (OSError, ValueError):
                    raise TaskError("audit_pending", "Product/Issue transaction journal is unreadable", 4) from None
                if document.get("kind") != kind or document.get("intent") != intent:
                    raise TaskError("validation", "request id belongs to another operation or payload", 2)
                return document, None
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def pending_for_reference(self, reference: str, *, excluding: str) -> list[dict[str, Any]]:
        try:
            paths = list(self.directory.glob("v1-*.json"))
        except OSError:
            raise TaskError("audit_pending", "Product/Issue transaction journal is unreadable", 4) from None
        result = []
        for path in paths:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise TaskError("audit_pending", "Product/Issue transaction journal is unreadable", 4) from None
            event = document.get("event")
            event_reference = event.get("ref") if isinstance(event, dict) else None
            if document.get("request_id") != excluding and event_reference == reference:
                result.append(document)
        return result

    def status(self) -> dict[str, int | bool]:
        try:
            pending = sum(path.is_file() for path in self.directory.glob("v1-*.json"))
        except OSError:
            pending = 1
        return {"ok": pending == 0, "pending": pending}

    def reference_lock(self, reference: str):
        self.directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        return open(self.directory / f".reference-{digest}.lock", "a+", encoding="utf-8")

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

    def discard(self, document: dict[str, Any]) -> None:
        """Remove an unstarted transaction that failed a terminal precondition."""
        request_id = document.get("request_id")
        if not isinstance(request_id, str):
            raise TaskError("audit_pending", "Product/Issue transaction has no request id", 4)
        if document.get("progress"):
            raise TaskError("audit_pending", "Product/Issue transaction has recorded progress", 4)
        self.drop(request_id)

    def drop(self, request_id: str) -> None:
        """Remove a transaction whose caller has established that the backend is untouched."""
        try:
            self._path(request_id).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise TaskError("audit_pending", "Product/Issue audit cleanup is pending repair", 4) from None

    def documents(self) -> list[dict[str, Any]]:
        try:
            paths = sorted(self.directory.glob("v1-*.json"))
        except OSError:
            raise TaskError("audit_pending", "Product/Issue transaction journal is unreadable", 4) from None
        result = []
        for path in paths:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                raise TaskError("audit_pending", "Product/Issue transaction journal is unreadable", 4) from None
            if isinstance(document, dict):
                result.append(document)
        return result

    def load(self, request_id: str) -> dict[str, Any]:
        try:
            document = json.loads(self._path(request_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise TaskError("not_found", "no staged Product/Issue transaction has that request id", 2) from None
        except (OSError, ValueError):
            raise TaskError("audit_pending", "Product/Issue transaction journal is unreadable", 4) from None
        if not isinstance(document, dict):
            raise TaskError("audit_pending", "Product/Issue transaction journal is unreadable", 4)
        return document

    def adopt(self, path: str | Path) -> dict[str, Any]:
        """Take a transaction document that lives outside the journal back into it.

        This is the supported way back for a document an operator had to carry out of the
        journal by hand: the file is validated as a transaction of this journal, filed under
        its own request id and removed from where it was, so `retry` and `discard` can see it.
        """
        source = Path(path).expanduser()
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise TaskError("not_found", f"{source} does not exist", 2) from None
        except (OSError, ValueError):
            raise TaskError("validation", f"{source} is not a readable transaction document", 2) from None
        request_id = document.get("request_id") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or not isinstance(request_id, str)
            or not request_id
            or document.get("kind") not in _TRANSACTION_KINDS
            or not isinstance(document.get("intent"), dict)
            or not isinstance(document.get("event"), dict)
        ):
            raise TaskError("validation", f"{source} is not a Product/Issue transaction document", 2)
        with self._lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                target = self._path(request_id)
                if target.exists():
                    raise TaskError("validation", "that request id is already staged in the journal", 2)
                self._atomic(target, document)
                try:
                    source.unlink()
                except OSError:
                    raise TaskError(
                        "audit_pending",
                        f"the transaction is back in the journal but {source} could not be removed",
                        4,
                    ) from None
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return document


def ensure_swimlane(client: KanboardClient, board_id: int, name: str) -> int:
    """The id of the board's active swimlane called ``name``, created when the board has none.

    The name is matched exactly, so a lane is the same lane for every caller and no near-name
    ever stands in for it.  Kanboard answers an ``addSwimlane`` for a name the board already
    carries with a false-ish reply rather than the existing id, so a refused create is read back
    once: a lane another writer added between the two calls is that answer, and only a board that
    still has no such lane is an error.
    """
    wanted = name.strip()
    if not wanted:
        raise TaskError("validation", "a swimlane needs a name", 2)
    identifier = _named_swimlane(client, board_id, wanted)
    if identifier is not None:
        return identifier
    created = _positive_int(client.call("addSwimlane", project_id=board_id, name=wanted))
    if created is not None:
        return created
    identifier = _named_swimlane(client, board_id, wanted)
    if identifier is None:
        raise TaskError("backend_error", f"Kanboard refused the {wanted!r} swimlane", 1)
    return identifier


def _named_swimlane(client: KanboardClient, board_id: int, name: str) -> int | None:
    lanes = client.call("getActiveSwimlanes", project_id=board_id) or []
    if not isinstance(lanes, list):
        raise TaskError("backend_error", "Kanboard returned invalid swimlanes", 1)
    for lane in lanes:
        if not isinstance(lane, dict) or str(lane.get("name") or "") != name:
            continue
        identifier = _positive_int(lane.get("id"))
        if identifier is not None:
            return identifier
    return None


def product_lane_name(product: str) -> str:
    """The name of the lane a Product or Issue row belongs in: its product id.

    This is the naming half of the product lane rule, split out so a reader that must not write -
    the reconcile plan - names the same lane the writer would create, instead of restating the
    rule in a second place.
    """
    identifier = str(product).removeprefix("product:").strip()
    if not identifier:
        raise TaskError("validation", "a Product/Issue row needs its product to choose a lane", 2)
    return identifier


def product_swimlane_id(client: KanboardClient, board_id: int, product: str) -> int:
    """The lane a Product or Issue row belongs in: the one named after its product.

    A record belongs to its product, so the lane is the active swimlane whose name is exactly the
    product id, created on demand when the board has none.  Nothing about the board takes part in
    the choice - not the order of the lanes, not which lane happens to be first, not whether a
    `Default swimlane` exists - so every writer, every retry and every restore of the same record
    choose the same lane.  Project bindings take no part either, which is why a product bound to
    several projects (`codegen`, `secretary`) is not ambiguous here: `issue_product`, and for a
    Product its own id, stay the single source of truth about what a record belongs to.

    This is the one implementation of that rule; both secretarial writers call it, and the
    reconcile command that repairs an existing row's placement takes its destination from here.
    """
    return ensure_swimlane(client, board_id, product_lane_name(product))


class ProductIssueStore:
    def __init__(self, client: KanboardClient, *, data_dir: str | Path, instance: str | Path) -> None:
        self.client = client
        self.data_dir = Path(data_dir)
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
        return all_project_cards(self.client, board_id)

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

    def reconcile_lanes(self, *, apply: bool = False) -> dict[str, Any]:
        """Report, and with ``apply`` perform, the lane repair of existing Product/Issue rows."""
        from secretary.product_lanes import reconcile_product_lanes

        return reconcile_product_lanes(self, apply=apply)

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
        marker = self._create_marker(document)
        matches = [card for card in self._cards() if card.get("description") == marker]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise TaskError("backend_error", "pending Product/Issue create correlation is ambiguous", 1)
        raise TaskError("backend_error", "pending Product/Issue row was not found", 1)

    @staticmethod
    def _remember_task_id(document: dict[str, Any], task_id: int) -> int:
        event = document["event"]
        event["task_id"] = f"task_kanboard_{task_id}"
        event["backend"] = {"kind": "kanboard", "task_id": task_id, "revision": "product-issue"}
        document.setdefault("progress", {})["task_id"] = task_id
        return task_id

    def _remember_card(self, document: dict[str, Any], card: dict[str, Any]) -> int:
        task_id = card.get("id")
        if not isinstance(task_id, int):
            raise TaskError("backend_error", "Kanboard returned an invalid task", 1)
        self._remember_task_id(document, task_id)
        self.transactions.save(document)
        return task_id

    @staticmethod
    def _create_marker(document: dict[str, Any]) -> str:
        request_id = document.get("request_id")
        if not isinstance(request_id, str):
            raise TaskError("audit_pending", "Product/Issue transaction has no request id", 4)
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return f"[secretary-product-issue-transaction:{digest}]"

    def _ensure_created(self, document: dict[str, Any], *, title: str, description: str, product: str) -> tuple[dict[str, Any], int]:
        try:
            card = self._transaction_card(document)
        except TaskError:
            event = document["event"]
            reference = str(event.get("ref") or "")
            if not reference:
                raise
            board_id, column_id = self._board()
            # The lane is provisioned before this attempt claims the uncertain write window, in
            # the same order the typed writer keeps it: the transaction is staged and retryable
            # already, and this writer's retry re-runs the whole step, so a death right here costs
            # at most an empty lane the next attempt reuses.
            swimlane_id = product_swimlane_id(self.client, board_id, product)
            document.setdefault("progress", {})["create_started"] = True
            self.transactions.save(document)
            task_id = _positive_int(self.client.call(
                "createTask", project_id=board_id, title=title, description=self._create_marker(document),
                column_id=column_id, swimlane_id=swimlane_id, reference=reference,
            ))
            if task_id is None:
                # Kanboard answers a refused create with `false`, and that refusal is
                # deterministic: the same call is refused again, so a retry can never finish this
                # transaction.  Once the board shows no row of this request, the attempt wrote
                # nothing, its progress marker is taken back and the failure is terminal, which
                # lets the caller drop the transaction instead of blocking checkpoint with it.
                marker = self._create_marker(document)
                if any(row.get("description") == marker for row in self._cards()):
                    raise TaskError("backend_error", "Kanboard rejected the Product/Issue row", 1)
                document["progress"].pop("create_started", None)
                self.transactions.save(document)
                raise TaskError("backend_rejected", "Kanboard refused the Product/Issue row", 1)
            self._remember_task_id(document, task_id)
            self.transactions.save(document)
            card = next((row for row in self._cards() if row.get("id") == task_id), None)
            if not isinstance(card, dict):
                raise TaskError("backend_error", "created Product/Issue row was not found", 1)
        reference = str(document["event"].get("ref") or "")
        if str(card.get("reference") or "") != reference or card.get("description") != description:
            document.setdefault("progress", {})["reference_started"] = True
            self.transactions.save(document)
            if not self.client.call("updateTask", id=int(card["id"]), reference=reference, description=description):
                raise TaskError("backend_error", "Kanboard rejected Product/Issue reference", 1)
            card = self._transaction_card(document)
            if not isinstance(card, dict) or str(card.get("reference") or "") != reference or card.get("description") != description:
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
        # The lane comes from the record's own product: a Product's id, an Issue's `issue_product`.
        # The staged intent already carries it, so a retry of this transaction resolves the same
        # lane as its first attempt did.
        is_product = intent["record_type"] == PRODUCT_TYPE
        card, task_id = self._ensure_created(
            document, title=str(intent["title"]), description=str(intent["description"]),
            product=str(intent["product_id"] if is_product else intent["product"]),
        )
        if is_product:
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
        card, _ = self._find(str(intent["reference"]), ISSUE_TYPE)
        if int(card.get("is_active", 1) or 0) == 0:
            raise TaskError("closed", "cannot reprioritize a closed issue", 3)
        task_id = self._remember_card(document, card)
        payload = document.get("event", {}).get("payload")
        previous = payload.get("from") if isinstance(payload, dict) else None
        if not isinstance(previous, str):
            raise TaskError("audit_pending", "Product/Issue priority transaction has no staged previous priority", 4)
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
            if exc.code in _TERMINAL_CODES and not document.get("progress"):
                # A refusal that a retry cannot turn into a success, with nothing written to the
                # backend: keeping the staged document would block checkpoint and board export
                # for a call that already reported its own failure code.
                self.transactions.discard(document)
                raise
            raise TaskError("audit_pending", "Product/Issue write is pending repair; retry with the same request id", 4) from None
        except (OSError, KeyError, TypeError):
            raise TaskError("audit_pending", "Product/Issue write is pending repair; retry with the same request id", 4) from None

    def _begin(self, request_id: str, *, kind: str, intent: dict[str, Any], event: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return self.transactions.begin(request_id, kind=kind, intent=intent, event=event)

    def _reject_other_pending_reference_operation(self, reference: str, request_id: str) -> None:
        if self.transactions.pending_for_reference(reference, excluding=request_id):
            raise TaskError(
                "audit_pending",
                "Product/Issue operation is pending repair; retry it with its original request id first",
                4,
            )

    def _reject_other_pending_typed_operation(self, reference: str, request_id: str) -> None:
        for event in self.audit.pending_events():
            if (
                event.get("record_type") == "board.protocol_event"
                and event.get("request_id") != request_id and event.get("ref") == reference
            ):
                raise TaskError(
                    "audit_pending",
                    "Product/Issue operation is pending repair; retry it with its original request id first",
                    4,
                )

    def _finish_for(self, kind: str):
        if kind in {"product_created", "issue_created"}:
            return self._finish_create
        if kind == "issue_priority_changed":
            return self._finish_priority
        if kind == "issue_closed":
            return self._finish_close
        raise TaskError("validation", f"unsupported Product/Issue transaction kind: {kind!r}", 2)

    @staticmethod
    def _transaction_view(document: dict[str, Any]) -> dict[str, Any]:
        event = document.get("event")
        return {
            "request_id": str(document.get("request_id") or ""),
            "kind": str(document.get("kind") or ""),
            "ref": str(event.get("ref") or "") if isinstance(event, dict) else "",
            "progress": sorted(str(key) for key in (document.get("progress") or {})),
        }

    def _record_view(self, document: dict[str, Any]) -> dict[str, Any]:
        if document.get("kind") == "product_created":
            return self.show_product(str(document["intent"]["product_id"]))
        return self.show_issue(str(document["event"]["ref"]))

    def _backend_trace(self, document: dict[str, Any]) -> str:
        """What this staged transaction has already written, as an operator-readable phrase.

        Every transaction writes its own recognisable mark first: a create writes the row (with
        the request marker in the description), a priority or close change writes the comment
        that carries the request id.  An empty answer therefore means the backend has not been
        touched by this request at all.
        """
        event = document.get("event") if isinstance(document.get("event"), dict) else {}
        reference = str(event.get("ref") or "")
        if document.get("kind") in {"product_created", "issue_created"}:
            staged = (document.get("progress") or {}).get("task_id")
            if isinstance(staged, int) and any(card.get("id") == staged for card in self._cards()):
                return "the staged row is on the board"
            board_id, _ = self._board()
            if reference and isinstance(
                self.client.call("getTaskByReference", project_id=board_id, reference=reference), dict
            ):
                return "a row already carries the reference of this request"
            marker = self._create_marker(document)
            if any(card.get("description") == marker for card in self._cards()):
                return "a row already carries the create marker of this request"
            return ""
        try:
            card, _ = self._find(reference, ISSUE_TYPE)
        except TaskError as exc:
            if exc.code in {"not_found", "validation"}:
                return ""
            raise
        stamp = f"[request-id:{document.get('request_id')}]"
        comments = self.client.call("getAllComments", task_id=int(card["id"])) or []
        if any(isinstance(comment, dict) and stamp in str(comment.get("comment") or "") for comment in comments):
            return "the board comment of this request is on the issue"
        return ""

    def list_transactions(self) -> list[dict[str, Any]]:
        legacy = [self._transaction_view(document) for document in self.transactions.documents()]
        typed = []
        for event in self.audit.pending_events():
            subject = event.get("subject")
            if (
                event.get("record_type") == "board.protocol_event"
                and isinstance(subject, dict)
                and subject.get("kind") in {PRODUCT_TYPE, ISSUE_TYPE}
                and isinstance(event.get("request_id"), str)
                and isinstance(event.get("kind"), str)
                and isinstance(subject.get("ref"), str)
            ):
                typed.append({
                    "request_id": event["request_id"], "kind": event["kind"],
                    "ref": subject["ref"], "progress": ["typed_event"],
                })
        return sorted(legacy + typed, key=lambda item: item["request_id"])

    def adopt_transaction(self, path: str | Path) -> dict[str, Any]:
        return self._transaction_view(self.transactions.adopt(path))

    def retry_transaction(self, request_id: str) -> dict[str, Any]:
        try:
            staged = self.transactions.load(request_id)
        except TaskError as exc:
            if exc.code != "not_found":
                raise
            try:
                result = self._host().recover_product_issue(request_id)
            except Exception as recovery:
                raise self._host_error(recovery) from None
            if result.entity.kind.value == PRODUCT_TYPE:
                return self.show_product(result.entity.ref.removeprefix("product:"))
            return self.show_issue(result.entity.ref)
        kind = str(staged.get("kind") or "")
        finish = self._finish_for(kind)
        intent = staged["intent"]
        self.audit.require_pending_layout()
        event = staged.get("event") if isinstance(staged.get("event"), dict) else {}
        with self.transactions.reference_lock(str(event.get("ref") or "")) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                document, completed = self.transactions.existing(request_id, kind=kind, intent=intent)
                if completed is not None:
                    return self._record_view(staged)
                if document is None:
                    raise TaskError("not_found", "no staged Product/Issue transaction has that request id", 2)
                self._complete_transaction(document, finish)
                return self._record_view(document)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def discard_transaction(self, request_id: str) -> dict[str, Any]:
        try:
            document = self.transactions.load(request_id)
        except TaskError as exc:
            if exc.code != "not_found":
                raise
            # New occurrences are owned by BoardEventCanon.  This released
            # generic/legacy operator surface must never discard or otherwise
            # alter that owner, even when the backend cannot be inspected.
            pending = self.audit.pending_event(request_id)
            if isinstance(pending, dict) and pending.get("record_type") == "board.protocol_event":
                raise TaskError(
                    "live_write",
                    "a typed Product/Issue occurrence owns that request; retry it instead of discarding it",
                    3,
                ) from None
            raise
        self._finish_for(str(document.get("kind") or ""))
        event = document.get("event") if isinstance(document.get("event"), dict) else {}
        with self.transactions.reference_lock(str(event.get("ref") or "")) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if self.audit.committed_event(request_id) is not None:
                    raise TaskError("validation", "that request is already committed; retry it instead", 2)
                trace = self._backend_trace(document)
                if trace:
                    raise TaskError(
                        "live_write",
                        f"{trace}; retry the transaction with its request id instead of discarding it",
                        3,
                    )
                self.transactions.drop(request_id)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return {"request_id": request_id, "discarded": True}

    def _host(self):
        # Deliberately local: the Kanboard adapter retains the compatibility
        # reader above, while new writer calls enter it through BoardHost.
        from secretary.board.kanboard import KanboardBoardHost
        return KanboardBoardHost(self.client, data_dir=self.data_dir, instance=self.instance, audit=self.audit)

    @staticmethod
    def _host_error(exc: Exception) -> TaskError:
        from secretary.board.events import BoardEventPending
        from secretary.board.transitions import BoardProtocolError
        if isinstance(exc, BoardEventPending):
            return TaskError("audit_pending", "Product/Issue write is pending repair; retry with the same request id", 4)
        if isinstance(exc, TaskError):
            return exc
        if isinstance(exc, BoardProtocolError) and (
            "Kanboard refused" in str(exc) or "Kanboard rejected" in str(exc)
        ):
            return TaskError("backend_rejected", str(exc), 1)
        return TaskError("validation", str(exc), 2)

    def create_product(self, *, product_id: str, projects: list[str], title: str, description: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if not _ID.fullmatch(product_id) or not title.strip() or not projects or len(set(projects)) != len(projects):
            raise TaskError("validation", "product needs a valid id, title and non-empty unique project set", 2)
        reference = f"product:{product_id}"
        request_id = request_id or str(uuid.uuid4())
        with self.transactions.reference_lock(reference) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._reject_other_pending_reference_operation(reference, request_id)
                self._reject_other_pending_typed_operation(reference, request_id)
                host = self._host()
                try:
                    claimed = host.canon.event(request_id) if host.canon is not None else None
                except ValueError:
                    claimed = True
                if not claimed:
                    unknown = sorted(set(projects) - registered_projects(self.instance))
                    if unknown:
                        raise TaskError("validation", "unknown registered project(s): " + ", ".join(unknown), 2)
                from secretary.board import Actor, Create, Product
                try:
                    host.create(Create(Product(reference, title, projects=tuple(sorted(projects)), description=description), Actor("po", actor), "Product created", request_id=request_id))
                except Exception as exc:
                    raise self._host_error(exc) from None
                return self.show_product(product_id)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def create_issue(self, *, product: str, issue_kind: str, priority: str, title: str, description: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if not product.strip() or issue_kind not in ISSUE_KINDS or priority not in ISSUE_PRIORITIES or not title.strip():
            raise TaskError("validation", "issue requires title, product, kind (bug|feature|question|improvement) and priority (P0-P3)", 2)
        request_id = request_id or str(uuid.uuid4())
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:20]
        reference = f"issue:{digest}"
        with self.transactions.reference_lock(reference) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._reject_other_pending_reference_operation(reference, request_id)
                self._reject_other_pending_typed_operation(reference, request_id)
                from secretary.board import Actor, Create, Issue
                try:
                    self._host().create(Create(
                        Issue(reference, title, f"product:{product}", priority=priority,
                              issue_kind=issue_kind, description=description),
                        Actor("po", actor), "Issue created", request_id=request_id,
                    ))
                except Exception as exc:
                    raise self._host_error(exc) from None
                return self.show_issue(reference)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def update_priority(self, *, reference: str, priority: str, reason: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if priority not in ISSUE_PRIORITIES or not reason.strip():
            raise TaskError("validation", "priority update requires P0-P3 and a non-empty reason", 2)
        request_id = request_id or str(uuid.uuid4())
        with self.transactions.reference_lock(reference) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._reject_other_pending_reference_operation(reference, request_id)
                self._reject_other_pending_typed_operation(reference, request_id)
                from secretary.board import Actor, EntityKind, Issue, Replace
                host = self._host()
                try:
                    known = host.canon.event(request_id) if host.canon is not None else None
                except ValueError as exc:
                    raise TaskError("validation", str(exc), 2) from None
                if known is not None:
                    if (
                        known.kind.value != "entity.updated"
                        or known.entity_kind is not EntityKind.ISSUE
                        or known.ref != reference
                        or known.actor != Actor("po", actor)
                        or known.reason != reason
                        or known.data.get("priority") != priority
                    ):
                        raise TaskError("validation", "request id belongs to another operation or payload", 2)
                    try:
                        host.recover_product_issue(request_id)
                    except Exception as exc:
                        raise self._host_error(exc) from None
                    return self.show_issue(reference)
                current = host.read(EntityKind.ISSUE, reference)
                if not isinstance(current, Issue):
                    raise TaskError("validation", "reference is not an Issue", 2)
                if current.state.value == "closed":
                    raise TaskError("closed", "cannot reprioritize a closed issue", 3)
                desired = Issue(current.ref, current.title, current.product_ref, current.state, priority, current.issue_kind, current.description, current.close_reason)
                try:
                    host.replace(Replace(desired, Actor("po", actor), reason, request_id=request_id))
                except Exception as exc:
                    raise self._host_error(exc) from None
                return self.show_issue(reference)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def close_issue(self, *, reference: str, reason: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if reason not in ISSUE_CLOSE_REASONS:
            raise TaskError("validation", "close reason must be one of: resolved, invalid, duplicate, wont_do", 2)
        request_id = request_id or str(uuid.uuid4())
        with self.transactions.reference_lock(reference) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._reject_other_pending_reference_operation(reference, request_id)
                self._reject_other_pending_typed_operation(reference, request_id)
                from secretary.board import Actor, EntityKind, Issue, IssueState, TransitionRequest
                current = self._host().read(EntityKind.ISSUE, reference)
                if isinstance(current, Issue) and current.state is IssueState.CLOSED:
                    try:
                        existing = self._host().canon.event(request_id) if self._host().canon is not None else None
                    except ValueError:
                        existing = None
                    if existing is None:
                        raise TaskError("closed", "issue is already closed", 3)
                try:
                    self._host().transition(TransitionRequest(EntityKind.ISSUE, reference, IssueState.CLOSED, Actor("po", actor), reason, request_id=request_id))
                except Exception as exc:
                    raise self._host_error(exc) from None
                return self.show_issue(reference)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
