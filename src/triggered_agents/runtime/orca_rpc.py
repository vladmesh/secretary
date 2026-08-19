"""Minimal client for Orca's runtime RPC over its unix socket.

Some runtime capabilities aren't exposed by the `orca` CLI — notably `session.tabs.close`,
which prunes a tab from the persisted workspace session (`tabsByWorktree`). That store is what
survives a pty's death as a ghost tab, and `terminal list/stop/close` never touch it. The GUI's
tab-× uses this RPC; so do we, to reap ghosts.

Frame format mirrors src/cli/runtime/transport.ts: one newline-terminated JSON request
{id, authToken, method, params} to the unix endpoint from ~/.config/orca/orca-runtime.json;
the reply is newline-delimited JSON, possibly interleaved with {"_keepalive":true} frames.
"""
from __future__ import annotations

import codecs
import json
import os
import socket
import uuid
from pathlib import Path

_META_PATH = Path(os.environ.get("ORCA_RUNTIME_META", str(Path.home() / ".config/orca/orca-runtime.json")))


def _endpoint_and_token() -> tuple[str, str | None]:
    meta = json.loads(_META_PATH.read_text())
    endpoint = next(t["endpoint"] for t in meta["transports"] if t["kind"] == "unix")
    return endpoint, meta.get("authToken")


def call(method: str, params=None, timeout: float = 20.0) -> dict:
    """Send one RPC and return the response envelope ({id, ok, result|error, _meta})."""
    endpoint, token = _endpoint_and_token()
    rid = str(uuid.uuid4())
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(endpoint)
        s.sendall((json.dumps({"id": rid, "authToken": token, "method": method, "params": params}) + "\n").encode())
        decoder = codecs.getincrementaldecoder("utf-8")()
        buf = ""
        while True:
            raw = s.recv(65536)
            if not raw:
                raise RuntimeError(f"orca rpc {method}: connection closed before reply")
            chunk = decoder.decode(raw)
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if not line.strip():
                    continue
                frame = json.loads(line)
                if frame.get("_keepalive"):
                    continue
                if frame.get("id") == rid:
                    return frame
    finally:
        s.close()
