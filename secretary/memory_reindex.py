"""Offline, atomic rebuild of the secretary memory index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import memory_service


def rebuild(canon, export, target_db, model, dim, document_embed=None, allow_empty=False) -> dict:
    """Build an index from an explicit canon snapshot."""
    return memory_service.offline_rebuild(
        canon, export, target_db, model, dim, document_embed, allow_empty
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canon", required=True, type=Path, help="markdown facts root")
    parser.add_argument("--export", required=True, type=Path, help="published export.ndjson")
    parser.add_argument("--target-db", required=True, type=Path, help="sqlite index to replace")
    parser.add_argument("--model", required=True, help="fastembed model id")
    parser.add_argument("--dim", required=True, type=int, help="embedding dimension")
    parser.add_argument("--allow-empty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        print(json.dumps(rebuild(**vars(parse_args(argv))), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
