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

PENDING_VERSION = 3
_SCAFFOLD = re.compile(r"<(local-command|command-name|command-message|command-args)[\s>]")
_CODEX_CONTEXT_PREFIXES = ("# AGENTS.md instructions for ", "<environment_context>")


class PendingError(ValueError):
    pass


def selector(project: str | None) -> str:
    """Normalize the explicit all-backlog selector used in signed pending records."""
    return project or "all"


def validate_project(project: str | None) -> str | None:
    """Reject a selector that is not a canonical id in the selected instance registry."""
    if project is None:
        return None
    if project not in discover.registered_project_ids():
        raise PendingError(f"unknown curator project {project!r}")
    return project


def _selected(sources: list[dict], project: str | None) -> list[dict]:
    """Filter before ordering and budget selection; all-backlog retains unknown and global."""
    return [source for source in sources if project is None or source.get("route", discover.ROUTE_UNKNOWN) == project]


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
        return fh.tell(), None, False, False
    if not data.endswith(b"\n"):
        # Discard a complete oversized record without materializing it.  An EOF
        # before its newline is a live incomplete record, which must remain for
        # the next harvest rather than becoming part of the cursor.
        while data and not data.endswith(b"\n"):
            data = fh.readline(limits.max_record_bytes + 1)
        return fh.tell(), None, bool(data), not bool(data)
    return fh.tell(), data, False, False


def _jsonl_source(sess, mark, parser: Callable[[list[str]], list[dict]], budget):
    path = Path(sess["path"])
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None, None, False, False
    start = _legacy_start(path, mark.get(sess["path"], {}), stat, budget.limits)
    if start is None:
        return None, None, False, False
    turns, last = [], None
    with path.open("rb") as fh:
        fh.seek(start)
        for _ in range(budget.limits.max_rows_per_source):
            end, data, oversized, incomplete = _read_record(fh, budget.limits)
            if data is None:
                if incomplete:
                    cursor = (
                        {"offset": last, "mtime": stat.st_mtime, "size": stat.st_size}
                        if last is not None
                        else None
                    )
                    return ({**sess, "turns": turns} if turns else None), cursor, False, True
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
                return ({**sess, "turns": turns} if turns else None), cursor, True, False
            for turn in parsed:
                turn["text"] = redact(turn["text"])
            budget.add(len(parsed), size, not turns)
            turns.extend(parsed)
            last = end
    cursor = {"offset": last, "mtime": stat.st_mtime, "size": stat.st_size} if last is not None else None
    return ({**sess, "turns": turns} if turns else None), cursor, False, False


def _jsonl_sessions(mark, sessions, parser, budget):
    output, pending, partial_sources = [], {}, []
    for sess in sorted(sessions, key=lambda s: (s["head"], s["path"], s["session_id"])):
        entry, cursor, stopped, incomplete = _jsonl_source(sess, mark, parser, budget)
        if entry:
            output.append(entry)
        if cursor:
            pending[sess["path"]] = cursor
        if incomplete:
            # An EOF tail belongs only to this source.  Its cursor remains at the
            # last complete record while later sessions may still safely settle.
            partial_sources.append({"head": sess["head"], "path": sess["path"], "session_id": sess["session_id"]})
        if stopped:
            return output, pending, True, partial_sources
    return output, pending, False, partial_sources


def _hermes_sessions(mark, budget, sessions=None):
    output, pending = [], {}
    for sess in sorted(sessions if sessions is not None else discover.hermes_sessions(), key=lambda s: (s["head"], s["path"], s["session_id"])):
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


def harvest_memory_files(mark, budget, files=None):
    entries, pending, rejected = [], {}, []
    for mem in sorted(files if files is not None else discover.all_memory_files(), key=lambda m: (m["head"], m["path"])):
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


