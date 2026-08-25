"""The wire between a supervisor and whoever addresses it, and the limits that bound it.

One request per line, one response per line, JSON both ways, payloads base64 so that a head's
bytes are never assumed to be text. A line is a frame; a frame is bounded; every refusal names the
limit it hit **and the size that hit it**.

Two properties hold for every exchange, and they are the reason the wire looks like this:

  * **a request carries an id and its answer carries the same id.** Responses are otherwise
    distinguishable only by their order, so a caller that stopped waiting for one — a timeout, an
    interrupt — would find that frame sitting in the socket as the answer to its *next* question,
    and the connection would be one answer out of step for the rest of its life. With an id the
    caller can see a frame is not its own and discard it explicitly (`SupervisorClient.request`
    does exactly that, and counts what it discarded);
  * **no answer waits for the head.** Every operation is either a question about state the
    supervisor already holds, or an intention it accepts or refuses on the spot. Input is the one
    that could have been otherwise, and it is deliberately not: a delivery is *admitted* and then
    written by the supervisor's loop, so how fast the head reads its terminal changes what
    `status` reports about the delivery, never how long a caller waits for an answer.

The limits are values with reasons, not round numbers:

  * `INPUT_MAX_BYTES` is 64 KiB. The number that matters is the one below it: the legacy path caps
    a delivery at 256 bytes, a continuation nudge did not fit, and the head silently received a
    truncated prompt (`issue:d9d049eaad39d02bbb1e`). A continuation payload — a card pointer, a
    reviewer finding, a rendered instruction — is kilobytes, so the limit is set two orders of
    magnitude above the largest one seen rather than at the edge of it. Over the limit is a named
    refusal: never a truncation, never a silent split into two writes. A declared limit is only a
    limit if the substrate makes it the real one, which is why the supervisor puts the head's pty
    into a non-canonical mode before the head starts: a canonical line discipline caps a line at
    4095 bytes and discards the rest **without telling the writer**, which would reproduce exactly
    the wound this limit exists to answer;
  * `FRAME_MAX_BYTES` bounds the request line itself, so a client cannot make the supervisor
    allocate without bound before it has parsed anything. It is above `INPUT_MAX_BYTES` by the
    slack base64 and the JSON envelope cost, so an input inside the input limit always fits in a
    frame and the two limits can never disagree about the same payload;
  * `OUTPUT_BUFFER_BYTES` is what the supervisor keeps of the head's output. A reader gets the
    freshest tail plus the count of what was dropped before it, so a bounded view is always
    labelled as one;
  * `ATTACH_MAX_CLIENTS` bounds concurrent attachments. Each attachment costs a socket and an
    outbound buffer, and an unbounded count of them is an unbounded amount of the supervisor's
    memory held by whoever dials it;
  * `CONNECTION_MAX_CLIENTS` bounds callers that have merely dialled. The socket is owner-only, so
    this is not a defence against a stranger; it is the bound that keeps a caller which connects
    without ever attaching — a probe that leaks, a client stuck mid-frame — from costing the
    supervisor a descriptor plus an inbox of up to `FRAME_MAX_BYTES` without any limit at all.
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
#: How many callers may hold a connection at once, attached or not: every attachment plus room for
#: the short-lived probes that connect, ask one question and leave.
CONNECTION_MAX_CLIENTS = ATTACH_MAX_CLIENTS * 4

#: File names inside one run directory. A caller that knows the run directory knows all of them.
SOCKET_NAME = "head.sock"
JOURNAL_NAME = "journal.jsonl"
PID_FILE_NAME = "head.pid"
SUPERVISOR_PID_NAME = "supervisor.pid"
SUPERVISOR_LOCK_NAME = "supervisor.lock"
SUPERVISOR_LOG_NAME = "supervisor.log"
#: A refusal on the way up: the supervisor never took the run over, and nothing of it is running.
STARTUP_ERROR_NAME = "startup.error"
#: A failure *after* the run was up, which is a different fact and deserves a different name: a
#: head that worked for an hour and then lost its supervisor was never a startup failure.
SUPERVISOR_ERROR_NAME = "supervisor.error"

#: Request verbs.
OP_STATUS = "status"
OP_INPUT = "input"
OP_OUTPUT = "output"
OP_ATTACH = "attach"
OP_RESIZE = "resize"
OP_DRAIN = "drain"
OP_STOP = "stop"
OPS = (OP_STATUS, OP_INPUT, OP_OUTPUT, OP_ATTACH, OP_RESIZE, OP_DRAIN, OP_STOP)

#: The correlation key. A request may carry one; an answer to a request that carried one repeats it
#: verbatim. Frames that answer nothing in particular — a connection refused before any request, a
#: refusal of bytes too malformed to have an id in them — carry none, and say so by its absence.
REQUEST_ID = "id"

#: What a delivery is doing, as `status` reports it. A delivery is admitted, then written by the
#: supervisor's loop, so its progress is state a reader asks about rather than a wait it endures.
DELIVERY_IN_FLIGHT = "in_flight"
DELIVERY_COMPLETE = "complete"
DELIVERY_STALLED = "stalled"
DELIVERY_FAILED = "failed"
DELIVERY_STATES = (DELIVERY_IN_FLIGHT, DELIVERY_COMPLETE, DELIVERY_STALLED, DELIVERY_FAILED)

#: Refusal tokens. Callers route on these, so they are tokens rather than sentences. A stall is not
#: among them: a delivery that the head does not take is not a refusal of the request that offered
#: it — that request was accepted — but a state the delivery reaches, so it is `DELIVERY_STALLED`
#: in `status` and in the journal rather than an error on a frame nobody is waiting for.
ERROR_INPUT_TOO_LARGE = "input_too_large"
ERROR_FRAME_TOO_LARGE = "frame_too_large"
ERROR_ATTACH_LIMIT = "attach_limit"
ERROR_CONNECTION_LIMIT = "connection_limit"
ERROR_INPUT_IN_FLIGHT = "input_in_flight"
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


#: How long the supervisor's loop keeps an admitted payload in flight before it abandons what is
#: left and records the stall. A pty's own buffer is a few kilobytes, so a payload at the input
#: limit only moves as fast as the head reads it; this bounds how long the supervisor carries a
#: payload for a head that has stopped reading, and it bounds nothing a caller waits for. Raising
#: or lowering it changes when `status` starts saying `stalled`; it cannot change what any request
#: costs, because no request waits on a delivery at all.
INPUT_DELIVERY_SECONDS = 10.0


def in_flight_refusal(delivery: dict[str, Any]) -> dict[str, Any]:
    """The refusal a delivery gets while the previous one is still being written.

    One payload at a time is what makes a delivery describable: two in flight would interleave on
    the terminal, and neither caller could be told what the head actually received. The refusal
    carries the delivery that holds the floor, so the caller learns which one to wait out rather
    than being told only "no".
    """
    return {
        "ok": False,
        "error": ERROR_INPUT_IN_FLIGHT,
        "delivery": delivery,
        "detail": (
            f"delivery {delivery.get('id')} is still being written "
            f"({delivery.get('written_bytes')} of {delivery.get('size_bytes')} bytes); "
            "this head takes one payload at a time"
        ),
    }


def delivery_detail(state: str, size: int, written: int, why: str, seconds: float) -> str:
    """How a finished or unfinished delivery reads in `status` and in the journal.

    A partial delivery is never described as an arrival: the two numbers are always both there, so
    "all of it landed" and "this much of it landed" cannot be confused for one another.
    """
    if state == DELIVERY_COMPLETE:
        return f"all {written} bytes reached the head's terminal"
    if state == DELIVERY_IN_FLIGHT:
        return f"{written} of {size} bytes have reached the head's terminal so far"
    if state == DELIVERY_STALLED:
        return (
            f"{written} of {size} bytes reached the head's terminal within {seconds:g}s and the "
            f"delivery was abandoned there: {why}"
        )
    return f"{written} of {size} bytes reached the head's terminal and the delivery ended: {why}"


def connection_refusal(connections: int) -> dict[str, Any]:
    """The refusal a caller gets when the supervisor is already holding all the callers it will."""
    return {
        "ok": False,
        "error": ERROR_CONNECTION_LIMIT,
        "limit": CONNECTION_MAX_CLIENTS,
        "connections": connections,
        "detail": (
            f"{connections} callers already hold a connection to this head, which is the "
            f"{CONNECTION_MAX_CLIENTS}-connection limit"
        ),
    }
