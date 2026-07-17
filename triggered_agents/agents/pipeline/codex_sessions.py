"""Codex session rollout lookup: the TUI liveness signal for the watchdog.

Codex TUI paints in an alternate screen, so Orca's terminal lastOutputAt can stay stale
while the head is reasoning and writing rollout JSONL. These helpers map session files
back to a workspace and give worker.terminal_status a supplemental activity signal.

Two hard rules, both from review 373:

- A malformed session file (broken UTF-8, JSON that is not an object, non-dict payload)
  is skipped, never raised: this code runs inside every dispatcher tick, and one bad
  .jsonl must not abort the tick for every card. Same tolerant posture as the curator's
  session_meta reader (agents/curator/discover.py), kept separate because liveness and
  transcript harvesting have different filtering and failure semantics.
- The scan only visits the freshest dated day dirs (sessions/YYYY/MM/DD/...): a watchdog
  poll cares about heads alive right now, and a long-lived CODEX_HOME accumulates years
  of rollouts that a per-tick rglob would keep re-reading.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from . import heads

SESSIONS_ROOT = Path(os.environ.get("TA_CODEX_SESSIONS", str(Path(heads.CODEX_HOME) / "sessions")))
# A TUI head alive right now wrote its rollout today, or yesterday across midnight.
SCAN_DAY_DIRS = 2


def session_cwd(path: Path) -> str | None:
    """Cwd from a session jsonl's session_meta line, or None when unreadable/malformed."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 20:
                    return None
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "session_meta":
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                cwd = payload.get("cwd") or rec.get("cwd")
                return cwd if isinstance(cwd, str) and cwd else None
    except OSError:
        return None
    return None


def _record_timestamp(rec: dict) -> float | None:
    raw = rec.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_user_turn(rec: dict) -> bool:
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return False
    return rec.get("type") == "event_msg" and payload.get("type") == "user_message"


def _recent_day_dirs(root: Path, limit: int = SCAN_DAY_DIRS) -> list[Path]:
    """Newest date-leaf dirs under sessions/YYYY/MM/DD, newest first, at most `limit`.

    An unexpected layout (flat dir, no digit-named years) degrades to scanning `root`
    itself: a full scan is slower but correct, a skipped tree would be a blind spot."""
    try:
        years = sorted((d for d in root.iterdir() if d.is_dir() and d.name.isdigit()),
                       key=lambda d: d.name, reverse=True)
    except OSError:
        return [root]
    if not years:
        return [root]
    days: list[Path] = []
    for year in years:
        try:
            months = sorted((d for d in year.iterdir() if d.is_dir()),
                            key=lambda d: d.name, reverse=True)
        except OSError:
            continue
        for month in months:
            try:
                leaves = sorted((d for d in month.iterdir() if d.is_dir()),
                                key=lambda d: d.name, reverse=True)
            except OSError:
                continue
            for day in leaves:
                days.append(day)
                if len(days) >= limit:
                    return days
    return days or [root]


def _session_paths_for(workspace: str):
    root = SESSIONS_ROOT
    if not root.is_dir():
        return
    want = str(Path(workspace).resolve(strict=False))
    for day_dir in _recent_day_dirs(root):
        try:
            files = list(day_dir.glob("*.jsonl"))
        except OSError:
            continue
        for path in files:
            if session_cwd(path) != want:
                continue
            yield path


def latest_activity_for(workspace: str) -> float | None:
    """Latest mtime among recent session files whose session_meta cwd is `workspace`."""
    latest: float | None = None
    for path in _session_paths_for(workspace):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    return latest


def latest_user_turn_for(workspace: str, since: float) -> float | None:
    """Latest Codex user turn for `workspace` after `since`, or None.

    This is a delivery proof, not transcript harvesting. It only checks record shape
    and timestamps, never prompt text, so TASK.md and REVIEW.md share the same path
    without leaking task content into telemetry.
    """
    latest: float | None = None
    for path in _session_paths_for(workspace):
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict) or not _is_user_turn(rec):
                        continue
                    ts = _record_timestamp(rec)
                    if ts is None or ts <= since:
                        continue
                    if latest is None or ts > latest:
                        latest = ts
        except OSError:
            continue
    return latest
