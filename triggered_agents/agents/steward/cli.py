"""steward agent — deterministic anomaly signals the `/steward` skill drives via Bash.

Flow the agent follows each run:
  1. `python3 -m triggered_agents steward scan`  -> JSON batch of anomaly signals since the
     watermark (see signals.py for the five kinds). Nothing here judges or writes anything —
     that is entirely the skill's job, same split as curator/retro.
  2. agent investigates by its own judgment (transcripts, workspaces, repos, curator/retro
     output — everything is readable), fixes what blocks the pipeline right now (infra/
     control-panel, direct commit), files cards for the rest, escalates what it cannot resolve
     to Blocked with a writeup, comments on every card it touched, and posts the run's report as
     a comment on the wake-up report card the dispatcher created for this run (triggered-
     agents-255/259 — no more markdown file in control-panel/docs/steward/).
  3. `python3 -m triggered_agents steward advance`  -> folds the scanned state into the
     watermark, so a condition that hasn't changed does not re-spawn the head next hour.

Two-phase like curator/retro: a crash before advance re-scans instead of dropping a signal.
`scan --json` emits the structured batch; `precheck` exits PRECHECK_SKIP (100) when there is no
signal, so the systemd gate can skip the run at zero LLM cost; `status` shows the watermark.

Second, unconditional mode (triggered-agents-254): `deep-sweep-since`/`deep-sweep-advance` keep
a separate watermark — just a timestamp, not a per-kind dedup like signals.py's — for the daily
whole-system audit that runs with no precheck gate at all (the second
secretary-steward-deep-sweep timer). Kept independent on purpose: the signal gate must not swallow an
anomaly the daily sweep hasn't looked at yet, and vice versa.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ...runtime.redact import scrub_secrets
from ...runtime.state import PRECHECK_SKIP
from . import signals

STATE = signals.STATE
ROLE_SKILLS = Path(__file__).resolve().parents[3] / "scripts" / "role_skills.py"


def _deep_sweep_file():
    """Recomputed on every call (not a module-level constant) so a test that patches `STATE`
    (or a variant timer resolving a different agent's state dir) sees it follow, same reasoning
    as production_telemetry.state_path()."""
    return STATE.dir / "deep_sweep_watermark.json"


def cmd_scan(as_json: bool) -> int:
    with STATE.lock():
        batch = signals.scan()
        STATE.ensure_dir()
        STATE.pending_file.write_text(json.dumps(batch["pending"], ensure_ascii=False), encoding="utf-8")
    signals.ensure_pipeline_baseline(batch)  # outside the lock above — it takes the lock itself
    if as_json:
        print(json.dumps(batch, ensure_ascii=False, indent=2))
    else:
        print(signals.render_markdown(batch))
    return 0


def cmd_advance() -> int:
    if not STATE.pending_file.is_file():
        print("steward: nothing to advance (run scan first)", file=sys.stderr)
        return 1
    pending = json.loads(STATE.pending_file.read_text(encoding="utf-8"))
    with STATE.lock():
        # Fold in currently-Blocked refs fresh, not just the scan-time snapshot: the skill's own
        # action phase (after scan(), before advance()) may have escalated a brand new card to
        # Blocked — without this it would look "new" again on the very next hour's scan, one
        # wasted wake-up for a card this same run just put there (2026-07-04 review,
        # triggered-agents-244 note Z2).
        current_blocked = {c["reference"] for c in signals.pipeline_ops.list_cards(column="Blocked")
                           if c.get("steward_report") != "1"}
        pending["notified_blocked"] = sorted(set(pending["notified_blocked"]) | current_blocked)
        STATE.save_watermark(pending)
        STATE.pending_file.unlink()
    STATE.log_run("advance")
    print("steward: watermark advanced")
    return 0


def cmd_precheck() -> int:
    """Exit 0 if any anomaly signal is present, PRECHECK_SKIP (100) to skip a clean run, 2 when
    precheck itself broke (Kanboard unreachable, bad env, any other exception). Both the caught error
    (2) and any uncaught crash (Python exits 1) fall in the gate's "anything but 0/100" error branch,
    so a dead precheck fails the unit instead of reading as a quiet hour. This removes the old
    1-means-skip masking. See runtime/state.py PRECHECK_SKIP and deploy/ta-gate.sh."""
    try:
        batch = signals.scan()
    except Exception as e:  # noqa: BLE001 — any precheck failure must be logged, not just KanboardError
        scrubbed = scrub_secrets(str(e))
        STATE.log_run("precheck", result="error", error_class=type(e).__name__, error=scrubbed)
        print(f"steward: precheck failed ({type(e).__name__}): {scrubbed}", file=sys.stderr)
        return 2
    # A skipped hour writes nothing else, so the pipeline baseline this scan took would be lost
    # and re-taken on every later failure, suppressing it (see signals.ensure_pipeline_baseline).
    signals.ensure_pipeline_baseline(batch)
    if signals.has_signal(batch):
        counts = {k: (len(v) if isinstance(v, (list, dict)) else v)
                 for k, v in batch["signals"].items()}
        STATE.log_run("precheck", result="change", **counts)
        return 0
    STATE.log_run("precheck", result="no-change")
    print("steward: no anomaly signal since watermark", file=sys.stderr)
    return PRECHECK_SKIP


def cmd_status() -> int:
    mark = signals.load_watermark()
    print(json.dumps(mark, ensure_ascii=False, indent=2))
    return 0


def cmd_deep_sweep_since() -> int:
    """Print the last unconditional-sweep timestamp (or null on a first-ever run) as JSON — the
    window the deep-sweep skill section should look back over. A malformed file (never expected,
    but the signal watermark has the same defensive read) is treated as null rather than raised,
    so a corrupt file degrades to "look back further", not a crashed run."""
    path = _deep_sweep_file()
    last = None
    if path.is_file():
        try:
            last = json.loads(path.read_text(encoding="utf-8")).get("last_run")
        except json.JSONDecodeError:
            last = None
    print(json.dumps({"last_deep_sweep": last}, ensure_ascii=False))
    return 0


def _render_role_skills_markdown(result: dict) -> str:
    if "error" in result:
        return f"role skills: error\n\n{result['error']}"
    lines = [f"role skills: {'ok' if result.get('ok') else 'drift'}", ""]
    for target, stats in sorted(result.get("targets", {}).items()):
        lines.append(
            f"- {target} ({stats['shell']}): expected={stats['expected']}, "
            f"missing={stats['missing']}, drift={stats['drift']}, source_missing={stats['source_missing']}"
        )
    for title, key in (("Missing", "missing"), ("Drift", "drift"), ("Source missing", "source_missing")):
        if result.get(key):
            lines.extend(["", f"{title}:"])
            for item in result[key]:
                lines.append(f"- {item['target']} {item['role']}/{item['skill']} -> {item['dest']}")
    return "\n".join(lines)


def cmd_role_skills(as_json: bool) -> int:
    """Deep-sweep-only helper: role-owned skills from control-panel vs. shell-owned copies.
    Always exits 0 like drift(): `ok: false` is a signal for the skill, not a crashed steward."""
    try:
        proc = subprocess.run(
            [sys.executable, str(ROLE_SKILLS), "audit", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode not in (0, 1):
            result = {
                "ok": False,
                "error": proc.stderr.strip() or proc.stdout.strip() or f"role_skills exited {proc.returncode}",
            }
        else:
            result = json.loads(proc.stdout)
    except Exception as e:  # noqa: BLE001 - report helper failure as steward data, not a crash
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_render_role_skills_markdown(result))
    return 0


def cmd_deep_sweep_advance() -> int:
    """Stamp now() as the last unconditional sweep. Independent of `advance` above (signals.py's
    per-kind watermark) on purpose — see module docstring."""
    now = datetime.now(timezone.utc).isoformat()
    with STATE.lock():
        STATE.ensure_dir()
        _deep_sweep_file().write_text(json.dumps({"last_run": now}, ensure_ascii=False), encoding="utf-8")
    STATE.log_run("deep-sweep-advance", last_run=now)
    print(f"steward: deep-sweep watermark advanced to {now}")
    return 0


def main(argv=None) -> int:
    argv = list(argv or [])
    cmd = argv[0] if argv else "help"
    if cmd == "scan":
        return cmd_scan("--json" in argv)
    if cmd == "advance":
        return cmd_advance()
    if cmd == "precheck":
        return cmd_precheck()
    if cmd == "status":
        return cmd_status()
    if cmd == "deep-sweep-since":
        return cmd_deep_sweep_since()
    if cmd == "deep-sweep-advance":
        return cmd_deep_sweep_advance()
    if cmd == "role-skills":
        return cmd_role_skills("--json" in argv)
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
