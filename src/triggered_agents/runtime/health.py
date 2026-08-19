"""Health check for every registered triggered-agent.

One line per agent: is the systemd timer active, and how fresh is the last *healthy* tick in
runs.jsonl (a tick that answered counts — precheck-nothing-to-do still proves the timer fires and
the runtime runs). A result of "error" or "board-unreachable" does NOT count on its own: precheck
logs one of those every tick a broken Kanboard/env keeps failing, so if freshness went by the raw
last event a permanently down board would look perpetually alive (fresh error every 3 minutes)
instead of red. "board-unreachable" is the deferred-run record (secretary-964): it is not the
agent's own failure, but it is not an answered tick either, and a board that never comes back must
still go red here. Last `advance` is informational: it can legitimately be days old when nothing changed
upstream. Exit non-zero if any agent is red.

Both sources here are the live data plane, not a checkout (secretary-833):

  * curator/steward/retro write runs.jsonl through `AgentState`, whose root is TA_STATE or
    `~/secretary-data/automation-state` — the packaged units set neither TA_STATE nor a different
    HOME, so that default is where their real records land. Reading a `state/` dir inside the
    agent's worktree instead reported `no runs.jsonl yet` for agents that were ticking fine.
  * the pipeline line is the production dispatcher's own tick telemetry
    (runtime/production_telemetry.py), the record written by the timer that actually moves cards.
    A recorded tick that ended unhealthy is red on its own: it must never be answered with the
    last healthy one that came before it.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import tomllib
from pathlib import Path

from . import production_telemetry
from .state import AgentState

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENV_MAX_AGE = os.environ.get("TA_HEALTH_MAX_AGE_S")  # global override, wins for every agent
# Units are the packaged ones under host.unit_prefix, not the decommissioned
# ta-* names. Most agents map to <prefix><agent>.timer; the pipeline's clock is
# the production dispatcher's timer, which is named after the component rather
# than the agent.
_UNIT_PREFIX = os.environ.get("SECRETARY_UNIT_PREFIX", "secretary-")
_UNIT_COMPONENT = {"pipeline": "dispatcher-production"}
# Freshness budget per systemd cadence: timer period + slack. Read from the agent's spec so a
# daily agent (retro) isn't flagged red just for ticking less often than an hourly one.
_CADENCE_MAX_AGE_S = {"hourly": 3 * 3600, "daily": 26 * 3600}


def _max_age_s(agent: str) -> int:
    if _ENV_MAX_AGE:
        return int(_ENV_MAX_AGE)
    try:
        spec = tomllib.loads(
            (_REPO_ROOT / "src" / "triggered_agents" / "agents" / agent / "automation.toml").read_text()
        )
    except (OSError, tomllib.TOMLDecodeError):
        return 3 * 3600
    # An explicit [health] max_age_s wins — needed for a calendar like pipeline's raw 3-min
    # OnCalendar expression, which isn't one of the named cadences below.
    explicit = spec.get("health", {}).get("max_age_s")
    if explicit is not None:
        return int(explicit)
    cadence = spec.get("systemd", {}).get("calendar", "hourly")
    return _CADENCE_MAX_AGE_S.get(cadence, 3 * 3600)


def timer_unit(agent: str) -> str:
    return f"{_UNIT_PREFIX}{_UNIT_COMPONENT.get(agent, agent)}.timer"


def _timer_active(agent: str) -> bool:
    p = subprocess.run(["systemctl", "is-active", timer_unit(agent)], capture_output=True, text=True)
    return p.stdout.strip() == "active"


def _runs(agent: str) -> list[dict]:
    path = AgentState(agent).dir / "runs.jsonl"
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
    then = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.UTC)
    return (datetime.datetime.now(datetime.UTC) - then).total_seconds()


def _stale(ts: str, agent: str) -> str | None:
    """Problem text when `ts` is older than the agent's freshness budget, else None. An
    unparseable timestamp is itself a problem: it is a record nobody can date, not a fresh one."""
    try:
        age = _age_s(ts)
    except ValueError:
        return f"last healthy tick has an unreadable timestamp ({ts!r})"
    max_age = _max_age_s(agent)
    if age > max_age:
        return f"last healthy tick {int(age / 60)}min ago (> {max_age // 60}min)"
    return None


#: Results that record a tick which never got an answer: the agent broke ("error"), or the board
#: refused the connection for every attempt the gate made ("board-unreachable"). Neither proves the
#: agent is alive, so neither may set the freshness clock.
_UNANSWERED_RESULTS = {"error", "board-unreachable"}


def _runs_status(agent: str) -> tuple[list[str], str]:
    """(problems, informational detail) from the agent's own runs.jsonl."""
    runs = _runs(agent)
    if not runs:
        return ["no runs.jsonl yet"], ""
    problems = []
    healthy = [r for r in runs if r.get("result") not in _UNANSWERED_RESULTS]
    if not healthy:
        problems.append("no answered tick yet — board/env never came up")
        last_tick = runs[-1]
    else:
        last_tick = healthy[-1]
        stale = _stale(str(last_tick.get("ts") or ""), agent)
        if stale:
            problems.append(stale)
    last_advance = next((r for r in reversed(runs) if r.get("event") == "advance"), None)
    tick = last_tick.get("ts", "-")
    adv = last_advance["ts"] if last_advance else "-"
    return problems, f"last tick {tick}, last advance {adv}"


def _pipeline_status() -> tuple[list[str], str]:
    """(problems, informational detail) from the production dispatcher's durable tick telemetry.

    The unhealthy branch is the point of this line: the dispatcher keeps ticking through a broken
    board or a failing host, so the last tick's own outcome decides the status and a fresher
    healthy predecessor never speaks for it. Freshness is checked on top of that, for the case
    where the timer or the whole tick stopped producing records at all.
    """
    telemetry = production_telemetry.read()
    if not telemetry.available:
        return [f"{telemetry.unavailable} ({telemetry.path})"], ""
    problems = []
    last = telemetry.last
    if not last:
        problems.append("no production tick recorded yet")
    elif not last.get("healthy"):
        problems.append(f"last tick unhealthy: {production_telemetry.describe(last)}")
    if not telemetry.last_healthy_at:
        problems.append("no healthy production tick recorded yet")
    else:
        stale = _stale(telemetry.last_healthy_at, "pipeline")
        if stale:
            problems.append(stale)
    detail = f"last tick {last.get('at', '-')}, last healthy {telemetry.last_healthy_at or '-'}"
    return problems, detail


def check(agents: tuple[str, ...]) -> int:
    rc = 0
    for agent in agents:
        # No automation.toml means the agent is CLI-only (no timer, no runs); it has nothing to
        # be red about, so report it neutrally and move on.
        if not (
            _REPO_ROOT / "src" / "triggered_agents" / "agents" / agent / "automation.toml"
        ).is_file():
            print(f"SKIP {agent}: no automation (CLI-only)")
            continue
        problems = []
        if not _timer_active(agent):
            problems.append(f"{timer_unit(agent)} not active")
        source_problems, detail = _pipeline_status() if agent == "pipeline" else _runs_status(agent)
        problems += source_problems
        status = "RED " if problems else "OK  "
        print(f"{status}{agent}: {'; '.join(problems) if problems else detail}")
        if problems:
            rc = 1
    return rc
