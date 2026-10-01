"""Leave a synchronous systemd scope running after its dispatcher caller exits.

The supervisor must stay as systemd-run's command for the scope's MemoryMax to
remain active. This short-lived launcher waits only until the supervisor has
started, then its systemd-run child is reparented and keeps the scope alive.
"""

from __future__ import annotations

import sys
import os

from .scoped_lifecycle import ScopedHeadLifecycle
from .scope_environment import EnvironmentTransferError, exec_scope


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[0] == "--exec-gated":
        fd = int(args[1])
        released = os.read(fd, 1) == b"1"
        os.close(fd)
        if not released:
            return 1
        try:
            exec_scope(args[2:])
        except (EnvironmentTransferError, OSError, ValueError):
            print("scoped environment launch refused", file=sys.stderr)
            return 1
    return ScopedHeadLifecycle.launch_until_started(args)


if __name__ == "__main__":  # pragma: no cover - launched as a separate process
    raise SystemExit(main())
