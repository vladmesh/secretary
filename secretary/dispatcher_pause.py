"""The production dispatcher's pause flag, and the legacy flag it mirrors itself into.

`ProductionPause` is the working pause, next to the production dispatcher's state in the live
data plane (`<data_dir>/dispatcher/pause.json`). The legacy flag under the pipeline worktree is
still read by the background roles, so a pause mirrors itself there and a resume removes that
mirror again — but only when the pause wrote it, never a file that was already on disk.

Semantics:

  drain  — no new claims and no background-role dispatch; cards already in flight keep riding
           their cycle to the end.
  freeze — drain, plus the live worker and reviewer heads are stopped and the tick advances
           nothing at all. Workspaces and worktrees are never removed.

A freeze set by automation carries a TTL and is resumed by the next tick once it outlives
`TA_HARD_PAUSE_AUTO_RESUME_TTL_S`, because `secretary backup create` resumes in its `finally` and
a backup killed before that block would otherwise leave the dispatcher frozen forever. A freeze
held by a human is a maintenance window and never expires.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from secretary._fsutil import write_json

PAUSE_MODES = ("drain", "freeze")
AUTO_RESUME_TTL_DEFAULT = 2700
AUTO_RESUME_ACTORS_DEFAULT = "pipeline,secretary-backup,secretary,steward,curator,retro"
_PAUSE_MODE_ALIASES = {"drain": "drain", "soft": "drain", "freeze": "freeze", "hard": "freeze"}
_LEGACY_MODES = {"drain": "soft", "freeze": "hard"}


def normalize_pause_mode(mode: str | None) -> str:
    """Public mode for a requested one, "" when it is not a pause mode. The legacy `soft`/`hard`
    spellings keep parsing: operators and runbooks still carry them."""
    return _PAUSE_MODE_ALIASES.get(str(mode or "").strip().lower(), "")


def auto_resume_ttl_seconds() -> int:
    """TTL for an automation-owned freeze, 0 when auto-resume is turned off."""
    try:
        ttl = int(os.environ.get("TA_HARD_PAUSE_AUTO_RESUME_TTL_S", str(AUTO_RESUME_TTL_DEFAULT)))
    except ValueError:
        ttl = AUTO_RESUME_TTL_DEFAULT
    return max(0, ttl)


def auto_resume_actors() -> tuple[str, ...]:
    """Actors whose freeze expires. Anything else is a human maintenance window and is held."""
    raw = os.environ.get("TA_HARD_PAUSE_AUTO_RESUME_ACTORS", AUTO_RESUME_ACTORS_DEFAULT)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def auto_resume_status(state: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Whether this pause state is an automation-owned freeze that has outlived its TTL.

    A freeze without a readable `since` counts as expired rather than eternal: the failure mode being
    fixed here is a freeze nobody lifts.
    """
    ttl = auto_resume_ttl_seconds()
    out: dict[str, Any] = {"eligible": False, "ttl_seconds": ttl, "reason": "not-freeze"}
    if normalize_pause_mode(state.get("mode")) != "freeze":
        return out
    if ttl <= 0:
        out["reason"] = "disabled"
        return out
    actor = str(state.get("actor") or "").strip()
    out["actor"] = actor
    if actor not in auto_resume_actors():
        out["reason"] = "manual-or-unknown-actor"
        return out
    since = _parse_since(state.get("since"))
    if since is None:
        out["eligible"] = True
        out["reason"] = "missing-or-invalid-since"
        return out
    age = max(0, int((now if now is not None else time.time()) - since))
    out["age_seconds"] = age
    if age < ttl:
        out["reason"] = "fresh"
        return out
    out["eligible"] = True
    out["reason"] = "stale-automation-freeze"
    return out


