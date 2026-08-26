"""A head that reads its terminal at a pace the caller chooses — or never reads it at all.

The substrate's hardest promise is about a head that is slower than the person addressing it: the
socket must keep answering, and what a delivery is doing must be something to ask about rather than
something to wait through. Neither property is visible with a head that reads as fast as it is
written to, so this one reads `--chunk` bytes, sleeps `--pause` seconds, and says how much it has
taken so far.

With a pause longer than any test, it is also the head that never reads at all: it prints `UP`,
sleeps, and its terminal fills.
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunk", type=int, default=4096)
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument("--total", type=int, default=0, help="say READ <n> once this much is read")
    args = parser.parse_args()

    print("UP", flush=True)
    read = 0
    reported = False
    while True:
        time.sleep(args.pause)
        data = os.read(0, args.chunk)
        if not data:
            return 0
        read += len(data)
        if args.total and not reported and read >= args.total:
            # Said once, and then this head stays up. A head that exited on the last byte of a
            # delivery would take its supervisor's socket with it, and a test reading the line it
            # just printed would be racing that exit rather than checking the delivery.
            print(f"READ {read}", flush=True)
            reported = True


if __name__ == "__main__":
    sys.exit(main())
