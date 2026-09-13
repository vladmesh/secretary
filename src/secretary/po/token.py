"""The token that opens `/po` in the dashboard, and the cookie derived from it.

A PO head has full permissions on this host, so the page that talks to it is a shell. The front's
password guards the whole site; this token is a second, independent key for `/po` alone. It lives in
``DATA_DIR/po-web-token`` — outside the PO workspace the agent can write — mode 0600, owned by the
runtime user. Install and upgrade create it once and never rewrite it; rotating it is deleting the
file and running the step again.

The browser never holds the token itself: the cookie is an HMAC keyed by the token, so replacing the
file invalidates every cookie issued under the old one.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

TOKEN_FILE_NAME = "po-web-token"
TOKEN_MODE = 0o600
# 32 random bytes, URL-safe base64: 256 bits.
TOKEN_BYTES = 32
COOKIE_NAME = "secretary_po"
COOKIE_PATH = "/po"
_COOKIE_CONTEXT = b"secretary po cookie v1"


class TokenError(RuntimeError):
    """The token file cannot be created or read."""


def token_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / TOKEN_FILE_NAME


def ensure_token(data_dir: Path | str, *, dry_run: bool = False) -> bool:
    """Create the token file if nothing is there. True when it was (or would be) created.

    `O_EXCL | O_NOFOLLOW`: an existing file, or a symlink in its place, is never written through.
    """
    path = token_path(data_dir)
    if path.exists() or path.is_symlink():
        return False
    if dry_run:
        return True
    token = secrets.token_urlsafe(TOKEN_BYTES)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, TOKEN_MODE)
    except FileExistsError:
        return False
    except OSError as exc:
        raise TokenError(f"cannot create {path}: {exc}") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, TOKEN_MODE)
    except OSError as exc:
        raise TokenError(f"cannot write {path}: {exc}") from None
    return True


def read_token(data_dir: Path | str) -> str:
    path = token_path(data_dir)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
    except FileNotFoundError:
        raise TokenError(f"there is no PO token at {path}; run `secretary upgrade` to create it") from None
    except (OSError, UnicodeError) as exc:
        raise TokenError(f"cannot read the PO token at {path}: {exc}") from None
    if not token:
        raise TokenError(f"the PO token at {path} is empty")
    return token


def cookie_value(token: str) -> str:
    return hmac.new(token.encode("utf-8"), _COOKIE_CONTEXT, hashlib.sha256).hexdigest()


def token_matches(token: str, presented: str) -> bool:
    return hmac.compare_digest(token.encode("utf-8"), presented.strip().encode("utf-8"))


def cookie_matches(token: str, presented: str) -> bool:
    return hmac.compare_digest(cookie_value(token).encode("utf-8"), presented.encode("utf-8"))


__all__ = [
    "COOKIE_NAME",
    "COOKIE_PATH",
    "TOKEN_FILE_NAME",
    "TOKEN_MODE",
    "TokenError",
    "cookie_matches",
    "cookie_value",
    "ensure_token",
    "read_token",
    "token_matches",
    "token_path",
]
