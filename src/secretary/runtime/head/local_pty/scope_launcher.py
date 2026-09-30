"""Leave a synchronous systemd scope running after its dispatcher caller exits.

The supervisor must stay as systemd-run's command for the scope's MemoryMax to
remain active. This short-lived launcher waits only until the supervisor has
started, then its systemd-run child is reparented and keeps the scope alive.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from . import protocol
from .journal import RUN_STARTED, read_events


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    run_dir, log_path, timeout = Path(arguments[0]), Path(arguments[1]), float(arguments[2])
    command = arguments[3:]
    with log_path.open("ab", buffering=0) as log:
        scope = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(event.get("kind") == RUN_STARTED for event in read_events(run_dir / protocol.JOURNAL_NAME).events):
            return 0
        if (run_dir / protocol.STARTUP_ERROR_NAME).exists():
            return 0
        status = scope.poll()
        if status is not None:
            return status
        time.sleep(0.05)
    return 0  # The client retains the startup deadline and its bounded log diagnostic.


if __name__ == "__main__":  # pragma: no cover - launched as a separate process
    raise SystemExit(main())
