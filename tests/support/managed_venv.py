"""A product checkout whose managed venv is the interpreter running this suite.

`role_env exec` requires the product's executable `.venv/bin/python3` for standing roles and for
the worker/reviewer Docker guard. A test checkout (and CI's) has no `.venv`, so a test that really
launches such a role points `TA_SECRETARY_REPO` at one of these instead: its `src` is this
checkout's and its `.venv` is the running interpreter's own prefix, dependencies included.
"""

from __future__ import annotations

import os
import shlex
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


def guarded_product_env(parent: Path) -> dict[str, str]:
    """A managed test product and a native Docker stub for probes that never call Docker.

    The PATH has no host Docker fallback. Any unexpected native call leaves `docker-calls` in
    `parent` and fails; callers assert that the file is absent after their launch probe.
    """
    product = managed_product_root(parent)
    native_bin = parent / "native-bin"
    native_bin.mkdir()
    docker = native_bin / "docker"
    docker.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(parent / 'docker-calls'))}\nexit 97\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return {
        "TA_SECRETARY_REPO": str(product),
        "PATH": str(native_bin) + os.pathsep + str(product / ".venv/bin"),
    }
