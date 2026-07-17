"""Deep-sweep drift check (triggered-agents-256): live ta-* systemd units plus the installed
precheck gate script vs. what deploy/provision.py would install right now from the current
triggered_agents/agents/*/automation.toml specs and repo copy of deploy/ta-gate.sh. Also flags a
double-schedule (triggered-agents-444): for an agent whose spec OPTS IN to single-scheduler
ownership (`[trigger] enabled = false`, e.g. curator), ta-<agent>.timer live on the host while the
same-named Orca automation still has its own trigger enabled — live state contradicting that
agent's own canon, which would let one hourly tick dispatch twice. An agent that never made that
promise (retro, steward) is not checked at all — its Orca automation being enabled is its
unchanged, intended state, not drift. Detection only, called from the deep-sweep section of
`.claude/skills/steward/SKILL.md`; filing an Идеи card with the diff is the skill's job, not this
module's. Auto-healing the drift is explicitly out of scope for this card (a live systemd rewrite
belongs to a human-reviewed provision run, not an unconditional daily sweep).

Reuses deploy/provision.py's own unit-string builders (_service_unit/_timer_unit/
_variant_service_unit) instead of reimplementing the rendering: two independently maintained
renderers of the same unit text would drift from EACH OTHER as much as from the host, defeating
the whole point of this check. Reads specs from THIS process's own checkout (the steward's named
worktree, kept current by the pipeline dispatcher's every-tick fast-forward,
worker._ff_agent_worktrees) rather than the canonical ~/triggered-agents checkout, which nothing
keeps up to date between provision runs and would otherwise make every comparison stale by
definition.
"""
from __future__ import annotations

import difflib
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import deploy.provision as provision  # noqa: E402

from ..pipeline import worker as pipeline_worker  # noqa: E402

SYSTEMD_DIR = Path("/etc/systemd/system")


def _expected_artifacts() -> dict[str, str]:
    """Artifact name/path -> expected content for every file provision.py owns: ta-* unit files for
    every agent spec plus the installed gate script. Unit rendering delegates to provision.
    render_units, the exact function ensure_systemd itself calls before writing units to disk, so
    this can never drift from what a real provision run would produce."""
    expected: dict[str, str] = {
        str(provision.GATE_INSTALL_PATH): provision.GATE_SCRIPT_SRC.read_text(encoding="utf-8"),
    }
    for agent_dir in sorted(provision.AGENTS_DIR.iterdir()):
        spec_path = agent_dir / "automation.toml"
        if not spec_path.is_file():
            continue
        agent = agent_dir.name
        spec = tomllib.loads(spec_path.read_text(encoding="utf-8"))
        workspace = pipeline_worker.AGENTS_ROOT / agent
        expected.update(provision.render_units(agent, spec, workspace))
    return expected


def _live_artifacts() -> dict[str, str]:
    """Every live artifact provision.py owns and the drift check can read without sudo."""
    live = {}
    if SYSTEMD_DIR.is_dir():
        for path in sorted(SYSTEMD_DIR.glob("ta-*.service")) + sorted(SYSTEMD_DIR.glob("ta-*.timer")):
            live[path.name] = path.read_text(encoding="utf-8")
    gate = provision.GATE_INSTALL_PATH
    if gate.is_file():
        live[str(gate)] = gate.read_text(encoding="utf-8")
    return live


def _diff(live: str, expected: str, artifact: str) -> str:
    return "".join(difflib.unified_diff(
        live.splitlines(keepends=True), expected.splitlines(keepends=True),
        fromfile=f"{artifact} (live)", tofile=f"{artifact} (expected from current checkout)"))


def _orca_automation_states() -> dict[str, bool] | None:
    """{Orca automation name: its own `enabled` flag} for every live automation, or None if the
    query itself failed (orca unreachable, or the response has no usable "automations" list at
    all) — None is distinct from {} (query succeeded, zero automations exist right now): the
    double-schedule check below must not read a failed query the same as "confirmed nothing to
    flag", see _double_schedule_drift / triggered-agents-444 review fixup."""
    try:
        data = provision.orca_json(["automations", "list"])
        automations = provision._unwrap(data, "automations")
        if automations is None:
            raise ValueError("orca automations list: response has no 'automations' key")
    except Exception:
        return None
    return {a["name"]: bool(a.get("enabled")) for a in automations if a.get("name")}


