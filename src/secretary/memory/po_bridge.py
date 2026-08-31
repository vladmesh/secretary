"""Per-client stdio bridge from an ordinary operator session to Memory HTTP MCP.

Claude and Codex start this process from their user-level MCP configuration.  The
bridge, rather than either provider process, owns the interactive PO ``HeadRun``:
its PID is the liveness boundary and its opaque bearer never appears in client
configuration or the model context.  Pipeline roles never start this command;
their launchers continue to issue card- and sprint-bound grants directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import TextContent

from secretary.memory import access
from triggered_agents.runtime.head import HeadRun, HeadSpec, TaskRef, new_run_id
from triggered_agents.runtime.head.identity import publish_heartbeat

DEFAULT_MEMORY_URL = "http://127.0.0.1:8077/mcp"
MEMORY_URL_ENV = "SECRETARY_MEMORY_URL"


class BridgeError(RuntimeError):
    """The operator bridge cannot establish or use its runtime identity."""


@dataclass(frozen=True)
class BridgeBinding:
    run: HeadRun
    grant: access.MemoryAccessGrant
    grant_path: Path
    pid_path: Path

    def close(self) -> None:
        for path in (self.grant_path, self.pid_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def bind_operator(data_dir: str | Path | None = None) -> BridgeBinding:
    """Issue the PO grant whose lifetime is this stdio process."""
    run_id = new_run_id()
    bindings = access.bindings_dir(data_dir)
    pid_path = bindings / "po-bridges" / f"{run_id}.pid"
    run = HeadRun(
        run_id=run_id,
        spec=HeadSpec(profile_id="memory-po-bridge", adapter="mcp-stdio"),
        workspace=os.getcwd(),
        task_ref=TaskRef.standing("interactive"),
        role="po",
        pid_file=str(pid_path),
    )
    grant = access.issue_grant(run, access.interactive_po_subject(), data_dir=data_dir)
    binding = BridgeBinding(run, grant, bindings / f"{grant.grant_id}.json", pid_path)
    try:
        publish_heartbeat(
            str(pid_path),
            {"run_id": run_id, "role": "po", "task": "standing:interactive"},
        )
    except Exception:
        binding.close()
        raise
    return binding


async def _call(binding: BridgeBinding, url: str, name: str, arguments: dict[str, Any]) -> Any:
    headers = {"Authorization": f"Bearer {binding.grant.token}"}
    try:
        async with (
            streamablehttp_client(url, headers=headers) as (read, write, _session_id),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)
    # The SDK deliberately exposes transport failures as an ExceptionGroup whose
    # leaves vary by HTTP backend; none of those details belong on the MCP wire.
    except Exception as exc:  # noqa: BLE001
        raise BridgeError(f"Memory MCP request failed: {type(exc).__name__}") from None
    if result.isError:
        raise BridgeError("Memory MCP tool call failed")
    structured = result.structuredContent
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    values: list[Any] = []
    for block in result.content:
        if not isinstance(block, TextContent):
            continue
        try:
            values.append(json.loads(block.text))
        except json.JSONDecodeError:
            values.append(block.text)
    if len(values) == 1:
        return values[0]
    return values


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    binding = bind_operator()
    try:
        yield {
            "binding": binding,
            "url": os.environ.get(MEMORY_URL_ENV, DEFAULT_MEMORY_URL),
        }
    finally:
        binding.close()


mcp = FastMCP("secretary-memory-po", lifespan=_lifespan)


def _state(ctx: Context) -> tuple[BridgeBinding, str]:
    state = ctx.request_context.lifespan_context
    if not isinstance(state, dict) or not isinstance(state.get("binding"), BridgeBinding):
        raise BridgeError("operator bridge identity is unavailable")
    return state["binding"], str(state["url"])


@mcp.tool()
async def memory_search(ctx: Context, query: str, k: int = 5, scope: str = "", caller: str = "") -> Any:
    """Search installation memory as the interactive PO; scope can only narrow access."""
    del caller
    binding, url = _state(ctx)
    return await _call(binding, url, "memory_search", {"query": query, "k": k, "scope": scope})


@mcp.tool()
async def memory_get(ctx: Context, id: int) -> Any:
    """Fetch one memory entry through the interactive PO identity."""
    binding, url = _state(ctx)
    return await _call(binding, url, "memory_get", {"id": id})


@mcp.tool()
async def memory_list(ctx: Context, limit: int = 50) -> Any:
    """List recent memory entries through the interactive PO identity."""
    binding, url = _state(ctx)
    return await _call(binding, url, "memory_list", {"limit": limit})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run the interactive PO Memory MCP bridge")
    parser.add_argument("--data-dir", help="installation data directory")
    parser.add_argument("--url", default=DEFAULT_MEMORY_URL, help="Memory HTTP MCP endpoint")
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ[access.MEMORY_ACCESS_BINDINGS_ENV] = str(access.bindings_dir(args.data_dir))
    os.environ[MEMORY_URL_ENV] = args.url
    try:
        mcp.run(transport="stdio")
    except (BridgeError, OSError, ValueError, access.MemoryAccessError) as exc:
        print(f"secretary memory PO bridge: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
