"""Typed durable hand-off for attempt outcome round identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self


class OutcomeRoundPhase(StrEnum):
    """A point whose exact round/source identity is handed to outcome freezing."""

    WORKER = "worker"
    REVIEW = "review"
    DECISION = "decision"
    REPORT = "report"
    VERDICT = "verdict"


_V1_PHASES = frozenset(
    {OutcomeRoundPhase.WORKER, OutcomeRoundPhase.REVIEW, OutcomeRoundPhase.DECISION}
)
_V2_PHASES = frozenset(OutcomeRoundPhase)
_V1_SOURCE_PHASES = frozenset({OutcomeRoundPhase.DECISION})
_V2_SOURCE_PHASES = frozenset(
    {OutcomeRoundPhase.REPORT, OutcomeRoundPhase.VERDICT, OutcomeRoundPhase.DECISION}
)
_V1_FIELDS = frozenset(
    {
        "version",
        "phase",
        "attempt_id",
        "attempt",
        "report_generation",
        "request_ids",
        "assessment_visit",
        "source_event_id",
    }
)
_V2_FIELDS = _V1_FIELDS | frozenset({"round_id", "specification_revision", "marker"})


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"outcome round context needs a positive {name}")
    return value


def _string(value: object, message: str) -> str:
    if not isinstance(value, str):
        raise ValueError(message)
    return value


@dataclass(frozen=True, slots=True)
class OutcomeRoundContext:
    """Immutable value for the released v1/v2 outcome-round hand-off document.

    The journal dictionary is a compatibility boundary. Domain code carries this
    value; ``to_data`` is the only place that projects it back to the historical
    field set.
    """

    version: int
    phase: OutcomeRoundPhase
    attempt_id: str
    attempt: int
    report_generation: int
    request_ids: tuple[str, ...]
    assessment_visit: str = ""
    source_event_id: str = ""
    round_id: str = ""
    specification_revision: str | None = None
    marker: str = ""

    def __post_init__(self) -> None:
        if self.version not in {1, 2}:
            raise ValueError("outcome round context has an unsupported field set")
        phases = _V1_PHASES if self.version == 1 else _V2_PHASES
        if self.phase not in phases:
            raise ValueError("outcome round context has an unsupported phase")
        if not self.attempt_id.strip():
            raise ValueError("outcome round context needs an attempt id")
        _positive_int(self.attempt, "attempt")
        _positive_int(self.report_generation, "report_generation")
        if (
            not self.request_ids
            or any(not isinstance(value, str) or not value.strip() for value in self.request_ids)
            or len(set(self.request_ids)) != len(self.request_ids)
        ):
            raise ValueError("outcome round context needs unique request ids")
        if self.phase is OutcomeRoundPhase.DECISION and not self.assessment_visit:
            raise ValueError("decision outcome round context needs an Assessment visit")
        if self.phase is not OutcomeRoundPhase.DECISION and self.assessment_visit:
            raise ValueError("only decision outcome round context has an Assessment visit")

        source_phases = _V1_SOURCE_PHASES if self.version == 1 else _V2_SOURCE_PHASES
        if self.phase in source_phases and not self.source_event_id:
            raise ValueError("source outcome round context needs its source event id")
        if self.phase not in source_phases and self.source_event_id:
            raise ValueError("only source outcome round context has a source event id")

        if self.version == 1:
            if self.round_id or self.specification_revision is not None or self.marker:
                raise ValueError("outcome round context has an unsupported field set")
            return
        if not self.round_id.strip():
            raise ValueError("outcome round context needs a stable round id")
        if self.specification_revision is not None and not self.specification_revision.strip():
            raise ValueError(
                "outcome round context specification revision must be a string or null"
            )
        if self.phase in _V2_SOURCE_PHASES and not self.marker:
            raise ValueError("source outcome round context needs its marker")
        if self.phase not in _V2_SOURCE_PHASES and self.marker:
            raise ValueError("only source outcome round context has a marker")

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> Self:
        """Normalize one released wire document without accepting additive fields."""
        version_value = data.get("version")
        expected = _V2_FIELDS if version_value == 2 else _V1_FIELDS
        if set(data) != expected or version_value not in {1, 2}:
            raise ValueError("outcome round context has an unsupported field set")
        version = int(version_value)

        phase_value = data.get("phase")
        try:
            phase = OutcomeRoundPhase(phase_value) if isinstance(phase_value, str) else None
        except ValueError:
            phase = None
        phases = _V1_PHASES if version == 1 else _V2_PHASES
        if phase is None or phase not in phases:
            raise ValueError("outcome round context has an unsupported phase")

        round_id = ""
        specification_revision: str | None = None
        marker = ""
        if version == 2:
            round_id = _string(
                data.get("round_id"), "outcome round context needs a stable round id"
            )
            if not round_id.strip():
                raise ValueError("outcome round context needs a stable round id")
            revision = data.get("specification_revision")
            if revision is not None and (not isinstance(revision, str) or not revision.strip()):
                raise ValueError(
                    "outcome round context specification revision must be a string or null"
                )
            specification_revision = revision
            marker = _string(
                data.get("marker"), "outcome round context marker must be a string"
            )

        attempt_id = _string(
            data.get("attempt_id"), "outcome round context needs an attempt id"
        )
        if not attempt_id.strip():
            raise ValueError("outcome round context needs an attempt id")
        attempt = _positive_int(data.get("attempt"), "attempt")
        report_generation = _positive_int(
            data.get("report_generation"), "report_generation"
        )

        request_ids_value = data.get("request_ids")
        if not isinstance(request_ids_value, list):
            raise ValueError("outcome round context needs unique request ids")
        request_ids = tuple(request_ids_value)
        if (
            not request_ids
            or any(not isinstance(value, str) or not value.strip() for value in request_ids)
            or len(set(request_ids)) != len(request_ids)
        ):
            raise ValueError("outcome round context needs unique request ids")

        assessment_visit = _string(
            data.get("assessment_visit"),
            "outcome round context assessment visit must be a string",
        )
        source_event_id = _string(
            data.get("source_event_id"),
            "outcome round context source event id must be a string",
        )

        return cls(
            version=version,
            phase=phase,
            attempt_id=attempt_id,
            attempt=attempt,
            report_generation=report_generation,
            request_ids=request_ids,
            assessment_visit=assessment_visit,
            source_event_id=source_event_id,
            round_id=round_id,
            specification_revision=specification_revision,
            marker=marker,
        )

    def to_data(self) -> dict[str, Any]:
        """Project the exact historical journal dictionary for this version."""
        data: dict[str, Any] = {
            "version": self.version,
            "phase": self.phase.value,
            "attempt_id": self.attempt_id,
            "attempt": self.attempt,
            "report_generation": self.report_generation,
            "request_ids": list(self.request_ids),
            "assessment_visit": self.assessment_visit,
            "source_event_id": self.source_event_id,
        }
        if self.version == 2:
            data.update(
                {
                    "round_id": self.round_id,
                    "specification_revision": self.specification_revision,
                    "marker": self.marker,
                }
            )
        return data
