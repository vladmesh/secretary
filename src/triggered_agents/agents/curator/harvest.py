"""Bounded curator batches with identity-bound two-phase advancement.

Limits: TA_CURATOR_MAX_TURNS (200), TA_CURATOR_MAX_INPUT_BYTES (262144),
TA_CURATOR_MAX_SOURCES (32), TA_CURATOR_MAX_ROWS_PER_SOURCE (512),
TA_CURATOR_MAX_RECORD_BYTES (65536), TA_CURATOR_MAX_MEMORY_BYTES (65536).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from ...runtime.redact import redact
from . import discover

PENDING_VERSION = 2
_SCAFFOLD = re.compile(r"<(local-command|command-name|command-message|command-args)[\s>]")
_CODEX_CONTEXT_PREFIXES = ("# AGENTS.md instructions for ", "<environment_context>")


class PendingError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    max_turns: int
    max_input_bytes: int
    max_sources: int
    max_rows_per_source: int
    max_record_bytes: int
    max_memory_bytes: int

    @classmethod
    def from_env(cls) -> "Limits":
        def get(name, default):
            try:
                value = int(os.environ.get(name, default))
            except ValueError as exc:
                raise ValueError(f"{name} must be a positive integer") from exc
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            return value

        return cls(
            get("TA_CURATOR_MAX_TURNS", 200),
            get("TA_CURATOR_MAX_INPUT_BYTES", 262144),
            get("TA_CURATOR_MAX_SOURCES", 32),
            get("TA_CURATOR_MAX_ROWS_PER_SOURCE", 512),
            get("TA_CURATOR_MAX_RECORD_BYTES", 65536),
            get("TA_CURATOR_MAX_MEMORY_BYTES", 65536),
        )


@dataclass
class _Budget:
    limits: Limits
    turns: int = 0
    bytes: int = 0
    sources: int = 0

    def accepts(self, turns, size, new_source):
        return (
            self.turns + turns <= self.limits.max_turns
            and self.bytes + size <= self.limits.max_input_bytes
            and self.sources + int(new_source) <= self.limits.max_sources
        )

    def add(self, turns, size, new_source):
        self.turns += turns
        self.bytes += size
        self.sources += int(new_source)


def current_identity() -> dict[str, str]:
    workspace = Path(os.environ.get("TA_CURATOR_WORKSPACE") or Path.cwd()).resolve(strict=False)
    result = {"workspace": str(workspace)}
    for env, key in (("TA_CURATOR_RUN_ID", "run_id"), ("TA_CURATOR_SESSION_ID", "session_id")):
        if value := os.environ.get(env):
            result[key] = value
    return result


def _text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return ""


def parse_claude_lines(lines) -> list[dict]:
    turns = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") not in ("user", "assistant") or row.get("isMeta") or row.get("isSidechain"):
            continue
        message = row.get("message") or {}
        text = _text_from_content(message.get("content")).strip()
        if text and not _SCAFFOLD.search(text):
            turns.append({"role": message.get("role", row["type"]), "text": text, "ts": row.get("timestamp")})
    return turns


def _text_from_codex_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text")
        ).strip()
    return ""


def parse_codex_lines(lines) -> list[dict]:
    turns = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = row.get("payload") or {}
        if row.get("type") != "response_item" or payload.get("type") != "message":
            continue
        role = payload.get("role")
        text = _text_from_codex_content(payload.get("content")).strip()
        if role in ("user", "assistant") and text and not _SCAFFOLD.search(text) and not text.startswith(_CODEX_CONTEXT_PREFIXES):
            turns.append({"role": role, "text": text, "ts": row.get("timestamp")})
    return turns


def _iso_ts(value):
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat() if value else None
    except (TypeError, ValueError, OSError):
        return None


def parse_hermes_rows(rows) -> list[dict]:
    turns = []
    for row in rows:
        text = (row.get("content") or "").strip()
        if row.get("role") in ("user", "assistant") and text and not _SCAFFOLD.search(text):
            turns.append({"role": row["role"], "text": text, "ts": _iso_ts(row.get("timestamp"))})
    return turns


def _legacy_start(path, previous, stat, limits):
    if "offset" in previous:
        offset = previous.get("offset")
        return offset if isinstance(offset, int) and 0 <= offset <= stat.st_size else 0
    count = previous.get("lines")
    if not isinstance(count, int) or count <= 0:
        return 0
    # Legacy line-watermarks are the supported disabled-installation input. Do not
    # discard them or treat them as a new baseline.
    if previous.get("mtime") == stat.st_mtime and previous.get("size") == stat.st_size:
        return None
    with path.open("rb") as fh:
        for _ in range(count):
            data = fh.readline(limits.max_record_bytes + 1)
            while data and not data.endswith(b"\n"):
                data = fh.readline(limits.max_record_bytes + 1)
        return fh.tell()


def _read_record(fh, limits):
    data = fh.readline(limits.max_record_bytes + 1)
    if not data:
        return fh.tell(), None, False
    if not data.endswith(b"\n"):
        # Discard a complete oversized record without materializing it.  An EOF
        # before its newline is a live incomplete record, which must remain for
        # the next harvest rather than becoming part of the cursor.
        while data and not data.endswith(b"\n"):
            data = fh.readline(limits.max_record_bytes + 1)
        return fh.tell(), None, bool(data)
    return fh.tell(), data, False


def _jsonl_source(sess, mark, parser: Callable[[list[str]], list[dict]], budget):
    path = Path(sess["path"])
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None, None, False
    start = _legacy_start(path, mark.get(sess["path"], {}), stat, budget.limits)
    if start is None:
        return None, None, False
    turns, last = [], None
    with path.open("rb") as fh:
        fh.seek(start)
        for _ in range(budget.limits.max_rows_per_source):
            end, data, oversized = _read_record(fh, budget.limits)
            if data is None:
                if not oversized:
                    break
                # This complete record is deliberately rejected for size, so it
                # is safe to scan through even when no turn is emitted.
                last = end
                continue
            parsed = parser([data.decode("utf-8", errors="replace")])
            if not parsed:
                # Parsed noise has no curator signal.  Persist the scan cursor
                # so a row-limited run cannot stall before a later real turn.
                last = end
                continue
            size = len(data)
            if not budget.accepts(len(parsed), size, not turns):
                cursor = {"offset": last, "mtime": stat.st_mtime, "size": stat.st_size} if last is not None else None
                return ({**sess, "turns": turns} if turns else None), cursor, True
            for turn in parsed:
                turn["text"] = redact(turn["text"])
            budget.add(len(parsed), size, not turns)
            turns.extend(parsed)
            last = end
    cursor = {"offset": last, "mtime": stat.st_mtime, "size": stat.st_size} if last is not None else None
    return ({**sess, "turns": turns} if turns else None), cursor, False


def _jsonl_sessions(mark, sessions, parser, budget):
    output, pending = [], {}
    for sess in sorted(sessions, key=lambda s: (s["head"], s["path"], s["session_id"])):
        entry, cursor, stopped = _jsonl_source(sess, mark, parser, budget)
        if entry:
            output.append(entry)
        if cursor:
            pending[sess["path"]] = cursor
        if stopped:
            return output, pending, True
    return output, pending, False


def _hermes_sessions(mark, budget):
    output, pending = [], {}
    for sess in sorted(discover.hermes_sessions(), key=lambda s: (s["head"], s["path"], s["session_id"])):
        key = f"hermes:{sess['session_id']}"
        rows = discover.hermes_messages(
            sess["session_id"], mark.get(key, {}).get("last_id", 0), budget.limits.max_rows_per_source,
            budget.limits.max_record_bytes,
        )
        turns, last = [], None
        for row in rows:
            parsed = parse_hermes_rows([row])
            if not parsed:
                # Tool, empty, and byte-capped rows are complete database rows
                # with no signal, so scanning through them cannot drop a turn.
                last = row["id"]
                continue
            size = row.get("content_bytes", len((row.get("content") or "").encode()))
            if not budget.accepts(len(parsed), size, not turns):
                if turns:
                    output.append({**sess, "turns": turns})
                    pending[key] = {"last_id": last}
                return output, pending, True
            for turn in parsed:
                turn["text"] = redact(turn["text"])
            budget.add(len(parsed), size, not turns)
            turns.extend(parsed)
            last = row["id"]
        if turns:
            output.append({**sess, "turns": turns})
        if last is not None:
            pending[key] = {"last_id": last}
    return output, pending, False


def harvest_memory_files(mark, budget):
    entries, pending, rejected = [], {}, []
    for mem in sorted(discover.all_memory_files(), key=lambda m: (m["head"], m["path"])):
        path = Path(mem["path"])
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        previous = mark.get(mem["path"], {})
        if previous.get("mtime") == stat.st_mtime and previous.get("size") == stat.st_size:
            continue
        if stat.st_size > budget.limits.max_memory_bytes:
            rejected.append({"head": mem["head"], "path": str(path), "reason": "memory-file-too-large"})
            continue
        if not budget.accepts(1, stat.st_size, True):
            return entries, pending, rejected, True
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text.encode()) > budget.limits.max_memory_bytes:
            rejected.append({"head": mem["head"], "path": str(path), "reason": "memory-file-too-large"})
            continue
        budget.add(1, stat.st_size, True)
        entries.append({**mem, "text": redact(text)})
        pending[mem["path"]] = {"mtime": stat.st_mtime, "size": stat.st_size}
    return entries, pending, rejected, False


def _batch_id(identity, batch, base):
    payload = json.dumps({"identity": identity, "batch": batch, "base": base}, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def pending_record(batch, identity, base):
    payload = copy.deepcopy(batch)
    payload.pop("batch_id", None)
    return {
        "version": PENDING_VERSION,
        "identity": copy.deepcopy(identity),
        "base": copy.deepcopy(base),
        "batch": payload,
        "batch_id": _batch_id(identity, payload, base),
    }


def read_pending(st, identity=None):
    identity = identity or current_identity()
    try:
        record = json.loads(st.pending_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PendingError("curator pending record is unreadable") from exc
    if not isinstance(record, dict) or record.get("version") != PENDING_VERSION:
        raise PendingError("curator pending record is legacy or unsupported; preserve it and resolve it manually")
    if record.get("identity") != identity:
        raise PendingError("curator pending record belongs to a different run identity")
    batch, base = record.get("batch"), record.get("base")
    if not isinstance(batch, dict) or not isinstance(base, dict) or not isinstance(batch.get("pending"), dict):
        raise PendingError("curator pending record has an invalid batch")
    if record.get("batch_id") != _batch_id(identity, batch, base):
        raise PendingError("curator pending record identity does not match its contents")
    if not batch.get("sessions") and not batch.get("memory"):
        raise PendingError("curator pending record has no fact-bearing input")
    return record


def harvest(st, identity=None, limits=None):
    identity = identity or current_identity()
    if st.pending_file.is_file():
        record = read_pending(st, identity)
        return {**copy.deepcopy(record["batch"]), "batch_id": record["batch_id"]}
    mark, budget = st.load_watermark(), _Budget(limits or Limits.from_env())
    claude, cp, stopped = _jsonl_sessions(mark, discover.claude_sessions(), parse_claude_lines, budget)
    codex, xp, xstop = ([], {}, True) if stopped else _jsonl_sessions(mark, discover.codex_sessions(), parse_codex_lines, budget)
    hermes, hp, hstop = ([], {}, True) if stopped or xstop else _hermes_sessions(mark, budget)
    memory, mp, rejected, _ = ([], {}, [], True) if stopped or xstop or hstop else harvest_memory_files(mark, budget)
    pending = {**cp, **xp, **hp, **mp}
    base = {key: copy.deepcopy(mark.get(key)) for key in pending}
    batch = {"sessions": claude + codex + hermes, "memory": memory, "pending": pending, "rejected": rejected}
    batch["batch_id"] = _batch_id(identity, batch, base)
    return batch


def advance(st, record, identity=None):
    identity = identity or current_identity()
    if not isinstance(record, dict) or record.get("version") != PENDING_VERSION:
        raise PendingError("curator advance requires the versioned pending record")
    if record.get("identity") != identity:
        raise PendingError("curator pending record belongs to a different run identity")
    batch, base = record.get("batch"), record.get("base")
    if not isinstance(batch, dict) or not isinstance(base, dict) or not isinstance(batch.get("pending"), dict):
        raise PendingError("curator pending record has an invalid batch")
    if record.get("batch_id") != _batch_id(identity, batch, base):
        raise PendingError("curator pending record identity does not match its contents")
    mark = st.load_watermark()
    for source, expected in base.items():
        if mark.get(source) != expected:
            raise PendingError(f"curator pending record is stale for {source}")
    mark.update(copy.deepcopy(batch["pending"]))
    st.save_watermark(mark)


def render_markdown(batch):
    sessions, memory = batch["sessions"], batch.get("memory", [])
    if not sessions and not memory:
        return "# No new turns since the previous run.\n"
    lines = ["# Transcript batch for the curator", ""]
    for sess in sessions:
        lines.extend([f"## {sess['head']} · {sess['cwd']} · session {sess['session_id'][:8]}", ""])
        for turn in sess["turns"]:
            who = "**User**" if turn["role"] == "user" else "**Agent**"
            stamp = f" _{turn['ts']}_" if turn.get("ts") else ""
            lines.extend([f"{who}{stamp}:", turn["text"], ""])
    if memory:
        lines.extend(["# Personal memory of the heads (new or changed)", ""])
        for mem in memory:
            lines.extend([f"## {mem['head']} · {mem['cwd']} · {Path(mem['path']).name}", "", mem["text"], ""])
    return "\n".join(lines)
