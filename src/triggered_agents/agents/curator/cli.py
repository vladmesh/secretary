"""curator agent — deterministic helpers the curator skill drives via Bash.

Flow the agent follows each run:
  1. `python3 -m triggered_agents curator harvest`  -> redacted batch (markdown) of new
                                     Claude/Hermes/Codex session turns and changed
                                     personal-memory files on stdout, and a fact-bearing,
                                     identity-bound pending batch cached on disk. Cursor-only
                                     scans advance before the command returns.
  2. agent extracts durable facts, dedups via memory_search, writes each accepted fact
     through `python3 -m triggered_agents curator memory-write`.
  3. `python3 -m triggered_agents curator advance`  -> moves the watermark past step 1.

Two-phase so a crash before the memory commit re-harvests instead of dropping turns.
`harvest --json` emits the structured batch; `sessions` lists discovered sources;
`status` shows the watermark; `precheck` exits PRECHECK_SKIP (100) when there is nothing new, so
the systemd gate can skip the run without spinning up a head.
"""

from __future__ import annotations

import fcntl
import json
import sys
from contextlib import contextmanager
from pathlib import Path

from ...runtime.state import PRECHECK_DEFERRED, PRECHECK_SKIP, AgentState
from . import discover, harvest
from .memory_protocol import (
    MemoryProtocolError,
    MemoryWriteRequest,
    default_secretary_instance,
    write_fact,
)

STATE = AgentState("curator")


class SettlementDeferred(RuntimeError):
    """Another process owns the short curator cursor-settlement transaction."""


@contextmanager
def cursor_settlement_transaction(*, nonblocking: bool = False):
    """Serialize curator watermark/pending read-validate-write without owning a run lifecycle.

    flock ownership is released by Linux when the owning process exits, including a killed curator
    head.  The file is intentionally persistent: it names the transaction boundary, not its owner.
    """
    STATE.ensure_dir()
    lock_path = STATE.dir / "cursor-settlement.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    locked = False
    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise SettlementDeferred from exc
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _write_pending(record: dict) -> None:
    """Atomically publish the fact-bearing batch that a later advance consumes."""
    STATE.ensure_dir()
    tmp = STATE.pending_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE.pending_file)


def _prepare_batch(*, nonblocking: bool = False) -> dict:
    """Replay fact work or atomically settle a fresh bounded scan in one transaction.

    Every complete source record the scan classified leaves this function either in an
    identity-bound fact batch or in the watermark.  A cursor-only pending record is not
    a supported batch because no curator step can consume it, so it fails closed.
    """
    with cursor_settlement_transaction(nonblocking=nonblocking):
        identity = harvest.current_identity()
        if STATE.pending_file.is_file():
            record = harvest.read_pending(STATE, identity)
            # A stale record must win over new discovery, but cannot be replayed or
            # advanced as though it belonged to the current watermark.
            for source, expected in record["base"].items():
                if STATE.load_watermark().get(source) != expected:
                    raise harvest.PendingError(f"curator pending record is stale for {source}")
            return {**record["batch"], "batch_id": record["batch_id"]}

        batch = harvest.harvest(STATE, identity)
        if batch["sessions"] or batch["memory"]:
            base = {key: STATE.load_watermark().get(key) for key in batch["pending"]}
            _write_pending(harvest.pending_record(batch, identity, base))
        elif batch["pending"]:
            # These are complete non-emitting records.  Persist their precise scan
            # cursor now, not as a pending batch that the zero-input skill will exit
            # without advancing.
            base = {key: STATE.load_watermark().get(key) for key in batch["pending"]}
            harvest.advance(STATE, harvest.pending_record(batch, identity, base), identity)
        return batch


def cmd_harvest(as_json: bool) -> int:
    try:
        batch = _prepare_batch()
    except harvest.PendingError as exc:
        print(f"curator: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(batch, ensure_ascii=False, indent=2))
    else:
        print(harvest.render_markdown(batch))
    return 0


def cmd_advance() -> int:
    try:
        with cursor_settlement_transaction():
            if not STATE.pending_file.is_file():
                raise harvest.PendingError("nothing to advance (run harvest first)")
            identity = harvest.current_identity()
            pending = harvest.read_pending(STATE, identity)
            harvest.advance(STATE, pending, identity)
            STATE.pending_file.unlink()
    except harvest.PendingError as exc:
        print(f"curator: {exc}", file=sys.stderr)
        return 1
    STATE.log_run("advance")
    print(f"curator: watermark advanced for {len(pending['batch']['pending'])} source(s)")
    return 0


def cmd_precheck() -> int:
    """Return work, clean-skip, or an unclaimed settlement defer for the systemd gate."""
    try:
        batch = _prepare_batch(nonblocking=True)
    except SettlementDeferred:
        print("curator: cursor settlement is busy; tick deferred", file=sys.stderr)
        return PRECHECK_DEFERRED
    except harvest.PendingError as exc:
        print(f"curator: {exc}", file=sys.stderr)
        return 1
    if batch["sessions"] or batch["memory"]:
        STATE.log_run("precheck", result="change")
        return 0
    if batch.get("partial_sources"):
        STATE.log_run("precheck", result="source-local-partial")
        print("curator: source-local partial JSONL tail; safe cursors settled", file=sys.stderr)
        return PRECHECK_SKIP
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
        elif "offset" in v:
            print(f"  {v['offset']:>6} bytes  {Path(src).name}")
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
        STATE.log_run(
            "memory_write",
            result="error",
            actor=ns.actor,
            scope=ns.scope,
            slug=ns.slug,
            error=exc.payload.get("error"),
            commit=exc.payload.get("commit"),
        )
        print(json.dumps(exc.payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    STATE.log_run(
        "memory_write",
        result="ok",
        actor=ns.actor,
        scope=ns.scope,
        slug=ns.slug,
        commit=result.get("commit"),
        fact=result.get("fact"),
    )
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
