from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CODEC = '''"""Stable parsers for the legacy Kanboard task wire format.

Legacy readers, restore code, and the one-shot board importer all have to interpret
old Kanboard rows exactly the same way.  Keep those small, pure normalization rules
here instead of making feature packages import private helpers from ``tasks.py``.
"""

from __future__ import annotations

from typing import Any

TASK_STATE_BY_COLUMN = {
    "Issues": "issues",
    "Ready": "ready",
    "In progress": "in_progress",
    "Validate": "validate",
    "Assessment": "assessment",
    "Blocked": "blocked",
    "Done": "done",
}

TASK_KNOWN_METADATA = {
    "task_type",
    "project",
    "blocked_by",
    "claim",
    "slug",
    "base_branch",
    "seed_ref",
    "supersedes",
    "issues",
    "head",
    "resolved_head",
    "review_head",
    "resolved_review_head",
    "retry_same",
    "retry_switch",
    "retry_heads",
    "complexity",
    "family_preference",
    "routing_reason",
    "quota_snapshot_at",
    "codex_launch_mode",
    "sprint_ref",
}


def text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def null_if_empty(value: Any) -> str | None:
    value_text = text(value)
    return value_text or None


def positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def split_heads(value: Any) -> list[str]:
    return [head for head in text(value).split(",") if head]


def enum_or_default(value: Any, allowed: set[str], default: str) -> str:
    candidate = text(value)
    return candidate if candidate in allowed else default


def enum_or_none(value: Any, allowed: set[str]) -> str | None:
    candidate = text(value)
    return candidate if candidate in allowed else None


__all__ = [
    "TASK_KNOWN_METADATA",
    "TASK_STATE_BY_COLUMN",
    "enum_or_default",
    "enum_or_none",
    "nonnegative_int",
    "null_if_empty",
    "positive_int",
    "split_heads",
    "text",
]
'''

TASK_IMPORT = '''from secretary.board.legacy_codec import (
    TASK_KNOWN_METADATA as _KNOWN_METADATA,
    TASK_STATE_BY_COLUMN as _STATE_BY_COLUMN,
    enum_or_default as _enum_or_default,
    enum_or_none as _enum_or_none,
    nonnegative_int as _nonnegative_int,
    null_if_empty as _null_if_empty,
    positive_int as _positive_int,
    split_heads as _split_heads,
    text as _text,
)
'''

IMPORTER_IMPORT = TASK_IMPORT

RESTORE_IMPORT = '''from secretary.board.legacy_codec import (
    TASK_STATE_BY_COLUMN as _STATE_BY_COLUMN,
    enum_or_default as _enum_or_default,
    positive_int as _positive_int,
)
'''


def _remove_top_level(source: str, *, functions: set[str] = set(), assignments: set[str] = set()) -> str:
    tree = ast.parse(source)
    ranges: list[tuple[int, int]] = []
    found_functions: set[str] = set()
    found_assignments: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            ranges.append((node.lineno, node.end_lineno or node.lineno))
            found_functions.add(node.name)
        if isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            selected = names & assignments
            if selected:
                ranges.append((node.lineno, node.end_lineno or node.lineno))
                found_assignments.update(selected)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in assignments:
            ranges.append((node.lineno, node.end_lineno or node.lineno))
            found_assignments.add(node.target.id)
    missing = (functions - found_functions) | (assignments - found_assignments)
    if missing:
        raise RuntimeError(f"expected top-level definitions not found: {sorted(missing)}")
    lines = source.splitlines(keepends=True)
    for start, end in sorted(ranges, reverse=True):
        del lines[start - 1 : end]
    return "".join(lines)


def _insert_after(source: str, marker: str, addition: str) -> str:
    if addition in source:
        return source
    if source.count(marker) != 1:
        raise RuntimeError(f"expected one insertion marker, found {source.count(marker)}: {marker!r}")
    return source.replace(marker, marker + addition, 1)


def _remove_import_names(source: str, names: tuple[str, ...]) -> str:
    for name in names:
        line = f"    {name},\n"
        count = source.count(line)
        if count != 1:
            raise RuntimeError(f"expected one import line for {name}, found {count}")
        source = source.replace(line, "", 1)
    return source


def patch_tasks() -> None:
    path = ROOT / "src/secretary/tasks.py"
    source = path.read_text(encoding="utf-8")
    source = _insert_after(
        source,
        "from secretary.board.host import MarkerComment, MutationResult, TransitionRequest\n",
        TASK_IMPORT,
    )
    source = _remove_top_level(
        source,
        functions={
            "_text",
            "_null_if_empty",
            "_positive_int",
            "_nonnegative_int",
            "_split_heads",
            "_enum_or_default",
            "_enum_or_none",
        },
        assignments={"_STATE_BY_COLUMN", "_KNOWN_METADATA"},
    )
    path.write_text(source, encoding="utf-8")


