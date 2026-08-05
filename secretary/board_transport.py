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
from pathlib import Path
from typing import Mapping

from secretary._fsutil import write_text_atomic
from secretary import state_repo
from triggered_agents.runtime.board_transport import (
    BoardTransport, BoardTransportError, DEFAULT_TOKEN, DEFAULT_TRANSPORT,
    TRANSPORT_ENV, TRANSPORT_FILE, parse as _parse, resolve, transport_path,
)

def legacy_transport(values: Mapping[str, str] | None) -> BoardTransport | None:
    """Turn a complete old runtime tuple into a transport, rejecting partial state."""
    if not values:
        return None
    present = [name for name in TRANSPORT_ENV if values.get(name)]
    if not present:
        return None
    if len(present) != len(TRANSPORT_ENV):
        raise BoardTransportError("legacy Kanboard runtime configuration is incomplete: " + ", ".join(
            name for name in TRANSPORT_ENV if name not in present
        ))
    return BoardTransport(*(str(values[name]) for name in TRANSPORT_ENV))


def ensure(instance_dir: Path | str, *, legacy_values: Mapping[str, str] | None = None,
           dry_run: bool = False, allow_default: bool = False) -> tuple[BoardTransport, str]:
    """Create a deterministic file, or perform the one explicit legacy import."""
    path = transport_path(instance_dir)
    legacy = legacy_transport(legacy_values)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        mode = None
    except OSError as exc:
        raise BoardTransportError(f"board transport configuration is unreadable: {path}") from exc
    if mode is not None:
        configured = _parse(path, require_private=False)
        if legacy is not None and configured != legacy:
            raise BoardTransportError(
                "board transport mismatch: board-transport.env and legacy runtime.env disagree; "
                "reconcile the container and configuration explicitly"
            )
        needs_mode_repair = bool(mode & 0o077)
        try:
            ignore_changed = state_repo.ensure_ignored(
                path.parent, f"/{TRANSPORT_FILE}", dry_run=dry_run,
            ) if (path.parent / ".git").exists() else False
        except state_repo.StateRepoError as exc:
            raise BoardTransportError("board-transport.env is not ignored by this repo") from exc
        if needs_mode_repair and not dry_run:
            path.chmod(0o600)
        if needs_mode_repair:
            return configured, "would-secure" if dry_run else "secured"
        if ignore_changed:
            return configured, "would-ignore" if dry_run else "ignored"
        return configured, "unchanged"
    if legacy is None and not allow_default:
        raise BoardTransportError(
            "board transport is missing and legacy runtime.env has no complete Kanboard tuple; "
            "refuse to guess or rotate the live transport"
        )
    configured = legacy or DEFAULT_TRANSPORT
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, "".join(f"{key}={value}\n" for key, value in configured.as_environ().items()))
        path.chmod(0o600)
        if (path.parent / ".git").exists():
            try:
                state_repo.ensure_ignored(path.parent, f"/{TRANSPORT_FILE}")
            except state_repo.StateRepoError as exc:
                raise BoardTransportError("board-transport.env is not ignored by this repo") from exc
    status = "imported-legacy" if legacy is not None else "created-default"
    return configured, ("would-" + status if dry_run else status)


def ensure_from_runtime_file(instance_dir: Path | str, runtime_env: Path | str | None = None,
                             *, dry_run: bool = False, allow_default: bool = False) -> tuple[BoardTransport, str]:
    """Migration entry point for install/upgrade; only this module reads old names."""
    path = Path(runtime_env) if runtime_env is not None else Path(instance_dir) / "runtime.env"
    values: dict[str, str] = {}
    if path.exists():
        _validate_runtime_file(path)
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="strict").splitlines(), 1):
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                key = key.strip()
                if key in TRANSPORT_ENV:
                    if key in values:
                        raise BoardTransportError(
                            f"legacy Kanboard runtime configuration is ambiguous: {key} appears more than once "
                            f"(line {number})"
                        )
                    values[key] = value
    transport, status = ensure(
        instance_dir, legacy_values=values, dry_run=dry_run, allow_default=allow_default,
    )
    if values:
        # After a successful, equality-checked import the legacy tuple has no
        # authority. Preserve unrelated runtime lines and comments verbatim.
        raw = path.read_text(encoding="utf-8")
        retained = [line for line in raw.splitlines(keepends=True)
                    if line.split("=", 1)[0].strip() not in TRANSPORT_ENV]
        if not dry_run:
            write_text_atomic(path, "".join(retained))
        status = "would-retire-legacy" if dry_run and status == "unchanged" else (
            "retired-legacy" if status == "unchanged" else status
        )
    return transport, status


def _validate_runtime_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise BoardTransportError(f"runtime.env is unreadable: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or mode & 0o077:
        raise BoardTransportError("runtime.env must be a regular 0600 file")


__all__ = [
    "BoardTransport", "BoardTransportError", "DEFAULT_TOKEN", "DEFAULT_TRANSPORT", "ensure",
    "ensure_from_runtime_file", "resolve", "transport_path",
]
