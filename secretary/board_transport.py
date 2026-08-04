"""Non-secret, local Kanboard JSON-RPC transport configuration.

Kanboard uses HTTP Basic authentication, so an application token still has to
match the container and its clients.  It is deliberately not a recovery secret:
this file is ordinary installation configuration and its fresh-install default
is deterministic.  A pre-transport installation can import its complete legacy
``runtime.env`` tuple once; disagreement with an existing file is an operator
action, never a silent repair.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from secretary._fsutil import write_text_atomic
from triggered_agents.runtime.paths import default_instance_path

TRANSPORT_FILE = "board-transport.env"
LEGACY_ENV = ("KANBOARD_URL", "KANBOARD_API_USER", "KANBOARD_API_TOKEN")
DEFAULT_URL = "http://127.0.0.1:8080/jsonrpc.php"
DEFAULT_USER = "jsonrpc"
# This authenticates only the local, host-owned Kanboard service. It is an
# explicit transport setting, not entropy that recovery must preserve.
DEFAULT_TOKEN = "secretary-local-kanboard-jsonrpc-v1"


class BoardTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoardTransport:
    url: str
    user: str
    token: str

    def as_environ(self) -> dict[str, str]:
        return dict(zip(LEGACY_ENV, (self.url, self.user, self.token)))

    def authorization_header(self) -> str:
        encoded = base64.b64encode(f"{self.user}:{self.token}".encode("utf-8")).decode("ascii")
        return f"Basic {encoded}"


def transport_path(instance_dir: Path | str | None = None, *, environ: Mapping[str, str] | None = None) -> Path:
    if instance_dir is not None:
        return Path(instance_dir).expanduser() / TRANSPORT_FILE
    env = os.environ if environ is None else environ
    return Path(env.get("SECRETARY_INSTANCE") or default_instance_path()) / TRANSPORT_FILE


def _parse(path: Path) -> BoardTransport:
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BoardTransportError(f"board transport configuration is unreadable: {path}") from exc
    fields: dict[str, str] = {}
    for number, line in enumerate(raw, 1):
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise BoardTransportError(f"board transport configuration line {number} must use KEY=VALUE")
        key, value = line.split("=", 1)
        if key not in LEGACY_ENV or key in fields or not value:
            raise BoardTransportError(f"board transport configuration line {number} is invalid")
        fields[key] = value
    missing = [name for name in LEGACY_ENV if not fields.get(name)]
    if missing:
        raise BoardTransportError("board transport configuration is missing " + ", ".join(missing))
    return BoardTransport(fields["KANBOARD_URL"], fields["KANBOARD_API_USER"], fields["KANBOARD_API_TOKEN"])


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


def resolve(instance_dir: Path | str | None = None, *, environ: Mapping[str, str] | None = None) -> BoardTransport:
    """Read the one authoritative local transport file.

    ``environ`` accepts a complete tuple only as an explicit in-process adapter.
    A normal caller never falls back to ambient legacy variables: after migration
    the file is the sole authority, rather than one of two competing sources.
    """
    path = transport_path(instance_dir, environ=environ)
    if path.is_file():
        return _parse(path)
    if environ is not None:
        direct = legacy_transport(environ)
        if direct is not None:
            return direct
    raise BoardTransportError(f"board transport configuration is missing: {path}")


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
            _ignore_local_file(path.parent)
        return configured, "unchanged"
    configured = legacy or default_transport()
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, "".join(f"{key}={value}\n" for key, value in configured.as_environ().items()))
        path.chmod(0o644)
        _ignore_local_file(path.parent)
    return configured, "imported-legacy" if legacy is not None else "created-default"


def _ignore_local_file(instance_dir: Path) -> None:
    """Keep local transport config out of an instance checkpoint without touching it."""
    exclude = instance_dir / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return
    current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    entry = f"/{TRANSPORT_FILE}"
    if entry not in current.splitlines():
        write_text_atomic(exclude, current + ("" if not current or current.endswith("\n") else "\n") + entry + "\n")


def ensure_from_runtime_file(instance_dir: Path | str, runtime_env: Path | str | None = None,
                             *, dry_run: bool = False) -> tuple[BoardTransport, str]:
    """Migration entry point for install/upgrade; only this module reads old names."""
    path = Path(runtime_env) if runtime_env is not None else Path(instance_dir) / "runtime.env"
    values: dict[str, str] = {}
    if path.is_file():
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="strict").splitlines(), 1):
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key in LEGACY_ENV:
                    if key in values:
                        raise BoardTransportError(
                            f"legacy Kanboard runtime configuration is ambiguous: {key} appears more than once "
                            f"(line {number})"
                        )
                    values[key] = value
    transport, status = ensure(instance_dir, legacy_values=values, dry_run=dry_run)
    if values and not dry_run:
        # After a successful, equality-checked import the legacy tuple has no
        # authority. Preserve unrelated runtime lines and comments verbatim.
        raw = path.read_text(encoding="utf-8")
        retained = [line for line in raw.splitlines(keepends=True)
                    if line.split("=", 1)[0].strip() not in LEGACY_ENV]
        write_text_atomic(path, "".join(retained))
        status = "imported-legacy" if status != "unchanged" else "retired-legacy"
    return transport, status


__all__ = ["BoardTransport", "BoardTransportError", "DEFAULT_TOKEN", "LEGACY_ENV", "ensure", "ensure_from_runtime_file", "resolve", "transport_path"]
