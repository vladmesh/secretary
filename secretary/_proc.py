"""One thin, explicit gateway to ordinary child processes."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


def run(
    argv: Sequence[str],
    *,
    input: str | bytes | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    cwd: str | Path | None = None,
    check: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        input=input,
        env=env,
        timeout=timeout,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=text,
    )
