"""A redraw-heavy child for the real-pty progress journal test."""

import sys
import time

sys.stdin.readline()
glyphs = "✢✶✻✽*"
verbs = ("Thinking", "Working")
for frame in range(400):
    sys.stdout.write(
        f"\x1b[1A\r{glyphs[frame % len(glyphs)]} {verbs[frame % len(verbs)]} "
        f"{frame // 100}s {frame * 37} tokens\n"
    )
    sys.stdout.flush()
    time.sleep(0.01)
time.sleep(1.2)
sys.stdout.write("Found a new result\nChecked a second fact\n")
sys.stdout.flush()
time.sleep(2.5)
