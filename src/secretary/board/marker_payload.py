"""Typed payloads for the three control-plane Card marker events.

The durable board-event wire format remains a dictionary.  This module owns the
closed marker schemas so callers can normalize once at the host boundary and
serialize only when an event is staged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, TypeAlias


@dataclass(frozen=True, slots=True)
class ReportPayload:
    status: str
    body: str
    classification: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"done", "blocked"}:
            raise ValueError("Card report status must be done or blocked")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("Card report body must be non-empty")
        if self.status == "blocked":
            if self.classification not in {"external_fact", "wrong_task_definition"}:
                raise ValueError("blocked Card report requires a supported classification")
        elif self.classification is not None:
            raise ValueError("done Card report carries no classification")

    @property
    def marker(self) -> str:
        return f"report:{self.status}"

    def to_event_data(self) -> dict[str, object]:
        return {
            "marker": self.marker,
            "body": self.body,
            "status": self.status,
            "classification": self.classification,
        }


@dataclass(frozen=True, slots=True)
class VerdictPayload:
    status: str
    body: str

    def __post_init__(self) -> None:
        if self.status not in {"green", "red"}:
            raise ValueError("Card verdict status must be green or red")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("Card verdict body must be non-empty")

    @property
    def marker(self) -> str:
        return f"review:{self.status}"

    def to_event_data(self) -> dict[str, object]:
        return {"marker": self.marker, "body": self.body, "status": self.status}


@dataclass(frozen=True, slots=True)
class DecisionPayload:
    decision: str
    body: str
    protocol_prerequisites: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decision not in {"release", "rework", "reslice"}:
            raise ValueError("Card decision must be release, rework or reslice")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("Card decision body must be non-empty")
        if any(not isinstance(value, str) or not value for value in self.protocol_prerequisites):
            raise ValueError("Card decision prerequisites must be non-empty strings")
        if len(set(self.protocol_prerequisites)) != len(self.protocol_prerequisites):
            raise ValueError("Card decision prerequisites must be unique")

    @property
    def marker(self) -> str:
        return f"decision:{self.decision}"

    def to_event_data(self) -> dict[str, object]:
        return {
            "marker": self.marker,
            "body": self.body,
            "decision": self.decision,
            "protocol_prerequisites": list(self.protocol_prerequisites),
        }


MarkerPayload: TypeAlias = ReportPayload | VerdictPayload | DecisionPayload


def marker_payload_from_data(kind: str, reason: str, data: Mapping[str, Any]) -> MarkerPayload:
    """Normalize the closed marker schema while tolerating unrelated event evidence.

    Event dictionaries may additionally carry request/recovery evidence such as
    ``request_related_refs``, ``marker_occurrence`` or ``assessment_visit``.
    Those keys remain owned by the event/adapter layer and are intentionally not
    part of these domain payloads.
    """
    body = data.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("control-plane marker events require a non-empty body")
    if reason != body:
        raise ValueError("control-plane marker event reason must match its body")

    if kind == "card.reported":
        status = data.get("status")
        classification = data.get("classification")
        if not isinstance(status, str):
            raise ValueError("Card report event has an unsupported marker payload")
        payload = ReportPayload(status, body, classification if isinstance(classification, str) else None)
    elif kind == "card.verdict":
        status = data.get("status")
        if not isinstance(status, str):
            raise ValueError("Card verdict event has an unsupported marker payload")
        payload = VerdictPayload(status, body)
    elif kind == "card.decided":
        decision = data.get("decision")
        prerequisites = data.get("protocol_prerequisites", [])
        if not isinstance(decision, str) or not isinstance(prerequisites, list):
            raise ValueError("Card decision event has an unsupported marker payload")
        payload = DecisionPayload(decision, body, tuple(prerequisites))
    else:
        raise ValueError("event is not a Card marker occurrence")

    marker = data.get("marker")
    if marker != payload.marker:
        raise ValueError("Card marker event has an unsupported marker payload")
    return payload


__all__ = [
    "DecisionPayload",
    "MarkerPayload",
    "ReportPayload",
    "VerdictPayload",
    "marker_payload_from_data",
]
