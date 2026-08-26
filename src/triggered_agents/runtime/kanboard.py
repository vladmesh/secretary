"""Kanboard JSON-RPC transport — thin, stdlib-only, local configuration.

App-level access is HTTP Basic against the endpoint in the installation's
``board-transport.env`` (`.../jsonrpc.php`).

`call(method, **params)` returns the JSON-RPC `result` or raises KanboardError on a
transport failure or an RPC-level error. A refused connection is retried for a bounded
window and then raised as KanboardUnreachable, the one failure a caller may treat as
"not yet" rather than "broken". `call_batch` sends several calls in one request
for read paths that would otherwise be one round trip per task. Higher-level board
operations live in agents/pipeline/ops.py.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .board_transport import BoardTransportError, resolve_for_environ

_BATCH_CHUNK = 200

# How long a call keeps retrying a refused connection before it gives up (seconds), and how long it
# waits between attempts. The board is docker-hosted and the agent units are timer-driven with
# `Persistent=true`, so the first board call of the day routinely lands in the seconds right after a
# reboot, when the port is not listening yet (secretary-964: two prechecks died at 07:30:26 and
# 07:30:32 against a board that answered fine at 07:31:34). A refused connection means nothing was
# read or written, so retrying it is safe for every method, read or write.
_CONNECT_RETRY_WINDOW_S = float(os.environ.get("TA_BOARD_CONNECT_RETRY_S", "45"))
_CONNECT_RETRY_SLEEP_S = float(os.environ.get("TA_BOARD_CONNECT_RETRY_SLEEP_S", "3"))


class KanboardError(RuntimeError):
    """A JSON-RPC call failed at the transport or protocol level."""


class KanboardUnreachable(KanboardError):
    """The board never answered: the connection was refused, so nothing was sent.

    Kept apart from a plain KanboardError because the two deserve different outcomes upstream. An
    RPC error, a malformed response or a bad configuration means the caller is broken and has to be
    looked at; a refused connection usually means the board is not listening *yet*, which a precheck
    reports as a retryable outcome rather than as its own failure (runtime/state.py
    PRECHECK_BOARD_UNREACHABLE).
    """


def _connection_refused(exc: urllib.error.URLError) -> bool:
    """Was the TCP connection refused outright (nothing listening on the port)?

    Deliberately narrow. An HTTPError is an answer from something that *is* listening, and a read
    timeout or a reset may mean the request was already delivered; neither is retried here.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc.reason, ConnectionRefusedError)


def _creds():
    try:
        transport = resolve_for_environ(os.environ)
    except BoardTransportError as exc:
        raise KanboardError(f"board transport configuration is unavailable: {exc}") from exc
    return transport


def _post(payload, label: str):
    transport = _creds()
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        transport.url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": transport.authorization_header()},
        method="POST",
    )
    deadline = time.monotonic() + _CONNECT_RETRY_WINDOW_S
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
            break
        except urllib.error.URLError as e:
            if not _connection_refused(e):
                raise KanboardError(f"{label}: transport error: {e}") from e
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KanboardUnreachable(
                    f"{label}: board unreachable after {_CONNECT_RETRY_WINDOW_S:g}s: {e}"
                ) from e
            time.sleep(min(_CONNECT_RETRY_SLEEP_S, remaining))
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise KanboardError(f"{label}: non-JSON response") from e


def call(method: str, **params):
    """Invoke a Kanboard JSON-RPC method; return its `result` or raise KanboardError."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        payload["params"] = params
    doc = _post(payload, method)
    if "error" in doc:
        raise KanboardError(f"{method}: rpc error: {doc['error']}")
    return doc.get("result")


def call_batch(calls):
    """Run `[(method, params), ...]` in one HTTP request; return results in call order.

    Kanboard has no bulk read for per-task metadata or comments, so anything that wants the
    whole board (the checkpoint export) would otherwise pay one round trip per task. Requests
    go out in chunks so a large board does not turn into one oversized request.
    """
    calls = list(calls)
    results = [None] * len(calls)
    for start in range(0, len(calls), _BATCH_CHUNK):
        chunk = calls[start : start + _BATCH_CHUNK]
        payload = []
        for offset, (method, params) in enumerate(chunk):
            request = {"jsonrpc": "2.0", "id": start + offset, "method": method}
            if params:
                request["params"] = params
            payload.append(request)
        doc = _post(payload, "batch")
        if not isinstance(doc, list):
            raise KanboardError(f"batch: expected {len(chunk)} responses, got a single object")
        by_id = {}
        for item in doc:
            if not isinstance(item, dict):
                raise KanboardError("batch: malformed response entry")
            if "error" in item:
                raise KanboardError(f"batch: rpc error: {item['error']}")
            by_id[item.get("id")] = item.get("result")
        for offset, (method, _) in enumerate(chunk):
            index = start + offset
            if index not in by_id:
                raise KanboardError(f"batch: no response for {method} (id {index})")
            results[index] = by_id[index]
    return results
