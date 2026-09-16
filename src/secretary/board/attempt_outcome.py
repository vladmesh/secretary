"""Typed payload boundary for the closed ``attempt.outcome`` board-event schema."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from secretary.board.terminal_taxonomy import (
    BLOCKED_REASONS,
    TERMINAL_DISPOSITIONS,
    BlockedReason,
    TerminalDisposition,
)


class AttemptOutcomeTerminalState(StrEnum):
    """Terminal board state observed for one sealed attempt."""

    DONE = "done"
    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"


class AttemptOutcomeVerdict(StrEnum):
    """Review/report verdict projected into the attempt outcome ledger."""

    GREEN = "green"
    RED = "red"
    BLOCKED = "blocked"
    MISSING = "missing"
    LEGACY = "legacy"


class AttemptOutcomeCompleteness(StrEnum):
    """Whether one role's provider-usage evidence is complete for the attempt."""

    COLLECTED = "collected"
    DEGRADED = "degraded"
    MISSING = "missing"
    LEGACY = "legacy"


ATTEMPT_OUTCOME_VERDICTS = tuple(value.value for value in AttemptOutcomeVerdict)
ATTEMPT_OUTCOME_DISPOSITIONS = TERMINAL_DISPOSITIONS
ATTEMPT_OUTCOME_BLOCKED_REASONS = BLOCKED_REASONS
ATTEMPT_OUTCOME_COMPLETENESS = tuple(value.value for value in AttemptOutcomeCompleteness)

_SOURCE_EVENT_FIELDS = (
    "report",
    "verdict",
    "decision",
    "effect",
    "worker_usage",
    "review_usage",
)
_LINEAGE_FIELDS = (
    "specification_revision",
    "report",
    "verdict",
    "decision",
    "effect",
    "worker_usage",
    "review_usage",
)
_BASE_FIELDS = {
    "version",
    "attempt_id",
    "attempt",
    "report_generation",
    "sprint_ref",
    "specification_revision",
    "terminal_state",
    "verdict",
    "disposition",
    "blocked_reason",
    "source_event_ids",
    "usage_completeness",
}


def _optional_non_empty(value: str | None, field_name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"attempt outcome {field_name} must be a non-empty string or null")


@dataclass(frozen=True, slots=True)
class AttemptOutcomeSourceEventIds:
    """Typed lineage references carried by one attempt outcome."""

    report: str | None
    verdict: str | None
    decision: str | None
    effect: str | None
    worker_usage: str | None
    review_usage: str | None

    def __post_init__(self) -> None:
        for name in _SOURCE_EVENT_FIELDS:
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(
                    f"attempt outcome source event {name} must be a non-empty string or null"
                )

    def to_data(self) -> dict[str, str | None]:
        return {name: getattr(self, name) for name in _SOURCE_EVENT_FIELDS}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AttemptOutcomeSourceEventIds:
        if set(data) != set(_SOURCE_EVENT_FIELDS):
            raise ValueError("attempt outcome source_event_ids are incomplete")
        return cls(**{name: data[name] for name in _SOURCE_EVENT_FIELDS})


