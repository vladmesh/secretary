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
BASELINE_VERSION = 1
_SCAFFOLD = re.compile(r"<(local-command|command-name|command-message|command-args)[\s>]")
_CODEX_CONTEXT_PREFIXES = ("# AGENTS.md instructions for ", "<environment_context>")
# These are the timestamp-less Claude records that are metadata controls, not transcript
# messages.  The exact top-level grammar is intentional: a new field, especially a
# message-shaped one, is retained until this list is deliberately reviewed.  `last-prompt`
# and `file-history-snapshot` are intentionally absent because their payloads can preserve
# user or workspace content.
_CLAUDE_NONCONTENT_CONTROL_KEYS = {
    "mode": frozenset({"type", "sessionId", "mode"}),
    "permission-mode": frozenset({"type", "sessionId", "permissionMode"}),
    "ai-title": frozenset({"type", "sessionId", "aiTitle"}),
    "atis-latch": frozenset({"type", "sessionId", "atis"}),
    "bridge-session": frozenset(
        {"type", "sessionId", "bridgeSessionId", "lastSequenceNum", "ownerAccountUuid", "ownerOrganizationUuid"}
    ),
    "cost-state": frozenset(
        {
            "type", "sessionId", "hasUnknownModelCost", "modelUsage", "startTime", "totalAPIDuration",
            "totalAPIDurationWithoutRetries", "totalCostUSD", "totalDuration", "totalLinesAdded",
            "totalLinesRemoved", "totalToolDuration",
        }
    ),
    "agent-name": frozenset({"type", "sessionId", "agentName"}),
    "custom-title": frozenset({"type", "sessionId", "customTitle"}),
}


class PendingError(ValueError):
    pass


class BaselineError(PendingError):
    """A baseline cannot prove that its requested cursor movement is safe."""


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


def _rfc3339(value: object, *, field: str, reject_future: bool = False) -> tuple[datetime, str]:
    """Parse a deliberately unambiguous RFC3339 timestamp and normalize it to UTC."""
    if not isinstance(value, str) or not value:
        raise BaselineError(f"{field} must be an RFC3339 timestamp with an explicit timezone")
    # `datetime.fromisoformat` accepts several useful but non-RFC3339 spellings.  Do not make an
    # operator's cutoff depend on those local or implementation-specific interpretations.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise BaselineError(f"{field} must be an RFC3339 timestamp with an explicit timezone")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise BaselineError(f"{field} is not a valid RFC3339 timestamp") from exc
    normalized = parsed.astimezone(UTC)
    if reject_future and normalized > datetime.now(UTC):
        raise BaselineError(f"{field} must not be in the future")
    return normalized, normalized.isoformat().replace("+00:00", "Z")


def validate_baseline_request(project: object, cutoff: object, actor: object, reason: object) -> dict:
    """Validate every operator-controlled baseline field before it can write state."""
    if not isinstance(project, str) or not project.strip():
        raise BaselineError("baseline requires a canonical --project")
    try:
        canonical = validate_project(project.strip())
    except PendingError as exc:
        raise BaselineError("baseline requires one registered canonical project") from exc
    # `validate_project` only returns None for its all-backlog input; keep this branch explicit so
    # the baseline command can never acquire all/unknown/global semantics by accident.
    if canonical is None or canonical in {"all", discover.ROUTE_UNKNOWN, discover.ROUTE_GLOBAL}:
        raise BaselineError("baseline requires one canonical project, not all, unknown, or global")
    _parsed, normalized_cutoff = _rfc3339(cutoff, field="baseline cutoff", reject_future=True)
    if not isinstance(actor, str) or not actor.strip():
        raise BaselineError("baseline requires a non-empty --actor")
    if not isinstance(reason, str) or not reason.strip():
        raise BaselineError("baseline requires a non-blank --reason")
    return {"project": canonical, "cutoff": normalized_cutoff, "actor": actor.strip(), "reason": reason.strip()}


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


