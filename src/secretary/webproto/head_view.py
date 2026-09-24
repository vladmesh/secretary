"""A card's heads, and a read-only view of one local-pty head: its terminal's tail and its journal.

The web half of the sprint's "diagnostics without Orca" item (secretary-1703). An operator who
wants to know what a card's worker or reviewer is doing -- or what it did before it ended -- reads
it here from what the local-pty backend keeps, and never from a pane: the supervisor's own output
buffer while the head runs, the `output.tail` it leaves in the run directory when it lets go, and
the head's journal. Every one of those is read through `secretary.runtime.local_pty_head`, the one
module that reaches the substrate.

Read-only, in the sense `head-status` is: the one question a supervisor is asked is `output`, and
nothing here delivers, attaches, drains, stops or writes. There is no input and no control.

**Which runs are the card's.** The ones the card itself recorded, and nothing else: the worker and
reviewer runs the dispatcher record holds for it now, and every `launch_id` its own history's
`routing` and `attempt.usage` events name. A run id asked for that is not among them is not found,
so a path is only ever built from a run id the card recorded -- never from what a request carried.
A local-pty run directory whose journal says it belongs to another card is not read either.

**What is shown is untrusted.** A head's terminal output is whatever the head printed, so it is
turned into plain text here -- escape sequences removed, carriage returns and backspaces applied --
and passed through `secretary.runtime.redact` before any page or document carries it. The page
escapes it again. The journal is shown through the same key whitelist `head-status` uses: its first
record carries the head's command, and the command carries the head's memory token.

**No source can fail the read.** Each is read under `_source`, the pattern of
`secretary.dispatch.head_status._source`: whatever a dead supervisor, an unreadable file or a
damaged journal raises is that source not answering, said as such, and never an error page.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.runtime.head import HeadRun, HeadRunError, TaskRefError
from secretary.runtime.head_runtime_backends import head_runtime_name
from secretary.runtime.head_runtimes import LOCAL_PTY_RUNTIME, ORCA_LEGACY_RUNTIME
from secretary.runtime.local_pty_head import (
    HEAD_OUTPUT_TAIL_BYTES,
    head_run_directory,
    head_run_first_record,
    head_run_journal_tail,
    head_run_live_output,
    head_run_output_tail,
    head_run_supervisor_lease,
)
from secretary.runtime.redact import scrub_secrets

#: What a row of a card's heads, or a head view, says about a head on the legacy runtime.
LEGACY_NOTICE = "no local-pty transcript (legacy runtime)"
#: What a finished head's view says when its supervisor kept no output tail.
NOT_KEPT_NOTICE = "no transcript was kept for this run"
#: Stated on every view, so a reader never has to wonder whether it can act on the head.
READ_ONLY_NOTICE = (
    "read-only: this view asks the head's supervisor for its output and nothing else; nothing is "
    "typed into the head, and nothing here drains or stops it"
)

#: The journal keys a view shows, and the only ones: `head-status`'s whitelist, plus `at`.
JOURNAL_KEYS = ("seq", "kind", "at", "turn", "reason", "bytes", "subject")
#: How many of the journal's last records a view shows.
JOURNAL_TAIL_RECORDS = 40

#: A head's state, as its supervisor lock says it: a held lock is a supervisor that owns the run.
RUNNING = "running"
FINISHED = "finished"
UNKNOWN = "unknown"

#: The history events that name a head run, and where in them the run id is.
_ROUTING = "routing"
_USAGE = "attempt.usage"
#: A recorded run id has this shape or it is not used at all, not even to look for a directory.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_ROLES = (("worker", "worker"), ("review", "reviewer"))


@dataclass(frozen=True)
class RecordedHead:
    """One head run the card recorded, and what the recording says about it."""

    run_id: str
    role: str
    head: str = ""
    #: The runtime the durable run names, or `""` when only the card's history names the run.
    runtime: str = ""
    #: Whether the dispatcher record still holds this run's head identity for the card.
    current: bool = False


def recorded_heads(record: Any, history: Iterable[dict[str, Any]] | None) -> list[RecordedHead]:
    """The card's head runs, oldest first: its history's, then the dispatcher's current ones.

    `record` is the card's `DispatcherRecord` or `None`; `history` is the card's committed records
    as `kind` and `data`. A run both name is one row, carrying the runtime the durable run says.
    """
    found: dict[str, RecordedHead] = {}
    for event in history or ():
        data = event.get("data") if isinstance(event, dict) else None
        if not isinstance(data, dict):
            continue
        kind = event.get("kind")
        heads = data.get("heads") if kind == _ROUTING else [data] if kind == _USAGE else []
        for head in heads if isinstance(heads, list) else []:
            if not isinstance(head, dict):
                continue
            run_id = _run_id(head.get("launch_id"))
            if run_id and run_id not in found:
                found[run_id] = RecordedHead(
                    run_id=run_id, role=_text(head.get("role")), head=_text(head.get("head"))
                )
    if record is not None:
        for kind, role in _ROLES:
            raw = record.review_head_run if kind == "review" else record.worker_head_run
            run_id = _run_id(raw.get("run_id") if isinstance(raw, dict) else None)
            if not run_id:
                continue
            profile = (record.review_head if kind == "review" else record.head) or ""
            earlier = found.pop(run_id, None)
            found[run_id] = RecordedHead(
                run_id=run_id,
                role=role,
                head=profile or (earlier.head if earlier else ""),
                runtime=_runtime_of(raw),
                current=record.owns_head(kind),
            )
    return list(found.values())


def head_rows(ref: str, heads: list[RecordedHead], root: Path) -> list[dict[str, Any]]:
    """One row per recorded head for the card page: role, run id, state, and whether it has a view."""
    return [_row(ref, head, root) for head in heads]


def head_view(
    ref: str, run_id: str, heads: list[RecordedHead], root: Path, *, observed_at: str
) -> dict[str, Any] | None:
    """The read-only view of one of the card's heads, or `None` when the card recorded no such run.

    `run_id` is compared with the card's recorded runs and used for nothing else: the run directory
    is built from the recorded value it matched.
    """
    head = next((head for head in heads if head.run_id == run_id), None)
    if head is None:
        return None
    row = _row(ref, head, root)
    document: dict[str, Any] = {
        "schema_version": 1,
        "kind": "head_view",
        "observed_at": observed_at,
        "ref": ref,
        "run_id": head.run_id,
        "head": row,
        "read_only": READ_ONLY_NOTICE,
    }
    if not row["local_pty"]:
        document["transcript"] = _not_applicable(row["reason"])
        document["journal"] = {**_not_applicable(row["reason"]), "tail": []}
        return document
    run_dir = head_run_directory(root, head.run_id)
    document["transcript"] = _transcript(run_dir, state=row["state"])
    document["journal"] = _source("journal", lambda: _journal(run_dir), tail=[])
    return document


# -- one head --------------------------------------------------------------------------------


def _row(ref: str, head: RecordedHead, root: Path) -> dict[str, Any]:
    """What the card page says about one recorded head, decided from the run directory it names."""
    row: dict[str, Any] = {
        "ref": ref,
        "role": head.role or "head",
        "run_id": head.run_id,
        "head": head.head or None,
        "current": head.current,
        "runtime": head.runtime or None,
        "local_pty": False,
        "state": UNKNOWN,
        "reason": "",
    }
    if head.runtime and head.runtime != LOCAL_PTY_RUNTIME:
        return {**row, "reason": LEGACY_NOTICE}
    identity = _source("run directory", lambda: _identity(root, head.run_id), found=False)
    if not identity["answered"]:
        return {**row, "runtime": LOCAL_PTY_RUNTIME, "local_pty": True, "reason": identity["reason"]}
    if not identity["found"]:
        if head.runtime == LOCAL_PTY_RUNTIME:
            return {
                **row,
                "local_pty": True,
                "reason": "the run directory holds no journal yet, so this head has said nothing",
            }
        # Nothing but the card's history names this run, and no local-pty supervisor ever held it:
        # every local-pty run leaves its journal under the heads root and nothing sweeps it.
        return {**row, "runtime": ORCA_LEGACY_RUNTIME, "reason": LEGACY_NOTICE}
    if identity.get("task") != f"card:{ref}":
        return {
            **row,
            "runtime": LOCAL_PTY_RUNTIME,
            "reason": "the run directory under this run id belongs to another card, so it is not read",
        }
    row.update(runtime=LOCAL_PTY_RUNTIME, local_pty=True)
    lease = _source("supervisor lock", lambda: _lease(root, head.run_id), state=UNKNOWN)
    row.update(state=lease["state"], reason=lease["reason"])
    return row


def _identity(root: Path, run_id: str) -> dict[str, Any]:
    first = head_run_first_record(head_run_directory(root, run_id))
    if first is None:
        return {"answered": True, "found": False}
    return {"answered": True, "found": True, "task": _text(first.get("task"))}


def _lease(root: Path, run_id: str) -> dict[str, Any]:
    lease = head_run_supervisor_lease(head_run_directory(root, run_id))
    if not lease.lock_readable or not lease.table_readable:
        return {
            "answered": False,
            "state": UNKNOWN,
            "reason": f"the supervisor lock could not be read ({lease.error or 'unreadable'})",
        }
    if lease.holders:
        return {"answered": True, "state": RUNNING, "reason": "a supervisor holds this run"}
    return {"answered": True, "state": FINISHED, "reason": "no supervisor holds this run any more"}


def _transcript(run_dir: Path, *, state: str) -> dict[str, Any]:
    """The end of the head's terminal output, from the one source the head's state names.

    Decided by the supervisor lock and nothing else, never by trying one source and then another:
    a held lock is a running head, and only its supervisor's live answer is its transcript -- the
    run directory's tail, if any, belongs to no incarnation that is running. A free lock is a run
    that ended, and only the tail its supervisor kept is its transcript. An unreadable lock says
    nothing about which one this is, so the transcript is not answering.
    """
    if state == RUNNING:
        return _source(
            "supervisor output", lambda: _output(head_run_live_output(run_dir), "supervisor")
        )
    if state == FINISHED:
        return _source("output tail", lambda: _kept(run_dir))
    return {
        "answered": False,
        "state": "unavailable",
        "source": None,
        "reason": "the supervisor lock could not be read, so whether this head is running is not known",
        "text": "",
    }


def _kept(run_dir: Path) -> dict[str, Any]:
    tail = head_run_output_tail(run_dir)
    if tail is None:
        return {
            "answered": True,
            "state": "not_kept",
            "source": None,
            "reason": NOT_KEPT_NOTICE,
            "text": "",
            "bytes": 0,
            "total_bytes": None,
            "truncated": False,
        }
    return _output(tail, "output.tail")


def _output(tail: Any, source: str) -> dict[str, Any]:
    return {
        "answered": True,
        "state": "live" if source == "supervisor" else "kept",
        "source": source,
        "reason": "",
        "text": scrub_secrets(terminal_text(tail.data)),
        "bytes": len(tail.data),
        "total_bytes": tail.total_bytes,
        "truncated": bool(tail.truncated),
        "limit_bytes": HEAD_OUTPUT_TAIL_BYTES,
    }


def _journal(run_dir: Path) -> dict[str, Any]:
    """The last records of the head's journal, each through `journal_record`, and what was left out."""
    read = head_run_journal_tail(run_dir)
    tail = []
    dropped = 0
    for event in read.events[-JOURNAL_TAIL_RECORDS:]:
        record, lost = journal_record(event)
        tail.append(record)
        dropped += lost
    damage = []
    if read.malformed:
        damage.append(f"{read.malformed} malformed line(s) skipped")
    if read.truncated_tail:
        damage.append("the final line is torn")
    if not read.ordered:
        damage.append("records are out of sequence")
    if dropped:
        damage.append(f"{dropped} field value(s) out of shape were left out")
    return {
        "answered": True,
        "state": "degraded" if damage else "available",
        "reason": ("the journal tail is incomplete: " + ", ".join(damage)) if damage else "",
        "partial": read.partial_head,
        "tail": tail,
    }


