from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    content = read(path)
    count = content.count(old)
    if count < 1:
        raise RuntimeError(f"{path}: expected a match, found {count}: {old[:80]!r}")
    write(path, content.replace(old, new, 1))


def replace_all(path: str, old: str, new: str, *, minimum: int = 1) -> None:
    content = read(path)
    count = content.count(old)
    if count < minimum:
        raise RuntimeError(f"{path}: expected at least {minimum} matches, found {count}: {old!r}")
    write(path, content.replace(old, new))


TASK_ROUTING = '''"""Typed task routing vocabulary and legacy metadata boundary.

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
UNDECIDED_EXITS: frozenset[CardState] = frozenset(
    {CardState.READY, CardState.VALIDATE, CardState.ISSUES}
)
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
'''

TEST_TASK_ROUTING = '''from __future__ import annotations

import unittest

import secretary.tasks as tasks
from secretary.board.models import CardState
from secretary.board.task_routing import (
    ACTIVE_STATES,
    BLOCK_CLASSIFICATION_VALUES,
    DECIDED_TARGETS,
    DECISION_TARGETS,
    DECISION_VALUES,
    EDITABLE_STATES,
    FAMILY_PREFERENCE_VALUES,
    ROUTING_PHASE_VALUES,
    TASK_COMPLEXITY_VALUES,
    TASK_TYPE_VALUES,
    UNDECIDED_EXITS,
    BlockClassification,
    FamilyPreference,
    RoutingPhase,
    TaskComplexity,
    TaskDecision,
    TaskMetadata,
    TaskRouting,
    TaskType,
)


class TaskRoutingVocabularyTests(unittest.TestCase):
    def test_closed_vocabularies_keep_the_released_spellings(self) -> None:
        self.assertEqual(TASK_TYPE_VALUES, {"code", "research"})
        self.assertEqual(TASK_COMPLEXITY_VALUES, {"cheap", "standard", "hard", "frontier"})
        self.assertEqual(FAMILY_PREFERENCE_VALUES, {"auto", "claude", "codex"})
        self.assertEqual(ROUTING_PHASE_VALUES, {"worker", "review", "verdict"})
        self.assertEqual(BLOCK_CLASSIFICATION_VALUES, ("external_fact", "wrong_task_definition"))
        self.assertEqual(DECISION_VALUES, {"release", "rework", "reslice"})
        self.assertEqual(TaskType.CODE, "code")
        self.assertEqual(TaskComplexity.FRONTIER, "frontier")
        self.assertEqual(FamilyPreference.CODEX, "codex")
        self.assertEqual(RoutingPhase.REVIEW, "review")
        self.assertEqual(BlockClassification.EXTERNAL_FACT, "external_fact")
        self.assertEqual(TaskDecision.RESLICE, "reslice")

    def test_decision_and_state_sets_are_typed(self) -> None:
        self.assertEqual(
            DECISION_TARGETS,
            {
                TaskDecision.RELEASE: CardState.DONE,
                TaskDecision.REWORK: CardState.IN_PROGRESS,
                TaskDecision.RESLICE: CardState.BLOCKED,
            },
        )
        self.assertEqual(EDITABLE_STATES, {CardState.READY, CardState.BLOCKED})
        self.assertEqual(ACTIVE_STATES, {CardState.IN_PROGRESS, CardState.VALIDATE, CardState.ASSESSMENT})
        self.assertEqual(DECIDED_TARGETS, {CardState.DONE, CardState.IN_PROGRESS})
        self.assertEqual(UNDECIDED_EXITS, {CardState.READY, CardState.VALIDATE, CardState.ISSUES})

    def test_legacy_routing_normalizes_once_and_renders_the_same_document(self) -> None:
        routing = TaskRouting.from_legacy(
            {
                "complexity": "hard",
                "family_preference": "codex",
                "head": "codex-sol",
                "review_head": "claude-opus",
                "resolved_head": "codex-sol-1",
                "resolved_review_head": "claude-opus-1",
                "routing_reason": "quota",
                "quota_snapshot_at": "2026-09-16T10:00:00Z",
                "codex_launch_mode": "tui",
            },
            codex_modes={"tui"},
        )
        self.assertIs(routing.complexity, TaskComplexity.HARD)
        self.assertIs(routing.family_preference, FamilyPreference.CODEX)
        self.assertEqual(
            routing.to_document(),
            {
                "complexity": "hard",
                "family_preference": "codex",
                "head_override": "codex-sol",
                "review_head_override": "claude-opus",
                "resolved_worker_family": None,
                "resolved_worker_head": "codex-sol-1",
                "resolved_review_family": None,
                "resolved_review_head": "claude-opus-1",
                "routing_reason": "quota",
                "quota_snapshot_at": "2026-09-16T10:00:00Z",
                "codex_launch_mode": "tui",
            },
        )

    def test_legacy_routing_keeps_existing_fallback_semantics(self) -> None:
        routing = TaskRouting.from_legacy(
            {"complexity": "retired", "family_preference": "other", "codex_launch_mode": "exec"},
            codex_modes={"tui"},
        )
        self.assertIs(routing.complexity, TaskComplexity.STANDARD)
        self.assertIs(routing.family_preference, FamilyPreference.AUTO)
        self.assertIsNone(routing.codex_launch_mode)

    def test_metadata_is_typed_internally_but_keeps_the_public_shape(self) -> None:
        metadata = TaskMetadata.from_legacy(
            {
                "project": "secretary",
                "task_type": "code",
                "blocked_by": "secretary-1",
                "claim": "worker-1",
                "complexity": "frontier",
                "family_preference": "claude",
                "retry_same": "2",
                "retry_switch": "1",
                "retry_heads": "claude-opus,codex-sol",
                "slug": "work",
                "base_branch": "main",
                "seed_ref": "abc123",
                "supersedes": "secretary-2",
                "sprint_ref": "sprint:7",
                "record_type": "task",
            },
            codex_modes={"tui"},
        )
        self.assertIs(metadata.task_type, TaskType.CODE)
        self.assertIs(metadata.routing.complexity, TaskComplexity.FRONTIER)
        self.assertIs(metadata.routing.family_preference, FamilyPreference.CLAUDE)
        self.assertEqual(metadata.retry_heads, ("claude-opus", "codex-sol"))
        document = metadata.to_document_fields()
        self.assertEqual(document["type"], "code")
        self.assertEqual(document["routing"]["complexity"], "frontier")
        self.assertEqual(document["retry"]["heads"], ["claude-opus", "codex-sol"])

    def test_unknown_legacy_task_type_is_preserved_only_as_compatibility_text(self) -> None:
        metadata = TaskMetadata.from_legacy({"task_type": "old-kind"}, codex_modes={"tui"})
        self.assertIsNone(metadata.task_type)
        self.assertEqual(metadata.task_type_raw, "old-kind")
        self.assertEqual(metadata.to_document_fields()["type"], "old-kind")

    def test_tasks_no_longer_owns_duplicate_routing_registries(self) -> None:
        for name in (
            "_TASK_TYPES",
            "_COMPLEXITIES",
            "_FAMILY_PREFERENCES",
            "_ROUTING_PHASES",
            "_BLOCK_CLASSIFICATIONS",
            "_DECISION_TARGETS",
            "_DECISIONS",
            "_DECIDED_TARGETS",
            "_UNDECIDED_EXITS",
            "_EDITABLE_STATES",
        ):
            self.assertFalse(hasattr(tasks, name), name)


if __name__ == "__main__":
    unittest.main()
'''


