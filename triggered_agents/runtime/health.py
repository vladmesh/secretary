"""Health check for every registered triggered-agent.

One line per agent: is the systemd timer active, and how fresh is the last *healthy* tick in
runs.jsonl (any non-error event counts — precheck-nothing-to-do still proves the timer fires and
the runtime runs). An event with result "error" does NOT count on its own: precheck logs one of
those every tick a broken Kanboard/env keeps failing, so if freshness went by the raw last event
a permanently down board would look perpetually alive (fresh error every 3 minutes) instead of
red. Last `advance` is informational: it can legitimately be days old when nothing changed
upstream. Exit non-zero if any agent is red.

State lives in the agent's worktree (the systemd unit's WorkingDirectory), not in this
checkout — so we look under the dispatch workspace, honoring TA_STATE the same way the
unit would only if it were set globally.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import tomllib
from pathlib import Path

from .dispatch import _workspace

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_MAX_AGE = os.environ.get("TA_HEALTH_MAX_AGE_S")  # global override, wins for every agent
# Freshness budget per systemd cadence: timer period + slack. Read from the agent's spec so a
# daily agent (retro) isn't flagged red just for ticking less often than an hourly one.
_CADENCE_MAX_AGE_S = {"hourly": 3 * 3600, "daily": 26 * 3600}


def _max_age_s(agent: str) -> int:
    if _ENV_MAX_AGE:
        return int(_ENV_MAX_AGE)
    try:
        spec = tomllib.loads((_REPO_ROOT / "triggered_agents" / "agents" / agent / "automation.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return 3 * 3600
    # An explicit [health] max_age_s wins — needed for a calendar like pipeline's raw 3-min
    # OnCalendar expression, which isn't one of the named cadences below.
    explicit = spec.get("health", {}).get("max_age_s")
    if explicit is not None:
        return int(explicit)
    cadence = spec.get("systemd", {}).get("calendar", "hourly")
    return _CADENCE_MAX_AGE_S.get(cadence, 3 * 3600)


def _timer_active(agent: str) -> bool:
    p = subprocess.run(["systemctl", "is-active", f"ta-{agent}.timer"], capture_output=True, text=True)
    return p.stdout.strip() == "active"


def _runs(agent: str) -> list[dict]:
    path = Path(_workspace(agent)) / "state" / agent / "runs.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _age_s(ts: str) -> float:
    then = datetime.datetime.fromisoformat(ts)
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()


def check(agents: tuple[str, ...]) -> int:
    rc = 0
    for agent in agents:
        # No automation.toml means the agent is CLI-only (no timer, no runs); it has nothing to
        # be red about, so report it neutrally and move on.
        if not (_REPO_ROOT / "triggered_agents" / "agents" / agent / "automation.toml").is_file():
            print(f"SKIP {agent}: no automation (CLI-only)")
            continue
        problems = []
        if not _timer_active(agent):
            problems.append(f"ta-{agent}.timer not active")
        runs = _runs(agent)
        if not runs:
            problems.append("no runs.jsonl yet")
            last_tick = last_advance = None
        else:
            healthy = [r for r in runs if r.get("result") != "error"]
            if not healthy:
                problems.append("only error events so far — board/env never came up")
                last_tick = runs[-1]
            else:
                last_tick = healthy[-1]
                age = _age_s(last_tick["ts"])
                max_age = _max_age_s(agent)
                if age > max_age:
                    problems.append(f"last healthy tick {int(age / 60)}min ago (> {max_age // 60}min)")
            last_advance = next((r for r in reversed(runs) if r.get("event") == "advance"), None)
        status = "RED " if problems else "OK  "
        tick = last_tick["ts"] if last_tick else "-"
        adv = last_advance["ts"] if last_advance else "-"
        detail = "; ".join(problems) if problems else f"last tick {tick}, last advance {adv}"
        print(f"{status}{agent}: {detail}")
        if problems:
            rc = 1
    return rc
