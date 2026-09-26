"""The handover fakes shared by the handover and owner-event suites (secretary-1761, secretary-1770).

`OneCardClient` is a card client whose writes land on one in-memory card, `MemoryAudit` the audit
`TaskWriter._write` uses with the claim rules of `SqlTaskAudit`, and `HandedOverFixture` a card the
dispatcher submitted to a real PO service, with what the PO's handover and the owner's comments leave on it.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from secretary.board.audit_contract import require_claim
from secretary.board.owner_handover import HANDED_TO_OWNER, mark_values, render_handover_comment
from tests.po_card_fakes import REF, DispatcherFixture, card

REASON = "Pay the relay provider: a card is needed and the owner holds it."
SINCE = "2026-09-26T15:00:00Z"


class MemoryAudit:
    """The audit `TaskWriter._write` uses, with the claim rules of `SqlTaskAudit` and nothing stored."""

    def __init__(self) -> None:
        self.committed: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.committed.get(request_id))

    def pending_event(self, request_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.pending.get(request_id))

    def require_claim(self, existing: dict[str, Any], **fields: Any) -> None:
        require_claim(existing, **fields)

    def stage(self, request_id: str, event: dict[str, Any]) -> None:
        self.pending[request_id] = copy.deepcopy(event)

    def discard(self, request_id: str, event: dict[str, Any] | None = None) -> None:
        self.pending.pop(request_id, None)

    def append(self, request_id: str, event: dict[str, Any]) -> str:
        self.pending.pop(request_id, None)
        self.committed[request_id] = copy.deepcopy(event)
        return str(event["event_id"])

    def events(self, reference: str = "", **_: Any) -> list[dict[str, Any]]:
        return [copy.deepcopy(event) for event in self.committed.values() if not reference or event["ref"] == reference]


class OneCardClient:
    """A card client whose `saveTaskMetadata` and `createComment` land on one card document."""

    def __init__(self, document: dict[str, Any], instance_dir: str) -> None:
        self.document = document
        self.instance_dir = instance_dir
        self.writes: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, **params: Any) -> Any:
        self.writes.append((method, params))
        if method == "saveTaskMetadata":
            bag = self.document.setdefault("extensions", {}).setdefault("extra", {})
            for key, value in params["values"].items():
                if value:
                    bag[key] = value
                else:
                    bag.pop(key, None)
            return True
        if method == "createComment":
            content = params["content"]
            first = content.splitlines()[0]
            self.document["comments"].append(
                {"created_at": SINCE, "body": content, "marker": first[1:-1]}
            )
            return len(self.document["comments"])
        raise AssertionError(f"unexpected board call {method}")


def decision_card(state: str = "in_progress", kind: str = "decision", **fields: Any) -> dict[str, Any]:
    return {**card(kind, state=state), "backend_task_id": 1900, "audit": {"backend": {"kind": "sql", "sql_task_id": 1900}}, **fields}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HandedOverFixture(DispatcherFixture):
    """A card the dispatcher submitted, and what the PO's handover and the owner's comments leave on it."""

    def hand_over(self, reason: str = REASON) -> None:
        """What `task handover` leaves on the card and in its audit, done by the PO inside its turn."""
        board = self.cards
        board.card.setdefault("extensions", {}).setdefault("extra", {}).update(mark_values(SINCE, reason, "po"))
        board.card["comments"].append(
            {"created_at": SINCE, "marker": "po", "body": "[po]\n" + render_handover_comment(reason)}
        )
        board.log.append({"request_id": "handover-1", "ref": REF, "kind": HANDED_TO_OWNER, "event_id": "evt-h"})

    def owner_says(self, text: str, event_id: str) -> None:
        self.cards.card["comments"].append({"created_at": _now_iso(), "marker": "owner", "body": f"[owner]\n{text}"})
        self.cards.log.append(
            {"request_id": f"req-{event_id}", "ref": REF, "kind": "commented", "event_id": event_id,
             "payload": {"marker": "owner"}}
        )

    def submitted_card(self, description: str = "Which relay do we pay for?") -> tuple[Any, str]:
        self.start()
        runtime = self.runtime(card(description=description))
        self.claim(runtime)
        session = self.record().po_submission.session_id
        self.settled(session, 2)
        return runtime, session
