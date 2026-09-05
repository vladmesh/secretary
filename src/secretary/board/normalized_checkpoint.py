"""One restorable normalized-board contract for producers and consumers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from secretary.product_issues import ProductIssueValidationError, validate_product_issue_records


class NormalizedBoardError(ValueError):
    pass


def validated_normalized_cards(
    board_dir: Path,
    *,
    registered_project_ids: set[str] | None = None,
    require_ndjson: bool = True,
) -> list[dict[str, Any]]:
    """Read and validate the JSON/NDJSON pair that restore actually consumes."""
    try:
        payload = json.loads((board_dir / "cards.json").read_text(encoding="utf-8"))
        cards = payload["cards"]
    except (OSError, ValueError, KeyError, TypeError):
        raise NormalizedBoardError("normalized board export is unavailable") from None
    if not isinstance(cards, list) or any(not isinstance(card, dict) for card in cards):
        raise NormalizedBoardError("normalized board export is invalid")
    ndjson_path = board_dir / "cards.ndjson"
    if ndjson_path.is_file():
        try:
            ndjson = [
                json.loads(line)
                for line in ndjson_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, ValueError, TypeError):
            raise NormalizedBoardError("normalized board NDJSON export is unavailable") from None
        if any(not isinstance(card, dict) for card in ndjson) or cards != ndjson:
            raise NormalizedBoardError("normalized board cards.json/cards.ndjson parity mismatch")
    elif require_ndjson:
        raise NormalizedBoardError("normalized board NDJSON export is unavailable")
    validate_card_records(cards, registered_project_ids=registered_project_ids)
    return cards


def validate_card_records(
    cards: list[dict[str, Any]], *, registered_project_ids: set[str] | None = None
) -> None:
    """Validate the record-level contract when only canonical NDJSON is present."""
    refs = [card.get("reference") for card in cards]
    invalid = [ref for ref in refs if not isinstance(ref, str) or not ref]
    duplicates = sorted({str(ref) for ref in refs if refs.count(ref) > 1})
    if invalid or duplicates:
        detail = f": duplicate references {', '.join(duplicates)}" if duplicates else ""
        raise NormalizedBoardError(f"normalized board export has invalid references{detail}")
    try:
        validate_product_issue_records(cards, registered_project_ids=registered_project_ids)
    except ProductIssueValidationError as exc:
        raise NormalizedBoardError(f"normalized Product/Issue record is invalid: {exc}") from None
