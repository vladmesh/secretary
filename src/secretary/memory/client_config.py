"""Materialize the operator/automation split for Memory MCP clients.

User sessions start the installation-owned stdio PO bridge.  Dispatcher-launched
roles override that entry point at launch and connect to the HTTP daemon with a
runtime-issued bearer.  These writers own only the named Secretary MCP entries;
all unrelated user configuration is preserved.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary._fsutil import write_text_atomic

MEMORY_URL = "http://127.0.0.1:8077/mcp"
LEGACY_SERVER = "memory"
PO_SERVER = "po_memory"
TOKEN_ENV = "SECRETARY_MEMORY_ACCESS_TOKEN"
_SECTION = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")


class ClientConfigError(RuntimeError):
    """A user client configuration cannot be reconciled safely."""


@dataclass(frozen=True)
class ClientConfigResult:
    codex_managed: bool = False
    codex_user: bool = False
    claude_user: bool = False

    @property
    def changed(self) -> int:
        return sum((self.codex_managed, self.codex_user, self.claude_user))


def bridge_executable(product_root: Path) -> Path:
    return product_root / ".venv" / "bin" / "secretary-memory-po-bridge"


def _bridge_env(data_dir: Path) -> dict[str, str]:
    return {
        "MEMORY_ACCESS_BINDINGS": str(data_dir / "memory" / "access-grants"),
        "SECRETARY_MEMORY_URL": MEMORY_URL,
    }


def _codex_section(command: Path, data_dir: Path) -> str:
    env = _bridge_env(data_dir)
    return "\n".join(
        (
            f"[mcp_servers.{PO_SERVER}]",
            f"command = {json.dumps(str(command))}",
            "args = []",
            "env = { "
            + ", ".join(f"{key} = {json.dumps(value)}" for key, value in sorted(env.items()))
            + " }",
            "",
        )
    )


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ClientConfigError(f"{label} config is a symlink: {path}")


def _section_ranges(lines: list[str]) -> list[tuple[str, int, int]]:
    starts: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        match = _SECTION.match(line)
        if match:
            starts.append((match.group(1), index))
    return [
        (name, start, starts[index + 1][1] if index + 1 < len(starts) else len(lines))
        for index, (name, start) in enumerate(starts)
    ]


def _owned_codex_sections(payload: dict[str, Any]) -> set[str]:
    servers = payload.get("mcp_servers")
    if not isinstance(servers, dict):
        return set()
    owned = {PO_SERVER} if PO_SERVER in servers else set()
    legacy = servers.get(LEGACY_SERVER)
    if isinstance(legacy, dict) and legacy.get("url") == MEMORY_URL:
        owned.add(LEGACY_SERVER)
    return owned


def reconcile_codex(path: Path, command: Path, data_dir: Path, *, dry_run: bool = False) -> bool:
    """Replace only Secretary's legacy/direct entry with the operator bridge."""
    _reject_symlink(path, "Codex")
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        payload = tomllib.loads(current) if current.strip() else {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ClientConfigError(f"cannot read Codex config {path}: {exc}") from None
    if not isinstance(payload, dict):
        raise ClientConfigError(f"Codex config {path} is not a table")
    owned = _owned_codex_sections(payload)
    lines = current.splitlines(keepends=True)
    remove: set[int] = set()
    for name, start, end in _section_ranges(lines):
        if any(
            name == f"mcp_servers.{server}" or name.startswith(f"mcp_servers.{server}.") for server in owned
        ):
            remove.update(range(start, end))
    retained = "".join(line for index, line in enumerate(lines) if index not in remove).rstrip()
    desired = (retained + ("\n\n" if retained else "") + _codex_section(command, data_dir)).rstrip() + "\n"
    if desired == current:
        return False
    if dry_run:
        return True
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_text_atomic(path, desired)
    except (OSError, RuntimeError) as exc:
        raise ClientConfigError(f"cannot update Codex config {path}: {exc}") from None
    return True


def _claude_bridge(command: Path, data_dir: Path) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": str(command),
        "args": [],
        "env": _bridge_env(data_dir),
    }


def reconcile_claude(path: Path, command: Path, data_dir: Path, *, dry_run: bool = False) -> bool:
    """Publish one user-scoped bridge entry without replacing other Claude state."""
    _reject_symlink(path, "Claude")
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClientConfigError(f"cannot read Claude config {path}: {exc}") from None
    if not isinstance(payload, dict):
        raise ClientConfigError(f"Claude config {path} is not an object")
    servers = payload.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ClientConfigError(f"Claude config {path} has non-object mcpServers")
    changed = False
    legacy = servers.get(LEGACY_SERVER)
    if isinstance(legacy, dict) and legacy.get("url") == MEMORY_URL:
        del servers[LEGACY_SERVER]
        changed = True
    desired = _claude_bridge(command, data_dir)
    if servers.get(PO_SERVER) == desired and not changed:
        return False
    servers[PO_SERVER] = desired
    if dry_run:
        return True
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except (OSError, RuntimeError) as exc:
        raise ClientConfigError(f"cannot update Claude config {path}: {exc}") from None
    return True


def reconcile_clients(
    product_root: Path, runtime_home: Path, data_dir: Path, *, dry_run: bool = False
) -> ClientConfigResult:
    command = bridge_executable(product_root)
    if not dry_run and not command.is_file():
        raise ClientConfigError(f"PO bridge executable is missing: {command}")
    managed = runtime_home / ".config" / "orca" / "codex-runtime-home" / "home" / "config.toml"
    user_codex = runtime_home / ".codex" / "config.toml"
    claude = runtime_home / ".claude.json"
    return ClientConfigResult(
        codex_managed=reconcile_codex(managed, command, data_dir, dry_run=dry_run),
        codex_user=reconcile_codex(user_codex, command, data_dir, dry_run=dry_run),
        claude_user=reconcile_claude(claude, command, data_dir, dry_run=dry_run),
    )
