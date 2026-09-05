"""One thin, explicit gateway to ordinary child processes."""

from __future__ import annotations

import os
import signal
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


def run_isolated(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a child in its own process group and reap that group on abnormal exit."""
    process = subprocess.Popen(
        argv,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stdout, stderr = _kill_and_reap(process)
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
    except BaseException:
        _kill_and_reap(process)
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _kill_and_reap(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Kill an isolated child's complete process group, then reap its leader."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return process.communicate()