#: The latest time `datetime.fromtimestamp(..., UTC)` renders: the start of the year 10000.
_LAST_TIME = 253402300800.0
#: The largest count a journal integer field may carry and still be shown: 2**53, the last integer a
#: JSON reader in a browser holds exactly.
_LAST_COUNT = 2**53
_COUNTS = ("seq", "turn", "bytes")


def journal_record(event: Any) -> tuple[dict[str, Any], int]:
    """One journal record as the view shows it, and how many of its whitelisted values were dropped.

    The one normaliser every record the view returns goes through, so the page and the JSON route
    only ever hold values of the shape they expect: `at` a float the page can turn into a date
    (0 < at < year 10000), `seq`, `turn` and `bytes` ints in 0..2**53, and `kind`, `reason` and
    `subject` strings, scrubbed and bounded. A value of any other shape is left out and counted,
    never passed on and never raised about.
    """
    record: dict[str, Any] = {}
    lost = 0
    source = event if isinstance(event, dict) else {}
    for key in JOURNAL_KEYS:
        if key not in source:
            continue
        value = source[key]
        if key == "at":
            kept: Any = _time(value)
        elif key in _COUNTS:
            kept = value if _count(value) else None
        else:
            kept = scrub_secrets(value)[:400] if isinstance(value, str) else None
        if kept is None:
            lost += value is not None
            continue
        record[key] = kept
    return record, lost


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _LAST_COUNT


