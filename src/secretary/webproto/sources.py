"""One shape for "did this source answer, and if not, why, and how old is what we have".

Every snapshot this layer returns is assembled from several independent sources -- the
installation health collector, the project registry, the board, the dispatcher's own durable
state, the event journal -- and they fail apart. A dashboard that gets one object with a missing
key cannot tell "there are no running agents" from "the file that would say so could not be read",
and that difference is the whole reason an operator opens a dashboard.

So every section of every snapshot carries one of these, always, with the same four fields:

``state``            ``available`` or ``unavailable``; never absent, never inferred from emptiness
``reason``           why an unavailable source could not answer, in plain words; null when it did
``observed_at``      when the value in the snapshot was true
``data_age_seconds`` how old that value is, in seconds, or null when nothing dated it

For an available source, ``observed_at`` is the moment of the read and the age is 0. For one that
refused, both describe the newest evidence still on disk behind it, so "unavailable" comes with
"and what I am showing you instead is 40 minutes old" rather than with silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AVAILABLE = "available"
UNAVAILABLE = "unavailable"


def isoformat(moment: float) -> str:
    """The journal's own UTC spelling, so timestamps compare as strings across snapshots."""
    return datetime.fromtimestamp(moment, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Source:
    """The availability of one source of one snapshot section."""

    state: str
    reason: str | None = None
    observed_at: float | None = None
    data_age_seconds: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "observed_at": None if self.observed_at is None else isoformat(self.observed_at),
            "data_age_seconds": self.data_age_seconds,
        }


def available(now: float) -> Source:
    return Source(AVAILABLE, None, now, 0.0)


def unavailable(reason: str, *, now: float, evidence: Path | None = None) -> Source:
    """A refusal, dated by the newest evidence still readable behind it.

    ``evidence`` is the file the section would have been built from. Its modification time is the
    only honest answer to "how old is what you are showing me" when the reader itself failed, and a
    file that is not there at all dates nothing, so both fields stay null.
    """
    stamped: float | None = None
    if evidence is not None:
        try:
            stamped = evidence.stat().st_mtime
        except OSError:
            stamped = None
    age = None if stamped is None else max(0.0, round(now - stamped, 3))
    return Source(UNAVAILABLE, reason[:400], stamped, age)
