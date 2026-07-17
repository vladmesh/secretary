"""Persistent pipeline-wide pause flag: `state/pipeline/pause.json`.

Absent (no file) = running. Present = paused, with internal `mode` stored as `"soft"` or `"hard"`,
plus the public reason/actor and the refs stopped by a hard pause. The two `stopped_*` lists are
only ever populated by a hard pause (dispatcher.pause) and name exactly the cards whose live
terminal it stopped, so dispatcher.resume() knows what to park or relaunch without re-deriving it
from cards.json. `excluded_worker` names a narrow initiator/operator exception: a worker workspace
deliberately left running while every other In-progress worker is stopped. A card's column/record
shape alone can't tell "a terminal was actually running here when pause hit" from "this Validate
card just hasn't spawned a reviewer yet". A single Validate card can appear in both stopped lists
at once: its original worker terminal is parked for CI rework, and its live layer-3 reviewer is
relaunched to finish the review.

Automation-owned hard pauses are not allowed to freeze the queue forever. A hard pause whose
actor is in HARD_PAUSE_AUTO_RESUME_ACTORS auto-resumes after
HARD_PAUSE_AUTO_RESUME_TTL_SECONDS, default 45 minutes, through dispatcher.resume(), so stopped
heads follow the same park/relaunch path as a manual resume. Human or unknown actors are treated
as manual maintenance windows and never auto-resume.

Read from two places outside dispatcher.py itself: runtime/dispatch.py (steward/curator/retro
dispatch) and cli.py (pause-status) — both only ever call is_paused()/load()/status(), never
write. Kept in its own tiny module (state.py primitives only, no ops/worker/dispatcher import) so
runtime/dispatch.py can read it without pulling in dispatcher's board/host machinery — the same
reason it already lazy-imports agents.pipeline.health instead of importing dispatcher directly.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .state import STATE

PAUSE_FILE = STATE.dir / "pause.json"
MODES = ("soft", "hard")
PUBLIC_MODES = ("drain", "freeze")
MODE_ALIASES = {
    "drain": "soft",
    "freeze": "hard",
    "soft": "soft",
    "hard": "hard",
}
DISPLAY_MODES = {
    "soft": "drain",
    "hard": "freeze",
}
DEFAULT_REASON = "admin pause"
DEFAULT_ACTOR = "pipeline"
HARD_PAUSE_AUTO_RESUME_TTL_SECONDS = int(os.environ.get("TA_HARD_PAUSE_AUTO_RESUME_TTL_S", "2700"))
HARD_PAUSE_AUTO_RESUME_ACTORS = tuple(
    actor.strip()
    for actor in os.environ.get(
        "TA_HARD_PAUSE_AUTO_RESUME_ACTORS",
        "pipeline,secretary-backup,secretary,steward,curator,retro",
    ).split(",")
    if actor.strip()
)


def normalize_mode(mode: str | None) -> str | None:
    return MODE_ALIASES.get(mode or "")


def display_mode(mode: str | None) -> str | None:
    return DISPLAY_MODES.get(mode or "", mode)


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _checkout_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _candidate_pause_files() -> list[Path]:
    """Likely legacy pause locations. This stays narrow so a read-only status call never turns
    into a host-wide filesystem walk."""
    paths: set[Path] = set()
    ta_state = os.environ.get("TA_STATE")
    if ta_state:
        paths.add(Path(ta_state) / "pipeline" / "pause.json")
    paths.add(_checkout_root() / "state" / "pipeline" / "pause.json")
    paths.add(Path.home() / "triggered-agents" / "state" / "pipeline" / "pause.json")

    workspaces_root = Path(os.environ.get("TA_WORKSPACES_ROOT") or Path.home() / "orca" / "workspaces")
    agents_root = workspaces_root / "triggered-agents"
    if agents_root.is_dir():
        paths.update(agents_root.glob("*/state/pipeline/pause.json"))
    return sorted(paths, key=lambda p: str(p))


def shadow_pause_files() -> list[str]:
    live = _resolve(PAUSE_FILE)
    out: list[str] = []
    for path in _candidate_pause_files():
        if not path.is_file():
            continue
        resolved = _resolve(path)
        if resolved != live:
            out.append(str(resolved))
    return out


def _on_resume(mode: str | None, stopped_worker: list[str], stopped_reviewer: list[str],
               excluded_worker: list[str] | None = None) -> str:
    if mode == "hard":
        workers = len(stopped_worker)
        reviewers = len(stopped_reviewer)
        excluded = len(excluded_worker or [])
        return (
            f"resume clears freeze, relaunches {workers} stopped worker head(s) and "
            f"{reviewers} stopped reviewer head(s) in existing workspaces, parks Validate-only "
            f"workers for rework return, and resets watchdog clocks; {excluded} explicitly "
            "excluded In-progress worker head(s) keep their "
            "existing terminal"
        )
    if mode == "soft":
        return (
            "resume clears drain and allows new worker claims plus steward/curator/retro dispatch; "
            "cards already in progress kept running during the pause"
        )
    return "resume clears the pause flag"


def _parse_since(value: object) -> datetime | None:
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
    return parsed.astimezone(timezone.utc)


def hard_pause_auto_resume_status(state: dict, *, now: datetime | None = None) -> dict:
    """Decision data for stale hard-pause recovery.

    Only automation-owned freeze pauses auto-resume. Human or unknown actors may hold a freeze for
    as long as needed, so a long maintenance window does not get lifted without the person who set
    it noticing. The dispatcher calls this from precheck and tick before the ordinary hard-paused
    skip, then uses its normal resume() path to relaunch stopped In-progress workers, relaunch
    reviewers, park Validate-only workers for future rework return and reset watchdog clocks.
    """
    ttl = max(0, int(HARD_PAUSE_AUTO_RESUME_TTL_SECONDS))
    out = {
        "eligible": False,
        "ttl_seconds": ttl,
        "reason": "not-hard-pause",
    }
    if state.get("mode") != "hard":
        return out
    if ttl <= 0:
        out["reason"] = "disabled"
        return out
    actor = (state.get("actor") or "").strip()
    out["actor"] = actor
    if actor not in HARD_PAUSE_AUTO_RESUME_ACTORS:
        out["reason"] = "manual-or-unknown-actor"
        return out
    since = _parse_since(state.get("since"))
    if since is None:
        out["reason"] = "missing-or-invalid-since"
        return out
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = max(0, int((current - since).total_seconds()))
    out["age_seconds"] = age
    if age < ttl:
        out["reason"] = "fresh"
        return out
    out["eligible"] = True
    out["reason"] = "stale-automation-hard-pause"
    return out


def load() -> dict:
    """{} (not paused) when the file is absent or unreadable. A corrupt file fails toward "not
    paused" rather than wedging every caller (precheck/tick/dispatch.run all call this on every
    single tick) — but that fail-open is exactly backwards from the pause flag's own purpose, so
    it's not silent: logged as a warn every time it's hit, same discipline as any other
    recurring-until-fixed condition here (e.g. precheck's own head-health probe failure)."""
    if not PAUSE_FILE.is_file():
        return {}
    try:
        return json.loads(PAUSE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        STATE.log_run("pause-flag", result="corrupt", level="warn", error=str(e))
        return {}


def save(mode: str, stopped_worker: list[str] | None = None,
        stopped_reviewer: list[str] | None = None, excluded_worker: list[str] | None = None,
        reason: str = DEFAULT_REASON, actor: str = DEFAULT_ACTOR) -> None:
    STATE.ensure_dir()
    state = {
        "mode": mode,
        "since": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "actor": actor,
        "stopped_worker": stopped_worker or [],
        "stopped_reviewer": stopped_reviewer or [],
        "excluded_worker": excluded_worker or [],
    }
    tmp = PAUSE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PAUSE_FILE)


def clear() -> None:
    try:
        PAUSE_FILE.unlink()
    except FileNotFoundError:
        pass


def is_paused() -> bool:
    return bool(load())


def status() -> dict:
    live_path = str(_resolve(PAUSE_FILE))
    shadow_files = shadow_pause_files()
    base = {
        "live_state_path": live_path,
        "other_pause_files": shadow_files,
        "warnings": [
            f"other pause.json exists outside the live pipeline state: {path}"
            for path in shadow_files
        ],
    }
    state = load()
    if not state:
        return {"paused": False, **base}
    stopped_worker = state.get("stopped_worker") or []
    stopped_reviewer = state.get("stopped_reviewer") or []
    excluded_worker = state.get("excluded_worker") or []
    mode = state.get("mode")
    return {
        "paused": True,
        "mode": display_mode(mode),
        "internal_mode": mode,
        "since": state.get("since"),
        "reason": state.get("reason") or "",
        "actor": state.get("actor") or "",
        "stopped_worker": stopped_worker,
        "stopped_reviewer": stopped_reviewer,
        "excluded_worker": excluded_worker,
        "on_resume": _on_resume(mode, stopped_worker, stopped_reviewer, excluded_worker),
        "auto_resume": hard_pause_auto_resume_status(state),
        **base,
    }
