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
        for column in columns:
            if isinstance(column, dict) and column.get("title") == ISSUES_COLUMN and isinstance(column.get("id"), int):
                return board["id"], column["id"]
        raise TaskError("legacy_layout", "Pipeline first column is not Issues; run the supported board migration", 2)

    def _cards(self, *, active: int = 0) -> list[dict[str, Any]]:
        board_id, _ = self._board()
        cards = self.client.call("getAllTasks", project_id=board_id, status_id=active) or []
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

    def _event(self, *, kind: str, role: str, actor: str, reference: str, task_id: int, request_id: str, payload: dict[str, Any]) -> None:
        event = {"event_id": "evt_" + uuid.uuid4().hex, "schema_version": 1, "occurred_at": _now(), "actor": {"role": role, "id": actor}, "kind": kind, "outcome": "success", "task_id": f"task_kanboard_{task_id}", "ref": reference, "backend": {"kind": "kanboard", "task_id": task_id, "revision": "product-issue"}, "request_id": request_id, "payload": payload}
        self.audit.stage(request_id, event)
        try:
            self.audit.append(request_id, event)
        except OSError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None

    def create_product(self, *, product_id: str, projects: list[str], title: str, description: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if not _ID.fullmatch(product_id):
            raise TaskError("validation", "product id must match [a-z0-9][a-z0-9-]{0,62}", 2)
        if not title.strip() or not projects or len(set(projects)) != len(projects):
            raise TaskError("validation", "product needs a title and a non-empty unique project set", 2)
        unknown = sorted(set(projects) - registered_projects(self.instance))
        if unknown:
            raise TaskError("validation", "unknown registered project(s): " + ", ".join(unknown), 2)
        ref = f"product:{product_id}"
        board_id, column_id = self._board()
        if self.client.call("getTaskByReference", project_id=board_id, reference=ref):
            raise TaskError("validation", f"product {product_id!r} already exists", 2)
        task_id = self.client.call("createTask", project_id=board_id, title=title, description=description, column_id=column_id, swimlane_id=0)
        if not isinstance(task_id, int):
            raise TaskError("backend_error", "Kanboard rejected the product", 1)
        if not self.client.call("updateTask", id=task_id, reference=ref):
            raise TaskError("backend_error", "Kanboard rejected the product reference", 1)
        self.client.call("saveTaskMetadata", task_id=task_id, values={META_RECORD_TYPE: PRODUCT_TYPE, META_PRODUCT_ID: product_id, META_PRODUCT_PROJECTS: json.dumps(sorted(projects), separators=(",", ":"))})
        self._event(kind="product_created", role="po", actor=actor, reference=ref, task_id=task_id, request_id=request_id or str(uuid.uuid4()), payload={"product_id": product_id, "projects": sorted(projects)})
        return self.show_product(product_id)

    def list_products(self) -> list[dict[str, Any]]:
        return sorted((self._view(card, meta) for card in self._cards() if (meta := self._metadata(card)).get(META_RECORD_TYPE) == PRODUCT_TYPE), key=lambda item: str(item["id"]))

    def show_product(self, product_id: str) -> dict[str, Any]:
        card, meta = self._find(f"product:{product_id}", PRODUCT_TYPE)
        return self._view(card, meta)

    def create_issue(self, *, product: str, issue_kind: str, priority: str, title: str, description: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if issue_kind not in ISSUE_KINDS or priority not in ISSUE_PRIORITIES or not title.strip():
            raise TaskError("validation", "issue requires title, product, kind (bug|feature|question|improvement) and priority (P0-P3)", 2)
        self.show_product(product)
        board_id, column_id = self._board()
        task_id = self.client.call("createTask", project_id=board_id, title=title, description=description, column_id=column_id, swimlane_id=0)
        if not isinstance(task_id, int):
            raise TaskError("backend_error", "Kanboard rejected the issue", 1)
        ref = f"issue:{task_id}"
        if not self.client.call("updateTask", id=task_id, reference=ref):
            raise TaskError("backend_error", "Kanboard rejected the issue reference", 1)
        self.client.call("saveTaskMetadata", task_id=task_id, values={META_RECORD_TYPE: ISSUE_TYPE, META_ISSUE_PRODUCT: product, META_ISSUE_KIND: issue_kind, META_ISSUE_PRIORITY: priority})
        self._event(kind="issue_created", role="po", actor=actor, reference=ref, task_id=task_id, request_id=request_id or str(uuid.uuid4()), payload={"product": product, "kind": issue_kind, "priority": priority})
        return self.show_issue(ref)

    def list_issues(self, *, product: str | None = None, include_closed: bool = False) -> list[dict[str, Any]]:
        cards = self._cards(active=0 if include_closed else 1)
        result = []
        for card in cards:
            meta = self._metadata(card)
            if meta.get(META_RECORD_TYPE) == ISSUE_TYPE and (product is None or meta.get(META_ISSUE_PRODUCT) == product):
                result.append(self._view(card, meta))
        return sorted(result, key=lambda item: (item["priority"], item["ref"]))

    def show_issue(self, reference: str) -> dict[str, Any]:
        card, meta = self._find(reference, ISSUE_TYPE)
        return self._view(card, meta)

    def update_priority(self, *, reference: str, priority: str, reason: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if priority not in ISSUE_PRIORITIES or not reason.strip():
            raise TaskError("validation", "priority update requires P0-P3 and a non-empty reason", 2)
        card, meta = self._find(reference, ISSUE_TYPE)
        if int(card.get("is_active", 1) or 0) == 0:
            raise TaskError("closed", "cannot reprioritize a closed issue", 3)
        task_id = int(card["id"])
        previous = meta.get(META_ISSUE_PRIORITY, "")
        self.client.call("saveTaskMetadata", task_id=task_id, values={META_ISSUE_PRIORITY: priority})
        self.client.call("createComment", task_id=task_id, user_id=0, content=f"[issue:priority]\n{reason}")
        self._event(kind="issue_priority_changed", role="po", actor=actor, reference=reference, task_id=task_id, request_id=request_id or str(uuid.uuid4()), payload={"from": previous, "to": priority, "reason": reason})
        return self.show_issue(reference)

    def close_issue(self, *, reference: str, reason: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        if reason not in ISSUE_CLOSE_REASONS:
            raise TaskError("validation", "close reason must be one of: resolved, invalid, duplicate, wont_do", 2)
        card, _ = self._find(reference, ISSUE_TYPE)
        task_id = int(card["id"])
        if int(card.get("is_active", 1) or 0) != 0:
            self.client.call("saveTaskMetadata", task_id=task_id, values={META_ISSUE_CLOSED_REASON: reason})
            self.client.call("createComment", task_id=task_id, user_id=0, content=f"[issue:closed]\n{reason}")
            if not self.client.call("closeTask", task_id=task_id):
                raise TaskError("backend_error", "Kanboard rejected issue closure", 1)
        self._event(kind="issue_closed", role="po", actor=actor, reference=reference, task_id=task_id, request_id=request_id or str(uuid.uuid4()), payload={"reason": reason})
        return self.show_issue(reference)
