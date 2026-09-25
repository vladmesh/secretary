"""A cell-diff spinner child for the real-pty progress journal test."""

import sys
import time

sys.stdin.readline()
glyphs = "✢✶✻✽*•◦"
sys.stdout.write("\x1b[4;1H\x1b[2K✢ Thinking\x1b[4;20H0000s 000000 tokens")
sys.stdout.flush()
for frame in range(400):
    verb = "Thinking" if frame < 200 else "Working"
    if frame == 200:
        sys.stdout.write("\x1b[4;1H\x1b[2K◦ Working")
    sys.stdout.write(
        f"\x1b[?2026h\x1b[?25l\x1b[4;1H\x1b[38;5;{174 + frame % 4}m"
        f"{glyphs[frame % len(glyphs)]}\x1b[5G\x1b[38;5;{216 + frame % 4}m"
        f"{verb[2]}\x1b[20G\x1b[39m{frame // 100:04d}s {frame * 37:06d} tokens"
        "\x1b[?25h\x1b[?2026l"
    )
    sys.stdout.flush()
    time.sleep(0.01)
time.sleep(1.2)
sys.stdout.write("\x1b[6;1HFound a new result\r\nChecked a second fact\r\n")
sys.stdout.flush()
time.sleep(2.5)
