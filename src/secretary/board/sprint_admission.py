"""Typed Sprint admission resources and local reservation index.

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
