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
    input: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a child in its own process group and reap that group on abnormal exit.

    This is the one place that bounds what a production tick waits on with a timeout. The
    dispatcher unit sets ``KillMode=process`` so the local-pty heads a tick launches outlive it
    (secretary-1699), so the unit's control-group kill no longer sweeps what a timed-out child
    left behind: a gate's test processes under ``bash -lc``, a provider CLI under a probe shell,
    Git's ``git-remote-https`` helper. Killing the child's whole group does.

    A command that deliberately leaves its group (``setsid``, a daemon) is outside this contract:
    it is not swept, and it can hold the reap open for at most the grace period.
    """
    process = subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
        text=True,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        stdout, stderr = _kill_and_reap(process)
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
    except BaseException:
        _kill_and_reap(process)
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


# How long the output of a killed group may keep flowing, and then how long its leader may take to
# die. Only a descendant that left the group (its own `setsid`) or one this process may not signal
# can still hold the pipes open after the kill; waiting on it would turn a bounded child into an
# unbounded wait.
_REAP_GRACE_SECONDS = 5.0


def _kill_and_reap(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Kill an isolated child's complete process group, then reap its leader within the grace.

    ``killpg`` signals every member this process may signal and fails with EPERM only when it may
    signal none of them, e.g. a non-root caller whose group holds only a setuid ``sudo`` and the
    other-uid command under it. Then the leader alone is tried, and whatever cannot be signalled is
    left running rather than raised or waited on without bound. A root caller (the only one that
    crosses identity through ``runuser``, which keeps its child in the group) may signal everyone.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            process.kill()
        except (ProcessLookupError, PermissionError):
            pass
    try:
        return process.communicate(timeout=_REAP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        try:
            process.wait(timeout=_REAP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        return "", ""
