"""A product checkout whose managed venv is the interpreter running this suite.

`role_env exec` refuses to start an observer, steward, retro or curator head unless the product root
has an executable `.venv/bin/python3`. A test checkout (and CI's) has no `.venv`, so a test that
really launches such a role points `TA_SECRETARY_REPO` at one of these instead: its `src` is this
checkout's and its `.venv` is the running interpreter's own prefix, dependencies included.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def managed_product_root(parent: Path) -> Path:
    """`<parent>/product`, a checkout with a real managed interpreter at `.venv/bin/python3`."""
    root = parent / "product"
    root.mkdir()
    (root / "src").symlink_to(REPO / "src", target_is_directory=True)
    (root / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    if not (root / ".venv" / "bin" / "python3").is_file():
        raise RuntimeError(f"the running interpreter's prefix {sys.prefix} has no bin/python3")
    return root
