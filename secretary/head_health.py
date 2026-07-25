"""Resource health the head resolver reads at bring-up.

A resource (`heads.yaml` `[resources.*]`) is the account several profiles draw from, so red is a
property of the resource, not of a profile: a card whose head sits on a red resource still gets a
head, by walking that profile's declared `fallback` chain to one drawing from a green resource.
That walk is what makes a reviewer asked for as `codex-reviewer` actually launch as `claude-opus`.

The dispatcher never probes here. Probing costs real quota and already happens on its own timer;
this module only reads the status file that probe leaves behind, so a tick is a file read. The file
is `state/heads/resource_health.json` under the instance, or whatever `SECRETARY_RESOURCE_HEALTH`
points at when the probe writes elsewhere. Shape, one entry per resource:

    {"openai-sub": {"status": "red", "checked_at": 1784941231.2}}

No file, an unreadable file, or an entry older than `HEALTH_TTL_S` all mean "unknown", and unknown
means green: an absent or stale probe must not silently re-route every card in the queue onto a
different model family. Everything else about routing (which chains exist, what they contain) stays
in the registry; this module answers one question, "is this resource red right now".
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


GREEN = "green"
RED = "red"
HEALTH_RELATIVE = Path("state") / "heads" / "resource_health.json"
HEALTH_ENV = "SECRETARY_RESOURCE_HEALTH"
# A status nobody has refreshed for a quarter of an hour is not evidence about now. The pipeline
# probe re-checks every ~5 minutes, so this leaves room for a couple of missed cycles before a red
# stops counting.
HEALTH_TTL_S = float(os.environ.get("SECRETARY_RESOURCE_HEALTH_TTL_S", "900"))


def health_path(instance_dir: Path) -> Path:
    override = os.environ.get(HEALTH_ENV)
    return Path(override) if override else Path(instance_dir) / HEALTH_RELATIVE


def resource_statuses(instance_dir: Path, *, now: float | None = None) -> dict[str, str]:
    """`{resource: "green"|"red"}` for every resource the status file still speaks for."""
    try:
        loaded = json.loads(health_path(instance_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    moment = time.time() if now is None else now
    statuses: dict[str, str] = {}
    for resource, entry in loaded.items():
        status = _status_of(entry, moment)
        if status:
            statuses[str(resource)] = status
    return statuses


def _status_of(entry: Any, now: float) -> str:
    """One resource's status, or "" when the entry says nothing usable about this moment."""
    if isinstance(entry, str):
        # A hand-written file may carry the bare status; it has no timestamp to age out.
        value = entry.strip().lower()
        return value if value in (GREEN, RED) else ""
    if not isinstance(entry, dict):
        return ""
    value = str(entry.get("status") or "").strip().lower()
    if value not in (GREEN, RED):
        return ""
    checked_at = entry.get("checked_at")
    if isinstance(checked_at, (int, float)) and not isinstance(checked_at, bool):
        if HEALTH_TTL_S > 0 and now - float(checked_at) > HEALTH_TTL_S:
            return ""
    return value
