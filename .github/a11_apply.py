from __future__ import annotations

from pathlib import Path


def replace_between(text: str, start: str, end: str, replacement: str) -> str:
    left = text.index(start)
    right = text.index(end, left)
    return text[:left] + replacement.rstrip() + "\n\n" + text[right:]


root = Path(__file__).resolve().parents[1]

module = '''"""Typed Sprint admission resources and local reservation index.

The live Sprint protocol still exposes dict-shaped documents at compatibility boundaries. This
module owns the closed admission/resource shape used internally by collision checks and by the
versioned local guard index, so those rules no longer pass ``dict[str, Any]`` through domain code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from secretary.board.models import SprintState


def _document_strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


@dataclass(frozen=True, slots=True)
class SprintAdmission:
    """The resources needed to decide whether one Sprint may be open."""

    ref: str
    product: str
    reservations: tuple[str, ...]
    repositories: tuple[str, ...]
    state: SprintState | None

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> SprintAdmission:
        raw_state = str(document.get("status") or "")
        try:
            state: SprintState | None = SprintState(raw_state)
        except ValueError:
            state = None
        return cls(
            ref=str(document.get("ref") or ""),
            product=str(document.get("product") or "").strip(),
            reservations=_document_strings(document.get("reservations")),
            repositories=_document_strings(document.get("repositories")),
            state=state,
        )

    @property
    def is_open(self) -> bool:
        return self.state is SprintState.OPEN

    @property
    def guard_projects(self) -> tuple[str, ...]:
        return tuple(sorted({project.strip() for project in self.reservations if project.strip()}))


@dataclass(frozen=True, slots=True)
class SprintReservationIndex:
    """Immutable project -> open Sprint references index used by write guards."""

    entries: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def from_document(cls, value: Any, *, version: int) -> SprintReservationIndex | None:
        if not isinstance(value, Mapping) or value.get("version") != version:
            return None
        projects = value.get("projects")
        if not isinstance(projects, Mapping):
            return None
        entries: list[tuple[str, tuple[str, ...]]] = []
        for project, refs in projects.items():
            name = str(project)
            if not name or not isinstance(refs, list):
                continue
            entries.append((name, tuple(sorted({str(ref) for ref in refs if str(ref)}))))
        return cls(entries=tuple(sorted(entries)))

    @classmethod
    def from_sprints(cls, sprints: list[SprintAdmission]) -> SprintReservationIndex:
        index = cls()
        for sprint in sprints:
            if sprint.is_open:
                index = index.with_sprint(sprint)
        return index

    def to_projects_document(self) -> dict[str, list[str]]:
        return {project: list(refs) for project, refs in self.entries}

    def to_document(self, *, version: int) -> dict[str, Any]:
        return {"version": version, "projects": self.to_projects_document()}

    def without_sprint(self, reference: str) -> SprintReservationIndex:
        entries = []
        for project, refs in self.entries:
            kept = tuple(ref for ref in refs if ref != reference)
            if kept:
                entries.append((project, kept))
        return SprintReservationIndex(entries=tuple(entries))

    def with_sprint(self, sprint: SprintAdmission) -> SprintReservationIndex:
        if not sprint.ref or not sprint.is_open:
            return self
        projects = self.to_projects_document()
        for project in sprint.guard_projects:
            projects[project] = sorted(set(projects.get(project, []) + [sprint.ref]))
        return SprintReservationIndex(
            entries=tuple((project, tuple(refs)) for project, refs in sorted(projects.items()))
        )


__all__ = ["SprintAdmission", "SprintReservationIndex"]
'''
(root / "src/secretary/board/sprint_admission.py").write_text(module, encoding="utf-8")