def _not_applicable(reason: str) -> dict[str, Any]:
    return {"answered": False, "state": "not_applicable", "reason": reason, "source": None, "text": ""}


def _source(name: str, read: Callable[[], dict[str, Any]], **empty: Any) -> dict[str, Any]:
    """One source's answer, or the uniform shape of a source that did not answer; never raises.

    `secretary.dispatch.head_status._source`, for the same reason: a supervisor, a lock file, an
    output tail and a journal are all outside this process's control, so anything their read raises
    is that source not answering and never a failed page.
    """
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 - every head source is untrusted input
        return {
            **empty,
            "answered": False,
            "state": "unavailable",
            "reason": f"the {name} could not be read ({type(exc).__name__}: {scrub_secrets(str(exc))[:200]})",
        }


# -- terminal output as plain text ------------------------------------------------------------

#: OSC, DCS, SOS, PM and APC: a string introduced by ESC and ended by BEL or ST, or by the end.
_ESC_STRING = re.compile(r"\x1b[\]PX^_][^\x07\x1b]*(?:\x07|\x1b\\|\Z)")
#: CSI: parameters, intermediates, one final byte. Also its one-byte C1 spelling.
_CSI = re.compile(r"(?:\x1b\[|\x9b)([0-?]*)[ -/]*([@-~])")
#: A CSI the tail cut off before its final byte.
_CSI_CUT = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*\Z")
#: Every other escape: charset designations and the rest (`ESC ( B`, `ESC 7`, `ESC =`).
_ESC_OTHER = re.compile(r"\x1b[ -/]*[0-~]?")
#: What is left of the C0 and C1 controls once tabs, newlines, CR and BS have been interpreted.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
#: A redrawing terminal leaves stacks of blank lines; two in a row say the same thing.
_BLANK_RUN = re.compile(r"\n{3,}")