def patch_importer() -> None:
    path = ROOT / "src/secretary/board/import_board.py"
    source = path.read_text(encoding="utf-8")
    source = _insert_after(
        source,
        "from secretary.board.backend import card_transport_key, record_key, sprint_reference_number\n",
        IMPORTER_IMPORT,
    )
    source = _remove_import_names(
        source,
        (
            "_KNOWN_METADATA",
            "_STATE_BY_COLUMN",
            "_enum_or_default",
            "_enum_or_none",
            "_nonnegative_int",
            "_null_if_empty",
            "_positive_int",
            "_split_heads",
            "_text",
        ),
    )
    old = '''**The normalizers are the readers' own.**  ``_STATE_BY_COLUMN``, ``_KNOWN_METADATA``,
``_enum_or_default``, ``_split_heads`` and the sprint side's ``_budget``, ``_resume``,
``_source_audit`` and ``_json_list`` are imported from :mod:`secretary.tasks` and
:mod:`secretary.sprints` rather than restated here, so the importer and the reader cannot come
to disagree about what a metadata bag means.  What this module adds is only the part that has no
'''
    new = '''**Legacy task normalization has one compatibility codec.**  The task column/metadata
vocabularies and pure wire-format parsers come from :mod:`secretary.board.legacy_codec`; the task
reader and restore path use the same functions.  Sprint-specific ``_budget``, ``_resume``,
``_source_audit`` and ``_json_list`` remain in :mod:`secretary.sprints` until that larger model is
migrated.  What this module adds is only the part that has no
'''
    if old not in source:
        raise RuntimeError("importer normalization contract paragraph changed unexpectedly")
    source = source.replace(old, new, 1)
    path.write_text(source, encoding="utf-8")


def patch_restore() -> None:
    path = ROOT / "src/secretary/restore.py"
    source = path.read_text(encoding="utf-8")
    source = _insert_after(
        source,
        "from secretary.board.backend import CARD, SPRINT, board_client, entity_number\n",
        RESTORE_IMPORT,
    )
    source = _remove_import_names(source, ("_STATE_BY_COLUMN", "_positive_int"))
    source = _remove_top_level(source, functions={"_enum_or_default"})
    path.write_text(source, encoding="utf-8")


def patch_tests() -> None:
    path = ROOT / "tests/test_tasks.py"
    source = path.read_text(encoding="utf-8")
    marker = "class LegacyTaskCodecTests(unittest.TestCase):"
    if marker in source:
        return
    source += '''\n\nclass LegacyTaskCodecTests(unittest.TestCase):
    def test_reader_restore_and_importer_share_legacy_task_codec(self) -> None:
        import importlib

        from secretary.board import legacy_codec

        importer = importlib.import_module("secretary.board.import_board")
        restore = importlib.import_module("secretary.restore")

        self.assertIs(tasks._STATE_BY_COLUMN, legacy_codec.TASK_STATE_BY_COLUMN)
        self.assertIs(tasks._KNOWN_METADATA, legacy_codec.TASK_KNOWN_METADATA)
        self.assertIs(tasks._text, legacy_codec.text)
        self.assertIs(tasks._positive_int, legacy_codec.positive_int)
        self.assertIs(tasks._nonnegative_int, legacy_codec.nonnegative_int)
        self.assertIs(tasks._null_if_empty, legacy_codec.null_if_empty)
        self.assertIs(tasks._split_heads, legacy_codec.split_heads)
        self.assertIs(tasks._enum_or_default, legacy_codec.enum_or_default)
        self.assertIs(tasks._enum_or_none, legacy_codec.enum_or_none)
        self.assertIs(restore._STATE_BY_COLUMN, legacy_codec.TASK_STATE_BY_COLUMN)
        self.assertIs(restore._positive_int, legacy_codec.positive_int)
        self.assertIs(restore._enum_or_default, legacy_codec.enum_or_default)
        self.assertIs(importer._STATE_BY_COLUMN, legacy_codec.TASK_STATE_BY_COLUMN)
        self.assertIs(importer._KNOWN_METADATA, legacy_codec.TASK_KNOWN_METADATA)
        self.assertIs(importer._enum_or_default, legacy_codec.enum_or_default)
        self.assertIs(importer._split_heads, legacy_codec.split_heads)

    def test_legacy_task_codec_preserves_released_normalization(self) -> None:
        from secretary.board import legacy_codec

        self.assertEqual(legacy_codec.text(None), "")
        self.assertEqual(legacy_codec.text(17), "17")
        self.assertEqual(legacy_codec.positive_int("3"), 3)
        self.assertIsNone(legacy_codec.positive_int("0"))
        self.assertEqual(legacy_codec.nonnegative_int("-3"), 0)
        self.assertEqual(legacy_codec.split_heads("a,,b"), ["a", "b"])
        self.assertEqual(legacy_codec.enum_or_default("x", {"x"}, "d"), "x")
        self.assertEqual(legacy_codec.enum_or_default("y", {"x"}, "d"), "d")
        self.assertIsNone(legacy_codec.enum_or_none("y", {"x"}))
'''
    path.write_text(source, encoding="utf-8")


def main() -> None:
    codec_path = ROOT / "src/secretary/board/legacy_codec.py"
    if codec_path.exists():
        raise RuntimeError(f"refusing to overwrite existing {codec_path.relative_to(ROOT)}")
    codec_path.write_text(CODEC, encoding="utf-8")
    patch_tasks()
    patch_importer()
    patch_restore()
    patch_tests()


if __name__ == "__main__":
    main()
