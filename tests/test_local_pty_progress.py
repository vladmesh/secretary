"""Progress normalization and bounded per-turn folding without a running head."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from secretary.runtime.head.local_pty import protocol, supervisor as module


class ProgressLinesTests(unittest.TestCase):
    def test_normalization_table(self) -> None:
        first = b"\x1b[2A\xe2\x9c\xa2 Thinking 12s 456 tokens\r\n"
        same = b"\x1b]0;title\x07\x1b[1A\xe2\x9c\xb6 Thinking 93s 999 tokens\n"
        for candidate, equal in (
            (same, True),
            (b"* Thinking 1s 2 tokens\n", True),
            ("⠋ Thinking 7s 8 tokens\n".encode(), True),
            (b"Thinking 1s 2 tokens\n", True),
            (b"Planning 1s 2 tokens\n", False),
            (b"Thinking 1s 2 tokens\nNew finding\n", False),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(module._progress_lines(first) == module._progress_lines(candidate), equal)
        self.assertEqual(module._progress_lines(b"\x1b[1A\r  \n\x1b]0;x\x07"), set())


class ProgressFoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.supervisor = module.Supervisor(
            run_dir=Path("/tmp"), run_id="unit", role="worker", task="unit", command="true"
        )
        self.addCleanup(self.supervisor._selector.close)
        self.events: list[tuple[str, dict]] = []
        self.supervisor._append = lambda kind, **fields: self.events.append((kind, fields))  # type: ignore[method-assign]
        self._start_turn()

    def _start_turn(self) -> None:
        self.supervisor._turn_open = False
        delivery = module._Delivery(1, b"x", "unit", 1.0)
        delivery.written = 1
        self.supervisor._delivery = delivery
        self.supervisor._finish_delivery(protocol.DELIVERY_COMPLETE, "")

    def _window(self, data: bytes) -> None:
        self.supervisor._record_output(data)
        self.supervisor._flush_progress()

    def test_fold_keeps_bytes_and_counts_windows_until_new_content(self) -> None:
        self._window(b"* Thinking 1s 2 tokens\n")
        self._window("✶ Thinking 3s 4 tokens\n".encode())
        self._window("⠋ Thinking 5s 6 tokens\n".encode())
        progress = [fields for kind, fields in self.events if kind == module.PROVIDER_PROGRESSED]
        self.assertEqual(len(progress), 1)
        self._window(b"New finding\n")
        progress = [fields for kind, fields in self.events if kind == module.PROVIDER_PROGRESSED]
        self.assertEqual(len(progress), 2)
        self.assertEqual(progress[-1]["folded_windows"], 2)
        self.assertEqual(
            progress[-1]["output_bytes"],
            len("✶ Thinking 3s 4 tokens\n⠋ Thinking 5s 6 tokens\nNew finding\n".encode()),
        )

    def test_seen_cap_and_turn_reset(self) -> None:
        with mock.patch.object(module, "PROGRESS_SEEN_LINES_MAX", 2):
            for line in (b"alpha\n", b"beta\n", b"gamma\n"):
                self._window(line)
            self.assertEqual(len(self.supervisor._progress_seen), 2)
            self._window(b"alpha\n")
            count = len([kind for kind, _ in self.events if kind == module.PROVIDER_PROGRESSED])
            self.assertEqual(count, 3)
            self._start_turn()
            self.assertEqual(self.supervisor._progress_seen, set())
            self._window(b"alpha\n")
            self.assertEqual(len([kind for kind, _ in self.events if kind == module.PROVIDER_PROGRESSED]), 4)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