def _parse_since(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class ProductionPause:
    """The pause flag the production tick reads. Absent file = running."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "dispatcher"
        self.path = self.root / "pause.json"

    def load(self) -> dict[str, Any]:
        """State, or {} when the pause is not set.

        A corrupt file is read as a freeze: continuing a dispatch while an operator's stop state cannot
        be read is worse than deferring it. `summary()` and `status()` carry an explicit warning.
        """
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError):
            return {"corrupt": True, "mode": "freeze"}
        if not isinstance(payload, dict):
            return {"corrupt": True, "mode": "freeze"}
        return payload

    def mode(self) -> str:
        return normalize_pause_mode(self.load().get("mode"))

    def save(self, payload: dict[str, Any]) -> None:
        write_json(self.path, payload)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def summary(self) -> dict[str, Any]:
        """Compact pause state for a tick result: enough for the log to say why nothing moved."""
        state = self.load()
        mode = normalize_pause_mode(state.get("mode"))
        out: dict[str, Any] = {
            "paused": bool(mode),
            "mode": mode,
            "pause_file": str(self.path),
        }
        if state.get("corrupt"):
            out["warnings"] = [f"pause file is unreadable and read as frozen: {self.path}"]
        if not mode:
            return out
        out.update(
            {
                "auto_resume": auto_resume_status(state),
                "since": str(state.get("since") or ""),
                "actor": str(state.get("actor") or ""),
                "reason": str(state.get("reason") or ""),
                "stopped_worker": list(state.get("stopped_worker") or []),
                "stopped_reviewer": list(state.get("stopped_reviewer") or []),
                "stopped_observer": list(state.get("stopped_observer") or []),
                "excluded_worker": list(state.get("excluded_worker") or []),
            }
        )
        return out


def pause_payload(
    *,
    mode: str,
    actor: str,
    reason: str,
    since: str,
    stopped_worker: list[str],
    stopped_reviewer: list[str],
    excluded_worker: list[str],
    legacy_mirror: dict[str, Any],
    stopped_observer: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "mode": mode,
        "since": since,
        "actor": actor,
        "reason": reason,
        "stopped_worker": sorted(stopped_worker),
        "stopped_reviewer": sorted(stopped_reviewer),
        "stopped_observer": sorted(stopped_observer or []),
        "excluded_worker": sorted(excluded_worker),
        "legacy_mirror": legacy_mirror,
    }


def on_resume_text(mode: str, stopped_worker: list[str], stopped_reviewer: list[str]) -> str:
    if mode == "freeze":
        return (
            f"resume clears the freeze, relaunches {len(stopped_worker)} stopped worker head(s) and "
            f"{len(stopped_reviewer)} stopped reviewer head(s) in their existing workspaces, gives "
            "every wait watchdog a fresh window, and lets the next tick bring the stopped sprint "
            "observers back"
        )
    if mode == "drain":
        return (
            "resume clears the drain and lets the tick claim Ready cards again; cards already in "
            "flight kept running through the pause"
        )
    return "not paused, resume is a no-op"


def legacy_mirror_path() -> Path:
    """Where a mirrored legacy flag is written.

    Resolved the way the background roles resolve their own state dir, not the wider candidate list
    the probe reads: a mirror written anywhere else is a file nobody checks.
    """
    explicit = os.environ.get("SECRETARY_LEGACY_PAUSE_FILE")
    if explicit:
        return Path(explicit)
    for name in ("SECRETARY_LEGACY_PIPELINE_STATE_DIR", "TA_PIPELINE_STATE_DIR"):
        value = os.environ.get(name)
        if value:
            return Path(value) / "pause.json"
    workspaces_root = Path(os.environ.get("TA_WORKSPACES_ROOT") or Path.home() / "orca" / "workspaces")
    return workspaces_root / "secretary" / "pipeline" / "state" / "pipeline" / "pause.json"


def write_legacy_mirror(*, mode: str, actor: str, reason: str, since: str) -> dict[str, Any]:
    """Mirror the pause into the legacy flag so steward/curator/retro keep shedding.

    Best effort by design: a legacy path that cannot be written must not fail the pause. An existing
    legacy flag is never overwritten — someone else owns it, and clearing it on resume would lift a
    pause this command did not set.
    """
    path = legacy_mirror_path()
    out: dict[str, Any] = {"path": str(path), "written": False}
    if path.exists():
        out["reason"] = "a legacy pause file already exists and is left untouched"
        return out
    payload = {
        "mode": _LEGACY_MODES.get(mode, "soft"),
        "since": since,
        "reason": reason,
        "actor": actor,
        "stopped_worker": [],
        "stopped_reviewer": [],
        "excluded_worker": [],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, payload)
    except OSError as exc:
        out["reason"] = f"legacy mirror could not be written: {exc}"
        return out
    out["written"] = True
    return out


def clear_legacy_mirror(state: dict[str, Any]) -> dict[str, Any]:
    """Remove a mirror this pause wrote. A mirror it did not write is left where it is."""
    mirror = state.get("legacy_mirror")
    mirror = mirror if isinstance(mirror, dict) else {}
    path = str(mirror.get("path") or "")
    if not mirror.get("written") or not path:
        return {"path": path, "cleared": False, "reason": "no legacy mirror was written by this pause"}
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return {"path": path, "cleared": False, "reason": "legacy mirror is already gone"}
    except OSError as exc:
        return {"path": path, "cleared": False, "reason": f"legacy mirror could not be removed: {exc}"}
    return {"path": path, "cleared": True}
