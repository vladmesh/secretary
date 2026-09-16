from __future__ import annotations

import re
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
    if count != 1:
        raise RuntimeError(f"{path}: expected one exact match, found {count}: {old[:80]!r}")
    write(path, content.replace(old, new, 1))


def regex_replace(path: str, pattern: str, replacement: str) -> None:
    content = read(path)
    updated, count = re.subn(pattern, replacement, content)
    if count == 0:
        raise RuntimeError(f"{path}: no matches for {pattern!r}")
    write(path, updated)


SPRINT_READ = '''\
"""Typed Sprint read model and legacy metadata boundary.

Sprint storage remains string/JSON shaped at the Kanboard and PostgreSQL adapters.  This
module owns the stable parsing rules for the closed Sprint vocabularies and compound metadata
so readers and one-shot migration code normalize those values exactly once before consuming them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from secretary.board.legacy_codec import positive_int, text
from secretary.board.models import SprintState

SOURCE_AUDIT_FIELDS = ("created_at", "updated_at", "board")

# Charged restart types contribute to total and thresholds.
BUDGET_EVENT_TYPES = (
    "red_review",
    "blocked",
    "red_ci",
    "preempt",
    "recreated_task",
    "hotfix",
)
# Infrastructure bring-up failures are visible but never spend restart budget.
BUDGET_UNCHARGED_INFRASTRUCTURE = "infrastructure_blocked"
BUDGET_UNCHARGED_EVENT_TYPES = (BUDGET_UNCHARGED_INFRASTRUCTURE,)
BUDGET_RECORDED_EVENT_TYPES = BUDGET_EVENT_TYPES + BUDGET_UNCHARGED_EVENT_TYPES
BUDGET_UNCHARGED_FIELD = "sprint_budget_uncharged"
DEFAULT_BUDGET_SIGNAL = 3
DEFAULT_BUDGET_HARD = 6

RESUME_FIELDS = (
    "selected_step",
    "selected_why",
    "rejected_alternatives",
    "current_task",
    "dod_state",
    "next_safe_step",
)

SPRINT_STATE_VALUES: frozenset[str] = frozenset(member.value for member in SprintState)


def _default_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def budget_thresholds(config: Mapping[str, Any] | None = None) -> dict[str, int]:
    """Resolve effective Sprint budget thresholds without depending on the legacy façade."""
    raw = config.get("sprint_budget") if isinstance(config, Mapping) else {}
    raw = raw if isinstance(raw, Mapping) else {}
    signal = positive_int(raw.get("signal")) or DEFAULT_BUDGET_SIGNAL
    hard = positive_int(raw.get("hard")) or DEFAULT_BUDGET_HARD
    if hard < signal:
        raise ValueError("sprint budget hard threshold must not be below signal threshold")
    return {"signal": signal, "hard": hard}


def _unique_strings(values: list[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def sprint_string_list(value: str | None) -> list[str]:
    """Read one legacy JSON string-list with the released de-duplication semantics."""
    try:
        raw = json.loads(value or "[]")
    except ValueError:
        return []
    return list(_unique_strings(raw)) if isinstance(raw, list) else []


def _budget_count(counts: Any, event_type: str) -> int:
    if not isinstance(counts, Mapping):
        return 0
    try:
        return max(0, int(counts.get(event_type, 0)))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class SprintBudget:
    """Normalized charged and uncharged restart accounting for one Sprint."""

    total: int
    by_type: dict[str, int]
    uncharged: dict[str, int]
    thresholds: dict[str, int]
    signal_reached: bool
    hard_reached: bool

    @classmethod
    def from_legacy(
        cls,
        value: Any = None,
        *,
        thresholds: Mapping[str, int] | None = None,
        uncharged: Any = None,
    ) -> SprintBudget:
        source = value if isinstance(value, Mapping) else {}
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                source = decoded if isinstance(decoded, Mapping) else {}
            except ValueError:
                source = {}
        by_type = source.get("by_type") if isinstance(source, Mapping) else {}
        counts = {event_type: _budget_count(by_type, event_type) for event_type in BUDGET_EVENT_TYPES}
        spare_source = uncharged
        if spare_source is None:
            spare_source = source.get("uncharged") if isinstance(source, Mapping) else {}
        if isinstance(spare_source, str):
            try:
                decoded = json.loads(spare_source)
                spare_source = decoded if isinstance(decoded, Mapping) else {}
            except ValueError:
                spare_source = {}
        spare = {
            event_type: _budget_count(spare_source, event_type)
            for event_type in BUDGET_UNCHARGED_EVENT_TYPES
        }
        limits = dict(thresholds) if thresholds is not None else budget_thresholds()
        total = sum(counts.values())
        return cls(
            total=total,
            by_type=counts,
            uncharged=spare,
            thresholds=limits,
            signal_reached=total >= limits["signal"],
            hard_reached=total >= limits["hard"],
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_type": dict(self.by_type),
            "uncharged": dict(self.uncharged),
            "thresholds": dict(self.thresholds),
            "signal_reached": self.signal_reached,
            "hard_reached": self.hard_reached,
        }


def sprint_budget_document(
    value: Any = None,
    thresholds: Mapping[str, int] | None = None,
    uncharged: Any = None,
) -> dict[str, Any]:
    """Compatibility projection for code that still emits the released dict document."""
    return SprintBudget.from_legacy(value, thresholds=thresholds, uncharged=uncharged).to_document()


@dataclass(frozen=True, slots=True)
class SprintSourceAudit:
    """The source audit carried by a restored Sprint, when one was recorded."""

    created_at: str
    updated_at: str
    board: str

    @classmethod
    def from_legacy(cls, value: Any) -> SprintSourceAudit | None:
        source = value
        if isinstance(value, str):
            try:
                source = json.loads(value or "null")
            except ValueError:
                return None
        if not isinstance(source, Mapping):
            return None
        values = {field: text(source.get(field)) for field in SOURCE_AUDIT_FIELDS}
        if not any(values.values()):
            return None
        return cls(
            created_at=values["created_at"],
            updated_at=values["updated_at"],
            board=values["board"],
        )

    def to_document(self) -> dict[str, str]:
        return {
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "board": self.board,
        }


def sprint_source_audit_document(value: Any) -> dict[str, str] | None:
    """Compatibility projection of :class:`SprintSourceAudit`."""
    source = SprintSourceAudit.from_legacy(value)
    return source.to_document() if source is not None else None


@dataclass(frozen=True, slots=True)
class SprintResume:
    """One complete observer resume entry projected from legacy metadata."""

    selected_step: str
    selected_why: str
    rejected_alternatives: str
    current_task: str
    dod_state: str
    next_safe_step: str
    recorded_at: str

    @classmethod
    def from_legacy(
        cls,
        value: Any,
        *,
        required: bool = False,
        now: Callable[[], str] = _default_now,
    ) -> SprintResume | None:
        source = value
        if isinstance(value, str):
            try:
                source = json.loads(value)
            except ValueError:
                source = None
        if not isinstance(source, Mapping):
            if required:
                raise ValueError("resume entry must be a JSON object")
            return None
        missing = [
            field
            for field in RESUME_FIELDS
            if not isinstance(source.get(field), str) or not str(source[field]).strip()
        ]
        if missing:
            if required:
                raise ValueError("resume entry is missing required fields: " + ", ".join(missing))
            return None
        recorded_at = text(source.get("recorded_at")) or now()
        return cls(
            selected_step=str(source["selected_step"]).strip(),
            selected_why=str(source["selected_why"]).strip(),
            rejected_alternatives=str(source["rejected_alternatives"]).strip(),
            current_task=str(source["current_task"]).strip(),
            dod_state=str(source["dod_state"]).strip(),
            next_safe_step=str(source["next_safe_step"]).strip(),
            recorded_at=recorded_at,
        )

    def to_document(self) -> dict[str, str]:
        return {
            "selected_step": self.selected_step,
            "selected_why": self.selected_why,
            "rejected_alternatives": self.rejected_alternatives,
            "current_task": self.current_task,
            "dod_state": self.dod_state,
            "next_safe_step": self.next_safe_step,
            "recorded_at": self.recorded_at,
        }


def sprint_resume_document(
    value: Any,
    *,
    now: Callable[[], str] = _default_now,
) -> dict[str, str] | None:
    """Compatibility projection for non-validating legacy resume reads."""
    resume = SprintResume.from_legacy(value, now=now)
    return resume.to_document() if resume is not None else None


@dataclass(frozen=True, slots=True)
class SprintReadMetadata:
    """Typed compound fields read from one Sprint's legacy metadata bag."""

    repositories: tuple[str, ...]
    state: SprintState
    budget: SprintBudget
    resume: SprintResume | None
    source_audit: SprintSourceAudit | None

    @classmethod
    def from_legacy(
        cls,
        meta: Mapping[str, Any],
        *,
        thresholds: Mapping[str, int] | None = None,
        now: Callable[[], str] = _default_now,
    ) -> SprintReadMetadata:
        raw_state = text(meta.get("sprint_status"))
        try:
            state = SprintState(raw_state)
        except ValueError:
            state = SprintState.OPEN
        return cls(
            repositories=tuple(sprint_string_list(text(meta.get("sprint_repositories")))),
            state=state,
            budget=SprintBudget.from_legacy(
                meta.get("sprint_budget"),
                thresholds=thresholds,
                uncharged=meta.get(BUDGET_UNCHARGED_FIELD),
            ),
            resume=SprintResume.from_legacy(meta.get("sprint_resume"), now=now),
            source_audit=SprintSourceAudit.from_legacy(meta.get("sprint_source_audit")),
        )


__all__ = [
    "BUDGET_EVENT_TYPES",
    "BUDGET_RECORDED_EVENT_TYPES",
    "BUDGET_UNCHARGED_EVENT_TYPES",
    "BUDGET_UNCHARGED_FIELD",
    "DEFAULT_BUDGET_HARD",
    "DEFAULT_BUDGET_SIGNAL",
    "RESUME_FIELDS",
    "SOURCE_AUDIT_FIELDS",
    "SPRINT_STATE_VALUES",
    "SprintBudget",
    "SprintReadMetadata",
    "SprintResume",
    "SprintSourceAudit",
    "budget_thresholds",
    "sprint_budget_document",
    "sprint_resume_document",
    "sprint_source_audit_document",
    "sprint_string_list",
]
'''