def _double_schedule_drift(automation_states: dict[str, bool] | None) -> list[dict]:
    """Flag every agent whose spec OPTS IN to single-scheduler ownership (`[trigger] enabled =
    false`, e.g. curator, triggered-agents-444) but whose live state doesn't match that canon: both
    ta-<agent>.timer live on the host AND the same-named Orca automation still has its own trigger
    enabled — two independent schedule owners that could both fire the same tick's skill.

    Deliberately scoped to opt-in agents only, not every agent with a live timer: an agent whose
    spec never sets `[trigger] enabled = false` (retro, steward) has made no single-scheduler
    promise in the first place, so its Orca automation being enabled is its unchanged, intended
    state, not drift. Flagging it anyway would report every retro/steward host as perpetually
    out-of-sync for a policy that was never asked of them (triggered-agents-444 review fixup) —
    the blast radius of this check must match the blast radius of the fix, not every agent that
    happens to share the same timer/automation shape.

    `automation_states` comes from `_orca_automation_states`. `None` means the query itself
    failed: every opted-in agent with a live timer gets a "double-schedule-unknown" hit instead of
    being silently skipped — the whole point of this check is to catch curator's automation coming
    back enabled, and a query failure must not read as "confirmed fine" (triggered-agents-444
    review fixup, second round: the first version of this fix let exactly that failure mode go
    quiet). A dict (query succeeded) with an agent's name simply absent means that name has no live
    automation at all — nothing to double-fire from, correctly not flagged."""
    hits = []
    for agent_dir in sorted(provision.AGENTS_DIR.iterdir()):
        spec_path = agent_dir / "automation.toml"
        if not spec_path.is_file():
            continue
        agent = agent_dir.name
        spec = tomllib.loads(spec_path.read_text(encoding="utf-8"))
        if spec.get("dispatcher") or not spec.get("skill"):
            continue  # deterministic agent, no Orca automation at all
        if spec.get("trigger", {}).get("enabled", True):
            continue  # never opted into single-scheduler ownership — nothing to check
        if not (SYSTEMD_DIR / f"ta-{agent}.timer").is_file():
            continue
        if automation_states is None:
            hits.append({
                "unit": f"ta-{agent}.timer + orca:{agent}",
                "kind": "double-schedule-unknown",
                "diff": (
                    f"ta-{agent}.timer is live on the host and '{agent}' opts into "
                    "single-scheduler ownership ([trigger] enabled = false), but querying Orca "
                    "automation state failed — the double-schedule invariant could not be "
                    "confirmed this run. Re-run once Orca is reachable."
                ),
            })
        elif automation_states.get(agent):
            hits.append({
                "unit": f"ta-{agent}.timer + orca:{agent}",
                "kind": "double-schedule",
                "diff": (
                    f"ta-{agent}.timer is live on the host AND the Orca automation '{agent}' "
                    "still has its own trigger enabled, though its spec's [trigger] enabled = "
                    "false — both could fire the same tick's skill. "
                    f"Re-provision (deploy/provision.py {agent}) to reassert --disabled."
                ),
            })
    return hits


def check() -> dict:
    """{"in_sync": bool, "drift": [{"unit", "kind", "diff"}, ...]}. The "unit" field may hold a
    unit filename or an installed script path. `kind`:
      "content" -> artifact exists on both sides but differs (a merged spec/script change never
                   applied to the host; post-merge apply catches this earlier, and this check is
                   the safety net for whatever still slips past it).
      "missing" -> the checkout calls for this artifact, the host has none (never provisioned, or
                   removed by hand without touching the source).
      "extra"   -> the unit lives on the host, no spec calls for it any more (a decommissioned
                   agent/variant whose unit was never torn down, the ta-board class, 2026-07-04).
      "double-schedule" -> ta-<agent>.timer is live AND the same-named Orca automation's own
                   trigger is enabled (triggered-agents-444) — two schedule owners, not just a
                   stale artifact; see _double_schedule_drift.
      "double-schedule-unknown" -> same live-timer/opted-in agent as above, but the Orca
                   automation query itself failed, so the invariant above could not be confirmed
                   this run — never silently read as "fine".
    Sorted by artifact name for a deterministic report, double-schedule hits appended after."""
    expected = _expected_artifacts()
    live = _live_artifacts()
    drift = []
    for unit in sorted(set(expected) | set(live)):
        exp = expected.get(unit)
        got = live.get(unit)
        if exp is None:
            drift.append({"unit": unit, "kind": "extra", "diff": _diff(got, "", unit)})
        elif got is None:
            drift.append({"unit": unit, "kind": "missing", "diff": _diff("", exp, unit)})
        elif exp != got:
            drift.append({"unit": unit, "kind": "content", "diff": _diff(got, exp, unit)})
    drift.extend(_double_schedule_drift(_orca_automation_states()))
    return {"in_sync": not drift, "drift": drift}


def render_markdown(result: dict) -> str:
    if result["in_sync"]:
        return "steward: дрейфа systemd-артефактов нет, живой набор совпадает с текущим рендером.\n"
    lines = [f"# steward: дрейф systemd-артефактов ({len(result['drift'])})", ""]
    for hit in result["drift"]:
        lines.append(f"## {hit['unit']} ({hit['kind']})")
        lines.append("```diff")
        lines.append(hit["diff"].rstrip("\n") or "(пусто)")
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
