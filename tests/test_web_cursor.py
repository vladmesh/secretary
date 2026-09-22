"""The card-history cursor carries one position, a count of committed records, and says so.

A cursor of the pre-2026-09-10 file journal was a byte offset into `events.ndjson` with no `pos`
member. It is refused like any other cursor this reader did not issue (secretary-1673).
"""

from __future__ import annotations

import base64
import json
import unittest

from secretary.webproto.cursor import CURSOR_VERSION, POSITION_ORDINAL, Cursor, decode
from secretary.webproto.errors import InvalidCursor


def _document(document: dict[str, object]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def released_offset_cursor(ref: str, offset: int) -> str:
    """A cursor exactly as the file-journal reader issued it: a byte offset and no `pos`."""
    return _document({"v": CURSOR_VERSION, "ref": ref, "offset": offset})


class CursorTests(unittest.TestCase):
    def test_a_cursor_round_trips_and_names_its_position(self) -> None:
        encoded = Cursor(ref="secretary-1", offset=3).encode()

        self.assertEqual(decode(encoded, ref="secretary-1"), Cursor(ref="secretary-1", offset=3))
        padding = "=" * (-len(encoded) % 4)
        document = json.loads(base64.urlsafe_b64decode(encoded + padding))
        self.assertEqual(document["pos"], POSITION_ORDINAL)

    def test_a_released_byte_offset_cursor_is_refused_like_any_malformed_cursor(self) -> None:
        with self.assertRaises(InvalidCursor) as refused:
            decode(released_offset_cursor("secretary-1", 4212), ref="secretary-1")

        self.assertEqual(refused.exception.code, "validation")
        self.assertIn("fresh task snapshot", str(refused.exception))

    def test_a_released_offset_spelling_is_refused_as_well(self) -> None:
        stated = _document({"v": CURSOR_VERSION, "ref": "secretary-1", "offset": 7, "pos": "offset"})

        with self.assertRaises(InvalidCursor):
            decode(stated, ref="secretary-1")

    def test_other_malformed_cursors_are_refused(self) -> None:
        for label, value in (
            ("empty", ""),
            ("not base64 json", "not-a-cursor"),
            ("another card", Cursor(ref="secretary-2", offset=0).encode()),
            ("negative", _document({"v": CURSOR_VERSION, "ref": "secretary-1", "offset": -1, "pos": POSITION_ORDINAL})),
            ("unknown version", _document({"v": 99, "ref": "secretary-1", "offset": 0, "pos": POSITION_ORDINAL})),
        ):
            with self.subTest(label), self.assertRaises(InvalidCursor):
                decode(value, ref="secretary-1")


if __name__ == "__main__":
    unittest.main()
