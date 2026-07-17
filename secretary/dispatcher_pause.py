"""Legacy dispatcher pause probe for the pilot cutover."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
