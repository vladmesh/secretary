"""A child a product run can own: it publishes the result it was given and ends as it was told to.

Deliberately not an agent, and deliberately not the substrate's own echo child either. What the
product runtime claims about a *real* head is a contract about three things — the result file the
head is told the path of, the exit status its supervisor records, and the ending its owner then
settles — and none of them is exercised by a process that only echoes. This one does exactly those
three and nothing else, so a test over it is a test of the product's path rather than of a program.

    result <json>   publish that document to the path in $SECRETARY_RUN_RESULT and then wait to be
                    ended, which is what a head whose work is done does: the product that owns the
                    process is what ends it;
    exit <status>   end immediately with that status, which is what a head whose CLI refused what
                    it was asked for does.
"""

from __future__ import annotations

import json
import os
import sys
import time


def publish(document: str) -> None:
    path = os.environ["SECRETARY_RUN_RESULT"]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(json.loads(document)))
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    mode = sys.argv[1]
    if mode == "result":
        publish(sys.argv[2])
        print("RESULT PUBLISHED", flush=True)
        while True:  # ended by the product that owns this process, never by itself
            time.sleep(0.1)
    if mode == "exit":
        print("REFUSED", flush=True)
        return int(sys.argv[2])
    raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    sys.exit(main())
