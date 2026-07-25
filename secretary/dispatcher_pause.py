"""Pause flags: the production dispatcher's own, plus the legacy probe kept from the cutover.

`ProductionPause` is the working pause. It lives next to the production dispatcher's state in the
live data plane (`<data_dir>/dispatcher/pause.json`), because that is the file the tick reads on
every run. The legacy flag under the pipeline worktree is still read by the background roles
(steward/curator/retro, `triggered_agents/runtime/dispatch.py`), so a pause mirrors itself there
and a resume removes that mirror again — but only when the pause wrote it, never a file that was
already on disk.

Semantics come from the legacy `dispatcher.pause()` docstring and are unchanged:

  drain  — no new claims and no background-role dispatch; cards already in flight keep riding
           their cycle to the end.
  freeze — drain, plus the live worker and reviewer heads are stopped and the tick advances
           nothing at all. Workspaces and worktrees are never removed, so branches and
           uncommitted work stay exactly as the heads left them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary._fsutil import write_json

PAUSE_MODES = ("drain", "freeze")
_PAUSE_MODE_ALIASES = {"drain": "drain", "soft": "drain", "freeze": "freeze", "hard": "freeze"}
_LEGACY_MODES = {"drain": "soft", "freeze": "hard"}


def normalize_pause_mode(mode: str | None) -> str:
    """Public mode for a requested one, "" when it is not a pause mode. The legacy `soft`/`hard`
    spellings keep parsing: operators and runbooks still carry them."""
    return _PAUSE_MODE_ALIASES.get(str(mode or "").strip().lower(), "")


@dataclass(frozen=True)
class LegacyPauseSnapshot:
    sufficient: bool
    reason: str
    path: str = ""
    mode: str = ""
    actor: str = ""
    since: str = ""
    checked_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "sufficient": self.sufficient,
            "reason": self.reason,
            "path": self.path,
            "mode": self.mode,
            "actor": self.actor,
            "since": self.since,
            "checked_paths": list(self.checked_paths),
            "warnings": list(self.warnings),
        }


class FileLegacyPauseProbe:
    """Read the legacy dispatcher pause flag without importing legacy runtime modules."""

    def __init__(self, pause_file: Path | None = None) -> None:
        self.pause_file = pause_file

    def snapshot(self) -> LegacyPauseSnapshot:
        paths = [self.pause_file] if self.pause_file is not None else _legacy_pause_candidates()
        paths = [path for path in paths if path is not None]
        checked = tuple(str(path) for path in paths)
        path = next((candidate for candidate in paths if candidate.is_file()), None)
        if path is None:
            return LegacyPauseSnapshot(
                False,
                "legacy freeze pause file is missing",
                checked_paths=checked,
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return LegacyPauseSnapshot(
                False,
                "legacy freeze pause file is unreadable",
                path=str(path),
                checked_paths=checked,
            )
        if not isinstance(payload, dict):
            return LegacyPauseSnapshot(
                False,
                "legacy freeze pause file has an unsupported shape",
                path=str(path),
                checked_paths=checked,
            )
        mode = str(payload.get("mode") or "")
        display_mode = _legacy_display_mode(mode)
        base = {
            "path": str(path),
            "mode": display_mode,
            "actor": str(payload.get("actor") or ""),
            "since": str(payload.get("since") or ""),
            "checked_paths": checked,
        }
        if mode != "hard":
            shown = display_mode or "not paused"
            return LegacyPauseSnapshot(
                False,
                f"legacy pause mode is {shown}, requires freeze",
                **base,
            )
        auto_resume = _legacy_hard_pause_auto_resume_status(payload)
        if auto_resume.get("eligible"):
            return LegacyPauseSnapshot(
                False,
                "legacy freeze is automation-owned and auto-resume eligible",
                warnings=(str(auto_resume.get("reason") or ""),),
                **base,
            )
        return LegacyPauseSnapshot(True, "legacy dispatcher is freeze-paused", **base)


class ProductionPause:
    """The pause flag the production tick reads. Absent file = running."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "dispatcher"
        self.path = self.root / "pause.json"

    def load(self) -> dict[str, Any]:
        """State, or {} when the pause is not set.

        A corrupt file reads as "not paused" rather than wedging every tick, the same fail-open the
        legacy flag chose. It is not silent: `mode()` still returns "" but `status()` carries the
        warning, so an operator asking why the pipeline is running gets an answer.
        """
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError):
            return {"corrupt": True}
        if not isinstance(payload, dict):
            return {"corrupt": True}
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
            out["warnings"] = [f"pause file is unreadable and read as not paused: {self.path}"]
        if not mode:
            return out
        out.update(
            {
                "since": str(state.get("since") or ""),
                "actor": str(state.get("actor") or ""),
                "reason": str(state.get("reason") or ""),
                "stopped_worker": list(state.get("stopped_worker") or []),
                "stopped_reviewer": list(state.get("stopped_reviewer") or []),
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
) -> dict[str, Any]:
    return {
        "version": 1,
        "mode": mode,
        "since": since,
        "actor": actor,
        "reason": reason,
        "stopped_worker": sorted(stopped_worker),
        "stopped_reviewer": sorted(stopped_reviewer),
        "excluded_worker": sorted(excluded_worker),
        "legacy_mirror": legacy_mirror,
    }


def on_resume_text(mode: str, stopped_worker: list[str], stopped_reviewer: list[str]) -> str:
    if mode == "freeze":
        return (
            f"resume clears the freeze, relaunches {len(stopped_worker)} stopped worker head(s) and "
            f"{len(stopped_reviewer)} stopped reviewer head(s) in their existing workspaces, and "
            "gives every wait watchdog a fresh window"
        )
    if mode == "drain":
        return (
            "resume clears the drain and lets the tick claim Ready cards again; cards already in "
            "flight kept running through the pause"
        )
    return "not paused, resume is a no-op"


def legacy_mirror_path() -> Path:
    """Where a mirrored legacy flag is written.

    Resolved the way the background roles resolve their own state dir
    (`triggered_agents/runtime/shared_state.resolve_pipeline_state_dir`), not the wider candidate
    list the probe reads: a mirror written anywhere else is a file nobody checks.
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

    Best effort by design: the product dispatcher is paused by its own file, and a legacy path that
    cannot be written must not fail the pause. The result records what happened either way, and an
    existing legacy flag is never overwritten — someone else owns it, and clearing it on resume
    would lift a pause this command did not set.
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


def _legacy_pause_candidates() -> list[Path]:
    explicit_file = os.environ.get("SECRETARY_LEGACY_PAUSE_FILE")
    if explicit_file:
        return [Path(explicit_file)]
    paths: list[Path] = []
    for name in ("SECRETARY_LEGACY_PIPELINE_STATE_DIR", "TA_PIPELINE_STATE_DIR"):
        value = os.environ.get(name)
        if value:
            paths.append(Path(value) / "pause.json")
    ta_state = os.environ.get("TA_STATE")
    if ta_state:
        paths.append(Path(ta_state) / "pipeline" / "pause.json")
    workspaces_root = Path(os.environ.get("TA_WORKSPACES_ROOT") or Path.home() / "orca" / "workspaces")
    paths.append(workspaces_root / "secretary" / "pipeline" / "state" / "pipeline" / "pause.json")
    return _dedupe_paths(paths)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _legacy_display_mode(mode: str) -> str:
    return {"hard": "freeze", "soft": "drain"}.get(mode, mode)


def _legacy_hard_pause_auto_resume_status(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"eligible": False, "reason": "not-hard-pause"}
    if payload.get("mode") != "hard":
        return out
    try:
        ttl = int(os.environ.get("TA_HARD_PAUSE_AUTO_RESUME_TTL_S", "2700"))
    except ValueError:
        ttl = 2700
    ttl = max(0, ttl)
    out["ttl_seconds"] = ttl
    if ttl <= 0:
        out["reason"] = "disabled"
        return out
    actors = tuple(
        item.strip()
        for item in os.environ.get(
            "TA_HARD_PAUSE_AUTO_RESUME_ACTORS",
            "pipeline,secretary-backup,secretary,steward,curator,retro",
        ).split(",")
        if item.strip()
    )
    actor = str(payload.get("actor") or "").strip()
    out["actor"] = actor
    if actor not in actors:
        out["reason"] = "manual-or-unknown-actor"
        return out
    out["eligible"] = True
    out["reason"] = "automation-hard-pause"
    return out
