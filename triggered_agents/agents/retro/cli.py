"""retro agent — deterministic helpers the /retro skill drives via Bash.

Retro scans recent head transcripts (harvest reused from the curator) and the memory-mcp
search log for concrete failures — answered from a fact in panelmem WITHOUT a memory_search and
got it wrong, repeated a known mistake, looped without progress, burned a session for nothing.
Output is PROPOSALS only — Идеи cards on the Pipeline board (never Ready, never a merge/push to
any main). All judgment lives in the /retro skill; Python only gathers and redacts.

Flow the agent follows each run:
  1. `python3 -m triggered_agents retro harvest`  -> redacted transcript batch (markdown) plus
                                     the search-log tail for the batch's time window on stdout;
                                     the pending watermark is cached on disk.
  2. agent judges the batch, files each proposal as an Идеи card on the Pipeline board
     (`pipeline --role retro idea ...`) or concludes there is nothing, optionally
     `retro log-proposal --ref <card reference> [--ref <card reference> ...]`.
  3. `python3 -m triggered_agents retro advance`  -> moves the watermark past step 1.

Two-phase like the curator: a crash before the proposals are filed re-harvests instead of
dropping turns. `harvest --json` emits the structured batch; `sessions` lists discovered sources;
`status` shows the watermark; `precheck` exits PRECHECK_SKIP (100) when nothing is new, so the
systemd gate can skip the run without spinning up a head.

Retro's watermark/lock/runs.jsonl live under state/retro, independent of the curator's cursor,
so the two harvest the same sources on separate schedules without clobbering each other.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ...runtime.state import PRECHECK_SKIP, AgentState
from ..curator import discover, harvest
from ..pipeline import ops as pipeline_ops
from . import search_log

STATE = AgentState("retro")


def _batch_window(batch: dict):
    """(min_ts, max_ts) across the batch's turns, for scoping the search-log tail."""
    ts = [t["ts"] for s in batch["sessions"] for t in s["turns"] if t.get("ts")]
    return (min(ts), max(ts)) if ts else (None, None)


def _cleanup_done() -> dict:
    out = pipeline_ops.close_old_done_cards()
    refs = out["closed"]
    STATE.log_run(
        "done-cleanup",
        result="closed" if refs else "no-op",
        closed_count=len(refs),
        references=",".join(refs),
    )
    return {"closed_refs": refs, "closed_count": len(refs)}


def cmd_harvest(as_json: bool) -> int:
    cleanup = _cleanup_done()
    with STATE.lock():
        batch = harvest.harvest(STATE)
        STATE.ensure_dir()
        STATE.pending_file.write_text(json.dumps(batch["pending"], ensure_ascii=False), encoding="utf-8")
    since, until = _batch_window(batch)
    log = search_log.tail(since, until)
    if as_json:
        print(json.dumps(
            {"batch": batch, "search_log": log, "done_cleanup": cleanup},
            ensure_ascii=False, indent=2))
    else:
        print("## Done cleanup")
        if cleanup["closed_refs"]:
            print("Закрыты: " + ", ".join(cleanup["closed_refs"]))
        else:
            print("Нет старых Done-карточек для закрытия")
        print()
        print(harvest.render_markdown(batch))
        print()
        print(search_log.render_markdown(log))
    return 0


def cmd_advance() -> int:
    if not STATE.pending_file.is_file():
        print("retro: nothing to advance (run harvest first)", file=sys.stderr)
        return 1
    pending = json.loads(STATE.pending_file.read_text(encoding="utf-8"))
    with STATE.lock():
        harvest.advance(STATE, pending)
        STATE.pending_file.unlink()
    STATE.log_run("advance")
    print(f"retro: watermark advanced for {len(pending)} source(s)")
    return 0


def cmd_precheck() -> int:
    """Exit 0 if there are new turns to review, PRECHECK_SKIP (100) to skip a clean run when nothing
    is new. Any other code means precheck crashed. An uncaught exception exits 1, which the systemd
    gate treats as an error, not a skip. See runtime/state.py PRECHECK_SKIP and deploy/ta-gate.sh."""
    _cleanup_done()
    batch = harvest.harvest(STATE)
    if batch["sessions"]:
        STATE.log_run("precheck", result="change")
        return 0
    STATE.log_run("precheck", result="no-change")
    print("retro: no new turns since watermark", file=sys.stderr)
    return PRECHECK_SKIP


def cmd_log_proposal(refs: list[str]) -> int:
    """Record that this run filed proposal card(s) (board references) in runs.jsonl."""
    STATE.log_run("proposal", refs=",".join(refs))
    print(f"retro: proposal logged ({', '.join(refs)})")
    return 0


def cmd_sessions() -> int:
    for s in discover.all_sessions():
        print(f"{s['head']:8} {s['session_id'][:8]}  {s['cwd']}")
    return 0


def cmd_status() -> int:
    mark = STATE.load_watermark()
    print(f"watermark: {len(mark)} source(s) tracked; state={STATE.dir}")
    for src, v in mark.items():
        print(f"  {v.get('lines', 0):>6} lines  {Path(src).name}")
    return 0


def main(argv=None) -> int:
    argv = list(argv or [])
    cmd = argv[0] if argv else "help"
    if cmd == "harvest":
        return cmd_harvest("--json" in argv)
    if cmd == "advance":
        return cmd_advance()
    if cmd == "precheck":
        return cmd_precheck()
    if cmd == "sessions":
        return cmd_sessions()
    if cmd == "status":
        return cmd_status()
    if cmd == "log-proposal":
        import argparse

        p = argparse.ArgumentParser(prog="triggered_agents retro log-proposal")
        p.add_argument("--ref", required=True, action="append",
                       help="board card reference filed this run (repeatable)")
        ns = p.parse_args(argv[1:])
        return cmd_log_proposal(ns.ref)
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
