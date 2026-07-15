"""Shared dispatcher exceptions and selectors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class DispatcherError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


class HostError(Exception):
    pass


@dataclass(frozen=True)
class PilotSelector:
    reference: str

    @classmethod
    def exact(cls, reference: str | None) -> "PilotSelector":
        value = (reference or "").strip()
        if not value:
            raise DispatcherError("pilot_selector_required", "dispatcher requires an exact pilot ref")
        return cls(value)

    def accepts(self, task: dict[str, Any]) -> bool:
        return task.get("ref") == self.reference
