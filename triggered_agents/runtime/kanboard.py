"""Kanboard JSON-RPC transport — thin, stdlib-only, credentials from env.

App-level access is HTTP Basic `jsonrpc:$KANBOARD_API_TOKEN` against the endpoint in
`$KANBOARD_URL` (`.../jsonrpc.php`). Launchers provide these through role-scoped runtime env.

`call(method, **params)` returns the JSON-RPC `result` or raises KanboardError on a
transport failure or an RPC-level error. Higher-level board operations live in
agents/pipeline/ops.py.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

_ENV_URL = "KANBOARD_URL"
_ENV_USER = "KANBOARD_API_USER"
_ENV_TOKEN = "KANBOARD_API_TOKEN"


class KanboardError(RuntimeError):
    """A JSON-RPC call failed at the transport or protocol level."""


def _creds() -> tuple[str, str, str]:
    try:
        url = os.environ[_ENV_URL]
        user = os.environ[_ENV_USER]
        token = os.environ[_ENV_TOKEN]
    except KeyError as e:
        raise KanboardError(
            f"missing {e.args[0]} in runtime env (check provisioning/launcher)"
        ) from e
    return url, user, token


def call(method: str, **params):
    """Invoke a Kanboard JSON-RPC method; return its `result` or raise KanboardError."""
    url, user, token = _creds()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        payload["params"] = params
    body = json.dumps(payload).encode("utf-8")
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise KanboardError(f"{method}: transport error: {e}") from e
    except json.JSONDecodeError as e:
        raise KanboardError(f"{method}: non-JSON response") from e
    if "error" in doc:
        raise KanboardError(f"{method}: rpc error: {doc['error']}")
    return doc.get("result")
