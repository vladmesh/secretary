from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "src/secretary/tasks.py",
    "src/secretary/task_commands.py",
    "src/secretary/restore.py",
    "src/secretary/board/import_board.py",
)


def base(path: str) -> None:
    data = subprocess.check_output(["git", "show", f"origin/main:{path}"], cwd=ROOT)
    (ROOT / path).write_bytes(data)


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:90]!r}")
    write(path, text.replace(old, new, 1))


def replace_all(path: str, old: str, new: str, *, minimum: int = 1) -> None:
    text = read(path)
    count = text.count(old)
    if count < minimum:
        raise RuntimeError(f"{path}: expected at least {minimum} matches, found {count}: {old!r}")
    write(path, text.replace(old, new))


def patch_tasks() -> None:
    path = "src/secretary/tasks.py"
    replace_once(
        path,
        "    enum_or_default as _enum_or_default,\n    enum_or_none as _enum_or_none,\n",
        "    enum_or_default as _enum_or_default,  # noqa: F401 - released private compatibility alias\n"
        "    enum_or_none as _enum_or_none,  # noqa: F401 - released private compatibility alias\n",
    )
    roles = '''from secretary.board.roles import (
    BOARD_ROLES,
    COMMENT_ROLES,
    CREATE_ROLES,
    EDIT_ROLES,
    PROPOSAL_CREATE_ROLES,
    Role,
)
'''
    replace_once(
        path,
        roles,
        roles
        + '''from secretary.board.task_routing import (
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
''',
    )
    replace_once(
        path,
        '''_TASK_TYPES = {"code", "research"}
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
''',
        '''# Retired launch modes normalize away rather than silently changing a requested shape.
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
''',
    )
    replace_once(
        path,
        '''            "project": _text(meta.get("project")),
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
''',
        '''            **TaskMetadata.from_legacy(meta, codex_modes=_CODEX_LAUNCH_MODES).to_document_fields(),
''',
    )
    replace_all(path, 'complexity: str = "standard",', "complexity: str = TaskComplexity.STANDARD.value,", minimum=2)
    replace_all(path, 'family_preference: str = "auto",', "family_preference: str = FamilyPreference.AUTO.value,", minimum=2)
    replace_once(
        path,
        '        complexity = complexity.strip() or "standard"\n        family_preference = family_preference.strip() or "auto"\n',
        "        complexity = complexity.strip() or TaskComplexity.STANDARD.value\n"
        "        family_preference = family_preference.strip() or FamilyPreference.AUTO.value\n",
    )
    replace_once(
        path,
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
        path,
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
                "validation", "family preference must be one of: " + ", ".join(sorted(FAMILY_PREFERENCE_VALUES)), 2
            ) from None
        family_preference = family_preference_value.value