TEST_SPRINT_READ = '''\
from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from secretary.board.models import SprintState
from secretary.board.sprint_read import (
    BUDGET_EVENT_TYPES,
    SprintReadMetadata,
    SprintResume,
)


class SprintReadModelTests(unittest.TestCase):
    def test_compound_metadata_is_typed_once_and_renders_the_released_shape(self) -> None:
        resume = {
            "selected_step": " next ",
            "selected_why": " because ",
            "rejected_alternatives": " other ",
            "current_task": " task:1 ",
            "dod_state": " pending ",
            "next_safe_step": " continue ",
            "recorded_at": "2026-09-16T12:00:00Z",
        }
        meta = {
            "sprint_repositories": json.dumps([" /repo/a ", "/repo/a", "/repo/b", ""]),
            "sprint_status": "closed",
            "sprint_budget": json.dumps(
                {"by_type": {"red_review": "2", "blocked": -4, "hotfix": "bad"}}
            ),
            "sprint_budget_uncharged": json.dumps({"infrastructure_blocked": "3"}),
            "sprint_resume": json.dumps(resume),
            "sprint_source_audit": json.dumps(
                {
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-02T00:00:00Z",
                    "board": "old",
                    "ignored": "not part of the contract",
                }
            ),
        }

        read = SprintReadMetadata.from_legacy(meta, thresholds={"signal": 2, "hard": 5})

        self.assertEqual(read.repositories, ("/repo/a", "/repo/b"))
        self.assertIs(read.state, SprintState.CLOSED)
        self.assertEqual(read.budget.total, 2)
        self.assertEqual(
            read.budget.by_type,
            {event: (2 if event == "red_review" else 0) for event in BUDGET_EVENT_TYPES},
        )
        self.assertEqual(read.budget.uncharged, {"infrastructure_blocked": 3})
        self.assertTrue(read.budget.signal_reached)
        self.assertFalse(read.budget.hard_reached)
        self.assertEqual(
            read.resume.to_document() if read.resume else None,
            {
                **{key: value.strip() for key, value in resume.items() if key != "recorded_at"},
                "recorded_at": resume["recorded_at"],
            },
        )
        self.assertEqual(
            read.source_audit.to_document() if read.source_audit else None,
            {
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "board": "old",
            },
        )

    def test_invalid_legacy_values_keep_the_old_safe_defaults(self) -> None:
        read = SprintReadMetadata.from_legacy(
            {
                "sprint_repositories": "not-json",
                "sprint_status": "future-state",
                "sprint_budget": "{",
                "sprint_budget_uncharged": "{",
                "sprint_resume": json.dumps({"selected_step": "only one field"}),
                "sprint_source_audit": json.dumps({"other": "ignored"}),
            }
        )

        self.assertEqual(read.repositories, ())
        self.assertIs(read.state, SprintState.OPEN)
        self.assertEqual(read.budget.total, 0)
        self.assertEqual(read.budget.thresholds, {"signal": 3, "hard": 6})
        self.assertFalse(read.budget.signal_reached)
        self.assertFalse(read.budget.hard_reached)
        self.assertIsNone(read.resume)
        self.assertIsNone(read.source_audit)

    def test_required_resume_reports_the_same_missing_field_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "resume entry is missing required fields"):
            SprintResume.from_legacy({"selected_step": "x"}, required=True)
        with self.assertRaisesRegex(ValueError, "resume entry must be a JSON object"):
            SprintResume.from_legacy("not-json", required=True)

    def test_board_importer_no_longer_imports_private_sprint_helpers(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src" / "secretary" / "board" / "import_board.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        private = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "secretary.sprints"
            for alias in node.names
            if alias.name.startswith("_")
        ]
        self.assertEqual(private, [])


if __name__ == "__main__":
    unittest.main()
'''


