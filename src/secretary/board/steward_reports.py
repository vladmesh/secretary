"""Canonical Secretary adapter for the steward's report-card lifecycle."""

from __future__ import annotations

from typing import Any, Literal

from secretary.tasks import TaskReader, TaskWriter


class StewardReportBoard:
    """A small structural adapter for a dispatch-owned report-board port.

    The port itself belongs to the triggered-agent runtime, which keeps that
    scheduler independent of Secretary.  This implementation intentionally
    imports no runtime protocol: matching its three methods is enough and keeps
    the dependency directed from the product domain to the adapter.
    """

    def __init__(
        self, reader: TaskReader, writer: TaskWriter, *, actor: str = "triggered-agent-dispatch"
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.actor = actor

    def create_report(self, *, project: str, title: str, slug: str) -> str:
        result = self.writer.create_steward_report(
            actor=self.actor,
            project=project,
            title=title,
            slug=slug,
        )
        return str(result["task"]["ref"])

    def in_progress_reports(self, *, project: str) -> list[dict[str, Any]]:
        return self.reader.steward_reports_in_progress(project)

    def move_report(self, *, reference: str, target: Literal["done", "blocked"], reason: str) -> None:
        self.writer.move(
            role="steward",
            actor=self.actor,
            reference=reference,
            target=target,
            reason=reason,
        )


class StewardSignalBoard:
    """Secretary-owned implementation of the steward's structural read port."""

    def __init__(self, reader: TaskReader) -> None:
        self.reader = reader

    def active_cards(
        self, *, states: set[str] | None = None, project: str | None = None
    ) -> list[dict[str, Any]]:
        return self.reader.steward_signal_cards(states=states, project=project)