@dataclass(frozen=True, slots=True)
class AttemptOutcomeUsageCompleteness:
    """Typed worker/reviewer provider-evidence completeness."""

    worker: AttemptOutcomeCompleteness
    review: AttemptOutcomeCompleteness

    def __post_init__(self) -> None:
        if not isinstance(self.worker, AttemptOutcomeCompleteness):
            raise ValueError("attempt outcome worker completeness is unsupported")
        if not isinstance(self.review, AttemptOutcomeCompleteness):
            raise ValueError("attempt outcome review completeness is unsupported")

    def to_data(self) -> dict[str, str]:
        return {"worker": self.worker.value, "review": self.review.value}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AttemptOutcomeUsageCompleteness:
        if set(data) != {"worker", "review"}:
            raise ValueError("attempt outcome usage completeness is incomplete")
        try:
            return cls(
                worker=AttemptOutcomeCompleteness(data["worker"]),
                review=AttemptOutcomeCompleteness(data["review"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"attempt outcome completeness is unsupported: {exc}") from None


@dataclass(frozen=True, slots=True)
class AttemptOutcomeLineageRequired:
    """Which v2 forward-lineage facts are obligations rather than optional evidence."""

    specification_revision: bool
    report: bool
    verdict: bool
    decision: bool
    effect: bool
    worker_usage: bool
    review_usage: bool

    def __post_init__(self) -> None:
        for name in _LINEAGE_FIELDS:
            if not isinstance(getattr(self, name), bool):
                raise ValueError("attempt outcome lineage requiredness must be boolean")

    def to_data(self) -> dict[str, bool]:
        return {name: getattr(self, name) for name in _LINEAGE_FIELDS}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AttemptOutcomeLineageRequired:
        if set(data) != set(_LINEAGE_FIELDS):
            raise ValueError("attempt outcome lineage requiredness is incomplete")
        return cls(**{name: data[name] for name in _LINEAGE_FIELDS})


@dataclass(frozen=True, slots=True)
class AttemptOutcomePayload:
    """Immutable domain value for the released v1/v2 ``attempt.outcome`` event data."""

    version: int
    attempt_id: str
    attempt: int
    report_generation: int
    sprint_ref: str | None
    specification_revision: str | None
    terminal_state: AttemptOutcomeTerminalState
    verdict: AttemptOutcomeVerdict
    disposition: TerminalDisposition
    blocked_reason: BlockedReason | None
    source_event_ids: AttemptOutcomeSourceEventIds
    usage_completeness: AttemptOutcomeUsageCompleteness
    lineage_required: AttemptOutcomeLineageRequired | None = None

    def __post_init__(self) -> None:
        if self.version not in {1, 2} or isinstance(self.version, bool):
            raise ValueError("unsupported attempt outcome version")
        if not isinstance(self.attempt_id, str) or not self.attempt_id.strip():
            raise ValueError("attempt outcome requires a non-empty attempt_id")
        for name in ("attempt", "report_generation"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"attempt outcome requires a positive {name}")
        _optional_non_empty(self.sprint_ref, "sprint_ref")
        _optional_non_empty(self.specification_revision, "specification_revision")
        if not isinstance(self.terminal_state, AttemptOutcomeTerminalState):
            raise ValueError("attempt outcome terminal state must be done, blocked or in_progress")
        if not isinstance(self.verdict, AttemptOutcomeVerdict):
            raise ValueError("attempt outcome verdict is unsupported")
        if self.disposition not in TERMINAL_DISPOSITIONS:
            raise ValueError("attempt outcome disposition is unsupported")
        if self.blocked_reason is not None and self.blocked_reason not in BLOCKED_REASONS:
            raise ValueError("attempt outcome blocked reason is unsupported")
        if (self.disposition == "blocked") != (self.blocked_reason is not None):
            raise ValueError("attempt outcome blocked reason is present exactly for blocked disposition")
        if not isinstance(self.source_event_ids, AttemptOutcomeSourceEventIds):
            raise ValueError("attempt outcome source_event_ids are incomplete")
        if not isinstance(self.usage_completeness, AttemptOutcomeUsageCompleteness):
            raise ValueError("attempt outcome usage completeness is incomplete")
        for role in ("worker", "review"):
            state = getattr(self.usage_completeness, role)
            usage_ref = getattr(self.source_event_ids, f"{role}_usage")
            if state in {
                AttemptOutcomeCompleteness.COLLECTED,
                AttemptOutcomeCompleteness.DEGRADED,
            } and usage_ref is None:
                raise ValueError(f"attempt outcome {role} completeness requires its usage event ref")
            if state in {
                AttemptOutcomeCompleteness.MISSING,
                AttemptOutcomeCompleteness.LEGACY,
            } and usage_ref is not None:
                raise ValueError(f"attempt outcome {role} missingness carries no usage event ref")
        if self.version == 1 and self.lineage_required is not None:
            raise ValueError("attempt outcome v1 has an unsupported field set")
        if self.version == 2 and not isinstance(self.lineage_required, AttemptOutcomeLineageRequired):
            raise ValueError("attempt outcome lineage requiredness is incomplete")

    def natural_key(self, card_ref: str) -> tuple[str, str, int]:
        """Return the released ledger identity for this outcome."""
        if not isinstance(card_ref, str) or not card_ref.strip():
            raise ValueError("attempt outcome card ref must be non-empty")
        return (card_ref, self.attempt_id, self.report_generation)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "attempt_id": self.attempt_id,
            "attempt": self.attempt,
            "report_generation": self.report_generation,
            "sprint_ref": self.sprint_ref,
            "specification_revision": self.specification_revision,
            "terminal_state": self.terminal_state.value,
            "verdict": self.verdict.value,
            "disposition": self.disposition,
            "blocked_reason": self.blocked_reason,
            "source_event_ids": self.source_event_ids.to_data(),
            "usage_completeness": self.usage_completeness.to_data(),
        }
        if self.version == 2:
            assert self.lineage_required is not None
            data["lineage_required"] = self.lineage_required.to_data()
        return data

    def lineage_missing(self) -> tuple[str, ...]:
        """Return missing required v2 lineage facts in released field order."""
        if self.version == 1:
            return ()
        assert self.lineage_required is not None
        missing: list[str] = []
        for name in _LINEAGE_FIELDS:
            if not getattr(self.lineage_required, name):
                continue
            value = (
                self.specification_revision
                if name == "specification_revision"
                else getattr(self.source_event_ids, name)
            )
            if value is None:
                missing.append(name)
        return tuple(missing)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AttemptOutcomePayload:
        version = data.get("version")
        if version == 1:
            expected = _BASE_FIELDS
        elif version == 2:
            expected = _BASE_FIELDS | {"lineage_required"}
        else:
            raise ValueError("unsupported attempt outcome version")
        if set(data) != expected:
            raise ValueError(f"attempt outcome v{version} has an unsupported field set")
        try:
            refs_raw = data["source_event_ids"]
            completeness_raw = data["usage_completeness"]
            if not isinstance(refs_raw, Mapping):
                raise ValueError("attempt outcome source_event_ids are incomplete")
            if not isinstance(completeness_raw, Mapping):
                raise ValueError("attempt outcome usage completeness is incomplete")
            lineage_required: AttemptOutcomeLineageRequired | None = None
            if version == 2:
                lineage_raw = data.get("lineage_required")
                if not isinstance(lineage_raw, Mapping):
                    raise ValueError("attempt outcome lineage requiredness is incomplete")
                lineage_required = AttemptOutcomeLineageRequired.from_data(lineage_raw)
            return cls(
                version=version,
                attempt_id=data["attempt_id"],
                attempt=data["attempt"],
                report_generation=data["report_generation"],
                sprint_ref=data["sprint_ref"],
                specification_revision=data["specification_revision"],
                terminal_state=AttemptOutcomeTerminalState(data["terminal_state"]),
                verdict=AttemptOutcomeVerdict(data["verdict"]),
                disposition=data["disposition"],
                blocked_reason=data["blocked_reason"],
                source_event_ids=AttemptOutcomeSourceEventIds.from_data(refs_raw),
                usage_completeness=AttemptOutcomeUsageCompleteness.from_data(completeness_raw),
                lineage_required=lineage_required,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid attempt outcome payload: {exc}") from None
