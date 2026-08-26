#!/usr/bin/env python3
"""Compatibility entry point. The implementation lives in secretary.role_skills."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from secretary.role_skills import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