tests = '''from __future__ import annotations

import unittest

from secretary.board.models import SprintState
from secretary.board.sprint_admission import SprintAdmission, SprintReservationIndex


class SprintAdmissionModelTests(unittest.TestCase):
    def test_document_boundary_types_the_closed_admission_shape(self) -> None:
        admission = SprintAdmission.from_document(
            {
                "ref": "sprint:42",
                "product": " product:a ",
                "reservations": [" beta ", "alpha", "alpha", ""],
                "repositories": ["/repo/a", "/repo/b"],
                "status": "open",
                "ignored": {"legacy": True},
            }
        )

        self.assertEqual(admission.ref, "sprint:42")
        self.assertEqual(admission.product, "product:a")
        self.assertEqual(admission.reservations, (" beta ", "alpha", "alpha", ""))
        self.assertEqual(admission.repositories, ("/repo/a", "/repo/b"))
        self.assertIs(admission.state, SprintState.OPEN)
        self.assertTrue(admission.is_open)
        self.assertEqual(admission.guard_projects, ("alpha", "beta"))

    def test_unknown_state_does_not_become_an_open_guard_reservation(self) -> None:
        admission = SprintAdmission.from_document(
            {"ref": "sprint:42", "status": "future", "reservations": ["secretary"]}
        )

        self.assertIsNone(admission.state)
        self.assertFalse(admission.is_open)
        self.assertEqual(SprintReservationIndex.from_sprints([admission]).entries, ())

    def test_guard_index_round_trip_preserves_released_versioned_shape(self) -> None:
        raw = {
            "version": 2,
            "projects": {
                "secretary": ["sprint:2", "sprint:1", "sprint:1", ""],
                "other": [],
                "ignored": "not-a-list",
            },
        }

        index = SprintReservationIndex.from_document(raw, version=2)

        self.assertIsNotNone(index)
        assert index is not None
        self.assertEqual(
            index.to_document(version=2),
            {
                "version": 2,
                "projects": {"other": [], "secretary": ["sprint:1", "sprint:2"]},
            },
        )
        self.assertIsNone(SprintReservationIndex.from_document(raw, version=3))
        self.assertIsNone(SprintReservationIndex.from_document({"version": 2}, version=2))

    def test_index_updates_are_immutable_and_keep_open_sprints_only(self) -> None:
        first = SprintAdmission.from_document(
            {"ref": "sprint:1", "status": "open", "reservations": ["secretary"]}
        )
        second = SprintAdmission.from_document(
            {"ref": "sprint:2", "status": "open", "reservations": ["secretary", "other"]}
        )
        closed = SprintAdmission.from_document(
            {"ref": "sprint:3", "status": "closed", "reservations": ["secretary"]}
        )

        index = SprintReservationIndex.from_sprints([first, second, closed])
        removed = index.without_sprint("sprint:1")

        self.assertEqual(
            index.to_projects_document(),
            {"other": ["sprint:2"], "secretary": ["sprint:1", "sprint:2"]},
        )
        self.assertEqual(
            removed.to_projects_document(),
            {"other": ["sprint:2"], "secretary": ["sprint:2"]},
        )
        self.assertEqual(index.to_projects_document()["secretary"], ["sprint:1", "sprint:2"])


if __name__ == "__main__":
    unittest.main()
'''
(root / "tests/test_sprint_admission.py").write_text(tests, encoding="utf-8")


sprints_path = root / "src/secretary/sprints.py"
sprints = sprints_path.read_text(encoding="utf-8")
import_anchor = "from secretary.board.models import SprintState\n"
import_text = """from secretary.board.models import SprintState
from secretary.board.sprint_admission import SprintAdmission, SprintReservationIndex
"""
if import_anchor not in sprints:
    raise RuntimeError("sprint admission import anchor moved")
sprints = sprints.replace(import_anchor, import_text, 1)

