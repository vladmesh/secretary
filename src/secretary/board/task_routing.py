"""Typed task routing vocabulary and legacy metadata boundary.

The board wire formats remain strings.  This module owns the closed task-routing
vocabularies and converts legacy Kanboard metadata into typed immutable values
before the rest of the product consumes it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from secretary.board.legacy_codec import nonnegative_int, null_if_empty, split_heads, text
from secretary.board.models import CardState


class TaskType(StrEnum):
    CODE = "code"
    RESEARCH = "research"


class TaskComplexity(StrEnum):
    CHEAP = "cheap"
    STANDARD = "standard"
    HARD = "hard"
    FRONTIER = "frontier"


class FamilyPreference(StrEnum):
    AUTO = "auto"
    CLAUDE = "claude"
    CODEX = "codex"


class RoutingPhase(StrEnum):
    WORKER = "worker"
    REVIEW = "review"
    VERDICT = "verdict"


class BlockClassification(StrEnum):
    EXTERNAL_FACT = "external_fact"
    WRONG_TASK_DEFINITION = "wrong_task_definition"


class TaskDecision(StrEnum):
    RELEASE = "release"
    REWORK = "rework"
    RESLICE = "reslice"


TASK_TYPE_VALUES: frozenset[str] = frozenset(member.value for member in TaskType)
TASK_COMPLEXITY_VALUES: frozenset[str] = frozenset(member.value for member in TaskComplexity)
FAMILY_PREFERENCE_VALUES: frozenset[str] = frozenset(member.value for member in FamilyPreference)
ROUTING_PHASE_VALUES: frozenset[str] = frozenset(member.value for member in RoutingPhase)
BLOCK_CLASSIFICATION_VALUES: tuple[str, ...] = tuple(member.value for member in BlockClassification)
DECISION_VALUES: frozenset[str] = frozenset(member.value for member in TaskDecision)

EDITABLE_STATES: frozenset[CardState] = frozenset({CardState.READY, CardState.BLOCKED})
ACTIVE_STATES: frozenset[CardState] = frozenset(
    {CardState.IN_PROGRESS, CardState.VALIDATE, CardState.ASSESSMENT}
)
DECIDED_TARGETS: frozenset[CardState] = frozenset({CardState.DONE, CardState.IN_PROGRESS})
UNDECIDED_EXITS: frozenset[CardState] = frozenset({CardState.READY, CardState.VALIDATE, CardState.ISSUES})
DECISION_TARGETS: dict[TaskDecision, CardState] = {
    TaskDecision.RELEASE: CardState.DONE,
    TaskDecision.REWORK: CardState.IN_PROGRESS,
    TaskDecision.RESLICE: CardState.BLOCKED,
}


def _complexity(value: Any) -> TaskComplexity:
    try:
        return TaskComplexity(text(value))
    except ValueError:
        return TaskComplexity.STANDARD


def _family_preference(value: Any) -> FamilyPreference:
    try:
        return FamilyPreference(text(value))
    except ValueError:
        return FamilyPreference.AUTO


@dataclass(frozen=True, slots=True)
class TaskRouting:
    """Typed routing fields projected from one task's legacy metadata."""

    complexity: TaskComplexity = TaskComplexity.STANDARD
    family_preference: FamilyPreference = FamilyPreference.AUTO
    head_override: str | None = None
    review_head_override: str | None = None
    resolved_worker_family: str | None = None
    resolved_worker_head: str | None = None
    resolved_review_family: str | None = None
    resolved_review_head: str | None = None
    routing_reason: str | None = None
    quota_snapshot_at: str | None = None
    codex_launch_mode: str | None = None

    @classmethod
    def from_legacy(cls, meta: Mapping[str, Any], *, codex_modes: Collection[str]) -> TaskRouting:
        mode = text(meta.get("codex_launch_mode"))
        return cls(
            complexity=_complexity(meta.get("complexity")),
            family_preference=_family_preference(meta.get("family_preference")),
            head_override=null_if_empty(meta.get("head")),
            review_head_override=null_if_empty(meta.get("review_head")),
            resolved_worker_family=None,
            resolved_worker_head=null_if_empty(meta.get("resolved_head")),
            resolved_review_family=None,
            resolved_review_head=null_if_empty(meta.get("resolved_review_head")),
            routing_reason=null_if_empty(meta.get("routing_reason")),
            quota_snapshot_at=null_if_empty(meta.get("quota_snapshot_at")),
            codex_launch_mode=mode if mode in codex_modes else None,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "complexity": self.complexity.value,
            "family_preference": self.family_preference.value,
            "head_override": self.head_override,
            "review_head_override": self.review_head_override,
            "resolved_worker_family": self.resolved_worker_family,
            "resolved_worker_head": self.resolved_worker_head,
            "resolved_review_family": self.resolved_review_family,
            "resolved_review_head": self.resolved_review_head,
            "routing_reason": self.routing_reason,
            "quota_snapshot_at": self.quota_snapshot_at,
            "codex_launch_mode": self.codex_launch_mode,
        }


