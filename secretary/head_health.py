"""Cheap, cached preflight checks for dispatcher head resources."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary._fsutil import write_json


PROBE_TTL_SECONDS = 300
PROBE_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class HeadReadiness:
    resource: str
    status: str
    reason: str
    checked_at: float
    cached: bool = False

    @property
    def launch_allowed(self) -> bool:
        return self.status in {"ready", "unknown"}

    def to_json(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "status": self.status,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "cached": self.cached,
        }


class HeadHealth:
    """Store resource verdicts independently from the dispatcher attempt state.

    A failed probe is not proof that the provider is down.  Only a definite authentication or
    provider failure stops a launch; probe execution failures are recorded as ``unknown`` and
    retry after the normal TTL.
    """

    def __init__(self, catalog: Any, data_dir: Path) -> None:
        self.catalog = catalog
        self.path = data_dir / "dispatcher" / "resource_health.json"

    def check(self, head: str) -> HeadReadiness:
        try:
            profile = self.catalog.head_profile(head)
            resource = str(profile["resource"])
            probe = str(self.catalog.resource(resource).get("probe") or "")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            return HeadReadiness("", "unknown", f"head health configuration unavailable: {type(exc).__name__}", time.time())
        if not probe:
            return HeadReadiness(resource, "unknown", "resource has no probe command", time.time())

        cache = self._load()
        entry = cache.get(resource)
        now = time.time()
        if isinstance(entry, dict) and now - float(entry.get("checked_at") or 0) < PROBE_TTL_SECONDS:
            return HeadReadiness(resource, str(entry.get("status") or "unknown"), str(entry.get("reason") or ""), float(entry["checked_at"]), True)

        verdict = self._run(resource, probe, now)
        cache[resource] = verdict.to_json()
        try:
            self._save(cache)
        except RuntimeError:
            # The preflight still has a useful verdict when its observability cache cannot be
            # written.  A later dispatcher write will surface a broader data-dir failure.
            pass
        return verdict

    def snapshot(self) -> dict[str, Any]:
        return self._load()

    def _run(self, resource: str, probe: str, now: float) -> HeadReadiness:
        try:
            completed = subprocess.run(
                probe, shell=True, text=True, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return HeadReadiness(resource, "unknown", "probe timed out", now)
        except Exception as exc:  # a broken probe must not turn into a false resource outage
            return HeadReadiness(resource, "unknown", f"probe could not run: {type(exc).__name__}", now)
        if completed.returncode == 0:
            return HeadReadiness(resource, "ready", "probe succeeded", now)
        text = " ".join((completed.stdout or "", completed.stderr or "")).lower()
        if any(marker in text for marker in ("login", "not authenticated", "unauthorized", "authentication", " 401", " 403")):
            return HeadReadiness(resource, "unauthenticated", "resource authentication failed", now)
        if any(marker in text for marker in ("503", "circuit_open", "unavailable", "rate limit", " 429", "connection", "network")):
            return HeadReadiness(resource, "unavailable", "resource provider is unavailable", now)
        return HeadReadiness(resource, "unknown", "probe returned an unclassified failure", now)

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save(self, cache: dict[str, Any]) -> None:
        write_json(self.path, cache)