guard_block = '''def active_sprint_projects(data_dir: str | Path) -> dict[str, list[str]]:
    """Return the local index of projects reserved by open sprints."""
    index = _read_guard_index(Path(data_dir) / _GUARD_INDEX)
    return index.to_projects_document() if index is not None else {}


def _read_guard_index(path: Path) -> SprintReservationIndex | None:
    """Return the typed index, or None when it is absent, unreadable or of an older version."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return SprintReservationIndex.from_document(raw, version=_GUARD_INDEX_VERSION)


def sprint_guard_index_initialized(data_dir: str | Path) -> bool:
    return _read_guard_index(Path(data_dir) / _GUARD_INDEX) is not None


def require_active_sprint_projects(data_dir: str | Path) -> dict[str, list[str]]:
    """The reserved-project index, or a refusal naming the file that could not answer.

    :func:`active_sprint_projects` answers `{}` for an index that is missing, unreadable or of
    another version, which is the right answer for the write guard -- nothing is reserved that
    cannot be proven reserved. A *read* that reports which reservations a close released cannot use
    it: "this sprint holds no project any more" and "nobody could read the index" are the opposite
    answers, so this one refuses instead of flattening them together.
    """
    index = _read_guard_index(Path(data_dir) / _GUARD_INDEX)
    if index is None:
        raise TaskError(
            "backend_error",
            f"the reserved-project index {_GUARD_INDEX} is missing, unreadable or of another version",
            1,
        )
    return index.to_projects_document()


def refresh_active_sprint_projects(data_dir: str | Path, reader: Any) -> None:
    """Seed the index from the live board without racing a sprint mutation."""
    with _sprint_guard_index_lock(data_dir):
        _replace_active_sprint_projects(data_dir, reader.list(statuses={"open"}, create=False))


def _replace_active_sprint_projects(data_dir: str | Path, sprints: list[dict[str, Any]]) -> None:
    admissions = [SprintAdmission.from_document(sprint) for sprint in sprints]
    _write_guard_index(data_dir, SprintReservationIndex.from_sprints(admissions))


def update_active_sprint_projects(data_dir: str | Path, sprint: dict[str, Any]) -> None:
    """Update one sprint's entries in the local reserved-project index."""
    with _sprint_guard_index_lock(data_dir):
        path = Path(data_dir) / _GUARD_INDEX
        index = _read_guard_index(path)
        if index is None and path.exists():
            # Rebuild stale index key spaces from the board.
            path.unlink()
            return
        admission = SprintAdmission.from_document(sprint)
        index = (index or SprintReservationIndex()).without_sprint(admission.ref)
        if admission.is_open:
            index = index.with_sprint(admission)
        _write_guard_index(data_dir, index)
'''
sprints = replace_between(
    sprints,
    "def active_sprint_projects(data_dir: str | Path)",
    "@contextmanager\ndef _sprint_guard_index_lock",
    guard_block,
)

write_block = '''def _write_guard_index(data_dir: str | Path, index: SprintReservationIndex) -> None:
    path = Path(data_dir) / _GUARD_INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            index.to_document(version=_GUARD_INDEX_VERSION),
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
'''
sprints = replace_between(
    sprints,
    "def _write_guard_index(data_dir: str | Path",
    "def budget_thresholds",
    write_block,
)