# Baselines deliberately share the watermark representation and settlement owner with harvest, but
# are not harvest pending batches: a baseline has no fact-bearing input a curator head could safely
# advance.  Its own prepared record is therefore a recovery fence for every ordinary curator path.
def _baseline_timestamp(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return _rfc3339(value, field="source timestamp")[0]
    except BaselineError:
        return None


def _claude_noncontent_control(row: object) -> bool:
    """Recognize only an exact, non-message Claude control grammar.

    `parse_claude_lines` accepts curator signal only from `user` and `assistant`
    rows carrying a `message`.  The allowlist below excludes both types and demands
    the known complete key set, so a control cannot smuggle that signal shape across
    a timestamp-less cursor proof.
    """
    if not isinstance(row, dict):
        return False
    keys = _CLAUDE_NONCONTENT_CONTROL_KEYS.get(row.get("type"))
    return keys is not None and set(row) == keys


def _baseline_source_metadata(source: dict, *, outcome: str, reason: str, base=None, target=None, records: int = 0) -> dict:
    result = {
        "head": source.get("head"),
        "path": source.get("path"),
        "route": source.get("route", discover.ROUTE_UNKNOWN),
        "outcome": outcome,
        "reason": reason,
    }
    if source.get("session_id") is not None:
        result["session_id"] = source["session_id"]
    if base is not None or outcome == "affected":
        result["base"] = copy.deepcopy(base)
    if target is not None:
        result["target"] = copy.deepcopy(target)
    if records:
        result["record_count"] = records
    return result


def _baseline_jsonl_source(source: dict, mark: dict, parser, cutoff: datetime, limits: Limits) -> dict:
    """Find one exact JSONL cursor at the cutoff, without retaining source text."""
    path = Path(source["path"])
    base = copy.deepcopy(mark.get(source["path"]))
    try:
        stat = path.stat()
        start = _legacy_start(path, base or {}, stat, limits)
        if start is None:
            return _baseline_source_metadata(source, outcome="unselected", reason="no-unadvanced-records", base=base)
        last, records, proof_kinds = None, 0, set()
        retained_reason = None

        with path.open("rb") as fh:
            fh.seek(start)
            while True:
                end, data, oversized, incomplete = _read_record(fh, limits)
                if data is None:
                    if incomplete:
                        # A completed prefix is still safe; the incomplete tail remains ordinary
                        # harvest work.  There is no invented timestamp for the tail.
                        retained_reason = "incomplete-tail"
                        break
                    if oversized:
                        # `_read_record` retains only a bounded prefix of an oversized line.
                        # Without this row's own timestamp or control proof it cannot cross,
                        # regardless of timestamps on neighbouring rows.
                        retained_reason = "oversized-record"
                        break
                    break
                try:
                    row = json.loads(data)
                except (TypeError, json.JSONDecodeError):
                    retained_reason = "unparseable-record"
                    break
                timestamp = _baseline_timestamp(row.get("timestamp") if isinstance(row, dict) else None)
                if timestamp is None:
                    if source.get("head") == "claude" and _claude_noncontent_control(row):
                        proof_kinds.add("noncontent-control")
                    else:
                        retained_reason = "unapproved-timestamp-less-record"
                        break
                elif timestamp > cutoff:
                    retained_reason = "post-cutoff"
                    break
                else:
                    proof_kinds.add("timestamp")
                # Reuse the normal parser only as a format guard.  Both emitted and non-emitting
                # records receive their own cutoff or non-content-control proof.
                parser([data.decode("utf-8", errors="replace")])
                last, records = end, records + 1
    except (OSError, ValueError):
        return _baseline_source_metadata(source, outcome="unselected", reason="unreadable-source", base=base)
    if last is None:
        return _baseline_source_metadata(source, outcome="unselected", reason=retained_reason or "post-cutoff", base=base)
    target = {"offset": last, "mtime": stat.st_mtime, "size": stat.st_size}
    proof = "per-record-timestamp-or-noncontent-control" if proof_kinds == {"noncontent-control", "timestamp"} else (
        "per-record-noncontent-control" if proof_kinds == {"noncontent-control"} else "at-or-before-cutoff"
    )
    reason = proof
    if retained_reason:
        reason = f"complete-prefix-{proof}; {retained_reason}-tail-retained"
    return _baseline_source_metadata(
        source,
        outcome="affected",
        reason=reason,
        base=base,
        target=target,
        records=records,
    )


def _baseline_hermes_source(source: dict, mark: dict, cutoff: datetime, limits: Limits) -> dict:
    key = f"hermes:{source['session_id']}"
    base = copy.deepcopy(mark.get(key))
    previous = (base or {}).get("last_id", 0)
    if not isinstance(previous, int) or previous < 0:
        return _baseline_source_metadata(source, outcome="unselected", reason="invalid-watermark", base=base)
    try:
        rows = discover.hermes_messages(source["session_id"], previous, 2**31 - 1, limits.max_record_bytes)
    except Exception:
        return _baseline_source_metadata(source, outcome="unselected", reason="unreadable-source", base=base)
    last, records = None, 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), int):
            return _baseline_source_metadata(source, outcome="unselected", reason="unparseable-record", base=base)
        timestamp = _baseline_timestamp(row.get("timestamp"))
        if timestamp is None:
            return _baseline_source_metadata(source, outcome="unselected", reason="missing-or-invalid-timestamp", base=base)
        if timestamp > cutoff:
            break
        # Keep the normal source-format parser in the proof path, while never retaining content.
        parse_hermes_rows([row])
        last, records = row["id"], records + 1
    if last is None:
        return _baseline_source_metadata(source, outcome="unselected", reason="post-cutoff-or-empty", base=base)
    return _baseline_source_metadata(
        source, outcome="affected", reason="at-or-before-cutoff", base=base, target={"last_id": last}, records=records
    )


