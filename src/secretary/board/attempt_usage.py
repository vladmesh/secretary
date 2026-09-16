"""Typed payload boundary for the closed ``attempt.usage`` board-event schema."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from secretary.board.models import TOKEN_DIMENSIONS, AttemptUsageOutcome
from secretary.board.roles import Role


class AttemptUsagePhase(StrEnum):
    """The lifecycle phase whose provider usage one occurrence accounts for."""

    WORKER = "worker"
    REVIEW = "review"


_ATTEMPT_USAGE_ROLES = frozenset({Role.WORKER, Role.REVIEWER})


@dataclass(frozen=True, slots=True)
class TokenAccount:
    """One canonical token account carried by an attempt-usage occurrence."""

    input: int | None = None
    cache_input: int | None = None
    cache_read_input: int | None = None
    output: int | None = None
    reasoning: int | None = None

    def __post_init__(self) -> None:
        for name in TOKEN_DIMENSIONS:
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(
                    f"attempt usage token dimension {name} must be a non-negative integer or null"
                )
        if self.output is not None and self.reasoning is not None and self.reasoning > self.output:
            raise ValueError("attempt usage reasoning must be contained in output")

    def to_data(self) -> dict[str, int | None]:
        return {name: getattr(self, name) for name in TOKEN_DIMENSIONS}

    @classmethod
    def from_data(cls, source: Mapping[str, Any]) -> TokenAccount:
        if set(source) != set(TOKEN_DIMENSIONS):
            raise ValueError("attempt usage token account must carry exactly the declared dimensions")
        return cls(**{name: source[name] for name in TOKEN_DIMENSIONS})

    @property
    def empty(self) -> bool:
        return all(getattr(self, name) is None for name in TOKEN_DIMENSIONS)


@dataclass(frozen=True, slots=True)
class AttemptUsagePayload:
    """Immutable domain value for the released ``attempt.usage`` event data."""

    attempt: int
    attempt_id: str
    phase: AttemptUsagePhase
    role: Role
    report_generation: int
    head: str
    adapter: str
    model: str
    model_source: str
    session_id: str | None
    session_id_reason: str
    launch_id: str
    outcome: AttemptUsageOutcome
    detail: str
    source_kind: str
    records: int
    skipped_records: int
    tokens: TokenAccount
    session_totals: TokenAccount
    phase_baseline: TokenAccount

    def __post_init__(self) -> None:
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("attempt usage requires a positive attempt")
        if (
            isinstance(self.report_generation, bool)
            or not isinstance(self.report_generation, int)
            or self.report_generation < 1
        ):
            raise ValueError("attempt usage requires a positive report generation")
        if not isinstance(self.attempt_id, str) or not self.attempt_id.strip():
            raise ValueError("attempt usage requires a non-empty attempt_id")
        if not isinstance(self.adapter, str) or not self.adapter.strip():
            raise ValueError("attempt usage requires a non-empty adapter")
        if not isinstance(self.phase, AttemptUsagePhase):
            raise ValueError("attempt usage phase is unsupported")
        if not isinstance(self.role, Role) or self.role not in _ATTEMPT_USAGE_ROLES:
            raise ValueError("attempt usage role must be worker or reviewer")
        if not isinstance(self.model, str) or not isinstance(self.model_source, str) or not self.model_source:
            raise ValueError("attempt usage carries a model string and where it was resolved")
        for name in ("head", "session_id_reason", "launch_id", "detail", "source_kind"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"attempt usage {name} must be a string")
        if self.session_id is not None and (
            not isinstance(self.session_id, str) or not self.session_id.strip()
        ):
            raise ValueError("attempt usage session id must be a non-empty string or null")
        if self.session_id is None and not self.session_id_reason.strip():
            raise ValueError("an absent attempt usage session id must record why it is absent")
        for name in ("records", "skipped_records"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"attempt usage {name} must be a non-negative integer")
        for name in ("tokens", "session_totals", "phase_baseline"):
            if not isinstance(getattr(self, name), TokenAccount):
                raise ValueError(f"attempt usage {name} must be a TokenAccount")

        if self.outcome is AttemptUsageOutcome.COLLECTED:
            if self.session_totals.empty:
                raise ValueError("a collected attempt usage outcome reports at least one session total")
            for name in TOKEN_DIMENSIONS:
                owned = getattr(self.tokens, name)
                total = getattr(self.session_totals, name)
                start = getattr(self.phase_baseline, name)
                if owned is None and start is None:
                    continue
                if owned is None or start is None:
                    raise ValueError(
                        f"attempt usage dimension {name} attributes its interval and its baseline together"
                    )
                if total is None:
                    raise ValueError(
                        f"attempt usage dimension {name} attributes an interval with no session total"
                    )
                if total < start:
                    raise ValueError(
                        f"attempt usage dimension {name} has a session total below its baseline"
                    )
                if owned != total - start:
                    raise ValueError(
                        f"attempt usage dimension {name} does not match its session-total interval"
                    )
        elif not self.tokens.empty or not self.session_totals.empty or not self.phase_baseline.empty:
            raise ValueError("a degraded attempt usage outcome reports no token totals")

    def to_data(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "attempt_id": self.attempt_id,
            "phase": self.phase.value,
            "role": self.role.value,
            "report_generation": self.report_generation,
            "head": self.head,
            "adapter": self.adapter,
            "model": self.model,
            "model_source": self.model_source,
            "session_id": self.session_id,
            "session_id_reason": self.session_id_reason,
            "launch_id": self.launch_id,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "source_kind": self.source_kind,
            "records": self.records,
            "skipped_records": self.skipped_records,
            "tokens": self.tokens.to_data(),
            "session_totals": self.session_totals.to_data(),
            "phase_baseline": self.phase_baseline.to_data(),
        }

    def reason(self) -> str:
        line = (
            f"attempt.usage: {self.role.value} phase of attempt {self.attempt} "
            f"(round {self.report_generation}) on {self.adapter}: {self.outcome.value}"
        )
        return f"{line} — {self.detail}" if self.detail else line

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AttemptUsagePayload:
        required = {
            "attempt",
            "attempt_id",
            "phase",
            "role",
            "report_generation",
            "head",
            "adapter",
            "model",
            "model_source",
            "session_id",
            "session_id_reason",
            "launch_id",
            "outcome",
            "detail",
            "source_kind",
            "records",
            "skipped_records",
            "tokens",
            "session_totals",
            "phase_baseline",
        }
        if set(data) != required:
            raise ValueError("attempt usage payload has an unsupported field set")
        try:
            tokens_raw = data["tokens"]
            totals_raw = data["session_totals"]
            baseline_raw = data["phase_baseline"]
            if not all(isinstance(value, Mapping) for value in (tokens_raw, totals_raw, baseline_raw)):
                raise ValueError("attempt usage accounts must be objects")
            return cls(
                attempt=data["attempt"],
                attempt_id=data["attempt_id"],
                phase=AttemptUsagePhase(data["phase"]),
                role=Role(data["role"]),
                report_generation=data["report_generation"],
                head=data["head"],
                adapter=data["adapter"],
                model=data["model"],
                model_source=data["model_source"],
                session_id=data["session_id"],
                session_id_reason=data["session_id_reason"],
                launch_id=data["launch_id"],
                outcome=AttemptUsageOutcome(data["outcome"]),
                detail=data["detail"],
                source_kind=data["source_kind"],
                records=data["records"],
                skipped_records=data["skipped_records"],
                tokens=TokenAccount.from_data(tokens_raw),
                session_totals=TokenAccount.from_data(totals_raw),
                phase_baseline=TokenAccount.from_data(baseline_raw),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid attempt usage payload: {exc}") from None