admission_block = '''def open_sprint_admission_error(rows: list[dict[str, Any]], *, limit: int) -> str | None:
    """Why this set of open sprints could not have been admitted, or None if it could.

    A whole set is judged by admitting it one row at a time, in reference order, against the rows
    already accepted: an export must not be a way to arrive at a pair `create` would have refused.
    """
    admissions = [SprintAdmission.from_document(row) for row in rows]
    admitted: list[SprintAdmission] = []
    for row in sorted(admissions, key=lambda row: row.ref):
        try:
            _refuse_open_sprint(row, admitted, limit=limit)
        except TaskError as exc:
            return f"{row.ref or '?'}: {exc.message}"
        admitted.append(row)
    return None


def _refuse_open_sprint(
    candidate: SprintAdmission,
    others: list[SprintAdmission],
    *,
    limit: int,
) -> None:
    """Refuse a sprint this installation has no room, or no disjoint room, for.

    Every collision the caller can act on is reported before the generic count refusal, because a
    resource refusal names the sprint holding the resource while the count refusal distinguishes
    none of them.
    """
    saturated = len(others) >= limit
    _refuse_shared_reservations(candidate.reservations, others)
    if limit > DEFAULT_OPEN_SPRINT_LIMIT:
        _refuse_shared_resources(candidate, others)
    if saturated:
        raise _open_sprint_count_error(others, limit)


def _open_sprint_count_error(others: list[SprintAdmission], limit: int) -> TaskError:
    refs = ", ".join(sorted(sprint.ref for sprint in others))
    if limit == DEFAULT_OPEN_SPRINT_LIMIT:
        return TaskError(
            "sprint_conflict",
            f"installation already has an open sprint: {refs}; close it before opening another",
            2,
        )
    return TaskError(
        "sprint_conflict",
        f"installation already holds its limit of {limit} open sprints: {refs}; "
        "close one before opening another",
        2,
    )


def _refuse_shared_reservations(
    reservations: tuple[str, ...], others: list[SprintAdmission]
) -> None:
    """Refuse a project another open sprint already reserves, naming both."""
    held: dict[str, str] = {}
    for sprint in others:
        for project in sprint.reservations:
            held.setdefault(project, sprint.ref)
    clashes = [(project, held[project]) for project in reservations if project in held]
    if clashes:
        raise TaskError(
            "resource_conflict",
            "project(s) already reserved by an open sprint: "
            + ", ".join(f"{project} held by {ref}" for project, ref in sorted(clashes)),
            2,
        )


def _refuse_shared_resources(candidate: SprintAdmission, others: list[SprintAdmission]) -> None:
    """The invariants that make a second open sprint safe, above the reservations.

    Two open sprints may only exist while nothing they work on is shared: a different product, no
    shared project reservation, and no repository tree either contains. Overlap includes nesting
    and is judged on canonical paths; a stored root that is not already absolute is refused rather
    than resolved here, because the tree it names would depend on the resolving process's cwd.

    The candidate's own roots are judged before any pairwise comparison and whether or not another
    sprint is open.
    """
    product = candidate.product
    ordered = sorted(others, key=lambda row: row.ref)
    # Judge both sides so disjointness does not depend on iteration order.
    if ordered and not product:
        raise TaskError(
            "resource_conflict",
            "this sprint declares no product, so it cannot be proven disjoint from "
            f"open sprint {ordered[0].ref!s}",
            2,
        )
    roots = _scanned_roots(
        candidate.repositories,
        refusal=lambda text, why: TaskError(
            "resource_conflict",
            f"this sprint declares repository root {text!r}, which {why}, so it cannot be "
            "proven disjoint from another open sprint",
            2,
        ),
    )
    for sprint in ordered:
        reference = sprint.ref
        other_product = sprint.product
        if not other_product:
            raise TaskError(
                "resource_conflict",
                f"open sprint {reference} declares no product, so a second open sprint "
                "cannot be proven disjoint from it",
                2,
            )
        if other_product == product:
            raise TaskError(
                "resource_conflict",
                f"product {product} is already the product of open sprint {reference}; "
                "a second open sprint needs a different product",
                2,
            )
        held_roots = _scanned_roots(
            sprint.repositories,
            # `reference` is bound here rather than closed over: the callee calls this back
            # inside the same iteration, but a refusal that named the wrong sprint would be a
            # silent lie, and the binding costs nothing.
            refusal=lambda text, why, reference=reference: TaskError(
                "resource_conflict",
                f"open sprint {reference} declares repository root {text!r}, which {why}, "
                "so a second open sprint cannot be proven disjoint from it",
                2,
            ),
        )
        for held in held_roots:
            clash = next((root for root in roots if _roots_overlap(root, held)), None)
            if clash is not None:
                raise TaskError(
                    "resource_conflict",
                    f"repository root {clash} overlaps {held}, held by open sprint {reference}",
                    2,
                )
'''
sprints = replace_between(
    sprints,
    "def open_sprint_admission_error(rows: list[dict[str, Any]]",
    "def ensure_sprint_board",
    admission_block,
)
sprints_path.write_text(sprints, encoding="utf-8")

pyproject_path = root / "pyproject.toml"
pyproject = pyproject_path.read_text(encoding="utf-8")
anchor = '    "src/secretary/board/sprint_read.py",\n'
addition = anchor + '    "src/secretary/board/sprint_admission.py",\n'
if anchor not in pyproject:
    raise RuntimeError("mypy sprint_read anchor moved")
pyproject = pyproject.replace(anchor, addition, 1)
pyproject_path.write_text(pyproject, encoding="utf-8")
