"""retro agent — deterministic helpers the /retro skill drives via Bash.

Retro scans recent head transcripts (harvest reused from the curator) and the memory-mcp
search log for concrete failures — answered from a fact in the memory canon WITHOUT a memory_search and
got it wrong, repeated a known mistake, looped without progress, burned a session for nothing.
Output is PROPOSALS only — cards in the Pipeline board's Issues column (never Ready, never a
merge/push to any main). All judgment lives in the /retro skill; Python only gathers and redacts.

Flow the agent follows each run:
  1. `python3 -P -m secretary automations retro harvest`  -> redacted transcript batch (markdown) plus
                                     the search-log tail for the batch's time window on stdout;
                                     the pending watermark is cached on disk.
  2. agent judges the batch, files each proposal as an Issues card on the Pipeline board
     (`pipeline --role retro idea ...`) or concludes there is nothing, optionally
     `retro log-proposal --ref <card reference> [--ref <card reference> ...]`.
  3. `python3 -P -m secretary automations retro advance`  -> moves the watermark past step 1.

Two-phase like the curator, and through the curator's own API: harvest publishes the versioned,
identity-bound pending record (`harvest.pending_record` / `harvest.write_pending`), advance reads it
with `harvest.read_pending`, so a crash before the proposals are filed replays the same batch instead
of dropping turns. A scan with no turns to judge settles its cursors at once and leaves nothing
pending. A flat pre-version pending file (only an old retro harvest wrote one) is renamed aside to
`pending.legacy-<UTC>.json` with a runs.jsonl warning and its sources are simply harvested again; any
other refused pending record fails closed. `harvest --json` emits the structured batch; `sessions` lists discovered sources;
`status` shows the watermark; `precheck` exits PRECHECK_SKIP (100) when nothing is new, so the
systemd gate can skip the run without spinning up a head.

Retro's watermark/lock/runs.jsonl live under state/retro, independent of the curator's cursor,
so the two harvest the same sources on separate schedules without clobbering each other.
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from secretary.runtime.state import PRECHECK_BOARD_UNREACHABLE, PRECHECK_SKIP, AgentState, BoardUnavailable

from ..curator import discover, harvest
from . import search_log

STATE = AgentState("retro")


class DoneRetention(Protocol):
    """The one destructive board operation retro may request during cleanup."""

    def close_old_done(self) -> dict[str, Any]: ...


def _batch_window(batch: dict):
    """(min_ts, max_ts) across the batch's turns, for scoping the search-log tail."""
    ts = [t["ts"] for s in batch["sessions"] for t in s["turns"] if t.get("ts")]
    return (min(ts), max(ts)) if ts else (None, None)


def _cleanup_done(retention: DoneRetention | None = None) -> dict:
    if retention is None:
        raise RuntimeError("retro Done-retention board must be supplied by the composition root")
    out = retention.close_old_done()
    refs = out["closed"]
    STATE.log_run(
        "done-cleanup",
        result="closed" if refs else "no-op",
        closed_count=len(refs),
        references=",".join(refs),
    )
    return {"closed_refs": refs, "closed_count": len(refs)}


def _load_pending(identity: dict) -> tuple[dict, dict]:
    """The one place retro loads its pending record and interprets it, as (record, replay batch).

    The shared reader validates the batch shape; this is the backstop behind it. Any failure while
    reading the record or deriving what the commands use from it (the batch window, the rendered
    markdown) is a PendingError, so nothing in pending.json can escape a command as a traceback.
    """
    try:
        record = harvest.read_pending(STATE, identity)
        batch = {**copy.deepcopy(record["batch"]), "batch_id": record["batch_id"]}
        _batch_window(batch)
        harvest.render_markdown(batch)
    except harvest.PendingError:
        raise
    except Exception as exc:
        raise harvest.PendingError("curator pending record has an invalid batch") from exc
    return record, batch


def _legacy_pending(identity: dict) -> bool:
    """Whether the pending file is there and refused only for not being a versioned record."""
    if not STATE.pending_file.is_file():
        return False
    try:
        _load_pending(identity)
    except harvest.LegacyPendingError:
        return True
    except harvest.PendingError:
        # Unreadable, foreign identity, invalid batch: the caller's own read fails closed on it.
        return False
    return False


def _set_aside_legacy_pending(identity: dict) -> None:
    """Rename a legacy flat pending file aside, never delete it, and harvest its sources again.

    Its sources were never advanced past, so re-reading them loses nothing; the retro skill checks
    existing Issues before it proposes, so a re-reviewed turn does not breed a duplicate card.
    """
    if not _legacy_pending(identity):
        return
    with STATE.lock():
        if not _legacy_pending(identity):
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = STATE.pending_file.with_name(f"pending.legacy-{stamp}.json")
        suffix = 1
        while target.exists():
            target = STATE.pending_file.with_name(f"pending.legacy-{stamp}-{suffix}.json")
            suffix += 1
        try:
            STATE.pending_file.rename(target)
        except OSError as exc:
            raise harvest.PendingError(f"retro could not set the legacy pending record aside: {exc}") from exc
    STATE.log_run("pending-set-aside", level="warning", reason="legacy", file=str(target))
    print(f"retro: warning: legacy pending record set aside as {target}; its sources are harvested again", file=sys.stderr)


