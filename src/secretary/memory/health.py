"""A bounded, launch-authenticated read probe for the Memory MCP service."""

from __future__ import annotations

import http.client
import json
import os
import pwd
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


def _rows(value: Any) -> list[dict[str, Any]] | None:
    """Normalize the pinned MCP list-tool result shapes to rows."""
    if isinstance(value, list):
        return value if all(isinstance(row, dict) for row in value) else None
    if isinstance(value, dict):
        wrapped = value.get("result")
        if isinstance(wrapped, list):
            return _rows(wrapped)
        return [value]
    return None


def _tool_value(result: dict[str, Any]) -> list[dict[str, Any]]:
    payload = result.get("result")
    if not isinstance(payload, dict) or payload.get("isError"):
        raise MemoryProbeError("MCP did not return an allowed read")
    structured = payload.get("structuredContent")
    if structured is not None:
        rows = _rows(structured)
        if rows is not None:
            return rows
    content = payload.get("content")
    if not isinstance(content, list):
        raise MemoryProbeError("MCP returned no read result")
    rows: list[dict[str, Any]] = []
    parsed_row_block = False
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            parsed = _rows(json.loads(str(item.get("text") or "")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if parsed is not None:
            parsed_row_block = True
            rows.extend(parsed)
    if parsed_row_block:
        return rows
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
    denial = next((row for row in value if row.get("status") == "denied"), None)
    if isinstance(denial, dict):
        code = str(denial.get("error") or "unknown")
        raise MemoryProbeError(f"Memory MCP denied the authenticated probe: {code}")
    if not value:
        raise MemoryProbeError("Memory MCP returned no expected authorized entry")
    return value


def _probe_read(
    run: HeadRun,
    token: str,
    *,
    port: int,
    timeout_seconds: float,
    retry_seconds: float,
) -> None:
    """Publish the caller-owned heartbeat, then perform one bounded MCP read."""
    publish_heartbeat(
        run.pid_file,
        {"run_id": run.run_id, "role": run.role, "task": "standing:steward"},
    )
    deadline = time.monotonic() + timeout_seconds
    last_error: MemoryProbeError | None = None
    while time.monotonic() < deadline:
        try:
            rows = _authenticated_list(token, port=port, timeout_seconds=min(5, timeout_seconds))
            if any(row.get("scope") in {"project:secretary", "product:secretary"} for row in rows):
                return
            raise MemoryProbeError("Memory MCP returned no expected steward-scoped entry")
        except MemoryProbeError as exc:
            # A service may still be binding or rebuilding its derived index after
            # systemd restarts it. A response from Memory that denies this identity,
            # however, is decisive: retrying a denial turns a typed authorization
            # failure into an opaque timeout and does not make a stale identity live.
            if str(exc) not in {
                "Memory MCP is unavailable",
                "Memory MCP returned no expected steward-scoped entry",
            }:
                raise
            last_error = exc
            if time.monotonic() + retry_seconds >= deadline:
                break
            time.sleep(retry_seconds)
    raise last_error or MemoryProbeError("Memory MCP probe timed out")


def _drop_to_runtime_user(runtime_user: str) -> None:
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise MemoryProbeError(f"runtime user {runtime_user!r} does not exist") from None
    try:
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    except OSError as exc:
        raise MemoryProbeError(f"could not enter runtime user {runtime_user!r}: {exc}") from None


def _run_from_runtime_user(runtime_user: str | None, callback: Callable[[], None]) -> None:
    """Run the heartbeat and read as the account that owns the Memory daemon.

    Root upgrade is permitted to create and hand off the short-lived grant, but it
    must not lend its process identity to an unprivileged server.  The forked
    helper publishes its own heartbeat after dropping privileges, so the ordinary
    server-side reader can inspect the exact process that made the MCP request.
    """
    if not runtime_user or os.geteuid() != 0:
        callback()
        return
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            _drop_to_runtime_user(runtime_user)
            callback()
            result = {"ok": True}
        except Exception as exc:
            # This pipe crosses only the result boundary.  It never carries the
            # bearer, grant body, request payload, or a Memory fact.
            result = {"ok": False, "error": str(exc)}
        try:
            os.write(write_fd, json.dumps(result).encode("utf-8"))
        finally:
            os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        raw = os.read(read_fd, 16_384)
    finally:
        os.close(read_fd)
        _, status = os.waitpid(child, 0)
    try:
        result = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        result = None
    if status != 0 or not isinstance(result, dict) or not result.get("ok"):
        detail = str(result.get("error") if isinstance(result, dict) else "runtime-user helper failed")
        raise MemoryProbeError(f"runtime-user authenticated probe failed: {detail}")


def probe_memory(
    data_dir: Path,
    *,
    port: int = 8077,
    timeout_seconds: float = 30,
    retry_seconds: float = 1,
    runtime_handoff: Callable[[Path], None] | None = None,
    runtime_user: str | None = None,
) -> None:
    """Prove one real MCP read with a temporary steward launch identity.

    The process making the request owns the heartbeat for its short lifetime.
    During a root upgrade that process is a narrowly dropped-privilege child of
    the configured runtime user, not root.  The service therefore verifies both
    the opaque bearer and the existing HeadRun reader; no caller, requested
    scope, ambient PO authority, or health bypass exists.
    """
    run_id = new_run_id()
    bindings = access.bindings_dir(data_dir)
    pid_file = bindings / "health-probes" / f"{run_id}.pid"
    run = HeadRun(
        run_id=run_id,
        spec=HeadSpec(profile_id="memory-health", adapter="probe"),
        workspace="",
        task_ref=TaskRef.standing("steward"),
        role="steward",
        pid_file=str(pid_file),
    )
    grant = access.issue_grant(run, access.standing_subject("steward"), data_dir=data_dir, ttl_seconds=120)
    grant_path = bindings / f"{grant.grant_id}.json"
    try:
        if runtime_handoff is not None:
            # The grant directory itself can be root-created during recovery.
            # Handoff the whole tree before the daemon resolves it, not merely
            # the leaf files a root process happened to write.
            runtime_handoff(bindings)
        _run_from_runtime_user(
            runtime_user,
            lambda: _probe_read(
                run,
                grant.token,
                port=port,
                timeout_seconds=timeout_seconds,
                retry_seconds=retry_seconds,
            ),
        )
    finally:
        for path in (grant_path, pid_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