def _batch_id(identity, batch, base, selected_project):
    payload = json.dumps(
        {"identity": identity, "batch": batch, "base": base, "selector": selector(selected_project)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def pending_record(batch, identity, base, project=None):
    payload = copy.deepcopy(batch)
    payload.pop("batch_id", None)
    return {
        "version": PENDING_VERSION,
        "identity": copy.deepcopy(identity),
        "base": copy.deepcopy(base),
        "selector": selector(project),
        "batch": payload,
        "batch_id": _batch_id(identity, payload, base, project),
    }


def read_pending(st, identity=None, project=None):
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
    if record.get("selector") != selector(project):
        raise PendingError("curator pending record belongs to a different project selector")
    if record.get("batch_id") != _batch_id(identity, batch, base, project):
        raise PendingError("curator pending record identity does not match its contents")
    if not batch.get("sessions") and not batch.get("memory"):
        raise PendingError("curator pending record has no fact-bearing input")
    if batch.get("incomplete"):
        raise PendingError("curator pending record has incomplete input")
    return record


def harvest(st, identity=None, limits=None, project=None):
    identity = identity or current_identity()
    project = validate_project(project)
    if st.pending_file.is_file():
        record = read_pending(st, identity, project)
        return {**copy.deepcopy(record["batch"]), "batch_id": record["batch_id"]}
    mark, budget = st.load_watermark(), _Budget(limits or Limits.from_env())
    claude, cp, stopped, cpartial = _jsonl_sessions(
        mark, _selected(discover.claude_sessions(), project), parse_claude_lines, budget
    )
    if stopped:
        codex, xp, xstop, xpartial = [], {}, True, []
    else:
        codex, xp, xstop, xpartial = _jsonl_sessions(
            mark, _selected(discover.codex_sessions(), project), parse_codex_lines, budget
        )
    hermes, hp, hstop = (
        ([], {}, True)
        if stopped or xstop
        else _hermes_sessions(mark, budget, _selected(discover.hermes_sessions(), project))
    )
    memory, mp, rejected, _ = (
        ([], {}, [], True)
        if stopped or xstop or hstop
        else harvest_memory_files(mark, budget, _selected(discover.all_memory_files(), project))
    )
    pending = {**cp, **xp, **hp, **mp}
    base = {key: copy.deepcopy(mark.get(key)) for key in pending}
    batch = {
        "sessions": claude + codex + hermes,
        "memory": memory,
        "pending": pending,
        "rejected": rejected,
        # This is observability only.  It does not invalidate safe records or
        # cursors from this or later sources.
        "partial_sources": cpartial + xpartial,
        "project": selector(project),
    }
    batch["batch_id"] = _batch_id(identity, batch, base, project)
    return batch


def advance(st, record, identity=None, project=None):
    identity = identity or current_identity()
    if not isinstance(record, dict) or record.get("version") != PENDING_VERSION:
        raise PendingError("curator advance requires the versioned pending record")
    if record.get("identity") != identity:
        raise PendingError("curator pending record belongs to a different run identity")
    batch, base = record.get("batch"), record.get("base")
    if not isinstance(batch, dict) or not isinstance(base, dict) or not isinstance(batch.get("pending"), dict):
        raise PendingError("curator pending record has an invalid batch")
    if record.get("selector") != selector(project):
        raise PendingError("curator pending record belongs to a different project selector")
    if record.get("batch_id") != _batch_id(identity, batch, base, project):
        raise PendingError("curator pending record identity does not match its contents")
    if batch.get("incomplete"):
        raise PendingError("curator pending record has incomplete input")
    mark = st.load_watermark()
    for source, expected in base.items():
        if mark.get(source) != expected:
            raise PendingError(f"curator pending record is stale for {source}")
    mark.update(copy.deepcopy(batch["pending"]))
    st.save_watermark(mark)


def _timestamp(value) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _stat_timestamp(stat) -> str:
    return datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()


def _jsonl_backlog(path: Path, previous: dict, parser, limits: Limits) -> tuple[bool, int, list[str]]:
    """Metadata about complete unadvanced JSONL records, without returning their text."""
    try:
        stat = path.stat()
        start = _legacy_start(path, previous, stat, limits)
    except (OSError, ValueError):
        return False, 0, []
    if start is None:
        return False, 0, []
    records, turns, timestamps = False, 0, []
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            while True:
                _end, data, _oversized, incomplete = _read_record(fh, limits)
                if data is None:
                    if incomplete:
                        break
                    # At EOF, `_read_record` has no data and is not an oversized record.
                    if not _oversized:
                        break
                    records = True
                    continue
                records = True
                for turn in parser([data.decode("utf-8", errors="replace")]):
                    turns += 1
                    if timestamp := _timestamp(turn.get("ts")):
                        timestamps.append(timestamp)
    except OSError:
        return False, 0, []
    return records, turns, timestamps or ([_stat_timestamp(stat)] if records else [])


def _add_backlog_group(groups: dict, route: str, head: str, *, sessions=0, turns=0, memory=0, timestamps=()):
    group = groups.setdefault(
        (route, head),
        {"project": route, "head": head, "session_count": 0, "signal_turn_count": 0, "memory_file_count": 0, "timestamps": []},
    )
    group["session_count"] += sessions
    group["signal_turn_count"] += turns
    group["memory_file_count"] += memory
    group["timestamps"].extend(timestamp for timestamp in timestamps if timestamp)


def backlog(st, project: str | None = None, limits: Limits | None = None) -> dict:
    """Summarize unadvanced sources without creating pending work or changing state.

    This intentionally returns only aggregate metadata.  It reads enough source structure to count
    signal turns, but neither renders nor retains transcript or personal-memory text.
    """
    project = validate_project(project)
    mark, limits, groups = st.load_watermark(), limits or Limits.from_env(), {}
    for sess, parser in (
        *((source, parse_claude_lines) for source in _selected(discover.claude_sessions(), project)),
        *((source, parse_codex_lines) for source in _selected(discover.codex_sessions(), project)),
    ):
        records, turns, timestamps = _jsonl_backlog(Path(sess["path"]), mark.get(sess["path"], {}), parser, limits)
        if records:
            _add_backlog_group(
                groups, sess.get("route", discover.ROUTE_UNKNOWN), sess["head"], sessions=1, turns=turns, timestamps=timestamps
            )
    for sess in _selected(discover.hermes_sessions(), project):
        key = f"hermes:{sess['session_id']}"
        rows = discover.hermes_messages(sess["session_id"], mark.get(key, {}).get("last_id", 0), 2**31 - 1, limits.max_record_bytes)
        if not rows:
            continue
        turns = parse_hermes_rows(rows)
        timestamps = [_timestamp(turn.get("ts")) for turn in turns]
        _add_backlog_group(
            groups, sess.get("route", discover.ROUTE_UNKNOWN), sess["head"], sessions=1, turns=len(turns), timestamps=timestamps
        )
    for mem in _selected(discover.all_memory_files(), project):
        try:
            stat = Path(mem["path"]).stat()
        except OSError:
            continue
        previous = mark.get(mem["path"], {})
        if previous.get("mtime") == stat.st_mtime and previous.get("size") == stat.st_size:
            continue
        _add_backlog_group(
            groups,
            mem.get("route", discover.ROUTE_UNKNOWN),
            mem["head"],
            memory=1,
            timestamps=[_stat_timestamp(stat)],
        )
    result = []
    for group in groups.values():
        timestamps = sorted(group.pop("timestamps"))
        group["oldest"] = timestamps[0] if timestamps else None
        group["newest"] = timestamps[-1] if timestamps else None
        result.append(group)
    return {"project": selector(project), "groups": sorted(result, key=lambda group: (group["project"], group["head"]))}


def render_markdown(batch):
    sessions, memory = batch["sessions"], batch.get("memory", [])
    partial_sources = batch.get("partial_sources", [])
    if not sessions and not memory:
        if partial_sources:
            lines = ["# No new complete turns since the previous run.", "", "## Source-local partial JSONL tails", ""]
            lines.extend(
                f"- {source['head']} session {source['session_id'][:8]}: {source['path']}"
                for source in partial_sources
            )
            return "\n".join(lines) + "\n"
        return "# No new turns since the previous run.\n"
    lines = ["# Transcript batch for the curator", ""]
    for sess in sessions:
        lines.extend(
            [f"## {sess['head']} · {sess.get('route', discover.ROUTE_UNKNOWN)} · {sess['cwd']} · session {sess['session_id'][:8]}", ""]
        )
        for turn in sess["turns"]:
            who = "**User**" if turn["role"] == "user" else "**Agent**"
            stamp = f" _{turn['ts']}_" if turn.get("ts") else ""
            lines.extend([f"{who}{stamp}:", turn["text"], ""])
    if memory:
        lines.extend(["# Personal memory of the heads (new or changed)", ""])
        for mem in memory:
            lines.extend(
                [f"## {mem['head']} · {mem.get('route', discover.ROUTE_UNKNOWN)} · {mem['cwd']} · {Path(mem['path']).name}", "", mem["text"], ""]
            )
    if partial_sources:
        lines.extend(["## Source-local partial JSONL tails", ""])
        lines.extend(
            f"- {source['head']} session {source['session_id'][:8]}: {source['path']}"
            for source in partial_sources
        )
    return "\n".join(lines)