def main() -> None:
    write("src/secretary/board/sprint_read.py", SPRINT_READ)
    write("tests/test_sprint_read.py", TEST_SPRINT_READ)

    # sprints.py: make the typed boundary canonical while keeping the released private helpers as adapters.
    replace_once(
        "src/secretary/sprints.py",
        "from secretary.board.backend import (\n    KANBOARD,\n    BoardBackendError,\n    entity_id,\n    entity_number,\n    sprint_reference_number,\n)\n",
        "from secretary.board.backend import (\n    KANBOARD,\n    BoardBackendError,\n    entity_id,\n    entity_number,\n    sprint_reference_number,\n)\nfrom secretary.board.models import SprintState\nfrom secretary.board.sprint_read import (\n    BUDGET_EVENT_TYPES,\n    BUDGET_RECORDED_EVENT_TYPES,\n    BUDGET_UNCHARGED_EVENT_TYPES,\n    BUDGET_UNCHARGED_FIELD,\n    DEFAULT_BUDGET_HARD,\n    DEFAULT_BUDGET_SIGNAL,\n    RESUME_FIELDS,\n    SOURCE_AUDIT_FIELDS,\n    SprintBudget,\n    SprintReadMetadata,\n    SprintResume,\n    SprintSourceAudit,\n    budget_thresholds as _read_budget_thresholds,\n    sprint_string_list,\n)\n",
    )

    replace_once(
        "src/secretary/sprints.py",
        '''SOURCE_AUDIT_FIELDS = ("created_at", "updated_at", "board")\n# Charged restart types contribute to total and thresholds.\nBUDGET_EVENT_TYPES = (\n    "red_review",\n    "blocked",\n    "red_ci",\n    "preempt",\n    "recreated_task",\n    "hotfix",\n)\n# Infrastructure bring-up failures are visible but never spend restart budget.\nBUDGET_UNCHARGED_INFRASTRUCTURE = "infrastructure_blocked"\nBUDGET_UNCHARGED_EVENT_TYPES = (BUDGET_UNCHARGED_INFRASTRUCTURE,)\nBUDGET_RECORDED_EVENT_TYPES = BUDGET_EVENT_TYPES + BUDGET_UNCHARGED_EVENT_TYPES\n# Uncharged counts stay outside computed sprint_budget metadata.\nBUDGET_UNCHARGED_FIELD = "sprint_budget_uncharged"\nDEFAULT_BUDGET_SIGNAL = 3\nDEFAULT_BUDGET_HARD = 6\nDEFAULT_OPEN_SPRINT_LIMIT = 1\nMAX_OPEN_SPRINT_LIMIT = 2\n# Observer freshness is based on card transitions, not status-read time.\nRESUME_FRESHNESS_GRACE_SECONDS = 5 * 60\nRESUME_FIELDS = (\n    "selected_step",\n    "selected_why",\n    "rejected_alternatives",\n    "current_task",\n    "dod_state",\n    "next_safe_step",\n)\n''',
        '''DEFAULT_OPEN_SPRINT_LIMIT = 1\nMAX_OPEN_SPRINT_LIMIT = 2\n# Observer freshness is based on card transitions, not status-read time.\nRESUME_FRESHNESS_GRACE_SECONDS = 5 * 60\n''',
    )
    replace_once(
        "src/secretary/sprints.py",
        'SPRINT_STATUSES = {"open", "closed", "stopped"}\n# Terminal sprint states reject semantic writes, so their resume freshness is stable.\nSPRINT_TERMINAL_STATUSES = {"closed", "stopped"}\n',
        'SPRINT_STATUSES = {state.value for state in SprintState}\n# Terminal sprint states reject semantic writes, so their resume freshness is stable.\nSPRINT_TERMINAL_STATUSES = {SprintState.CLOSED.value, SprintState.STOPPED.value}\n',
    )
    replace_once(
        "src/secretary/sprints.py",
        '''def budget_thresholds(config: dict[str, Any] | None = None) -> dict[str, int]:\n    """Read installation budget limits, retaining safe defaults for old installations."""\n    raw = (config or {}).get("sprint_budget") if isinstance(config, dict) else {}\n    raw = raw if isinstance(raw, dict) else {}\n    signal = _positive_int(raw.get("signal")) or DEFAULT_BUDGET_SIGNAL\n    hard = _positive_int(raw.get("hard")) or DEFAULT_BUDGET_HARD\n    if hard < signal:\n        raise TaskError("validation", "sprint budget hard threshold must not be below signal threshold", 2)\n    return {"signal": signal, "hard": hard}\n''',
        '''def budget_thresholds(config: dict[str, Any] | None = None) -> dict[str, int]:\n    """Read installation budget limits, retaining safe defaults for old installations."""\n    try:\n        return _read_budget_thresholds(config)\n    except ValueError as exc:\n        raise TaskError("validation", str(exc), 2) from None\n''',
    )

    replace_once(
        "src/secretary/sprints.py",
        '        task_id = _task_id(raw)\n        repositories = _json_list(meta.get("sprint_repositories"))\n        budget = _budget(meta.get("sprint_budget"), self.thresholds, meta.get(BUDGET_UNCHARGED_FIELD))\n',
        '        task_id = _task_id(raw)\n        read = SprintReadMetadata.from_legacy(meta, thresholds=self.thresholds, now=_now)\n        repositories = list(read.repositories)\n        budget = read.budget.to_document()\n',
    )
    replace_once(
        "src/secretary/sprints.py",
        '            "status": meta.get("sprint_status") if meta.get("sprint_status") in SPRINT_STATUSES else "open",\n',
        '            "status": read.state.value,\n',
    )
    replace_once(
        "src/secretary/sprints.py",
        '                "source": _source_audit(meta.get("sprint_source_audit")),\n',
        '                "source": read.source_audit.to_document() if read.source_audit is not None else None,\n',
    )
    replace_once(
        "src/secretary/sprints.py",
        '        resume = _resume(meta.get("sprint_resume"))\n',
        '        resume = read.resume.to_document() if read.resume is not None else None\n',
    )

    replace_once(
        "src/secretary/sprints.py",
        '''def _json_list(value: str | None) -> list[str]:\n    try:\n        raw = json.loads(value or "[]")\n    except ValueError:\n        return []\n    return _unique_strings(raw) if isinstance(raw, list) else []\n''',
        '''def _json_list(value: str | None) -> list[str]:\n    """Released private compatibility alias around the typed Sprint read boundary."""\n    return sprint_string_list(value)\n''',
    )
    replace_once(
        "src/secretary/sprints.py",
        '''def _budget(\n    value: Any = None,\n    thresholds: dict[str, int] | None = None,\n    uncharged: Any = None,\n) -> dict[str, Any]:\n    """The normalized budget: charged counts that move the thresholds, uncharged counts beside them.\n\n    `uncharged` is the separately stored quantity; where it is not given, an already normalized\n    budget passed as `value` carries its own. Both families default to zero for every type, so a\n    sprint stored before a type existed reads as zero rather than as an error.\n    """\n    source = value if isinstance(value, dict) else {}\n    if isinstance(value, str):\n        try:\n            source = json.loads(value)\n        except ValueError:\n            source = {}\n    by_type = source.get("by_type") if isinstance(source, dict) else {}\n    counts = {event_type: _budget_count(by_type, event_type) for event_type in BUDGET_EVENT_TYPES}\n    if uncharged is None:\n        uncharged = source.get("uncharged") if isinstance(source, dict) else {}\n    if isinstance(uncharged, str):\n        try:\n            uncharged = json.loads(uncharged)\n        except ValueError:\n            uncharged = {}\n    spare = {event_type: _budget_count(uncharged, event_type) for event_type in BUDGET_UNCHARGED_EVENT_TYPES}\n    limits = thresholds or budget_thresholds()\n    # Deliberately only the charged counts: an uncharged outcome is visible, and moves nothing.\n    total = sum(counts.values())\n    return {\n        "total": total,\n        "by_type": counts,\n        "uncharged": spare,\n        "thresholds": limits,\n        "signal_reached": total >= limits["signal"],\n        "hard_reached": total >= limits["hard"],\n    }\n\n\ndef _budget_count(counts: Any, event_type: str) -> int:\n    if not isinstance(counts, dict):\n        return 0\n    try:\n        return max(0, int(counts.get(event_type, 0)))\n    except (TypeError, ValueError):\n        return 0\n''',
        '''def _budget(\n    value: Any = None,\n    thresholds: dict[str, int] | None = None,\n    uncharged: Any = None,\n) -> dict[str, Any]:\n    """Released private compatibility projection of :class:`SprintBudget`."""\n    return SprintBudget.from_legacy(value, thresholds=thresholds, uncharged=uncharged).to_document()\n''',
    )
    replace_once(
        "src/secretary/sprints.py",
        '''def _source_audit(value: Any) -> dict[str, str] | None:\n    """The audit metadata a restored sprint was recreated from, when it has one."""\n    source = value\n    if isinstance(value, str):\n        try:\n            source = json.loads(value or "null")\n        except ValueError:\n            return None\n    if not isinstance(source, dict):\n        return None\n    result = {field: _text(source.get(field)) for field in SOURCE_AUDIT_FIELDS}\n    return result if any(result.values()) else None\n''',
        '''def _source_audit(value: Any) -> dict[str, str] | None:\n    """Released private compatibility projection of :class:`SprintSourceAudit`."""\n    source = SprintSourceAudit.from_legacy(value)\n    return source.to_document() if source is not None else None\n''',
    )
    replace_once(
        "src/secretary/sprints.py",
        '''def _resume(value: Any, *, required: bool = False) -> dict[str, Any] | None:\n    source = value\n    if isinstance(value, str):\n        try:\n            source = json.loads(value)\n        except ValueError:\n            source = None\n    if not isinstance(source, dict):\n        if required:\n            raise TaskError("validation", "resume entry must be a JSON object", 2)\n        return None\n    missing = [\n        field\n        for field in RESUME_FIELDS\n        if not isinstance(source.get(field), str) or not source[field].strip()\n    ]\n    if missing:\n        if required:\n            raise TaskError("validation", "resume entry is missing required fields: " + ", ".join(missing), 2)\n        return None\n    recorded_at = _text(source.get("recorded_at")) or _now()\n    if required and _timestamp(recorded_at) is None:\n        raise TaskError("validation", "resume recorded_at must include a timezone", 2)\n    return {**{field: source[field].strip() for field in RESUME_FIELDS}, "recorded_at": recorded_at}\n''',
        '''def _resume(value: Any, *, required: bool = False) -> dict[str, Any] | None:\n    try:\n        resume = SprintResume.from_legacy(value, required=required, now=_now)\n    except ValueError as exc:\n        raise TaskError("validation", str(exc), 2) from None\n    if resume is None:\n        return None\n    if required and _timestamp(resume.recorded_at) is None:\n        raise TaskError("validation", "resume recorded_at must include a timezone", 2)\n    return resume.to_document()\n''',
    )

    # import_board.py: consume the public compatibility boundary, not sprints.py internals.
    replace_once(
        "src/secretary/board/import_board.py",
        '''**Legacy task normalization has one compatibility codec.**  The task column/metadata\nvocabularies and pure wire-format parsers come from :mod:`secretary.board.legacy_codec`; the task\nreader and restore path use the same functions.  Sprint-specific ``_budget``, ``_resume``,\n``_source_audit`` and ``_json_list`` remain in :mod:`secretary.sprints` until that larger model is\nmigrated.  What this module adds is only the part that has no\nreader today: the §8.1 marker rule, which is deliberately *stricter* than\n``tasks._normalize_comment``.\n''',
        '''**Legacy normalization has explicit compatibility boundaries.**  Task column/metadata\nvocabularies and pure wire-format parsers come from :mod:`secretary.board.legacy_codec`; Sprint\ncompound metadata is parsed by :mod:`secretary.board.sprint_read`.  The live readers and this\none-shot importer therefore share public normalization rules instead of importing private feature\nhelpers.  What this module adds is only the part that has no reader today: the §8.1 marker rule,\nwhich is deliberately *stricter* than ``tasks._normalize_comment``.\n''',
    )
    replace_once(
        "src/secretary/board/import_board.py",
        '''from secretary.board.roles import BOARD_ROLES\nfrom secretary.board.task_routing import (\n''',
        '''from secretary.board.roles import BOARD_ROLES\nfrom secretary.board.sprint_read import (\n    BUDGET_RECORDED_EVENT_TYPES,\n    BUDGET_UNCHARGED_EVENT_TYPES,\n    BUDGET_UNCHARGED_FIELD,\n    RESUME_FIELDS,\n    SPRINT_STATE_VALUES,\n    sprint_budget_document,\n    sprint_resume_document,\n    sprint_source_audit_document,\n    sprint_string_list,\n)\nfrom secretary.board.task_routing import (\n''',
    )
    replace_once(
        "src/secretary/board/import_board.py",
        '''from secretary.sprints import (\n    BUDGET_RECORDED_EVENT_TYPES,\n    BUDGET_UNCHARGED_EVENT_TYPES,\n    BUDGET_UNCHARGED_FIELD,\n    RESUME_FIELDS,\n    SPRINT_BOARD_NAME,\n    SPRINT_REFERENCE_PREFIX,\n    SPRINT_STATUSES,\n    _budget,\n    _json_list,\n    _resume,\n    _source_audit,\n)\n''',
        '''from secretary.sprints import SPRINT_BOARD_NAME, SPRINT_REFERENCE_PREFIX\n''',
    )
    regex_replace("src/secretary/board/import_board.py", r"(?<![A-Za-z0-9_])_budget\(", "sprint_budget_document(")
    regex_replace("src/secretary/board/import_board.py", r"(?<![A-Za-z0-9_])_json_list\(", "sprint_string_list(")
    regex_replace("src/secretary/board/import_board.py", r"(?<![A-Za-z0-9_])_resume\(", "sprint_resume_document(")
    regex_replace(
        "src/secretary/board/import_board.py",
        r"(?<![A-Za-z0-9_])_source_audit\(",
        "sprint_source_audit_document(",
    )
    content = read("src/secretary/board/import_board.py")
    content = re.sub(r"\bSPRINT_STATUSES\b", "SPRINT_STATE_VALUES", content)
    write("src/secretary/board/import_board.py", content)

    # Expand the incremental type gate and register the focused unit suite.
    replace_once(
        "pyproject.toml",
        '    "src/secretary/board/task_routing.py",\n',
        '    "src/secretary/board/task_routing.py",\n    "src/secretary/board/sprint_read.py",\n',
    )
    replace_once(
        "tests/ci-shards.txt",
        "unit tests/test_sprint_listing_budget.py\n",
        "unit tests/test_sprint_listing_budget.py\nunit tests/test_sprint_read.py\n",
    )


if __name__ == "__main__":
    main()
