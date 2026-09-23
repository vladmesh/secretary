"""A head that behaves like an agent's composer, and says what it was asked to do.

The failure of issue:70562b15a7dc8764437e was not a byte that went missing: every byte of the launch
prompt reached Claude's terminal, and the prompt sat in its composer unsent, because a line feed is a
line break there and only a carriage return sends. A line-reading fixture cannot tell those apart, so
this one takes its terminal in raw mode and keeps a composer the way the TUI does:

  * it draws a banner, then goes quiet, as an agent does once its composer is up;
  * a printable byte or a line feed goes into the composer and is echoed back, nothing more;
  * a carriage return sends the composer: the text is appended to the record file named by the first
    argument as one JSON line, the composer is emptied, and the head "works" — it prints a few
    kilobytes over about half a second, as an agent's redraw and spinner do. An Enter over an empty
    composer sends nothing and prints nothing;
  * `--deaf-enter` makes it ignore carriage returns altogether: the composer it was typed into is the
    composer it keeps, which is the head a prompt must never be reported as delivered to.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import termios
import time
import tty


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("record")
    parser.add_argument("--deaf-enter", action="store_true")
    parser.add_argument("--banner-delay", type=float, default=0.3)
    args = parser.parse_args()

    fd = sys.stdin.fileno()
    if os.isatty(fd):
        tty.setraw(fd, termios.TCSANOW)
    out = sys.stdout.buffer

    def write(data: bytes) -> None:
        out.write(data)
        out.flush()

    time.sleep(args.banner_delay)
    write(b"fake agent v1\r\n" + b"=" * 200 + b"\r\n> ")
    composer = bytearray()
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            return 0
        for byte in chunk:
            if byte == 0x0D:
                if args.deaf_enter or not composer.strip():
                    continue
                text = composer.decode("utf-8", "replace")
                composer.clear()
                with open(args.record, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"submitted": text}) + "\n")
                for step in range(10):
                    write(f"\r\n* working {step} ".encode() + b"." * 400)
                    time.sleep(0.05)
                write(b"\r\ndone\r\n> ")
            else:
                composer.append(byte)
                write(bytes([byte]))


if __name__ == "__main__":
    sys.exit(main())
