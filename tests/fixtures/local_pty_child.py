"""A trivial child for the local-pty substrate tests: a real process on a real terminal.

It is deliberately not an agent. What the substrate has to prove is ownership of a process — a
session of its own, a terminal that resizes, bytes in and bytes out, an exit status that is read
back — and every one of those is visible with a program that echoes lines.
"""
from __future__ import annotations

import fcntl
import os
import signal
import struct
import sys
import termios
import time


def terminal_size() -> tuple[int, int]:
    packed = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
    rows, cols, _, _ = struct.unpack("HHHH", packed)
    return rows, cols


def on_winch(_signum: int, _frame: object) -> None:
    rows, cols = terminal_size()
    print(f"WINCH {rows}x{cols}", flush=True)


def main() -> int:
    signal.signal(signal.SIGWINCH, on_winch)
    rows, cols = terminal_size()
    print(f"SIZE {rows}x{cols}", flush=True)
    while True:
        raw = sys.stdin.readline()
        if not raw:
            return 0
        line = raw.strip()
        if line.startswith("bulk "):
            # The one thing an echo cannot show: how much of a delivery actually arrived. A line
            # discipline that drops the tail of a long write drops it in silence, so the child says
            # the length it read and a caller compares it with the length it sent.
            print(f"BULK {len(raw)}", flush=True)
            continue
        if line == "quit":
            print("BYE", flush=True)
            return 0
        if line.startswith("exit "):
            print("BYE", flush=True)
            return int(line.split()[1])
        if line.startswith("die "):
            # Exit saying nothing at all. A test that needs the *last* thing a head ever produced
            # to be a particular chunk cannot afford a goodbye after it.
            sys.stdout.flush()
            os._exit(int(line.split()[1]))
        if line.startswith("busy "):
            # A turn that stays open for as long as the caller asked. The substrate closes a turn
            # when the head goes quiet, so a head that answers instantly cannot be observed
            # mid-turn without a race; this one keeps printing until its time is up.
            until = time.monotonic() + float(line.split()[1])
            while time.monotonic() < until:
                print("WORKING", flush=True)
                time.sleep(0.1)
            print("DONE", flush=True)
            continue
        if line.startswith("spew "):
            count = int(line.split()[1])
            for index in range(count):
                print(f"{index:06d} " + "x" * 993, flush=True)
            print("SPEWDONE", flush=True)
            continue
        print(f"ECHO {line}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
