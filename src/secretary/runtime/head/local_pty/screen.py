"""A bounded text screen for judging PTY progress, not for rendering a terminal.

Only cursor placement, erasure and scrolling affect the text snapshot. Other terminal controls
are consumed so a colour shimmer cannot become new text. The parser retains at most one short CSI
parameter string across chunks; OSC and other strings retain no payload.
"""

from __future__ import annotations

import codecs
import unicodedata

MAX_ROWS = 120
MAX_COLS = 240
_CSI_PARAMETER_MAX = 64
_COMBINING_MAX = 8


def _size(value: int, limit: int) -> int:
    try:
        return min(max(int(value), 1), limit)
    except (TypeError, ValueError, OverflowError):
        return 1


class _Buffer:
    def __init__(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self.cells = [[" "] * cols for _ in range(rows)]
        self.row = 0
        self.col = 0
        self.scroll_top = 0
        self.scroll_bottom = rows - 1
        self.wrap_next = False
        self.saved = (0, 0)

    def resize(self, rows: int, cols: int) -> None:
        cells = [[" "] * cols for _ in range(rows)]
        for row in range(min(rows, self.rows)):
            cells[row][: min(cols, self.cols)] = self.cells[row][: min(cols, self.cols)]
        self.rows, self.cols, self.cells = rows, cols, cells
        self.row = min(self.row, rows - 1)
        self.col = min(self.col, cols - 1)
        self.scroll_top, self.scroll_bottom = 0, rows - 1
        self.saved = (min(self.saved[0], rows - 1), min(self.saved[1], cols - 1))
        self.wrap_next = False

    def linefeed(self) -> None:
        self.wrap_next = False
        if self.scroll_top <= self.row <= self.scroll_bottom and self.row == self.scroll_bottom:
            self.cells.pop(self.scroll_top)
            self.cells.insert(self.scroll_bottom, [" "] * self.cols)
        else:
            self.row = min(self.row + 1, self.rows - 1)

    def carriage_return(self) -> None:
        self.col = 0
        self.wrap_next = False

    def backspace(self) -> None:
        self.col = max(0, self.col - 1)
        self.wrap_next = False

    def tab(self) -> None:
        self.col = min((self.col // 8 + 1) * 8, self.cols - 1)
        self.wrap_next = False

    def _clear_cell(self, row: int, col: int) -> None:
        cells = self.cells[row]
        if cells[col] == "" and col:
            cells[col - 1] = " "
        elif col + 1 < self.cols and cells[col + 1] == "":
            cells[col + 1] = " "
        cells[col] = " "

    def put(self, char: str) -> None:
        if unicodedata.combining(char):
            previous = self.col - 1 if not self.wrap_next else self.col
            if previous >= 0 and self.cells[self.row][previous] not in ("", " "):
                cell = self.cells[self.row][previous]
                if len(cell) < _COMBINING_MAX:
                    self.cells[self.row][previous] = cell + char
            return
        width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if self.wrap_next or (width == 2 and self.col == self.cols - 1):
            self.linefeed()
            self.carriage_return()
        if width == 2 and self.cols == 1:
            return
        self._clear_cell(self.row, self.col)
        if width == 2:
            self._clear_cell(self.row, self.col + 1)
        self.cells[self.row][self.col] = char
        if width == 2:
            self.cells[self.row][self.col + 1] = ""
        self.col += width
        if self.col >= self.cols:
            self.col = self.cols - 1
            self.wrap_next = True

    def move(self, row: int | None = None, col: int | None = None) -> None:
        if row is not None:
            self.row = min(max(row, 0), self.rows - 1)
        if col is not None:
            self.col = min(max(col, 0), self.cols - 1)
        self.wrap_next = False

    def erase_line(self, mode: int) -> None:
        if mode == 0:
            start, end = self.col, self.cols
        elif mode == 1:
            start, end = 0, self.col + 1
        elif mode == 2:
            start, end = 0, self.cols
        else:
            return
        for col in range(start, end):
            self._clear_cell(self.row, col)

    def erase_display(self, mode: int) -> None:
        if mode in (2, 3):
            for row in range(self.rows):
                self.cells[row] = [" "] * self.cols
        elif mode == 0:
            self.erase_line(0)
            for row in range(self.row + 1, self.rows):
                self.cells[row] = [" "] * self.cols
        elif mode == 1:
            self.erase_line(1)
            for row in range(self.row):
                self.cells[row] = [" "] * self.cols

    def lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.cells]


class ScreenModel:
    """Text cells from an arbitrary PTY byte stream, within a fixed memory bound."""

    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        self.rows = _size(rows, MAX_ROWS)
        self.cols = _size(cols, MAX_COLS)
        self._main = _Buffer(self.rows, self.cols)
        self._alternate = _Buffer(self.rows, self.cols)
        self._alternate_active = False
        self._state = "ground"
        self._parameters = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("ignore")

    @property
    def _buffer(self) -> _Buffer:
        return self._alternate if self._alternate_active else self._main

    def resize(self, rows: int, cols: int) -> None:
        self.rows = _size(rows, MAX_ROWS)
        self.cols = _size(cols, MAX_COLS)
        self._main.resize(self.rows, self.cols)
        self._alternate.resize(self.rows, self.cols)

    def lines(self) -> list[str]:
        return self._buffer.lines()

    def feed(self, data: bytes) -> None:
        for byte in data:
            state = self._state
            if state == "ground":
                self._ground(byte)
            elif state == "esc":
                if byte == ord("["):
                    self._state, self._parameters = "csi", ""
                elif byte in b"]P^_X":
                    self._state = "string"
                elif byte in b"()*+-./#%":
                    self._state = "esc_intermediate"
                else:
                    self._state = "ground"
                    if byte == ord("7"):
                        self._buffer.saved = (self._buffer.row, self._buffer.col)
                    elif byte == ord("8"):
                        self._buffer.move(*self._buffer.saved)
            elif state == "esc_intermediate":
                if 0x30 <= byte <= 0x7E:
                    self._state = "ground"
                elif byte == 0x1B:
                    self._state = "esc"
            elif state == "csi":
                if 0x40 <= byte <= 0x7E:
                    self._control(chr(byte))
                    self._state = "ground"
                elif 0x20 <= byte <= 0x3F:
                    if len(self._parameters) < _CSI_PARAMETER_MAX:
                        self._parameters += chr(byte)
                elif byte == 0x1B:
                    self._state = "esc"
                else:
                    self._state = "ground"
            elif state == "string":
                if byte == 0x07:
                    self._state = "ground"
                elif byte == 0x1B:
                    self._state = "string_esc"
            elif state == "string_esc":
                if byte in (ord("\\"), 0x07):
                    self._state = "ground"
                elif byte != 0x1B:
                    self._state = "string"

    def _ground(self, byte: int) -> None:
        if byte == 0x1B:
            self._decoder.reset()
            self._state = "esc"
        elif byte == 0x9B and not self._decoder.getstate()[0]:
            self._state, self._parameters = "csi", ""
        elif byte in (0x90, 0x98, 0x9D, 0x9E, 0x9F) and not self._decoder.getstate()[0]:
            self._state = "string"
        elif byte == 0x0D:
            self._decoder.reset()
            self._buffer.carriage_return()
        elif byte in (0x0A, 0x0B, 0x0C):
            self._decoder.reset()
            self._buffer.linefeed()
        elif byte == 0x08:
            self._decoder.reset()
            self._buffer.backspace()
        elif byte == 0x09:
            self._decoder.reset()
            self._buffer.tab()
        elif byte < 0x20 or byte == 0x7F:
            self._decoder.reset()
        else:
            for char in self._decoder.decode(bytes((byte,))):
                if char.isprintable():
                    self._buffer.put(char)

    def _control(self, final: str) -> None:
        raw = self._parameters
        private = raw.startswith("?")
        if private:
            raw = raw[1:]
        values = [int(item) if item.isdigit() else 0 for item in raw.split(";")[:8]]
        first = values[0] if values else 0
        buffer = self._buffer
        count = max(1, first)
        if private:
            if final in "hl" and any(value in (47, 1049) for value in values):
                if final == "h":
                    if 1049 in values:
                        self._alternate = _Buffer(self.rows, self.cols)
                    self._alternate_active = True
                else:
                    self._alternate_active = False
            return
        if final == "A":
            buffer.move(row=buffer.row - count)
        elif final == "B":
            buffer.move(row=buffer.row + count)
        elif final == "C":
            buffer.move(col=buffer.col + count)
        elif final == "D":
            buffer.move(col=buffer.col - count)
        elif final == "E":
            buffer.move(row=buffer.row + count, col=0)
        elif final == "F":
            buffer.move(row=buffer.row - count, col=0)
        elif final == "G":
            buffer.move(col=count - 1)
        elif final in "Hf":
            buffer.move(row=count - 1, col=(max(1, values[1]) if len(values) > 1 else 1) - 1)
        elif final == "d":
            buffer.move(row=count - 1)
        elif final == "J":
            buffer.erase_display(first)
        elif final == "K":
            buffer.erase_line(first)
        elif final == "r":
            top = count - 1
            bottom = (values[1] if len(values) > 1 and values[1] else buffer.rows) - 1
            if 0 <= top < bottom < buffer.rows:
                buffer.scroll_top, buffer.scroll_bottom = top, bottom
            else:
                buffer.scroll_top, buffer.scroll_bottom = 0, buffer.rows - 1
            buffer.move(row=0, col=0)
