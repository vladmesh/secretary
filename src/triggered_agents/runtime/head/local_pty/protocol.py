"""The wire between a supervisor and whoever addresses it, and the four limits that bound it.

One request per line, one response per line, JSON both ways, payloads base64 so that a head's
bytes are never assumed to be text. A line is a frame; a frame is bounded; every refusal names the
limit it hit **and the size that hit it**.

The limits are values with reasons, not round numbers:

  * `INPUT_MAX_BYTES` is 64 KiB. The number that matters is the one below it: the legacy path caps
    a delivery at 256 bytes, a continuation nudge did not fit, and the head silently received a
    truncated prompt (`issue:d9d049eaad39d02bbb1e`). A continuation payload — a card pointer, a
    reviewer finding, a rendered instruction — is kilobytes, so the limit is set two orders of
    magnitude above the largest one seen rather than at the edge of it. Over the limit is a named
    refusal: never a truncation, never a silent split into two writes;
  * `FRAME_MAX_BYTES` bounds the request line itself, so a client cannot make the supervisor
    allocate without bound before it has parsed anything. It is above `INPUT_MAX_BYTES` by the
    slack base64 and the JSON envelope cost, so an input inside the input limit always fits in a
    frame and the two limits can never disagree about the same payload;
  * `OUTPUT_BUFFER_BYTES` is what the supervisor keeps of the head's output. A reader gets the
    freshest tail plus the count of what was dropped before it, so a bounded view is always
    labelled as one;
  * `ATTACH_MAX_CLIENTS` bounds concurrent attachments. Each attachment costs a socket and an
    outbound buffer, and an unbounded count of them is an unbounded amount of the supervisor's
    memory held by whoever dials it.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

#: The largest payload one `input` request may carry, and the reason it is not 256 bytes.
INPUT_MAX_BYTES = 64 * 1024
#: Envelope slack over `INPUT_MAX_BYTES`: base64 costs 4 bytes per 3, plus the JSON keys.
_ENVELOPE_SLACK_BYTES = 4096
#: The largest single request line the supervisor will read before refusing the connection.
FRAME_MAX_BYTES = (INPUT_MAX_BYTES * 4 + 2) // 3 + _ENVELOPE_SLACK_BYTES
#: How much of the head's output the supervisor keeps for a reader that was not attached.
OUTPUT_BUFFER_BYTES = 256 * 1024
#: How many callers may hold the head's live stream at once.
ATTACH_MAX_CLIENTS = 4

#: File names inside one run directory. A caller that knows the run directory knows all of them.
SOCKET_NAME = "head.sock"
JOURNAL_NAME = "journal.jsonl"
PID_FILE_NAME = "head.pid"
SUPERVISOR_PID_NAME = "supervisor.pid"
SUPERVISOR_LOCK_NAME = "supervisor.lock"
SUPERVISOR_LOG_NAME = "supervisor.log"
STARTUP_ERROR_NAME = "startup.error"

#: Request verbs.
OP_STATUS = "status"
OP_INPUT = "input"
OP_OUTPUT = "output"
OP_ATTACH = "attach"
OP_RESIZE = "resize"
OP_DRAIN = "drain"
OP_STOP = "stop"
OPS = (OP_STATUS, OP_INPUT, OP_OUTPUT, OP_ATTACH, OP_RESIZE, OP_DRAIN, OP_STOP)

#: Refusal tokens. Callers route on these, so they are tokens rather than sentences.
ERROR_INPUT_TOO_LARGE = "input_too_large"
ERROR_FRAME_TOO_LARGE = "frame_too_large"
ERROR_ATTACH_LIMIT = "attach_limit"
ERROR_DRAINING = "draining"
ERROR_HEAD_GONE = "head_gone"
ERROR_MALFORMED = "malformed_request"
ERROR_UNKNOWN_OP = "unknown_op"

#: Pushed frames an attached client receives, as distinct from responses to its own requests.
EVENT_OUTPUT = "output"
EVENT_DROPPED = "dropped"
EVENT_EXITED = "exited"

#: A Unix socket path is bounded by the kernel's `sun_path`, and the failure when it is too long is
#: an opaque one. It is checked where the path is built instead.
SUN_PATH_MAX = 100


class ProtocolError(RuntimeError):
    """A frame that cannot be spoken or understood."""


def run_dir_for(root: str | os.PathLike[str], run_id: str) -> Path:
    """The one directory that holds everything about one run."""
    if not run_id or "/" in run_id or run_id in (".", ".."):
        raise ProtocolError(f"a run directory is named by a run id, not by {run_id!r}")
    return Path(root) / run_id


def socket_path_for(run_dir: str | os.PathLike[str]) -> Path:
    """The predictable socket path for a run directory, checked against the kernel's limit."""
    path = Path(run_dir) / SOCKET_NAME
    if len(str(path).encode("utf-8")) > SUN_PATH_MAX:
        raise ProtocolError(
            f"the head socket path is {len(str(path))} bytes, over the {SUN_PATH_MAX}-byte "
            f"limit a Unix socket address has: {path}"
        )
    return path


def encode_frame(payload: dict[str, Any]) -> bytes:
    """One JSON object, one line."""
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def decode_frame(line: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"unreadable frame: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError("a frame is a JSON object")
    return parsed


def encode_payload(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_payload(data: Any) -> bytes:
    if not isinstance(data, str):
        raise ProtocolError("a payload is base64 text")
    try:
        return base64.b64decode(data.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ProtocolError(f"a payload is base64 text: {exc}") from exc


def input_refusal(size: int) -> dict[str, Any]:
    """The refusal an oversized input gets: the limit, the actual size, and no truncation."""
    return {
        "ok": False,
        "error": ERROR_INPUT_TOO_LARGE,
        "limit_bytes": INPUT_MAX_BYTES,
        "size_bytes": size,
        "detail": (
            f"input of {size} bytes exceeds the {INPUT_MAX_BYTES}-byte limit; "
            "it was neither truncated nor split"
        ),
    }


#: How much accepted-but-not-yet-written input the supervisor will hold for a head whose pty is
#: not draining. Four inputs deep: enough that a normal delivery never waits on the one before it,
#: small enough that a head which has stopped reading is refused rather than buffered forever.
INPUT_BACKLOG_MAX_BYTES = 4 * INPUT_MAX_BYTES
ERROR_INPUT_BACKLOG = "input_backlog"


def backlog_refusal(size: int, pending: int) -> dict[str, Any]:
    """The refusal an input gets when the head is not reading: named limit, named actual."""
    return {
        "ok": False,
        "error": ERROR_INPUT_BACKLOG,
        "limit_bytes": INPUT_BACKLOG_MAX_BYTES,
        "size_bytes": size,
        "pending_bytes": pending,
        "detail": (
            f"the head has {pending} bytes of unwritten input; accepting {size} more would exceed "
            f"the {INPUT_BACKLOG_MAX_BYTES}-byte backlog limit"
        ),
    }