def terminal_text(data: bytes) -> str:
    """A terminal's bytes as the plain text a person would read off them, with the standard library.

    Escape sequences are removed rather than interpreted, with two exceptions that keep words
    apart: a cursor-forward (`CSI n C`) becomes that many spaces, and a cursor placement onto
    another line (`CSI H`, `f`, `d`, `E`, `B`) becomes a line break. Within a line, a carriage
    return goes back to its start and what follows overwrites it; a backspace steps back one.
    """
    text = data.decode("utf-8", errors="replace")
    text = _ESC_STRING.sub("", text)
    text = _CSI_CUT.sub("", _CSI.sub(_csi, text))
    text = _ESC_OTHER.sub("", text)
    lines = [_overstrike(line) for line in text.replace("\r\n", "\n").split("\n")]
    return _BLANK_RUN.sub("\n\n", "\n".join(_CONTROL.sub("", line).rstrip() for line in lines)).strip("\n")


def _csi(match: re.Match[str]) -> str:
    parameters, final = match.group(1), match.group(2)
    if final == "C":
        count = parameters.split(";")[0]
        return " " * min(int(count) if count.isdigit() else 1, 400)
    if final in "HfdEB":
        return "\n"
    return ""


def _overstrike(line: str) -> str:
    """One line as it ends up on the screen after its carriage returns and backspaces."""
    if "\r" not in line and "\b" not in line:
        return line
    cells: list[str] = []
    column = 0
    for char in line:
        if char == "\r":
            column = 0
        elif char == "\b":
            column = max(0, column - 1)
        else:
            if column < len(cells):
                cells[column] = char
            else:
                cells.append(char)
            column += 1
    return "".join(cells)


# -- small readers ----------------------------------------------------------------------------


def _runtime_of(raw: Any) -> str:
    """The runtime a durable run names, or `""` when the record holds no readable run."""
    if not isinstance(raw, dict) or not raw.get("run_id"):
        return ""
    try:
        return head_runtime_name(HeadRun.from_json(raw))
    except (HeadRunError, TaskRefError, TypeError, ValueError):
        return ""


def _run_id(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return text if _RUN_ID.fullmatch(text) else ""


def _time(value: Any) -> float | None:
    """A time the page can render: a float with 0 < at < year 10000, or `None` for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except (OverflowError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and 0 < seconds < _LAST_TIME else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
