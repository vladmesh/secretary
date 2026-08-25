"""A trivial child for the local-pty substrate tests: a real process on a real terminal.

It is deliberately not an agent. What the substrate has to prove is ownership of a process — a
session of its own, a terminal that resizes, bytes in and bytes out, an exit status that is read
back — and every one of those is visible with a program that echoes lines.
"""
from __future__ import annotations

import fcntl
import signal
import struct
import sys
import termios


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
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if line == "quit":
            print("BYE", flush=True)
            return 0
        if line.startswith("exit "):
            print("BYE", flush=True)
            return int(line.split()[1])
        if line.startswith("spew "):
            count = int(line.split()[1])
            for index in range(count):
                print(f"{index:06d} " + "x" * 993, flush=True)
            print("SPEWDONE", flush=True)
            continue
        print(f"ECHO {line}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
