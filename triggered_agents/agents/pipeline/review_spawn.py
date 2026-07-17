"""Reviewer spawn failure handlers for Validate layer 3."""
from __future__ import annotations

from collections.abc import Callable

from . import ops, worker
from .state import STATE


def block_inject_delivery(
    ref: str,
    clear_review: Callable[[dict], None],
    rec: dict,
    records: dict,
    pr: str | None,
    review_head: str,
    note: str,
    error: Exception,
) -> bool:
    clear_review(rec)
    scrubbed = worker.scrub_secrets(str(error))
    ops.add_comment("dispatcher", ref,
                    f"Не удалось поднять голову-ревьюера (слой 3): inject не доставлен "
                    f"в TUI, turn не стартовал после bounded delivery protocol. Карточка в Blocked "
                    f"до vladmesh. {note}")
    ops.move_card("dispatcher", ref, "Blocked")
    records.pop(ref, None)
    STATE.log_run("review", reference=ref, to="Blocked", reason="inject-delivery",
                  error=scrubbed, pr=pr, review_head=review_head)
    return True