def _harvest_batch(identity: dict) -> dict:
    """Replay the pending record, or publish a fresh fact-bearing one; the caller holds the lock."""
    if STATE.pending_file.is_file():
        return _load_pending(identity)[1]
    batch = harvest.harvest(STATE, identity)
    base = {key: STATE.load_watermark().get(key) for key in batch["pending"]}
    record = harvest.pending_record(batch, identity, base)
    if batch["sessions"] or batch["memory"]:
        harvest.write_pending(STATE, record)
    elif batch["pending"]:
        # Nothing to judge, only scan cursors: settle them now. A record with no fact-bearing
        # input is refused by read_pending, so publishing one would wedge the next tick.
        harvest.advance(STATE, record, identity)
    return batch


def cmd_harvest(as_json: bool, retention: DoneRetention | None = None) -> int:
    try:
        identity = harvest.current_identity()
        _set_aside_legacy_pending(identity)
        cleanup = _cleanup_done(retention)
        with STATE.lock():
            batch = _harvest_batch(identity)
    except harvest.PendingError as exc:
        print(f"retro: {exc}", file=sys.stderr)
        return 1
    since, until = _batch_window(batch)
    log = search_log.tail(since, until)
    if as_json:
        print(
            json.dumps(
                {"batch": batch, "search_log": log, "done_cleanup": cleanup}, ensure_ascii=False, indent=2
            )
        )
    else:
        print("## Done cleanup")
        if cleanup["closed_refs"]:
            print("Closed: " + ", ".join(cleanup["closed_refs"]))
        else:
            print("No old Done cards to close")
        print()
        print(harvest.render_markdown(batch))
        print()
        print(search_log.render_markdown(log))
    return 0


def cmd_advance() -> int:
    try:
        identity = harvest.current_identity()
        _set_aside_legacy_pending(identity)
        with STATE.lock():
            if not STATE.pending_file.is_file():
                # A harvest with no turns to judge already settled its cursors.
                print("retro: nothing pending to advance", file=sys.stderr)
                return 0
            record, _ = _load_pending(identity)
            harvest.advance(STATE, record, identity)
            STATE.pending_file.unlink()
    except harvest.PendingError as exc:
        print(f"retro: {exc}", file=sys.stderr)
        return 1
    STATE.log_run("advance")
    print(f"retro: watermark advanced for {len(record['batch']['pending'])} source(s)")
    return 0


def cmd_precheck(retention: DoneRetention | None = None) -> int:
    """Exit 0 if there are new turns to review, PRECHECK_SKIP (100) to skip a clean run when nothing
    is new, PRECHECK_BOARD_UNREACHABLE (101) when the board never answered, so the gate re-attempts
    the run instead of spending it. Any other code means precheck crashed. An uncaught exception
    exits 1, which the systemd gate treats as an error, not a skip. See secretary/runtime/state.py
    PRECHECK_SKIP and scripts/secretary-agent-gate.sh."""
    try:
        identity = harvest.current_identity()
        _set_aside_legacy_pending(identity)
        _cleanup_done(retention)
        batch = _load_pending(identity)[1] if STATE.pending_file.is_file() else harvest.harvest(STATE, identity)
    except BoardUnavailable as e:
        # Not retro's failure and not a clean tick: the day's run has not happened yet. Logged so
        # the loss is visible in runs.jsonl instead of only as a stale "last healthy tick".
        STATE.log_run("precheck", result="board-unreachable", error=str(e))
        print(f"retro: board unreachable, run deferred: {e}", file=sys.stderr)
        return PRECHECK_BOARD_UNREACHABLE
    except harvest.PendingError as e:
        # A pending record refused for any reason but being legacy stays for the operator; exit 1
        # is the gate's error branch, not a skip.
        STATE.log_run("precheck", result="pending-refused", error=str(e))
        print(f"retro: {e}", file=sys.stderr)
        return 1
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


def main(argv=None, *, retention: DoneRetention | None = None) -> int:
    argv = list(argv or [])
    cmd = argv[0] if argv else "help"
    if cmd == "harvest":
        return cmd_harvest("--json" in argv, retention)
    if cmd == "advance":
        return cmd_advance()
    if cmd == "precheck":
        return cmd_precheck(retention)
    if cmd == "sessions":
        return cmd_sessions()
    if cmd == "status":
        return cmd_status()
    if cmd == "log-proposal":
        import argparse

        p = argparse.ArgumentParser(prog="secretary automations retro log-proposal")
        p.add_argument(
            "--ref", required=True, action="append", help="board card reference filed this run (repeatable)"
        )
        ns = p.parse_args(argv[1:])
        return cmd_log_proposal(ns.ref)
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
