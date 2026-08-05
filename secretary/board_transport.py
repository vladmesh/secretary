"""Non-secret, local Kanboard JSON-RPC transport configuration.

Kanboard uses HTTP Basic authentication, so an application token still has to
match the container and its clients.  It is deliberately not a recovery secret:
this file is ordinary installation configuration and its fresh-install default
is deterministic.  A pre-transport installation can import its complete legacy
``runtime.env`` tuple once; disagreement with an existing file is an operator
action, never a silent repair.
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from typing import Mapping

from secretary._fsutil import write_text_atomic
from triggered_agents.runtime.board_transport import (
    BoardTransport, BoardTransportError, DEFAULT_TOKEN, DEFAULT_URL, DEFAULT_USER,
    TRANSPORT_ENV, parse as _parse, resolve, transport_path,
)

TRANSPORT_FILE = "board-transport.env"
LEGACY_ENV = TRANSPORT_ENV
# This authenticates only the local, host-owned Kanboard service. It is an
# explicit transport setting, not entropy that recovery must preserve.
DEFAULT_TOKEN = "secretary-local-kanboard-jsonrpc-v1"


def default_transport() -> BoardTransport:
    return BoardTransport(DEFAULT_URL, DEFAULT_USER, DEFAULT_TOKEN)


def legacy_transport(values: Mapping[str, str] | None) -> BoardTransport | None:
    """Turn a complete old runtime tuple into a transport, rejecting partial state."""
    if not values:
        return None
    present = [name for name in LEGACY_ENV if values.get(name)]
    if not present:
        return None
    if len(present) != len(LEGACY_ENV):
        raise BoardTransportError("legacy Kanboard runtime configuration is incomplete: " + ", ".join(
            name for name in LEGACY_ENV if name not in present
        ))
    return BoardTransport(*(str(values[name]) for name in LEGACY_ENV))


def ensure(instance_dir: Path | str, *, legacy_values: Mapping[str, str] | None = None,
           dry_run: bool = False) -> tuple[BoardTransport, str]:
    """Create a deterministic file, or perform the one explicit legacy import."""
    path = transport_path(instance_dir)
    legacy = legacy_transport(legacy_values)
    if path.exists():
        configured = _parse(path)
        if legacy is not None and configured != legacy:
            raise BoardTransportError(
                "board transport mismatch: board-transport.env and legacy runtime.env disagree; "
                "reconcile the container and configuration explicitly"
            )
        if not dry_run:
            _ensure_transport_ignored(path.parent)
        return configured, "unchanged"
    configured = legacy or default_transport()
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, "".join(f"{key}={value}\n" for key, value in configured.as_environ().items()))
        path.chmod(0o600)
        _ensure_transport_ignored(path.parent)
    return configured, "imported-legacy" if legacy is not None else "created-default"


def _ensure_transport_ignored(instance_dir: Path) -> None:
    """Make the local transport exclusion durable in the instance repository."""
    if not (instance_dir / ".git").exists():
        return
    ignore = instance_dir / ".gitignore"
    current = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    entry = f"/{TRANSPORT_FILE}"
    if entry not in current.splitlines():
        write_text_atomic(ignore, current + ("" if not current or current.endswith("\n") else "\n") + entry + "\n")
        completed = subprocess.run(
            ["git", "-C", str(instance_dir), "add", ".gitignore"], capture_output=True, text=True
        )
        if completed.returncode == 0:
            subprocess.run(
                ["git", "-C", str(instance_dir), "commit", "-m", "Ignore local board transport"],
                capture_output=True, text=True,
            )
    checked = subprocess.run(
        ["git", "-C", str(instance_dir), "check-ignore", "--quiet", "--", TRANSPORT_FILE],
        capture_output=True, text=True,
    )
    if checked.returncode != 0:
        raise BoardTransportError("board-transport.env is not ignored by this repo")


def ensure_from_runtime_file(instance_dir: Path | str, runtime_env: Path | str | None = None,
                             *, dry_run: bool = False) -> tuple[BoardTransport, str]:
    """Migration entry point for install/upgrade; only this module reads old names."""
    path = Path(runtime_env) if runtime_env is not None else Path(instance_dir) / "runtime.env"
    values: dict[str, str] = {}
    if path.exists():
        _validate_runtime_file(path)
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="strict").splitlines(), 1):
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                key = key.strip()
                if key in LEGACY_ENV:
                    if key in values:
                        raise BoardTransportError(
                            f"legacy Kanboard runtime configuration is ambiguous: {key} appears more than once "
                            f"(line {number})"
                        )
                    values[key] = value
    transport, status = ensure(instance_dir, legacy_values=values, dry_run=dry_run)
    if values:
        # After a successful, equality-checked import the legacy tuple has no
        # authority. Preserve unrelated runtime lines and comments verbatim.
        raw = path.read_text(encoding="utf-8")
        retained = [line for line in raw.splitlines(keepends=True)
                    if line.split("=", 1)[0].strip() not in LEGACY_ENV]
        if not dry_run:
            write_text_atomic(path, "".join(retained))
        status = "imported-legacy" if status != "unchanged" else "retired-legacy"
    return transport, status


def _validate_runtime_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise BoardTransportError(f"runtime.env is unreadable: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or mode & 0o077:
        raise BoardTransportError("runtime.env must be a regular 0600 file")


__all__ = [
    "BoardTransport", "BoardTransportError", "DEFAULT_TOKEN", "LEGACY_ENV", "ensure",
    "ensure_from_runtime_file", "resolve", "transport_path",
]