@dataclass(frozen=True, slots=True)
class TaskMetadata:
    """Typed task metadata rendered back to the released public document shape."""

    project: str
    task_type: TaskType | None
    task_type_raw: str
    blocked_by: str | None
    claim_worker: str | None
    routing: TaskRouting
    slug: str | None
    base_branch: str | None
    seed_ref: str | None
    supersedes: str | None
    retry_same: int
    retry_switched: int
    retry_heads: tuple[str, ...]
    sprint_ref: str | None
    record_type: str | None

    @classmethod
    def from_legacy(cls, meta: Mapping[str, Any], *, codex_modes: Collection[str]) -> TaskMetadata:
        raw_task_type = text(meta.get("task_type"))
        try:
            task_type = TaskType(raw_task_type) if raw_task_type else None
        except ValueError:
            task_type = None
        return cls(
            project=text(meta.get("project")),
            task_type=task_type,
            task_type_raw=raw_task_type,
            blocked_by=null_if_empty(meta.get("blocked_by")),
            claim_worker=null_if_empty(meta.get("claim")),
            routing=TaskRouting.from_legacy(meta, codex_modes=codex_modes),
            slug=null_if_empty(meta.get("slug")),
            base_branch=null_if_empty(meta.get("base_branch")),
            seed_ref=null_if_empty(meta.get("seed_ref")),
            supersedes=null_if_empty(meta.get("supersedes")),
            retry_same=nonnegative_int(meta.get("retry_same")),
            retry_switched=nonnegative_int(meta.get("retry_switch")),
            retry_heads=tuple(split_heads(meta.get("retry_heads"))),
            sprint_ref=null_if_empty(meta.get("sprint_ref")),
            record_type=null_if_empty(meta.get("record_type")),
        )

    @property
    def task_type_text(self) -> str:
        return self.task_type.value if self.task_type is not None else self.task_type_raw

    def to_document_fields(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "type": self.task_type_text,
            "blocked_by": self.blocked_by,
            "claim": {"worker": self.claim_worker, "claimed_at": None},
            "routing": self.routing.to_document(),
            "workspace": {
                "slug": self.slug,
                "base_branch": self.base_branch,
                "seed_ref": self.seed_ref,
                "supersedes": self.supersedes,
            },
            "retry": {
                "same": self.retry_same,
                "switched": self.retry_switched,
                "heads": list(self.retry_heads),
            },
            "sprint": self.sprint_ref,
            "record_type": self.record_type,
        }


__all__ = [
    "ACTIVE_STATES",
    "BLOCK_CLASSIFICATION_VALUES",
    "DECIDED_TARGETS",
    "DECISION_TARGETS",
    "DECISION_VALUES",
    "EDITABLE_STATES",
    "FAMILY_PREFERENCE_VALUES",
    "ROUTING_PHASE_VALUES",
    "TASK_COMPLEXITY_VALUES",
    "TASK_TYPE_VALUES",
    "UNDECIDED_EXITS",
    "BlockClassification",
    "FamilyPreference",
    "RoutingPhase",
    "TaskComplexity",
    "TaskDecision",
    "TaskMetadata",
    "TaskRouting",
    "TaskType",
]
