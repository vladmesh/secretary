"""The bounded PTY text screen on cursor controls, resizes and hostile bytes."""

from __future__ import annotations

import random
import unittest

from secretary.runtime.head.local_pty.screen import MAX_COLS, MAX_ROWS, ScreenModel


class ScreenModelTests(unittest.TestCase):
    def test_cursor_moves_and_text_controls(self) -> None:
        screen = ScreenModel(4, 12)
        screen.feed(b"\x1b[2;4H")
        for control, position in (
            (b"\x1b[1A", (0, 3)),
            (b"\x1b[2B", (2, 3)),
            (b"\x1b[1C", (2, 4)),
            (b"\x1b[2D", (2, 2)),
            (b"\x1b[1E", (3, 0)),
            (b"\x1b[2F", (1, 0)),
            (b"\x1b[6G", (1, 5)),
            (b"\x1b[4d", (3, 5)),
            (b"\x1b[2;3f", (1, 2)),
        ):
            with self.subTest(control=control):
                screen.feed(control)
                self.assertEqual((screen._buffer.row, screen._buffer.col), position)
        screen.feed(b"AB\bZ\rQ\tT")
        self.assertEqual(screen.lines()[1], "Q AZ    T")

    def test_cursor_erase_wide_text_and_alternate_screen(self) -> None:
        screen = ScreenModel(3, 12)
        screen.feed("a界b".encode())
        self.assertEqual(screen.lines()[0], "a界b")
        screen.feed(b"\x1b[1;2H\x1b[0KZ")
        self.assertEqual(screen.lines()[0], "aZ")
        screen.feed(b"\x1b[?1049hALT\x1b[?1049l")
        self.assertEqual(screen.lines()[0], "aZ")
        screen.feed(b"\x1b[?47h")
        self.assertEqual(screen.lines()[0], "ALT")

    def test_scroll_region_and_resize(self) -> None:
        screen = ScreenModel(3, 6)
        screen.feed(b"\x1b[1;1Htop\x1b[2;1Hmiddle\x1b[3;1Hbottom")
        screen.feed(b"\x1b[2;3r\x1b[3;1H\n")
        self.assertEqual(screen.lines()[0], "top")
        self.assertEqual(screen.lines()[1], "bottom")
        screen.resize(1, 1)
        self.assertEqual((screen.rows, screen.cols), (1, 1))
        self.assertEqual(len(screen.lines()), 1)

    def test_utf8_title_continuation_byte_does_not_end_control_string(self) -> None:
        screen = ScreenModel(2, 40)
        screen.feed(b"hello")
        for byte in "\x1b]0;✳ Task.md review\x07".encode():
            screen.feed(bytes((byte,)))
        screen.feed(b" world")
        self.assertEqual(screen.lines()[0], "hello world")
        screen.feed(b"\x1bPignored\x9cstill ignored\x1b\\!")
        self.assertEqual(screen.lines()[0], "hello world!")

    def test_control_table_and_random_bytes_are_total_and_bounded(self) -> None:
        cases = (
            b"\x1b[99999999999A",
            b"\x1b[99999999999;99999999999H",
            b"\x1b[2J\x1b[1K\x1b[2;3r",
            b"\x1b[",
            b"\x1b]unfinished OSC",
            b"\x1bPunfinished DCS",
            b"\xff\xfe\xc0\x80\xe2\x9c",
            b"\x1b[?2026h\x1b[?25l\x1b[?1049h\x1b[?1049l",
        )
        for data in cases:
            with self.subTest(data=data):
                screen = ScreenModel(0, -3)
                screen.feed(data[:3])
                screen.feed(data[3:])
                screen.resize(-1, 0)
                screen.resize(10**100, 10**100)
                self.assertLessEqual(screen.rows, MAX_ROWS)
                self.assertLessEqual(screen.cols, MAX_COLS)
                self.assertLessEqual(len(screen.lines()), MAX_ROWS)
                self.assertTrue(all(len(line) <= MAX_COLS * 8 for line in screen.lines()))
                self.assertLessEqual(len(screen._parameters), 64)
        rng = random.Random(1738)
        screen = ScreenModel(4, 8)
        for _ in range(1000):
            screen.feed(rng.randbytes(rng.randrange(0, 50)))
            if rng.randrange(20) == 0:
                screen.resize(rng.randrange(-10, 300), rng.randrange(-10, 500))
            self.assertLessEqual(len(screen.lines()), MAX_ROWS)
            self.assertTrue(all(len(line) <= MAX_COLS * 8 for line in screen.lines()))
            self.assertLessEqual(len(screen._parameters), 64)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
