"""One head's event journal: versioned, append-only, and readable while it is being written.

Three properties are what make this a journal rather than a log file, and each of them is a field:

  * **`schema_version`** is on every record, not on the file, because a reader may open a journal
    written by an older supervisor that is still running. There is no header to miss and no
    per-file negotiation;
  * **`seq`** is a strictly increasing sequence within the run, so a reader can tell "nothing new
    happened" from "I missed something". It is recovered from the file on open, so a supervisor
    that takes an orphaned run dir over does not restart the sequence;
  * **`run_id`** is on every record, so a record is never ambiguous about which head it describes
    once it has been copied out of its file.

Records are single-line JSON appended to a file opened `O_APPEND`, one `write()` per record, then
`fsync`. A `SIGKILL` can therefore leave at most one partial trailing line, and `read_events`
reports that as a truncated tail instead of failing: everything before it is complete and ordered.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JOURNAL_SCHEMA_VERSION = 1

#: The head's process is up and the supervisor owns it.
RUN_STARTED = "run.started"
#: A bounded input was accepted onto the head's pty. An oversized one is refused, and a refusal is
#: not an event: nothing about the head changed.
INPUT_ACCEPTED = "input.accepted"
#: A turn opened — the first accepted input since the head last went quiet.
TURN_STARTED = "turn.started"
#: The head produced output during an open turn, coalesced so that one chatty second is one record.
PROVIDER_PROGRESSED = "provider.progressed"
#: The open turn's head went quiet for the configured settle time.
TURN_FINISHED = "turn.finished"
#: Admission closed: this supervisor takes no further input for this head.
DRAIN_REQUESTED = "drain.requested"
#: A stop was asked for, by a client or by a signal to the supervisor itself.
RUN_STOPPING = "run.stopping"
#: The head process — not the supervisor — ended, with its exit code or its signal.
RUN_EXITED = "run.exited"

EVENT_KINDS = (
    RUN_STARTED,
    INPUT_ACCEPTED,
    TURN_STARTED,
    PROVIDER_PROGRESSED,
    TURN_FINISHED,
    DRAIN_REQUESTED,
    RUN_STOPPING,
    RUN_EXITED,
)

_RESERVED_FIELDS = frozenset({"schema_version", "seq", "run_id", "kind", "at"})


class JournalError(RuntimeError):
    """A journal that would say something untrue about a run."""


@dataclass(frozen=True)
class JournalReadResult:
    """Everything a reader can honestly say about a journal file it just read.

    `truncated_tail` is the `SIGKILL` case: the last line has no newline, so the supervisor died
    mid-write. It is reported rather than raised, because the records before it are intact.
    `malformed` counts complete lines that were not usable records at all — a different failure
    from a torn tail, and one a reader should not be able to confuse with it.
    """

    events: tuple[dict[str, Any], ...] = ()
    truncated_tail: bool = False
    malformed: int = 0
    ordered: bool = True

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(str(event.get("kind") or "") for event in self.events)

    def of_kind(self, kind: str) -> tuple[dict[str, Any], ...]:
        return tuple(event for event in self.events if event.get("kind") == kind)


@dataclass
class JournalWriter:
    """The supervisor's own end of the journal. One process writes; anybody may read."""

    path: Path
    run_id: str
    _fd: int = field(default=-1, init=False, repr=False)
    _seq: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise JournalError("a journal names the run it belongs to")
        self.path = Path(self.path)

    def open(self) -> JournalWriter:
        """Open for append, continuing the sequence already in the file."""
        self._seq = _last_seq(self.path)
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        # Close-on-exec: a head that inherited this descriptor could keep the journal open after
        # the supervisor that owns it is gone.
        os.set_inheritable(self._fd, False)
        return self

    def fileno(self) -> int:
        """The append descriptor, for a caller that has to reason about inheritance."""
        return self._fd

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    @property
    def seq(self) -> int:
        """The sequence number of the last record this writer appended."""
        return self._seq

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Append one record and return exactly what was written.

        An unknown kind raises here rather than reaching the file: the set of things this substrate
        can say about a head is closed, and a journal with an invented kind in it is a journal no
        reader can route on.
        """
        if kind not in EVENT_KINDS:
            known = ", ".join(EVENT_KINDS)
            raise JournalError(f"unknown journal event kind {kind!r} (known: {known})")
        collisions = _RESERVED_FIELDS.intersection(fields)
        if collisions:
            raise JournalError(
                f"a journal record's own fields cannot be overwritten: {', '.join(sorted(collisions))}"
            )
        if self._fd < 0:
            raise JournalError("the journal is not open")
        self._seq += 1
        record: dict[str, Any] = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "seq": self._seq,
            "run_id": self.run_id,
            "kind": kind,
            "at": round(time.time(), 6),
        }
        record.update(fields)
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        os.write(self._fd, line.encode("utf-8"))
        os.fsync(self._fd)
        return record

    def __enter__(self) -> JournalWriter:
        return self.open()

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _last_seq(path: Path) -> int:
    try:
        result = read_events(path)
    except OSError:
        return 0
    return max((int(event.get("seq") or 0) for event in result.events), default=0)


def _usable(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    if record.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        return None
    if record.get("kind") not in EVENT_KINDS:
        return None
    if not str(record.get("run_id") or ""):
        return None
    try:
        seq = int(record["seq"])
    except (KeyError, TypeError, ValueError):
        return None
    if seq <= 0:
        return None
    record["seq"] = seq
    return record


def read_events(path: str | os.PathLike[str]) -> JournalReadResult:
    """Read a journal a live or dead supervisor wrote, without trusting its last line.

    A missing file reads as an empty journal, because "the supervisor has not written yet" and "the
    supervisor wrote nothing" are the same fact to a reader that has just been pointed at a run.
    """
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return JournalReadResult()
    if not raw:
        return JournalReadResult()
    truncated = not raw.endswith(b"\n")
    lines = raw.split(b"\n")
    if truncated:
        lines = lines[:-1]
    events: list[dict[str, Any]] = []
    malformed = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            malformed += 1
            continue
        record = _usable(parsed)
        if record is None:
            malformed += 1
            continue
        events.append(record)
    ordered = all(
        int(later["seq"]) > int(earlier["seq"])
        for earlier, later in zip(events, events[1:], strict=False)
    )
    return JournalReadResult(
        events=tuple(events), truncated_tail=truncated, malformed=malformed, ordered=ordered
    )


def events_since(events: Sequence[Mapping[str, Any]], seq: int) -> tuple[dict[str, Any], ...]:
    """The records a reader holding `seq` has not seen yet."""
    return tuple(dict(event) for event in events if int(event.get("seq") or 0) > seq)
