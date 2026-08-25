"""A head that says, record by record, what its own terminal actually handed it.

Every other fixture here is read *about* — through a receipt, a journal record or a supervisor's
byte count. This one is read *from*: it reports what it takes off its own file descriptor, so a
test can check the one thing no receipt can prove, which is whether two payloads reached the head
as one sentence.

It reads slowly and never stops. Slowly, because a head that takes `--chunk` bytes every `--pause`
seconds is one a large payload cannot be written to in time, so the delivery stops part-way and
leaves a fragment on the terminal; never stopping, because the fragment then has to be taken in
for the head to be able to say what it was — and because a payload written behind that fragment
would be taken in with it, in the same read, which is exactly the gluing being tested for.

A record is `LINE` when the bytes ended with a newline and `FRAG` when the writer never sent one
and nothing more arrived for `--idle` seconds. A payload abandoned mid-flight loses its own
newline, so it can only ever be a `FRAG` — unless something else was written behind it, in which
case that something else's newline finishes it and the two are reported as one `LINE`.

Each record carries its length, the distinct printable bytes in it, and its first and last few
bytes, because that is what tells a fragment of one payload from a fragment of one payload with
another payload welded onto its end.
"""
from __future__ import annotations

import argparse
import os
import select
import sys
import time

_EDGE = 6


def report(kind: str, line: bytes) -> None:
    kinds = "".join(sorted({chr(byte) for byte in line if 32 <= byte < 127}))
    head = line[:_EDGE].decode("utf-8", "replace")
    tail = line[-_EDGE:].decode("utf-8", "replace")
    print(f"{kind} len={len(line)} kinds={kinds} head={head} tail={tail}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunk", type=int, default=4096)
    parser.add_argument("--pause", type=float, default=0.5, help="seconds between reads")
    parser.add_argument("--idle", type=float, default=0.5, help="quiet before a fragment is said")
    args = parser.parse_args()

    print("UP", flush=True)
    buffer = b""
    while True:
        if not select.select([0], [], [], args.idle)[0]:
            if buffer:
                report("FRAG", buffer)
                buffer = b""
            continue
        data = os.read(0, args.chunk)
        if not data:
            if buffer:
                report("FRAG", buffer)
            return 0
        buffer += data
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            report("LINE", line)
        time.sleep(args.pause)


if __name__ == "__main__":
    sys.exit(main())
