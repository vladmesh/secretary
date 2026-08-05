"""Kanboard JSON-RPC transport — thin, stdlib-only, local configuration.

App-level access is HTTP Basic against the endpoint in the installation's
``board-transport.env`` (`.../jsonrpc.php`).

`call(method, **params)` returns the JSON-RPC `result` or raises KanboardError on a
transport failure or an RPC-level error. `call_batch` sends several calls in one request
for read paths that would otherwise be one round trip per task. Higher-level board
operations live in agents/pipeline/ops.py.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .board_transport import BoardTransportError, resolve_for_environ

_BATCH_CHUNK = 200


class KanboardError(RuntimeError):
    """A JSON-RPC call failed at the transport or protocol level."""


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
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise KanboardError(f"{label}: transport error: {e}") from e
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
        chunk = calls[start:start + _BATCH_CHUNK]
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