def _baseline_memory_source(source: dict, mark: dict, cutoff: datetime) -> dict:
    key = source["path"]
    base = copy.deepcopy(mark.get(key))
    try:
        stat = Path(key).stat()
    except OSError:
        return _baseline_source_metadata(source, outcome="unselected", reason="unreadable-source", base=base)
    current = {"mtime": stat.st_mtime, "size": stat.st_size}
    if base == current:
        return _baseline_source_metadata(source, outcome="unselected", reason="no-unadvanced-records", base=base)
    try:
        modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return _baseline_source_metadata(source, outcome="unselected", reason="invalid-source-timestamp", base=base)
    if modified > cutoff:
        return _baseline_source_metadata(source, outcome="unselected", reason="post-cutoff", base=base)
    return _baseline_source_metadata(source, outcome="affected", reason="at-or-before-cutoff", base=base, target=current, records=1)


def _baseline_plan(st, project: str, cutoff: datetime, limits: Limits | None = None) -> tuple[list[dict], list[dict]]:
    """Select only sources already routed by the canonical discovery boundary."""
    mark, limits = st.load_watermark(), limits or Limits.from_env()
    affected, unselected = [], []
    source_sets = (
        (discover.claude_sessions(), lambda s: _baseline_jsonl_source(s, mark, parse_claude_lines, cutoff, limits)),
        (discover.codex_sessions(), lambda s: _baseline_jsonl_source(s, mark, parse_codex_lines, cutoff, limits)),
        (discover.hermes_sessions(), lambda s: _baseline_hermes_source(s, mark, cutoff, limits)),
        (discover.all_memory_files(), lambda s: _baseline_memory_source(s, mark, cutoff)),
    )
    for sources, inspect in source_sets:
        for source in sorted(sources, key=lambda item: (item.get("head", ""), item.get("path", ""), str(item.get("session_id", "")))):
            if source.get("route", discover.ROUTE_UNKNOWN) != project:
                unselected.append(_baseline_source_metadata(source, outcome="unselected", reason="route-not-selected"))
                continue
            outcome = inspect(source)
            (affected if outcome["outcome"] == "affected" else unselected).append(outcome)
    return affected, unselected