''',
    )
    replace_all(path, 'task_type="research"', "task_type=TaskType.RESEARCH.value")
    replace_once(
        path,
        '        if steward_report and (task_type != "research" or not slug or reference or sprint):\n',
        "        if steward_report and (task_type != TaskType.RESEARCH.value or not slug or reference or sprint):\n",
    )
    replace_once(
        path,
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
                "blocked reports require --classification, one of " + ", ".join(BLOCK_CLASSIFICATION_VALUES),
                2,
            )
        if kind == "done" and classification:
            raise TaskError("validation", "a done report carries no classification", 2)
        classification_value = BlockClassification(classification) if classification else None
''',
    )
    replace_once(path, '                "classification": classification or None,\n', '                "classification": classification_value.value if classification_value is not None else None,\n')
    replace_once(
        path,
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
    replace_once(path, '            if kind == "rework":\n', "            if decision_kind is TaskDecision.REWORK:\n")
    replace_once(
        path,
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
        path,
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
        path,
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
    replace_once(path, '        if source == "assessment" and target in _UNDECIDED_EXITS and role in _DECISION_BOUND_ROLES:\n', '        if source == "assessment" and target in UNDECIDED_EXITS and role in _DECISION_BOUND_ROLES:\n')
    replace_once(path, '        if decision and not self._decision_recorded(task["ref"], decision):\n', '        if decision_kind is not None and not self._decision_recorded(task["ref"], decision_kind.value):\n')
    replace_all(path, "_EDITABLE_STATES", "EDITABLE_STATES")


def patch_commands() -> None:
    path = "src/secretary/task_commands.py"
    replace_once(
        path,
        "from secretary.board.backend import card_client\nfrom secretary.board.roles import BOARD_ROLES, CREATE_ROLES, EDIT_ROLES, Role\n",
        "from secretary.board.backend import card_client\n"
        "from secretary.board.models import CardState\n"
        "from secretary.board.roles import BOARD_ROLES, CREATE_ROLES, EDIT_ROLES, Role\n"
        "from secretary.board.task_routing import (\n"
        "    BlockClassification,\n    FamilyPreference,\n    TaskComplexity,\n    TaskDecision,\n    TaskType,\n)\n",
    )
    replace_once(path, "from secretary.tasks import (\n    _BLOCK_CLASSIFICATIONS,\n    TaskError,\n", "from secretary.tasks import (\n    TaskError,\n")
    replace_all(path, 'choices=("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done")', "choices=tuple(state.value for state in CardState)", minimum=2)
    replace_once(path, '    task_create.add_argument("--type", required=True, choices=("code", "research"))\n', '    task_create.add_argument("--type", required=True, choices=tuple(kind.value for kind in TaskType))\n')
    replace_once(path, '    task_create.add_argument("--state", choices=("issues", "ready"), default="ready")\n', '    task_create.add_argument("--state", choices=(CardState.ISSUES.value, CardState.READY.value), default=CardState.READY.value)\n')
    replace_once(path, '        "--complexity", choices=("cheap", "standard", "hard", "frontier"), default="standard"\n', '        "--complexity", choices=tuple(value.value for value in TaskComplexity), default=TaskComplexity.STANDARD.value\n')
    replace_once(path, '    task_create.add_argument("--family-preference", choices=("auto", "claude", "codex"), default="auto")\n', '    task_create.add_argument("--family-preference", choices=tuple(value.value for value in FamilyPreference), default=FamilyPreference.AUTO.value)\n')
    replace_once(path, '            command.add_argument("--classification", default="", choices=("", *_BLOCK_CLASSIFICATIONS))\n', '            command.add_argument("--classification", default="", choices=("", *(value.value for value in BlockClassification)))\n')
    replace_once(path, '            command.add_argument("--kind", required=True, choices=("release", "rework", "reslice"))\n', '            command.add_argument("--kind", required=True, choices=tuple(value.value for value in TaskDecision))\n')
    replace_once(path, '            command.add_argument("--decision", default="", choices=("", "release", "rework", "reslice"))\n', '            command.add_argument("--decision", default="", choices=("", *(value.value for value in TaskDecision)))\n')


def patch_restore() -> None:
    path = "src/secretary/restore.py"
    replace_once(path, "    enum_or_default as _enum_or_default,\n", "    enum_or_default as _enum_or_default,  # noqa: F401 - released private compatibility alias\n")
    replace_once(path, "from secretary.board.normalized_checkpoint import NormalizedBoardError, validated_normalized_cards\n", "from secretary.board.normalized_checkpoint import NormalizedBoardError, validated_normalized_cards\nfrom secretary.board.task_routing import TaskMetadata\n")
    replace_once(
        path,
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


def patch_importer() -> None:
    path = "src/secretary/board/import_board.py"
    replace_once(
        path,
        "from secretary.board.roles import BOARD_ROLES\n",
        "from secretary.board.roles import BOARD_ROLES\n"
        "from secretary.board.task_routing import (\n    FAMILY_PREFERENCE_VALUES,\n    TASK_COMPLEXITY_VALUES,\n    TASK_TYPE_VALUES,\n)\n",
    )
    replace_once(
        path,
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
    replace_all(path, "_TASK_TYPES", "TASK_TYPE_VALUES")
    replace_all(path, "_COMPLEXITIES", "TASK_COMPLEXITY_VALUES")
    replace_all(path, "_FAMILY_PREFERENCES", "FAMILY_PREFERENCE_VALUES")


def main() -> None:
    for path in FILES:
        base(path)
    patch_tasks()
    patch_commands()
    patch_restore()
    patch_importer()


if __name__ == "__main__":
    main()
