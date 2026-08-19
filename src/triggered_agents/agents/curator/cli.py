"""curator agent — deterministic helpers the curator skill drives via Bash.

Flow the agent follows each run:
  1. `python3 -m triggered_agents curator harvest`  -> redacted batch (markdown) of new
                                     Claude/Hermes/Codex session turns and changed
                                     personal-memory files on stdout, and the pending
                                     watermark cached on disk.
  2. agent extracts durable facts, dedups via memory_search, writes each accepted fact
     through `python3 -m triggered_agents curator memory-write`.
  3. `python3 -m triggered_agents curator advance`  -> moves the watermark past step 1.

Two-phase so a crash before the memory commit re-harvests instead of dropping turns.
`harvest --json` emits the structured batch; `sessions` lists discovered sources;
`status` shows the watermark; `precheck` exits PRECHECK_SKIP (100) when there is nothing new, so
the systemd gate can skip the run without spinning up a head.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ...runtime.state import PRECHECK_SKIP, AgentState
from . import discover, harvest
from .memory_protocol import (
    MemoryProtocolError,
    MemoryWriteRequest,
    default_secretary_instance,
    write_fact,
)

STATE = AgentState("curator")


def cmd_harvest(as_json: bool) -> int:
    with STATE.lock():
        batch = harvest.harvest(STATE)
        STATE.ensure_dir()
        STATE.pending_file.write_text(json.dumps(batch["pending"], ensure_ascii=False), encoding="utf-8")
    if as_json:
        print(json.dumps(batch, ensure_ascii=False, indent=2))
    else:
        print(harvest.render_markdown(batch))
    return 0


def cmd_advance() -> int:
    if not STATE.pending_file.is_file():
        print("curator: nothing to advance (run harvest first)", file=sys.stderr)
        return 1
    pending = json.loads(STATE.pending_file.read_text(encoding="utf-8"))
    with STATE.lock():
        harvest.advance(STATE, pending)
        STATE.pending_file.unlink()
    STATE.log_run("advance")
    print(f"curator: watermark advanced for {len(pending)} source(s)")
    return 0


def cmd_precheck() -> int:
    """Exit 0 if there are new turns or memory files to curate, PRECHECK_SKIP (100) to skip a clean
    run when nothing is new. Any other code means precheck crashed. An uncaught exception exits 1,
    which the systemd gate treats as an error, not a skip. See runtime/state.py PRECHECK_SKIP and
    deploy/ta-gate.sh."""
    batch = harvest.harvest(STATE)
    if batch["sessions"] or batch["memory"]:
        STATE.log_run("precheck", result="change")
        return 0
    STATE.log_run("precheck", result="no-change")
    print("curator: no new turns since watermark", file=sys.stderr)
    return PRECHECK_SKIP


def cmd_sessions() -> int:
    for s in discover.all_sessions():
        print(f"{s['head']:8} {s['session_id']}  {s['cwd']}  {s['path']}")
    return 0


def cmd_status() -> int:
    mark = STATE.load_watermark()
    print(f"watermark: {len(mark)} source(s) tracked; state={STATE.dir}")
    for src, v in mark.items():
        if "lines" in v:
            print(f"  {v['lines']:>6} lines  {Path(src).name}")
        else:
            print(f"  {v.get('size', 0):>6} bytes  {Path(src).name}")
    return 0


def cmd_memory_write(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python3 -m triggered_agents curator memory-write")
    parser.add_argument("--instance", default=str(default_secretary_instance()))
    parser.add_argument("--data-dir")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--source")
    parser.add_argument("--tags", default="")
    parser.add_argument("--pinned", action="store_true")
    parser.add_argument("--supersedes", default="")
    parser.add_argument("--secretary-repo")
    ns = parser.parse_args(argv)

    request = MemoryWriteRequest(
        instance=Path(ns.instance),
        actor=ns.actor,
        scope=ns.scope,
        slug=ns.slug,
        fact_file=Path(ns.file),
        source=ns.source,
        tags=ns.tags,
        pinned=ns.pinned,
        supersedes=ns.supersedes,
        data_dir=Path(ns.data_dir) if ns.data_dir else None,
        secretary_repo=Path(ns.secretary_repo) if ns.secretary_repo else None,
    )
    try:
        result = write_fact(STATE, request)
    except MemoryProtocolError as exc:
        STATE.log_run("memory_write", result="error", actor=ns.actor, scope=ns.scope, slug=ns.slug,
                      error=exc.payload.get("error"), commit=exc.payload.get("commit"))
        print(json.dumps(exc.payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    STATE.log_run("memory_write", result="ok", actor=ns.actor, scope=ns.scope, slug=ns.slug,
                  commit=result.get("commit"), fact=result.get("fact"))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
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
    if cmd == "memory-write":
        return cmd_memory_write(argv[1:])
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
