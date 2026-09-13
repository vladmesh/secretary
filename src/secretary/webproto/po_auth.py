"""The `/po` token check, as a layer: it reads the token file and nothing else.

The transport asks :meth:`PoTokenLayer.po_admits` before any `/po` route other than the login form
reaches a handler, so a request without a valid cookie never reaches the PO runner or the board store.
The file is read on every check, so replacing it takes effect on the next request.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from secretary.config import DataDirError, instance_data_dir
from secretary.po import token as po_token
from secretary.webproto.boundary import ProtocolBoundary
from secretary.webproto.errors import InstallationUnavailable, RuntimeUnavailable

COOKIE_NAME = po_token.COOKIE_NAME
COOKIE_PATH = po_token.COOKIE_PATH


class PoTokenLayer(ProtocolBoundary):
    """Checks a presented token or cookie against `DATA_DIR/po-web-token`. Construction does no I/O."""

    def __init__(self, instance: str | Path, *, data_dir: str | Path | None = None) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._lock = threading.Lock()

    def po_login(self, token: str) -> dict[str, Any]:
        """The cookie value for a matching token, or `admitted: false`. Never the token itself."""
        expected = self._token()
        if not token or not po_token.token_matches(expected, token):
            return {"kind": "po_login", "admitted": False, "cookie": None}
        return {"kind": "po_login", "admitted": True, "cookie": po_token.cookie_value(expected)}

    def po_admits(self, cookie: str) -> dict[str, Any]:
        expected = self._token()
        admitted = bool(cookie) and po_token.cookie_matches(expected, cookie)
        return {"kind": "po_admits", "admitted": admitted}

    def _token(self) -> str:
        try:
            return po_token.read_token(self._resolved_data_dir())
        except po_token.TokenError as exc:
            raise RuntimeUnavailable(str(exc)) from None

    def _resolved_data_dir(self) -> Path:
        with self._lock:
            if self._data_dir is None:
                try:
                    self._data_dir = instance_data_dir(self.instance)
                except DataDirError as exc:
                    raise InstallationUnavailable(str(exc)) from None
            return self._data_dir


__all__ = ["COOKIE_NAME", "COOKIE_PATH", "PoTokenLayer"]
