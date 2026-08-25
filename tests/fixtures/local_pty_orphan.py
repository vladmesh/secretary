"""A head that outlives its supervisor: it ignores the hangup its terminal gets and keeps running.

The one fact a backend must never collapse is "the socket stopped answering" into "the head has
ended", and the only way to test it is with a head that really is still there once its supervisor
is gone. A shell loop is not that head: when the supervisor dies the pty master closes, everything
in the foreground group is hung up, and whether a shell survives that depends on the shell.

So this one says so explicitly — `SIGHUP` ignored, nothing read from the terminal, nothing written
to it — and it stays up until it is killed. Its only output is one line, so a test can tell that it
started.
"""
from __future__ import annotations

import signal
import sys
import time


def main() -> int:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    print("ORPHAN", flush=True)
    while True:
        time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main())
