from __future__ import annotations

from typing import Any


class MemoryProtocolError(RuntimeError):
    pass


class MemoryValidationError(MemoryProtocolError):
    pass


class MemoryPermissionError(MemoryProtocolError):
    pass


class MemoryLockError(MemoryProtocolError):
    pass


class MemoryExportPublishError(MemoryProtocolError):
    def __init__(self, message: str, *, result: Any) -> None:
        super().__init__(message)
        self.result = result
