"""Shared JSON rendering for CLI handlers with deliberately distinct layouts."""

from __future__ import annotations

import json
from typing import Any


def print_json(payload: Any, *, indent: int | None = None, compact: bool = False) -> None:
    separators = (",", ":") if compact else None
    print(json.dumps(payload, ensure_ascii=False, indent=indent, separators=separators, sort_keys=True))
