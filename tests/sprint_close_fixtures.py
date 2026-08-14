"""The decisions a sprint close requires, for tests whose subject is something else.

`sprint close` states a verdict on every issue the sprint declared and a disposition for
every card it still holds in a working state.  Tests about closing itself write those
verdicts out; the many tests that merely need a closed sprint use this, which keeps every
issue open and takes every unfinished card off the contract.
"""

from __future__ import annotations

from typing import Any


KEEP_OPEN_REASON = "the sprint closed with this issue unfinished"
DROP_REASON = "unfinished when the sprint closed"


def close_decisions(writer: Any, reference: str) -> dict[str, list[dict[str, str]]]:
    sprint = writer.reader.show(reference)
    return {
        "issues": [
            {"ref": str(issue), "verdict": "open", "reason": KEEP_OPEN_REASON}
            for issue in sprint.get("issues") or []
        ],
        "cards": [
            {"ref": str(card["ref"]), "verdict": "drop", "reason": DROP_REASON}
            for card in sprint.get("cards") or []
            if card.get("state") != "done" and card.get("record_type") not in {"product", "issue"}
        ],
    }


def settle_dispatcher_work(data_dir: Any, references: list[str]) -> None:
    """Drop the live-work fields of the dispatcher records of `references`.

    Archiving a card whose head is still running is refused, and a close now archives every
    card it disposes of, so a sprint with work in flight is closed only after that work is
    settled.  A test whose subject is elsewhere settles it here rather than driving a whole
    worker to its end.
    """
    import json
    from pathlib import Path

    path = Path(data_dir) / "dispatcher" / "production-state.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    records = payload.get("records")
    if not isinstance(records, dict):
        return
    for reference in references:
        record = records.get(reference)
        if isinstance(record, dict):
            for field in ("workspace", "handle", "review_handle", "review_leaf"):
                record.pop(field, None)
    path.write_text(json.dumps(payload), encoding="utf-8")
