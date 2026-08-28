"""A bounded, launch-authenticated read probe for the Memory MCP service."""

from __future__ import annotations

import http.client
import json
import os
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

from triggered_agents.runtime.head import HeadRun, HeadSpec, TaskRef, new_run_id
from triggered_agents.runtime.head.identity import publish_heartbeat

from . import access


class MemoryProbeError(RuntimeError):
    """The restarted MCP service did not complete an authorized read."""


def _response_json(response: http.client.HTTPResponse) -> tuple[dict[str, Any], str | None]:
    body = response.read().decode("utf-8", errors="replace")
    if response.status not in {200, 202}:
        raise MemoryProbeError(f"MCP returned HTTP {response.status}")
    if not body.strip():
        return {}, response.getheader("Mcp-Session-Id")
    # Streamable HTTP may answer a JSON-RPC response directly or frame it as SSE.
    if response.getheader("Content-Type", "").split(";", 1)[0] == "text/event-stream":
        data = "\n".join(line[5:].strip() for line in body.splitlines() if line.startswith("data:"))
    else:
        data = body
    try:
        payload = json.loads(data)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MemoryProbeError("MCP returned an unreadable response") from exc
    if not isinstance(payload, dict):
        raise MemoryProbeError("MCP returned a malformed response")
    if "error" in payload:
        raise MemoryProbeError("MCP rejected the authenticated probe")
    return payload, response.getheader("Mcp-Session-Id")


def _request(
    connection: http.client.HTTPConnection,
    token: str,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-11-25",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    connection.request("POST", "/mcp", body=json.dumps(payload), headers=headers)
    return _response_json(connection.getresponse())


def _tool_value(result: dict[str, Any]) -> Any:
    payload = result.get("result")
    if not isinstance(payload, dict) or payload.get("isError"):
        raise MemoryProbeError("MCP did not return an allowed read")
    structured = payload.get("structuredContent")
    if isinstance(structured, list):
        return structured
    content = payload.get("content")
    if not isinstance(content, list):
        raise MemoryProbeError("MCP returned no read result")
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            return json.loads(str(item.get("text") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    raise MemoryProbeError("MCP returned an unreadable read result")


def _authenticated_list(token: str, *, port: int, timeout_seconds: float) -> list[dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_seconds)
    try:
        initialized, session_id = _request(
            connection,
            token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "secretary-memory-health", "version": "1"},
                },
            },
        )
        if not isinstance(initialized.get("result"), dict) or not session_id:
            raise MemoryProbeError("MCP did not establish an authenticated session")
        _request(
            connection,
            token,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
        )
        result, _ = _request(
            connection,
            token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "memory_list", "arguments": {"limit": 1}},
            },
            session_id=session_id,
        )
    except (OSError, http.client.HTTPException) as exc:
        raise MemoryProbeError("Memory MCP is unavailable") from exc
    finally:
        connection.close()
    value = _tool_value(result)
    if isinstance(value, dict) and value.get("status") == "denied":
        code = str(value.get("error") or "unknown")
        raise MemoryProbeError(f"Memory MCP denied the authenticated probe: {code}")
    if not isinstance(value, list) or not value or not isinstance(value[0], dict):
        raise MemoryProbeError("Memory MCP returned no expected authorized entry")
    return value


def probe_memory(
    data_dir: Path,
    *,
    port: int = 8077,
    timeout_seconds: float = 30,
    retry_seconds: float = 1,
    runtime_handoff: Callable[[Path], None] | None = None,
) -> None:
    """Prove one real MCP read with a temporary steward launch identity.

    The process itself owns the heartbeat for its short lifetime.  The service
    therefore verifies both the opaque bearer and the existing HeadRun reader;
    no caller, requested scope, ambient PO authority, or health bypass exists.
    """
    run_id = new_run_id()
    pid_file = access.bindings_dir(data_dir) / "health-probes" / f"{run_id}.pid"
    run = HeadRun(
        run_id=run_id,
        spec=HeadSpec(profile_id="memory-health", adapter="probe"),
        workspace="",
        task_ref=TaskRef.standing("steward"),
        role="steward",
        pid_file=str(pid_file),
    )
    grant = access.issue_grant(run, access.standing_subject("steward"), data_dir=data_dir, ttl_seconds=120)
    grant_path = access.bindings_dir(data_dir) / f"{grant.grant_id}.json"
    try:
        if runtime_handoff is not None:
            runtime_handoff(grant_path)
        publish_heartbeat(
            run.pid_file,
            {"run_id": run.run_id, "role": run.role, "task": "standing:steward"},
        )
        if runtime_handoff is not None:
            runtime_handoff(pid_file)
        deadline = time.monotonic() + timeout_seconds
        last_error: MemoryProbeError | None = None
        while time.monotonic() < deadline:
            try:
                rows = _authenticated_list(grant.token, port=port, timeout_seconds=min(5, timeout_seconds))
                if any(row.get("scope") in {"project:secretary", "product:secretary"} for row in rows):
                    return
                raise MemoryProbeError("Memory MCP returned no expected steward-scoped entry")
            except MemoryProbeError as exc:
                last_error = exc
                if time.monotonic() + retry_seconds >= deadline:
                    break
                time.sleep(retry_seconds)
        raise last_error or MemoryProbeError("Memory MCP probe timed out")
    finally:
        for path in (grant_path, pid_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
