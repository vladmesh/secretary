#!/usr/bin/env python3
"""Required local cross-repository gate for the memory-mcp restore contract.

Run with ``MEMORY_MCP_REPO=/path/to/memory-mcp`` and optionally
``MEMORY_MCP_PYTHON=/path/to/python``. This is intentionally outside unittest
discovery: GitHub CI does not have the private memory-mcp checkout.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo = Path(os.environ.get("MEMORY_MCP_REPO", "/home/dev/memory-mcp")).expanduser().resolve()
    python = Path(os.environ.get("MEMORY_MCP_PYTHON", str(repo / ".venv/bin/python"))).expanduser()
    selftest = repo / "selftest.py"
    if not python.is_file() or not selftest.is_file():
        print("memory-mcp restore e2e: checkout or Python environment is unavailable", file=sys.stderr)
        return 2
    return subprocess.run([str(python), str(selftest)], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
