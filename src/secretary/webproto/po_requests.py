"""What a request id owns on `/po`: one created session, or one started turn.

The same idea as the sprint request index (:mod:`secretary.webproto.sprint_requests`): a form carries
a request id minted when it was served, and a repeat of that id is answered with what the first
submission produced. Here the lock is held across the operation itself — creating a session or
starting a turn is one insert and one `Popen` — so a double click waits for the first submission and
then finds its record, instead of racing it.

A refused operation records nothing: a turn refused because another one runs wrote nothing, and the
same form may be submitted again once that turn is over.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from secretary._fsutil import file_lock
from secretary.webproto.runs import RequestMismatch
from secretary.webproto.store_io import RunStoreError, write_document

PO_REQUESTS_RELATIVE = Path("webproto") / "po-requests"


class PoRequestStore:
    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.root = Path(os.fspath(data_dir)) / PO_REQUESTS_RELATIVE
        self.lock_path = self.root / ".po-requests.lock"

    def _path(self, request_id: str) -> Path:
        return self.root / f"{hashlib.sha256(request_id.encode('utf-8')).hexdigest()}.json"

    def _read(self, request_id: str) -> dict[str, Any] | None:
        path = self._path(request_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RunStoreError(f"the PO request index at {path} could not be read: {exc}") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise RunStoreError(f"the PO request record at {path} is not a record")
        return payload

    def once(
        self,
        request_id: str,
        *,
        operation: str,
        fingerprint: str,
        action: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """`action()`'s result, run at most once per request id. The flag says whether it ran now."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunStoreError(f"the PO request index at {self.root} could not be opened: {exc}") from None
        with file_lock(self.lock_path):
            existing = self._read(request_id)
            if existing is not None:
                if existing.get("operation") != operation or existing.get("fingerprint") != fingerprint:
                    raise RequestMismatch(
                        f"request id {request_id!r} already carried a different {operation} request; "
                        "open the page again for a new form"
                    )
                return dict(existing["result"]), False
            result = action()
            record = {
                "request_id": request_id,
                "operation": operation,
                "fingerprint": fingerprint,
                "result": result,
            }
            write_document(self._path(request_id), json.dumps(record, sort_keys=True, indent=2))
            return result, True


def fingerprint(*parts: str) -> str:
    return hashlib.sha256(json.dumps(list(parts)).encode("utf-8")).hexdigest()


__all__ = ["PO_REQUESTS_RELATIVE", "PoRequestStore", "fingerprint"]