def _baseline_identity(project: str, cutoff: str, affected: list[dict]) -> str:
    targets = [
        {key: source[key] for key in ("head", "path", "route", "session_id", "base", "target") if key in source}
        for source in affected
    ]
    payload = json.dumps(
        {"version": BASELINE_VERSION, "project": project, "cutoff": cutoff, "targets": targets},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"curator-baseline-v{BASELINE_VERSION}:{hashlib.sha256(payload).hexdigest()}"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _append_baseline_audit(st, record: dict, phase: str) -> None:
    entry = {
        "version": BASELINE_VERSION,
        "kind": "curator-baseline",
        "phase": phase,
        "identity": record["identity"],
        "project": record["project"],
        "cutoff": record["cutoff"],
        "actor": record["actor"],
        "reason": record["reason"],
        "operation_at": record["operation_at"],
        "affected_sources": record["affected_sources"],
        "unselected_sources": record["unselected_sources"],
    }
    st.ensure_dir()
    encoded = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(st.baseline_audit_file, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _baseline_audit(st) -> list[dict]:
    if not st.baseline_audit_file.is_file():
        return []
    try:
        lines = st.baseline_audit_file.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError("curator baseline audit is unreadable; preserve it for recovery") from exc
    if not all(isinstance(record, dict) and record.get("kind") == "curator-baseline" for record in records):
        raise BaselineError("curator baseline audit has an unsupported record")
    return records


def _baseline_summary(record: dict, *, status: str) -> dict:
    return {
        "baseline_id": record["identity"],
        "project": record["project"],
        "cutoff": record["cutoff"],
        "affected_source_count": len(record["affected_sources"]),
        "unselected_source_count": len(record["unselected_sources"]),
        "status": status,
    }


def _read_baseline_pending(st) -> dict:
    try:
        record = json.loads(st.baseline_pending_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError("curator prepared baseline is unreadable; preserve it for recovery") from exc
    required = {"version", "identity", "project", "cutoff", "actor", "reason", "operation_at", "affected_sources", "unselected_sources"}
    if not isinstance(record, dict) or record.get("version") != BASELINE_VERSION or not required <= record.keys():
        raise BaselineError("curator prepared baseline is unsupported; preserve it for recovery")
    if not isinstance(record["affected_sources"], list) or not isinstance(record["unselected_sources"], list):
        raise BaselineError("curator prepared baseline has invalid source metadata")
    expected = _baseline_identity(record["project"], record["cutoff"], record["affected_sources"])
    if record["identity"] != expected:
        raise BaselineError("curator prepared baseline identity does not match its cursor targets")
    return record


def require_no_prepared_baseline(st) -> None:
    """Prevent harvest or advance from crossing an operator's recoverable baseline decision."""
    if st.baseline_pending_file.is_file():
        _read_baseline_pending(st)
        raise BaselineError("curator prepared baseline must be recovered by repeating curator baseline")


def _normal_pending_blocks_baseline(st) -> None:
    if not st.pending_file.is_file():
        return
    try:
        raw = json.loads(st.pending_file.read_text(encoding="utf-8"))
        identity = raw.get("identity") if isinstance(raw, dict) else None
        selected = raw.get("selector") if isinstance(raw, dict) else None
        project = None if selected == "all" else selected
        if not isinstance(identity, dict):
            raise PendingError("invalid identity")
        read_pending(st, identity, project)
    except PendingError as exc:
        raise BaselineError("curator pending record blocks baseline and is not safely recoverable") from exc
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise BaselineError("curator pending record blocks baseline and is unreadable") from exc
    raise BaselineError("curator fact-bearing pending batch must be advanced before baseline")


def _settle_baseline(st, record: dict) -> dict:
    audit = _baseline_audit(st)
    intent = any(item.get("identity") == record["identity"] and item.get("phase") == "prepared" for item in audit)
    settled = any(item.get("identity") == record["identity"] and item.get("phase") == "settled" for item in audit)
    base = {source["path"] if source["head"] != "hermes" else f"hermes:{source['session_id']}": source.get("base") for source in record["affected_sources"]}
    targets = {source["path"] if source["head"] != "hermes" else f"hermes:{source['session_id']}": source["target"] for source in record["affected_sources"]}
    mark = st.load_watermark()
    at_base = all(mark.get(key) == value for key, value in base.items())
    at_target = all(mark.get(key) == value for key, value in targets.items())
    if not at_base and not at_target:
        raise BaselineError("curator prepared baseline is stale because a base watermark changed")
    if not intent:
        _append_baseline_audit(st, record, "prepared")
    if not at_target:
        updated = copy.deepcopy(mark)
        updated.update(copy.deepcopy(targets))
        st.save_watermark(updated)
    if not settled:
        _append_baseline_audit(st, record, "settled")
    try:
        st.baseline_pending_file.unlink()
    except FileNotFoundError:
        pass
    return _baseline_summary(record, status="replayed" if settled else "settled")


def baseline(st, project: object, cutoff: object, actor: object, reason: object, *, limits: Limits | None = None) -> dict:
    """Prepare, audit, and settle one project-only baseline under the caller's transaction."""
    request = validate_baseline_request(project, cutoff, actor, reason)
    _normal_pending_blocks_baseline(st)
    if st.baseline_pending_file.is_file():
        record = _read_baseline_pending(st)
        if {key: record[key] for key in request} != request:
            raise BaselineError("curator prepared baseline belongs to a different request")
        return _settle_baseline(st, record)
    for audit in _baseline_audit(st):
        if audit.get("phase") == "settled" and all(audit.get(key) == value for key, value in request.items()):
            record = {
                "identity": audit["identity"],
                "project": audit["project"],
                "cutoff": audit["cutoff"],
                "affected_sources": audit["affected_sources"],
                "unselected_sources": audit["unselected_sources"],
            }
            return _baseline_summary(record, status="replayed")
    cutoff_at, _ = _rfc3339(request["cutoff"], field="baseline cutoff")
    affected, unselected = _baseline_plan(st, request["project"], cutoff_at, limits)
    record = {
        "version": BASELINE_VERSION,
        "project": request["project"],
        "cutoff": request["cutoff"],
        "actor": request["actor"],
        "reason": request["reason"],
        "operation_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "affected_sources": affected,
        "unselected_sources": unselected,
    }
    record["identity"] = _baseline_identity(record["project"], record["cutoff"], affected)
    _atomic_json(st.baseline_pending_file, record)
    return _settle_baseline(st, record)


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
