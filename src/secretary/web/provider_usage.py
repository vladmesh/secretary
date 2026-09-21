"""Read-only subscription usage snapshots for the local dashboard.

Credentials are read only long enough to make the providers' own usage request.  They are never
returned, logged, cached, or included in an error message.  Codex session events are also a useful
fallback: the CLI records the same rate-limit document on normal turns.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
STALE_AFTER_SECONDS = 15 * 60
CACHE_SECONDS = 5 * 60
# The Codex fallback reads a fixed amount however large ~/.codex/sessions grows: it descends the
# sessions/YYYY/MM/DD directories newest-first, opens at most CODEX_FALLBACK_FILES rollouts and reads
# only the last CODEX_TAIL_BYTES of each.  On the production tree (2,478 rollouts, 2026-09-21) the
# last rate-limit line sat at most 157 KB before the end of a file, the median 1.4 KB.
CODEX_FALLBACK_FILES = 20
CODEX_FALLBACK_LISTINGS = 32
CODEX_TAIL_BYTES = 256 * 1024


def _iso(epoch: float | str | None) -> str | None:
    if isinstance(epoch, str):
        try:
            parsed = datetime.fromisoformat(epoch)
        except ValueError:
            return None
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _window(name: str, raw: Any, *, default_minutes: int | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    used = _number(raw.get("used_percent", raw.get("used_percentage")))
    utilization = _number(raw.get("utilization"))
    if used is None and utilization is not None:
        used = utilization * 100 if utilization <= 1 else utilization
    if used is None:
        return None
    minutes = raw.get("window_minutes")
    if minutes is None and isinstance(raw.get("limit_window_seconds"), (int, float)):
        minutes = round(raw["limit_window_seconds"] / 60)
    if minutes is None:
        minutes = default_minutes
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
        minutes = default_minutes
    reset = raw.get("resets_at", raw.get("reset_at"))
    return {
        "name": name,
        "window_minutes": minutes,
        "remaining_percent": round(max(0.0, min(100.0, 100.0 - used)), 1),
        "resets_at": _iso(reset),
    }


def _fetch_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read(1024 * 1024))
    if not isinstance(value, dict):
        raise TypeError("provider returned a non-object document")
    return value


class ProviderUsageLayer:
    """Collect compact Claude and Codex usage documents without mutating provider auth."""

    def __init__(
        self,
        *,
        home: str | os.PathLike[str] | None = None,
        fetch_json: Callable[[str, dict[str, str], float], dict[str, Any]] = _fetch_json,
        now: Callable[[], float] = time.time,
        timeout: float = 3.0,
    ) -> None:
        self.home = Path(home) if home is not None else Path.home()
        self.fetch_json = fetch_json
        self.now = now
        self.timeout = timeout
        self._cached: tuple[float, dict[str, Any]] | None = None

    def usage_snapshot(self) -> dict[str, Any]:
        observed = self.now()
        if self._cached is not None and observed - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        document = {
            "kind": "provider_usage",
            "observed_at": _iso(observed),
            "providers": [self._claude(observed), self._codex(observed)],
        }
        self._cached = (observed, document)
        return document

    def _auth(self, path: Path, keys: tuple[str, ...]) -> str | None:
        try:
            value: Any = json.loads(path.read_text(encoding="utf-8"))
            for key in keys:
                value = value[key]
            return value if isinstance(value, str) and value else None
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def _claude(self, observed: float) -> dict[str, Any]:
        token = self._auth(self.home / ".claude" / ".credentials.json", ("claudeAiOauth", "accessToken"))
        if token is None:
            return self._unavailable("claude", "Claude", "Claude login is unavailable")
        try:
            raw = self.fetch_json(
                CLAUDE_USAGE_URL,
                {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
                self.timeout,
            )
            windows = [
                item
                for item in (
                    _window("5-hour", raw.get("five_hour"), default_minutes=300),
                    _window("weekly", raw.get("seven_day"), default_minutes=10080),
                )
                if item is not None
            ]
            return (
                self._available("claude", "Claude", observed, windows)
                if windows
                else self._unavailable("claude", "Claude", "Claude did not report usage windows")
            )
        except (OSError, TypeError, ValueError, HTTPError, URLError, TimeoutError):
            return self._unavailable("claude", "Claude", "Claude usage is temporarily unavailable")

    def _codex(self, observed: float) -> dict[str, Any]:
        token = self._auth(self.home / ".codex" / "auth.json", ("tokens", "access_token"))
        if token is not None:
            try:
                headers = {"Authorization": f"Bearer {token}"}
                account = self._auth(self.home / ".codex" / "auth.json", ("tokens", "account_id"))
                if account:
                    headers["ChatGPT-Account-Id"] = account
                raw = self.fetch_json(CODEX_USAGE_URL, headers, self.timeout)
                windows = self._codex_windows(raw)
                if windows:
                    return self._available("codex", "Codex", observed, windows)
            except (OSError, TypeError, ValueError, HTTPError, URLError, TimeoutError):
                pass
        fallback = self._latest_codex_event()
        if fallback is None:
            reason = (
                "Codex login is unavailable" if token is None else "Codex usage is temporarily unavailable"
            )
            return self._unavailable("codex", "Codex", reason)
        raw, source_time = fallback
        windows = self._codex_windows(raw)
        age = max(0.0, observed - source_time)
        if not windows:
            return self._unavailable("codex", "Codex", "Codex did not report usage windows")
        result = self._available("codex", "Codex", source_time, windows)
        result["age_seconds"] = round(age, 1)
        if age > STALE_AFTER_SECONDS:
            result["status"] = "stale"
            result["reason"] = "Latest Codex usage observation is stale"
        return result

    def _codex_windows(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        limits = raw.get("rate_limits", raw.get("rate_limit", raw))
        if not isinstance(limits, dict):
            return []
        primary = limits.get("primary", limits.get("primary_window"))
        secondary = limits.get("secondary", limits.get("secondary_window"))
        return [
            item
            for item in (
                _window(self._window_name(primary, "primary"), primary),
                _window(self._window_name(secondary, "secondary"), secondary),
            )
            if item is not None
        ]

    @staticmethod
    def _window_name(raw: Any, fallback: str) -> str:
        minutes = raw.get("window_minutes") if isinstance(raw, dict) else None
        if (
            minutes is None
            and isinstance(raw, dict)
            and isinstance(raw.get("limit_window_seconds"), (int, float))
        ):
            minutes = round(raw["limit_window_seconds"] / 60)
        if minutes == 300:
            return "5-hour"
        if minutes == 10080:
            return "weekly"
        return fallback

    def _latest_codex_event(self) -> tuple[dict[str, Any], float] | None:
        for path, mtime in self._newest_codex_rollouts():
            for line in self._tail_lines(path):
                try:
                    event = json.loads(line)
                    limits = event["payload"]["info"]["rate_limits"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                if isinstance(limits, dict):
                    timestamp = event.get("timestamp")
                    try:
                        source_time = datetime.fromisoformat(timestamp).timestamp()
                    except (AttributeError, ValueError):
                        source_time = mtime
                    return limits, source_time
        return None

    def _newest_codex_rollouts(self) -> list[tuple[Path, float]]:
        """Return up to CODEX_FALLBACK_FILES rollouts from the newest days, newest mtime first.

        Directories are listed newest-first, at most CODEX_FALLBACK_LISTINGS of them; within a day the
        file name (which starts with the session's start time) picks the newest ones.
        """
        budget = [CODEX_FALLBACK_LISTINGS]
        found: list[Path] = []

        def listing(path: Path) -> list[os.DirEntry[str]]:
            if budget[0] <= 0:
                return []
            budget[0] -= 1
            try:
                with os.scandir(path) as entries:
                    return list(entries)
            except OSError:
                return []

        def dated(entries: list[os.DirEntry[str]]) -> list[os.DirEntry[str]]:
            directories = []
            for entry in entries:
                try:
                    if entry.name.isdigit() and entry.is_dir():
                        directories.append(entry)
                except OSError:
                    continue
            return sorted(directories, key=lambda entry: entry.name, reverse=True)

        def descend(path: Path, depth: int) -> None:
            entries = listing(path)
            if depth == 3:
                names = sorted(
                    (
                        entry.name
                        for entry in entries
                        if entry.name.startswith("rollout-") and entry.name.endswith(".jsonl")
                    ),
                    reverse=True,
                )
                found.extend(path / name for name in names[: CODEX_FALLBACK_FILES - len(found)])
                return
            for entry in dated(entries):
                if len(found) >= CODEX_FALLBACK_FILES or budget[0] <= 0:
                    return
                descend(path / entry.name, depth + 1)

        descend(self.home / ".codex" / "sessions", 0)
        dated_files = []
        for path in found:
            try:
                dated_files.append((path, path.stat().st_mtime))
            except OSError:
                continue
        return sorted(dated_files, key=lambda item: item[1], reverse=True)

    @staticmethod
    def _tail_lines(path: Path) -> list[str]:
        """Return the complete lines in the last CODEX_TAIL_BYTES of a file, last line first."""
        try:
            with open(path, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                start = max(0, size - CODEX_TAIL_BYTES)
                handle.seek(start)
                tail = handle.read(CODEX_TAIL_BYTES)
        except OSError:
            return []
        lines = tail.split(b"\n")
        if start > 0:
            lines = lines[1:]
        return [line.decode("utf-8", errors="replace") for line in reversed(lines) if line.strip()]

    @staticmethod
    def _available(
        provider_id: str, label: str, observed: float, windows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "id": provider_id,
            "label": label,
            "status": "available",
            "reason": None,
            "observed_at": _iso(observed),
            "age_seconds": 0.0,
            "windows": windows,
        }

    @staticmethod
    def _unavailable(provider_id: str, label: str, reason: str) -> dict[str, Any]:
        return {
            "id": provider_id,
            "label": label,
            "status": "unavailable",
            "reason": reason,
            "observed_at": None,
            "age_seconds": None,
            "windows": [],
        }