def main() -> None:
    if (ROOT / "src/secretary/board/task_routing.py").exists():
        raise RuntimeError("task_routing.py already exists")
    write("src/secretary/board/task_routing.py", TASK_ROUTING)
    write("tests/test_task_routing.py", TEST_TASK_ROUTING)

    # The legacy parsers accept any closed collection; A08's canonical projections are immutable.
    replace_once(
        "src/secretary/board/legacy_codec.py",
        "from __future__ import annotations\n\nfrom typing import Any\n",
        "from __future__ import annotations\n\nfrom collections.abc import Collection\nfrom typing import Any\n",
    )
    replace_once(
        "src/secretary/board/legacy_codec.py",
        "def enum_or_default(value: Any, allowed: set[str], default: str) -> str:\n",
        "def enum_or_default(value: Any, allowed: Collection[str], default: str) -> str:\n",
    )
    replace_once(
        "src/secretary/board/legacy_codec.py",
        "def enum_or_none(value: Any, allowed: set[str]) -> str | None:\n",
        "def enum_or_none(value: Any, allowed: Collection[str]) -> str | None:\n",
    )

    # Export the typed task vocabulary from the board protocol package.
    replace_once(
        "src/secretary/board/__init__.py",
        "from secretary.board.roles import Role\n",
        "from secretary.board.roles import Role\nfrom secretary.board.task_routing import (\n"
        "    BlockClassification,\n"
        "    FamilyPreference,\n"
        "    RoutingPhase,\n"
        "    TaskComplexity,\n"
        "    TaskDecision,\n"
        "    TaskMetadata,\n"
        "    TaskRouting,\n"
        "    TaskType,\n"
        ")\n",
    )
    replace_once(
        "src/secretary/board/__init__.py",
        '    "BoardProtocolError",\n',
        '    "BoardProtocolError",\n    "BlockClassification",\n',
    )
    replace_once(
        "src/secretary/board/__init__.py",
        '    "FakeBoardHost",\n',
        '    "FakeBoardHost",\n    "FamilyPreference",\n',
    )
    replace_once(
        "src/secretary/board/__init__.py",
        '    "Role",\n',
        '    "Role",\n    "RoutingPhase",\n',
    )
    replace_once(
        "src/secretary/board/__init__.py",
        '    "SprintSupplement",\n',
        '    "SprintSupplement",\n    "TaskComplexity",\n    "TaskDecision",\n    "TaskMetadata",\n    "TaskRouting",\n    "TaskType",\n',
    )

    # tasks.py consumes typed metadata at its adapter boundary and typed enums at mutation boundaries.
    replace_once(
        "src/secretary/tasks.py",
        "    enum_or_default as _enum_or_default,\n    enum_or_none as _enum_or_none,\n",
        "    enum_or_default as _enum_or_default,  # noqa: F401 - released private compatibility alias\n"
        "    enum_or_none as _enum_or_none,  # noqa: F401 - released private compatibility alias\n",
    )
    roles_import = '''from secretary.board.roles import (
    BOARD_ROLES,
    COMMENT_ROLES,
    CREATE_ROLES,
    EDIT_ROLES,
    PROPOSAL_CREATE_ROLES,
    Role,
)
'''
    routing_import = roles_import + '''from secretary.board.task_routing import (
    ACTIVE_STATES,
    BLOCK_CLASSIFICATION_VALUES,
    DECIDED_TARGETS,
    DECISION_TARGETS,
    DECISION_VALUES,
    EDITABLE_STATES,
    FAMILY_PREFERENCE_VALUES,
    ROUTING_PHASE_VALUES,
    TASK_COMPLEXITY_VALUES,
    TASK_TYPE_VALUES,
    UNDECIDED_EXITS,
    BlockClassification,
    FamilyPreference,
    RoutingPhase,
    TaskComplexity,
    TaskDecision,
    TaskMetadata,
    TaskType,
)
'''
    replace_once("src/secretary/tasks.py", roles_import, routing_import)

    old_constants = '''_TASK_TYPES = {"code", "research"}
_COMPLEXITIES = {"cheap", "standard", "hard", "frontier"}
_FAMILY_PREFERENCES = {"auto", "claude", "codex"}
# Retired launch modes normalize away rather than silently changing a requested shape.
_CODEX_LAUNCH_MODES = CODEX_LAUNCH_MODES
# Agent roles that may not open an execution card are represented by
# PROPOSAL_CREATE_ROLES in the canonical board-role vocabulary.
_EDITABLE_STATES = {"ready", "blocked"}
_READY_RESET_METADATA = {
    "claim": "",
    "resolved_head": "",
    "resolved_review_head": "",
    "retry_same": "",
    "retry_switch": "",
    "retry_heads": "",
}
_ROUTING_PHASES = {"worker", "review", "verdict"}
# Worker blocker classification is evidence for, not the observer's final verdict.
_BLOCK_CLASSIFICATIONS = ("external_fact", "wrong_task_definition")
# Persist a parked-card decision before effects; blocked remains the failure escape hatch.
_DECISION_TARGETS = {"release": "done", "rework": "in_progress", "reslice": "blocked"}
_DECISIONS = set(_DECISION_TARGETS)
_DECIDED_TARGETS = {"done", "in_progress"}
# Only the PO may use these Assessment exits; the dispatcher must record a decision.
_UNDECIDED_EXITS = {"ready", "validate", "issues"}
# Dispatcher Assessment moves require decisions; human escape-hatch moves do not.
_DECISION_BOUND_ROLES: frozenset[Role] = frozenset({Role.DISPATCHER})
# States in which a card holds a workspace, a suspended worker or a running head. `assessment`
# is one of them: the reviewer is gone, but the worker and its checkout are retained for a
# rework decision, so a second writer in the same project is as wrong there as in Validate.
ACTIVE_STATES = frozenset({"in_progress", "validate", "assessment"})
'''
    new_constants = '''# Retired launch modes normalize away rather than silently changing a requested shape.
_CODEX_LAUNCH_MODES = CODEX_LAUNCH_MODES
# Agent roles that may not open an execution card are represented by
# PROPOSAL_CREATE_ROLES in the canonical board-role vocabulary.
_READY_RESET_METADATA = {
    "claim": "",
    "resolved_head": "",
    "resolved_review_head": "",
    "retry_same": "",
    "retry_switch": "",
    "retry_heads": "",
}
# Dispatcher Assessment moves require decisions; human escape-hatch moves do not.
_DECISION_BOUND_ROLES: frozenset[Role] = frozenset({Role.DISPATCHER})
'''
    replace_once("src/secretary/tasks.py", old_constants, new_constants)

    old_metadata = '''            "project": _text(meta.get("project")),
            "type": _text(meta.get("task_type")),
            "blocked_by": _null_if_empty(meta.get("blocked_by")),
            "claim": {"worker": _null_if_empty(meta.get("claim")), "claimed_at": None},
            "routing": {
                "complexity": _enum_or_default(meta.get("complexity"), _COMPLEXITIES, "standard"),
                "family_preference": _enum_or_default(
                    meta.get("family_preference"), _FAMILY_PREFERENCES, "auto"
                ),
                "head_override": _null_if_empty(meta.get("head")),
                "review_head_override": _null_if_empty(meta.get("review_head")),
                "resolved_worker_family": None,
                "resolved_worker_head": _null_if_empty(meta.get("resolved_head")),
                "resolved_review_family": None,
                "resolved_review_head": _null_if_empty(meta.get("resolved_review_head")),
                "routing_reason": _null_if_empty(meta.get("routing_reason")),
                "quota_snapshot_at": _null_if_empty(meta.get("quota_snapshot_at")),
                "codex_launch_mode": _enum_or_none(meta.get("codex_launch_mode"), _CODEX_LAUNCH_MODES),
            },
            "workspace": {
                "slug": _null_if_empty(meta.get("slug")),
                "base_branch": _null_if_empty(meta.get("base_branch")),
                "seed_ref": _null_if_empty(meta.get("seed_ref")),
                "supersedes": _null_if_empty(meta.get("supersedes")),
            },
            "retry": {
                "same": _nonnegative_int(meta.get("retry_same")),
                "switched": _nonnegative_int(meta.get("retry_switch")),
                "heads": _split_heads(meta.get("retry_heads")),
            },
            "sprint": _null_if_empty(meta.get("sprint_ref")),
            "record_type": _null_if_empty(meta.get("record_type")),
'''
    new_metadata = '''            **TaskMetadata.from_legacy(meta, codex_modes=_CODEX_LAUNCH_MODES).to_document_fields(),
'''
    replace_once("src/secretary/tasks.py", old_metadata, new_metadata)

    # Typed create inputs, rendered back to the same strings before persistence/audit.
    replace_all(
        "src/secretary/tasks.py",
        'complexity: str = "standard",',
        "complexity: str = TaskComplexity.STANDARD.value,",
        minimum=2,
    )
    replace_all(
        "src/secretary/tasks.py",
        'family_preference: str = "auto",',
        "family_preference: str = FamilyPreference.AUTO.value,",
        minimum=2,
    )
    replace_once(
        "src/secretary/tasks.py",
        '        complexity = complexity.strip() or "standard"\n        family_preference = family_preference.strip() or "auto"\n',
        "        complexity = complexity.strip() or TaskComplexity.STANDARD.value\n"
        "        family_preference = family_preference.strip() or FamilyPreference.AUTO.value\n",
    )
    replace_once(
        "src/secretary/tasks.py",
        '''        if task_type not in _TASK_TYPES:
            known = ", ".join(sorted(_TASK_TYPES))
            raise TaskError("validation", f"unknown task type {task_type!r} (known: {known})", 2)
''',
        '''        try:
            task_type_value = TaskType(task_type)
        except ValueError:
            known = ", ".join(sorted(TASK_TYPE_VALUES))
            raise TaskError("validation", f"unknown task type {task_type!r} (known: {known})", 2) from None
        task_type = task_type_value.value
''',
    )
    replace_once(
        "src/secretary/tasks.py",
        '''        if complexity not in _COMPLEXITIES:
            raise TaskError("validation", "complexity must be one of: " + ", ".join(sorted(_COMPLEXITIES)), 2)
        if family_preference not in _FAMILY_PREFERENCES:
            raise TaskError(
                "validation", "family preference must be one of: " + ", ".join(sorted(_FAMILY_PREFERENCES)), 2
            )
''',
        '''        try:
            complexity_value = TaskComplexity(complexity)
        except ValueError:
            raise TaskError(
                "validation", "complexity must be one of: " + ", ".join(sorted(TASK_COMPLEXITY_VALUES)), 2
            ) from None
        complexity = complexity_value.value
        try:
            family_preference_value = FamilyPreference(family_preference)
        except ValueError:
            raise TaskError(
                "validation",
                "family preference must be one of: " + ", ".join(sorted(FAMILY_PREFERENCE_VALUES)),
                2,
            ) from None
        family_preference = family_preference_value.value
''',
    )
    replace_all("src/secretary/tasks.py", 'task_type="research"', "task_type=TaskType.RESEARCH.value")
    replace_once(
        "src/secretary/tasks.py",
        '        if steward_report and (task_type != "research" or not slug or reference or sprint):\n',
        "        if steward_report and (task_type != TaskType.RESEARCH.value or not slug or reference or sprint):\n",
    )

    # Worker report classification is converted to its typed vocabulary before the event is built.
    replace_once(
        "src/secretary/tasks.py",
        '''        if kind == "blocked" and classification not in _BLOCK_CLASSIFICATIONS:
            raise TaskError(
                "validation",
                "blocked reports require --classification, one of " + ", ".join(_BLOCK_CLASSIFICATIONS),
                2,
            )
        if kind == "done" and classification:
            raise TaskError("validation", "a done report carries no classification", 2)
''',
        '''        if kind == "blocked" and classification not in BLOCK_CLASSIFICATION_VALUES:
            raise TaskError(
                "validation",
                "blocked reports require --classification, one of "
                + ", ".join(BLOCK_CLASSIFICATION_VALUES),
                2,
            )
        if kind == "done" and classification:
            raise TaskError("validation", "a done report carries no classification", 2)
        classification_value = BlockClassification(classification) if classification else None
''',
    )
    replace_once(
        "src/secretary/tasks.py",
        '                "classification": classification or None,\n',
        '                "classification": classification_value.value if classification_value is not None else None,\n',
    )

    # Observer decisions and their target mapping are one typed vocabulary.
    replace_once(
        "src/secretary/tasks.py",
        '''        if kind not in _DECISIONS:
            raise TaskError("validation", f"decision must be one of {', '.join(sorted(_DECISIONS))}", 2)
        if not body.strip():
''',
        '''        if kind not in DECISION_VALUES:
            raise TaskError("validation", f"decision must be one of {', '.join(sorted(DECISION_VALUES))}", 2)
        decision_kind = TaskDecision(kind)
        kind = decision_kind.value
        if not body.strip():
''',
    )
    replace_once("src/secretary/tasks.py", '            if kind == "rework":\n', "            if decision_kind is TaskDecision.REWORK:\n")

    replace_once(
        "src/secretary/tasks.py",
        '''        phase = _text(payload.get("phase"))
        if phase not in _ROUTING_PHASES:
            known = ", ".join(sorted(_ROUTING_PHASES))
            raise TaskError("validation", f"unknown routing phase {phase!r} (known: {known})", 2)
        heads = payload.get("heads")
''',
        '''        phase = _text(payload.get("phase"))
        if phase not in ROUTING_PHASE_VALUES:
            known = ", ".join(sorted(ROUTING_PHASE_VALUES))
            raise TaskError("validation", f"unknown routing phase {phase!r} (known: {known})", 2)
        routing_phase = RoutingPhase(phase)
        normalized_payload = dict(payload)
        normalized_payload["phase"] = routing_phase.value
        heads = payload.get("heads")
''',
    )
    replace_once(
        "src/secretary/tasks.py",
        '''            dict(payload),
            lambda task: None,
            identity=dict(payload),
''',
        '''            normalized_payload,
            lambda task: None,
            identity=normalized_payload,
''',
    )

    replace_once(
        "src/secretary/tasks.py",
        '''        if decision and decision not in _DECISIONS:
            raise TaskError("validation", f"decision must be one of {', '.join(sorted(_DECISIONS))}", 2)
        if decision and source != "assessment":
            raise TaskError("validation", "a decision is only carried by a move out of Assessment", 2)
        if decision and _DECISION_TARGETS[decision] != target:
            raise TaskError(
                "decision_mismatch",
                f"a {decision} decision moves the card to {_DECISION_TARGETS[decision]}, not {target}",
                3,
            )
        if (
            source == "assessment"
            and target in _DECIDED_TARGETS
            and not decision
            and role in _DECISION_BOUND_ROLES
        ):
''',
        '''        if decision and decision not in DECISION_VALUES:
            raise TaskError("validation", f"decision must be one of {', '.join(sorted(DECISION_VALUES))}", 2)
        decision_kind = TaskDecision(decision) if decision else None
        if decision_kind is not None and source != "assessment":
            raise TaskError("validation", "a decision is only carried by a move out of Assessment", 2)
        if decision_kind is not None and DECISION_TARGETS[decision_kind].value != target:
            raise TaskError(
                "decision_mismatch",
                f"a {decision} decision moves the card to {DECISION_TARGETS[decision_kind].value}, not {target}",
                3,
            )
        if (
            source == "assessment"
            and target in DECIDED_TARGETS
            and decision_kind is None
            and role in _DECISION_BOUND_ROLES
        ):
''',
    )
    replace_once(
        "src/secretary/tasks.py",
        '        if source == "assessment" and target in _UNDECIDED_EXITS and role in _DECISION_BOUND_ROLES:\n',
        '        if source == "assessment" and target in UNDECIDED_EXITS and role in _DECISION_BOUND_ROLES:\n',
    )
    replace_once(
        "src/secretary/tasks.py",
        '        if decision and not self._decision_recorded(task["ref"], decision):\n',
        '        if decision_kind is not None and not self._decision_recorded(task["ref"], decision_kind.value):\n',
    )
    replace_all("src/secretary/tasks.py", "_EDITABLE_STATES", "EDITABLE_STATES")

    # CLI choices project the canonical enums instead of maintaining their own literal copies.
    replace_once(
        "src/secretary/task_commands.py",
        "from secretary.board.backend import card_client\nfrom secretary.board.roles import BOARD_ROLES, CREATE_ROLES, EDIT_ROLES, Role\n",
        "from secretary.board.backend import card_client\n"
        "from secretary.board.models import CardState\n"
        "from secretary.board.roles import BOARD_ROLES, CREATE_ROLES, EDIT_ROLES, Role\n"
        "from secretary.board.task_routing import (\n"
        "    BlockClassification,\n"
        "    FamilyPreference,\n"
        "    TaskComplexity,\n"
        "    TaskDecision,\n"
        "    TaskType,\n"
        ")\n",
    )
    replace_once(
        "src/secretary/task_commands.py",
        "from secretary.tasks import (\n    _BLOCK_CLASSIFICATIONS,\n    TaskError,\n",
        "from secretary.tasks import (\n    TaskError,\n",
    )
    replace_once(
        "src/secretary/task_commands.py",
        '        choices=("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done"),\n',
        "        choices=tuple(state.value for state in CardState),\n",
    )
    replace_once(
        "src/secretary/task_commands.py",
        '    task_create.add_argument("--type", required=True, choices=("code", "research"))\n',
        '    task_create.add_argument("--type", required=True, choices=tuple(kind.value for kind in TaskType))\n',
    )
    replace_once(
        "src/secretary/task_commands.py",
        '    task_create.add_argument("--state", choices=("issues", "ready"), default="ready")\n',
        "    task_create.add_argument(\n"
        "        \"--state\",\n"
        "        choices=(CardState.ISSUES.value, CardState.READY.value),\n"
        "        default=CardState.READY.value,\n"
        "    )\n",
    )
    replace_once(
        "src/secretary/task_commands.py",
        '''    task_create.add_argument(
        "--complexity", choices=("cheap", "standard", "hard", "frontier"), default="standard"
    )
    task_create.add_argument("--family-preference", choices=("auto", "claude", "codex"), default="auto")
''',
        '''    task_create.add_argument(
        "--complexity",
        choices=tuple(value.value for value in TaskComplexity),
        default=TaskComplexity.STANDARD.value,
    )
    task_create.add_argument(
        "--family-preference",
        choices=tuple(value.value for value in FamilyPreference),
        default=FamilyPreference.AUTO.value,
    )
''',
    )
    replace_once(
        "src/secretary/task_commands.py",
        '            command.add_argument("--classification", default="", choices=("", *_BLOCK_CLASSIFICATIONS))\n',
        '            command.add_argument(\n'
        '                "--classification",\n'
        '                default="",\n'
        '                choices=("", *(value.value for value in BlockClassification)),\n'
        '            )\n',
    )
    replace_once(
        "src/secretary/task_commands.py",
        '            command.add_argument("--kind", required=True, choices=("release", "rework", "reslice"))\n',
        '            command.add_argument(\n'
        '                "--kind", required=True, choices=tuple(value.value for value in TaskDecision)\n'
        '            )\n',
    )
    replace_once(
        "src/secretary/task_commands.py",
        '                choices=("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done"),\n',
        "                choices=tuple(state.value for state in CardState),\n",
    )
    replace_once(
        "src/secretary/task_commands.py",
        '            command.add_argument("--decision", default="", choices=("", "release", "rework", "reslice"))\n',
        '            command.add_argument(\n'
        '                "--decision", default="", choices=("", *(value.value for value in TaskDecision))\n'
        '            )\n',
    )

    # Importer depends on the typed vocabulary leaf, not private task registries.
    replace_once(
        "src/secretary/board/import_board.py",
        "from secretary.board.roles import BOARD_ROLES\n",
        "from secretary.board.roles import BOARD_ROLES\n"
        "from secretary.board.task_routing import (\n"
        "    FAMILY_PREFERENCE_VALUES,\n"
        "    TASK_COMPLEXITY_VALUES,\n"
        "    TASK_TYPE_VALUES,\n"
        ")\n",
    )
    replace_once(
        "src/secretary/board/import_board.py",
        '''from secretary.tasks import (
    _COMPLEXITIES,
    _FAMILY_PREFERENCES,
    _TASK_TYPES,
    KanboardClient,
    _task_metadata,
    all_project_cards,
)
''',
        '''from secretary.tasks import (
    KanboardClient,
    _task_metadata,
    all_project_cards,
)
''',
    )
    replace_all("src/secretary/board/import_board.py", "_TASK_TYPES", "TASK_TYPE_VALUES")
    replace_all("src/secretary/board/import_board.py", "_COMPLEXITIES", "TASK_COMPLEXITY_VALUES")
    replace_all("src/secretary/board/import_board.py", "_FAMILY_PREFERENCES", "FAMILY_PREFERENCE_VALUES")

    # Restore uses the same typed legacy metadata boundary rather than duplicating routing sets.
    replace_once(
        "src/secretary/restore.py",
        "    TASK_STATE_BY_COLUMN as _STATE_BY_COLUMN,\n    enum_or_default as _enum_or_default,\n    positive_int as _positive_int,\n",
        "    TASK_STATE_BY_COLUMN as _STATE_BY_COLUMN,\n    enum_or_default as _enum_or_default,  # noqa: F401 - released private compatibility alias\n    positive_int as _positive_int,\n",
    )
    replace_once(
        "src/secretary/restore.py",
        "from secretary.board.normalized_checkpoint import NormalizedBoardError, validated_normalized_cards\n",
        "from secretary.board.normalized_checkpoint import NormalizedBoardError, validated_normalized_cards\n"
        "from secretary.board.task_routing import TaskMetadata\n",
    )
    replace_once(
        "src/secretary/restore.py",
        '''    return {
        "project": value("project"),
        "task_type": value("task_type"),
        "blocked_by": value("blocked_by"),
        "head": value("head"),
        "review_head": value("review_head"),
        "slug": value("slug"),
        "base_branch": value("base_branch"),
        "seed_ref": value("seed_ref"),
        "supersedes": value("supersedes"),
        "complexity": _enum_or_default(
            value("complexity"), {"cheap", "standard", "hard", "frontier"}, "standard"
        ),
        "family_preference": _enum_or_default(
            value("family_preference"), {"auto", "claude", "codex"}, "auto"
        ),
        # Legacy `exec` reads as no mode; live modes round-trip unchanged.
        "codex_launch_mode": _enum_or_default(value("codex_launch_mode"), {"", *CODEX_LAUNCH_MODES}, ""),
    }
''',
        '''    typed = TaskMetadata.from_legacy(
        {
            "task_type": value("task_type"),
            "complexity": value("complexity"),
            "family_preference": value("family_preference"),
            "codex_launch_mode": value("codex_launch_mode"),
        },
        codex_modes=CODEX_LAUNCH_MODES,
    )
    return {
        "project": value("project"),
        "task_type": typed.task_type_text,
        "blocked_by": value("blocked_by"),
        "head": value("head"),
        "review_head": value("review_head"),
        "slug": value("slug"),
        "base_branch": value("base_branch"),
        "seed_ref": value("seed_ref"),
        "supersedes": value("supersedes"),
        "complexity": typed.routing.complexity.value,
        "family_preference": typed.routing.family_preference.value,
        # Legacy `exec` reads as no mode; live modes round-trip unchanged.
        "codex_launch_mode": typed.routing.codex_launch_mode or "",
    }
''',
    )

    # Expand the incremental type gate with the new isolated typed leaf.
    replace_once(
        "pyproject.toml",
        '    "src/secretary/board/card_transitions.py",\n',
        '    "src/secretary/board/card_transitions.py",\n    "src/secretary/board/task_routing.py",\n',
    )

    # Register the new unit test in the repository's exhaustive shard manifest.
    replace_once(
        "tests/ci-shards.txt",
        "unit tests/test_task_commands.py\n",
        "unit tests/test_task_commands.py\nunit tests/test_task_routing.py\n",
    )

    # The board-store vocabulary table names the canonical owners, not the legacy façade.
    replace_once(
        "docs/BOARD_STORE.md",
        "| `tasks.task_type` | `code`, `research`, or NULL | `tasks._TASK_TYPES` |\n"
        "| `tasks.complexity` | `cheap`, `standard`, `hard`, `frontier` | `tasks._COMPLEXITIES` |\n"
        "| `tasks.family_preference` | `auto`, `claude`, `codex` | `tasks._FAMILY_PREFERENCES` |\n",
        "| `tasks.task_type` | `code`, `research`, or NULL | `board.task_routing.TaskType` |\n"
        "| `tasks.complexity` | `cheap`, `standard`, `hard`, `frontier` | `board.task_routing.TaskComplexity` |\n"
        "| `tasks.family_preference` | `auto`, `claude`, `codex` | `board.task_routing.FamilyPreference` |\n",
    )

    # No old private registry may remain in product code after the migration.
    source = read("src/secretary/tasks.py")
    for old_name in (
        "_TASK_TYPES",
        "_COMPLEXITIES",
        "_FAMILY_PREFERENCES",
        "_ROUTING_PHASES",
        "_BLOCK_CLASSIFICATIONS",
        "_DECISION_TARGETS",
        "_DECISIONS",
        "_DECIDED_TARGETS",
        "_UNDECIDED_EXITS",
        "_EDITABLE_STATES",
    ):
        if old_name in source:
            raise RuntimeError(f"legacy task registry remains: {old_name}")


if __name__ == "__main__":
    main()
