"""Leave a synchronous systemd scope running after its dispatcher caller exits.

The supervisor must stay as systemd-run's command for the scope's MemoryMax to
remain active. This short-lived launcher waits only until the supervisor has
started, then its systemd-run child is reparented and keeps the scope alive.
"""

from __future__ import annotations

import sys

from .scoped_lifecycle import ScopedHeadLifecycle


def main(argv: list[str] | None = None) -> int:
    return ScopedHeadLifecycle.launch_until_started(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover - launched as a separate process
    raise SystemExit(main())
