"""Canonical local board transport primitives shared by product and runtime layers."""

from __future__ import annotations

import base64
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .paths import instance_dir as normalize_instance_dir

TRANSPORT_FILE = "board-transport.env"
TRANSPORT_ENV = ("KANBOARD_URL", "KANBOARD_API_USER", "KANBOARD_API_TOKEN")
DEFAULT_URL = "http://127.0.0.1:8080/jsonrpc.php"
DEFAULT_USER = "jsonrpc"
DEFAULT_TOKEN = "secretary-local-kanboard-jsonrpc-v1"


class BoardTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoardTransport:
    url: str
    user: str
    token: str

    def as_environ(self) -> dict[str, str]:
        return dict(zip(TRANSPORT_ENV, (self.url, self.user, self.token)))

    def authorization_header(self) -> str:
        encoded = base64.b64encode(f"{self.user}:{self.token}".encode()).decode("ascii")
        return f"Basic {encoded}"


DEFAULT_TRANSPORT = BoardTransport(DEFAULT_URL, DEFAULT_USER, DEFAULT_TOKEN)


def transport_path(instance_dir: Path | str) -> Path:
    root = normalize_instance_dir(instance_dir)
    return root / TRANSPORT_FILE


def parse(path: Path, *, require_private: bool = True) -> BoardTransport:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise BoardTransportError(f"board transport configuration is missing: {path}") from None
    except OSError as exc:
        raise BoardTransportError(f"board transport configuration is unreadable: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise BoardTransportError("board transport configuration must be a regular file, not a symlink")
    if require_private and mode & 0o077:
        raise BoardTransportError("board transport configuration permissions are too broad; run chmod 0600")
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
        if key not in TRANSPORT_ENV or key in fields or not value:
            raise BoardTransportError(f"board transport configuration line {number} is invalid")
        fields[key] = value
    missing = [name for name in TRANSPORT_ENV if not fields.get(name)]
    if missing:
        raise BoardTransportError("board transport configuration is missing " + ", ".join(missing))
    return BoardTransport(fields["KANBOARD_URL"], fields["KANBOARD_API_USER"], fields["KANBOARD_API_TOKEN"])


def resolve(instance_dir: Path | str) -> BoardTransport:
    path = transport_path(instance_dir)
    return parse(path)


def resolve_for_environ(environ: Mapping[str, str]) -> BoardTransport:
    """Resolve only the installation a caller explicitly bound itself to."""
    instance = str(environ.get("SECRETARY_INSTANCE") or "").strip()
    if not instance:
        raise BoardTransportError("SECRETARY_INSTANCE must name the installation")
    return resolve(instance)
