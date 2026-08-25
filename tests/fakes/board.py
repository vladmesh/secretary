"""What every fake board client shares with the real one."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class BatchedCalls:
    """Answer a batched read the way the transport does: one result per call, in call order.

    A fake has no round trips to save, so the batch is the calls it stands for. Answering it here
    keeps every fake's `call` the single description of what the board returns.
    """

    def call_batch(self, calls: Iterable[tuple[str, dict[str, Any]]]) -> list[Any]:
        return [self.call(method, **params) for method, params in calls]  # type: ignore[attr-defined]
